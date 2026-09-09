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

import db  # noqa: E402
from models import CommitRequest, LogCreate  # noqa: E402
from routes import agent as agent_routes  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from routes.photo import ConfirmEstimateRequest, confirm_estimate  # noqa: E402
from services import agent as agent_svc  # noqa: E402
from services import quota  # noqa: E402
from test_agent import NOW, TZ, _deps, _fake_runner, _invoke, _parsed  # noqa: E402
from test_tenancy import (  # noqa: E402
    _become,
    _scalar,
    _stub_pipeline,
    insert_alias,
    insert_food,
    isolated_db,
)


def _run(coro):
    return asyncio.run(coro)


SHRIMP = {"phrase": "shrimp, cooked", "qty": 400, "unit": "g", "grams": None, "note": "~50 shrimp at ~8 g each"}
SAUCE = {"phrase": "cocktail sauce", "qty": 60, "unit": "g", "grams": None, "note": "the small tub in the tray"}
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
        (items, basis, meal_hint, when_hint, day_offset, when_meal), = deps.calls["estimate"]
        assert basis == BASIS and items[0]["qty"] == 400 and items[0]["unit"] == "g"
        assert meal_hint is None and when_hint is None  # none spoken, none passed
        assert day_offset is None and when_meal is None

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
        # a count of pieces without its weight is refused too: on a food whose only
        # serving is 100 g it would have staged as pieces × 100 g (50 shrimp → 5 kg)
        out = await _invoke(agent2, "log_estimate",
                            items=[{**SHRIMP, "qty": 50, "unit": None, "grams": None}], basis=BASIS)
        assert out["error"] == "missing_grams" and "shrimp" in out["message"]
        assert state2["estimate_calls"] == 0

        # the spoken meal / time words ride through, verbatim (§7.2)
        deps3 = _deps()
        agent3 = agent_svc.build_agent(deps3, {})
        out = await _invoke(agent3, "log_estimate", items=[SAUCE], basis=BASIS,
                            meal_hint="breakfast", when_hint=" yesterday morning ",
                            when_day_offset=-1, when_meal="breakfast")
        assert out["meal"] == "breakfast"
        # the phrase rides verbatim for the trace; the DAY comes from the
        # model's own offset, because nothing downstream parses the phrase (174)
        assert deps3.calls["estimate"][0][2:] == (
            "breakfast", "yesterday morning", -1, "breakfast")

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
         "output": {"query": "costco shrimp platter", "sources": ["https://www.costco.com/shrimp-platter"]}},
    ]

    # live: the raw in-progress event puts the line up first (query unknown), the
    # completed item then settles it — only the result, carrying the query
    live = SimpleNamespace(type="raw_response_event",
                           data=SimpleNamespace(type="response.web_search_call.in_progress", item_id="ws_7"))
    assert agent_svc.wire_events(live, names) == [
        {"type": "tool_call", "id": "ws_7", "name": "web_search", "args": {"query": None}}]
    searching = SimpleNamespace(type="raw_response_event",
                                data=SimpleNamespace(type="response.web_search_call.searching", item_id="ws_7"))
    assert agent_svc.wire_events(searching, names) == []  # already up
    done_item = ResponseFunctionWebSearch(
        id="ws_7", status="completed", type="web_search_call",
        action=ActionSearch(type="search", query="kirkland shrimp platter weight",
                            sources=[ActionSearchSource(type="url", url="https://www.costco.com/x")]),
    )
    assert agent_svc.wire_events(ev("tool_called", ToolCallItem(raw_item=done_item, agent=agent)), names) == [
        {"type": "tool_result", "id": "ws_7", "name": "web_search",
         "output": {"query": "kirkland shrimp platter weight", "sources": ["https://www.costco.com/x"]}}]
    other = SimpleNamespace(type="raw_response_event", data=SimpleNamespace(type="response.output_text.delta"))
    assert agent_svc.wire_events(other, names) == []

    # a search that consulted nothing still resolves its line: the result
    # arrives with the call, sources empty
    quiet = ResponseFunctionWebSearch(
        id="ws_2", status="completed", type="web_search_call",
        action=ActionSearch(type="search", query="nothing found", sources=None),
    )
    out = agent_svc.wire_events(ev("tool_called", ToolCallItem(raw_item=quiet, agent=agent)), names)
    assert [e["type"] for e in out] == ["tool_call", "tool_result"]
    assert out[1]["output"] == {"query": "nothing found", "sources": []}

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
    # …but the tool's own short scalars ride along: the chat line is written
    # from them (past_foods' count), and losing `error` made a truncated
    # failure read as success
    fat = ToolCallOutputItem(
        raw_item={"type": "function_call_output", "call_id": "call_10", "output": "x"},
        output=json.dumps({
            "count": 200, "error": "unavailable", "note": "z" * 500,
            "foods": [{"food": "y" * 100} for _ in range(60)],
        }),
        agent=agent,
    )
    (only,) = agent_svc.wire_events(ev("tool_output", fat), names)
    assert only["output"]["truncated"] is True
    assert only["output"]["count"] == 200 and only["output"]["error"] == "unavailable"
    assert "foods" not in only["output"]  # the bulk went into the summary
    assert "note" not in only["output"]  # and so did an over-long scalar


