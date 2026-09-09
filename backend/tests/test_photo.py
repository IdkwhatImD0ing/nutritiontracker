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
from tracing_stub import make_tracing_stub
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


async def _vision_dispatch(image, mime, **kw):
    fn = _vision["fn"]
    if fn is None:
        raise AssertionError("vision must not be called by this test")
    return await fn(image, mime)


def _build_stubs() -> dict:
    tracing_mod = make_tracing_stub("trace-test")

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


from tenancy_harness import bind_test_tenant, override_user  # noqa: E402
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
        confidence=confidence, note=note or phrase, menu=None,
    )


def install_vision(items, meal_hint=None):
    async def fake(image, mime, **kw):
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
    assert VREAL.MODEL == "gpt-6-astra"  # a pinned full id, never a family alias
    assert photo.sniff_image(JPEG) == "image/jpeg"
    assert photo.sniff_image(PNG) == "image/png"
    assert photo.sniff_image(WEBP) == "image/webp"
    assert photo.sniff_image(HEIC) == "image/heic"
    assert photo.sniff_image(b"hello world, not an image") is None
    assert photo.sniff_image(b"") is None


def test_vision_call_sends_the_pinned_model_and_effort():
    """The model id and the reasoning effort as they actually reach the API.
    `MODEL` is asserted above, but nothing tested the effort until now — it is
    the expensive half of this call (2026-09-08: astra at "medium"), and a
    silent revert to "low" would show up only as worse portions."""
    sent: dict = {}

    class _Parse:
        async def parse(self, **kwargs):
            sent.update(kwargs)
            return types.SimpleNamespace(
                output_parsed=VREAL.VisionResult(items=[], meal_hint=None), usage=None
            )

    VREAL._client = types.SimpleNamespace(responses=_Parse())
    try:
        asyncio.run(VREAL.analyze(JPEG, "image/jpeg"))
    finally:
        VREAL._client = None
    assert sent["model"] == "gpt-6-astra"
    assert sent["reasoning"] == {"effort": "medium"}
    assert sent["text_format"] is VREAL.VisionResult  # strict schema, not free text
    assert sent["prompt_cache_key"] == VREAL.PROMPT_CACHE_KEY
    # the image rides as a data URL of the SNIFFED type, never a file path
    image_part = sent["input"][1]["content"][1]
    assert image_part["image_url"].startswith("data:image/jpeg;base64,")


def test_photo_reply_says_what_it_saw_and_invites_the_correction():
    """The photo turn's reply is built in code from vision's own user-facing
    notes — no model call (§6.4). It must name what was seen, flag a row with
    no database match, and always end by asking for the correction, which is
    the whole point of the lane being a chat."""
    seen = VREAL.VisionResult(
        items=[vitem("eggs", note="two fried eggs"), vitem("rice", note="about a cup of white rice")],
        meal_hint=None,
    )
    plain = photo.photo_reply(seen, [])
    assert "two fried eggs and about a cup of white rice" in plain
    assert plain.endswith("tell me what's off.")

    unresolved = photo.photo_reply(
        seen,
        [
            StagedItem(
                item_index=0, phrase="mystery sauce", status="resolved",
                meal=Meal("lunch"), confidence="low", estimated="unresolved",
            )
        ],
    )
    assert "no database match for mystery sauce" in unresolved

    assert "couldn't find any food" in photo.photo_reply(
        VREAL.VisionResult(items=[], meal_hint=None), []
    )


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
                assert seen["est_grams"] == expect and seen["model"] == VREAL.MODEL
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
            assert food["provenance"]["model"] == VREAL.MODEL
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
    app.dependency_overrides[require_user] = override_user(email="test", user_id="t")
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
# `menu` (2026-09-09) is the ONE place a number may ride, and only the web
# lane fills it: a dish's published label, transcribed, never estimated
_ITEM_KEYS = {"phrase", "qty", "unit", "est_grams", "confidence", "note", "menu"}


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
        assert set(it.keys()) == _ITEM_KEYS  # no numeric field but the menu label
        assert it["phrase"].strip() and it["note"].strip()
        assert it["confidence"] in ("high", "medium", "low")
        # this call has no web tools, so there is nothing to transcribe from
        assert it["menu"] is None, it
    # what the photo shows: eggs on a plate
    assert any("egg" in it["phrase"].lower() for it in out["items"]), out


