"""§7.2 meal inference — clock windows corrected by today's diary (2026-10-13).

Replays the owner's 2026-10-12: ramen, a probiotic and sausages logged at
15:30–15:32, the day's first food, which the clock alone filed as dinner.

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one force-rollback connection) — the live Neon DB keeps
no row.

Run:  uv run --project backend --with pytest pytest backend/tests/test_meal_inference.py -q
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
from psycopg.rows import dict_row

import db  # noqa: E402
from models import Meal  # noqa: E402
from routes.logs import infer_meal  # noqa: E402
from tenancy_harness import bind_test_tenant  # noqa: E402

TZ = "America/Los_Angeles"


def at(day: int, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=ZoneInfo(TZ))


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
    await bind_test_tenant(conn)  # 002: app role + the fixed test tenant, inside the rollback
    orig = db.fetchone

    async def fetchone(sql, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchone()

    db.fetchone = fetchone
    try:
        yield conn
    finally:
        db.fetchone = orig
        await outer.__aexit__(None, None, None)  # force rollback
        await conn.close()


async def _entry(conn, when: datetime, meal: str) -> None:
    food = await (await conn.execute("SELECT id FROM foods LIMIT 1")).fetchone()
    await conn.execute(
        "INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, macros) "
        "VALUES (%s, %s, %s, 'label', 1, '{}'::jsonb)",
        (when, meal, food["id"]),
    )


def test_mondays_late_lunch_and_the_items_after_it():
    async def run():
        async with isolated_db() as conn:
            got = [await infer_meal(at(12, "15:30"), TZ)]  # first food of the day
            await _entry(conn, at(12, "15:30"), got[0].value)
            got.append(await infer_meal(at(12, "15:31"), TZ))  # same sitting
            await _entry(conn, at(12, "15:31"), got[1].value)
            got.append(await infer_meal(at(12, "18:11"), TZ))  # lunch done, 2.5 h later
            return got

    assert asyncio.run(run()) == [Meal.lunch, Meal.lunch, Meal.dinner]


def test_late_lunch_needs_an_empty_day_and_ends_at_five():
    async def run():
        async with isolated_db() as conn:
            empty_late = await infer_meal(at(12, "17:05"), TZ)
            await _entry(conn, at(12, "12:30"), "lunch")
            had_lunch = await infer_meal(at(12, "16:30"), TZ)
            return empty_late, had_lunch

    assert asyncio.run(run()) == (Meal.dinner, Meal.dinner)


def test_a_snack_does_not_count_as_the_days_lunch():
    async def run():
        async with isolated_db() as conn:
            await _entry(conn, at(12, "02:32"), "snack")
            return await infer_meal(at(12, "15:45"), TZ)

    assert asyncio.run(run()) == Meal.lunch


def test_same_sitting_reads_diary_time_not_capture_time():
    """A breakfast back-dated to 08:00 (spoken at 12:38) must not pull a
    12:46 photo into breakfast; a 21:40 dessert joins the 21:10 dinner."""

    async def run():
        async with isolated_db() as conn:
            await _entry(conn, at(15, "08:00"), "breakfast")
            noon = await infer_meal(at(15, "12:46"), TZ)
            await _entry(conn, at(15, "21:10"), "dinner")
            dessert = await infer_meal(at(15, "21:40"), TZ)
            midnight = await infer_meal(at(16, "00:30"), TZ)  # 3 h 20 min later
            return noon, dessert, midnight

    assert asyncio.run(run()) == (Meal.lunch, Meal.dinner, Meal.snack)
