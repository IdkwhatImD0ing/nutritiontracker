"""Phase-4 fix-pass tests (adversarial panel, 2026-08-21).

1. §3.1 — a tier-0 expansion row stays tier 0 through EVERY edit path
   (alternates chip → resolve_item_from_food, manual pick →
   resolve_item_from_source, rephrase → POST /api/resolve), so commit never
   learns an alias for the display-name phrase and §9 never counts a macro
   hit as a tier-3 resolution.
2. §7.2 / §9 — logs.captured_at is immutable. The when-hint's diary time
   rides the staged jsonb, survives per-item rewrites, lands on
   entries.logged_at for every row of the log (added rows included), and the
   commit route's totals are for the shifted day.
3. §8.4 — the create paths: an itemless "save this as my usual lunch" builds
   the macro from the most recent committed log (or says why not), and the
   commit route's save_intent ("Save as meal" on the staging sheet) creates it.

Rollback harness + run-time stubs as in test_meals.py / test_logs_lifecycle.py.
services.tier3 and routes.resolve are the REAL modules — every path exercised
resolves locally (no agent run, no network); tracing, services.context and
services.decompose are stubbed.

Run:  uv run --project backend --with pytest pytest backend/tests/test_phase4_fixes.py -q
"""

from __future__ import annotations

import asyncio
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
from psycopg.rows import dict_row
from psycopg.types.json import Json

# ---------------------------------------------------------------------------
# stubs — tracing / context / decompose only; services.tier3 stays REAL
# ---------------------------------------------------------------------------

_decompose: dict = {"fn": None}


async def _decompose_dispatch(text, ctx, **kw):
    fn = _decompose["fn"]
    if fn is None:
        raise AssertionError("decompose must not be called by this test")
    return await fn(text, ctx, **kw)


def _build_stubs() -> dict:
    tracing_mod = make_tracing_stub("trace-test")

    ctx_mod = types.ModuleType("services.context")

    async def build_agent_context():
        return SimpleNamespace(profile_notes="", targets={}, food_index=[], meal_names=[], recent_foods=[])

    ctx_mod.build_agent_context = build_agent_context

    dec_mod = types.ModuleType("services.decompose")
    dec_mod.decompose = _decompose_dispatch
    return {
        "tracing": tracing_mod,
        "services.context": ctx_mod,
        "services.decompose": dec_mod,
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


@pytest.fixture(autouse=True)
def _reset_decompose():
    _decompose["fn"] = None
    yield
    _decompose["fn"] = None


import db  # noqa: E402


from tenancy_harness import bind_test_tenant  # noqa: E402
import services.tier3 as tier3  # noqa: E402
from models import (  # noqa: E402
    CommitRequest,
    LogCreate,
    Meal,
    ParsedItem,
    ParsedLog,
    ResolveRequest,
    SaveIntent,
)
from resolver import cascade  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes import commit as commit_routes  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes import resolve as resolve_routes  # noqa: E402
from routes.commit import CommitItemRequest, CommitRequestIn  # noqa: E402
from routes.commit import commit_item as commit_item_route  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from routes.diary import local_date  # noqa: E402
from routes.logs import when_shift  # noqa: E402
from routes.resolve import Answer, AnswerRequest  # noqa: E402

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731
TZ = "America/Los_Angeles"

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
# rollback harness
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


async def insert_food(conn, name, servings, *, brand=None, source="fdc", source_id=None):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, brand, food_type,
                           servings, default_serving_id, payload)
        VALUES (%s, %s, %s, %s, %s, 'generic', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (source, source_id or uuid4().hex, name, name, brand, Json(servings),
         servings[0]["serving_id"]),
    )
    return str((await cur.fetchone())["id"])


async def insert_alias(conn, alias_norm, food_id):
    await conn.execute(
        "INSERT INTO aliases (alias_norm, food_id, origin) VALUES (%s, %s, 'manual')",
        (alias_norm, food_id),
    )


