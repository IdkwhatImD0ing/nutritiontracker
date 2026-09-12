"""Saved meals through the agent (DESIGN.md §6.4 + §8.4, 2026-09-07) — Bill's
ruling that the agent can do whatever the UI can, saved meals first.

Offline: test_agent's fake Runner drives the REAL tool closures; decompose is
stubbed; database work runs inside one force-rollback transaction against
the live database (isolated_db).

1. services/agent: save_meal / update_meal refuse bad input in code (an empty
   name, two sources, a zero quantity, nothing to change, log_food still in
   flight); the per-run input lists the saved meals; the fallback reply
   names what was saved.
2. routes/logs: save_meal from a committed log the conversation names, from
   the newest conversation log, from the last committed one (§8.4 path a);
   a save in the same turn as the food stamps the staged rows and the commit
   builds the macro with the spoken triggers; foods in words resolve through
   tiers 1–2 with nothing staged and the unknown ones reported; update_meal
   renames, re-triggers, sets a quantity, removes, deactivates and
   reactivates by name and refuses what routes/meals refuses; a log outside
   the conversation, a discarded one and another tenant's meals are out of
   reach.
3. routes/agent: the stream shows the save_meal call, its result, the reply,
   and a done that carries saved_meal.
4. past_foods (2026-09-08): the one read — a row per food this account has
   logged with the amount it was last logged at, searchable by name; the ids
   it hands out rebuild a saved meal with the diary's own amounts, and a food
   this tenant never ate cannot join one by id.

Run:  cd backend && uv run --with pytest pytest tests/test_agent_meals.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from models import AgentTurn, CommitRequest, LogCreate  # noqa: E402
from resolver import foods_repo  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes import agent as agent_routes  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from services import agent as agent_svc  # noqa: E402
from test_agent import NOW, TZ, _ctx, _deps, _fake_runner, _log, _tool, food  # noqa: E402
from test_meals import breakfast_fixture  # noqa: E402
from test_meals import insert_alias as insert_named_alias  # noqa: E402
from test_meals import insert_food as insert_named_food  # noqa: E402
from test_tenancy import (  # noqa: E402
    _become,
    _scalar,
    _stub_pipeline,
    insert_alias,
    insert_entry,
    insert_food,
    insert_log,
    isolated_db,
)

BANANA = {
    "serving_id": "srv-medium",
    "description": "1 medium",
    "grams": 118,
    "macros": {"kcal": 105, "protein_g": 1.3, "carb_g": 27, "fat_g": 0.4},
}
HUNDRED_G = {
    "serving_id": "srv-100g",
    "description": "100 g",
    "grams": 100,
    "macros": {"kcal": 99, "protein_g": 24, "carb_g": 0.2, "fat_g": 0.3},
}


def _run(coro):
    return asyncio.run(coro)


async def _raw(agent, tool: str, **kwargs) -> str:
    """The tool's own serialized answer — what actually reaches the model."""
    args = json.dumps(kwargs)
    return await _tool(agent, tool).on_invoke_tool(_ctx(tool, args), args)


async def _call(agent, tool: str, **kwargs) -> dict:
    """test_agent._invoke, for tools whose own first parameter is `name`."""
    return json.loads(await _raw(agent, tool, **kwargs))


def _probe_runner(probe, reply: str = "Done."):
    """A Runner whose run is `probe(agent)` — direct tool invocations with
    their own assertions (the fake stream refuses an error output) — then
    `reply`. Records the run's input like test_agent's fake does."""

    class Runner:
        input = ""

        @staticmethod
        def run_streamed(agent, agent_input, max_turns):
            Runner.input = agent_input

            class S:
                final_output = agent_svc.AgentReply(reply=reply, options=None)

                async def stream_events(self):
                    await probe(agent)
                    return
                    yield  # pragma: no cover

                def to_input_list(self):
                    return []

            return S()

    return Runner


def _turn(text: str, log_id, state: str, items: list[str] | None = None) -> AgentTurn:
    return AgentTurn(role="agent", text=text, log_id=log_id, items=items or [], state=state)


async def _macro(conn, name: str) -> dict | None:
    return await (
        await conn.execute("SELECT * FROM meal_macros WHERE name = %s", (name,))
    ).fetchone()


async def _drain(resp) -> list[dict]:
    raw = b"".join([chunk async for chunk in resp.body_iterator])
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. services/agent — the tools' own guards, the context, the fallback
# ---------------------------------------------------------------------------


