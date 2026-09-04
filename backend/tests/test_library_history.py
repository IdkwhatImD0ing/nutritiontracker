"""X2 tests — Library, aliases, food detail, diary display, history (M3).

DESIGN.md §3.2 (pick/pin semantics), §5 (/api/aliases, /api/diary), §7.1
("everything the database returned"), §7.4 (Library, History, food detail),
§3.4a (citation in the diary); BACKLOG 33 + the Phase-3 base_* sidecar leak.

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one force-rollback connection, same harness as
test_logs_lifecycle.py) — the live Neon DB is never dirtied. Routes are called
as plain functions (auth is a router dependency, not exercised here).

Run:  uv run --with pytest pytest backend/tests/test_library_history.py -q
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

if __import__("sys").platform == "win32":  # psycopg async needs the selector loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import db  # noqa: E402

from tenancy_harness import bind_test_tenant  # noqa: E402
from routes.aliases import AliasPatch, delete_alias, list_aliases, patch_alias  # noqa: E402
from routes.diary import get_diary  # noqa: E402
from routes.foods import get_food_detail  # noqa: E402
from routes.history import get_history  # noqa: E402

TZ = "America/Los_Angeles"


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
        # pin the settings the routes read (rolled back with everything else)
        await conn.execute(
            "UPDATE settings SET value = %s WHERE key = 'timezone'", (Json(TZ),)
        )
        await conn.execute(
            "UPDATE settings SET value = %s WHERE key = 'targets'",
            (Json({"kcal": 2400, "protein_g": 180, "carb_g": 250, "fat_g": 80}),),
        )
        yield conn
    finally:
        db.fetchall, db.fetchone, db.execute, db.tx = orig
        await outer.__aexit__(None, None, None)  # force rollback
        await conn.close()


# ---------------------------------------------------------------------------
# seed helpers
# ---------------------------------------------------------------------------

FDC_NUTRIENTS = [  # a slice of a real /food/{id} foodNutrients shape
    {"nutrient": {"id": 1003, "name": "Protein", "unitName": "g"}, "amount": 31.02},
    {"nutrient": {"id": 1004, "name": "Total lipid (fat)", "unitName": "g"}, "amount": 3.57},
    {"nutrient": {"id": 1005, "name": "Carbohydrate, by difference", "unitName": "g"}, "amount": 0.0},
    {"nutrient": {"id": 1008, "name": "Energy", "unitName": "kcal"}, "amount": 165.0},
    {"nutrient": {"id": 1103, "name": "Selenium, Se", "unitName": "µg"}, "amount": 27.6},
    {"nutrient": {"id": 1180, "name": "Choline, total", "unitName": "mg"}, "amount": 85.3},  # unmapped key
    {"nutrient": {"id": 1316, "name": "PUFA 18:2", "unitName": "g"}, "amount": 0.6},  # unmapped key
    {"nutrient": {"id": 1062, "name": "Energy", "unitName": "kJ"}, "amount": None},  # header row
]


async def seed_food(conn, display_name, *, brand=None, source="fdc", payload=None,
                    servings=None, source_url=None, provenance=None, state_sensitive=False):
    servings = servings or [
        {"serving_id": "100g", "description": "100 g", "grams": 100.0,
         "macros": {"kcal": 165.0, "protein_g": 31.02, "carb_g": 0.0, "fat_g": 3.57}},
        {"serving_id": "breast", "description": "1 breast (174 g)", "grams": 174.0,
         "macros": {"kcal": 287.1, "protein_g": 53.97, "carb_g": 0.0, "fat_g": 6.21}},
    ]
    row = await (
        await conn.execute(
            """
            INSERT INTO foods (source, source_id, source_url, provenance, name, display_name,
                               brand, food_type, state, state_sensitive, servings,
                               default_serving_id, per_100g, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                source, f"test-{uuid4().hex[:12]}", source_url,
                Json(provenance) if provenance is not None else None,
                display_name, display_name, brand, "branded" if brand else "generic",
                state_sensitive, Json(servings), servings[0]["serving_id"],
                Json(servings[0]["macros"]), Json(payload or {}),
            ),
        )
    ).fetchone()
    return str(row["id"])


async def seed_alias(conn, alias_norm, food_id, *, use_count=1, pinned=False,
                     last_used_days_ago=0, last_explicit_days_ago=None,
                     default_serving_id=None, default_qty=None):
    now = datetime.now(timezone.utc)
    row = await (
        await conn.execute(
            """
            INSERT INTO aliases (alias_norm, food_id, default_serving_id, default_qty, pinned,
                                 use_count, last_used_at, last_explicit_at, origin)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'tier3')
            RETURNING id
            """,
            (alias_norm, food_id, default_serving_id, default_qty, pinned, use_count,
             now - timedelta(days=last_used_days_ago),
             None if last_explicit_days_ago is None
             else now - timedelta(days=last_explicit_days_ago)),
        )
    ).fetchone()
    return str(row["id"])