async def insert_macro(conn, name, triggers, items):
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
    """eggs ×2, oats ×1, whey ×1 as the 'usual breakfast <tag>' macro."""
    tag = uuid4().hex[:6]
    eggs = await insert_food(conn, f"egg {tag}", [EGG])
    oats = await insert_food(conn, f"oats {tag}", [OATS_40G])
    whey = await insert_food(conn, f"whey {tag}", [SCOOP], brand="Optimum")
    trigger = f"usual breakfast {tag}"
    macro_id = await insert_macro(
        conn, f"Usual breakfast {tag}", [trigger],
        [
            {"food_id": eggs, "serving_id": "srv-large", "qty": 2},
            {"food_id": oats, "serving_id": "srv-40g", "qty": 1},
            {"food_id": whey, "serving_id": "srv-scoop", "qty": 1},
        ],
    )
    return SimpleNamespace(
        eggs=eggs, oats=oats, whey=whey, trigger=trigger, tag=tag, macro_id=macro_id
    )


def parsed(phrase, kind="food", meal_ref=None):
    return ParsedItem(
        phrase=phrase, phrase_kind=kind, qty=None, unit=None, state=None,
        brand=None, library_match=None, meal_ref=meal_ref,
    )


def parsed_log(items, *, when_hint=None, save_intent=None,
               when_day_offset=None, when_meal=None):
    return ParsedLog(items=items, meal_hint=None, when_hint=when_hint,
                     when_day_offset=when_day_offset, when_meal=when_meal,
                     save_intent=save_intent)


def log_body(text, log_id=None):
    return LogCreate(id=log_id or uuid4(), text=text, captured_at=NOW(), tz=TZ)


async def item_row(log_id, idx):
    return await db.fetchone(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s",
        (log_id, idx),
    )


async def alias_count(norms):
    row = await db.fetchone(
        "SELECT count(*) AS n FROM aliases WHERE alias_norm = ANY(%s)", (list(norms),)
    )
    return row["n"]


async def committed_lunch(conn, tag):
    """A committed two-item log (tier 1 via aliases) → (log_id, eggs, coffee, rows)."""
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


# ---------------------------------------------------------------------------
# 1. §3.1 — tier 0 survives every edit path; commit learns nothing
# ---------------------------------------------------------------------------