# ---------------------------------------------------------------------------
# 6. adjust_photo (§6.4, 2026-09-08): the chat tunes the plate in place
#
# The point of the lane being a chat: "that's three eggs, not two" has to land
# on the row the photo already staged, keeping vision's record, the estimate
# flag and every row it did not touch.
# ---------------------------------------------------------------------------


def _change(item, op="amount", qty=None, unit=None, grams=None, note="they said so"):
    from services.agent import PhotoChange

    return PhotoChange(op=op, item=item, qty=qty, unit=unit, grams=grams, note=note)


def _body(log_id):
    from models import LogCreate

    return LogCreate(
        id=uuid4(), text="", captured_at=CAPTURED, tz=TZ, photo_log_id=log_id
    )


def test_match_row_names_a_row_by_food_or_phrase_and_says_so_when_it_cannot():
    rows = [
        {"staged": {"phrase": "eggs abc", "food": {"name": "Egg, whole, fried"}}},
        {"staged": {"phrase": "rice abc", "food": {"name": "Rice, white, cooked"}}},
        {"staged": {"phrase": "mystery sauce abc"}},  # searching: no food yet
    ]
    assert photo._match_row(rows, "Rice, white, cooked") == 1  # exact food name
    assert photo._match_row(rows, "eggs abc") == 0  # exact phrase
    assert photo._match_row(rows, "sauce") == 2  # substring, on a foodless row
    assert photo._match_row(rows, "  RICE, WHITE, COOKED ") == 1  # case/space insensitive

    unknown = photo._match_row(rows, "bacon")
    assert unknown["error"] == "unknown_item"
    assert "Rice, white, cooked" in unknown["items"]  # the model is told what IS there
    # "rice" hits the phrase AND the food name of one row — still one row, not ambiguous
    assert photo._match_row(rows, "rice") == 1
    ambiguous = photo._match_row(rows, "a")
    assert ambiguous["error"] == "ambiguous_item"


def test_adjust_photo_amount_lands_on_the_row_and_keeps_what_the_photo_saw():
    """The core of the feature. A corrected count re-maps onto the food's own
    serving with macros from the cache, the vision record and the diary-date
    shift survive the rewrite (KEEP_LOG_KEYS_SQL, BACKLOG 57), the row is
    flagged an estimate again and its confirmation is gone — a new amount
    needs a new confirm (§3.4b rule 4)."""

    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            eggs = p.items[0]
            assert eggs.qty == 2 and eggs.estimated == "portion"
            await confirm(p.log_id, 0)  # the user had already confirmed the photo's 2
            # a §7.2 shift on the row: the key KEEP_LOG_KEYS_SQL exists to save.
            # spoken_qty rides the same extra-key channel but KEEP does NOT
            # carry it, so a full payload rewrite has to re-apply it by hand.
            await db.execute(
                "UPDATE item_resolutions SET staged = staged || "
                "jsonb_build_object('logged_at', '2026-09-07T12:00:00+00:00', "
                "                   'spoken_qty', 2, 'spoken_unit', null) "
                "WHERE log_id = %s AND item_index = 0",
                (p.log_id,),
            )

            out = await photo.adjust_rows(
                p.log_id, [_change(f"eggs {p.tag}", qty=3, grams=150, note="there were three")],
                _body(p.log_id),
            )
            assert "error" not in out
            row = await item_row(p.log_id, 0)
            staged = row["staged"]
            assert staged["qty"] == 3  # one egg serving each
            assert staged["macros"]["kcal"] == pytest.approx(EGG["macros"]["kcal"] * 3)
            assert staged["estimated"] == "portion"
            assert "there were three" in staged["estimate_note"]
            assert staged["vision"]["note"] == "two fried eggs"  # the record stands
            assert staged["logged_at"] == "2026-09-07T12:00:00+00:00"  # BACKLOG 57
            assert "estimate_confirmed" not in staged  # must be confirmed again
            assert row["was_edited"] is True
            # a resolved row keeps the photo's own spoken estimate: it is what
            # a later search pick sizes the swapped-in food by (§3.4b rule 4)
            assert staged["spoken_qty"] == 2
            # every other row untouched — this is what restaging would have lost
            assert (await item_row(p.log_id, 1))["staged"]["qty"] == p.items[1].qty

    asyncio.run(run())


