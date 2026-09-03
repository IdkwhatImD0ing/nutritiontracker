"""M4 photo-lane tests (B1) — DESIGN.md §3.4a, §12 M4; contracts C1–C4.

1. POST /api/logs/photo: vision (stubbed) → the same cascade → staged rows.
   Staples resolve with DATABASE macros and the photo's portion applied,
   flagged estimated="portion" + note; an unmatched item stays "searching"
   with the estimate in spoken_qty/unit for tier 3; a row vision could not
   size keeps the resolver's usual portion, unflagged; raw vision output
   lands in staged->'vision'.
2. Commit: 409 "confirm the estimated rows first" while any estimated row is
   unconfirmed — the DB row is the authority, a client-forged
   estimate_confirmed changes nothing — then commits after
   /api/resolve/confirm-estimate; photo rows write NO aliases and NO
   portion defaults; the late commit-item path has the same gate.
3. The unresolvable item: a tier-3 not_found rewrite keeps the flag
   (KEEP_LOG_KEYS_SQL); stage_estimate_food() persists a source='estimate'
   foods row with provenance and NO macros, flagged "unresolved", which
   neither confirms nor commits; a search pick swaps a database food in and
   the flag downgrades to "portion".
4. Upload validation through the HTTP route: content type, magic bytes
   (HEIC named), the 12 MB cap — all before any DB or model spend.
5. One live vision call on a CC0 photo (marked live).

Rollback harness + run-time stubs as in test_phase4_fixes.py: tracing and
services.vision are stubbed (the vision stub RAISES unless a test installs
an output); services.tier3 and the resolver are the REAL modules.

Run:  uv run --project backend --with pytest pytest backend/tests/test_photo.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

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

# The real vision module, loaded under its own name so the sys.modules stub
# below never shadows it: its schema classes build the fixtures, and the live
# test calls its analyze().
_spec = importlib.util.spec_from_file_location("tally_vision_real", BACKEND / "services" / "vision.py")
VREAL = importlib.util.module_from_spec(_spec)
sys.modules["tally_vision_real"] = VREAL  # pydantic resolves the postponed annotations here
_spec.loader.exec_module(VREAL)

# ---------------------------------------------------------------------------
# stubs — tracing / services.vision; services.tier3 + the resolver stay REAL
# ---------------------------------------------------------------------------

_vision: dict = {"fn": None}


async def _vision_dispatch(image, mime):
    fn = _vision["fn"]
    if fn is None:
        raise AssertionError("vision must not be called by this test")
    return await fn(image, mime)


def _build_stubs() -> dict:
    tracing_mod = types.ModuleType("tracing")

    class _Gen:
        trace_id = "trace-test"

        def update(self, **kw):
            pass

    @asynccontextmanager
    async def generation(name):
        yield _Gen()

    tracing_mod.init_tracing = lambda: None
    tracing_mod.generation = generation
    tracing_mod.flush = lambda: None
    tracing_mod.score_commit = lambda *a, **kw: None

    vision_mod = types.ModuleType("services.vision")
    vision_mod.analyze = _vision_dispatch
    vision_mod.MODEL = VREAL.MODEL
    return {"tracing": tracing_mod, "services.vision": vision_mod}


@pytest.fixture(scope="module", autouse=True)
def _workstream_stubs():
    stubs = _build_stubs()
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    import services

    saved_attr = getattr(services, "vision", None)
    services.vision = stubs["services.vision"]
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        if saved_attr is None:
            delattr(services, "vision")
        else:
            services.vision = saved_attr


@pytest.fixture(autouse=True)
def _reset_vision():
    _vision["fn"] = None
    yield
    _vision["fn"] = None


import db  # noqa: E402
import services.tier3 as tier3  # noqa: E402
from models import CommitRequest, Meal, StagedItem  # noqa: E402
from routes import photo  # noqa: E402
from routes import resolve as resolve_routes  # noqa: E402
from routes.commit import CommitItemRequest  # noqa: E402
from routes.commit import commit_item as commit_item_route  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from routes.photo import ConfirmEstimateRequest, confirm_estimate  # noqa: E402

TZ = "America/Los_Angeles"
CAPTURED = datetime(2000, 1, 5, 20, 15, tzinfo=timezone.utc)  # 12:15 PT → lunch

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 64
HEIC = b"\0\0\0\x18ftypheic" + b"\0" * 64

EGG = {
    "serving_id": "srv-large",
    "description": "1 large",
    "grams": 50,
    "macros": {"kcal": 78, "protein_g": 6, "carb_g": 0.6, "fat_g": 5},
}
RICE_100 = {
    "serving_id": "srv-100g",
    "description": "100 g",
    "grams": 100,
    "macros": {"kcal": 130, "protein_g": 2.7, "carb_g": 28, "fat_g": 0.3},
}
RICE_CUP = {
    "serving_id": "srv-cup",
    "description": "1 cup",
    "grams": 158,
    "macros": {"kcal": 205, "protein_g": 4.3, "carb_g": 44.5, "fat_g": 0.4},
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
        (uuid4().hex, name, name, Json(servings), servings[0]["serving_id"]),
    )
    return str((await cur.fetchone())["id"])


async def insert_alias(conn, alias_norm, food_id):
    await conn.execute(
        "INSERT INTO aliases (alias_norm, food_id, origin) VALUES (%s, %s, 'manual')",
        (alias_norm, food_id),
    )


def vitem(phrase, *, qty=None, unit=None, est_grams=None, confidence="high", note=""):
    return VREAL.VisionItem(
        phrase=phrase, qty=qty, unit=unit, est_grams=est_grams,
        confidence=confidence, note=note or phrase,
    )


def install_vision(items, meal_hint=None):
    async def fake(image, mime):
        assert mime in ("image/png", "image/jpeg", "image/webp")
        return VREAL.VisionResult(items=items, meal_hint=meal_hint)

    _vision["fn"] = fake


async def post_photo(log_id, *, data=PNG, mime="image/png", meal_hint=None, tz=TZ):
    return await photo.create_log_photo(
        image=UploadFile(
            file=BytesIO(data), filename="plate.png",
            headers=Headers({"content-type": mime}),
        ),
        log_id=log_id, captured_at=CAPTURED, meal_hint=meal_hint, tz=tz,
    )


async def item_row(log_id, idx):
    return await db.fetchone(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s",
        (log_id, idx),
    )


async def alias_state(alias_norm):
    return await db.fetchone(
        "SELECT use_count, default_serving_id, default_qty FROM aliases WHERE alias_norm = %s",
        (alias_norm,),
    )


async def confirm(log_id, idx):
    return await confirm_estimate(ConfirmEstimateRequest(log_id=log_id, item_index=idx))


async def plate(conn):
    """eggs ×2 (count), rice ~150 g, an unknown sauce ~30 g, coffee (no
    portion) — eggs/rice/coffee are aliased staples. Returns a namespace."""
    tag = uuid4().hex[:6]
    eggs = await insert_food(conn, f"egg {tag}", [EGG])
    rice = await insert_food(conn, f"rice {tag}", [RICE_100, RICE_CUP])
    coffee = await insert_food(conn, f"coffee {tag}", [CUP])
    await insert_alias(conn, f"eggs {tag}", eggs)
    await insert_alias(conn, f"rice {tag}", rice)
    await insert_alias(conn, f"coffee {tag}", coffee)
    install_vision([
        vitem(f"eggs {tag}", qty=2, unit="egg", est_grams=100, note="two fried eggs"),
        vitem(f"rice {tag}", est_grams=150, confidence="medium", note="a scoop of white rice"),
        vitem(f"mystery sauce {tag}", est_grams=30, confidence="low", note="a brown sauce"),
        vitem(f"coffee {tag}", note="black coffee"),
    ])
    log_id = uuid4()
    resp = await post_photo(log_id)
    return types.SimpleNamespace(
        tag=tag, eggs=eggs, rice=rice, coffee=coffee, log_id=log_id, resp=resp,
        items=list(resp.items),
    )


# ---------------------------------------------------------------------------
# 0. pure helpers
# ---------------------------------------------------------------------------


def test_model_pin_and_sniff():
    assert VREAL.MODEL == "gpt-5.6-luna"  # a pinned tier, never bare gpt-5.6
    assert photo.sniff_image(JPEG) == "image/jpeg"
    assert photo.sniff_image(PNG) == "image/png"
    assert photo.sniff_image(WEBP) == "image/webp"
    assert photo.sniff_image(HEIC) == "image/heic"
    assert photo.sniff_image(b"hello world, not an image") is None
    assert photo.sniff_image(b"") is None


def test_vision_item_rides_the_spoken_quantity_channel():
    count = photo._parsed_item(vitem("eggs", qty=2, unit="egg", est_grams=100))
    assert (count.qty, count.unit) == (2.0, None)  # count wins; the piece word is dropped
    grams = photo._parsed_item(vitem("rice", est_grams=150))
    assert (grams.qty, grams.unit) == (150.0, "g")
    none = photo._parsed_item(vitem("coffee"))
    assert (none.qty, none.unit) == (None, None)
    zero = photo._parsed_item(vitem("x", qty=0, est_grams=0))
    assert (zero.qty, zero.unit) == (None, None)
    assert all(p.phrase_kind == "food" and p.library_match is None for p in (count, grams, none))


def test_router_shape():
    assert [r.path for r in photo.router.routes] == [
        "/api/logs/photo", "/api/resolve/confirm-estimate",
    ]
    from auth import require_user

    assert any(d.dependency is require_user for d in photo.router.dependencies)


# ---------------------------------------------------------------------------
# 1. the pipeline: staples resolve with DB macros, portions flagged
# ---------------------------------------------------------------------------


def test_photo_stages_staples_with_db_macros_and_flagged_portions():
    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            assert p.resp.meal == Meal.lunch and p.resp.note is None
            assert [i.item_index for i in p.items] == [0, 1, 2, 3]
            assert all(i.meal == Meal.lunch for i in p.items)

            log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (p.log_id,))
            assert log["input_mode"] == "photo" and log["status"] == "staged"
            assert log["short_circuit"] is False and log["decompose_ms"] is not None
            assert log["trace_id"] == "trace-test"
            assert log["raw_input"] == (
                f"photo: 2 eggs {p.tag}, rice {p.tag} ~150 g, "
                f"mystery sauce {p.tag} ~30 g, coffee {p.tag}"
            )

            # eggs: tier 1, the count on the household serving, DB macros
            eggs = p.items[0]
            assert eggs.status == "resolved" and eggs.tier == 1
            assert eggs.food.id == p.eggs and eggs.serving.id == "srv-large"
            assert eggs.qty == 2 and eggs.macros.kcal == 156 and eggs.confidence == "high"
            assert eggs.estimated == "portion" and eggs.estimate_confirmed is None
            assert eggs.estimate_note == "Photo: two fried eggs (~100 g)"

            # rice: est_grams onto the 100 g serving, macros scaled from the cache
            rice = p.items[1]
            assert rice.status == "resolved" and rice.food.id == p.rice
            assert rice.serving.id == "srv-100g" and rice.qty == 1.5
            assert rice.macros.kcal == 195 and rice.macros.carb_g == 42
            assert rice.estimated == "portion"
            assert rice.estimate_note == "Photo: a scoop of white rice (~150 g)"
            # §3.4b: vision's identification confidence caps the row's — a tier-1
            # hit is "high" on its own; vision's "medium" caps it (fix-pass P7)
            assert rice.confidence == "medium"

            # the unknown: searching for tier 3, the estimate riding along
            sauce = p.items[2]
            assert sauce.status == "searching" and sauce.food is None
            assert sauce.estimated == "portion" and "brown sauce" in sauce.estimate_note
            row2 = await item_row(p.log_id, 2)
            assert row2["status"] == "searching"
            assert row2["staged"]["spoken_qty"] == 30 and row2["staged"]["spoken_unit"] == "g"

            # coffee: vision saw it but could not size it → the usual portion, unflagged
            coffee = p.items[3]
            assert coffee.status == "resolved" and coffee.food.id == p.coffee
            assert coffee.qty == 1 and coffee.serving.id == "srv-cup"
            assert coffee.estimated == "none" and coffee.estimate_note is None

            # the raw vision output rides every row (eval loop), never the image
            for idx, expect in ((0, 100), (1, 150), (2, 30), (3, None)):
                row = await item_row(p.log_id, idx)
                seen = row["staged"]["vision"]
                assert seen["est_grams"] == expect and seen["model"] == "gpt-5.6-luna"
                assert row["staged"]["estimated"] == ("none" if idx == 3 else "portion")
                assert row["alias_id"] is not None if idx != 2 else row["alias_id"] is None
            assert set(log.keys()).isdisjoint({"image", "photo"})  # no bytes column, nothing stored

            # form meal_hint beats the clock; vision's hint beats the clock too
            install_vision([vitem(f"coffee {p.tag}")])
            dinner = await post_photo(uuid4(), meal_hint=Meal.dinner)
            assert dinner.meal == Meal.dinner and dinner.items[0].meal == Meal.dinner
            install_vision([vitem(f"coffee {p.tag}")], meal_hint=Meal.breakfast)
            assert (await post_photo(uuid4())).meal == Meal.breakfast
            # the cap applies to an unsized (unflagged) row too, at every level
            for level in ("medium", "low"):
                install_vision([vitem(f"coffee {p.tag}", confidence=level)])
                capped = (await post_photo(uuid4())).items[0]
                assert capped.estimated == "none" and capped.confidence == level

            # nothing recognized → an honest note, never a silently empty list
            install_vision([])
            empty_id = uuid4()
            empty = await post_photo(empty_id)
            assert empty.items == [] and empty.note and "No food recognized" in empty.note
            assert (await db.fetchone("SELECT raw_input FROM logs WHERE id = %s", (empty_id,)))[
                "raw_input"
            ] == "photo: nothing recognized"

            # idempotent replay of the same client id returns the stored state
            _vision["fn"] = None  # would raise if the pipeline re-ran
            again = await post_photo(p.log_id)
            assert again.log_id == p.log_id and [i.phrase for i in again.items] == [
                i.phrase for i in p.items
            ]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. commit: the confirm gate, then commit; photo rows learn nothing
# ---------------------------------------------------------------------------


def test_commit_refuses_unconfirmed_estimates_then_commits_and_learns_nothing():
    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            eggs, rice, sauce, coffee = p.items
            before = {k: await alias_state(f"{k} {p.tag}") for k in ("eggs", "rice", "coffee")}

            with pytest.raises(HTTPException) as ei:
                await commit_route(p.log_id, CommitRequest(items=[eggs, rice, sauce, coffee]))
            assert ei.value.status_code == 409
            assert ei.value.detail == "confirm the estimated rows first: [0, 1]"
            # the CAS rolled back with the refusal — nothing committed, nothing written
            assert (await db.fetchone("SELECT status FROM logs WHERE id = %s", (p.log_id,)))[
                "status"
            ] == "staged"
            assert await db.fetchall("SELECT 1 FROM entries WHERE log_id = %s", (p.log_id,)) == []

            # a client-forged confirmation changes nothing: the stored row decides
            forged = [i.model_copy(update={"estimate_confirmed": True}) for i in (eggs, rice)]
            with pytest.raises(HTTPException) as ei:
                await commit_route(p.log_id, CommitRequest(items=forged + [sauce, coffee]))
            assert ei.value.detail == "confirm the estimated rows first: [0, 1]"

            # confirm one → the other still blocks
            ok = await confirm(p.log_id, 0)
            assert ok.estimate_confirmed is True and ok.estimated == "portion"
            assert ok.qty == 2 and ok.macros.kcal == 156
            assert (await item_row(p.log_id, 0))["staged"]["estimate_confirmed"] is True
            with pytest.raises(HTTPException) as ei:
                await commit_route(p.log_id, CommitRequest(items=[eggs, rice, sauce, coffee]))
            assert ei.value.detail == "confirm the estimated rows first: [1]"
            # an unflagged row has nothing to confirm
            with pytest.raises(HTTPException) as ei:
                await confirm(p.log_id, 3)
            assert ei.value.status_code == 409 and "no estimate" in ei.value.detail

            await confirm(p.log_id, 1)
            totals = await commit_route(p.log_id, CommitRequest(items=[eggs, rice, sauce, coffee]))
            assert totals.errors == []
            entries = await db.fetchall(
                "SELECT item_index, food_id, serving_id, qty, macros, alias_id FROM entries "
                "WHERE log_id = %s ORDER BY item_index",
                (p.log_id,),
            )
            assert [(str(e["food_id"]), e["serving_id"], float(e["qty"])) for e in entries] == [
                (p.eggs, "srv-large", 2.0), (p.rice, "srv-100g", 1.5), (p.coffee, "srv-cup", 1.0),
            ]
            assert [e["macros"]["kcal"] for e in entries] == [156, 195, 2]  # DB numbers
            assert all(e["alias_id"] is None for e in entries)

            # R3: nothing learned — no alias use, no portion default, no alias_id
            for k, prior in before.items():
                assert await alias_state(f"{k} {p.tag}") == prior
            assert await db.fetchone(
                "SELECT 1 AS hit FROM aliases WHERE alias_norm = %s", (f"mystery sauce {p.tag}",)
            ) is None
            rows = await db.fetchall(
                "SELECT item_index, alias_id, committed_at, staged FROM item_resolutions "
                "WHERE log_id = %s ORDER BY item_index",
                (p.log_id,),
            )
            assert [r["alias_id"] for r in rows] == [None] * 4
            assert [r["committed_at"] is not None for r in rows] == [True, True, False, True]
            # the committed rows record the flag and the stored confirmation
            assert rows[0]["staged"]["estimated"] == "portion"
            assert rows[0]["staged"]["estimate_confirmed"] is True
            assert rows[0]["staged"]["vision"]["note"] == "two fried eggs"
            assert rows[3]["staged"]["estimated"] == "none"

            # idempotent replay, and a confirm on a committed row is refused
            replay = await commit_route(p.log_id, CommitRequest(items=[eggs, rice, sauce, coffee]))
            assert replay.errors == []
            with pytest.raises(HTTPException) as ei:
                await confirm(p.log_id, 0)
            assert ei.value.status_code == 409 and "committed" in ei.value.detail

            # the late commit-item path: same gate, same confirm
            late = await tier3.resolve_item_from_food(str(p.log_id), 2, p.rice)
            assert late.status == "resolved" and late.serving.id == "srv-100g"
            assert late.qty == 0.3  # the ~30 g estimate applied to the picked food
            row2 = await item_row(p.log_id, 2)
            assert row2["staged"]["estimated"] == "portion"  # KEEP carried the flag
            with pytest.raises(HTTPException) as ei:
                await commit_item_route(p.log_id, CommitItemRequest(item_index=2, item=late))
            assert ei.value.status_code == 409
            assert ei.value.detail == "confirm the estimated rows first: [2]"
            await confirm(p.log_id, 2)
            await commit_item_route(p.log_id, CommitItemRequest(item_index=2, item=late))
            e2 = await db.fetchone(
                "SELECT qty, macros, alias_id FROM entries WHERE log_id = %s AND item_index = 2",
                (p.log_id,),
            )
            assert float(e2["qty"]) == 0.3 and e2["macros"]["kcal"] == 39 and e2["alias_id"] is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. the unresolvable item — flag survives tier 3; the estimate row has no numbers
# ---------------------------------------------------------------------------


def test_not_found_photo_row_becomes_a_numberless_estimate_row_that_routes_to_search():
    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            sauce = p.items[2]

            # a tier-3 not_found rewrite (the real write path, with the spoken
            # qty/unit run_tier3 always forwards) keeps the flag
            not_found = StagedItem(
                item_index=2, phrase=sauce.phrase, status="not_found", tier=3,
                meal=Meal.lunch, confidence="low",
            )
            assert await tier3._write_item_row(
                str(p.log_id), 2, not_found, tier=3, spoken_qty=30, spoken_unit="g"
            )
            row = await item_row(p.log_id, 2)
            assert row["status"] == "not_found"
            assert row["staged"]["estimated"] == "portion"
            assert row["staged"]["estimate_note"] == sauce.estimate_note
            assert row["staged"]["vision"]["note"] == "a brown sauce"
            assert "estimate_confirmed" not in row["staged"]

            item = await photo.stage_estimate_food(str(p.log_id), 2)
            assert item is not None and item.status == "resolved"
            assert item.estimated == "unresolved" and item.macros is None
            assert item.search_instead is True and item.confidence == "low"
            assert item.serving.id == "photo" and item.qty == 1
            assert item.serving.description == "~30 g (photo estimate)"
            assert item.servings[0].macros is None
            assert "no database match" in item.estimate_note
            food = await db.fetchone("SELECT * FROM foods WHERE id = %s", (item.food.id,))
            assert food["source"] == "estimate" and food["source_id"] == f"{p.log_id}:2"
            assert food["display_name"] == f"mystery sauce {p.tag}"
            assert food["servings"] == [item.servings[0].model_dump() | {"macros": {}}]
            assert food["provenance"]["vision"]["est_grams"] == 30
            assert food["provenance"]["model"] == "gpt-5.6-luna"
            assert food["provenance"]["captured_at"] == CAPTURED.isoformat()
            row = await item_row(p.log_id, 2)
            assert row["status"] == "resolved"
            assert row["staged"]["estimated"] == "unresolved"  # not downgraded by KEEP on its own write
            # fix-pass E4: KEEP carried the OLD note over the payload's — the
            # response said "no database match" while the row kept "~30 g"
            assert row["staged"]["estimate_note"] == item.estimate_note
            assert row["staged"]["spoken_qty"] == 30 and row["staged"]["spoken_unit"] == "g"
            # fix-pass E3 (R1): the placeholder never surfaces as a search hit
            from routes.foods import _local_hits

            assert all(h.food_id != item.food.id for h in await _local_hits(f"mystery sauce {p.tag}"))

            # no numbers → nothing to confirm; the commit gate blocks it (the
            # gate reads the STORED rows, so the plate's other estimates are
            # confirmed first to isolate this row's refusal)
            with pytest.raises(HTTPException) as ei:
                await confirm(p.log_id, 2)
            assert ei.value.status_code == 409 and "pick a database food" in ei.value.detail
            await confirm(p.log_id, 0)
            await confirm(p.log_id, 1)
            with pytest.raises(HTTPException) as ei:
                await commit_route(p.log_id, CommitRequest(items=[item]))
            assert ei.value.detail == "confirm the estimated rows first: [2]"

            # a search pick swaps a database food in: real numbers, the photo's
            # portion, the flag downgraded to "portion", confirmation required afresh
            picked = await tier3.resolve_item_from_food(str(p.log_id), 2, p.rice)
            assert picked.food.id == p.rice and picked.serving.id == "srv-100g"
            assert picked.qty == 0.3 and picked.macros.kcal == 39
            row = await item_row(p.log_id, 2)
            assert row["staged"]["estimated"] == "portion"
            assert row["staged"]["vision"]["note"] == "a brown sauce"
            assert "estimate_confirmed" not in row["staged"]
            confirmed = await confirm(p.log_id, 2)
            assert confirmed.estimate_confirmed is True and confirmed.estimated == "portion"
            # the estimate food stays a cache row nothing references — never an entry
            assert await db.fetchall(
                "SELECT 1 FROM entries WHERE food_id = %s", (item.food.id,)
            ) == []

            # not a photo row / no longer live → None, nothing written
            text_log = uuid4()
            await conn.execute(
                "INSERT INTO logs (id, input_mode, raw_input, captured_at) "
                "VALUES (%s, 'text', 'x', now())",
                (text_log,),
            )
            await conn.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged)
                VALUES (%s, 0, 'x', 'x', 'not_found', %s)
                """,
                (text_log, Json({"item_index": 0, "phrase": "x", "status": "not_found",
                                 "meal": "snack"})),
            )
            assert await photo.stage_estimate_food(str(text_log), 0) is None
            await conn.execute(
                "UPDATE item_resolutions SET status = 'removed' WHERE log_id = %s AND item_index = 2",
                (p.log_id,),
            )
            assert await photo.stage_estimate_food(str(p.log_id), 2) is None
            assert await photo.stage_estimate_food(str(uuid4()), 0) is None

    asyncio.run(run())


