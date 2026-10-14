"""X3 tests — goal wizard math, settings routes, /api/metrics series.

DESIGN.md §7.4 (wizard), §4 settings seeds, §5 routes, §9 instrumentation.

Pure goal math is tested against hand-computed cases. DB tests run inside a
transaction that is ROLLED BACK (same isolated_db harness as
test_logs_lifecycle.py) — the live Neon DB is never dirtied. Metrics rows are
seeded in the week of 2001-01-08 (a Monday) so real production data can never
share the bucket under assertion.

Run:  cd backend && uv run --with pytest pytest tests/test_goals_metrics.py -q
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json
from pydantic import ValidationError

import db

from tenancy_harness import bind_test_tenant  # noqa: E402
from routes.metrics import get_metrics, parse_range
from routes.settings import (
    GoalInputs,
    SettingsUpdate,
    compute_goals,
    get_settings,
    put_settings,
)
from services import goals


def _run(coro):
    return asyncio.run(coro)


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


# ===========================================================================
# goals.py — hand-computed cases
# ===========================================================================


def test_bmr_mifflin_st_jeor_hand_computed():
    # 10*82 + 6.25*180 - 5*28 = 820 + 1125 - 140 = 1805; male +5, female -161
    assert goals.bmr_mifflin_st_jeor(82, 180, 28, "male") == 1810.0
    assert goals.bmr_mifflin_st_jeor(82, 180, 28, "female") == 1644.0
    with pytest.raises(ValueError):
        goals.bmr_mifflin_st_jeor(82, 180, 28, "other")


def test_tdee_activity_multipliers():
    assert goals.tdee(1810.0, "moderate") == pytest.approx(2805.5)
    assert goals.tdee(1810.0, "sedentary") == pytest.approx(2172.0)
    assert goals.tdee(1810.0, "very_active") == pytest.approx(3439.0)
    with pytest.raises(ValueError):
        goals.tdee(1810.0, "couch")


def test_kcal_target_deficit_rounds_half_up():
    # 0.5 kg/week × 7700 / 7 = 550 kcal/day deficit; 2805.5 - 550 = 2255.5 → 2256
    kt = goals.kcal_target(2805.5, 82, "male", goal_weight_kg=76, weekly_rate_kg=0.5)
    assert kt.daily_delta_kcal == pytest.approx(-550.0)
    assert kt.kcal == 2256
    assert kt.notes == []


def test_kcal_target_surplus_and_maintenance():
    up = goals.kcal_target(2805.5, 82, "male", goal_weight_kg=86, weekly_rate_kg=0.25)
    assert up.daily_delta_kcal == pytest.approx(275.0)
    assert up.kcal == 3081  # 3080.5 → half-up
    same = goals.kcal_target(2805.5, 82, "male", goal_weight_kg=82, weekly_rate_kg=0.5)
    assert same.daily_delta_kcal == 0.0 and same.kcal == 2806
    none = goals.kcal_target(2805.5, 82, "male")
    assert none.daily_delta_kcal == 0.0 and none.kcal == 2806


def test_kcal_target_safe_floor_clamps():
    # female 50 kg / 160 cm / 40 sedentary: BMR 1139, TDEE 1366.8, -1100 → 266.8 → floor 1200
    bmr = goals.bmr_mifflin_st_jeor(50, 160, 40, "female")
    assert bmr == 1139.0
    kt = goals.kcal_target(goals.tdee(bmr, "sedentary"), 50, "female", 45, 1.0)
    assert kt.kcal == 1200
    assert any("safe minimum" in n for n in kt.notes)


def test_weekly_rate_clamped_to_max():
    kt = goals.kcal_target(2805.5, 82, "male", goal_weight_kg=70, weekly_rate_kg=3.0)
    assert kt.daily_delta_kcal == pytest.approx(-1100.0)  # 1.0 kg/week, not 3.0
    assert any("capped" in n for n in kt.notes)
    assert kt.kcal == 1706  # 2805.5 - 1100 = 1705.5 → half-up


def test_macro_split_defaults_protein_2_fat_floor_carbs_remainder():
    # 2256 kcal @ 82 kg: protein 164 g (656 kcal), fat 65.6 → 66 g (594 kcal),
    # carbs (2256 - 1250) / 4 = 251.5 → 252 g
    ms = goals.macro_split(2256, 82)
    assert (ms.protein_g, ms.fat_g, ms.carb_g) == (164, 66, 252)
    assert ms.protein_g_per_kg == 2.0 and ms.fat_g_per_kg == 0.8
    assert ms.notes == []


def test_macro_split_overrides_and_fat_floor():
    ms = goals.macro_split(2256, 82, protein_g_per_kg=1.6, fat_g_per_kg=1.0)
    assert ms.protein_g == 131  # 131.2
    assert ms.fat_g == 82
    assert ms.carb_g == 249  # (2256 - 524 - 738) / 4 = 248.5 → 249
    low_fat = goals.macro_split(2256, 82, fat_g_per_kg=0.5)
    assert low_fat.fat_g == 66 and low_fat.fat_g_per_kg == 0.8
    assert any("Fat" in n for n in low_fat.notes)


def test_macro_split_carbs_never_negative():
    ms = goals.macro_split(1200, 150)  # protein 300 g (1200) + fat 120 g (1080) > 1200
    assert ms.carb_g == 0
    assert any("carbs" in n for n in ms.notes)


def test_compute_targets_end_to_end_shape():
    out = goals.compute_targets(
        height_cm=180, weight_kg=82, age=28, sex="male", activity_level="moderate",
        goal_weight_kg=76, weekly_rate_kg=0.5,
    )
    assert out["bmr"] == 1810
    assert out["tdee"] == 2806  # 2805.5 half-up (display only; kcal uses the unrounded TDEE)
    assert out["daily_delta_kcal"] == -550
    assert out["targets"] == {"kcal": 2256, "protein_g": 164, "carb_g": 252, "fat_g": 66}
    assert out["notes"] == []


def test_round_half_up_is_symmetric():
    assert goals.round_half_up(2.5) == 3
    assert goals.round_half_up(-2.5) == -3
    assert goals.round_half_up(2.4999) == 2


# ===========================================================================
# settings route models — range validation (pydantic raises before the route)
# ===========================================================================


@pytest.mark.parametrize(
    "payload",
    [
        {"body": {"age": 5}},
        {"body": {"height_cm": 30}},
        {"body": {"weight_kg": 500}},
        {"body": {"sex": "yes"}},
        {"body": {"activity_level": "couch"}},
        {"body": {"weekly_rate_kg": 2.0}},
        {"targets": {"kcal": 100, "protein_g": 100, "carb_g": 100, "fat_g": 50}},
        {"targets": {"kcal": 2000, "protein_g": -1, "carb_g": 100, "fat_g": 50}},
        {"targets": {"kcal": 2000, "protein_g": 100}},  # incomplete
        {"meal_windows": {
            "breakfast": {"start": "11:00", "end": "04:00"},
            "lunch": {"start": "11:00", "end": "15:30"},
            "dinner": {"start": "15:30", "end": "21:30"},
        }},
        {"meal_windows": {
            "breakfast": {"start": "4am", "end": "11:00"},
            "lunch": {"start": "11:00", "end": "15:30"},
            "dinner": {"start": "15:30", "end": "21:30"},
        }},
        {"timezone": "Mars/Olympus"},
        {"cronometer_baseline_days_per_week": 8},
        {"profile_notes": "x" * 4001},
    ],
)
def test_settings_put_rejects_out_of_range(payload):
    with pytest.raises(ValidationError):
        SettingsUpdate(**payload)


def test_goal_inputs_require_full_body_and_ranges():
    with pytest.raises(ValidationError):
        GoalInputs(height_cm=180, weight_kg=82, age=28, sex="male")  # no activity_level
    with pytest.raises(ValidationError):
        GoalInputs(height_cm=180, weight_kg=82, age=5, sex="male", activity_level="moderate")
    ok = GoalInputs(height_cm=180, weight_kg=82, age=28, sex="male", activity_level="moderate",
                    weekly_rate_kg=3.0)  # over the cap is clamped by the math, not rejected
    assert ok.weekly_rate_kg == 3.0


# ===========================================================================
# settings routes — DB (rolled back)
# ===========================================================================


def test_settings_put_then_get_roundtrip_and_partial_update():
    async def scenario():
        async with isolated_db() as conn:
            before = await get_settings()
            assert before.timezone  # seeded
            full = SettingsUpdate(
                body={"height_cm": 180, "weight_kg": 82, "age": 28, "sex": "male",
                      "activity_level": "moderate", "goal_weight_kg": 76, "weekly_rate_kg": 0.5},
                targets={"kcal": 2256, "protein_g": 164, "carb_g": 252, "fat_g": 66},
                meal_windows={
                    "breakfast": {"start": "05:00", "end": "10:30"},
                    "lunch": {"start": "10:30", "end": "14:00"},
                    "dinner": {"start": "17:00", "end": "22:00"},
                    "default": "snack",
                },
                profile_notes="lactose intolerant; 'fairlife' means Core Power Elite 42g",
                timezone="America/New_York",
                cronometer_baseline_days_per_week=2,
            )
            out = await put_settings(full)
            assert out.body.weight_kg == 82 and out.body.sex == "male"
            assert out.targets is not None and out.targets.protein_g == 164
            assert out.meal_windows.dinner.end == "22:00"
            assert out.profile_notes.startswith("lactose")
            assert out.timezone == "America/New_York"
            assert out.cronometer_baseline_days_per_week == 2

            again = await get_settings()
            assert again == out

            # partial PUT touches only the keys present
            partial = await put_settings(SettingsUpdate(profile_notes="trains at 6pm"))
            assert partial.profile_notes == "trains at 6pm"
            assert partial.targets == out.targets
            assert partial.timezone == "America/New_York"

            # keys outside the page's surface are untouched by PUT (food_index stays)
            cur = await conn.execute("SELECT value FROM settings WHERE key = 'food_index'")
            assert (await cur.fetchone())["value"]["inline_default"] == 20

            # rows are real jsonb, keyed exactly as §4 seeds them
            cur = await conn.execute("SELECT value FROM settings WHERE key = 'body'")
            assert (await cur.fetchone())["value"]["goal_weight_kg"] == 76

    _run(scenario())


def test_goals_compute_proposes_without_saving():
    async def scenario():
        async with isolated_db() as conn:
            await conn.execute("UPDATE settings SET value = '{}'::jsonb WHERE key = 'targets'")
            proposal = await compute_goals(GoalInputs(
                height_cm=180, weight_kg=82, age=28, sex="male", activity_level="moderate",
                goal_weight_kg=76, weekly_rate_kg=0.5,
            ))
            assert proposal.bmr == 1810
            assert proposal.targets.kcal == 2256
            assert proposal.targets.protein_g == 164
            assert proposal.targets.carb_g == 252
            assert proposal.targets.fat_g == 66
            cur = await conn.execute("SELECT value FROM settings WHERE key = 'targets'")
            assert (await cur.fetchone())["value"] == {}  # not saved until PUT
            assert (await get_settings()).targets is None

    _run(scenario())


# ===========================================================================
# metrics — DB (rolled back), seeded in the week of 2001-01-08
# ===========================================================================

WEEK = date(2001, 1, 8)  # Monday


def test_parse_range():
    assert parse_range("12w") == 12
    assert parse_range("1w") == 1
    assert parse_range("all") is None
    for bad in ("0w", "abc", "12", "w", "1000w"):
        with pytest.raises(HTTPException) as ei:
            parse_range(bad)
        assert ei.value.status_code == 422


async def _insert_log(conn, *, captured_at, status, committed_at=None, staged_ms=None,
                      short_circuit=False, capture_fallback=False, capture_error=None):
    log_id = uuid4()
    await conn.execute(
        """
        INSERT INTO logs (id, input_mode, raw_input, captured_at, status, committed_at,
                          staged_ms, short_circuit, capture_fallback, capture_error)
        VALUES (%s, 'text', 'x3 seed', %s, %s, %s, %s, %s, %s, %s)
        """,
        (log_id, captured_at, status, committed_at, staged_ms, short_circuit,
         capture_fallback, capture_error),
    )
    return log_id


async def _insert_item(conn, log_id, idx, *, tier, status="resolved",
                       was_edited=False, was_disambiguated=False):
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status, staged,
                                      tier, was_edited, was_disambiguated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (log_id, idx, f"seed{idx}", f"seed{idx}", status, Json({"item_index": idx}),
         tier, was_edited, was_disambiguated),
    )


async def _insert_food(conn, display_name, per_100g, source_id):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, food_type, servings,
                           default_serving_id, per_100g, payload)
        VALUES ('fdc', %s, %s, %s, 'generic', %s, '100g', %s, '{}'::jsonb)
        RETURNING id
        """,
        (source_id, display_name, display_name,
         Json([{"serving_id": "100g", "description": "100 g", "grams": 100, "macros": per_100g}]),
         Json(per_100g)),
    )
    return (await cur.fetchone())["id"]