async def seed_entry(conn, food_id, *, logged_at, meal="lunch", qty=1.0,
                     macros=None, alias_id=None, serving_id="100g", grams=100.0):
    macros = macros or {"kcal": 165.0, "protein_g": 31.02, "carb_g": 0.0, "fat_g": 3.57}
    row = await (
        await conn.execute(
            """
            INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, grams, macros, alias_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (logged_at, meal, food_id, serving_id, qty, grams, Json(macros), alias_id),
        )
    ).fetchone()
    return row["id"]


def local_noon(day: date) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=ZoneInfo(TZ))


def today_local() -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()


# ---------------------------------------------------------------------------
# 1. aliases — list by phrase (norm + usual), pin, default portion, delete
# ---------------------------------------------------------------------------


def test_aliases_list_pin_default_delete():
    async def run():
        async with isolated_db() as conn:
            key = f"fairlife{uuid4().hex[:6]}"
            core = await seed_food(conn, "core power elite chocolate", brand="Fairlife")
            milk = await seed_food(conn, "2% milk", brand="Fairlife")
            a_core = await seed_alias(conn, key, core, use_count=60)
            a_milk = await seed_alias(conn, key, milk, use_count=3, last_used_days_ago=40)
            entry_id = await seed_entry(conn, milk, logged_at=local_noon(today_local()),
                                        alias_id=a_milk)

            # norm() applied: article + punctuation + case never matter
            resp = await list_aliases(phrase=f"My {key.upper()}!")
            assert resp.phrase_norm == key
            by_id = {a.id: a for a in resp.aliases}
            assert set(by_id) == {a_core, a_milk}
            assert by_id[a_core].use_count == 60 and by_id[a_core].food_name == "core power elite chocolate"
            assert by_id[a_core].brand == "Fairlife" and by_id[a_core].source == "fdc"
            assert by_id[a_core].usual is True and by_id[a_milk].usual is False  # §3.2 rule 4

            # pin the rival: exclusive per phrase, explicit, flips the usual (§3.2 rule 1)
            pinned = await patch_alias(UUID(a_milk), AliasPatch(pinned=True))
            assert pinned.pinned is True and pinned.last_explicit_at is not None
            resp = await list_aliases(phrase=key)
            by_id = {a.id: a for a in resp.aliases}
            assert by_id[a_milk].usual is True and by_id[a_core].usual is False
            assert by_id[a_core].pinned is False
            # a second pin moves the pin — exactly one pinned row per phrase
            await patch_alias(UUID(a_core), AliasPatch(pinned=True))
            rows = await db.fetchall(
                "SELECT id, pinned FROM aliases WHERE alias_norm = %s", (key,)
            )
            assert {str(r["id"]): r["pinned"] for r in rows} == {a_core: True, a_milk: False}

            # default portion: validated against the food's servings (§4)
            with pytest.raises(HTTPException) as exc:
                await patch_alias(UUID(a_core), AliasPatch(default_serving_id="nope"))
            assert exc.value.status_code == 422
            with pytest.raises(HTTPException) as exc:
                await patch_alias(UUID(a_core), AliasPatch(default_qty=0))
            assert exc.value.status_code == 422
            with pytest.raises(HTTPException) as exc:
                await patch_alias(UUID(a_core), AliasPatch())
            assert exc.value.status_code == 422
            updated = await patch_alias(
                UUID(a_core), AliasPatch(default_serving_id="breast", default_qty=2)
            )
            assert updated.default_serving_id == "breast"
            assert updated.default_serving == "1 breast (174 g)"
            assert updated.default_qty == 2.0

            # delete a mapping: the row goes, history keeps its snapshot
            gone = await delete_alias(UUID(a_milk))
            assert gone.deleted is True and gone.id == a_milk
            assert await db.fetchone("SELECT 1 FROM aliases WHERE id = %s", (a_milk,)) is None
            e = await db.fetchone("SELECT alias_id, macros FROM entries WHERE id = %s", (entry_id,))
            assert e["alias_id"] is None and e["macros"]["kcal"] == 165.0
            with pytest.raises(HTTPException) as exc:
                await delete_alias(UUID(a_milk))
            assert exc.value.status_code == 404
            resp = await list_aliases(phrase=key)
            assert [a.id for a in resp.aliases] == [a_core]

            # Library listing (no phrase) includes the survivor
            everything = await list_aliases()
            assert a_core in {a.id for a in everything.aliases}
            assert everything.phrase is None

            with pytest.raises(HTTPException) as exc:
                await list_aliases(phrase="the")  # norm() strips it to nothing
            assert exc.value.status_code == 422

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. food detail — every nutrient the payload carries, badge, aliases, citation
# ---------------------------------------------------------------------------


def test_food_detail_returns_everything_the_database_returned():
    async def run():
        async with isolated_db() as conn:
            food_id = await seed_food(
                conn, "chicken breast, roasted", state_sensitive=True,
                payload={
                    "fdcId": 171077, "dataType": "SR Legacy",
                    "publishedDate": "4/1/2019", "foodNutrients": FDC_NUTRIENTS,
                },
            )
            alias_id = await seed_alias(conn, f"chicken{uuid4().hex[:6]}", food_id, use_count=7)

            detail = await get_food_detail(UUID(food_id))
            assert detail.id == food_id and detail.display_name == "chicken breast, roasted"
            # every non-null nutrient row, including keys our adapter never maps
            names = {n.name for n in detail.nutrients}
            expected = {n["nutrient"]["name"] for n in FDC_NUTRIENTS if n["amount"] is not None}
            assert names == expected
            assert "Choline, total" in names and "PUFA 18:2" in names
            assert len(detail.nutrients) == len(expected)  # the null header row is skipped
            sel = next(n for n in detail.nutrients if n.name == "Selenium, Se")
            assert sel.amount == 27.6 and sel.unit == "µg" and sel.basis == "per 100 g"
            # servings with macros, the default, and the §4 per_100g block
            assert [s.serving_id for s in detail.servings] == ["100g", "breast"]
            assert detail.servings[1].grams == 174.0 and detail.servings[1].macros["protein_g"] == 53.97
            assert detail.default_serving_id == "100g" and detail.per_100g["kcal"] == 165.0
            # source badge with date + staleness flag (published 2019)
            assert detail.badge.source == "fdc" and detail.badge.data_type == "SR Legacy"
            assert detail.badge.published_date == "4/1/2019"
            assert any("old record" in f for f in detail.badge.quality_flags)
            assert detail.citation is None and detail.state_sensitive is True
            # the aliases pointing at this food, with use counts
            assert [(a.id, a.use_count) for a in detail.aliases] == [(alias_id, 7)]

            # a web-transcribed food: citation + provenance + per-serving values
            web_id = await seed_food(
                conn, "Big Mac", brand="McDonald's", source="web",
                source_url="https://www.mcdonalds.com/us/en-us/product/big-mac.html",
                provenance={
                    "page_title": "Big Mac | McDonald's",
                    "citations": [{"url": "https://www.mcdonalds.com/x", "title": "Big Mac"}],
                    "values_as_read": {"serving_description": "1 burger (219 g)", "grams": 219,
                                       "kcal": 590, "protein_g": 25, "carb_g": 46, "fat_g": 34,
                                       "fiber_g": 3, "sugar_g": None, "sodium_mg": 1050},
                    "transcribed_at": "2026-08-21T00:00:00+00:00",
                },
                servings=[{"serving_id": "web", "description": "1 burger (219 g)", "grams": 219.0,
                           "macros": {"kcal": 590, "protein_g": 25, "carb_g": 46, "fat_g": 34}}],
            )
            web = await get_food_detail(UUID(web_id))
            assert web.citation is not None
            assert web.citation.url.startswith("https://www.mcdonalds.com/")
            assert web.citation.title == "Big Mac | McDonald's"
            assert web.badge.source == "web" and web.badge.published_date == "2026-08-21T00:00:00+00:00"
            assert {n.name for n in web.nutrients} == {
                "grams", "kcal", "protein_g", "carb_g", "fat_g", "fiber_g", "sodium_mg"
            }  # sugar_g was not read → absent, never zero
            assert web.provenance["citations"][0]["url"] == "https://www.mcdonalds.com/x"
            assert web.aliases == []

            with pytest.raises(HTTPException) as exc:
                await get_food_detail(uuid4())
            assert exc.value.status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. diary — grouped by meal, base_* sidecar stripped, citation on web foods
# ---------------------------------------------------------------------------


def test_diary_strips_base_sidecar_and_carries_citation():
    async def run():
        async with isolated_db() as conn:
            day = today_local() - timedelta(days=400)  # a day nothing else touches
            food = await seed_food(conn, "chicken breast, roasted")
            web = await seed_food(
                conn, "Big Mac", brand="McDonald's", source="web",
                source_url="https://www.mcdonalds.com/us/en-us/product/big-mac.html",
                provenance={"page_title": "Big Mac | McDonald's", "citations": []},
                servings=[{"serving_id": "web", "description": "1 burger", "grams": 219.0,
                           "macros": {"kcal": 590, "protein_g": 25, "carb_g": 46, "fat_g": 34}}],
            )
            # an entry that has been qty-edited: the BACKLOG 28 sidecar is present
            await seed_entry(
                conn, food, logged_at=local_noon(day), meal="lunch", qty=1.5, grams=150.0,
                macros={"kcal": 247.5, "protein_g": 46.53, "carb_g": 0.0, "fat_g": 5.36,
                        "fiber_g": 0.0, "base_qty": 1.0, "base_grams": 100.0, "base_kcal": 165.0,
                        "base_protein_g": 31.02, "base_carb_g": 0.0, "base_fat_g": 3.57,
                        "base_fiber_g": 0.0},
            )
            await seed_entry(
                conn, web, logged_at=local_noon(day) + timedelta(hours=7), meal="dinner",
                serving_id="web", grams=219.0,
                macros={"kcal": 590, "protein_g": 25, "carb_g": 46, "fat_g": 34},
            )

            resp = await get_diary(date=day)
            assert resp.date == day
            assert set(resp.meals) == {"breakfast", "lunch", "dinner", "snack"}
            assert len(resp.meals["lunch"]) == 1 and len(resp.meals["dinner"]) == 1
            assert resp.meals["breakfast"] == [] and resp.meals["snack"] == []

            lunch = resp.meals["lunch"][0]
            wire = lunch.macros.model_dump()
            assert not any(k.startswith("base_") for k in wire), wire
            assert wire["kcal"] == 247.5 and wire["protein_g"] == 46.53 and wire["fiber_g"] == 0.0
            assert lunch.qty == 1.5 and lunch.grams == 150.0
            assert lunch.source == "fdc" and lunch.web_source is None
            assert lunch.serving.description == "100 g"

            dinner = resp.meals["dinner"][0]
            assert dinner.source == "web"
            assert dinner.web_source is not None
            assert dinner.web_source.url == "https://www.mcdonalds.com/us/en-us/product/big-mac.html"
            assert dinner.web_source.title == "Big Mac | McDonald's"

            # totals = SUM over snapshots (display values, sidecar ignored) vs targets
            assert resp.totals.kcal == 837.5 and resp.totals.protein_g == 71.53
            assert resp.totals.targets.protein_g == 180.0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. history — per-day totals, streak, weekly averages + adherence for 'y'
# ---------------------------------------------------------------------------


def test_history_ranges_streak_and_weekly_averages():
    async def run():
        async with isolated_db() as conn:
            today = today_local()
            food = await seed_food(conn, "chicken breast, roasted")
            m = {"kcal": 100.0, "protein_g": 10.0, "carb_g": 5.0, "fat_g": 2.0, "fiber_g": 1.0}

            # today: two meals; yesterday + 2 days ago: one entry each → streak 3;
            # 3 days ago: nothing; 4 days ago: one entry; 60 days ago: two entries
            await seed_entry(conn, food, logged_at=local_noon(today), meal="lunch", macros=m)
            await seed_entry(conn, food, logged_at=local_noon(today) + timedelta(hours=7),
                             meal="dinner", qty=2, macros={k: v * 2 for k, v in m.items()})
            await seed_entry(conn, food, logged_at=local_noon(today - timedelta(days=1)), macros=m)
            await seed_entry(conn, food, logged_at=local_noon(today - timedelta(days=2)), macros=m)
            await seed_entry(conn, food, logged_at=local_noon(today - timedelta(days=4)),
                             macros={k: v for k, v in m.items() if k != "fiber_g"})
            old = today - timedelta(days=60)
            await seed_entry(conn, food, logged_at=local_noon(old), macros=m)
            await seed_entry(conn, food, logged_at=local_noon(old) + timedelta(hours=1), macros=m)
            # a late-night entry at 23:30 local stays on ITS local day (tz-correct bucketing)
            await seed_entry(
                conn, food, meal="snack",
                logged_at=datetime.combine(today - timedelta(days=1), time(23, 30), tzinfo=ZoneInfo(TZ)),
                macros=m,
            )

            # --- 3d: full per-day detail with per-meal subtotals
            r3 = await get_history(rng="3d")
            assert r3.range == "3d" and r3.timezone == TZ
            assert [d.date for d in r3.days] == [today - timedelta(days=2), today - timedelta(days=1), today]
            assert r3.start == today - timedelta(days=2) and r3.end == today
            t = r3.days[-1]
            assert t.logged and t.entries == 2
            assert (t.kcal, t.protein_g, t.carb_g, t.fat_g, t.fiber_g) == (300.0, 30.0, 15.0, 6.0, 3.0)
            assert set(t.meals) == {"lunch", "dinner"}
            assert t.meals["dinner"].protein_g == 20.0 and t.meals["lunch"].kcal == 100.0
            y = r3.days[1]
            assert y.entries == 2 and y.kcal == 200.0 and set(y.meals) == {"lunch", "snack"}
            assert r3.streak == 3
            assert r3.targets.kcal == 2400.0 and r3.baseline_days_per_week == 0.0
            assert r3.weeks == []

            # --- w: continuous 7-day series, unlogged days are zero/logged=false
            rw = await get_history(rng="w")
            assert len(rw.days) == 7 and rw.days[-1].date == today
            by_date = {d.date: d for d in rw.days}
            gap = by_date[today - timedelta(days=3)]
            assert gap.logged is False and gap.entries == 0 and gap.kcal == 0.0
            assert gap.meals is None and gap.fiber_g is None
            four = by_date[today - timedelta(days=4)]
            assert four.logged and four.kcal == 100.0 and four.fiber_g is None  # not measured
            assert rw.days[0].meals is None  # per-meal detail is the 3d view's
            assert sum(d.entries for d in rw.days) == 6

            # --- m: 30 days, the 60-day-old entries fall outside
            rm = await get_history(rng="m")
            assert len(rm.days) == 30 and sum(d.entries for d in rm.days) == 6
            assert rm.start == today - timedelta(days=29)

            # --- y: 52 Monday-aligned weeks, averages over logged days, adherence strip
            ry = await get_history(rng="y")
            assert len(ry.weeks) == 52 and ry.days == []
            assert all(w.week_start.weekday() == 0 for w in ry.weeks)
            assert ry.weeks[-1].week_start == today - timedelta(days=today.weekday())
            assert ry.weeks[-1].week_start <= today <= ry.weeks[-1].week_start + timedelta(days=6)
            by_week = {w.week_start: w for w in ry.weeks}
            old_week = old - timedelta(days=old.weekday())
            ow = by_week[old_week]
            assert ow.days_logged == 1 and ow.adherence == pytest.approx(1 / 7)
            assert ow.avg is not None and ow.avg.kcal == 200.0 and ow.avg.protein_g == 20.0
            logged_total = sum(w.days_logged for w in ry.weeks)
            assert logged_total == 5  # today, -1, -2, -4, -60
            empty = next(w for w in ry.weeks if w.days_logged == 0)
            assert empty.avg is None and empty.adherence == 0.0
            # the current partial week: averages over the days logged so far
            recent = {today, today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=4)}
            this_week = ry.weeks[-1]
            in_week = [d for d in recent if d >= this_week.week_start]
            assert this_week.days_logged == len(in_week)
            assert ry.streak == 3

            # streak stays alive when today has nothing yet (counts back from yesterday)
            await conn.execute(
                "DELETE FROM entries WHERE food_id = %s AND logged_at >= %s",
                (food, datetime.combine(today, time.min, tzinfo=ZoneInfo(TZ))),
            )
            assert (await get_history(rng="w")).streak == 2
            # …and is 0 once yesterday is empty too
            await conn.execute(
                "DELETE FROM entries WHERE food_id = %s AND logged_at >= %s",
                (food, datetime.combine(today - timedelta(days=1), time.min, tzinfo=ZoneInfo(TZ))),
            )
            assert (await get_history(rng="w")).streak == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. the routes expose what the integrator wires (router objects, query alias)
# ---------------------------------------------------------------------------


def test_routers_and_openapi_shape():
    from fastapi import FastAPI

    from routes import aliases, diary, foods, history

    app = FastAPI()
    for mod in (aliases, diary, foods, history):
        app.include_router(mod.router)
    spec = app.openapi()
    assert "/api/aliases" in spec["paths"] and "/api/aliases/{alias_id}" in spec["paths"]
    assert set(spec["paths"]["/api/aliases/{alias_id}"]) == {"patch", "delete"}
    assert "/api/foods/{food_id}" in spec["paths"]
    params = spec["paths"]["/api/history"]["get"]["parameters"]
    assert [p["name"] for p in params] == ["range"]
    assert params[0]["schema"]["enum"] == ["3d", "w", "m", "y"]
    assert "FoodDetail" in spec["components"]["schemas"]
    assert "HistoryResponse" in spec["components"]["schemas"]