def test_a_clarifying_question_rides_the_reply_and_discard_replaces_a_staged_log(monkeypatch):
    async def scenario():
        # no tool call, the question in the reply, its answers as options
        class Asking:
            @staticmethod
            def run_streamed(agent, agent_input, max_turns):
                class S:
                    final_output = agent_svc.AgentReply(
                        reply="Which Costco shrimp — the 3.75 lb party platter, the 1.5 lb tray, or the frozen ring?",
                        options=[" the 3.75 lb party platter ", "the 1.5 lb tray", "the frozen ring", "", "something else", "too many"],
                    )

                    async def stream_events(self):
                        return
                        yield  # pragma: no cover

                    def to_input_list(self):
                        return []

                return S()

        monkeypatch.setattr(agent_svc, "Runner", Asking)
        seen: list[dict] = []

        async def emit(ev):
            seen.append(ev)

        deps = _deps()
        run = await agent_svc.run_agent("I had a platter of costco shrimp", deps, emit)
        assert run.options == ["the 3.75 lb party platter", "the 1.5 lb tray", "the frozen ring", "something else"]  # trimmed, blanks out, four at most
        assert seen == [{"type": "reply", "text": run.reply, "options": run.options}]
        assert run.food_calls == 0 and run.estimate_calls == 0 and deps.calls["food"] == []

        # discard_log goes through the route's effect; without one it says so
        agent = agent_svc.build_agent(deps, {})
        out = await _invoke(agent, "discard_log", log_id=" 11111111-1111-1111-1111-111111111111 ")
        assert out == {"log_id": "11111111-1111-1111-1111-111111111111", "status": "discarded"}
        assert deps.calls["discard"] == ["11111111-1111-1111-1111-111111111111"]
        bare = agent_svc.build_agent(_deps(discard_log=None), {})
        assert (await _invoke(bare, "discard_log", log_id="x"))["error"] == "unavailable"

    _run(scenario())


