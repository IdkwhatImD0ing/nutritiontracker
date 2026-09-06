"""Agent mode (DESIGN.md §6.4, 2026-09-06) + the weigh-in log (§4 `weights`, 003).

Offline: the Runner is a fake that drives the REAL tool closures, the way
test_tier3_hardening does for tier 3; decompose is stubbed (test_tenancy's
_stub_pipeline). Database work runs inside one force-rollback transaction
(isolated_db) against the live database — 003 is applied in-transaction
while the table is not there yet (tenancy_harness), so nothing persists.

1. services/agent: log_weight converts and refuses in code, log_food runs
   once per utterance, the fallback reply is built from tool-side state,
   run_agent reports what the tools did.
2. routes/weights: kg storage, one row per day updated in place,
   settings.body.weight_kg follows the newest weigh-in only, RLS keeps
   tenants apart; GET / PUT /api/weights.
3. POST /api/logs with agent=true: the §3.0 short-circuit still wins with no
   agent run; otherwise the agent stages the food part through the same
   pipeline and records the weigh-in under the log's id, and the response
   carries both; a weight-only utterance leaves an itemless log; agent=false
   never builds an agent.

Run:  cd backend && uv run --with pytest pytest tests/test_agent.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
from agents import MaxTurnsExceeded
from agents.tool_context import ToolContext
from fastapi import HTTPException

from models import LogCreate, ParsedItem, ParsedLog  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes import weights as weights_route  # noqa: E402
from services import agent as agent_svc  # noqa: E402
from test_tenancy import (  # noqa: E402
    _become,
    _scalar,
    _stub_pipeline,
    insert_alias,
    insert_food,
    isolated_db,
)

TZ = "America/Los_Angeles"
NOW = datetime(2026, 9, 6, 15, 30, tzinfo=timezone.utc)  # 08:30 in LA
LOCAL_DAY = NOW.astimezone(ZoneInfo(TZ)).date()


def _run(coro):
    return asyncio.run(coro)


def _ctx(name: str, args: str) -> ToolContext:
    return ToolContext(context=None, tool_name=name, tool_call_id="call_1", tool_arguments=args)


def _tool(agent, name: str):
    return next(t for t in agent.tools if t.name == name)


async def _invoke(agent, name: str, **kwargs) -> dict:
    args = json.dumps(kwargs)
    return json.loads(await _tool(agent, name).on_invoke_tool(_ctx(name, args), args))


def _deps(**over) -> agent_svc.AgentDeps:
    calls: dict = {"food": [], "weight": []}

    async def stage_food(text: str) -> dict:
        calls["food"].append(text)
        return {"meal": "breakfast", "items": [{"phrase": "eggs", "status": "searching"}]}

    async def record_weight(value: float, unit: str, day: date) -> dict:
        calls["weight"].append((value, unit, day))
        return {"day": day.isoformat(), "weight": value, "unit": unit}

    kw = dict(
        stage_food=stage_food,
        record_weight=record_weight,
        now_local=NOW.astimezone(ZoneInfo(TZ)),
        unit_pref="lb",
    )
    kw.update(over)
    deps = agent_svc.AgentDeps(**kw)
    deps.calls = calls  # type: ignore[attr-defined]
    return deps


class _FakeRun:
    def __init__(self, reply: str):
        self._reply = reply

    def final_output_as(self, cls):
        return cls(reply=self._reply)

    def to_input_list(self):
        return [{"type": "message", "role": "assistant", "content": self._reply}]


def _fake_runner(plan: list[tuple[str, dict]], reply: str):
    """A Runner whose run() drives the REAL tool closures in `plan` order and
    answers with `reply` — what a model turn would do, minus the model."""

    class FakeRunner:
        calls = 0

        @staticmethod
        async def run(agent, agent_input, max_turns):
            FakeRunner.calls += 1
            FakeRunner.input = agent_input
            for name, kwargs in plan:
                out = await _invoke(agent, name, **kwargs)
                assert "error" not in out, out
            return _FakeRun(reply)

    return FakeRunner


class _RefusingRunner:
    @staticmethod
    async def run(agent, agent_input, max_turns):
        raise AssertionError("the agent must not run here")


# ---------------------------------------------------------------------------
# 1. services/agent — the tools and the run, offline
# ---------------------------------------------------------------------------


def test_log_weight_tool_converts_days_ago_and_refuses_in_code():
    async def scenario():
        deps = _deps()
        state: dict = {}
        agent = agent_svc.build_agent(deps, state)

        out = await _invoke(agent, "log_weight", weight=180.4, unit="lb", days_ago=1)
        assert out == {"day": (LOCAL_DAY - timedelta(days=1)).isoformat(), "weight": 180.4, "unit": "lb"}
        assert deps.calls["weight"] == [(180.4, "lb", LOCAL_DAY - timedelta(days=1))]
        assert state["weight"] == out

        # days_ago is clamped in code — a hallucinated 999 lands within the month
        await _invoke(agent, "log_weight", weight=82, unit="kg", days_ago=999)
        assert deps.calls["weight"][-1] == (82.0, "kg", LOCAL_DAY - timedelta(days=agent_svc.MAX_DAYS_AGO))
        await _invoke(agent, "log_weight", weight=82, unit="kg", days_ago=-3)
        assert deps.calls["weight"][-1][2] == LOCAL_DAY

        # the route's range refusal (ValueError) becomes a tool error the
        # model can act on — and leaves the run's state untouched
        async def refuse(value, unit, day):
            raise ValueError("10 kg is outside the range")

        deps2 = _deps(record_weight=refuse)
        state2: dict = {}
        agent2 = agent_svc.build_agent(deps2, state2)
        out = await _invoke(agent2, "log_weight", weight=10, unit="kg")
        assert out["error"] == "invalid_weight" and "10 kg" in out["message"]
        assert state2["weight"] is None

    _run(scenario())


def test_log_food_tool_runs_once_per_utterance():
    async def scenario():
        deps = _deps()
        state: dict = {}
        agent = agent_svc.build_agent(deps, state)
        out = await _invoke(agent, "log_food", text="two eggs")
        assert out["items"][0]["phrase"] == "eggs"
        assert state["food_calls"] == 1
        # a second call stages nothing again (item indexes would collide)
        out = await _invoke(agent, "log_food", text="a coffee")
        assert out["error"] == "already_called"
        assert deps.calls["food"] == ["two eggs"] and state["food_calls"] == 1

    _run(scenario())


def test_log_food_failure_frees_the_slot_and_is_not_reported_as_staged():
    async def scenario():
        calls: list[str] = []

        async def flaky(text: str) -> dict:
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("decompose is down")
            return {"meal": "lunch", "items": [{"phrase": "rice", "status": "searching"}]}

        deps = _deps(stage_food=flaky)
        state: dict = {}
        agent = agent_svc.build_agent(deps, state)
        name, args = "log_food", json.dumps({"text": "rice"})
        out = await _tool(agent, name).on_invoke_tool(_ctx(name, args), args)
        # the sanitized tool error reaches the model; the internals do not
        assert "RuntimeError" in out and "decompose" not in out
        assert state["food_calls"] == 0  # the slot is free again: nothing was staged
        assert agent_svc._fallback_reply(state) == "I couldn't work that one out — try again, or type it."
        out = await _invoke(agent, name, text="rice")
        assert out["items"][0]["phrase"] == "rice" and state["food_calls"] == 1
        assert calls == ["rice", "rice"]

    _run(scenario())


def test_agent_input_carries_the_context_block():
    deps = _deps(last_weight={"day": "2026-09-02", "weight": 181.0, "unit": "lb"}, profile_notes="trains at 6pm")
    text = agent_svc.agent_input("I weigh 180", deps)
    assert "2026-09-06 08:30, Sunday" in text
    assert "Usual weight unit: lb" in text
    assert "Last weigh-in: 181 lb on 2026-09-02" in text
    assert "Profile notes: trains at 6pm" in text
    assert text.endswith('Utterance: "I weigh 180"')
    assert "Last weigh-in: none yet" in agent_svc.agent_input("x", _deps())


def test_run_agent_reports_tool_state_and_survives_max_turns(monkeypatch):
    async def scenario():
        deps = _deps()
        runner = _fake_runner([("log_weight", {"weight": 180.4, "unit": "lb"})], "Logged 180.4 lb.")
        monkeypatch.setattr(agent_svc, "Runner", runner)
        run = await agent_svc.run_agent("I weigh 180.4 pounds", deps)
        assert run.reply == "Logged 180.4 lb." and run.food_calls == 0
        assert run.weight == {"day": LOCAL_DAY.isoformat(), "weight": 180.4, "unit": "lb"}
        assert run.transcript and not run.max_turns_hit
        assert runner.input.endswith('Utterance: "I weigh 180.4 pounds"')

        # max turns: whatever the tools did stands, the reply is built in code
        class Looping:
            @staticmethod
            async def run(agent, agent_input, max_turns):
                await _invoke(agent, "log_food", text="two eggs")
                raise MaxTurnsExceeded("max turns")

        monkeypatch.setattr(agent_svc, "Runner", Looping)
        run = await agent_svc.run_agent("two eggs", deps)
        assert run.max_turns_hit and run.food_calls == 1 and run.weight is None
        assert run.reply == "I staged what you ate — check the list."
        assert agent_svc._fallback_reply({}) == "I couldn't work that one out — try again, or type it."

    _run(scenario())


# ---------------------------------------------------------------------------
# 2. routes/weights — storage, the body sync, tenancy, the routes
# ---------------------------------------------------------------------------


async def _body_weight(conn) -> float | None:
    v = await _scalar(conn, "SELECT value->>'weight_kg' FROM settings WHERE key = 'body'")
    return float(v) if v is not None else None


def test_record_weight_one_row_per_day_body_sync_and_tenancy():
    async def scenario():
        async with isolated_db() as conn:
            day = date(2026, 9, 6)
            e = await weights_route.record_weight(weight=180, unit="lb", day=day, source="manual")
            assert (e.weight_kg, e.unit, e.source, e.day, e.log_id) == (81.65, "lb", "manual", day, None)
            assert await _body_weight(conn) == 81.65  # the wizard's current weight follows

            # the same day again: updated in place, never a second row
            e = await weights_route.record_weight(weight=181, unit="lb", day=day, source="manual")
            assert e.weight_kg == 82.1
            assert await _scalar(conn, "SELECT count(*) FROM weights") == 1
            assert await _body_weight(conn) == 82.1

            # a back-dated entry behind the newest one leaves the body alone
            e = await weights_route.record_weight(weight=77, unit="kg", day=day - timedelta(days=1), source="manual")
            assert e.weight_kg == 77 and await _scalar(conn, "SELECT count(*) FROM weights") == 2
            assert await _body_weight(conn) == 82.1

            latest = await weights_route.latest_entry()
            assert latest is not None and latest.day == day
            before = await weights_route.latest_entry(before=day)
            assert before is not None and before.day == day - timedelta(days=1)

            # the range is the goal wizard's (30–300 kg), refused before any write
            with pytest.raises(ValueError):
                await weights_route.record_weight(weight=10, unit="kg", day=day, source="manual")
            with pytest.raises(ValueError):
                await weights_route.record_weight(weight=700, unit="lb", day=day, source="manual")
            assert await _scalar(conn, "SELECT count(*) FROM weights") == 2

            # another tenant sees none of it (RLS on weights, 003)
            await _become(conn, "weights-other")
            assert await weights_route.latest_entry() is None
            assert (await weights_route.get_weights(days=365)).entries == []

    _run(scenario())


def test_weights_routes_get_and_put():
    async def scenario():
        async with isolated_db() as conn:
            today = await weights_route.local_date(datetime.now(timezone.utc))
            put = await weights_route.put_weight(weights_route.WeightPut(weight=75.5, unit="kg"))
            assert put.day == today and put.weight_kg == 75.5 and put.source == "manual"
            await weights_route.put_weight(
                weights_route.WeightPut(weight=76, unit="kg", day=today - timedelta(days=2))
            )
            res = await weights_route.get_weights(days=30)
            assert res.latest is not None and res.latest.day == today
            assert [e.day for e in res.entries] == [today, today - timedelta(days=2)]  # newest first
            assert (await weights_route.get_weights(days=1)).entries[0].day == today
            assert len((await weights_route.get_weights(days=1)).entries) == 1
            with pytest.raises(HTTPException) as exc:
                await weights_route.put_weight(weights_route.WeightPut(weight=5, unit="kg"))
            assert exc.value.status_code == 422
            assert weights_route.to_kg(180.4, "lb") == 81.83
            assert weights_route.from_kg(81.83, "lb") == 180.4

    _run(scenario())


# ---------------------------------------------------------------------------
# 3. POST /api/logs, agent=true — the route end to end (model faked)
# ---------------------------------------------------------------------------


def _parsed(*phrases: tuple[str, float | None]) -> ParsedLog:
    return ParsedLog(
        items=[
            ParsedItem(phrase=p, phrase_kind="food", qty=q, unit=None, state=None,
                       brand=None, library_match=None, meal_ref=None)
            for p, q in phrases
        ],
        meal_hint=None, when_hint=None, save_intent=None,
    )


def _log(text: str, *, agent: bool) -> LogCreate:
    return LogCreate(id=uuid4(), text=text, captured_at=NOW, tz=TZ, agent=agent)


def test_agent_lane_stages_food_and_records_weight_under_the_log(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            seen: list[str] = []

            async def decompose(text, ctx):
                seen.append(text)
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            runner = _fake_runner(
                [("log_food", {"text": "two eggs"}),
                 ("log_weight", {"weight": 180.4, "unit": "lb", "days_ago": 0})],
                "Staged 1 item and logged 180.4 lb.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)

            body = _log("two eggs and I weigh 180.4 pounds", agent=True)
            resp = await logs_routes.create_log(body)

            # the food part went through the ordinary pipeline, verbatim
            assert seen == ["two eggs"]
            assert [(it.phrase, it.qty, it.status) for it in resp.items] == [("eggs", 2.0, "searching")]
            assert resp.log_id == body.id and resp.meal.value == "breakfast"
            # the model's reply, the tool's weigh-in
            assert resp.agent is not None and resp.agent.reply == "Staged 1 item and logged 180.4 lb."
            w = resp.agent.weight
            assert w is not None and (w.weight_kg, w.unit, w.source, w.day, w.log_id) == (
                81.83, "lb", "agent", LOCAL_DAY, body.id,
            )
            # persisted: the weigh-in under this log, the item row, the log's timings
            row = await (await conn.execute(
                "SELECT weight_kg, unit, source, log_id::text, day FROM weights"
            )).fetchone()
            assert row["log_id"] == str(body.id) and row["day"] == LOCAL_DAY and float(row["weight_kg"]) == 81.83
            assert await _scalar(conn, "SELECT count(*) FROM item_resolutions WHERE log_id = %s", (body.id,)) == 1
            log = await (await conn.execute("SELECT * FROM logs WHERE id = %s", (body.id,))).fetchone()
            assert log["status"] == "staged" and log["decompose_ms"] is not None
            assert runner.calls == 1
            assert "Usual weight unit: lb" in runner.input and "2026-09-06 08:30" in runner.input

            # the next agent run sees this weigh-in as the last one, in its unit
            runner2 = _fake_runner([], "Nothing to log.")
            monkeypatch.setattr(agent_svc, "Runner", runner2)
            resp2 = await logs_routes.create_log(_log("hello there", agent=True))
            assert resp2.items == [] and resp2.agent is not None and resp2.agent.weight is None
            assert resp2.agent.reply == "Nothing to log."
            assert "Last weigh-in: 180.4 lb on 2026-09-06" in runner2.input
            assert seen == ["two eggs"]  # no decompose for an utterance with no food

    _run(scenario())


def test_agent_lane_weight_only_leaves_an_itemless_log(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)  # decompose would fail loudly if called
            runner = _fake_runner(
                [("log_weight", {"weight": 82.5, "unit": "kg", "days_ago": 1})],
                "Logged 82.5 kg for yesterday.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = _log("yesterday morning I weighed 82.5 kilos", agent=True)
            resp = await logs_routes.create_log(body)
            assert resp.items == [] and resp.saved_meal is None and resp.note is None
            assert resp.agent is not None and resp.agent.weight is not None
            assert resp.agent.weight.day == LOCAL_DAY - timedelta(days=1)
            assert resp.agent.weight.weight_kg == 82.5 and resp.agent.weight.unit == "kg"
            assert await _scalar(conn, "SELECT count(*) FROM item_resolutions WHERE log_id = %s", (body.id,)) == 0
            log = await (await conn.execute("SELECT status, raw_input FROM logs WHERE id = %s", (body.id,))).fetchone()
            assert log["status"] == "staged" and log["raw_input"] == body.text

    _run(scenario())


def test_short_circuit_still_skips_the_agent(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            food_id = await insert_food(conn)
            await insert_alias(conn, food_id)  # 'two eggs' → tier 1, whole utterance
            _stub_pipeline(monkeypatch)
            monkeypatch.setattr(agent_svc, "Runner", _RefusingRunner)
            resp = await logs_routes.create_log(_log("two eggs", agent=True))
            assert len(resp.items) == 1 and resp.items[0].status == "resolved" and resp.items[0].tier == 1
            assert resp.agent is None
            assert await _scalar(conn, "SELECT short_circuit FROM logs WHERE id = %s", (resp.log_id,)) is True

    _run(scenario())


def test_plain_log_never_builds_the_agent(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            seen: list[str] = []

            async def decompose(text, ctx):
                seen.append(text)
                return _parsed(("eggs", 2))

            _stub_pipeline(monkeypatch, decompose=decompose)
            monkeypatch.setattr(agent_svc, "Runner", _RefusingRunner)
            resp = await logs_routes.create_log(_log("two eggs and I weigh 180 pounds", agent=False))
            assert seen == ["two eggs and I weigh 180 pounds"]  # the whole utterance, as before
            assert resp.agent is None and len(resp.items) == 1
            assert await _scalar(conn, "SELECT count(*) FROM weights") == 0

    _run(scenario())