def test_meal_tools_refuse_bad_input_in_code():
    async def scenario():
        deps = _deps()
        state: dict = {}
        agent = agent_svc.build_agent(deps, state)

        assert (await _call(agent, "save_meal", name="  "))["error"] == "invalid_name"
        egg = {"phrase": "egg", "qty": 1, "unit": None, "food_id": None, "serving_id": None}
        out = await _call(agent, "save_meal", name="x", log_id=str(uuid4()), items=[egg])
        assert out["error"] == "choose_one"
        out = await _call(agent, "save_meal", name="x", items=[dict(egg, qty=0)])
        assert out["error"] == "invalid_qty" and deps.calls["meal"] == []
        # an empty list named a source that holds nothing — never a silent
        # fall-through to the log ladder
        out = await _call(agent, "save_meal", name="x", items=[])
        assert out["error"] == "no_items" and deps.calls["meal"] == []
        # a staging tool claimed this turn and is not back yet: this turn's log
        # is not a target — the model is told to come back once it has returned
        state["estimate_calls"] = 1
        out = await _call(agent, "save_meal", name="x")
        assert out["error"] == "food_in_flight" and deps.calls["meal"] == []
        state["estimate_done"] = True
        state["food_calls"] = 1
        out = await _call(agent, "save_meal", name="x")
        assert out["error"] == "food_in_flight" and deps.calls["meal"] == []
        state["food_done"] = True
        out = await _call(agent, "save_meal", name=" x ", trigger_phrases=[" my x ", ""])
        assert "error" not in out and deps.calls["meal"] == [("x", ["my x"], None, None)]
        assert state["meal_calls"] == 1 and state["meal_note"] == "saved 'x'"
        # an explicit log or foods in words need no such wait
        out = await _call(agent, "save_meal", name="y", items=[egg, dict(egg, qty=None)])
        assert "error" not in out and deps.calls["meal"][-1][3] == [egg, dict(egg, qty=None)]

        assert (await _call(agent, "update_meal", name="x"))["error"] == "nothing_to_change"
        out = await _call(agent, "update_meal", name="x", set_qty=[{"food": "egg", "qty": 0}])
        assert out["error"] == "invalid_qty" and deps.calls["update"] == []
        out = await _call(agent, "update_meal", name="x", new_name=" y ", remove=["oats"])
        assert "error" not in out
        assert deps.calls["update"] == [("x", "y", None, None, None, ["oats"])]
        assert state["meal_calls"] == 3 and state["meal_note"] == "updated 'y'"
        # max turns: the reply is built from what the tools did (the food and
        # estimate calls were claimed above, so they are in it too)
        assert agent_svc._fallback_reply(state) == (
            "I staged what you ate and staged an estimate for you to check "
            "and updated 'y' — check the list."
        )

        # a staged target: saved at commit — the note says so
        async def pending(name, triggers, log_id, items):
            return {"meal": name, "pending": True, "log_id": "l1"}

        state2: dict = {}
        out = await _call(agent_svc.build_agent(_deps(save_meal=pending), state2), "save_meal", name="z")
        assert out["pending"] is True and state2["meal_note"] == "'z' will be saved when you commit"

        # no effect wired (a caller outside the lane): the tool says so
        bare = agent_svc.build_agent(_deps(save_meal=None, update_meal=None), {})
        assert (await _call(bare, "save_meal", name="x"))["error"] == "unavailable"
        assert (await _call(bare, "update_meal", name="x", is_active=False))["error"] == "unavailable"

    _run(scenario())


def test_agent_input_lists_saved_meals():
    lines = [
        "Usual breakfast: egg 2 × 1 large; whey 1 × 1 scoop — say: usual breakfast",
        "Old lunch (inactive): rice 1 × 1 cup — say: old lunch",
    ]
    text = agent_svc.agent_input("hi", _deps(saved_meals=lines))
    assert (
        "Saved meals (name: items — say: trigger phrases):\n"
        f"- {lines[0]}\n- {lines[1]}\n\n"
    ) in text
    assert "Saved meals (name: items — say: trigger phrases):\n(none yet)\n" in (
        agent_svc.agent_input("hi", _deps())
    )


# ---------------------------------------------------------------------------
# 2. routes/logs — the effects, against the database
# ---------------------------------------------------------------------------