def test_a_correction_discards_the_earlier_staged_log_and_reports_it(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            async def decompose(text, ctx, **kw):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            monkeypatch.setattr(agent_svc, "Runner", _fake_runner([("log_food", {"text": "two eggs"})], "Staged 2 eggs."))
            first = await logs_routes.create_log(_log("two eggs"))
            assert first.agent is not None and first.agent.discarded is None and first.agent.options is None

            from models import AgentTurn
            history = [
                AgentTurn(role="user", text="two eggs"),
                AgentTurn(role="agent", text="Staged 2 eggs."),
                AgentTurn(role="agent", text="staged 1 item", log_id=first.log_id,
                          items=["egg — 2 × 1 large"], state="staged"),
            ]
            # the correction: discard the earlier log (named by the conversation), stage anew
            runner = _fake_runner(
                [("discard_log", {"log_id": str(first.log_id)}), ("log_food", {"text": "one egg"})],
                "Made it one egg.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = LogCreate(id=uuid4(), text="actually just one egg", captured_at=NOW, tz=TZ, agent=True, history=history)
            resp = await logs_routes.create_log(body)
            assert resp.agent is not None and resp.agent.discarded == [first.log_id]
            assert await _scalar(conn, "SELECT status FROM logs WHERE id = %s", (first.log_id,)) == "discarded"
            assert [it.phrase for it in resp.items] == ["eggs"]
            # the log's rows and status come from the database, not from the chat's copy
            # (the row is still a searching phrase there — the chat's "1 large" is not)
            assert f"Log {first.log_id} (staged): eggs" in runner.input
            assert "1 large" not in runner.input
            assert 'They said: "two eggs"' in runner.input

            # only a log the conversation names, and only while it is still staged
            class Refusals:
                @staticmethod
                def run_streamed(agent, agent_input, max_turns):
                    class S:
                        final_output = agent_svc.AgentReply(reply="Nothing to change.", options=None)

                        async def stream_events(self):
                            out = await _invoke(agent, "discard_log", log_id=str(uuid4()))
                            assert out["error"] == "unknown_log"
                            out = await _invoke(agent, "discard_log", log_id=str(first.log_id))
                            # already discarded (the earlier turn did it): nothing to replace,
                            # not a refusal — the correction is staged afresh
                            assert "error" not in out and out["status"] == "discarded" and out["already"] is True
                            return
                            yield  # pragma: no cover

                        def to_input_list(self):
                            return []

                    return S()

            monkeypatch.setattr(agent_svc, "Runner", Refusals)
            resp2 = await logs_routes.create_log(
                LogCreate(id=uuid4(), text="hello", captured_at=NOW, tz=TZ, agent=True, history=history))
            assert resp2.agent is not None and resp2.agent.discarded is None

            # a card that still says staged reports what the database says (discarded);
            # a log this tenant cannot see — another's, or invented — is not named at all,
            # so the conversation cannot make it discardable
            stale = [
                AgentTurn(role="agent", text="staged 1 item", log_id=first.log_id,
                          items=["egg — 2 × 1 large"], state="staged"),
                AgentTurn(role="agent", text="staged 1 item", log_id=uuid4(),
                          items=["ghost — 1 × 1 piece"], state="staged"),
            ]
            runner3 = _fake_runner([], "Nothing to change.")
            monkeypatch.setattr(agent_svc, "Runner", runner3)
            await logs_routes.create_log(
                LogCreate(id=uuid4(), text="hello again", captured_at=NOW, tz=TZ, agent=True, history=stale))
            assert f"Log {first.log_id} (discarded): eggs" in runner3.input
            assert "ghost" not in runner3.input and runner3.input.count("- Log ") == 1

    _run(scenario())


def test_a_question_turn_gives_a_demo_its_daily_log_attempt_back(monkeypatch):
    """A clarifying question is a full turn with its own client id, so it took
    the §8.6 claim and the demo's one-a-day log attempt; the answer, the next
    turn, would have been 429. The attempt goes back when nothing was staged
    or weighed (§8.9)."""
    async def scenario():
        async with isolated_db() as conn:
            await _become(conn, "demo-asker")  # a demo account: one log a day
            async def decompose(text, ctx, **kw):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)

            class Asking:
                @staticmethod
                def run_streamed(agent, agent_input, max_turns):
                    class S:
                        final_output = agent_svc.AgentReply(
                            reply="Which one?", options=["The big tray", "The small tray"])

                        async def stream_events(self):
                            return
                            yield  # pragma: no cover

                        def to_input_list(self):
                            return []

                    return S()

            monkeypatch.setattr(agent_svc, "Runner", Asking)
            first = await logs_routes.create_log(_log("costco shrimp"))
            assert first.items == [] and first.agent.options == ["The big tray", "The small tray"]
            # the attempt the claim recorded is handed back: the day's 'log' row is
            # now a 'question' row, which the one-a-day index does not count
            assert await _scalar(conn, "SELECT count(*) FROM quota_events WHERE kind = 'log'") == 0
            assert await _scalar(conn, "SELECT count(*) FROM quota_events WHERE kind = 'question'") == 1

            # the answer claims the day's attempt and stages
            monkeypatch.setattr(agent_svc, "Runner", _fake_runner([("log_food", {"text": "two eggs"})], "Staged."))
            second = await logs_routes.create_log(LogCreate(
                id=uuid4(), text="The big tray", captured_at=NOW, tz=TZ, agent=True,
                history=[{"role": "user", "text": "costco shrimp"}, {"role": "agent", "text": "Which one?"}]))
            assert [it.phrase for it in second.items] == ["eggs"] and second.agent.options is None
            assert await _scalar(conn, "SELECT count(*) FROM quota_events WHERE kind = 'log'") == 1
            # and that one is not refunded: it staged something
            with pytest.raises(HTTPException) as ei:
                await logs_routes.create_log(_log("more eggs"))
            assert ei.value.status_code == 429

        # the cap: past DEMO_QUESTION_LIMIT questions a day the attempt stands, so
        # ask → answer → ask is not unbounded model spend
        async with isolated_db() as conn:
            await _become(conn, "demo-asker-2")
            monkeypatch.setattr(quota, "DEMO_QUESTION_LIMIT", 1)
            monkeypatch.setattr(agent_svc, "Runner", Asking)
            await logs_routes.create_log(_log("costco shrimp"))  # handed back: the one question
            await logs_routes.create_log(_log("costco shrimp, again"))  # the cap: the row stays 'log'
            assert await _scalar(conn, "SELECT count(*) FROM quota_events WHERE kind = 'question'") == 1
            assert await _scalar(conn, "SELECT count(*) FROM quota_events WHERE kind = 'log'") == 1
            with pytest.raises(HTTPException) as ei:
                await logs_routes.create_log(_log("The big tray"))
            assert ei.value.status_code == 429

    _run(scenario())


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
            async def decompose(text, ctx, **kw):
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
                                   "meal_hint": None, "when_hint": "yesterday at lunch",
                                   "when_day_offset": -1, "when_meal": "lunch"})],
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

            # an explicit meal word outranks the shift's meal; a phrase the
            # agent could not resolve to a day is noted, never guessed at (174)
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
            assert "logged_at" not in staged2 and "left unresolved" in staged2["when_hint_note"]

    _run(scenario())


