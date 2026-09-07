"""X1 tests — saved meals (meal macros) + tier 0 (DESIGN.md §3.0, §3.1, §3.5
step 4, §5 /api/meals, §8.4).

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one force-rollback connection) — the live Neon DB is never
dirtied. Cross-workstream modules (tracing, services.*) are stubbed per-module
at run time (same pattern as test_logs_lifecycle.py); the decompose stub
RAISES if called, which is how "zero decompose" is asserted.

Run:  uv run --project backend --with pytest pytest backend/tests/test_meals.py -q
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from tracing_stub import make_tracing_stub
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

# ---------------------------------------------------------------------------
# stubs for parallel workstreams
# ---------------------------------------------------------------------------


def _build_stubs() -> dict:
    tracing_mod = make_tracing_stub("trace-test")

    services_mod = types.ModuleType("services")
    services_mod.__path__ = []

    ctx_mod = types.ModuleType("services.context")

    async def build_agent_context():
        return SimpleNamespace(profile_notes="", targets={}, food_index=[], meal_names=[], recent_foods=[])

    ctx_mod.build_agent_context = build_agent_context

    dec_mod = types.ModuleType("services.decompose")

    async def decompose(text, ctx):
        raise AssertionError("decompose must not be called (tier-0 short-circuit)")

    dec_mod.decompose = decompose

    t3_mod = types.ModuleType("services.tier3")

    async def run_tier3(log_id, item_index, refinement=None):
        raise AssertionError("tier3 is not exercised by these tests")

    t3_mod.run_tier3 = run_tier3

    services_mod.context = ctx_mod
    services_mod.decompose = dec_mod
    services_mod.tier3 = t3_mod
    return {
        "tracing": tracing_mod,
        "services": services_mod,
        "services.context": ctx_mod,
        "services.decompose": dec_mod,
        "services.tier3": t3_mod,
    }


@pytest.fixture(scope="module", autouse=True)
def _workstream_stubs():
    stubs = _build_stubs()
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


import db  # noqa: E402


from tenancy_harness import bind_test_tenant  # noqa: E402
from models import (  # noqa: E402
    CommitRequest,
    LogCreate,
    Meal,
    ParsedItem,
    SaveIntent,
)
from resolver import cascade  # noqa: E402
from resolver import commit as commit_tx  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from routes.meals import (  # noqa: E402
    MealCreate,
    MealFromLog,
    MealItemIn,
    MealPatch,
    create_meal,
    delete_meal,
    list_meals,
    meal_from_log,
    patch_meal,
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

EGG = {
    "serving_id": "srv-large",
    "description": "1 large",
    "grams": 50,
    "macros": {"kcal": 78, "protein_g": 6, "carb_g": 0.6, "fat_g": 5},
}
OATS_40G = {
    "serving_id": "srv-40g",
    "description": "40 g",
    "grams": 40,
    "macros": {"kcal": 150, "protein_g": 5, "carb_g": 27, "fat_g": 3},
}
SCOOP = {
    "serving_id": "srv-scoop",
    "description": "1 scoop",
    "grams": 31,
    "macros": {"kcal": 120, "protein_g": 25, "carb_g": 3, "fat_g": 1.5},
}
CUP = {
    "serving_id": "srv-cup",
    "description": "1 cup",
    "grams": 240,
    "macros": {"kcal": 2, "protein_g": 0.3, "carb_g": 0, "fat_g": 0},
}


# ---------------------------------------------------------------------------
# rollback harness — patches backend/db.py onto one force-rollback connection
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
    await bind_test_tenant(conn)  # 002: app role + the fixed test tenant, inside the rollback
    orig = (db.fetchall, db.fetchone, db.execute, db.tx)

    async def fetchall(sql, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchall()

    async def fetchone(sql, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchone()

    async def execute(sql, params=()):
        cur = await conn.execute(sql, params)
        return cur.rowcount

    @asynccontextmanager
    async def tx():
        async with conn.transaction():  # SAVEPOINT under the outer transaction
            yield conn

    db.fetchall, db.fetchone, db.execute, db.tx = fetchall, fetchone, execute, tx
    try:
        yield conn
    finally:
        db.fetchall, db.fetchone, db.execute, db.tx = orig
        await outer.__aexit__(None, None, None)  # force rollback
        await conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def insert_food(conn, name, servings, *, brand=None):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, brand, food_type,
                           servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, %s, 'generic', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, brand, Json(servings), servings[0]["serving_id"]),
    )
    return str((await cur.fetchone())["id"])


async def insert_alias(conn, alias_norm, food_id):
    await conn.execute(
        "INSERT INTO aliases (alias_norm, food_id, origin) VALUES (%s, %s, 'manual')",
        (alias_norm, food_id),
    )


async def insert_macro(conn, name, triggers, items):
    """Direct insert (already-norm()'d triggers) for cascade-level tests."""
    cur = await conn.execute(
        "INSERT INTO meal_macros (name, trigger_phrases, items) VALUES (%s, %s, %s) "
        "RETURNING id",
        (name, triggers, Json(items)),
    )
    return (await cur.fetchone())["id"]


async def insert_log(conn, raw="x"):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at) "
        "VALUES (%s, 'text', %s, now())",
        (log_id, raw),
    )
    return log_id


