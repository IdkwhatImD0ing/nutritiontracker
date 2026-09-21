"""M4 end-to-end (integrator) — the photo lane + the two quick-add lanes
(DESIGN.md §3.4b, §5, §7.4, §12 M4).

The M4 exit gate — "a photographed plate stages with resolved staples and
flagged estimates" — walked through the wired routes with vision MOCKED
(no model spend) inside the force-rollback harness (no rows left behind):

  1. POST /api/logs/photo, vision stubbed to "grilled chicken breast ~150 g,
     white rice ~200 g, broccoli ~80 g" → 3 rows; chicken and rice (and
     broccoli) resolve to DATABASE foods with cache macros scaled to the
     photo's portion, flagged estimated="portion". Wall time is printed.
  2. Commit takes the flagged rows straight (2026-09-09: the commit is the
     confirmation, no per-row tap) with snapshot macros, and writes ZERO
     aliases and ZERO portion defaults (logs.input_mode='photo' → no
     learning). The STORED flag is what the committed payload records.
  3. The §3.4b seam: a tier-3 not_found (run_tier3 stubbed) on a photo row
     comes back from the kick route as the numberless source='estimate' row,
     which still cannot commit — it has no macros.
  4. Barcode → from-food (fdc.lookup_barcode stubbed) and search → from-food
     each stage ONE resolved row and commit with no learning.
  5. main.app carries the routes, barcode ahead of /api/foods/{id}.

Run:  uv run --project backend --with pytest pytest backend/tests/test_e2e_m4.py -q -s
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from tracing_stub import make_tracing_stub
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
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
from starlette.datastructures import Headers, UploadFile

import services.vision as vision_real  # the real schema classes build the fixture

# ---------------------------------------------------------------------------
# stubs — tracing / services.vision (as test_photo.py); everything else REAL
# ---------------------------------------------------------------------------

_vision: dict = {"fn": None}


async def _vision_dispatch(image, mime, **kw):
    fn = _vision["fn"]
    if fn is None:
        raise AssertionError("vision must not be called by this test")
    return await fn(image, mime)


@pytest.fixture(scope="module", autouse=True)
def _stubs():
    tracing_mod = make_tracing_stub("trace-e2e-m4")

    vision_mod = types.ModuleType("services.vision")
    vision_mod.analyze = _vision_dispatch
    vision_mod.MODEL = vision_real.MODEL

    stubs = {"tracing": tracing_mod, "services.vision": vision_mod}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    import services

    saved_attr = getattr(services, "vision", None)
    services.vision = vision_mod
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        services.vision = saved_attr


@pytest.fixture(autouse=True)
def _reset_vision():
    _vision["fn"] = None
    yield
    _vision["fn"] = None


import db  # noqa: E402


from tenancy_harness import bind_test_tenant, override_user  # noqa: E402
import main  # noqa: E402
from models import CommitRequest, Meal, StagedItem, Tier3KickRequest  # noqa: E402
from resolver import cascade, foods_repo  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes import photo, quick  # noqa: E402
from routes import resolve as resolve_routes  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from sources import fdc  # noqa: E402

TZ = "America/Los_Angeles"
CAPTURED = datetime(2000, 1, 5, 20, 15, tzinfo=timezone.utc)  # 12:15 PT → lunch
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64

# USDA-shaped staples: a 100 g serving (the photo's est_grams maps onto it)
# plus a household one, so the count/gram mapping has both to choose from.
CHICKEN = [
    {"serving_id": "100g", "description": "100 g", "grams": 100,
     "macros": {"kcal": 165, "protein_g": 31, "carb_g": 0, "fat_g": 3.6}},
    {"serving_id": "breast", "description": "1 breast (174 g)", "grams": 174,
     "macros": {"kcal": 287, "protein_g": 53.9, "carb_g": 0, "fat_g": 6.3}},
]
RICE = [
    {"serving_id": "100g", "description": "100 g", "grams": 100,
     "macros": {"kcal": 130, "protein_g": 2.7, "carb_g": 28, "fat_g": 0.3}},
    {"serving_id": "cup", "description": "1 cup (158 g)", "grams": 158,
     "macros": {"kcal": 205, "protein_g": 4.3, "carb_g": 44.5, "fat_g": 0.4}},
]
BROCCOLI = [
    {"serving_id": "100g", "description": "100 g", "grams": 100,
     "macros": {"kcal": 35, "protein_g": 2.4, "carb_g": 7.2, "fat_g": 0.4}},
]
PLATE = [  # what vision "saw" — phrase, est_grams, note
    ("grilled chicken breast", 150, "a grilled chicken breast"),
    ("white rice", 200, "a mound of white rice"),
    ("broccoli", 80, "steamed broccoli florets"),
]
BAR = {
    "serving_id": "label",
    "description": "1 bar (60 g)",
    "grams": 60.0,
    "macros": {"kcal": 200.0, "protein_g": 21.0, "carb_g": 22.0, "fat_g": 8.0},
}

timings: dict[str, int] = {}


# ---------------------------------------------------------------------------
# rollback harness (as test_photo.py / test_quick.py)
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


async def insert_food(conn, name, servings):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, food_type,
                           servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, 'generic', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (f"e2e-m4-{uuid4().hex[:12]}", name, name, Json(servings), servings[0]["serving_id"]),
    )
    return str((await cur.fetchone())["id"])


async def seed_alias(conn, phrase, food_id):
    """Tier 1 for the fixture phrase. Production carried no alias for any of
    the three (checked 2026-09-02); if one ever exists the plate still
    resolves (to it) and every assertion below reads the food row it landed
    on, so nothing here depends on which food won."""
    hit = await db.fetchone(
        "SELECT 1 AS hit FROM aliases WHERE alias_norm = %s LIMIT 1", (norm(phrase),)
    )
    if hit is None:
        await conn.execute(
            "INSERT INTO aliases (alias_norm, food_id, origin) VALUES (%s, %s, 'manual')",
            (norm(phrase), food_id),
        )


async def alias_snapshot(phrases):
    """(count, use_count, default_serving_id, default_qty, last_used_at) per
    phrase — the learning writes commit must NOT make on a photo log."""
    out = {}
    for p in phrases:
        rows = await db.fetchall(
            "SELECT use_count, default_serving_id, default_qty, last_used_at FROM aliases "
            "WHERE alias_norm = %s ORDER BY id",
            (norm(p),),
        )
        out[p] = [tuple(r.values()) for r in rows]
    return out


def vitem(phrase, *, qty=None, unit=None, est_grams=None, confidence="high", note=""):
    return vision_real.VisionItem(
        phrase=phrase, qty=qty, unit=unit, est_grams=est_grams,
        confidence=confidence, note=note or phrase, menu=None,
    )


def install_vision(items, meal_hint=None):
    async def fake(image, mime, **kw):
        assert mime == "image/png"
        return vision_real.VisionResult(items=items, meal_hint=meal_hint)

    _vision["fn"] = fake


async def post_photo(log_id):
    return await photo.create_log_photo(
        image=UploadFile(
            file=BytesIO(PNG), filename="plate.png", headers=Headers({"content-type": "image/png"})
        ),
        log_id=log_id, captured_at=CAPTURED, meal_hint=None, tz=TZ,
    )


async def food_serving(food_id, serving_id):
    row = await db.fetchone("SELECT servings FROM foods WHERE id = %s", (food_id,))
    return cascade._serving_by_id(row["servings"], serving_id)


# ---------------------------------------------------------------------------
# 1 + 2. the plate: stages flagged, commits on the first press, learns nothing
# ---------------------------------------------------------------------------


def test_photographed_plate_stages_flagged_then_commits_and_learns_nothing():
    async def run():
        async with isolated_db() as conn:
            chicken = await insert_food(conn, "grilled chicken breast", CHICKEN)
            rice = await insert_food(conn, "white rice", RICE)
            broccoli = await insert_food(conn, "broccoli", BROCCOLI)
            for phrase, fid in (("grilled chicken breast", chicken), ("white rice", rice),
                                ("broccoli", broccoli)):
                await seed_alias(conn, phrase, fid)
            phrases = [p for p, _, _ in PLATE]
            before = await alias_snapshot(phrases)
            install_vision([vitem(p, est_grams=g, note=n) for p, g, n in PLATE])

            log_id = uuid4()
            t0 = time.perf_counter()
            resp = await post_photo(log_id)
            timings["photo_pipeline_ms"] = int((time.perf_counter() - t0) * 1000)

            # --- 1. three rows, staples resolved with DATABASE macros, flagged
            assert resp.log_id == log_id and resp.meal == Meal.lunch and resp.note is None
            assert [i.item_index for i in resp.items] == [0, 1, 2]
            log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
            assert log["input_mode"] == "photo" and log["status"] == "staged"
            assert log["raw_input"] == (
                "photo: grilled chicken breast ~150 g, white rice ~200 g, broccoli ~80 g"
            )
            for item, (phrase, grams, note) in zip(resp.items, PLATE):
                assert item.status == "resolved" and item.food is not None, item
                assert item.tier in (1, 2)  # a database hit, never tier 3 / estimate
                assert item.estimated == "portion"
                assert item.estimate_note == f"Photo: {note} (~{grams} g)"
                # the photo's grams on the food's own gram serving; macros = cache × qty
                serving = await food_serving(item.food.id, item.serving.id)
                assert serving is not None and serving["grams"]
                assert item.qty == round(grams / serving["grams"], 4)
                assert item.macros.model_dump(exclude_none=True) == cascade._scale_macros(
                    serving["macros"], item.qty
                )
                row = await db.fetchone(
                    "SELECT staged FROM item_resolutions WHERE log_id = %s AND item_index = %s",
                    (log_id, item.item_index),
                )
                assert row["staged"]["estimated"] == "portion"
                assert row["staged"]["vision"]["est_grams"] == grams
                assert row["staged"]["vision"]["model"] == vision_real.MODEL
            chicken_row, rice_row = resp.items[0], resp.items[1]
            assert chicken_row.food.id == chicken and chicken_row.macros.kcal == 247.5
            assert rice_row.food.id == rice and rice_row.macros.kcal == 260
            assert resp.items[2].macros.kcal == 28

            # --- 2. the flagged rows commit on the first press
            assert all(i.estimated == "portion" for i in resp.items)
            totals = await commit_route(log_id, CommitRequest(items=resp.items))
            assert totals.errors == []
            entries = await db.fetchall(
                "SELECT item_index, food_id, serving_id, qty, macros, alias_id FROM entries "
                "WHERE log_id = %s ORDER BY item_index",
                (log_id,),
            )
            assert [(str(e["food_id"]), e["serving_id"], float(e["qty"])) for e in entries] == [
                (chicken, "100g", 1.5), (rice, "100g", 2.0), (broccoli, "100g", 0.8),
            ]
            for e, item in zip(entries, resp.items):  # snapshot = the staged cache numbers
                assert {k: float(v) for k, v in e["macros"].items() if k in cascade._CORE_MACROS} == {
                    k: float(v) for k, v in item.macros.model_dump(exclude_none=True).items()
                }
            assert [e["macros"]["kcal"] for e in entries] == [247.5, 260, 28]

            # ZERO aliases learned, ZERO portion defaults recomputed, no alias_id
            assert await alias_snapshot(phrases) == before
            assert all(e["alias_id"] is None for e in entries)
            rows = await db.fetchall(
                "SELECT alias_id, committed_at, staged FROM item_resolutions "
                "WHERE log_id = %s ORDER BY item_index",
                (log_id,),
            )
            assert [r["alias_id"] for r in rows] == [None] * 3
            assert all(r["committed_at"] is not None for r in rows)
            assert all(r["staged"]["estimated"] == "portion" for r in rows)
            assert (await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,)))[
                "status"
            ] == "committed"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. the §3.4b seam: tier-3 not_found on a photo row → numberless estimate row
# ---------------------------------------------------------------------------


def test_tier3_not_found_on_a_photo_row_becomes_a_numberless_estimate_row(monkeypatch):
    async def run():
        async with isolated_db():
            tag = uuid4().hex[:6]
            phrase = f"mystery sauce {tag}"
            install_vision([vitem(phrase, est_grams=30, confidence="low", note="a brown sauce")])
            log_id = uuid4()
            resp = await post_photo(log_id)
            assert resp.items[0].status == "searching" and resp.items[0].estimated == "portion"

            async def not_found(log_id_, item_index, refinement=None):
                return StagedItem(
                    item_index=item_index, phrase=phrase, status="not_found", tier=3,
                    meal=Meal.lunch, confidence="low",
                )

            monkeypatch.setattr(resolve_routes.tier3, "run_tier3", not_found)
            item = await resolve_routes.kick_tier3(
                Tier3KickRequest(log_id=log_id, item_index=0)
            )
            assert item.status == "resolved" and item.estimated == "unresolved"
            assert item.macros is None and item.search_instead is True
            assert item.serving.description == "~30 g (photo estimate)"
            food = await db.fetchone("SELECT * FROM foods WHERE id = %s", (item.food.id,))
            assert food["source"] == "estimate" and food["source_id"] == f"{log_id}:0"
            assert food["provenance"]["vision"]["note"] == "a brown sauce"
            assert food["provenance"]["log_id"] == str(log_id)
            assert food["servings"][0]["macros"] == {}  # no numbers exist for this row
            row = await db.fetchone(
                "SELECT status, staged FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )
            assert row["status"] == "resolved" and row["staged"]["estimated"] == "unresolved"
            # still cannot commit — it has no macros, so the core-macros
            # guard refuses the row and search is its way out
            totals = await commit_route(log_id, CommitRequest(items=[item]))
            assert [e.item_index for e in totals.errors] == [0]
            assert await db.fetchall("SELECT 1 FROM entries WHERE log_id = %s", (log_id,)) == []

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. barcode → from-food, search → from-food: one resolved row, no learning
# ---------------------------------------------------------------------------


def _fake_food(display_name: str) -> dict:
    return {
        "source": "fdc", "source_id": f"e2e-m4-{uuid4().hex[:12]}",
        "name": display_name.upper(), "display_name": display_name, "brand": "TestBrand",
        "food_type": "branded", "state": None, "state_sensitive": False,
        "servings": [BAR], "default_serving_id": "label", "per_100g": None,
        "payload": {"test": True},
    }


async def _commit_one_row_learns_nothing(resp, expected_mode):
    assert len(resp.items) == 1
    item = resp.items[0]
    assert item.status == "resolved" and item.tier is None and item.estimated == "none"
    assert item.serving.id == "label" and item.qty == 1.0 and item.macros.kcal == 200.0
    log = await db.fetchone("SELECT input_mode, raw_input FROM logs WHERE id = %s", (resp.log_id,))
    assert log["input_mode"] == expected_mode and log["raw_input"] == item.food.name
    totals = await commit_route(resp.log_id, CommitRequest(items=resp.items))
    assert totals.errors == []
    entry = await db.fetchone(
        "SELECT food_id, serving_id, qty, macros, alias_id FROM entries WHERE log_id = %s",
        (resp.log_id,),
    )
    assert str(entry["food_id"]) == item.food.id and entry["serving_id"] == "label"
    assert float(entry["qty"]) == 1.0 and entry["macros"]["kcal"] == 200.0
    assert entry["alias_id"] is None
    assert await db.fetchone(
        "SELECT 1 AS hit FROM aliases WHERE alias_norm = %s", (norm(item.food.name),)
    ) is None


def test_barcode_and_search_lanes_stage_one_row_and_commit_without_learning(monkeypatch):
    async def run():
        async with isolated_db():
            # barcode: the §3.4 ladder is stubbed (no network); the hit is cached
            scanned = _fake_food(f"zz e2e bar {uuid4().hex[:8]}")

            async def fake_lookup(code):
                assert code == "049000050104"
                return scanned

            monkeypatch.setattr(fdc, "lookup_barcode", fake_lookup)
            hit = await quick.lookup_barcode(code="0 49000 05010 4")
            assert hit.found is True and hit.food.display_name == scanned["display_name"]
            t0 = time.perf_counter()
            resp = await quick.create_log_from_food(
                quick.FromFoodRequest(
                    id=uuid4(), food_id=hit.food.id, input_mode="barcode",
                    captured_at=datetime.now(timezone.utc),
                )
            )
            timings["from_food_ms"] = int((time.perf_counter() - t0) * 1000)
            await _commit_one_row_learns_nothing(resp, "barcode")

            # search: the pick is already a cached foods row (POST /api/foods/select
            # is the FDC network step the Search sheet runs first — persisted directly here)
            picked = await foods_repo.persist_food(_fake_food(f"zz e2e oats {uuid4().hex[:8]}"))
            resp = await quick.create_log_from_food(
                quick.FromFoodRequest(
                    id=uuid4(), food_id=picked, input_mode="search",
                    captured_at=datetime.now(timezone.utc),
                )
            )
            await _commit_one_row_learns_nothing(resp, "search")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. the assembled app
# ---------------------------------------------------------------------------


def test_main_app_carries_the_m4_routes_in_a_safe_order(monkeypatch):
    from fastapi.testclient import TestClient

    import auth

    paths = main.app.openapi()["paths"]
    for p in ("/api/logs/photo", "/api/foods/barcode", "/api/logs/from-food"):
        assert p in paths, p
    assert "/api/resolve/confirm-estimate" not in paths  # gone 2026-09-09
    # Through the assembled app (auth overridden, lifespan's tracing stubbed):
    # every request below fails validation BEFORE any DB or model spend, and
    # the 422 shapes prove which route answered — quick's barcode route
    # (string detail) must claim the path ahead of GET /api/foods/{food_id},
    # whose UUID parse error would be a list.
    monkeypatch.setattr(main, "tracing", sys.modules["tracing"])
    main.app.dependency_overrides[auth.require_user] = override_user(email="e2e")
    try:
        with TestClient(main.app) as client:
            r = client.get("/api/foods/barcode", params={"code": "no digits"})
            assert r.status_code == 422 and r.json()["detail"] == "barcode has no digits"
            r = client.post("/api/logs/photo", data={"id": str(uuid4())})  # no image part
            assert r.status_code == 422
            r = client.post("/api/logs/from-food", json={"id": str(uuid4())})
            assert r.status_code == 422
    finally:
        main.app.dependency_overrides.pop(auth.require_user, None)
    print(f"\n[e2e m4] timings: {timings}")