def test_a_counted_estimate_is_sized_by_weight_when_the_food_has_no_count_serving(monkeypatch):
    """133 shrimp on a food whose only serving is 100 g must not become 133 ×
    100 g: the count's `grams` size it (resolved rows here; searching rows
    hand tier 3 the weight). A food with a count serving keeps the count."""
    async def scenario():
        async with isolated_db() as conn:
            from psycopg.types.json import Json
            hundred = {"serving_id": "s100", "description": "100 g", "grams": 100,
                       "macros": {"kcal": 99, "protein_g": 24, "carb_g": 0.2, "fat_g": 0.3}}
            shrimp_id = str(await _scalar(conn, """
                INSERT INTO foods (source, source_id, name, display_name, food_type, servings,
                                   default_serving_id, payload)
                VALUES ('fdc', %s, 'shrimp, cooked', 'plain cooked shrimp', 'generic', %s, 's100', '{}'::jsonb)
                RETURNING id""", (uuid4().hex, Json([hundred]))))
            await conn.execute(
                "INSERT INTO aliases (alias_norm, food_id, origin) VALUES ('shrimp cooked', %s, 'tier3')",
                (shrimp_id,))
            egg_id = await insert_food(conn)          # its serving is "1 egg": count-type
            await insert_alias(conn, egg_id)          # 'two eggs' → tier 1
            _stub_pipeline(monkeypatch)
            runner = _fake_runner([("log_estimate", {"items": [
                {"phrase": "shrimp cooked", "qty": 133, "unit": None, "grams": 1701, "note": "the whole 3.75 lb tray"},
                {"phrase": "two eggs", "qty": 2, "unit": None, "grams": 100, "note": "two eggs"},
                {"phrase": "cocktail sauce from mars", "qty": 40, "unit": None, "grams": 320, "note": "eight cups"},
            ], "basis": "a platter"})], "Staged an estimate.")
            monkeypatch.setattr(agent_svc, "Runner", runner)
            resp = await logs_routes.create_log(_log("a platter of shrimp, two eggs, sauce"))
            shrimp, eggs, sauce = resp.items
            # mass-only food: sized by weight, macros from the food's own 100 g numbers
            assert shrimp.status == "resolved" and shrimp.serving.description == "100 g"
            assert shrimp.qty == 17.01 and shrimp.macros.kcal == round(99 * 17.01, 2)
            assert shrimp.estimated == "portion" and shrimp.confidence == "medium"
            # a count serving keeps the count
            assert eggs.status == "resolved" and eggs.serving.description == "1 egg" and eggs.qty == 2
            # a searching row hands tier 3 the weight, not the piece count
            assert sauce.status == "searching" and sauce.qty == 320
            stored = await _scalar(conn, "SELECT staged FROM item_resolutions WHERE log_id = %s AND item_index = 2", (resp.log_id,))
            assert (stored["spoken_qty"], stored["spoken_unit"]) == (320, "g")
            assert stored["estimate_note"] == "eight cups" and stored["estimated"] == "portion"

    _run(scenario())