def test_diary_marks_confirmed_photo_portions_estimated():
    """Fix-pass P5 (§3.4a): estimated values render flagged like web-sourced
    foods — the diary entry carries `estimated` ("portion" for a confirmed
    photo portion, read off the entry's item_resolutions row), so a photo
    entry never renders like any other."""
    from routes.diary import DiaryEntry, get_diary, local_date

    assert DiaryEntry.model_fields["estimated"].default == "none"

    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            eggs, rice, sauce, coffee = p.items
            await confirm(p.log_id, 0)
            await confirm(p.log_id, 1)
            totals = await commit_route(p.log_id, CommitRequest(items=[eggs, rice, sauce, coffee]))
            assert totals.errors == []
            diary = await get_diary(date=await local_date(CAPTURED))
            by_food = {
                e.food.id: e for entries in diary.meals.values() for e in entries
                if e.food.id in (p.eggs, p.rice, p.coffee)
            }
            assert set(by_food) == {p.eggs, p.rice, p.coffee}
            assert by_food[p.eggs].estimated == "portion" and by_food[p.eggs].qty == 2
            assert by_food[p.rice].estimated == "portion" and by_food[p.rice].qty == 1.5
            assert by_food[p.coffee].estimated == "none"  # vision could not size it

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3b. Phase-5 fix pass — every resolution response shows the flag the row holds
# ---------------------------------------------------------------------------