def test_adjust_photo_removes_a_row_and_adds_one_the_photo_missed():
    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            banana = await insert_food(conn, f"banana {p.tag}", [RICE_100])
            await insert_alias(conn, f"banana {p.tag}", banana)

            out = await photo.adjust_rows(
                p.log_id,
                [
                    _change(f"coffee {p.tag}", op="remove"),
                    _change(f"banana {p.tag}", op="add", qty=120, unit="g",
                            note="the photo missed it"),
                ],
                _body(p.log_id),
            )
            assert "error" not in out
            assert (await item_row(p.log_id, 3))["status"] == "removed"
            # the added row lands on the SAME log, after the highest index —
            # including over the removed one, or _persist's ON CONFLICT would
            # have swallowed it
            added = await item_row(p.log_id, 4)
            assert added is not None
            assert added["staged"]["food"]["id"] == banana
            assert added["staged"]["estimated"] == "portion"
            # the answer is the plate as it now stands, and carries no macros
            names = [i["food"] or i["phrase"] for i in out["items"]]
            assert any(f"banana {p.tag}" in (n or "") for n in names)
            assert not any("macros" in i for i in out["items"])

    asyncio.run(run())


def test_adjust_photo_refuses_a_committed_plate_and_an_unknown_row():
    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            unknown = await photo.adjust_rows(
                p.log_id, [_change("bacon", qty=2, grams=60)], _body(p.log_id)
            )
            assert unknown["error"] == "unknown_item"

            await db.execute(
                "UPDATE logs SET status = 'committed' WHERE id = %s", (p.log_id,)
            )
            done = await photo.adjust_rows(
                p.log_id, [_change(f"eggs {p.tag}", qty=3, grams=150)], _body(p.log_id)
            )
            assert done["error"] == "not_staged" and "diary" in done["message"]

    asyncio.run(run())


def test_adjust_photo_keeps_a_searching_row_on_the_spoken_channel_for_tier3():
    """Row 2 is the sauce nothing resolved. Correcting its amount cannot map
    onto servings that do not exist yet, so the amount rides spoken_qty/unit
    for whatever tier 3 picks — the §7.3 channel the whole lane uses."""

    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            assert p.items[2].status == "searching"
            out = await photo.adjust_rows(
                p.log_id,
                [_change(f"mystery sauce {p.tag}", qty=75, unit="g", note="more like 75 g")],
                _body(p.log_id),
            )
            assert "error" not in out
            staged = (await item_row(p.log_id, 2))["staged"]
            assert staged["qty"] == 75 and staged["spoken_qty"] == 75
            assert staged["spoken_unit"] == "g"
            assert staged.get("macros") is None  # no numbers exist for it yet

    asyncio.run(run())