async def breakfast_fixture(conn):
    """eggs ×2, oats ×1, whey ×1 as the 'usual breakfast' macro (+ a decoy
    alias on the same phrase — §3.0 orders macro before alias)."""
    eggs = await insert_food(conn, f"egg {uuid4().hex[:6]}", [EGG])
    oats = await insert_food(conn, f"oats {uuid4().hex[:6]}", [OATS_40G])
    whey = await insert_food(conn, f"whey {uuid4().hex[:6]}", [SCOOP], brand="Optimum")
    decoy = await insert_food(conn, f"decoy {uuid4().hex[:6]}", [SCOOP])
    tag = uuid4().hex[:6]
    trigger = f"usual breakfast {tag}"
    await insert_alias(conn, trigger, decoy)
    macro_id = await insert_macro(
        conn,
        f"Usual breakfast {tag}",
        [trigger, f"usual {tag}"],
        [
            {"food_id": eggs, "serving_id": "srv-large", "qty": 2},
            {"food_id": oats, "serving_id": "srv-40g", "qty": 1},
            {"food_id": whey, "serving_id": "srv-scoop", "qty": 1},
        ],
    )
    return SimpleNamespace(
        eggs=eggs, oats=oats, whey=whey, decoy=decoy, trigger=trigger, tag=tag,
        macro_id=macro_id,
    )


def parsed(phrase, kind="food", meal_ref=None, qty=None):
    return ParsedItem(
        phrase=phrase, phrase_kind=kind, qty=qty, unit=None, state=None,
        brand=None, library_match=None, meal_ref=meal_ref,
    )


def log_body(text, log_id=None):
    return LogCreate(
        id=log_id or uuid4(), text=text, captured_at=NOW(), tz="America/Los_Angeles"
    )


def assert_breakfast_rows(rows, fx, start=0):
    assert [r.staged.item_index for r in rows] == [start, start + 1, start + 2]
    assert all(r.staged.tier == 0 and r.staged.status == "resolved" for r in rows)
    assert all(r.alias_id is None for r in rows)
    eggs, oats, whey = (r.staged for r in rows)
    # stored portions, verbatim — no portion inference
    assert (eggs.food.id, eggs.serving.id, eggs.qty) == (fx.eggs, "srv-large", 2.0)
    assert (oats.food.id, oats.serving.id, oats.qty) == (fx.oats, "srv-40g", 1.0)
    assert (whey.food.id, whey.serving.id, whey.qty) == (fx.whey, "srv-scoop", 1.0)
    assert eggs.macros.kcal == 156 and eggs.macros.protein_g == 12  # 2 × 1 large
    assert whey.macros.protein_g == 25 and whey.food.brand == "Optimum"
    # §5: for tier-0 expansions the phrase is the food display name
    assert eggs.phrase.startswith("egg ") and whey.phrase.startswith("whey ")
    assert all(r.staged.confidence == "high" for r in rows)
    assert all(r.staged.servings for r in rows)  # portion sheet options ride along


# ---------------------------------------------------------------------------
# 1. §3.1 / §5 — trigger phrases are norm()'d on write; items validated
# ---------------------------------------------------------------------------