def test_resolution_responses_carry_the_stored_estimate_flag(monkeypatch):
    """Fix-pass finding: KEEP_LOG_KEYS_SQL kept estimated='portion' on the row
    while the in-memory StagedItem the write path returned said 'none' — the
    sheet merged it, rendered no confirm chip, and commit answered 409 with
    nothing to tap. The kick, the answer (alternates chip), /api/foods/select
    (resolve_item_from_food/source) and /api/resolve now return the row as
    stored."""
    from models import ResolveRequest, Tier3KickRequest
    from routes.resolve import Answer, AnswerRequest

    # test_phase3_minors re-imports routes.resolve against a tier-3 stub and
    # pops it again, so a run-time import here would bind that stub; pin the
    # real module the answer path calls into.
    monkeypatch.setattr(resolve_routes, "tier3", tier3)

    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            sauce = p.items[2]

            async def as_stored(item):
                row = await item_row(p.log_id, 2)
                assert item.estimated == row["staged"]["estimated"] == "portion"
                assert item.estimate_note == row["staged"]["estimate_note"] == sauce.estimate_note
                assert item.estimate_confirmed is None and "estimate_confirmed" not in row["staged"]

            # a search pick / alternates chip on a cached food
            picked = await tier3.resolve_item_from_food(str(p.log_id), 2, p.rice)
            assert picked.status == "resolved" and picked.food.id == p.rice
            await as_stored(picked)
            # the answer route wraps the same call
            answered = await resolve_routes.answer_question(
                AnswerRequest(log_id=p.log_id, item_index=2, answer=Answer(food_id=p.rice))
            )
            await as_stored(answered)

            # the kick: run_tier3 stubbed to do exactly the real result write
            async def fake_run_tier3(log_id_, item_index, refinement=None):
                _, food = tier3._food_row(await db.fetchone(
                    f"SELECT {tier3._FOOD_COLS} FROM foods WHERE id = %s", (p.rice,)
                ))
                item = tier3._staged_from_food(
                    item_index, sauce.phrase, "lunch", 0.3, food, p.rice,
                    serving_id="srv-100g", confidence="high", alternates=None, question=None,
                )
                stored = await tier3._write_item_row(
                    log_id_, item_index, item, proposed_food_id=p.rice, resolve_ms=1,
                    spoken_qty=30, spoken_unit="g",
                )
                return tier3._stored_item(stored, item)

            monkeypatch.setattr(resolve_routes.tier3, "run_tier3", fake_run_tier3)
            kicked = await resolve_routes.kick_tier3(
                Tier3KickRequest(log_id=p.log_id, item_index=2)
            )
            assert kicked.status == "resolved" and kicked.food.id == p.rice
            await as_stored(kicked)

            # /api/resolve (the edit sheet's rephrase): an exact alias match
            # resolves at tier 1, no model call
            edited = await resolve_routes.resolve_item(
                ResolveRequest(log_id=p.log_id, item_index=2, phrase=f"rice {p.tag}")
            )
            assert edited.status == "resolved" and edited.food.id == p.rice
            await as_stored(edited)

            # a refused write (row removed meanwhile) still answers the payload
            await conn.execute(
                "UPDATE item_resolutions SET status = 'removed' WHERE log_id = %s AND item_index = 2",
                (p.log_id,),
            )
            assert await tier3._write_item_row(str(p.log_id), 2, picked) is None

    asyncio.run(run())


