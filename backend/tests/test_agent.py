"""Agent mode (DESIGN.md §6.4, 2026-09-06) + the weigh-in log (§4 `weights`, 003).

Offline: the Runner is a fake that drives the REAL tool closures, the way
test_tier3_hardening does for tier 3 (test_tenancy's _stub_pipeline refuses
any model a test did not set). Database work runs inside one force-rollback transaction
(isolated_db) against the live database — 003 is applied in-transaction
while the table is not there yet (tenancy_harness), so nothing persists.

1. services/agent: log_weight converts and refuses in code, log_food runs
   once per utterance, the fallback reply is built from tool-side state,
   run_agent reports what the tools did. (The estimate tool, the wire events
   and the streaming route are test_agent_chat.py's.)
2. routes/weights: kg storage, one row per day updated in place,
   settings.body.weight_kg follows the newest weigh-in only, RLS keeps
   tenants apart; GET / PUT /api/weights.
3. POST /api/logs: the §3.0 short-circuit still wins with no agent run;
   otherwise the agent names the foods (no decompose since 2026-09-11) —
   one of his own by id — and records the weigh-in under the log's id, and
   the response carries both; a weight-only utterance leaves an itemless log.

Run:  cd backend && uv run --with pytest pytest tests/test_agent.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
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
from agents import MaxTurnsExceeded
from agents.tool_context import ToolContext
from fastapi import HTTPException

from models import LogCreate  # noqa: E402
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


def food(phrase: str, **over) -> dict:
    """One log_food item as the model would send it — every field spelled out,
    the strict schema requires them (§6.4, 2026-09-11: the agent names the
    foods itself; there is no decompose step)."""
    return {"phrase": phrase, "saved_meal": False, "food_id": None, "qty": None,
            "unit": None, "note": None, **over}


def _deps(**over) -> agent_svc.AgentDeps:
    calls: dict = {"food": [], "estimate": [], "weight": [], "meal": [], "update": [], "past": []}

    async def stage_food(items, meal_hint=None, when_hint=None, when_day_offset=None,
                         when_meal=None) -> dict:
        calls["food"].append(([it.model_dump() for it in items], meal_hint, when_hint,
                              when_day_offset, when_meal))
        return {"meal": "breakfast",
                "items": [{"phrase": it.phrase, "status": "resolved"} for it in items]}

    async def stage_estimate(items, basis: str, meal_hint=None, when_hint=None,
                             when_day_offset=None, when_meal=None) -> dict:
        calls["estimate"].append(([it.model_dump() for it in items], basis, meal_hint,
                                  when_hint, when_day_offset, when_meal))
        return {"meal": meal_hint or "lunch", "basis": basis,
                "items": [{"phrase": it.phrase, "status": "searching"} for it in items]}

    async def record_weight(value: float, unit: str, day: date) -> dict:
        calls["weight"].append((value, unit, day))
        return {"day": day.isoformat(), "weight": value, "unit": unit}

    async def discard_log(log_id: str) -> dict:
        calls.setdefault("discard", []).append(log_id)
        return {"log_id": log_id, "status": "discarded"}

    async def save_meal(name: str, triggers, log_id, items) -> dict:
        calls["meal"].append(
            (name, triggers, log_id, [it.model_dump() for it in items] if items else None)
        )
        return {"meal": name, "is_active": True, "item_count": 1,
                "items": ["egg 2 × 1 large"], "trigger_phrases": triggers or [name.lower()]}

    async def update_meal(name: str, new_name, triggers, is_active, set_qty, remove) -> dict:
        calls["update"].append((
            name, new_name, triggers, is_active,
            [c.model_dump() for c in set_qty] if set_qty else None, remove,
        ))
        return {"meal": new_name or name, "is_active": True if is_active is None else is_active,
                "item_count": 1, "items": ["egg 2 × 1 large"], "trigger_phrases": triggers or []}

    async def past_foods(query, limit) -> list[dict]:
        calls["past"].append((query, limit))
        return [{"food_id": "f-1", "food": "eggs", "qty": 2.0, "serving_id": "s-1",
                 "serving": "1 large", "day": "2026-09-05", "meal": "breakfast"}]

    kw = dict(
        stage_food=stage_food,
        stage_estimate=stage_estimate,
        record_weight=record_weight,
        discard_log=discard_log,
        past_foods=past_foods,
        save_meal=save_meal,
        update_meal=update_meal,
        now_local=NOW.astimezone(ZoneInfo(TZ)),
        unit_pref="lb",
    )
    kw.update(over)
    deps = agent_svc.AgentDeps(**kw)
    deps.calls = calls  # type: ignore[attr-defined]
    return deps


class _FakeStream:
    """What Runner.run_streamed hands back: the run happens while its events
    are iterated. Each planned tool call is driven through the REAL tool
    closure and surfaces as the SDK's tool_called / tool_output events, built
    from the SDK's own item classes so services/agent.wire_events sees the
    real shapes."""

    def __init__(self, agent, plan, reply):
        self._agent, self._plan, self._reply = agent, plan, reply
        self.final_output = None

    async def stream_events(self):
        from agents.items import ToolCallItem, ToolCallOutputItem
        from openai.types.responses import ResponseFunctionToolCall

        for n, (name, kwargs) in enumerate(self._plan):
            call_id = f"call_{n + 1}"
            raw = ResponseFunctionToolCall(
                id=f"fc_{n + 1}", call_id=call_id, name=name,
                arguments=json.dumps(kwargs), type="function_call",
            )
            yield SimpleNamespace(
                type="run_item_stream_event", name="tool_called",
                item=ToolCallItem(raw_item=raw, agent=self._agent),
            )
            args = json.dumps(kwargs)
            out = await _tool(self._agent, name).on_invoke_tool(_ctx(name, args), args)
            assert "error" not in json.loads(out), out
            yield SimpleNamespace(
                type="run_item_stream_event", name="tool_output",
                item=ToolCallOutputItem(
                    raw_item={"type": "function_call_output", "call_id": call_id, "output": out},
                    output=out, agent=self._agent,
                ),
            )
        self.final_output = agent_svc.AgentReply(reply=self._reply, options=None)

    def to_input_list(self):
        return [{"type": "message", "role": "assistant", "content": self._reply}]


def _fake_runner(plan: list[tuple[str, dict]], reply: str):
    """A Runner whose run_streamed() drives the REAL tool closures in `plan`
    order and answers with `reply` — what a model turn would do, minus the model."""

    class FakeRunner:
        calls = 0

        @staticmethod
        def run_streamed(agent, agent_input, max_turns):
            FakeRunner.calls += 1
            FakeRunner.input = agent_input
            return _FakeStream(agent, plan, reply)

    return FakeRunner


class _RefusingRunner:
    @staticmethod
    def run_streamed(agent, agent_input, max_turns):
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
        out = await _invoke(agent, "log_food", items=[food("eggs", qty=2)])
        assert out["items"][0]["phrase"] == "eggs"
        assert state["food_calls"] == 1
        # a second call stages nothing again (item indexes would collide)
        out = await _invoke(agent, "log_food", items=[food("coffee")])
        assert out["error"] == "already_called"
        assert [[it["phrase"] for it in c[0]] for c in deps.calls["food"]] == [["eggs"]]
        assert deps.calls["food"][0][0][0]["qty"] == 2 and state["food_calls"] == 1

    _run(scenario())


def test_log_food_validates_items_and_passes_the_time_fields():
    """The agent names the foods (§6.4, 2026-09-11): the tool refuses an empty
    list, a blank phrase and a non-positive amount before anything is
    staged, drops the tail past MAX_FOOD_ITEMS out loud, and hands the meal
    and day fields through exactly as log_estimate does."""
    async def scenario():
        deps = _deps()
        state: dict = {}
        agent = agent_svc.build_agent(deps, state)
        assert (await _invoke(agent, "log_food", items=[]))["error"] == "empty"
        assert (await _invoke(agent, "log_food", items=[food("  ")]))["error"] == "empty_phrase"
        assert (await _invoke(agent, "log_food", items=[food("rice", qty=0)]))["error"] == "invalid_qty"
        assert deps.calls["food"] == [] and state["food_calls"] == 0

        many = [food(f"food {i}") for i in range(agent_svc.MAX_FOOD_ITEMS + 2)]
        out = await _invoke(agent, "log_food", items=many, meal_hint="breakfast",
                            when_hint="this morning", when_day_offset=0, when_meal="breakfast")
        assert out["dropped"] == [f"food {i}" for i in range(agent_svc.MAX_FOOD_ITEMS, agent_svc.MAX_FOOD_ITEMS + 2)]
        items, meal_hint, when_hint, offset, when_meal = deps.calls["food"][0]
        assert len(items) == agent_svc.MAX_FOOD_ITEMS
        assert (meal_hint, when_hint, offset, when_meal) == ("breakfast", "this morning", 0, "breakfast")
        assert state["food_found"] == agent_svc.MAX_FOOD_ITEMS and state["food_missing"] == 0

    _run(scenario())


def test_log_food_failure_frees_the_slot_and_is_not_reported_as_staged():
    async def scenario():
        calls: list[str] = []

        async def flaky(items, *rest) -> dict:
            calls.append(items[0].phrase)
            if len(calls) == 1:
                raise RuntimeError("the cascade is down")
            return {"meal": "lunch", "items": [{"phrase": "rice", "status": "searching"}]}

        deps = _deps(stage_food=flaky)
        state: dict = {}
        agent = agent_svc.build_agent(deps, state)
        name, args = "log_food", json.dumps({"items": [food("rice")]})
        out = await _tool(agent, name).on_invoke_tool(_ctx(name, args), args)
        # the sanitized tool error reaches the model; the internals do not
        assert "RuntimeError" in out and "cascade" not in out
        assert state["food_calls"] == 0  # the slot is free again: nothing was staged
        assert agent_svc._fallback_reply(state) == "I couldn't work that one out — try again, or type it."
        out = await _invoke(agent, name, items=[food("rice")])
        assert out["items"][0]["phrase"] == "rice" and state["food_calls"] == 1
        assert calls == ["rice", "rice"]
        # a row still being looked up is never reported as staged
        assert (state["food_found"], state["food_pending"], state["food_missing"]) == (0, 1, 0)
        assert agent_svc._fallback_reply(state) == "I am still looking up what you ate — check the list."
        assert agent_svc._fallback_reply({"food_calls": 1, "food_missing": 2}) == (
            "I couldn't find what you ate — check the list.")

    _run(scenario())


def test_agent_input_carries_the_context_block():
    deps = _deps(last_weight={"day": "2026-09-02", "weight": 181.0, "unit": "lb"}, profile_notes="trains at 6pm")
    text = agent_svc.agent_input("I weigh 180", deps)
    assert "2026-09-06 08:30, Sunday" in text
    assert "Usual weight unit: lb" in text
    assert "Last weigh-in: 181 lb on 2026-09-02" in text
    assert "Profile notes: trains at 6pm" in text
    assert text.endswith('Utterance: "I weigh 180"')
    assert "Earlier in this conversation: nothing" in text
    assert "Last weigh-in: none yet" in agent_svc.agent_input("x", _deps())
    # their diary foods ride in, any lane, id first — what a misheard name is
    # matched against and what log_food names by id (§6.4, 2026-09-11)
    text = agent_svc.agent_input("Tom Katsu, Winston Roman", _deps(my_foods=["f-1: tonkotsu ramen (Nongshim)"]))
    assert "Foods they have logged, newest first (food_id: name (brand)):\n- f-1: tonkotsu ramen (Nongshim)" in text
    assert "Foods they have logged, newest first (food_id: name (brand)):\n(none yet)" in agent_svc.agent_input("x", _deps())
    # the conversation rides in: what they said, what we replied, each staged log
    deps = _deps(history=[
        {"role": "user", "text": "a platter of costco shrimp"},
        {"role": "agent", "text": "Which one?", "log_id": None, "items": None, "state": None},
        {"role": "agent", "text": "staged 2 items", "log_id": "11111111-1111-1111-1111-111111111111",
         "items": ["Plain cooked shrimp — 12.5 × 4 large", "cocktail sauce — 3.4 × 100 g"], "state": "staged"},
    ])
    text = agent_svc.agent_input("actually only half", deps)
    assert 'They said: "a platter of costco shrimp"' in text
    assert 'You replied: "Which one?"' in text
    assert "Log 11111111-1111-1111-1111-111111111111 (staged): Plain cooked shrimp — 12.5 × 4 large; cocktail sauce" in text
    assert text.index("Earlier in this conversation") < text.index('Utterance: "actually only half"')


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
        class _LoopingStream:
            def __init__(self, agent):
                self._agent = agent

            async def stream_events(self):
                await _invoke(self._agent, "log_food", items=[food("eggs", qty=2)])
                raise MaxTurnsExceeded("max turns")
                yield  # pragma: no cover — makes this an async generator

        class Looping:
            @staticmethod
            def run_streamed(agent, agent_input, max_turns):
                return _LoopingStream(agent)

        monkeypatch.setattr(agent_svc, "Runner", Looping)
        run = await agent_svc.run_agent("two eggs", deps)
        assert run.max_turns_hit and run.food_calls == 1 and run.weight is None
        assert run.reply == "I staged what you ate — check the list."
        assert agent_svc._fallback_reply({}) == "I couldn't work that one out — try again, or type it."
        assert agent_svc._fallback_reply({"estimate_calls": 1, "weight": {"x": 1}}) == (
            "I staged an estimate for you to check and logged your weight — check the list."
        )
        # never "staged" for a food the lookup did not find (trace 30e72425)
        assert agent_svc._fallback_reply({"food_calls": 1, "food_found": 1, "food_missing": 1}) == (
            "I staged what you ate and couldn't find 1 of the foods — check the list."
        )

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
# 3. POST /api/logs — the agent lane end to end (model faked)
# ---------------------------------------------------------------------------


def _log(text: str) -> LogCreate:
    return LogCreate(id=uuid4(), text=text, captured_at=NOW, tz=TZ)


def test_agent_lane_stages_food_and_records_weight_under_the_log(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)  # tracing off; the model is the fake Runner below
            runner = _fake_runner(
                [("log_food", {"items": [food("eggs", qty=2)]}),
                 ("log_weight", {"weight": 180.4, "unit": "lb", "days_ago": 0})],
                "Staged 1 item and logged 180.4 lb.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)

            body = _log("two eggs and I weigh 180.4 pounds")
            resp = await logs_routes.create_log(body)

            # the foods the agent named went straight to the cascade — no decompose
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
            assert log["status"] == "staged" and log["decompose_ms"] is None  # no decompose ran
            assert log["trace_id"] == "trace-test"  # commit scores find the run by it
            assert runner.calls == 1
            assert "Usual weight unit: lb" in runner.input and "2026-09-06 08:30" in runner.input

            # the next agent run sees this weigh-in as the last one, in its unit
            runner2 = _fake_runner([], "Nothing to log.")
            monkeypatch.setattr(agent_svc, "Runner", runner2)
            resp2 = await logs_routes.create_log(_log("hello there"))
            assert resp2.items == [] and resp2.agent is not None and resp2.agent.weight is None
            assert resp2.agent.reply == "Nothing to log."
            assert "Last weigh-in: 180.4 lb on 2026-09-06" in runner2.input

    _run(scenario())


def test_agent_lane_logs_his_own_food_by_id_and_keeps_the_note(monkeypatch):
    """Trace 30e72425, offline: the tonkotsu ramen he SCANNED is in his diary,
    and the agent names it by id — staged resolved from his own row, tier 2,
    confident, no search. An id that is not one of his foods is dropped and
    its phrase goes to tier 3 with the agent's note on the row (tier 3 reads
    it as what the person meant). A saved_meal name that matches no saved
    meal is a plain food, never the "no saved meal called…" question row.
    The meal the agent heard applies to every row."""
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            ramen = await insert_food(conn)
            stranger = await insert_food(conn)  # cached, never eaten by this tenant
            await conn.execute(  # a barcode log: in the diary, no alias learned
                "INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, macros) "
                "VALUES (now(), 'dinner', %s, 's', 1, '{}'::jsonb)",
                (ramen,),
            )
            runner = _fake_runner(
                [("log_food", {"items": [
                    food("tonkotsu ramen", food_id=ramen),
                    food("pork buns", food_id=stranger, note="from the Costco food court"),
                    food("usual lunch", saved_meal=True),
                ], "meal_hint": "breakfast"})],
                "Took 'Tom Katsu' as your tonkotsu ramen.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = _log("This morning for breakfast at a Costco, Tom Katsu, Winston Roman.")
            resp = await logs_routes.create_log(body)

            rows = {it.phrase: it for it in resp.items}
            got = rows["tonkotsu ramen"]
            assert (got.status, got.tier, got.confidence, got.food.id) == ("resolved", 2, "high", ramen)
            assert rows["pork buns"].status == "searching" and rows["pork buns"].food is None
            assert rows["usual lunch"].status == "searching" and rows["usual lunch"].question is None
            assert resp.meal.value == "breakfast" and {it.meal.value for it in resp.items} == {"breakfast"}
            # the agent's note rides the row that still needs looking up — only that one
            stored = await (await conn.execute(
                "SELECT phrase, staged->>'lookup_note' AS note FROM item_resolutions WHERE log_id = %s",
                (body.id,),
            )).fetchall()
            assert {r["phrase"]: r["note"] for r in stored} == {"tonkotsu ramen": None, "pork buns": "from the Costco food court",
                             "usual lunch": None}
            # the context the model read listed his diary food by id
            assert f"- {ramen}: egg" in runner.input

    _run(scenario())


def test_agent_lane_weight_only_leaves_an_itemless_log(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)  # tracing off; the model is the fake Runner below
            runner = _fake_runner(
                [("log_weight", {"weight": 82.5, "unit": "kg", "days_ago": 1})],
                "Logged 82.5 kg for yesterday.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = _log("yesterday morning I weighed 82.5 kilos")
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
            resp = await logs_routes.create_log(_log("two eggs"))
            assert len(resp.items) == 1 and resp.items[0].status == "resolved" and resp.items[0].tier == 1
            assert resp.agent is None
            assert await _scalar(conn, "SELECT short_circuit FROM logs WHERE id = %s", (resp.log_id,)) is True

    _run(scenario())