def test_triggers_normalized_on_write():
    async def run():
        async with isolated_db():
            async with db.tx() as conn:
                whey = await insert_food(conn, f"whey {uuid4().hex[:6]}", [SCOOP])
            tag = uuid4().hex[:6]

            meal = await create_meal(
                MealCreate(
                    name=f"Usual Breakfast {tag}",
                    trigger_phrases=[
                        f"My Usual Breakfast {tag}!", f"The usual {tag}", "  ", f"the USUAL {tag}",
                    ],
                    items=[MealItemIn(food_id=whey, serving_id="srv-scoop", qty=2)],
                )
            )
            assert meal.trigger_phrases == [f"usual breakfast {tag}", f"usual {tag}"]
            assert meal.is_active is True
            assert [(i.food_id, i.serving_id, i.qty) for i in meal.items] == [
                (whey, "srv-scoop", 2.0)
            ]
            assert meal.items[0].serving_description == "1 scoop"
            row = await db.fetchone("SELECT * FROM meal_macros WHERE id = %s", (meal.id,))
            assert row["trigger_phrases"] == [f"usual breakfast {tag}", f"usual {tag}"]
            assert row["items"] == [{"food_id": whey, "serving_id": "srv-scoop", "qty": 2.0}]

            # no triggers given → the meal's own name, post-norm
            meal2 = await create_meal(
                MealCreate(
                    name=f"The Post Workout {tag}",
                    items=[MealItemIn(food_id=whey, serving_id="srv-scoop")],
                )
            )
            assert meal2.trigger_phrases == [f"post workout {tag}"]

            # PATCH replaces triggers, still through norm()
            patched = await patch_meal(
                meal.id, MealPatch(trigger_phrases=[f"A Brand New One {tag}"])
            )
            assert patched.trigger_phrases == [f"brand new one {tag}"]
            assert patched.updated_at >= meal.updated_at

            # validation: unknown serving / duplicate active name
            with pytest.raises(HTTPException) as exc:
                await create_meal(
                    MealCreate(
                        name=f"Bad {tag}",
                        items=[MealItemIn(food_id=whey, serving_id="srv-nope")],
                    )
                )
            assert exc.value.status_code == 422
            with pytest.raises(HTTPException) as exc:
                await create_meal(
                    MealCreate(
                        name=f"usual breakfast {tag}",  # case-insensitive dup
                        items=[MealItemIn(food_id=whey, serving_id="srv-scoop")],
                    )
                )
            assert exc.value.status_code == 409

            listed = await list_meals()
            assert {m.id for m in listed} >= {meal.id, meal2.id}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. §3.0 — whole-utterance short-circuit: macro before alias, zero decompose
# ---------------------------------------------------------------------------


def test_short_circuit_expands_macro_before_alias_without_decompose():
    async def run():
        async with isolated_db() as conn:
            fx = await breakfast_fixture(conn)

            # decompose stub raises if touched; the decoy alias on the same
            # phrase must lose to the macro (§3.0 ordering)
            rows = await cascade.short_circuit_all(f"My usual breakfast {fx.tag}!", Meal.breakfast)
            assert rows is not None and len(rows) == 3
            assert_breakfast_rows(rows, fx)
            assert fx.decoy not in {r.staged.food.id for r in rows}

            # the second trigger works too
            rows2 = await cascade.short_circuit_all(f"the usual {fx.tag}", Meal.breakfast)
            assert rows2 is not None and len(rows2) == 3

            # persisted like any staged state: one item_resolutions row PER
            # expanded item, tier 0, its own item_index, no alias
            log_id = await insert_log(conn)
            await logs_routes._persist(log_id, rows, Meal.breakfast, short_circuit=True)
            db_rows = await db.fetchall(
                "SELECT * FROM item_resolutions WHERE log_id = %s ORDER BY item_index", (log_id,)
            )
            assert [r["item_index"] for r in db_rows] == [0, 1, 2]
            assert all(r["tier"] == 0 and r["status"] == "resolved" for r in db_rows)
            assert all(r["alias_id"] is None for r in db_rows)
            assert str(db_rows[0]["proposed_food_id"]) == fx.eggs
            assert db_rows[0]["proposed_serving_id"] == "srv-large"
            assert float(db_rows[0]["proposed_qty"]) == 2.0
            log = await db.fetchone("SELECT short_circuit FROM logs WHERE id = %s", (log_id,))
            assert log["short_circuit"] is True

            # no macro, no alias → None (decompose's turn); alias-only → tier 1
            assert await cascade.short_circuit_all(f"nothing here {fx.tag}", Meal.snack) is None
            await insert_alias(conn, f"decoy phrase {fx.tag}", fx.decoy)
            only = await cascade.short_circuit_all(f"decoy phrase {fx.tag}", Meal.snack)
            assert only is not None and len(only) == 1 and only[0].staged.tier == 1

    asyncio.run(run())


@pytest.mark.skipif(
    "short_circuit_all" not in inspect.getsource(logs_routes),
    reason="routes/logs.py not yet wired to cascade.short_circuit_all (integrator)",
)
def test_create_log_short_circuits_macro():
    """End-to-end once logs.py is wired: POST /api/logs on a trigger phrase
    stages N tier-0 rows and never calls decompose (the stub raises)."""

    async def run():
        async with isolated_db() as conn:
            fx = await breakfast_fixture(conn)
            resp = await logs_routes.create_log(log_body(f"my usual breakfast {fx.tag}"))
            assert len(resp.items) == 3
            assert all(i.tier == 0 for i in resp.items)
            assert [i.item_index for i in resp.items] == [0, 1, 2]
            log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (resp.log_id,))
            assert log["short_circuit"] is True and log["decompose_ms"] is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. §3.1 — per-item expansion after decompose; indexes shift past the macro