def test_vision_failure_leaves_no_log_so_the_same_id_retries_vision():
    """Fix-pass finding: the log row was inserted before the vision call, so a
    503 left an itemless staged log; PhotoCapture retries under the same
    client id (§5), which _duplicate_post_response replayed as items=[] — read
    by the client as "no food in that photo". The failed attempt now removes
    its row, and the retry runs vision again."""
    from openai import OpenAIError

    async def run():
        async with isolated_db() as conn:
            tag = uuid4().hex[:6]
            coffee = await insert_food(conn, f"coffee {tag}", [CUP])
            await insert_alias(conn, f"coffee {tag}", coffee)

            async def outage(image, mime):
                raise OpenAIError("boom")

            _vision["fn"] = outage
            log_id = uuid4()
            with pytest.raises(HTTPException) as ei:
                await post_photo(log_id)
            assert ei.value.status_code == 503 and "OpenAIError" in ei.value.detail
            assert await db.fetchone("SELECT 1 FROM logs WHERE id = %s", (log_id,)) is None

            install_vision([vitem(f"coffee {tag}", note="black coffee")])
            retry = await post_photo(log_id)  # same id — vision ran again
            assert retry.note is None and [i.food.id for i in retry.items] == [coffee]
            log = await db.fetchone("SELECT status, raw_input FROM logs WHERE id = %s", (log_id,))
            assert log["status"] == "staged" and log["raw_input"] == f"photo: coffee {tag}"

            # any other failure inside the vision span takes the same path
            async def crash(image, mime):
                raise RuntimeError("schema drift")

            _vision["fn"] = crash
            other = uuid4()
            with pytest.raises(RuntimeError):
                await post_photo(other)
            assert await db.fetchone("SELECT 1 FROM logs WHERE id = %s", (other,)) is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. upload validation through the HTTP route — before any DB or model spend