def test_adjust_photo_does_not_promote_an_unresolved_row_to_a_portion():
    """§3.4b rule 4: "unresolved" becomes "portion" only when the row gains a
    DATABASE food. Correcting the amount by voice does not give it one, so the
    numberless placeholder must keep its flag — that flag is what renders the
    honest "no match, no numbers" row and routes the user to Search, and it is
    what the corrected amount has to survive on (the pick reads spoken_qty)."""

    async def run():
        async with isolated_db() as conn:
            p = await plate(conn)
            # row 2 is the sauce nothing resolved; tier 3's not_found turns it
            # into the numberless placeholder
            placeholder = await photo.stage_estimate_food(str(p.log_id), 2)
            assert placeholder is not None
            assert placeholder.estimated == "unresolved"
            assert placeholder.macros is None and placeholder.search_instead

            out = await photo.adjust_rows(
                p.log_id,
                [_change(f"mystery sauce {p.tag}", qty=90, unit="g", note="more like 90 g")],
                _body(p.log_id),
            )
            assert "error" not in out
            staged = (await item_row(p.log_id, 2))["staged"]
            # the flag stands, so the row still reads as unmatched everywhere
            assert staged["estimated"] == "unresolved"
            assert staged["search_instead"] is True
            assert staged.get("macros") is None
            assert "search to pick a food" in staged["estimate_note"]
            # and the corrected amount is on the channel the pick will read
            assert staged["spoken_qty"] == 90 and staged["spoken_unit"] == "g"

            # it still cannot be confirmed — the gate that actually guards
            # commit is the missing macros, and it is untouched by any of this
            with pytest.raises(HTTPException) as exc:
                await confirm(p.log_id, 2)
            assert exc.value.status_code == 409

    asyncio.run(run())


def test_a_menu_match_stages_a_menu_food_row_with_the_pages_own_numbers():
    """§3.4a as widened 2026-09-09: the ONE path that carries model-sourced
    macros into the diary. The numbers still land the only way any numbers
    can — a cached `foods` row — so this pins the row it writes, not just the
    blocks it refuses (those are in test_photo_chat.py, which needs no DB).

    The row stays `estimated="portion"`: the FOOD is the menu's, the AMOUNT is
    one published serving and almost certainly not what was on the plate, so
    it still cannot commit until a human says it is right."""

    async def run():
        async with isolated_db():
            tag = uuid4().hex[:8]
            url = f"https://example-restaurant.test/menu/{tag}"
            item = StagedItem(
                item_index=0, phrase=f"lamb {tag}", status="searching",
                meal=Meal.lunch, confidence="medium",
            )
            v = VREAL.VisionItem(
                phrase=f"lamb {tag}", qty=None, unit=None, est_grams=120,
                confidence="medium", note="sliced lamb",
                menu=VREAL.MenuMatch(
                    dish=f"Herb Roasted Leg of Lamb {tag}", source_url=url,
                    serving="4 oz", kcal=320, protein_g=28, carb_g=2, fat_g=21,
                ),
            )
            staged = await photo._stage_menu_food(item, v)

            # the row the chat and the sheet will show
            assert staged.status == "resolved" and staged.search_instead is False
            assert staged.food.name == f"Herb Roasted Leg of Lamb {tag}"
            assert staged.serving.id == "menu" and staged.serving.description == "4 oz"
            assert staged.qty == 1.0
            # the four core macros and nothing invented around them: a menu
            # publishes no fibre, so the row says so rather than guessing 0
            assert staged.macros.model_dump() == {
                "kcal": 320, "protein_g": 28, "carb_g": 2, "fat_g": 21, "fiber_g": None,
            }
            # still flagged: the published serving is not what was on the plate
            assert staged.estimated == "portion"
            assert url in staged.estimate_note and "4 oz" in staged.estimate_note
            assert staged.servings and staged.servings[0].serving_id == "menu"

            # and the cached row it came from, traceable to page and model
            food = await db.fetchone("SELECT * FROM foods WHERE id = %s", (staged.food.id,))
            assert food["source"] == "menu"
            assert food["source_url"] == url
            assert food["source_id"].startswith(url + "#")
            assert food["provenance"]["model"] == VREAL.MODEL
            assert food["provenance"]["menu"]["source_url"] == url
            assert food["servings"][0]["macros"]["protein_g"] == 28
            # a `source='menu'` row is a real cached food — unlike the
            # numberless `source='estimate'` placeholder, search may return it
            from routes.foods import _local_hits

            hits = await _local_hits(f"Herb Roasted Leg of Lamb {tag}")
            assert any(h.food_id == staged.food.id for h in hits), hits

    asyncio.run(run())