def test_save_meal_from_a_conversation_log_and_the_fallbacks(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)  # no food in these utterances: decompose never runs
            fx = await breakfast_fixture(conn)
            tag = uuid4().hex[:6]
            name = f"Agent brekkie {tag}"

            # nothing committed yet: nothing to save (§8.4 path a has no log)
            async def nothing(agent):
                assert (await _call(agent, "save_meal", name=name))["error"] == "nothing_to_save"

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(nothing))
            resp0 = await logs_routes.create_log(_log(f"save that as {name}", agent=True))
            assert resp0.items == [] and resp0.saved_meal is None and resp0.note is None

            egg = await insert_food(conn)
            committed = await insert_log(conn, status="committed")
            await insert_entry(conn, committed, egg)  # 2 × "1 egg"
            history = [
                AgentTurn(role="user", text="two eggs"),
                _turn("staged 1 item", committed, "committed", ["egg — 2 × 1 egg"]),
            ]

            # the log the conversation names, saved now from its entries
            runner = _fake_runner(
                [("save_meal", {"name": name, "trigger_phrases": [f"Brekkie {tag}"],
                                "log_id": str(committed)})],
                "Saved it.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = LogCreate(id=uuid4(), text=f"save that as {name}", captured_at=NOW, tz=TZ,
                             agent=True, history=history)
            resp = await logs_routes.create_log(body)
            assert resp.items == [] and resp.note is None
            assert resp.saved_meal is not None and resp.saved_meal.name == name
            assert resp.saved_meal.item_count == 1 and resp.agent.reply == "Saved it."
            row = await _macro(conn, name)
            assert row["items"] == [{"food_id": egg, "serving_id": "s", "qty": 2.0}]
            assert row["trigger_phrases"] == [norm(f"Brekkie {tag}")] and row["is_active"]
            assert await _scalar(conn, "SELECT count(*) FROM item_resolutions WHERE log_id = %s", (body.id,)) == 0
            # the run read the saved meals as they stand, in words — the fixture's
            assert f"- Usual breakfast {fx.tag}: egg " in runner.input
            assert "2 × 1 large; oats " in runner.input and "1 × 1 scoop — say: " in runner.input
            assert f"say: {fx.trigger}, usual {fx.tag}" in runner.input

            # no log named: the newest log in the conversation — the same one,
            # so saying it twice updates the one macro (its triggers kept)
            runner2 = _fake_runner([("save_meal", {"name": name.upper()})], "Saved again.")
            monkeypatch.setattr(agent_svc, "Runner", runner2)
            resp2 = await logs_routes.create_log(LogCreate(
                id=uuid4(), text="save that as my brekkie", captured_at=NOW, tz=TZ,
                agent=True, history=history,
            ))
            assert resp2.saved_meal is not None and resp2.saved_meal.name == name
            assert await _scalar(conn, "SELECT count(*) FROM meal_macros WHERE lower(name) = lower(%s)", (name,)) == 1
            assert (await _macro(conn, name))["trigger_phrases"] == [norm(f"Brekkie {tag}")]
            assert f"- {name}: egg 2 × 1 egg — say: {norm(f'Brekkie {tag}')}" in runner2.input

            # no conversation either: the most recent committed log (§8.4 path a)
            latest = f"Latest {tag}"
            runner3 = _fake_runner([("save_meal", {"name": latest})], "Saved.")
            monkeypatch.setattr(agent_svc, "Runner", runner3)
            resp3 = await logs_routes.create_log(_log(f"save that as {latest}", agent=True))
            assert resp3.saved_meal is not None and resp3.saved_meal.name == latest
            assert (await _macro(conn, latest))["items"] == [{"food_id": egg, "serving_id": "s", "qty": 2.0}]

            # out of reach: a log the conversation does not name, a discarded one;
            # and a rename onto a name in use is refused as the Library refuses it
            discarded = await insert_log(conn, status="discarded")
            stale = history + [_turn("x", discarded, "discarded")]

            async def refusals(agent):
                out = await _call(agent, "save_meal", name="x", log_id=str(uuid4()))
                assert out["error"] == "unknown_log"
                out = await _call(agent, "save_meal", name="x", log_id=str(discarded))
                assert out["error"] == "not_saved" and out["status"] == "discarded"
                out = await _call(agent, "update_meal", name=latest, new_name=name)
                assert out["error"] == "duplicate_name" and name in out["message"]

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(refusals))
            resp4 = await logs_routes.create_log(LogCreate(
                id=uuid4(), text="save the other one", captured_at=NOW, tz=TZ,
                agent=True, history=stale,
            ))
            assert resp4.saved_meal is None and resp4.note is None
            assert (await _macro(conn, latest))["name"] == latest

    _run(scenario())


