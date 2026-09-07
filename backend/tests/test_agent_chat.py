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
   done as NDJSON; the voice variant leads with the transcript; the same-id
   replay is a single done; a failure inside the stream is an error event.

Run:  cd backend && uv run --with pytest pytest tests/test_agent_chat.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

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
        (items, basis), = deps.calls["estimate"]
        assert basis == BASIS and items[0]["qty"] == 400 and items[0]["unit"] == "g"

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

            # the same id again: the stored staging state as one `done`, no second run
            resp2 = await agent_routes.agent_text(body)
            events2 = await _drain(resp2)
            assert [e["type"] for e in events2] == ["done"]
            assert [it["phrase"] for it in events2[0]["log"]["items"]] == ["eggs"]
            assert runner.calls == 1

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

            # a silent blob is the §5 capture failure, as one `done`
            empty = UploadFile(BytesIO(b""), filename="clip.webm", headers=Headers({"content-type": "audio/webm"}))
            resp = await agent_routes.agent_voice(audio=empty, log_id=uuid4(), captured_at=NOW, tz=TZ)
            events = await _drain(resp)
            assert [e["type"] for e in events] == ["done"]
            assert events[0]["log"]["status"] == "capture_failure" and events[0]["log"]["capture_error"] == "silent_blob"

    _run(scenario())


def test_agent_stream_reports_a_failure_as_an_error_event(monkeypatch):
    async def scenario():
        async with isolated_db():
            _stub_pipeline(monkeypatch)

            class Exploding:
                @staticmethod
                def run_streamed(agent, agent_input, max_turns):
                    raise RuntimeError("provider down")

            monkeypatch.setattr(agent_svc, "Runner", Exploding)
            resp = await agent_routes.agent_text(LogCreate(id=uuid4(), text="hello", captured_at=NOW, tz=TZ))
            events = await _drain(resp)
            assert [e["type"] for e in events] == ["error"]
            assert events[0]["status"] == 500 and "provider down" not in events[0]["detail"]

    _run(scenario())