# ---------------------------------------------------------------------------


def test_resolve_items_expands_meal_ref_and_shifts_indexes():
    async def run():
        async with isolated_db() as conn:
            fx = await breakfast_fixture(conn)
            coffee = await insert_food(conn, f"coffee {uuid4().hex[:6]}", [CUP])
            await insert_alias(conn, f"coffee {fx.tag}", coffee)

            # "usual breakfast and a coffee": meal_ref expands to 3 rows (0–2),
            # coffee lands at index 3 via tier 1
            rows = await cascade.resolve_items(
                [
                    parsed(f"usual breakfast {fx.tag}", "meal_ref", meal_ref=f"Usual breakfast {fx.tag}"),
                    parsed(f"coffee {fx.tag}"),
                ],
                Meal.breakfast,
            )
            assert len(rows) == 4
            assert_breakfast_rows(rows[:3], fx)
            last = rows[3].staged
            assert (last.item_index, last.tier, last.food.id) == (3, 1, coffee)

            # a phrase that norm-matches a trigger expands even when decompose
            # tagged it "food" (the match is on the phrase, not the tag)
            rows = await cascade.resolve_items([parsed(f"The Usual {fx.tag}")], Meal.lunch)
            assert len(rows) == 3 and all(r.staged.tier == 0 for r in rows)
            assert all(r.staged.meal == Meal.lunch for r in rows)

            # decompose's meal_ref (the saved meal's name) is a key too
            rows = await cascade.resolve_items(
                [parsed("the usual thing", "meal_ref", meal_ref=f"usual {fx.tag}")], Meal.snack
            )
            assert len(rows) == 3 and all(r.staged.tier == 0 for r in rows)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. §3.5 — commit of an expansion writes entries, never aliases/portions
# ---------------------------------------------------------------------------


def test_commit_expansion_writes_entries_but_no_aliases():
    async def run():
        async with isolated_db() as conn:
            fx = await breakfast_fixture(conn)
            rows = await cascade.short_circuit_all(f"usual breakfast {fx.tag}", Meal.breakfast)
            log_id = await insert_log(conn, raw=f"usual breakfast {fx.tag}")
            await logs_routes._persist(log_id, rows, Meal.breakfast, short_circuit=True)
            norms = [norm(r.staged.phrase) for r in rows] + [fx.trigger]
            before = await db.fetchone(
                "SELECT count(*) AS n FROM aliases WHERE alias_norm = ANY(%s)", (norms,)
            )
            assert before["n"] == 1  # only the decoy alias on the trigger phrase

            totals = await commit_route(log_id, CommitRequest(items=[r.staged for r in rows]))
            assert totals.errors == []
            assert totals.kcal >= 156 + 150 + 120

            entries = await db.fetchall(
                "SELECT * FROM entries WHERE log_id = %s ORDER BY item_index", (log_id,)
            )
            assert [(str(e["food_id"]), e["serving_id"], float(e["qty"])) for e in entries] == [
                (fx.eggs, "srv-large", 2.0),
                (fx.oats, "srv-40g", 1.0),
                (fx.whey, "srv-scoop", 1.0),
            ]
            assert entries[0]["macros"]["kcal"] == 156  # snapshot = per-serving × qty
            assert entries[0]["meal"] == "breakfast"
            assert all(e["alias_id"] is None for e in entries)

            # NO alias learning, NO portion defaults: the macro is the memory
            after = await db.fetchone(
                "SELECT count(*) AS n FROM aliases WHERE alias_norm = ANY(%s)", (norms,)
            )
            assert after["n"] == before["n"]
            decoy_alias = await db.fetchone(
                "SELECT use_count FROM aliases WHERE alias_norm = %s", (fx.trigger,)
            )
            assert decoy_alias["use_count"] == 1  # untouched

            res = await db.fetchall(
                "SELECT * FROM item_resolutions WHERE log_id = %s ORDER BY item_index", (log_id,)
            )
            assert all(r["committed_at"] is not None and r["tier"] == 0 for r in res)
            assert all(r["alias_id"] is None and r["was_edited"] is False for r in res)
            assert [str(r["final_food_id"]) for r in res] == [fx.eggs, fx.oats, fx.whey]

            # no save_intent → the macro itself is untouched by the commit
            macro = await db.fetchone("SELECT * FROM meal_macros WHERE id = %s", (fx.macro_id,))
            assert len(macro["items"]) == 3 and macro["trigger_phrases"][0] == fx.trigger

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. §3.1 guard — a meal-shaped phrase with no macro never enters food search
# ---------------------------------------------------------------------------


