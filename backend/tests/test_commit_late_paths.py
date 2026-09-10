"""The commit paths added for the phrase-corpus findings (docs/PHRASE_FINDINGS.md).

Three branches that decide what reaches the diary and what the client is told,
none of which had a test:

1. `_row_logged_at` — one log's rows can name different diary days (§7.2), so
   each entry takes its OWN row's shifted time, not the log's first one.
2. `_extend_saved_meal` — §3.5's late path finishes what step 4 started: an
   item that resolves after the commit joins the meal the commit saved. And it
   must never be able to lose the entry it is bookkeeping for.
3. `_stored_refusals` — a replayed commit reports the SAME refusals as the
   first one, because the client's retry set is built from that list alone.

Same rollback harness as test_m1_findings.py: every test runs inside a
transaction that is rolled back, so nothing persists.

Run:  uv run --with pytest pytest backend/tests/test_commit_late_paths.py -q
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from tracing_stub import make_tracing_stub

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json


@pytest.fixture(scope="module", autouse=True)
def _tracing_stub():
    stub = make_tracing_stub()
    saved = sys.modules.get("tracing")
    sys.modules["tracing"] = stub
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop("tracing", None)
        else:
            sys.modules["tracing"] = saved


import db  # noqa: E402

from models import FoodRef, Meal, ServingRef, StagedItem  # noqa: E402
from resolver import commit as commit_tx  # noqa: E402
from resolver.norm import norm  # noqa: E402
from tenancy_harness import bind_test_tenant  # noqa: E402

COMPLETE = {
    "serving_id": "srv-scoop",
    "description": "1 scoop",
    "grams": 31,
    "macros": {"kcal": 120, "protein_g": 25, "carb_g": 3, "fat_g": 1.5},
}
PARTIAL_100G = {  # no carb/fat — the core-macros guard refuses this one
    "serving_id": "srv-100g",
    "description": "100 g",
    "grams": 100,
    "macros": {"kcal": 56, "protein_g": 10},
}


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(
        db._database_url(), row_factory=dict_row
    )
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
    await bind_test_tenant(conn)
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
        await outer.__aexit__(None, None, None)
        await conn.close()


async def insert_food(conn, name, servings, default_serving):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, food_type,
                           servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, 'generic', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, Json(servings), default_serving),
    )
    return str((await cur.fetchone())["id"])


async def insert_log(conn, status="staged"):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status) "
        "VALUES (%s, 'text', 'x', now(), %s)",
        (log_id, status),
    )
    return log_id


async def seed_item(conn, log_id, idx, phrase, staged, *, status="resolved"):
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                      status, staged)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (log_id, idx, phrase, norm(phrase), status, Json(staged)),
    )


def staged(phrase, food_id, name, serving, *, logged_at=None, save_intent=None):
    d = {
        "item_index": 0,
        "phrase": phrase,
        "status": "resolved",
        "tier": 1,
        "food": {"id": food_id, "name": name},
        "serving": {"id": serving["serving_id"], "description": serving["description"]},
        "qty": 1.0,
        "macros": dict(serving["macros"]),
        "meal": "snack",
        "confidence": "high",
    }
    if logged_at is not None:
        d["logged_at"] = logged_at.isoformat()
    if save_intent is not None:
        d["save_intent"] = {"name": save_intent}
    return d


def item(idx, phrase, food_id, name, serving):
    return StagedItem(
        item_index=idx, phrase=phrase, status="resolved", tier=1,
        food=FoodRef(id=food_id, name=name),
        serving=ServingRef(id=serving["serving_id"],
                           description=serving["description"]),
        qty=1.0, meal=Meal.snack,
    )


def searching(idx, phrase):
    """A row still in tier 3 at commit time. It must ride in the payload: a
    non-removed row the payload omits is marked 'removed' by the commit, which
    is what closes the §3.5 late path for it."""
    return StagedItem(item_index=idx, phrase=phrase, status="searching",
                      meal=Meal.snack)


def tok() -> str:
    return "zx" + uuid4().hex[:10]


# ---------------------------------------------------------------------------
# 1. _row_logged_at — a log is not always one moment
# ---------------------------------------------------------------------------


def test_row_logged_at_prefers_the_rows_own_day_and_survives_junk():
    """Pure: no database. The row's own stamp wins; anything unparseable falls
    back rather than raising, because a bad string in the staged jsonb must not
    be able to fail a commit."""
    fallback = datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc)
    own = datetime(2026, 9, 8, 18, 30, tzinfo=timezone.utc)

    assert commit_tx._row_logged_at(None, fallback) == fallback
    assert commit_tx._row_logged_at({"staged": {}}, fallback) == fallback
    assert commit_tx._row_logged_at(
        {"staged": {"logged_at": own.isoformat()}}, fallback
    ) == own
    assert commit_tx._row_logged_at(
        {"staged": {"logged_at": "not-a-date"}}, fallback
    ) == fallback


def test_two_rows_naming_two_days_commit_to_two_days():
    """The agent lane can shift log_food's rows and log_estimate's rows
    separately in one turn. Stamping every entry with one row's value moved
    food onto a day the user never named."""
    async def run():
        async with isolated_db() as conn:
            food = await insert_food(conn, "Whey", [COMPLETE], "srv-scoop")
            log_id = await insert_log(conn)
            now = datetime.now(timezone.utc)
            yesterday = now - timedelta(days=1)
            p0, p1 = f"a{tok()}", f"b{tok()}"
            # Both rows stamped, which is the real shape: log_food shifts its
            # own rows and log_estimate shifts its own, in one turn. (An
            # UNSTAMPED row would inherit the log-level value, which is the
            # first stamped row's — so it would land on that day too.)
            await seed_item(conn, log_id, 0, p0,
                            staged(p0, food, "Whey", COMPLETE, logged_at=yesterday))
            await seed_item(conn, log_id, 1, p1,
                            staged(p1, food, "Whey", COMPLETE, logged_at=now))

            result = await commit_tx.commit_log(
                log_id,
                [item(0, p0, food, "Whey", COMPLETE),
                 item(1, p1, food, "Whey", COMPLETE)],
            )
            rows = await db.fetchall(
                "SELECT item_index, logged_at FROM entries WHERE log_id = %s "
                "ORDER BY item_index",
                (log_id,),
            )
            assert len(rows) == 2
            assert rows[0]["logged_at"].date() != rows[1]["logged_at"].date()
            # and the caller is told it touched two days, so it cannot paint
            # one day's totals as the whole picture
            assert len(result["logged_at_days"]) == 2

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. _extend_saved_meal — the late path finishes the meal, and never eats the entry
# ---------------------------------------------------------------------------


def test_a_late_item_joins_the_meal_the_commit_saved():
    async def run():
        async with isolated_db() as conn:
            a = await insert_food(conn, "Eggs", [COMPLETE], "srv-scoop")
            b = await insert_food(conn, "Toast", [COMPLETE], "srv-scoop")
            log_id = await insert_log(conn)
            name = f"usual {tok()}"
            p0, p1 = f"e{tok()}", f"t{tok()}"
            # row 0 resolved and carries the save intent; row 1 still searching
            await seed_item(conn, log_id, 0, p0,
                            staged(p0, a, "Eggs", COMPLETE, save_intent=name))
            await seed_item(conn, log_id, 1, p1,
                            staged(p1, b, "Toast", COMPLETE, save_intent=name),
                            status="searching")

            await commit_tx.commit_log(
                log_id, [item(0, p0, a, "Eggs", COMPLETE), searching(1, p1)]
            )
            macro = await db.fetchone(
                "SELECT items FROM meal_macros WHERE lower(name) = lower(%s)", (name,)
            )
            assert len(macro["items"]) == 1, "step 4 saved only what had resolved"

            # the straggler lands afterwards
            await commit_tx.commit_item(log_id, item(1, p1, b, "Toast", COMPLETE))
            macro = await db.fetchone(
                "SELECT items FROM meal_macros WHERE lower(name) = lower(%s)", (name,)
            )
            assert {i["food_id"] for i in macro["items"]} == {a, b}

    asyncio.run(run())


def test_a_meal_that_cannot_be_revalidated_does_not_lose_the_entry():
    """Bookkeeping is a side effect of the entry, never a condition of it.
    validate_meal_items re-checks the WHOLE stored list, so a food deleted out
    of the macro since it was saved raises MealInvalid — inside the commit's
    own transaction, where an uncaught raise would roll back the diary row that
    just committed and lose the item the late path exists to save."""
    async def run():
        async with isolated_db() as conn:
            a = await insert_food(conn, "Eggs", [COMPLETE], "srv-scoop")
            b = await insert_food(conn, "Toast", [COMPLETE], "srv-scoop")
            log_id = await insert_log(conn)
            name = f"usual {tok()}"
            p0, p1 = f"e{tok()}", f"t{tok()}"
            await seed_item(conn, log_id, 0, p0,
                            staged(p0, a, "Eggs", COMPLETE, save_intent=name))
            await seed_item(conn, log_id, 1, p1,
                            staged(p1, b, "Toast", COMPLETE, save_intent=name),
                            status="searching")
            await commit_tx.commit_log(
                log_id, [item(0, p0, a, "Eggs", COMPLETE), searching(1, p1)]
            )

            # the macro now names a food that no longer exists
            await conn.execute(
                "UPDATE meal_macros SET items = %s WHERE lower(name) = lower(%s)",
                (Json([{"food_id": str(uuid4()), "serving_id": "srv-scoop", "qty": 1}]),
                 name),
            )

            await commit_tx.commit_item(log_id, item(1, p1, b, "Toast", COMPLETE))
            row = await db.fetchone(
                "SELECT id FROM entries WHERE log_id = %s AND item_index = 1", (log_id,)
            )
            assert row is not None, "the entry must survive a broken saved meal"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. _stored_refusals — the second answer is the same as the first
# ---------------------------------------------------------------------------


def test_a_replayed_commit_reports_the_refusal_the_first_one_recorded():
    """A lost response sends the user back to tap Log again. The client builds
    its retry set from `errors` alone, so a replay that answers [] is a row
    nothing will ever chase."""
    async def run():
        async with isolated_db() as conn:
            good = await insert_food(conn, "Good", [COMPLETE], "srv-scoop")
            bad = await insert_food(conn, "Bad", [PARTIAL_100G], "srv-100g")
            log_id = await insert_log(conn)
            pg, pb = f"g{tok()}", f"b{tok()}"
            await seed_item(conn, log_id, 0, pg, staged(pg, good, "Good", COMPLETE))
            await seed_item(conn, log_id, 1, pb, staged(pb, bad, "Bad", PARTIAL_100G))

            first = await commit_tx.commit_log(
                log_id,
                [item(0, pg, good, "Good", COMPLETE),
                 item(1, pb, bad, "Bad", PARTIAL_100G)],
            )
            assert [e["item_index"] for e in first["errors"]] == [1]

            replay = await commit_tx.commit_log(
                log_id, [item(0, pg, good, "Good", COMPLETE)]
            )
            assert replay["replayed"] is True
            assert [e["item_index"] for e in replay["errors"]] == [1]
            assert first["errors"][0]["error"] == replay["errors"][0]["error"]

    asyncio.run(run())