def test_tier0_rows_stay_tier0_through_chip_manual_pick_and_rephrase():
    async def run():
        async with isolated_db() as conn:
            fx = await breakfast_fixture(conn)
            whites = await insert_food(conn, f"egg whites {fx.tag}", [EGG])
            oats_off = await insert_food(
                conn, f"steel cut oats {fx.tag}", [OATS_40G],
                source="off", source_id=f"off-{fx.tag}",
            )
            coffee = await insert_food(conn, f"coffee {fx.tag}", [CUP])
            await insert_alias(conn, f"coffee {fx.tag}", coffee)

            rows = await cascade.short_circuit_all(fx.trigger, Meal.breakfast)
            assert rows is not None and [r.staged.tier for r in rows] == [0, 0, 0]
            log_id = await insert_log(conn, raw=fx.trigger)
            await logs_routes._persist(log_id, rows, Meal.breakfast, short_circuit=True)
            display_norms = [norm(r.staged.phrase) for r in rows]

            # (a) alternates / alias chip on the eggs row → egg whites
            chip = await resolve_routes.answer_question(
                AnswerRequest(
                    log_id=log_id, item_index=0,
                    answer=Answer(label=f"egg whites {fx.tag}", food_id=whites),
                )
            )
            assert chip.status == "resolved" and chip.food.id == whites
            assert chip.tier == 0  # the wire keeps it too
            assert (await item_row(log_id, 0))["tier"] == 0

            # (b) manual pick (an already-cached OFF food) on the oats row
            manual = await tier3.resolve_item_from_source(
                str(log_id), 1, "off", f"off-{fx.tag}"
            )
            assert manual.status == "resolved" and manual.food.id == oats_off
            assert manual.tier == 0
            assert (await item_row(log_id, 1))["tier"] == 0

            # (c) rephrase through /api/resolve on the whey row → tier-1 alias
            # hit on the NEW text (decompose stub: exact alias → no micro-call)
            re = await resolve_routes.resolve_item(
                ResolveRequest(log_id=log_id, item_index=2, phrase=f"coffee {fx.tag}")
            )
            assert re.status == "resolved" and re.food.id == coffee and re.tier == 1
            row2 = await item_row(log_id, 2)
            assert row2["tier"] == 0  # the column keeps the row's origin
            assert row2["phrase"] == rows[2].staged.phrase  # original display name
            assert row2["was_edited"] is True

            before = await alias_count(display_norms)
            coffee_uses = (await db.fetchone(
                "SELECT use_count FROM aliases WHERE alias_norm = %s", (f"coffee {fx.tag}",)
            ))["use_count"]

            totals = await commit_route(log_id, CommitRequest(items=[chip, manual, re]))
            assert totals.errors == []

            # NO alias for any display-name phrase, NO use on the coffee alias,
            # every row still tier 0 with no alias_id; the edits are recorded
            assert await alias_count(display_norms) == before == 0
            assert await alias_count([norm(f"egg whites {fx.tag}")]) == 0
            assert (await db.fetchone(
                "SELECT use_count FROM aliases WHERE alias_norm = %s", (f"coffee {fx.tag}",)
            ))["use_count"] == coffee_uses
            res = await db.fetchall(
                "SELECT * FROM item_resolutions WHERE log_id = %s ORDER BY item_index", (log_id,)
            )
            assert [r["tier"] for r in res] == [0, 0, 0]
            assert all(r["alias_id"] is None and r["committed_at"] is not None for r in res)
            assert [str(r["final_food_id"]) for r in res] == [whites, oats_off, coffee]
            assert all(r["was_edited"] for r in res)
            entries = await db.fetchall(
                "SELECT food_id, alias_id FROM entries WHERE log_id = %s ORDER BY item_index",
                (log_id,),
            )
            assert [str(e["food_id"]) for e in entries] == [whites, oats_off, coffee]
            assert all(e["alias_id"] is None for e in entries)

            # regression guard: a row with NO tier (searching → chip) is tier 3
            other = await insert_log(conn)
            await conn.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged)
                VALUES (%s, 0, %s, %s, 'searching', %s)
                """,
                (other, f"mystery {fx.tag}", f"mystery {fx.tag}",
                 Json({"item_index": 0, "phrase": f"mystery {fx.tag}",
                       "status": "searching", "meal": "snack"})),
            )
            late = await tier3.resolve_item_from_food(str(other), 0, whites)
            assert late.tier == 3 and (await item_row(other, 0))["tier"] == 3

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. §7.2 / §9 — captured_at immutable; the shift lands on entries.logged_at
# ---------------------------------------------------------------------------


def test_when_hint_keeps_captured_at_and_lands_every_entry_on_the_shifted_day():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            eggs = await insert_food(conn, f"egg {tag}", [EGG])
            coffee = await insert_food(conn, f"coffee {tag}", [CUP])
            await insert_alias(conn, f"eggs {tag}", eggs)
            await insert_alias(conn, f"coffee {tag}", coffee)

            async def fake_decompose(text, ctx, **kw):
                return parsed_log([parsed(f"eggs {tag}")], when_hint="yesterday at lunch",
                                  when_day_offset=-1, when_meal=Meal.lunch)

            _decompose["fn"] = fake_decompose
            body = log_body(f"yesterday at lunch i had eggs {tag}")
            resp = await logs_routes.create_log(body)
            assert resp.meal == Meal.lunch and len(resp.items) == 1
            expected = when_shift(-1, Meal.lunch, None, body.captured_at, body.tz).logged_at
            assert expected < body.captured_at

            log = await db.fetchone(
                "SELECT captured_at, meal_inferred FROM logs WHERE id = %s", (body.id,)
            )
            assert log["captured_at"] == body.captured_at  # the anchor never moves
            assert log["meal_inferred"] == "lunch"
            row0 = await item_row(body.id, 0)
            assert datetime.fromisoformat(row0["staged"]["logged_at"]) == expected

            # a per-item rewrite (rephrase via /api/resolve) keeps the key …
            re = await resolve_routes.resolve_item(
                ResolveRequest(log_id=body.id, item_index=0, phrase=f"eggs {tag}")
            )
            assert re.status == "resolved"
            row0 = await item_row(body.id, 0)
            assert datetime.fromisoformat(row0["staged"]["logged_at"]) == expected
            # … and so does a tier-3 result write
            ok = await tier3._write_item_row(
                str(body.id), 0, re, proposed_food_id=eggs, resolve_ms=1, tier=1
            )
            assert ok
            row0 = await item_row(body.id, 0)
            assert datetime.fromisoformat(row0["staged"]["logged_at"]) == expected

            # an ADDED row (no logged_at of its own) lands on the same day
            added = await resolve_routes.resolve_item(
                ResolveRequest(log_id=body.id, item_index=1, phrase=f"coffee {tag}")
            )
            assert added.status == "resolved" and "logged_at" not in (
                await item_row(body.id, 1)
            )["staged"]

            totals = await commit_route(body.id, CommitRequest(items=[re, added]))
            assert totals.errors == []
            entries = await db.fetchall(
                "SELECT logged_at, macros FROM entries WHERE log_id = %s ORDER BY item_index",
                (body.id,),
            )
            assert [e["logged_at"] for e in entries] == [expected, expected]
            # the commit response totals are for the day the entries landed on
            assert totals.kcal >= 78 + 2 - 0.01

            # §9: the latency sample is capture → commit (seconds), not the
            # ~30h the when-hint shifted the diary day by. captured_at is the
            # client's clock and committed_at the DB server's, so a small
            # negative sample is ordinary skew, not a day; metrics.py floors it.
            lat = await db.fetchone(
                "SELECT EXTRACT(EPOCH FROM (committed_at - captured_at)) AS s,"
                "       EXTRACT(EPOCH FROM GREATEST(committed_at - captured_at, interval '0')) AS floored"
                " FROM logs WHERE id = %s",
                (body.id,),
            )
            assert -60 < float(lat["s"]) < 120
            assert float(lat["floored"]) >= 0

            # a log without a when-hint still lands on captured_at
            async def plain(text, ctx):
                return parsed_log([parsed(f"coffee {tag}")])

            _decompose["fn"] = plain
            body2 = log_body(f"a coffee {tag}")
            resp2 = await logs_routes.create_log(body2)
            assert "logged_at" not in (await item_row(body2.id, 0))["staged"]
            await commit_route(body2.id, CommitRequest(items=list(resp2.items)))
            e2 = await db.fetchone("SELECT logged_at FROM entries WHERE log_id = %s", (body2.id,))
            assert e2["logged_at"] == body2.captured_at

    asyncio.run(run())


def test_commit_retry_and_late_item_still_bill_the_shifted_day():
    """The commit rewrite of the staged jsonb must carry `logged_at` forward.
    The first commit reads it BEFORE that rewrite, so only a SECOND read
    exposes a loss: the §8.6 idempotent retry and the §3.5 commit-item late
    path both re-read it and would otherwise bill the entries to today."""

    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            eggs = await insert_food(conn, f"egg {tag}", [EGG])
            coffee = await insert_food(conn, f"coffee {tag}", [CUP])
            await insert_alias(conn, f"eggs {tag}", eggs)
            await insert_alias(conn, f"coffee {tag}", coffee)

            async def fake_decompose(text, ctx, **kw):
                return parsed_log([parsed(f"eggs {tag}")], when_hint="yesterday at lunch",
                                  when_day_offset=-1, when_meal=Meal.lunch)

            _decompose["fn"] = fake_decompose
            body = log_body(f"yesterday at lunch i had eggs {tag}")
            resp = await logs_routes.create_log(body)
            expected = when_shift(-1, Meal.lunch, None, body.captured_at, body.tz).logged_at
            shift_day = await local_date(expected)
            assert shift_day != await local_date(body.captured_at)  # the shift crosses days

            # the day each route billed its totals to — the user-visible symptom
            days: list = []
            real_totals = commit_routes.compute_day_totals

            async def spy(day):
                days.append(day)
                return await real_totals(day)

            commit_routes.compute_day_totals = spy
            try:
                await commit_route(body.id, CommitRequest(items=list(resp.items)))
                # the commit rewrite kept the key — every later read still finds it
                row0 = await item_row(body.id, 0)
                assert datetime.fromisoformat(row0["staged"]["logged_at"]) == expected

                # (a) a retried commit replays onto the SAME day
                retried = await commit_route(body.id, CommitRequest(items=list(resp.items)))

                # (b) a straggler appended after the commit lands there too
                added = await resolve_routes.resolve_item(
                    ResolveRequest(log_id=body.id, item_index=1, phrase=f"coffee {tag}")
                )
                assert added.status == "resolved" and added.food.id == coffee
                late = await commit_item_route(
                    body.id, CommitItemRequest(item_index=1, item=added)
                )
            finally:
                commit_routes.compute_day_totals = real_totals

            assert days == [shift_day, shift_day, shift_day]
            assert retried.kcal >= 78 - 0.01 and late.kcal >= 78 + 2 - 0.01
            entries = await db.fetchall(
                "SELECT logged_at FROM entries WHERE log_id = %s ORDER BY item_index",
                (body.id,),
            )
            assert [e["logged_at"] for e in entries] == [expected, expected]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. §8.4 — the create paths actually create
# ---------------------------------------------------------------------------


def test_itemless_save_intent_saves_the_last_committed_log():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            log_id, eggs, coffee, rows = await committed_lunch(conn, tag)
            items = [rows[0].staged.model_copy(update={"qty": 3.0}), rows[1].staged]
            await commit_route(log_id, CommitRequest(items=items))

            async def fake_decompose(text, ctx, **kw):
                return parsed_log([], save_intent=SaveIntent(name=f"Usual Lunch {tag}"))

            _decompose["fn"] = fake_decompose
            body = log_body(f"save this as my usual lunch {tag}")
            resp = await logs_routes.create_log(body)
            assert resp.items == [] and resp.note is None
            assert resp.saved_meal is not None
            assert resp.saved_meal.name == f"Usual Lunch {tag}"
            assert resp.saved_meal.item_count == 2

            macro = await db.fetchone(
                "SELECT * FROM meal_macros WHERE id = %s", (resp.saved_meal.meal_id,)
            )
            assert macro["trigger_phrases"] == [f"usual lunch {tag}"]
            assert macro["items"] == [
                {"food_id": eggs, "serving_id": "srv-large", "qty": 3.0},  # as committed
                {"food_id": coffee, "serving_id": "srv-cup", "qty": 1.0},
            ]
            # …and it expands at tier 0 from now on
            exp = await cascade.short_circuit_all(f"my usual lunch {tag}", Meal.lunch)
            assert exp is not None and [r.staged.tier for r in exp] == [0, 0]
            # the itemless log is a staged row nothing counts
            log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (body.id,))
            assert log["status"] == "staged"

            # nothing committed yet → an honest note, no macro
            real_fetchone = db.fetchone

            async def no_committed(sql, params=()):
                if "l.status = 'committed'" in sql:
                    return None
                return await real_fetchone(sql, params)

            db.fetchone = no_committed
            try:
                resp2 = await logs_routes.create_log(
                    log_body(f"save this as my usual dinner {tag}")
                )
            finally:
                db.fetchone = real_fetchone
            fake_name = f"Usual Lunch {tag}"  # the stub always returns this name
            assert resp2.saved_meal is None and resp2.items == []
            assert resp2.note == f"Nothing to save as '{fake_name}' yet — log the items first."

    asyncio.run(run())


def test_commit_route_save_intent_creates_macro_and_reports_it():
    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            log_id, eggs, coffee, rows = await committed_lunch(conn, tag)
            resp = await commit_route(
                log_id,
                CommitRequestIn(
                    items=[r.staged for r in rows],
                    save_intent=SaveIntent(name=f"Usual Lunch {tag}"),
                ),
            )
            assert resp.errors == []
            assert resp.saved_meal is not None and resp.saved_meal.item_count == 2
            macro = await db.fetchone(
                "SELECT * FROM meal_macros WHERE id = %s", (resp.saved_meal.meal_id,)
            )
            assert macro["name"] == f"Usual Lunch {tag}"
            assert macro["items"] == [
                {"food_id": eggs, "serving_id": "srv-large", "qty": 1.0},
                {"food_id": coffee, "serving_id": "srv-cup", "qty": 1.0},
            ]
            # tier-1 rows still learned (the skip is tier-0 only)
            a = await db.fetchone(
                "SELECT use_count FROM aliases WHERE alias_norm = %s", (f"eggs {tag}",)
            )
            assert a["use_count"] == 2
            # no save_intent → no macro, nothing reported
            log2, *_ = await committed_lunch(conn, f"{tag}b")
            resp2 = await commit_route(log2, CommitRequest(items=[]))
            assert resp2.saved_meal is None

    asyncio.run(run())