def test_save_in_an_estimate_only_turn_stamps_this_turns_log(monkeypatch):
    """log_estimate stages rows without ever setting result["food"] — the save
    must still find this turn's log, not fall through to the last committed
    one and save an unrelated meal under the name."""

    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)  # log_estimate never calls decompose
            tag = uuid4().hex[:6]
            shrimp = await insert_named_food(conn, f"shrimp {tag}", [HUNDRED_G])
            await insert_named_alias(conn, norm(f"shrimp {tag}"), shrimp)
            # the decoy the fall-through would have saved instead
            egg = await insert_food(conn)
            decoy = await insert_log(conn, status="committed")
            await insert_entry(conn, decoy, egg)

            name = f"Platter {tag}"
            item = {"phrase": f"shrimp {tag}", "qty": 400, "unit": "g",
                    "grams": None, "note": "the whole tray"}
            runner = _fake_runner(
                [("log_estimate", {"items": [item], "basis": "the whole 3 lb tray"}),
                 ("save_meal", {"name": name})],
                "Staged the platter — it saves when you commit.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = _log(f"a platter of shrimp, save that as {name}", agent=True)
            resp = await logs_routes.create_log(body)

            assert [it.estimated for it in resp.items] == ["portion"]
            # this turn's log, stamped — not the decoy, saved outright
            assert resp.saved_meal is None
            assert resp.note == f"'{name}' will be saved when you commit."
            assert await _macro(conn, name) is None
            stamped = await (await conn.execute(
                "SELECT staged->'save_intent'->>'name' AS n FROM item_resolutions "
                "WHERE log_id = %s", (body.id,),
            )).fetchall()
            assert [r["n"] for r in stamped] == [name]

    _run(scenario())


def test_a_committed_log_with_nothing_in_it_is_not_a_name_collision(monkeypatch):
    """routes/meals.meal_from_log answers 409 for an empty log too — reporting
    that as duplicate_name would send the agent to rename a meal that is fine."""

    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            empty = await insert_log(conn, status="committed")  # committed, no entries
            history = [_turn("committed 0 items", empty, "committed", [])]

            async def probe(agent):
                out = await _call(agent, "save_meal", name="ghost", log_id=str(empty))
                assert out["error"] == "empty_log", out
                assert "no committed items" in out["message"], out

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(probe))
            resp = await logs_routes.create_log(LogCreate(
                id=uuid4(), text="save that as ghost", captured_at=NOW, tz=TZ,
                agent=True, history=history,
            ))
            assert resp.saved_meal is None
            assert await _macro(conn, "ghost") is None

    _run(scenario())


def test_save_in_the_same_turn_stamps_the_staged_rows_and_commit_builds_the_macro(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            egg = await insert_food(conn)
            await insert_alias(conn, egg)  # "two eggs" → tier 1, resolved

            _stub_pipeline(monkeypatch)
            tag = uuid4().hex[:6]
            name = f"Same turn {tag}"
            triggers = [f"Same-Turn {tag}", f"st {tag}"]
            runner = _fake_runner(
                [("log_food", {"items": [food("two eggs", qty=2)]}),
                 ("save_meal", {"name": name, "trigger_phrases": triggers})],
                "Staged the eggs — saved as a meal when you commit.",
            )
            monkeypatch.setattr(agent_svc, "Runner", runner)
            body = _log(f"two eggs, save that as {name}", agent=True)
            resp = await logs_routes.create_log(body)
            assert [(it.phrase, it.status, it.qty) for it in resp.items] == [("two eggs", "resolved", 2.0)]
            # not saved yet: the note (the chat's bubble) says when
            assert resp.saved_meal is None and resp.note == f"'{name}' will be saved when you commit."
            assert await _scalar(conn, "SELECT count(*) FROM meal_macros WHERE name = %s", (name,)) == 0
            # the intent rides the row's staged jsonb, triggers included
            stamped = await (await conn.execute(
                "SELECT staged->'save_intent' AS si FROM item_resolutions WHERE log_id = %s", (body.id,)
            )).fetchall()
            assert [r["si"] for r in stamped] == [{"name": name, "trigger_phrases": triggers}]

            # the commit builds it (§3.5 step 4) — the spoken triggers, norm()'d
            done = await commit_route(body.id, CommitRequest(items=resp.items))
            assert done.saved_meal is not None and done.saved_meal.name == name
            assert done.saved_meal.item_count == 1
            row = await _macro(conn, name)
            assert row["trigger_phrases"] == [norm(t) for t in triggers]
            assert row["items"] == [{"food_id": egg, "serving_id": "s", "qty": 2.0}]

    _run(scenario())


def test_save_from_words_resolves_through_the_tiers_and_reports_the_unknown(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)  # words go straight to the cascade — no decompose
            fx = await breakfast_fixture(conn)
            tag = fx.tag
            banana = await insert_named_food(conn, f"banana {tag}", [BANANA])
            await insert_named_alias(conn, norm(f"banana {tag}"), banana)
            await insert_named_alias(conn, norm(f"whey {tag}"), fx.whey)
            await insert_named_alias(conn, norm(f"eggs {tag}"), fx.eggs)
            name = f"Protein snack {tag}"
            cold = f"zzqx {uuid4().hex[:8]}"  # in no database
            words = [
                {"phrase": f"whey {tag}", "qty": 1, "unit": "scoop", "food_id": None, "serving_id": None},
                {"phrase": f"banana {tag}", "qty": 2, "unit": None, "food_id": None, "serving_id": None},
                {"phrase": cold, "qty": 1, "unit": None, "food_id": None, "serving_id": None},
            ]

            async def create(agent):
                out = await _call(agent, "save_meal", name=name, items=words)
                assert "error" not in out, out
                assert out["meal"] == name and out["item_count"] == 2
                assert [u["phrase"] for u in out["unresolved"]] == [cold]
                assert out["unresolved"][0]["status"] in ("searching", "not_found", "needs_input")
                assert out["items"][0].startswith("whey ") and out["items"][0].endswith(" 1 × 1 scoop")
                assert out["items"][1].startswith("banana ") and out["items"][1].endswith(" 2 × 1 medium")
                # nothing in the databases: nothing written
                out = await _call(agent, "save_meal", name=f"Nothing {tag}", items=[words[2]])
                assert out["error"] == "unresolved" and [u["phrase"] for u in out["unresolved"]] == [cold]

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(create, "Saved 2 of 3."))
            body = _log(f"make a meal called {name}", agent=True)
            resp = await logs_routes.create_log(body)
            assert resp.items == [] and resp.saved_meal is not None
            assert resp.saved_meal.name == name and resp.saved_meal.item_count == 2
            row = await _macro(conn, name)
            assert row["items"] == [
                {"food_id": fx.whey, "serving_id": "srv-scoop", "qty": 1.0},
                {"food_id": banana, "serving_id": "srv-medium", "qty": 2.0},
            ]
            assert row["trigger_phrases"] == [norm(name)]
            assert await _scalar(conn, "SELECT count(*) FROM meal_macros WHERE name = %s", (f"Nothing {tag}",)) == 0
            # the words never became rows on the log
            assert await _scalar(conn, "SELECT count(*) FROM item_resolutions WHERE log_id = %s", (body.id,)) == 0

            # extend: the same food and serving adds up; a new food joins the list
            more = [
                {"phrase": f"banana {tag}", "qty": 1, "unit": None, "food_id": None, "serving_id": None},
                {"phrase": f"eggs {tag}", "qty": 3, "unit": None, "food_id": None, "serving_id": None},
            ]
            runner = _fake_runner([("save_meal", {"name": name.lower(), "items": more})], "Added.")
            monkeypatch.setattr(agent_svc, "Runner", runner)
            resp2 = await logs_routes.create_log(_log(f"add a banana and 3 eggs to {name}", agent=True))
            assert resp2.saved_meal is not None and resp2.saved_meal.item_count == 3
            row = await _macro(conn, name)
            assert row["items"] == [
                {"food_id": fx.whey, "serving_id": "srv-scoop", "qty": 1.0},
                {"food_id": banana, "serving_id": "srv-medium", "qty": 3.0},
                {"food_id": fx.eggs, "serving_id": "srv-large", "qty": 3.0},
            ]

    _run(scenario())