# ---------------------------------------------------------------------------


def _photo_app():
    from fastapi import FastAPI

    from auth import require_user

    app = FastAPI()
    app.include_router(photo.router)
    app.dependency_overrides[require_user] = lambda: {"email": "test", "user_id": "t"}
    return app


def test_upload_validation_rejects_before_any_spend():
    from fastapi.testclient import TestClient

    form = {"id": str(uuid4()), "captured_at": CAPTURED.isoformat(), "tz": TZ}
    _vision["fn"] = None  # any model call would AssertionError → 500
    with TestClient(_photo_app()) as client:
        def post(data, mime):
            return client.post(
                "/api/logs/photo", data=form, files={"image": ("plate", data, mime)}
            )

        r = post(PNG, "text/plain")
        assert r.status_code == 415 and "unsupported image type" in r.json()["detail"]
        r = post(b"hello world, not an image", "image/png")
        assert r.status_code == 415 and "not a JPEG" in r.json()["detail"]
        r = post(HEIC, "image/jpeg")  # declared type lies; the bytes decide
        assert r.status_code == 415 and "HEIC" in r.json()["detail"]
        r = post(b"\xff\xd8\xff" + b"\0" * photo.MAX_IMAGE_BYTES, "image/jpeg")
        assert r.status_code == 413
        r = client.post("/api/logs/photo", data=form)  # no image part at all
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# 5. live — one real vision call on a CC0 photo (spends a few cents)
# ---------------------------------------------------------------------------

