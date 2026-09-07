"""The chat redesign of the Agent lane (DESIGN.md §6.4, §5; 2026-09-06).

Offline, model faked: test_agent's fake Runner drives the REAL tool closures
through `run_streamed`, so the events the chat window renders are built from
the SDK's own item classes. Database work runs inside one force-rollback
transaction (isolated_db) against the live database.

1. services/agent: log_estimate runs once per utterance and refuses bad
   amounts in code; wire_events turns the SDK's tool_called / tool_output /
   web_search items into the chat's events; run_agent emits them in order
   and the reply last.
2. routes/logs: log_estimate stages component rows flagged estimated=
   "portion" with the note, after log_food's rows (item indexes never
   collide); a flagged row commits only once confirmed and learns no alias.
3. routes/agent: POST /api/agent streams tool_call → tool_result → reply →
   done as NDJSON; the voice variant leads with the transcript; a same-id
   retry re-attaches to the live run's stream (nothing runs twice), and once
   the run has aged out it gets the stored state as one done marked replay
   (the voice replay leads with the transcript the row kept); a failure
   inside the stream is an error event.

Run:  cd backend && uv run --with pytest pytest tests/test_agent_chat.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from agents.items import ToolCallItem, ToolCallOutputItem  # noqa: E402
from openai.types.responses import ResponseFunctionToolCall, ResponseFunctionWebSearch  # noqa: E402
from openai.types.responses.response_function_web_search import (  # noqa: E402
    ActionSearch,
    ActionSearchSource,
)

from models import CommitRequest, LogCreate  # noqa: E402
from routes import agent as agent_routes  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from routes.photo import ConfirmEstimateRequest, confirm_estimate  # noqa: E402
from services import agent as agent_svc  # noqa: E402
from test_agent import NOW, TZ, _deps, _fake_runner, _invoke, _parsed  # noqa: E402
from test_tenancy import (  # noqa: E402
    _scalar,
    _stub_pipeline,
    insert_alias,
    insert_food,
    isolated_db,
)


def _run(coro):
    return asyncio.run(coro)


SHRIMP = {"phrase": "shrimp, cooked", "qty": 400, "unit": "g", "note": "~50 shrimp at ~8 g each"}
SAUCE = {"phrase": "cocktail sauce", "qty": 60, "unit": "g", "note": "the small tub in the tray"}
BASIS = "the whole 2 lb Costco shrimp platter, sauce included"


def _log(text: str) -> LogCreate:
    return LogCreate(id=uuid4(), text=text, captured_at=NOW, tz=TZ, agent=True)


async def _drain(resp) -> list[dict]:
    """The NDJSON body of a streaming response, one event per line."""
    raw = b"".join([chunk async for chunk in resp.body_iterator])
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. services/agent — the estimate tool and the wire events
# ---------------------------------------------------------------------------


def test_log_estimate_tool_runs_once_and_refuses_bad_amounts_in_code():
    async def scenario():
        deps = _deps()
        state: dict = {}
        agent = agent_svc.build_agent(deps, state)
        assert {t.name for t in agent.tools if hasattr(t, "name")} >= {"log_food", "log_estimate", "log_weight"}
        assert any(type(t).__name__ == "WebSearchTool" for t in agent.tools)

        out = await _invoke(agent, "log_estimate", items=[SHRIMP, SAUCE], basis=BASIS)
        assert out["basis"] == BASIS and [i["phrase"] for i in out["items"]] == ["shrimp, cooked", "cocktail sauce"]
        assert state["estimate_calls"] == 1
        (items, basis, meal_hint, when_hint), = deps.calls["estimate"]
        assert basis == BASIS and items[0]["qty"] == 400 and items[0]["unit"] == "g"
        assert meal_hint is None and when_hint is None  # none spoken, none passed

        # once per utterance — a second call stages nothing again
        out = await _invoke(agent, "log_estimate", items=[SAUCE], basis="again")
        assert out["error"] == "already_called" and len(deps.calls["estimate"]) == 1

        # a zero or negative amount is refused before anything is staged
        state2: dict = {}
        agent2 = agent_svc.build_agent(_deps(), state2)
        out = await _invoke(agent2, "log_estimate", items=[{**SHRIMP, "qty": 0}], basis=BASIS)
        assert out["error"] == "invalid_qty" and "shrimp" in out["message"]
        assert state2["estimate_calls"] == 0
        out = await _invoke(agent2, "log_estimate", items=[], basis=BASIS)
        assert out["error"] == "empty" and state2["estimate_calls"] == 0

        # the spoken meal / time words ride through, verbatim (§7.2)
        deps3 = _deps()
        agent3 = agent_svc.build_agent(deps3, {})
        out = await _invoke(agent3, "log_estimate", items=[SAUCE], basis=BASIS,
                            meal_hint="breakfast", when_hint=" yesterday morning ")
        assert out["meal"] == "breakfast"
        assert deps3.calls["estimate"][0][2:] == ("breakfast", "yesterday morning")

        # a demo account's agent has no web lane (§8.9)
        demo = agent_svc.build_agent(_deps(web_lane=False), {})
        assert not any(type(t).__name__ == "WebSearchTool" for t in demo.tools)
        assert "web_search is not available" in demo.instructions

    _run(scenario())


def test_wire_events_translate_function_calls_outputs_and_web_search():
    agent = agent_svc.build_agent(_deps(), {})
    names: dict = {}

    def ev(name, item):
        return SimpleNamespace(type="run_item_stream_event", name=name, item=item)

    call = ResponseFunctionToolCall(
        id="fc_1", call_id="call_1", name="log_food",
        arguments='{"text": "two eggs"}', type="function_call",
    )
    out = agent_svc.wire_events(ev("tool_called", ToolCallItem(raw_item=call, agent=agent)), names)
    assert out == [{"type": "tool_call", "id": "call_1", "name": "log_food", "args": {"text": "two eggs"}}]

    result = ToolCallOutputItem(
        raw_item={"type": "function_call_output", "call_id": "call_1", "output": '{"meal": "lunch"}'},
        output='{"meal": "lunch"}', agent=agent,
    )
    out = agent_svc.wire_events(ev("tool_output", result), names)
    assert out == [{"type": "tool_result", "id": "call_1", "name": "log_food", "output": {"meal": "lunch"}}]

    # the hosted search: its call and the sources it consulted arrive together
    search = ResponseFunctionWebSearch(
        id="ws_1", status="completed", type="web_search_call",
        action=ActionSearch(
            type="search", query="costco shrimp platter",
            sources=[ActionSearchSource(type="url", url="https://www.costco.com/shrimp-platter")],
        ),
    )
    out = agent_svc.wire_events(ev("tool_called", ToolCallItem(raw_item=search, agent=agent)), names)
    assert out == [
        {"type": "tool_call", "id": "ws_1", "name": "web_search", "args": {"query": "costco shrimp platter"}},
        {"type": "tool_result", "id": "ws_1", "name": "web_search",
         "output": {"sources": ["https://www.costco.com/shrimp-platter"]}},
    ]

    # a search that consulted nothing still resolves its line: the result
    # arrives with the call, sources empty
    quiet = ResponseFunctionWebSearch(
        id="ws_2", status="completed", type="web_search_call",
        action=ActionSearch(type="search", query="nothing found", sources=None),
    )
    out = agent_svc.wire_events(ev("tool_called", ToolCallItem(raw_item=quiet, agent=agent)), names)
    assert [e["type"] for e in out] == ["tool_call", "tool_result"]
    assert out[1]["output"] == {"sources": []}

    # anything else on the stream is not the chat's business
    assert agent_svc.wire_events(SimpleNamespace(type="raw_response_event"), names) == []
    assert agent_svc.wire_events(ev("reasoning_item_created", SimpleNamespace()), names) == []
    # an oversized output is summarised, never shipped whole
    big = ToolCallOutputItem(
        raw_item={"type": "function_call_output", "call_id": "call_9", "output": "x"},
        output="y" * 10_000, agent=agent,
    )
    (only,) = agent_svc.wire_events(ev("tool_output", big), names)
    assert only["output"]["truncated"] is True and len(only["output"]["summary"]) < 5_000


def test_run_agent_emits_tool_calls_results_then_the_reply(monkeypatch):
    async def scenario():
        deps = _deps()
        runner = _fake_runner(
            [("log_estimate", {"items": [SHRIMP, SAUCE], "basis": BASIS}),
             ("log_weight", {"weight": 180.4, "unit": "lb"})],
            "Staged two estimated rows and logged 180.4 lb.",
        )
        monkeypatch.setattr(agent_svc, "Runner", runner)
        seen: list[dict] = []

        async def emit(ev):
            seen.append(ev)

        run = await agent_svc.run_agent("a platter of costco shrimp and I weigh 180.4", deps, emit)
        assert [e["type"] for e in seen] == ["tool_call", "tool_result", "tool_call", "tool_result", "reply"]
        assert seen[0]["name"] == "log_estimate" and seen[0]["args"]["basis"] == BASIS
        assert seen[1]["output"]["items"][0]["phrase"] == "shrimp, cooked"
        assert seen[2]["name"] == "log_weight" and seen[3]["output"]["weight"] == 180.4
        assert seen[4] == {"type": "reply", "text": "Staged two estimated rows and logged 180.4 lb."}
        assert run.estimate_calls == 1 and run.food_calls == 0 and run.weight is not None
        # without emit the same run is silent
        run2 = await agent_svc.run_agent("again", deps)
        assert run2.reply and len(seen) == 5

    _run(scenario())


# ---------------------------------------------------------------------------
# 2. routes/logs — flagged rows after the food rows; the commit gate
# ---------------------------------------------------------------------------


def test_agent_lane_estimate_rows_are_flagged_and_indexed_after_the_food(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            async def decompose(text, ctx):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            runner = _fake_runner(
                [("log_food", {"text": "two eggs"}),
                 ("log_estimate", {"items": [SHRIMP, SAUCE], "basis": BASIS})],
                "Staged the eggs and an estimate for the platter.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = _log("two eggs and a platter of costco shrimp")
            resp = await logs_routes.create_log(body)

            assert [it.item_index for it in resp.items] == [0, 1, 2]
            eggs, shrimp, sauce = resp.items
            assert (eggs.phrase, eggs.estimated, eggs.estimate_note) == ("eggs", "none", None)
            assert shrimp.phrase == "shrimp, cooked" and shrimp.estimated == "portion"
            assert shrimp.estimate_note == "~50 shrimp at ~8 g each"
            assert sauce.phrase == "cocktail sauce" and sauce.estimated == "portion"
            assert all(it.status in ("resolved", "searching") for it in resp.items)
            assert all(it.confidence != "high" for it in (shrimp, sauce))  # a guess is never high
            assert resp.agent is not None and resp.agent.reply.startswith("Staged the eggs")

            rows = await (await conn.execute(
                "SELECT item_index, phrase, status, staged FROM item_resolutions "
                "WHERE log_id = %s ORDER BY item_index", (body.id,),
            )).fetchall()
            assert [r["item_index"] for r in rows] == [0, 1, 2]
            assert [r["staged"].get("estimated", "none") for r in rows] == ["none", "portion", "portion"]
            for r in rows[1:]:
                if r["status"] == "searching":  # the amount rides to tier 3 (§7.3 rule 1)
                    assert (r["staged"]["spoken_qty"], r["staged"]["spoken_unit"]) in ((400, "g"), (60, "g"))
            # log_food's timing survives the second staging call on the same log
            log = await (await conn.execute("SELECT decompose_ms FROM logs WHERE id = %s", (body.id,))).fetchone()
            assert log["decompose_ms"] is not None

    _run(scenario())


def test_estimate_rows_honor_the_spoken_meal_and_time(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)  # the estimate is the only staging
            runner = _fake_runner(
                [("log_estimate", {"items": [SHRIMP], "basis": BASIS,
                                   "meal_hint": None, "when_hint": "yesterday lunch"})],
                "Staged an estimate for yesterday's lunch.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = _log("yesterday at lunch I had a platter of costco shrimp")  # 08:30 → breakfast by the clock
            resp = await logs_routes.create_log(body)
            (row,) = resp.items
            # §7.2: the when-hint moves the meal and the diary day; captured_at is untouched
            assert row.meal.value == "lunch" and resp.meal.value == "lunch"
            stored = await (await conn.execute(
                "SELECT staged, (SELECT captured_at FROM logs WHERE id = %s) AS captured_at "
                "FROM item_resolutions WHERE log_id = %s", (body.id, body.id),
            )).fetchone()
            logged_at = datetime.fromisoformat(stored["staged"]["logged_at"])
            local = logged_at.astimezone(ZoneInfo(TZ))
            assert (local.date(), local.hour, local.minute) == ((NOW.astimezone(ZoneInfo(TZ)) - timedelta(days=1)).date(), 12, 30)
            assert stored["captured_at"] == NOW
            assert stored["staged"]["estimated"] == "portion"

            # an explicit meal word outranks the shift's meal; an unknown when-hint is noted, not guessed
            runner2 = _fake_runner(
                [("log_estimate", {"items": [SAUCE], "basis": BASIS,
                                   "meal_hint": "dinner", "when_hint": "a while ago"})],
                "Staged.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner2)
            body2 = _log("a while ago for dinner, some cocktail sauce from the deli")
            resp2 = await logs_routes.create_log(body2)
            assert resp2.items[0].meal.value == "dinner" and resp2.meal.value == "dinner"
            staged2 = await _scalar(conn, "SELECT staged FROM item_resolutions WHERE log_id = %s", (body2.id,))
            assert "logged_at" not in staged2 and "not recognized" in staged2["when_hint_note"]

    _run(scenario())


def test_flagged_estimate_row_commits_only_once_confirmed_and_learns_no_alias(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            food_id = await insert_food(conn)
            await insert_alias(conn, food_id)  # 'two eggs' → tier 1 for the component phrase
            _stub_pipeline(monkeypatch)  # no decompose: the estimate is the only staging
            runner = _fake_runner(
                [("log_estimate", {"items": [{"phrase": "two eggs", "qty": 2, "unit": None, "note": "a guess"}],
                                   "basis": "a homemade plate"})],
                "Staged an estimate.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            use_before = await _scalar(conn, "SELECT use_count FROM aliases WHERE alias_norm = 'two eggs'")

            body = _log("some scrambled eggs at a friend's")
            resp = await logs_routes.create_log(body)
            (row,) = resp.items
            assert row.status == "resolved" and row.tier == 1 and row.estimated == "portion"
            assert row.confidence == "medium" and row.qty == 2

            # §3.4a: the stored flag blocks the commit until the explicit confirm
            with pytest.raises(HTTPException) as ei:
                await commit_route(body.id, CommitRequest(items=[row]))
            assert ei.value.status_code == 409 and "confirm the estimated rows first: [0]" in ei.value.detail
            ok = await confirm_estimate(ConfirmEstimateRequest(log_id=body.id, item_index=0))
            assert ok.estimate_confirmed is True
            totals = await commit_route(body.id, CommitRequest(items=[row]))
            assert totals.errors == []

            # the row learned nothing: no alias use, no alias on the entry
            assert await _scalar(conn, "SELECT use_count FROM aliases WHERE alias_norm = 'two eggs'") == use_before
            assert await _scalar(conn, "SELECT alias_id FROM entries WHERE log_id = %s", (body.id,)) is None
            assert await _scalar(conn, "SELECT alias_id FROM item_resolutions WHERE log_id = %s", (body.id,)) is None

    _run(scenario())


# ---------------------------------------------------------------------------
# 3. routes/agent — the stream
# ---------------------------------------------------------------------------


def test_agent_stream_orders_tool_events_reply_and_done(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            async def decompose(text, ctx):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            runner = _fake_runner([("log_food", {"text": "two eggs"})], "Staged 1 item.")
            monkeypatch.setattr(agent_svc, "Runner", runner)

            body = LogCreate(id=uuid4(), text="two eggs", captured_at=NOW, tz=TZ)  # agent implied
            resp = await agent_routes.agent_text(body)
            assert resp.media_type == "application/x-ndjson"
            events = await _drain(resp)
            assert [e["type"] for e in events] == ["tool_call", "tool_result", "reply", "done"]
            assert events[0]["name"] == "log_food" and events[0]["args"] == {"text": "two eggs"}
            assert events[1]["output"]["items"][0]["phrase"] == "eggs"
            assert events[2]["text"] == "Staged 1 item."
            done = events[3]["log"]
            assert done["log_id"] == str(body.id) and done["agent"]["reply"] == "Staged 1 item."
            assert [(it["phrase"], it["qty"]) for it in done["items"]] == [("eggs", 2.0)]
            assert await _scalar(conn, "SELECT count(*) FROM item_resolutions WHERE log_id = %s", (body.id,)) == 1

            # the same id again while the run is fresh in this process: the whole
            # stream again — transcript-less here, tool lines, reply, done — no second run
            resp2 = await agent_routes.agent_text(body)
            events2 = await _drain(resp2)
            assert events2 == events and not events2[-1].get("replay")
            assert runner.calls == 1

            # once the run has aged out of the table (another instance, or later):
            # the stored staging state as one `done`, marked a replay — still no run
            agent_routes._RUNS[body.id].finished_at -= agent_routes._RUN_TTL_SECONDS + 1
            resp3 = await agent_routes.agent_text(body)
            events3 = await _drain(resp3)
            assert [e["type"] for e in events3] == ["done"] and events3[0]["replay"] is True
            assert [it["phrase"] for it in events3[0]["log"]["items"]] == ["eggs"]
            assert runner.calls == 1 and body.id not in agent_routes._RUNS

    _run(scenario())


def test_agent_stream_retry_attaches_to_the_live_run(monkeypatch):
    """A phone that lost the response mid-run sends the same id again (§8.6):
    while the run is live it follows the same stream — the events it missed,
    then the rest as they happen — and nothing runs twice."""
    async def scenario():
        async with isolated_db():
            async def decompose(text, ctx):
                await asyncio.sleep(0.6)  # the run is still going when the retry lands
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            runner = _fake_runner([("log_food", {"text": "two eggs"})], "Staged 1 item.")
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = LogCreate(id=uuid4(), text="two eggs", captured_at=NOW, tz=TZ)

            first = await agent_routes.agent_text(body)
            assert body.id in agent_routes._RUNS and agent_routes._RUNS[body.id].finished_at is None
            second = await agent_routes.agent_text(body)  # attaches: no claim, no second run
            events1, events2 = await asyncio.gather(_drain(first), _drain(second))
            assert [e["type"] for e in events1] == ["tool_call", "tool_result", "reply", "done"]
            assert events2 == events1
            assert runner.calls == 1
            assert agent_routes._RUNS[body.id].finished_at is not None
            # a late follower after the end gets the buffer, done included
            events3 = await _drain(await agent_routes.agent_text(body))
            assert events3 == events1 and runner.calls == 1

    _run(scenario())


def test_agent_stream_voice_leads_with_the_transcript(monkeypatch):
    async def scenario():
        async with isolated_db():
            async def decompose(text, ctx):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            from services import transcribe as stt

            async def fake_transcribe(audio_bytes, mime):
                assert audio_bytes == b"opus-bytes" and mime.startswith("audio/webm")
                return SimpleNamespace(text="two eggs", transcribe_ms=12, provider="fake")

            monkeypatch.setattr(stt, "transcribe", fake_transcribe)
            runner = _fake_runner([("log_food", {"text": "two eggs"})], "Staged 1 item.")
            monkeypatch.setattr(agent_svc, "Runner", runner)

            audio = UploadFile(BytesIO(b"opus-bytes"), filename="clip.webm",
                               headers=Headers({"content-type": "audio/webm;codecs=opus"}))
            resp = await agent_routes.agent_voice(audio=audio, log_id=uuid4(), captured_at=NOW, tz=TZ)
            events = await _drain(resp)
            assert [e["type"] for e in events] == ["transcript", "tool_call", "tool_result", "reply", "done"]
            assert events[0]["text"] == "two eggs"
            assert events[-1]["log"]["items"][0]["phrase"] == "eggs"

            # the same id from another instance (the run is not in this table): the
            # stored replay leads with the transcript the row kept, so the chat's
            # pending bubble fills, then `done` marked a replay
            log_id = uuid4()
            resp = await agent_routes.agent_voice(
                audio=UploadFile(BytesIO(b"opus-bytes"), filename="clip.webm",
                                 headers=Headers({"content-type": "audio/webm;codecs=opus"})),
                log_id=log_id, captured_at=NOW, tz=TZ,
            )
            await _drain(resp)
            agent_routes._RUNS.pop(log_id)
            again = UploadFile(BytesIO(b"opus-bytes"), filename="clip.webm",
                               headers=Headers({"content-type": "audio/webm;codecs=opus"}))
            events = await _drain(await agent_routes.agent_voice(audio=again, log_id=log_id, captured_at=NOW, tz=TZ))
            assert [e["type"] for e in events] == ["transcript", "done"]
            assert events[0]["text"] == "two eggs" and events[1]["replay"] is True
            assert events[1]["log"]["items"][0]["phrase"] == "eggs"

            # a silent blob is the §5 capture failure, as one `done`
            empty = UploadFile(BytesIO(b""), filename="clip.webm", headers=Headers({"content-type": "audio/webm"}))
            resp = await agent_routes.agent_voice(audio=empty, log_id=uuid4(), captured_at=NOW, tz=TZ)
            events = await _drain(resp)
            assert [e["type"] for e in events] == ["done"]
            assert events[0]["log"]["status"] == "capture_failure" and events[0]["log"]["capture_error"] == "silent_blob"

    _run(scenario())


def test_agent_stream_reports_a_failure_as_an_error_event_and_the_retry_runs_again(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            async def decompose(text, ctx):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)

            class Exploding:
                @staticmethod
                def run_streamed(agent, agent_input, max_turns):
                    raise RuntimeError("provider down")

            monkeypatch.setattr(agent_svc, "Runner", Exploding)
            body = LogCreate(id=uuid4(), text="two eggs", captured_at=NOW, tz=TZ)
            events = await _drain(await agent_routes.agent_text(body))
            assert [e["type"] for e in events] == ["error"]
            assert events[0]["status"] == 500 and "provider down" not in events[0]["detail"]
            # a failed run is not kept for replay, and its itemless log is gone (§8.6):
            # the same id runs the pipeline again instead of streaming the failure back
            assert body.id not in agent_routes._RUNS
            assert await _scalar(conn, "SELECT count(*) FROM logs WHERE id = %s", (body.id,)) == 0
            monkeypatch.setattr(agent_svc, "Runner", _fake_runner([("log_food", {"text": "two eggs"})], "Staged 1 item."))
            events = await _drain(await agent_routes.agent_text(body))
            assert [e["type"] for e in events] == ["tool_call", "tool_result", "reply", "done"]
            assert not events[-1]["replay"] and events[-1]["log"]["items"][0]["phrase"] == "eggs"

    _run(scenario())


def test_live_runs_that_hang_or_are_cancelled_still_end(monkeypatch):
    """A run with no terminal event is presumed dead after _RUN_MAX_SECONDS —
    its followers get an error instead of waiting forever, and the table
    forgets it — and a cancelled task closes its stream too."""
    async def scenario():
        async with isolated_db():
            gate = asyncio.Event()

            async def never(emit):
                await gate.wait()
                raise AssertionError("unreachable")

            # 1. a follower of a silent run gives up after the deadline
            monkeypatch.setattr(agent_routes, "_RUN_MAX_SECONDS", 0.2)
            hung_id = uuid4()
            run = agent_routes._start(hung_id, never)
            events = await _drain(agent_routes._stream(agent_routes._follow(run)))
            assert [e["type"] for e in events] == ["error"] and "went quiet" in events[0]["detail"]
            # 2. the table closes and forgets a run that is silent past the deadline
            run.started_at -= 1
            assert agent_routes.live_run(hung_id) is None
            assert run.events[-1]["type"] == "error" and hung_id not in agent_routes._RUNS
            # 3. cancellation appends the terminal event and forgets the run
            monkeypatch.setattr(agent_routes, "_RUN_MAX_SECONDS", 300.0)
            cancelled_id = uuid4()
            run2 = agent_routes._start(cancelled_id, never)
            await asyncio.sleep(0)  # let go() start waiting
            run2.task.cancel()
            await asyncio.gather(run2.task, return_exceptions=True)
            assert run2.finished and run2.events[-1]["type"] == "error"
            assert cancelled_id not in agent_routes._RUNS
            gate.set()
            await asyncio.sleep(0.05)  # the hung tasks finish inside the transaction

    _run(scenario())