def test_flagged_estimate_row_commits_only_once_confirmed_and_learns_no_alias(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            food_id = await insert_food(conn)
            await insert_alias(conn, food_id)  # 'two eggs' → tier 1 for the component phrase
            _stub_pipeline(monkeypatch)  # no decompose: the estimate is the only staging
            runner = _fake_runner(
                [("log_estimate", {"items": [{"phrase": "two eggs", "qty": 2, "unit": None, "grams": 100, "note": "a guess"}],
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
            async def decompose(text, ctx, **kw):
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
            async def decompose(text, ctx, **kw):
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
            async def decompose(text, ctx, **kw):
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


def test_a_live_run_is_its_tenants_alone(monkeypatch):
    """The in-process table sits in front of the database, so it keeps the
    database's rule itself (§8.9): another tenant sending the same log id
    gets what a foreign id gets from the claim — a 404 — never the stream."""
    async def scenario():
        async with isolated_db() as conn:
            async def decompose(text, ctx, **kw):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            runner = _fake_runner([("log_food", {"text": "two eggs"})], "Staged 1 item.")
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = LogCreate(id=uuid4(), text="two eggs", captured_at=NOW, tz=TZ)
            events = await _drain(await agent_routes.agent_text(body))
            assert events[-1]["type"] == "done"
            me = db.current_user_id.get()
            assert agent_routes._RUNS[body.id].owner == me

            # the run is finished and attachable — for its owner
            await _become(conn, "someone-else")
            assert agent_routes.live_run(body.id) is None and body.id in agent_routes._RUNS
            with pytest.raises(HTTPException) as ei:
                await agent_routes.agent_text(body)
            assert ei.value.status_code == 404 and runner.calls == 1

            await _become(conn, me, is_owner=True)
            again = await _drain(await agent_routes.agent_text(body))
            assert again == events and runner.calls == 1

    _run(scenario())


def test_a_run_that_fails_after_staging_completes_on_the_staged_rows(monkeypatch):
    """A model failure after log_food staged rows (a final output that did
    not parse, a provider error on a later turn) must not read as nothing
    logged: the rows are in the database, so the run completes on them with
    the code-built reply and the stream ends in `done`, not `error`."""
    async def scenario():
        async with isolated_db():
            async def decompose(text, ctx, **kw):
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)

            class StageThenExplode:
                @staticmethod
                def run_streamed(agent, agent_input, max_turns):
                    class S:
                        final_output = None

                        async def stream_events(self):
                            await _invoke(agent, "log_food", text="two eggs")
                            raise RuntimeError("the final output did not parse")
                            yield  # pragma: no cover

                    return S()

            monkeypatch.setattr(agent_svc, "Runner", StageThenExplode)
            body = LogCreate(id=uuid4(), text="two eggs", captured_at=NOW, tz=TZ)
            events = await _drain(await agent_routes.agent_text(body))
            assert [e["type"] for e in events] == ["reply", "done"]  # the bare stream yields no SDK items
            assert events[0]["text"] == "I staged what you ate — check the list."
            assert [it["phrase"] for it in events[1]["log"]["items"]] == ["eggs"]
            assert events[1]["log"]["agent"]["reply"] == events[0]["text"]

    _run(scenario())


def test_agent_stream_reports_a_failure_as_an_error_event_and_the_retry_runs_again(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            async def decompose(text, ctx, **kw):
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
            # a failed run is not kept for replay, and its claim is released (§8.6):
            # the row stays, its claim stamp aged past the grace, so the same id runs
            # the pipeline again at once instead of streaming the failure back
            assert body.id not in agent_routes._RUNS
            assert await _scalar(conn, "SELECT count(*) FROM logs WHERE id = %s", (body.id,)) == 1
            assert await _scalar(
                conn, "SELECT created_at < now() - make_interval(secs => %s) FROM logs WHERE id = %s",
                (logs_routes._AGENT_RERUN_GRACE_SECONDS, body.id),
            ) is True
            monkeypatch.setattr(agent_svc, "Runner", _fake_runner([("log_food", {"text": "two eggs"})], "Staged 1 item."))
            events = await _drain(await agent_routes.agent_text(body))
            assert [e["type"] for e in events] == ["tool_call", "tool_result", "reply", "done"]
            assert not events[-1]["replay"] and events[-1]["log"]["items"][0]["phrase"] == "eggs"

            # a run that recorded a weigh-in and then failed: the weigh-in stands (it
            # points at the log, which is why the row is kept, not dropped) and the
            # retry still runs again
            class WeighThenExplode:
                @staticmethod
                def run_streamed(agent, agent_input, max_turns):
                    class S:
                        final_output = None

                        async def stream_events(self):
                            await _invoke(agent, "log_weight", weight=180.4, unit="lb")
                            raise RuntimeError("provider down")
                            yield  # pragma: no cover

                    return S()

            monkeypatch.setattr(agent_svc, "Runner", WeighThenExplode)
            body2 = LogCreate(id=uuid4(), text="180.4 pounds and two eggs", captured_at=NOW, tz=TZ)
            events = await _drain(await agent_routes.agent_text(body2))
            assert [e["type"] for e in events] == ["error"]  # the bare stream yields no SDK items
            assert await _scalar(conn, "SELECT count(*) FROM weights WHERE log_id = %s", (body2.id,)) == 1
            monkeypatch.setattr(agent_svc, "Runner", _fake_runner(
                [("log_food", {"text": "two eggs"}), ("log_weight", {"weight": 180.4, "unit": "lb"})], "Staged 1 item and logged 180.4 lb."))
            events = await _drain(await agent_routes.agent_text(body2))
            assert [e["type"] for e in events][-2:] == ["reply", "done"] and not events[-1]["replay"]
            assert events[-1]["log"]["items"][0]["phrase"] == "eggs" and events[-1]["log"]["agent"]["weight"] is not None
            assert await _scalar(conn, "SELECT count(*) FROM weights WHERE log_id = %s", (body2.id,)) == 1  # one row a day, replaced

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
            # 4. an evicted run's task is cancelled and cleans up nothing that is no
            # longer its own: a replacement under the same id keeps its slot
            raced_id = uuid4()
            old = agent_routes._start(raced_id, never)
            await asyncio.sleep(0)
            old.started_at -= 301
            assert agent_routes.live_run(raced_id) is None and old.task.cancelling()
            replacement = agent_routes._start(raced_id, never)
            await asyncio.gather(old.task, return_exceptions=True)
            assert old.finished and agent_routes._RUNS[raced_id] is replacement
            replacement.task.cancel()
            await asyncio.gather(replacement.task, return_exceptions=True)
            gate.set()
            await asyncio.sleep(0.05)  # the hung tasks finish inside the transaction

    _run(scenario())


# ---------------------------------------------------------------------------
# 4. the photo turn (§3.4b in the chat, 2026-09-08)
# ---------------------------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def _image() -> UploadFile:
    return UploadFile(
        BytesIO(PNG), filename="plate.png", headers=Headers({"content-type": "image/png"})
    )


def test_a_photo_streams_read_photo_then_a_reply_and_runs_no_model(monkeypatch):
    """The whole point of the turn: the plate is read and staged by the SAME
    pipeline POST /api/logs/photo runs, the chat sees it happen as a tool
    line, and the reply comes back — with no chat model involved at all. The
    Runner is one that raises if it is touched, which is the assertion."""

    async def scenario():
        async with isolated_db() as conn:
            await _become(conn, "photo-owner", is_owner=True)
            # the shared fixtures: an 'egg' food with a "1 egg" serving, and
            # the alias 'two eggs' that resolves to it at tier 1
            await insert_alias(conn, await insert_food(conn))

            import services.vision as vision_mod

            async def fake_analyze(image, mime, **kw):
                assert image == PNG and mime == "image/png"  # the SNIFFED type
                return vision_mod.VisionResult(
                    items=[
                        vision_mod.VisionItem(
                            phrase="two eggs", qty=2, unit="egg", est_grams=100,
                            confidence="high", note="two fried eggs", menu=None,
                        )
                    ],
                    meal_hint=None,
                )

            monkeypatch.setattr(vision_mod, "analyze", fake_analyze)

            class _NoModel:
                def run_streamed(self, *a, **k):
                    raise AssertionError("the photo turn must not run the chat model")

            monkeypatch.setattr(agent_svc, "Runner", _NoModel())

            log_id = uuid4()
            events = await _drain(
                await agent_routes.agent_voice(
                    image=_image(), log_id=log_id, captured_at=NOW, tz=TZ
                )
            )
            assert [e["type"] for e in events] == [
                "tool_call", "tool_result", "reply", "done",
            ]
            call, result, reply, done = events
            assert call["name"] == "read_photo"
            # §8.8: the type and the size ride the wire, never the image
            assert call["args"] == {"mime": "image/png", "bytes": len(PNG)}
            assert result["output"]["seen"] == ["two fried eggs"]
            assert "two fried eggs" in reply["text"]
            assert reply["text"].endswith("tell me what's off.")
            # the plate is the turn's own log, staged by the photo pipeline
            log = done["log"]
            assert log["log_id"] == str(log_id)
            assert [it["phrase"] for it in log["items"]] == ["two eggs"]
            assert log["items"][0]["estimated"] == "portion"
            assert await _scalar(
                conn, "SELECT input_mode FROM logs WHERE id = %s", (log_id,)
            ) == "photo"

    _run(scenario())


def test_a_photo_upload_is_validated_before_the_stream_starts(monkeypatch):
    """415/413 stay plain HTTP errors, as every pre-stream refusal in this
    route does — the client's own copy depends on the status."""

    async def scenario():
        async with isolated_db() as conn:
            await _become(conn, "photo-owner", is_owner=True)
            not_an_image = UploadFile(
                BytesIO(b"hello, not an image"), filename="x.png",
                headers=Headers({"content-type": "image/png"}),
            )
            with pytest.raises(HTTPException) as exc:
                await agent_routes.agent_voice(
                    image=not_an_image, log_id=uuid4(), captured_at=NOW, tz=TZ
                )
            assert exc.value.status_code == 415
            # nothing was claimed: the refusal is before any database work
            assert await _scalar(conn, "SELECT count(*) FROM logs WHERE input_mode = 'photo'", ()) == 0

    _run(scenario())


def test_a_photo_conversation_carries_its_plate_into_the_next_turn(monkeypatch):
    """The tuning half: `photo_log_id` puts the run on the photo tier and
    hands it the plate, and adjust_photo's edit comes back on the outcome
    rather than as this turn's own (empty) log."""

    async def scenario():
        async with isolated_db() as conn:
            await _become(conn, "photo-owner", is_owner=True)
            # the shared fixtures: an 'egg' food with a "1 egg" serving, and
            # the alias 'two eggs' that resolves to it at tier 1
            await insert_alias(conn, await insert_food(conn))

            import services.vision as vision_mod

            async def fake_analyze(image, mime, **kw):
                return vision_mod.VisionResult(
                    items=[
                        vision_mod.VisionItem(
                            phrase="two eggs", qty=2, unit="egg", est_grams=100,
                            confidence="high", note="two fried eggs", menu=None,
                        )
                    ],
                    meal_hint=None,
                )

            monkeypatch.setattr(vision_mod, "analyze", fake_analyze)

            class _NoModel:
                def run_streamed(self, *a, **k):
                    raise AssertionError("no model on the photo turn")

            monkeypatch.setattr(agent_svc, "Runner", _NoModel())
            plate_id = uuid4()
            await _drain(
                await agent_routes.agent_voice(
                    image=_image(), log_id=plate_id, captured_at=NOW, tz=TZ
                )
            )

            # the next turn: "that's three eggs"
            runner = _fake_runner(
                [("adjust_photo", {"changes": [{
                    "op": "amount", "item": "two eggs", "qty": 3,
                    "unit": None, "grams": 150, "note": "they said three",
                }]})],
                "Updated — three eggs.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = LogCreate(
                id=uuid4(), text="that's three eggs", captured_at=NOW, tz=TZ,
                agent=True, photo_log_id=plate_id,
            )
            events = await _drain(await agent_routes.agent_text(body))
            assert [e["type"] for e in events] == [
                "tool_call", "tool_result", "reply", "done",
            ]
            done = events[3]["log"]
            # this turn staged nothing of its own — the plate came back on the
            # outcome, which is the only way the chat can patch its card
            assert done["items"] == [] and done["log_id"] == str(body.id)
            updated = done["agent"]["updated"]
            assert updated["log_id"] == str(plate_id)
            assert updated["items"][0]["qty"] == 3
            assert updated["items"][0]["estimated"] == "portion"

    _run(scenario())


def test_a_photo_with_no_food_says_so_once_and_leaves_no_staged_log(monkeypatch):
    """Vision ran and saw nothing. The chat must say so ONCE — `note` would be
    a second, identical bubble, since agentDone renders it alongside the reply
    — and the itemless log must not be left staged: the old client discarded it
    from PhotoCapture, and that call went away when the lane moved in here."""

    async def scenario():
        async with isolated_db() as conn:
            await _become(conn, "photo-owner", is_owner=True)

            import services.vision as vision_mod

            async def fake_analyze(image, mime, **kw):
                return vision_mod.VisionResult(items=[], meal_hint=None)

            monkeypatch.setattr(vision_mod, "analyze", fake_analyze)

            class _NoModel:
                def run_streamed(self, *a, **k):
                    raise AssertionError("no model on the photo turn")

            monkeypatch.setattr(agent_svc, "Runner", _NoModel())

            log_id = uuid4()
            events = await _drain(
                await agent_routes.agent_voice(
                    image=_image(), log_id=log_id, captured_at=NOW, tz=TZ
                )
            )
            assert [e["type"] for e in events] == [
                "tool_call", "tool_result", "reply", "done",
            ]
            assert events[1]["output"]["seen"] == []
            assert "couldn't find any food" in events[2]["text"]
            done = events[3]["log"]
            assert done["items"] == []
            # exactly one sentence reaches the chat: the reply, not reply + note
            assert done["note"] is None
            assert done["agent"]["reply"] == events[2]["text"]
            # §5: the attempt is real and stays as telemetry, but not as a log
            # the user has to go and dismiss
            assert await _scalar(
                conn, "SELECT status FROM logs WHERE id = %s", (log_id,)
            ) == "discarded"

    _run(scenario())


def test_adjust_photo_writes_nothing_when_one_change_names_no_row(monkeypatch):
    """A name that matches nothing is the likeliest failure — the model is
    naming rows from words. Applying the changes before it and then answering
    with a bare error would leave the plate in a state nobody asked for, and
    the model would retry over the top of it."""

    async def scenario():
        async with isolated_db() as conn:
            await _become(conn, "photo-owner", is_owner=True)
            await insert_alias(conn, await insert_food(conn))

            import services.vision as vision_mod

            async def fake_analyze(image, mime, **kw):
                return vision_mod.VisionResult(
                    items=[
                        vision_mod.VisionItem(
                            phrase="two eggs", qty=2, unit="egg", est_grams=100,
                            confidence="high", note="two fried eggs", menu=None,
                        )
                    ],
                    meal_hint=None,
                )

            monkeypatch.setattr(vision_mod, "analyze", fake_analyze)

            class _NoModel:
                def run_streamed(self, *a, **k):
                    raise AssertionError("no model on the photo turn")

            monkeypatch.setattr(agent_svc, "Runner", _NoModel())
            plate_id = uuid4()
            await _drain(
                await agent_routes.agent_voice(
                    image=_image(), log_id=plate_id, captured_at=NOW, tz=TZ
                )
            )
            before = await _scalar(
                conn,
                "SELECT staged->>'qty' FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (plate_id,),
            )

            from models import LogCreate as _LogCreate
            from routes import photo as photo_route
            from services.agent import PhotoChange

            body = _LogCreate(
                id=uuid4(), text="", captured_at=NOW, tz=TZ, photo_log_id=plate_id
            )
            out = await photo_route.adjust_rows(
                plate_id,
                [
                    PhotoChange(op="amount", item="two eggs", qty=3, unit=None,
                                grams=150, note="three"),
                    PhotoChange(op="remove", item="bacon", qty=None, unit=None,
                                grams=None, note="not there"),
                ],
                body,
            )
            assert out["error"] == "unknown_item"
            # the valid change ahead of it must NOT have landed
            assert await _scalar(
                conn,
                "SELECT staged->>'qty' FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (plate_id,),
            ) == before

    _run(scenario())