def test_update_meal_renames_retriggers_sets_qty_removes_and_toggles_active(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            fx = await breakfast_fixture(conn)
            old, new = f"Usual breakfast {fx.tag}", f"Weekday breakfast {fx.tag}"

            async def edits(agent):
                # rename, new triggers, a quantity and a removal in one call, by name
                out = await _call(
                    agent, "update_meal", name=old.upper(), new_name=new,
                    trigger_phrases=[f"Weekday {fx.tag}"],
                    set_qty=[{"food": "egg", "qty": 3}], remove=["oats"],
                )
                assert "error" not in out, out
                assert out["meal"] == new and out["is_active"] is True and out["item_count"] == 2
                assert out["trigger_phrases"] == [norm(f"Weekday {fx.tag}")]
                assert out["items"][0].startswith("egg ") and out["items"][0].endswith(" 3 × 1 large")
                assert out["items"][1].startswith("whey ") and out["items"][1].endswith(" 1 × 1 scoop")
                # an item that is not there, or that two items match: nothing written
                out = await _call(agent, "update_meal", name=new, remove=["toast"])
                assert out["error"] == "unknown_item" and len(out["items"]) == 2
                out = await _call(agent, "update_meal", name=new, set_qty=[{"food": "e", "qty": 1}])
                assert out["error"] == "ambiguous_item" and len(out["items"]) == 2
                # the last item cannot go (routes/meals: a saved meal needs one)
                out = await _call(agent, "update_meal", name=new, remove=["egg", "whey"])
                assert out["error"] == "invalid" and "at least one item" in out["message"]
                # a meal that does not exist: the names that do
                out = await _call(agent, "update_meal", name=f"nothing {fx.tag}", is_active=False)
                assert out["error"] == "not_found" and new in out["meals"]
                # deactivate ("delete"), then reactivate, by name
                out = await _call(agent, "update_meal", name=new, is_active=False)
                assert out["is_active"] is False
                out = await _call(agent, "update_meal", name=new.lower(), is_active=True)
                assert out["is_active"] is True

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(edits, "Updated."))
            resp = await logs_routes.create_log(_log("rename my usual breakfast", agent=True))
            # the items changed: the code-side note rides the response
            assert resp.saved_meal is not None and resp.saved_meal.name == new
            assert resp.saved_meal.item_count == 2 and resp.items == []
            row = await (await conn.execute(
                "SELECT * FROM meal_macros WHERE id = %s", (fx.macro_id,)
            )).fetchone()
            assert row["name"] == new and row["is_active"]
            assert row["trigger_phrases"] == [norm(f"Weekday {fx.tag}")]
            assert row["items"] == [
                {"food_id": fx.eggs, "serving_id": "srv-large", "qty": 3.0},
                {"food_id": fx.whey, "serving_id": "srv-scoop", "qty": 1.0},
            ]

    _run(scenario())