def test_metrics_series_shapes_and_companion_denominator():
    async def scenario():
        async with isolated_db() as conn:
            cur = await conn.execute("SELECT value FROM settings WHERE key = 'timezone'")
            tz = ZoneInfo((await cur.fetchone())["value"])
            tue = datetime(2001, 1, 9, 12, 0, tzinfo=tz)
            wed = datetime(2001, 1, 10, 12, 0, tzinfo=tz)
            s = timedelta(seconds=1)

            # A: committed, 60 s capture→commit, staged 800 ms; items t1 (hit), t3 (miss,
            #    edited), and a removed row (excluded everywhere)
            a = await _insert_log(conn, captured_at=tue, status="committed",
                                  committed_at=tue + 60 * s, staged_ms=800)
            await _insert_item(conn, a, 0, tier=1)
            await _insert_item(conn, a, 1, tier=3, was_edited=True)
            await _insert_item(conn, a, 2, tier=None, status="removed", was_edited=True)
            # B: committed short-circuit, 20 s, staged 400 ms; item tier 0
            b = await _insert_log(conn, captured_at=tue, status="committed",
                                  committed_at=tue + 20 * s, staged_ms=400, short_circuit=True)
            await _insert_item(conn, b, 0, tier=0)
            # C: discarded — items unresolved + tier 3: the companion's extra misses
            c = await _insert_log(conn, captured_at=wed, status="discarded")
            await _insert_item(conn, c, 0, tier=None, status="not_found")
            await _insert_item(conn, c, 1, tier=3)
            # D: voice capture failure (server-detected empty transcript)
            await _insert_log(conn, captured_at=wed, status="capture_failure",
                              capture_error="empty_transcript")
            # E: committed after a client-side blob fallback; 40 s, no staged_ms; item t2 disambiguated
            e = await _insert_log(conn, captured_at=wed, status="committed",
                                  committed_at=wed + 40 * s, capture_fallback=True)
            await _insert_item(conn, e, 0, tier=2, was_disambiguated=True)

            # foods: a duplicate-suspect pair (shared token 'yogurt', identical per_100g)
            # and a same-macro food with no shared token (not a pair)
            m = {"kcal": 59, "protein_g": 10.2, "carb_g": 3.6, "fat_g": 0.4}
            fa = await _insert_food(conn, "greek yogurt, plain", m, "x3-dup-a")
            fb = await _insert_food(conn, "yogurt, greek, plain, nonfat", m, "x3-dup-b")
            fc = await _insert_food(conn, "cottage cheese", m, "x3-dup-c")

            # entries on two distinct local dates (three rows) → adherence 2 days
            for when in (tue, tue + 3600 * s, wed):
                await conn.execute(
                    "INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, macros) "
                    "VALUES (%s, 'lunch', %s, '100g', 1, %s)",
                    (when, fa, Json(m)),
                )

            resp = await get_metrics(range="all")
            assert resp.timezone == tz.key
            assert resp.weeks[0] == WEEK  # nothing real predates the seed week
            today = datetime.now(tz).date()
            assert resp.weeks[-1] == today - timedelta(days=today.weekday())
            assert len(resp.latency) == len(resp.hit_rate) == len(resp.adherence) \
                == len(resp.secondary) == len(resp.weeks)
            assert [p.week for p in resp.latency] == resp.weeks

            i = resp.weeks.index(WEEK)
            lat = resp.latency[i]
            assert lat.committed_logs == 3
            # commit ms sorted [20000, 40000, 60000]: p50 = 40000, p90 = 40000 + 0.8*20000
            assert lat.commit_ms_p50 == pytest.approx(40000)
            assert lat.commit_ms_p90 == pytest.approx(56000)
            # staged_ms [400, 800] (E has none): p50 = 600, p90 = 760
            assert lat.staged_ms_p50 == pytest.approx(600)
            assert lat.staged_ms_p90 == pytest.approx(760)

            hr = resp.hit_rate[i]
            assert (hr.items, hr.hits) == (4, 3)  # A0 A1 B0 E0; misses: A1
            assert hr.rate == pytest.approx(0.75)
            assert (hr.items_incl_discarded, hr.hits_incl_discarded) == (6, 3)  # + C0 C1
            assert hr.rate_incl_discarded == pytest.approx(0.5)
            assert hr.rate_incl_discarded < hr.rate  # the companion must not flatter

            assert resp.adherence[i].days_logged == 2
            cur = await conn.execute(
                "SELECT value FROM settings WHERE key = 'cronometer_baseline_days_per_week'")
            assert resp.baseline_days_per_week == int((await cur.fetchone())["value"])

            sec = resp.secondary[i]
            assert sec.committed_logs == 3 and sec.items == 4
            assert sec.edited == 1 and sec.edit_rate == pytest.approx(0.25)  # removed A2 excluded
            assert sec.disambiguated == 1 and sec.disambiguation_rate == pytest.approx(0.25)
            assert sec.tier3 == 1  # A1; C1 is in a discarded log
            assert sec.short_circuit_logs == 1 and sec.short_circuit_share == pytest.approx(1 / 3)
            assert sec.discarded_logs == 1
            assert sec.capture_failures == 2  # D (server-detected) + E (client fallback)

            # a silent week in range is emitted as zeros / null rates
            quiet = resp.hit_rate[i + 1]
            assert quiet.week == WEEK + timedelta(days=7)
            assert quiet.items == 0 and quiet.rate is None
            assert resp.adherence[i + 1].days_logged == 0
            assert resp.latency[i + 1].commit_ms_p50 is None

            pairs = {(p.food_a, p.food_b) for p in resp.duplicate_suspects}
            assert (str(fa), str(fb)) in pairs or (str(fb), str(fa)) in pairs
            assert not any(str(fc) in pair for pair in pairs)

            # a bounded range drops the seed week and spans exactly n Mondays
            short = await get_metrics(range="2w")
            assert len(short.weeks) == 2 and WEEK not in short.weeks
            assert short.weeks[-1] == resp.weeks[-1]
            assert short.weeks[0] == short.weeks[-1] - timedelta(days=7)

    _run(scenario())