# Wikimedia Commons "Скромный завтрак.jpg" (CC0): fried eggs, buttered bread,
# tomato, cucumber. The 960 px thumbnail, ~150 KB.
_PHOTO_URL = (
    "https://thumb.wikimedia.org/wikipedia/commons/thumb/3/3a/"
    "%D0%A1%D0%BA%D1%80%D0%BE%D0%BC%D0%BD%D1%8B%D0%B9_%D0%B7%D0%B0%D0%B2%D1%82%D1%80%D0%B0%D0%BA.jpg/"
    "960px-%D0%A1%D0%BA%D1%80%D0%BE%D0%BC%D0%BD%D1%8B%D0%B9_%D0%B7%D0%B0%D0%B2%D1%82%D1%80%D0%B0%D0%BA.jpg"
)
_PHOTO_PATH = Path(os.environ.get("CLAUDE_SCRATCHPAD_DIR") or tempfile.gettempdir()) / "cc0-breakfast.jpg"
_ITEM_KEYS = {"phrase", "qty", "unit", "est_grams", "confidence", "note"}


def _load_env() -> None:
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@pytest.mark.live
def test_vision_live_identifies_foods_and_never_outputs_nutrition():
    import httpx

    _load_env()
    if not _PHOTO_PATH.is_file():
        resp = httpx.get(
            _PHOTO_URL, follow_redirects=True, timeout=60.0,
            headers={"User-Agent": "TallyTest/0.1 (billzhangsc@gmail.com)"},
        )
        resp.raise_for_status()
        _PHOTO_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PHOTO_PATH.write_bytes(resp.content)
    image = _PHOTO_PATH.read_bytes()
    assert photo.sniff_image(image[:16]) == "image/jpeg"

    t0 = time.perf_counter()
    seen = asyncio.run(VREAL.analyze(image, "image/jpeg"))
    ms = int((time.perf_counter() - t0) * 1000)
    out = seen.model_dump(mode="json")
    print(f"\n[vision live] {VREAL.MODEL} {ms} ms: {out}")

    assert out["items"], out
    assert set(out.keys()) == {"items", "meal_hint"}
    for it in out["items"]:
        assert set(it.keys()) == _ITEM_KEYS  # no numeric nutrition field exists
        assert it["phrase"].strip() and it["note"].strip()
        assert it["confidence"] in ("high", "medium", "low")
    # what the photo shows: eggs on a plate
    assert any("egg" in it["phrase"].lower() for it in out["items"]), out