def test_agent_meal_tools_are_the_tenants_alone(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            fx = await breakfast_fixture(conn)
            egg = await insert_food(conn)
            committed = await insert_log(conn, status="committed")
            await insert_entry(conn, committed, egg)
            mine = f"Usual breakfast {fx.tag}"

            await _become(conn, "other")
            history = [_turn("staged 1 item", committed, "committed", ["egg — 2 × 1 egg"])]

            async def probe(agent):
                out = await _call(agent, "update_meal", name=mine, is_active=False)
                assert out["error"] == "not_found" and out["meals"] == []
                # the conversation's log is another tenant's: the turn stops naming it
                assert (await _call(agent, "save_meal", name="theirs", log_id=str(committed)))["error"] == "unknown_log"
                assert (await _call(agent, "save_meal", name="theirs"))["error"] == "nothing_to_save"

            runner = _probe_runner(probe)
            monkeypatch.setattr(agent_svc, "Runner", runner)
            resp = await logs_routes.create_log(LogCreate(
                id=uuid4(), text="deactivate the usual breakfast", captured_at=NOW, tz=TZ,
                agent=True, history=history,
            ))
            assert resp.saved_meal is None and resp.items == []
            assert mine not in runner.input and "(none yet)" in runner.input

            await _become(conn, "test-user", is_owner=True)
            assert await _scalar(conn, "SELECT is_active FROM meal_macros WHERE id = %s", (fx.macro_id,)) is True

    _run(scenario())


# ---------------------------------------------------------------------------
# 3. routes/agent — the chat's stream
# ---------------------------------------------------------------------------


def test_agent_stream_shows_the_save_meal_call_result_reply_and_done(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            egg = await insert_food(conn)
            committed = await insert_log(conn, status="committed")
            await insert_entry(conn, committed, egg)
            name = f"Streamed {uuid4().hex[:6]}"
            monkeypatch.setattr(agent_svc, "Runner", _fake_runner([("save_meal", {"name": name})], "Saved it."))

            body = LogCreate(id=uuid4(), text=f"save that as {name}", captured_at=NOW, tz=TZ)
            events = await _drain(await agent_routes.agent_text(body))
            assert [e["type"] for e in events] == ["tool_call", "tool_result", "reply", "done"]
            assert events[0]["name"] == "save_meal" and events[0]["args"]["name"] == name
            out = events[1]["output"]
            assert out["meal"] == name and out["item_count"] == 1 and out["items"] == ["egg 2 × 1 egg"]
            assert events[2]["text"] == "Saved it."
            done = events[3]["log"]
            assert done["items"] == [] and done["agent"]["reply"] == "Saved it."
            assert done["saved_meal"]["name"] == name and done["saved_meal"]["item_count"] == 1
            assert await _scalar(conn, "SELECT count(*) FROM meal_macros WHERE name = %s", (name,)) == 1

    _run(scenario())


# ---------------------------------------------------------------------------
# 4. past_foods — the agent's one read (§6.4, 2026-09-08)
# ---------------------------------------------------------------------------

MILK_CUP = {
    "serving_id": "srv-cup",
    "description": "1 cup (240 ml)",
    "grams": 240,
    "macros": {"kcal": 122, "protein_g": 8, "carb_g": 12, "fat_g": 4.5},
}
SCOOP = {
    "serving_id": "srv-scoop",
    "description": "1 scoop (32 g)",
    "grams": 32,
    "macros": {"kcal": 120, "protein_g": 24, "carb_g": 3, "fat_g": 1},
}


async def _eat(conn, food_id, *, at, serving_id, qty, grams=None, meal="lunch"):
    """One committed diary row at a pinned instant — past_foods reads
    `entries` directly, and `at` is UTC so the local-day conversion is under
    test. tenancy's insert_entry pins the serving and the day; these turn on
    both."""
    await conn.execute(
        "INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, grams, macros) "
        "VALUES (%s::timestamptz, %s, %s, %s, %s, %s, '{}'::jsonb)",
        (at, meal, food_id, serving_id, qty, grams),
    )


def test_past_foods_reports_the_amount_each_food_was_last_logged_at():
    async def scenario():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            milk = await insert_named_food(conn, f"milk {tag}", [MILK_CUP], brand="fairlife")
            whey = await insert_named_food(conn, f"whey {tag}", [SCOOP])
            # the older amount must lose to the newer one, per food
            await _eat(conn, milk, at="2026-09-04T12:00:00+00", serving_id="srv-cup",
                       qty=3, grams=720)
            # 05:00 UTC is the previous evening in LA — the day must be theirs
            await _eat(conn, milk, at="2026-09-07T05:00:00+00", serving_id="srv-cup",
                       qty=1.5, grams=360, meal="breakfast")
            await _eat(conn, whey, at="2026-09-06T12:00:00+00", serving_id="srv-scoop", qty=2)

            rows = await foods_repo.past_foods(tz=TZ)
            assert len([r for r in rows if r["food_id"] == milk]) == 1  # one row per food
            by_id = {r["food_id"]: r for r in rows}
            assert by_id[milk]["qty"] == 1.5 and by_id[milk]["grams"] == 360
            assert by_id[milk]["meal"] == "breakfast"  # the newest row's meal, not the older
            assert by_id[milk]["serving"] == "1 cup (240 ml)"  # resolved out of foods.servings
            assert by_id[milk]["brand"] == "fairlife"
            assert by_id[whey]["qty"] == 2 and by_id[whey]["grams"] is None
            assert str(by_id[milk]["day"]) == "2026-09-06"
            assert str(by_id[whey]["day"]) == "2026-09-06"
            # §6.4: the agent may read amounts, never numbers
            assert "kcal" not in json.dumps(rows, default=str)

    _run(scenario())


def test_past_foods_searches_the_whole_history_by_name():
    async def scenario():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            milk = await insert_named_food(conn, f"milk {tag}", [MILK_CUP], brand="fairlife")
            whey = await insert_named_food(conn, f"whey {tag}", [SCOOP])
            await _eat(conn, milk, at="2026-08-01T12:00:00+00", serving_id="srv-cup", qty=1.5)
            await _eat(conn, whey, at="2026-09-06T12:00:00+00", serving_id="srv-scoop", qty=2)

            hits = [r["food_id"] for r in await foods_repo.past_foods(query="milk", tz=TZ)]
            assert milk in hits and whey not in hits
            # the brand is searchable too — it rides the same concatenation
            assert milk in [r["food_id"] for r in await foods_repo.past_foods(query="fairlife", tz=TZ)]
            # a fragment the trigram floor would miss still lands, via ILIKE
            assert milk in [r["food_id"] for r in await foods_repo.past_foods(query="ilk", tz=TZ)]
            # no query = the recent list, newest first: whey (1 day) before milk (40)
            recent = [r["food_id"] for r in await foods_repo.past_foods(tz=TZ)]
            assert recent.index(whey) < recent.index(milk)

    _run(scenario())


def test_save_meal_takes_the_ids_and_amounts_past_foods_reported(monkeypatch):
    """The traced failure, offline: two foods eaten yesterday become a saved
    meal with the diary's own amounts — no lookup, no question."""

    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            tag = uuid4().hex[:6]
            milk = await insert_named_food(conn, f"milk {tag}", [MILK_CUP], brand="fairlife")
            whey = await insert_named_food(conn, f"whey {tag}", [SCOOP])
            await _eat(conn, milk, at="2026-09-06T12:00:00+00", serving_id="srv-cup",
                       qty=1.5, grams=360)
            await _eat(conn, whey, at="2026-09-06T13:00:00+00", serving_id="srv-scoop", qty=2)
            name = f"Shake {tag}"

            async def build(agent):
                raw = await _raw(agent, "past_foods", query=tag)
                got = json.loads(raw)
                assert got["count"] == 2 and "error" not in got
                # §6.4 where it counts: the JSON the MODEL reads, not the repo
                # rows one layer below it (these two foods carry no such words)
                assert not any(
                    w in raw.lower() for w in ("kcal", "protein", "carb", "fat", "macros")
                ), raw
                out = await _call(agent, "save_meal", name=name, items=[
                    {"phrase": f["food"], "qty": f["qty"], "unit": None,
                     "food_id": f["food_id"], "serving_id": f["serving_id"]}
                    for f in got["foods"]
                ])
                assert "error" not in out and "unresolved" not in out, out

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(build, "Saved it."))
            body = _log(f"make a meal called {name} from what I had yesterday", agent=True)
            resp = await logs_routes.create_log(body)
            assert resp.saved_meal is not None and resp.saved_meal.item_count == 2
            row = await _macro(conn, name)
            assert sorted(row["items"], key=lambda i: i["food_id"]) == sorted(
                [
                    {"food_id": milk, "serving_id": "srv-cup", "qty": 1.5},
                    {"food_id": whey, "serving_id": "srv-scoop", "qty": 2.0},
                ],
                key=lambda i: i["food_id"],
            )
            # the read staged nothing and logged nothing
            assert resp.items == []
            assert await _scalar(
                conn, "SELECT count(*) FROM item_resolutions WHERE log_id = %s", (body.id,)
            ) == 0
            assert await _scalar(conn, "SELECT count(*) FROM entries WHERE log_id = %s", (body.id,)) == 0

    _run(scenario())


def test_a_food_this_account_never_logged_cannot_join_a_meal_by_id(monkeypatch):
    """`foods` is global, so a food_id proves nothing — only what THIS tenant
    has eaten may be named by id (the food analogue of known_logs)."""

    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            tag = uuid4().hex[:6]
            theirs = await insert_named_food(conn, f"milk {tag}", [MILK_CUP])
            mine = await insert_named_food(conn, f"whey {tag}", [SCOOP])

            await _become(conn, "other")
            await _eat(conn, theirs, at="2026-09-06T12:00:00+00", serving_id="srv-cup", qty=1.5)
            await _become(conn, "test-user", is_owner=True)
            await _eat(conn, mine, at="2026-09-06T12:00:00+00", serving_id="srv-scoop", qty=2)

            # their food is in the shared cache but not in this account's diary
            assert [r["food_id"] for r in await foods_repo.past_foods(query=tag, tz=TZ)] == [mine]
            name = f"Borrowed {tag}"

            async def probe(agent):
                for bad, why in (
                    ({"food_id": theirs, "serving_id": "srv-cup"}, "another tenant's food"),
                    ({"food_id": "not-a-uuid", "serving_id": "srv-cup"}, "a malformed id"),
                    ({"food_id": mine, "serving_id": "srv-nope"}, "a serving not on the food"),
                ):
                    out = await _call(agent, "save_meal", name=name, items=[
                        {"phrase": "x", "qty": 1, "unit": None, **bad}
                    ])
                    assert out["error"] == "unresolved", (why, out)
                    assert out["unresolved"][0]["status"] == "unknown_food", why
                    # the headline must not send them off to log a food they
                    # already have — the id was rejected, not the food missing
                    assert "past_foods" in out["message"], (why, out)
                    assert "log them once first" not in out["message"], (why, out)

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(probe, "Couldn't do that."))
            resp = await logs_routes.create_log(_log(f"save {name}", agent=True))
            assert resp.saved_meal is None
            assert await _scalar(
                conn, "SELECT count(*) FROM meal_macros WHERE name = %s", (name,)
            ) == 0

    _run(scenario())