def test_meal_ref_without_macro_is_needs_input():
    async def run():
        async with isolated_db() as conn:
            phrase = f"usual lunch {uuid4().hex[:6]}"
            expected = f"No saved meal called '{phrase}' — log the items, then tap Save as meal."

            res = await cascade.resolve_item(parsed(phrase, "meal_ref"), 0, Meal.lunch)
            assert res.staged.status == "needs_input"
            assert res.staged.tier is None and res.staged.food is None
            assert res.staged.question.text == expected
            assert res.staged.question.options == []

            rows = await cascade.resolve_items([parsed(phrase, "meal_ref")], Meal.lunch)
            assert len(rows) == 1 and rows[0].staged.question.text == expected

            # a multi-item macro through the single-row path (/api/resolve)
            # is named honestly, never reported as "no saved meal"
            fx = await breakfast_fixture(conn)
            res = await cascade.resolve_item(parsed(fx.trigger, "meal_ref"), 0, Meal.lunch)
            assert res.staged.status == "needs_input"
            assert "No saved meal" not in res.staged.question.text
            assert "3 items" in res.staged.question.text

            # a one-item macro fits a single row
            one = await insert_food(conn, f"bar {uuid4().hex[:6]}", [SCOOP])
            await insert_macro(conn, "bar meal", [f"bar meal {fx.tag}"],
                               [{"food_id": one, "serving_id": "srv-scoop", "qty": 3}])
            res = await cascade.resolve_item(parsed(f"bar meal {fx.tag}"), 5, Meal.snack)
            assert res.staged.tier == 0 and res.staged.item_index == 5
            assert (res.staged.food.id, res.staged.qty) == (one, 3.0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. §5 POST /api/meals/from-log — "Save as meal" from committed entries
# ---------------------------------------------------------------------------


async def committed_lunch(conn, tag):
    """A committed two-item log (tier 1 via aliases) → (log_id, eggs, coffee)."""
    eggs = await insert_food(conn, f"egg {tag}", [EGG])
    coffee = await insert_food(conn, f"coffee {tag}", [CUP])
    await insert_alias(conn, f"eggs {tag}", eggs)
    await insert_alias(conn, f"coffee {tag}", coffee)
    rows = await cascade.resolve_items(
        [parsed(f"eggs {tag}"), parsed(f"coffee {tag}")], Meal.lunch
    )
    assert [r.staged.tier for r in rows] == [1, 1]
    log_id = await insert_log(conn, raw=f"eggs {tag} and coffee {tag}")
    await logs_routes._persist(log_id, rows, Meal.lunch, short_circuit=False)
    return log_id, eggs, coffee, rows


def test_from_log_creates_macro_with_post_norm_triggers():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            log_id, eggs, coffee, rows = await committed_lunch(conn, tag)

            # not committed yet → nothing to save
            with pytest.raises(HTTPException) as exc:
                await meal_from_log(MealFromLog(log_id=log_id, name=f"Usual Lunch {tag}"))
            assert exc.value.status_code == 409
            with pytest.raises(HTTPException) as exc:
                await meal_from_log(MealFromLog(log_id=uuid4(), name="x"))
            assert exc.value.status_code == 404

            items = [r.staged.model_copy(update={"qty": 2.0}) for r in rows[:1]] + [rows[1].staged]
            await commit_route(log_id, CommitRequest(items=items))

            meal = await meal_from_log(
                MealFromLog(
                    log_id=log_id,
                    name=f"My Usual Lunch {tag}",
                    trigger_phrases=[f"The Usual Lunch {tag}", f"usual lunch {tag}"],
                )
            )
            assert meal.trigger_phrases == [f"usual lunch {tag}"]  # post-norm, deduped
            assert meal.name == f"My Usual Lunch {tag}"
            assert [(i.food_id, i.serving_id, i.qty) for i in meal.items] == [
                (eggs, "srv-large", 2.0),  # the committed qty, not the staged one
                (coffee, "srv-cup", 1.0),
            ]

            # …and it expands at tier 0 from now on
            exp = await cascade.short_circuit_all(f"the usual lunch {tag}", Meal.lunch)
            assert exp is not None and [r.staged.tier for r in exp] == [0, 0]
            assert [(r.staged.food.id, r.staged.qty) for r in exp] == [(eggs, 2.0), (coffee, 1.0)]

            # saving again under the same name updates the one macro
            again = await meal_from_log(
                MealFromLog(log_id=log_id, name=f"my usual lunch {tag}")
            )
            assert again.id == meal.id
            assert again.trigger_phrases == [f"usual lunch {tag}"]  # kept when none given
            n = await db.fetchone(
                "SELECT count(*) AS n FROM meal_macros WHERE lower(name) = lower(%s)",
                (f"my usual lunch {tag}",),
            )
            assert n["n"] == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 7. §3.5 step 4 — decompose's save_intent creates/updates the macro at commit
# ---------------------------------------------------------------------------


def test_commit_save_intent_creates_then_updates_macro():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            log_id, eggs, coffee, rows = await committed_lunch(conn, tag)
            # decompose's save_intent rides the staged jsonb (logs.py's extra-key channel)
            await conn.execute(
                "UPDATE item_resolutions SET staged = staged || %s::jsonb "
                "WHERE log_id = %s AND item_index = 0",
                (Json({"save_intent": {"name": f"usual dinner {tag}"}}), log_id),
            )

            result = await commit_tx.commit_log(log_id, [r.staged for r in rows])
            assert result["meal"] is not None
            macro = await db.fetchone(
                "SELECT * FROM meal_macros WHERE id = %s", (result["meal"]["id"],)
            )
            assert macro["name"] == f"usual dinner {tag}"
            assert macro["trigger_phrases"] == [f"usual dinner {tag}"]
            assert macro["items"] == [
                {"food_id": eggs, "serving_id": "srv-large", "qty": 1.0},
                {"food_id": coffee, "serving_id": "srv-cup", "qty": 1.0},
            ]
            # tier-1 rows still learned their aliases (the skip is tier-0 only)
            a = await db.fetchone(
                "SELECT use_count FROM aliases WHERE alias_norm = %s", (f"eggs {tag}",)
            )
            assert a["use_count"] == 2

            # a second log, explicit intent (the "Save as meal" tap at commit),
            # same name → same macro, items replaced, triggers kept
            log2, _, _, rows2 = await committed_lunch(conn, f"{tag}b")
            result2 = await commit_tx.commit_log(
                log2, [rows2[0].staged], save_intent=SaveIntent(name=f"Usual Dinner {tag}")
            )
            assert result2["meal"]["id"] == macro["id"]
            macro2 = await db.fetchone("SELECT * FROM meal_macros WHERE id = %s", (macro["id"],))
            assert len(macro2["items"]) == 1 and macro2["items"][0]["serving_id"] == "srv-large"
            assert macro2["trigger_phrases"] == [f"usual dinner {tag}"]
            # (updated_at = now() — the transaction start under this rollback
            # harness, so it cannot be compared against created_at here)

            # a replayed commit returns the stored result and never re-saves
            replay = await commit_tx.commit_log(log2, [rows2[0].staged])
            assert replay["replayed"] is True

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 8. §8.4 — delete = deactivate; tier 0 ignores inactive macros
# ---------------------------------------------------------------------------


def test_delete_deactivates_and_tier0_ignores_inactive():
    async def run():
        async with isolated_db() as conn:
            fx = await breakfast_fixture(conn)
            assert await cascade.short_circuit_all(fx.trigger, Meal.snack) is not None

            resp = await delete_meal(fx.macro_id)
            assert resp.is_active is False
            row = await db.fetchone("SELECT * FROM meal_macros WHERE id = %s", (fx.macro_id,))
            assert row is not None and row["is_active"] is False  # row stays (§8.4)
            assert fx.macro_id not in {m.id for m in await list_meals()}

            # the trigger now falls through to the (decoy) alias — tier 1, not 0
            after = await cascade.short_circuit_all(fx.trigger, Meal.snack)
            assert after is not None and len(after) == 1 and after[0].staged.tier == 1
            rows = await cascade.resolve_items([parsed(fx.trigger, "meal_ref")], Meal.snack)
            assert rows[0].staged.status == "needs_input"

            await delete_meal(fx.macro_id)  # idempotent
            with pytest.raises(HTTPException) as exc:
                await delete_meal(uuid4())
            assert exc.value.status_code == 404

            # reactivation via PATCH brings tier 0 back
            back = await patch_meal(fx.macro_id, MealPatch(is_active=True))
            assert back.is_active is True
            again = await cascade.short_circuit_all(fx.trigger, Meal.snack)
            assert again is not None and len(again) == 3

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 9. §3.1 — a spoken quantity on a food phrase is about the food, not a macro
# ---------------------------------------------------------------------------


def test_spoken_qty_on_food_phrase_skips_tier0():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            eggs = await insert_food(conn, f"egg {tag}", [EGG])
            await insert_alias(conn, f"eggs {tag}", eggs)
            # a one-item macro saved from a log — its trigger IS a food phrase
            await insert_macro(
                conn, f"Eggs {tag}", [f"eggs {tag}"],
                [{"food_id": eggs, "serving_id": "srv-large", "qty": 6}],
            )

            # no spoken quantity → the macro expands at its stored qty
            rows = await cascade.resolve_items([parsed(f"eggs {tag}")], Meal.breakfast)
            assert len(rows) == 1
            assert (rows[0].staged.tier, rows[0].staged.qty) == (0, 6.0)

            # "two eggs" → tier 0 stands aside; the spoken qty survives (§7.3)
            rows = await cascade.resolve_items(
                [parsed(f"eggs {tag}", qty=2)], Meal.breakfast
            )
            assert len(rows) == 1
            assert (rows[0].staged.tier, rows[0].staged.qty) == (1, 2.0)
            assert rows[0].staged.food.id == eggs

            # same on the single-row path (/api/resolve)
            one = await cascade.resolve_item(parsed(f"eggs {tag}", qty=3), 0, Meal.breakfast)
            assert (one.staged.tier, one.staged.qty) == (1, 3.0)

            # a meal_ref stays a meal reference even with a quantity attached
            ref = await cascade.resolve_item(
                parsed(f"eggs {tag}", "meal_ref", qty=2), 0, Meal.breakfast
            )
            assert (ref.staged.tier, ref.staged.qty) == (0, 6.0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 10. §3.1 / §6.1 — the saved meal's NAME matches too (triggers can diverge)
# ---------------------------------------------------------------------------


def test_macro_matches_saved_meal_name_when_triggers_diverge():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            whey = await insert_food(conn, f"whey {tag}", [SCOOP])
            name = f"Post-workout shake {tag}"
            await insert_macro(
                conn, name, [f"pwo {tag}"],
                [{"food_id": whey, "serving_id": "srv-scoop", "qty": 1}],
            )

            # decompose echoes the NAME it saw in context (§6.1); the triggers
            # no longer contain it, so only the name match saves this from the
            # §3.1 "no saved meal" guard
            rows = await cascade.resolve_items(
                [parsed("the usual shake", "meal_ref", meal_ref=name)], Meal.snack
            )
            assert len(rows) == 1 and rows[0].staged.tier == 0
            assert rows[0].staged.food.id == whey

            # the name said verbatim short-circuits the whole utterance too
            sc = await cascade.short_circuit_all(f"My Post-workout shake {tag}!", Meal.snack)
            assert sc is not None and len(sc) == 1 and sc[0].staged.tier == 0

            # …and a trigger match still beats a name match (tier-0 ordering),
            # even when the name-matching macro is the more recently saved one
            other = await insert_food(conn, f"bar {tag}", [SCOOP])
            newer = await insert_macro(
                conn, f"pwo {tag}", [f"other trigger {tag}"],
                [{"food_id": other, "serving_id": "srv-scoop", "qty": 1}],
            )
            await conn.execute(  # now() is the tx clock — bump it explicitly
                "UPDATE meal_macros SET updated_at = now() + interval '1 hour' WHERE id = %s",
                (newer,),
            )
            won = await cascade.short_circuit_all(f"pwo {tag}", Meal.snack)
            assert won is not None and won[0].staged.food.id == whey

            # a meal-shaped phrase matching neither still gets the §3.1 guard
            miss = await cascade.resolve_item(
                parsed(f"usual nothing {tag}", "meal_ref"), 0, Meal.snack
            )
            assert miss.staged.status == "needs_input"
            assert miss.staged.question.text == cascade.MEAL_REF_GUARD.format(
                phrase=f"usual nothing {tag}"
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 11. §5 — reactivation cannot create a second active macro under one name
# ---------------------------------------------------------------------------


def test_reactivate_rejects_duplicate_active_name():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            whey = await insert_food(conn, f"whey {tag}", [SCOOP])
            items = [MealItemIn(food_id=whey, serving_id="srv-scoop")]
            first = await create_meal(MealCreate(name=f"Usual breakfast {tag}", items=items))
            await delete_meal(first.id)

            # the name is free again while the old macro is inactive
            second = await create_meal(
                MealCreate(
                    name=f"usual breakfast {tag}",
                    trigger_phrases=[f"second {tag}"],
                    items=items,
                )
            )

            # reactivating the old one would leave two active macros under one
            # name (find_macro / save_meal_macro would pick by updated_at) — 409
            with pytest.raises(HTTPException) as exc:
                await patch_meal(first.id, MealPatch(is_active=True))
            assert exc.value.status_code == 409
            row = await db.fetchone(
                "SELECT is_active FROM meal_macros WHERE id = %s", (first.id,)
            )
            assert row["is_active"] is False  # the rejected PATCH wrote nothing

            # renaming out of the collision reactivates fine
            back = await patch_meal(
                first.id, MealPatch(name=f"Old breakfast {tag}", is_active=True)
            )
            assert back.is_active is True and back.name == f"Old breakfast {tag}"

            # an ordinary edit of an active macro is unaffected by the check
            edited = await patch_meal(second.id, MealPatch(items=items))
            assert edited.is_active is True and edited.id == second.id

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 12. §3.1 — a saved meal whose foods rows are gone is never logged in part,
#     and never reported as a meal that does not exist
# ---------------------------------------------------------------------------


async def broken_macro(conn, tag, *, keep: int):
    """A 2-item saved meal with `keep` (1 or 0) of its foods still cached."""
    eggs = await insert_food(conn, f"egg {tag}", [EGG])
    oats = await insert_food(conn, f"oats {tag}", [OATS_40G])
    macro_id = await insert_macro(
        conn, f"Broken meal {tag}", [f"broken meal {tag}"],
        [
            {"food_id": eggs, "serving_id": "srv-large", "qty": 2},
            {"food_id": oats, "serving_id": "srv-40g", "qty": 1},
        ],
    )
    gone = [oats] if keep == 1 else [eggs, oats]
    await conn.execute("DELETE FROM foods WHERE id = ANY(%s::uuid[])", (gone,))
    return SimpleNamespace(
        macro_id=macro_id, eggs=eggs, oats=oats, trigger=f"broken meal {tag}",
        name=f"Broken meal {tag}",
    )


def test_partial_macro_is_surfaced_not_silently_truncated():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            fx = await broken_macro(conn, tag, keep=1)
            expected = cascade.MEAL_BROKEN_GUARD.format(name=fx.name, missing=1, total=2)

            # single-row path (/api/resolve): the one surviving item must NOT
            # take the len(rows) == 1 shortcut and log half the meal
            res = await cascade.resolve_item(parsed(fx.trigger, "meal_ref"), 0, Meal.lunch)
            assert res.staged.status == "needs_input"
            assert res.staged.tier is None and res.staged.food is None
            assert res.staged.question.text == expected

            # the whole-utterance log path stages the same guard, not one row
            sc = await cascade.short_circuit_all(f"My broken meal {tag}!", Meal.lunch)
            assert sc is not None and len(sc) == 1
            assert sc[0].staged.status == "needs_input"
            assert sc[0].staged.question.text == expected

            # …and so does the post-decompose path, at its allocated index
            rows = await cascade.resolve_items(
                [parsed(f"coffee {tag}"), parsed(fx.trigger, "meal_ref")], Meal.lunch
            )
            assert len(rows) == 2 and rows[1].staged.item_index == 1
            assert rows[1].staged.status == "needs_input"
            assert rows[1].staged.question.text == expected

    asyncio.run(run())


def test_macro_with_no_expandable_foods_is_not_denied():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            fx = await broken_macro(conn, tag, keep=0)
            expected = cascade.MEAL_BROKEN_GUARD.format(name=fx.name, missing=2, total=2)
            live = await db.fetchone(
                "SELECT is_active FROM meal_macros WHERE id = %s", (fx.macro_id,)
            )
            assert live["is_active"] is True  # the saved meal EXISTS

            res = await cascade.resolve_item(parsed(fx.trigger, "meal_ref"), 0, Meal.lunch)
            assert res.staged.status == "needs_input"
            assert "No saved meal" not in res.staged.question.text
            assert res.staged.question.text == expected

            sc = await cascade.short_circuit_all(fx.trigger, Meal.lunch)
            assert sc is not None and len(sc) == 1
            assert sc[0].staged.question.text == expected

            rows = await cascade.resolve_items([parsed(fx.trigger, "meal_ref")], Meal.lunch)
            assert len(rows) == 1 and rows[0].staged.question.text == expected

            # the genuine "no such meal" case still gets §3.1's guard verbatim
            miss = await cascade.resolve_item(
                parsed(f"usual nothing {tag}", "meal_ref"), 0, Meal.lunch
            )
            assert miss.staged.question.text == cascade.MEAL_REF_GUARD.format(
                phrase=f"usual nothing {tag}"
            )

    asyncio.run(run())