def test_an_id_without_a_serving_takes_the_default_and_duplicates_merge(monkeypatch):
    """A food_id is what they pointed at: a missing serving_id falls back to
    that food's own default, never to a name search that could land somewhere
    else. And the same row named twice is one item, not two."""

    async def scenario():
        async with isolated_db() as conn:
            _stub_pipeline(monkeypatch)
            tag = uuid4().hex[:6]
            milk = await insert_named_food(conn, f"milk {tag}", [MILK_CUP], brand="fairlife")
            # what the cascade would find by name if the id were dropped
            decoy = await insert_named_food(conn, f"milk {tag} lite", [HUNDRED_G])
            await insert_named_alias(conn, norm(f"milk {tag}"), decoy)
            await _eat(conn, milk, at="2026-09-06T12:00:00+00", serving_id="srv-cup", qty=1.5)
            name = f"Half shake {tag}"

            async def build(agent):
                out = await _call(agent, "save_meal", name=name, items=[
                    {"phrase": f"milk {tag}", "qty": 2, "unit": None,
                     "food_id": milk, "serving_id": None},
                    {"phrase": f"milk {tag}", "qty": 1, "unit": None,
                     "food_id": milk, "serving_id": "srv-cup"},
                ])
                assert "error" not in out, out

            monkeypatch.setattr(agent_svc, "Runner", _probe_runner(build, "Saved."))
            resp = await logs_routes.create_log(_log(f"save {name}", agent=True))
            assert resp.saved_meal is not None
            row = await _macro(conn, name)
            # the id beat the alias, the default serving filled in, and the two
            # rows collapsed into one
            assert row["items"] == [{"food_id": milk, "serving_id": "srv-cup", "qty": 3.0}]
            assert decoy not in [i["food_id"] for i in row["items"]]

    _run(scenario())


def test_past_foods_is_unavailable_when_the_route_injects_nothing():
    async def scenario():
        deps = _deps(past_foods=None)
        out = await _call(agent_svc.build_agent(deps, {}), "past_foods")
        assert out["error"] == "unavailable"

    _run(scenario())
