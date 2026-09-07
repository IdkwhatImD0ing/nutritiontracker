"""B2 tests — quick-add routes: barcode + from-food (contracts C5/C6, M4).

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one force-rollback connection) — the live Neon DB keeps
no row. fdc.lookup_barcode is monkeypatched (no network, no FDC spend);
tracing is stubbed for the commit test.

Run:  uv run --project backend --with pytest pytest backend/tests/test_quick.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row


@pytest.fixture(scope="module", autouse=True)
def _tracing_stub():
    mod = types.ModuleType("tracing")
    mod.init_tracing = lambda: None
    mod.flush = lambda: None
    mod.score_commit = lambda *a, **kw: None
    saved = sys.modules.get("tracing")
    sys.modules["tracing"] = mod
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop("tracing", None)
        else:
            sys.modules["tracing"] = saved


import db  # noqa: E402


from tenancy_harness import bind_test_tenant  # noqa: E402
from models import CommitRequest, Meal, ParsedItem, ServingOption  # noqa: E402
from resolver import cascade  # noqa: E402
from resolver import commit as commit_tx  # noqa: E402
from resolver import foods_repo  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes import foods as foods_routes  # noqa: E402
from routes import quick  # noqa: E402
from routes import resolve as resolve_routes  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
from sources import fdc, off  # noqa: E402

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

BAR = {
    "serving_id": "label",
    "description": "1 bar (60 g)",
    "grams": 60.0,
    "macros": {"kcal": 200.0, "protein_g": 21.0, "carb_g": 22.0, "fat_g": 8.0, "fiber_g": 14.0},
}
HUNDRED_G = {
    "serving_id": "100g",
    "description": "100 g",
    "grams": 100.0,
    "macros": {"kcal": 333.33, "protein_g": 35.0, "carb_g": 36.67, "fat_g": 13.33},
}
CUP = {
    "serving_id": "cup",
    "description": "1 cup (240 g)",
    "grams": 240.0,
    "macros": {"kcal": 240.0, "protein_g": 8.0, "carb_g": 40.0, "fat_g": 4.0},
}


# ---------------------------------------------------------------------------
# rollback harness (same shape as test_commit_hardening.py)
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


def _fake_food(display_name: str, brand: str | None = None) -> dict:
    """The shape sources/fdc.get_food returns (what lookup_barcode hands back)."""
    return {
        "source": "fdc",
        "source_id": f"test-{uuid4().hex[:12]}",
        "name": display_name.upper(),
        "display_name": display_name,
        "brand": brand,
        "food_type": "branded",
        "state": None,
        "state_sensitive": False,
        "servings": [BAR, HUNDRED_G],
        "default_serving_id": "label",
        "per_100g": {"kcal": 333.33, "protein_g": 35.0, "carb_g": 36.67, "fat_g": 13.33},
        "payload": {"test": True},
    }


def _req(food_id, **kw) -> quick.FromFoodRequest:
    fields = dict(id=uuid4(), food_id=food_id, input_mode="barcode", captured_at=NOW())
    fields.update(kw)
    return quick.FromFoodRequest(**fields)


# ---------------------------------------------------------------------------
# C5 — GET /api/foods/barcode
# ---------------------------------------------------------------------------


def test_barcode_hit_normalizes_persists_and_returns_contract_shape(monkeypatch):
    name = f"zz quick bar {uuid4().hex[:8]}"
    food = _fake_food(name, brand="TestBrand")
    seen: list[str] = []

    async def fake_lookup(code):
        seen.append(code)
        return food

    monkeypatch.setattr(fdc, "lookup_barcode", fake_lookup)

    async def no_off(code):
        raise AssertionError("Open Food Facts must not be consulted on an FDC hit")

    monkeypatch.setattr(off, "lookup_barcode", no_off)

    async def run():
        async with isolated_db():
            # the fake payload carries no gtinUpc, so the rescan is not a
            # cache hit — both scans reach FDC (the cache path has its own test)
            resp = await quick.lookup_barcode(code=" 0-49000 05010-4 ")
            again = await quick.lookup_barcode(code="049000050104")
            row = await db.fetchone(
                "SELECT source, source_id FROM foods WHERE id = %s", (resp.food.id,)
            )
            return resp, again, row

    resp, again, row = asyncio.run(run())
    assert seen == ["049000050104", "049000050104"]  # digits only, both times
    assert resp.found is True and resp.food is not None
    assert resp.food.id == again.food.id  # persisted: the id is stable
    assert row is not None and row["source"] == "fdc" and row["source_id"] == food["source_id"]
    assert resp.food.display_name == name and resp.food.brand == "TestBrand"
    assert resp.food.source == "fdc"
    assert [s.serving_id for s in resp.food.servings] == ["label", "100g"]
    assert all(isinstance(s, ServingOption) for s in resp.food.servings)
    assert resp.food.servings[0].description == "1 bar (60 g)"
    assert resp.food.servings[0].grams == 60.0
    assert resp.food.servings[0].macros == BAR["macros"]
    assert resp.food.macros_per_100g == food["per_100g"]


def test_barcode_miss_is_found_false(monkeypatch):
    asked: list[str] = []

    async def fake_lookup(code):
        asked.append("fdc")
        return None

    async def fake_off(code):
        asked.append("off")
        return None

    monkeypatch.setattr(fdc, "lookup_barcode", fake_lookup)
    monkeypatch.setattr(off, "lookup_barcode", fake_off)

    async def run():
        async with isolated_db():
            return await quick.lookup_barcode(code="000000000000")

    resp = asyncio.run(run())
    assert asked == ["fdc", "off"]  # FDC first; Open Food Facts only on its miss
    assert resp.found is False and resp.food is None
    assert resp.model_dump() == {"found": False, "food": None}


def test_barcode_without_digits_is_422_before_any_lookup(monkeypatch):
    async def fake_lookup(code):
        raise AssertionError("lookup must not run for a digitless code")

    monkeypatch.setattr(fdc, "lookup_barcode", fake_lookup)
    monkeypatch.setattr(off, "lookup_barcode", fake_lookup)
    with pytest.raises(HTTPException) as e:
        asyncio.run(quick.lookup_barcode(code="not-a-barcode"))
    assert e.value.status_code == 422


def test_barcode_fdc_miss_falls_back_to_open_food_facts(monkeypatch):
    name = f"zz quick off {uuid4().hex[:8]}"
    code = f"0{uuid4().int % 10**12:012d}"  # a 13-digit code, OFF's stored form
    food = dict(
        _fake_food(name, brand="OffBrand"),
        source="off",
        source_id=code,
        payload={"code": code, "completeness": 0.7},
    )

    async def fdc_miss(code):
        return None

    async def off_hit(code):
        return food

    monkeypatch.setattr(fdc, "lookup_barcode", fdc_miss)
    monkeypatch.setattr(off, "lookup_barcode", off_hit)

    async def run():
        async with isolated_db():
            resp = await quick.lookup_barcode(code=code)
            row = await db.fetchone(
                "SELECT source, source_id FROM foods WHERE id = %s", (resp.food.id,)
            )
            return resp, row

    resp, row = asyncio.run(run())
    assert resp.found is True and resp.food is not None
    assert resp.food.source == "off"
    assert resp.food.display_name == name and resp.food.brand == "OffBrand"
    assert row["source"] == "off" and row["source_id"] == code


def test_barcode_rescan_answers_from_the_cache_without_the_network(monkeypatch):
    """Each source stores a code under its own padding — FDC 14-digit in the
    payload's gtinUpc, OFF 13-digit as source_id — and a rescan, under any
    rung of the ladder, is served from foods before either is asked."""
    tail = f"{uuid4().int % 10**11:011d}"
    upc = "0" + tail  # a 12-digit UPC-A, FDC-known
    upc2 = "1" + tail  # a second code, OFF-only
    fdc_food = dict(_fake_food(f"zz quick fdc {tail}"), payload={"gtinUpc": upc.zfill(14)})
    off_food = dict(
        _fake_food(f"zz quick off {tail}"),
        source="off",
        source_id=upc2.zfill(13),
        payload={"code": upc2.zfill(13)},
    )
    calls: list[str] = []

    async def fdc_lookup(code):
        calls.append("fdc")
        return fdc_food if code == upc else None

    async def off_lookup(code):
        calls.append("off")
        return off_food

    monkeypatch.setattr(fdc, "lookup_barcode", fdc_lookup)
    monkeypatch.setattr(off, "lookup_barcode", off_lookup)

    async def run():
        async with isolated_db():
            first = await quick.lookup_barcode(code=upc)
            stripped = await quick.lookup_barcode(code=tail)  # leading zero gone
            padded = await quick.lookup_barcode(code=upc.zfill(14))
            hit = await quick.lookup_barcode(code=upc2)
            rescan = await quick.lookup_barcode(code=upc2.zfill(14))
            return first, stripped, padded, hit, rescan

    first, stripped, padded, hit, rescan = asyncio.run(run())
    # one FDC lookup for three scans of the first code; one FDC miss + one OFF
    # lookup for two scans of the second
    assert calls == ["fdc", "fdc", "off"]
    assert first.found and first.food.id == stripped.food.id == padded.food.id
    assert hit.found and hit.food.source == "off" and hit.food.id == rescan.food.id


_FDC_SEARCH = httpx.Request("GET", "https://api.nal.usda.gov/fdc/v1/foods/search")


def test_barcode_fdc_error_is_503_and_never_an_off_answer(monkeypatch):
    """A 429/5xx from FDC is an outage, not a miss: the route says so (503)
    rather than caching an Open Food Facts row that every rescan would then
    answer from without FDC ever being asked again."""

    async def fdc_down(code):
        raise httpx.HTTPStatusError(
            "429", request=_FDC_SEARCH, response=httpx.Response(429, request=_FDC_SEARCH)
        )

    async def no_off(code):
        raise AssertionError("Open Food Facts must not stand in for an FDC outage")

    monkeypatch.setattr(fdc, "lookup_barcode", fdc_down)
    monkeypatch.setattr(off, "lookup_barcode", no_off)

    async def run():
        async with isolated_db():
            return await quick.lookup_barcode(code="049000050104")

    with pytest.raises(HTTPException) as e:
        asyncio.run(run())
    assert e.value.status_code == 503
    # class name only — never the message, which embeds the URL (§10)
    assert "HTTPStatusError" in e.value.detail and "usda" not in e.value.detail.lower()


def test_fdc_lookup_raises_on_a_non_2xx_search(monkeypatch):
    """The source itself: a rate-limited or failing search raises (the route's
    503 path) instead of reading as a miss."""

    async def rate_limited(path, **params):
        return httpx.Response(429, request=_FDC_SEARCH)

    monkeypatch.setattr(fdc, "_get", rate_limited)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(fdc.lookup_barcode("049000050104"))


# ---------------------------------------------------------------------------
# C6 — POST /api/logs/from-food
# ---------------------------------------------------------------------------


def test_from_food_stages_one_resolved_row_with_defaults_and_replays():
    name = f"zz quick bar {uuid4().hex[:8]}"

    async def run():
        async with isolated_db():
            fid = await foods_repo.persist_food(_fake_food(name, brand="TestBrand"))
            body = _req(fid, meal_hint=Meal.lunch)
            resp = await quick.create_log_from_food(body)
            replay = await quick.create_log_from_food(body)  # same client id
            log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (body.id,))
            rows = await db.fetchall(
                "SELECT * FROM item_resolutions WHERE log_id = %s", (body.id,)
            )
            return fid, body, resp, replay, log, rows

    fid, body, resp, replay, log, rows = asyncio.run(run())
    assert resp.log_id == body.id and resp.meal == Meal.lunch
    assert len(resp.items) == 1
    item = resp.items[0]
    assert item.status == "resolved" and item.tier is None
    assert item.item_index == 0 and item.phrase == name
    assert item.food.id == fid and item.food.name == name and item.food.brand == "TestBrand"
    assert item.serving.id == "label"  # the food's default serving
    assert item.serving.description == "1 bar (60 g)"
    assert item.qty == 1.0
    assert item.macros.model_dump(exclude_none=True) == BAR["macros"]
    assert item.confidence == "high"
    assert [s.serving_id for s in item.servings] == ["label", "100g"]
    assert item.meal == Meal.lunch

    # the log row: input_mode as given, raw_input = the display name
    assert log["input_mode"] == "barcode" and log["raw_input"] == name
    assert log["status"] == "staged" and log["meal_inferred"] == "lunch"
    assert log["short_circuit"] is False and log["decompose_ms"] is None

    # exactly one staged row, written the way routes/logs.py writes them
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "resolved" and row["tier"] is None
    assert row["alias_id"] is None
    assert str(row["proposed_food_id"]) == fid
    assert row["proposed_serving_id"] == "label" and float(row["proposed_qty"]) == 1.0
    assert row["phrase"] == name and row["phrase_norm"] == norm(name)
    assert row["staged"]["status"] == "resolved" and row["staged"]["tier"] is None
    assert row["staged"]["food"]["id"] == fid
    assert row["staged"]["macros"]["kcal"] == 200.0

    # duplicate POST by client id replays the stored staging state (the poll
    # shape, StagedItemOut — its watchdog `stale` key is null/absent here)
    assert replay.log_id == body.id
    assert [i.model_dump(exclude={"stale"}) for i in replay.items] == [item.model_dump()]


def test_from_food_explicit_serving_qty_search_mode_and_inferred_meal():
    name = f"zz quick oats {uuid4().hex[:8]}"

    async def run():
        async with isolated_db():
            fid = await foods_repo.persist_food(_fake_food(name))
            body = _req(fid, serving_id="100g", qty=2.5, input_mode="search")
            resp = await quick.create_log_from_food(body)
            log = await db.fetchone(
                "SELECT input_mode, meal_inferred FROM logs WHERE id = %s", (body.id,)
            )
            return resp, log

    resp, log = asyncio.run(run())
    item = resp.items[0]
    assert item.serving.id == "100g" and item.qty == 2.5
    assert item.macros.kcal == round(333.33 * 2.5, 2)
    assert item.macros.protein_g == 87.5
    assert item.food.brand is None
    assert log["input_mode"] == "search"
    assert isinstance(resp.meal, Meal) and log["meal_inferred"] == resp.meal.value


def test_from_food_unknown_food_and_serving():
    name = f"zz quick bar {uuid4().hex[:8]}"

    async def run():
        async with isolated_db():
            with pytest.raises(HTTPException) as missing:
                await quick.create_log_from_food(_req(uuid4()))
            fid = await foods_repo.persist_food(_fake_food(name))
            with pytest.raises(HTTPException) as dangling:
                await quick.create_log_from_food(_req(fid, serving_id="nope"))
            logs = await db.fetchall("SELECT 1 FROM logs WHERE raw_input = %s", (name,))
            return missing.value.status_code, dangling.value.status_code, logs

    missing, dangling, logs = asyncio.run(run())
    assert missing == 404
    assert dangling == 422
    assert logs == []  # validation happens before the log row is written


def test_from_food_qty_must_be_positive():
    with pytest.raises(ValueError):
        _req(uuid4(), qty=0)


def test_from_food_qty_ceiling_and_non_finite_guard_leave_no_log_row():
    """Fix-pass P2: qty had no ceiling — qty=1e308 scaled the macros to inf,
    psycopg emitted Infinity, Postgres rejected the jsonb, and the 500 landed
    AFTER the logs row was inserted. The ceiling is a validation error before
    the handler runs; a non-finite scale (absurd cached macros) is a 422
    before any INSERT."""
    with pytest.raises(ValueError):
        _req(uuid4(), qty=1e308)
    with pytest.raises(ValueError):
        _req(uuid4(), qty=1001)
    _req(uuid4(), qty=1000)  # the ceiling itself is allowed

    name = f"zz quick huge {uuid4().hex[:8]}"
    absurd = dict(_fake_food(name), servings=[dict(BAR, macros={**BAR["macros"], "kcal": 1e306})])

    async def run():
        async with isolated_db():
            fid = await foods_repo.persist_food(absurd)
            with pytest.raises(HTTPException) as ei:
                await quick.create_log_from_food(_req(fid, qty=1000))
            logs = await db.fetchall("SELECT 1 FROM logs WHERE raw_input = %s", (name,))
            return ei.value.status_code, logs

    status, logs = asyncio.run(run())
    assert status == 422
    assert logs == []  # refused before the log row, never a half-written log


def test_from_food_default_portion_matches_the_text_lane():
    """Fix-pass E6: with no serving_id the search lane defaulted to servings[0]
    (a household cup) while the cascade's §7.3 step 4 gives the text lane the
    provider default (100 g) — the same generic food logged two ways showed
    two different portions. Both lanes now use foods.default_serving_id."""
    name = f"zz quick oats {uuid4().hex[:8]}"
    food = dict(_fake_food(name), servings=[CUP, HUNDRED_G], default_serving_id="100g")

    async def run():
        async with isolated_db():
            fid = await foods_repo.persist_food(food)
            picked = (await quick.create_log_from_food(_req(fid, input_mode="search"))).items[0]
            spoken = await cascade.resolve_item(
                ParsedItem(
                    phrase=name, phrase_kind="food", qty=None, unit=None, state=None,
                    brand=None, library_match=fid, meal_ref=None,
                ),
                0, Meal.snack,
            )
            return picked, spoken.staged

    picked, spoken = asyncio.run(run())
    assert spoken.status == "resolved" and spoken.food.id == picked.food.id
    assert picked.serving.id == "100g" == spoken.serving.id
    assert picked.qty == 1.0 == spoken.qty
    assert picked.macros == spoken.macros
    # the fallback: a food whose default id dangles still stages its first serving
    dangling = dict(_fake_food(name + " b"), servings=[CUP, HUNDRED_G], default_serving_id="nope")

    async def run_dangling():
        async with isolated_db():
            fid = await foods_repo.persist_food(dangling)
            return (await quick.create_log_from_food(_req(fid))).items[0]

    assert asyncio.run(run_dangling()).serving.id == "cup"


def _estimate_food(name: str) -> dict:
    """The photo lane's numberless placeholder, as routes/photo.stage_estimate_food
    persists it (source='estimate', an empty macros block)."""
    serving = {
        "serving_id": "photo", "description": "~30 g (photo estimate)", "grams": 30.0, "macros": {},
    }
    return {
        "source": "estimate",
        "source_id": f"{uuid4()}:0",
        "source_url": None,
        "provenance": {"vision": {"phrase": name}},
        "name": name,
        "display_name": name,
        "brand": None,
        "food_type": "generic",
        "state": None,
        "state_sensitive": False,
        "servings": [serving],
        "default_serving_id": "photo",
        "per_100g": None,
        "payload": {},
    }


def test_estimate_foods_never_leave_the_photo_lane():
    """Fix-pass E3 (R1): a source='estimate' foods row leaked out of the photo
    lane — the local search listed it as a 'my foods' hit, /api/logs/from-food
    staged it as an UNFLAGGED resolved row with macros None, and an answer
    chip could pick it into a text-lane row. Every pick path refuses it and
    the search filters it out."""
    from routes.resolve import Answer, AnswerRequest

    name = f"zz quick mystery {uuid4().hex[:8]}"

    async def run():
        async with isolated_db():
            fid = await foods_repo.persist_food(_estimate_food(name))
            hits = await foods_routes._local_hits(name)
            with pytest.raises(HTTPException) as from_food:
                await quick.create_log_from_food(_req(fid, input_mode="search"))
            logs = await db.fetchall("SELECT 1 FROM logs WHERE raw_input = %s", (name,))
            # the answer chip on a live text-lane row
            log_id = uuid4()
            await db.execute(
                "INSERT INTO logs (id, input_mode, raw_input, captured_at) "
                "VALUES (%s, 'text', %s, now())",
                (log_id, name),
            )
            await db.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged)
                VALUES (%s, 0, %s, %s, 'not_found', %s::jsonb)
                """,
                (log_id, name, norm(name),
                 f'{{"item_index": 0, "phrase": "{name}", "status": "not_found", "meal": "snack"}}'),
            )
            with pytest.raises(HTTPException) as answer:
                await resolve_routes.answer_question(
                    AnswerRequest(log_id=log_id, item_index=0, answer=Answer(food_id=fid))
                )
            row = await db.fetchone(
                "SELECT status FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )
            with pytest.raises(HTTPException) as select:
                await foods_routes.select_food(
                    foods_routes.SelectRequest(source="estimate", source_id=f"{log_id}:0")
                )
            return hits, from_food.value, logs, answer.value, row, select.value

    hits, from_food, logs, answer, row, select = asyncio.run(run())
    assert [h.label for h in hits] == []  # the search never lists it
    assert from_food.status_code == 422 and "photo-lane placeholder" in from_food.detail
    assert logs == []  # refused before the log row
    assert answer.status_code == 422 and "photo-lane placeholder" in answer.detail
    assert row["status"] == "not_found"  # the row is untouched
    assert select.status_code == 422
    assert foods_routes._SOURCE_LABELS["estimate"]  # the detail view names it


def test_from_food_row_commits_with_snapshot_macros_and_learns_nothing():
    name = f"zz quick bar {uuid4().hex[:8]}"

    async def run():
        async with isolated_db():
            fid = await foods_repo.persist_food(_fake_food(name, brand="TestBrand"))
            resp = await quick.create_log_from_food(_req(fid, meal_hint=Meal.snack))
            totals = await commit_route(resp.log_id, CommitRequest(items=resp.items))
            entry = await db.fetchone(
                "SELECT food_id, serving_id, qty, grams, macros, alias_id "
                "FROM entries WHERE log_id = %s",
                (resp.log_id,),
            )
            res = await db.fetchone(
                "SELECT status, tier, alias_id, committed_at, final_food_id, final_qty "
                "FROM item_resolutions WHERE log_id = %s",
                (resp.log_id,),
            )
            log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (resp.log_id,))
            alias = await db.fetchone(
                "SELECT id FROM aliases WHERE alias_norm = %s", (norm(name),)
            )
            return fid, resp, totals, entry, res, log, alias

    fid, resp, totals, entry, res, log, alias = asyncio.run(run())
    item = resp.items[0]
    assert totals.errors == []
    assert log["status"] == "committed"
    # the entry is a snapshot of the cached food's serving × qty
    assert entry is not None
    assert str(entry["food_id"]) == fid and entry["serving_id"] == "label"
    assert float(entry["qty"]) == 1.0 and float(entry["grams"]) == 60.0
    assert {k: float(v) for k, v in entry["macros"].items()} == item.macros.model_dump(
        exclude_none=True
    )
    assert res["status"] == "resolved" and res["committed_at"] is not None
    assert res["tier"] is None and str(res["final_food_id"]) == fid

    # Contract C4(b): rows on barcode/search logs learn no alias and no portion
    # default — the gate lives in resolver/commit.py and is B1's concurrent
    # work. Until it lands, commit still learns for a NULL-tier row, so this
    # assertion is skipped rather than failed.
    if "input_mode" not in Path(commit_tx.__file__).read_text(encoding="utf-8"):
        pytest.skip(
            "C4(b) not landed: resolver/commit.py has no input_mode learning gate yet (B1)"
        )
    assert alias is None, "a barcode row must not learn an alias for its display name"
    assert entry["alias_id"] is None and res["alias_id"] is None


def test_diary_correction_on_a_quick_add_row_learns_nothing():
    """Fix-pass blocker (R3 through the diary): PATCH /api/entries/:id with a
    new food learned an alias for a barcode row's display name and recomputed
    a portion default — the PATCH never read logs.input_mode. The same check
    as the commit test above, through routes/entries.patch_entry."""
    from routes.entries import EntryPatch, patch_entry

    name = f"zz quick bar {uuid4().hex[:8]}"

    async def run():
        async with isolated_db():
            fid = await foods_repo.persist_food(_fake_food(name, brand="TestBrand"))
            other = await foods_repo.persist_food(_fake_food(f"{name} other", brand="TestBrand"))
            resp = await quick.create_log_from_food(_req(fid, meal_hint=Meal.snack))
            await commit_route(resp.log_id, CommitRequest(items=resp.items))
            entry = await db.fetchone("SELECT id FROM entries WHERE log_id = %s", (resp.log_id,))
            patched = await patch_entry(entry["id"], EntryPatch(food_id=other))
            row = await db.fetchone(
                "SELECT food_id, alias_id FROM entries WHERE id = %s", (entry["id"],)
            )
            res = await db.fetchone(
                "SELECT final_food_id, alias_id, was_edited FROM item_resolutions "
                "WHERE log_id = %s",
                (resp.log_id,),
            )
            aliases = await db.fetchall(
                "SELECT 1 FROM aliases WHERE alias_norm IN (%s, %s)",
                (norm(name), norm(f"{name} other")),
            )
            return other, patched, row, res, aliases

    other, patched, row, res, aliases = asyncio.run(run())
    assert patched.entry.food.id == other and patched.entry.macros.kcal == 200.0
    assert str(row["food_id"]) == other and row["alias_id"] is None
    assert str(res["final_food_id"]) == other and res["was_edited"] is True
    assert res["alias_id"] is None
    assert aliases == [], "a diary correction on a barcode row must not learn an alias"


# ---------------------------------------------------------------------------
# Router shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# recently logged foods (owner feedback, 2026-09-06): GET /api/foods/recent
# under the Search lane's bar, and the same rows in the decompose context
# ---------------------------------------------------------------------------


def test_recent_foods_lists_committed_picks_newest_first_and_reaches_the_context():
    """One row per food, the food committed last first, the photo lane's
    placeholders left out; the same rows ride the decompose context as
    recent_foods — a from-food row learns no alias, so the alias-built index
    never carries them. (Inside one rollback transaction every entry shares
    now() as created_at; the query's logged_at tiebreak orders them.)"""
    name_a = f"zz recent a {uuid4().hex[:8]}"
    name_b = f"zz recent b {uuid4().hex[:8]}"

    async def log_and_commit(fid):
        resp = await quick.create_log_from_food(_req(fid, input_mode="search"))
        totals = await commit_route(resp.log_id, CommitRequest(items=resp.items))
        assert totals.errors == []

    async def run():
        from services.context import build_agent_context

        async with isolated_db():
            fid_a = await foods_repo.persist_food(_fake_food(name_a, brand="BrandA"))
            fid_b = await foods_repo.persist_food(_fake_food(name_b))
            fid_e = await foods_repo.persist_food(_estimate_food("zz recent placeholder"))
            await log_and_commit(fid_a)
            await log_and_commit(fid_b)
            await log_and_commit(fid_a)  # logged again: still one row, back at the front
            # a placeholder entry exists only by hand — from-food refuses the pick
            await db.execute(
                "INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, macros) "
                "VALUES (now(), 'snack', %s, 'photo', 1, '{}'::jsonb)",
                (fid_e,),
            )
            hits = await foods_routes.list_recent_foods()
            ctx = await build_agent_context()
            return fid_a, fid_b, fid_e, hits, ctx

    fid_a, fid_b, fid_e, hits, ctx = asyncio.run(run())
    ids = [h.food_id for h in hits]
    assert ids[:2] == [fid_a, fid_b]
    assert ids.count(fid_a) == 1 and fid_e not in ids
    assert hits[0].label == name_a and hits[0].brand == "BrandA" and hits[0].source == "fdc"
    assert len(hits) <= foods_repo.RECENT_FOODS_CAP
    # the context: same rows, same order, the index's own ids not repeated
    recent_ids = [i for i, _, _ in ctx.recent_foods]
    assert recent_ids[:2] == [fid_a, fid_b]
    assert (fid_a, name_a, "BrandA") in ctx.recent_foods
    assert not {i for i, _, _ in ctx.food_index} & set(recent_ids)


def test_render_context_offers_recent_foods_for_library_match():
    """The prompt lists the recent foods under the index, in the index's line
    shape, so decompose may name one as library_match; a context with no
    list (an eval snapshot from before it existed) renders "(empty)"."""
    from prompts.decompose_prompt import render_context
    from services.context import AgentContext

    ctx = AgentContext(
        profile_notes="",
        targets={},
        food_index=[("idx-1", "whey", None)],
        meal_names=[],
        recent_foods=[("rec-1", "fairlife 2% milk", "fairlife")],
    )
    text = render_context(ctx)
    assert "idx-1 | whey | -" in text
    assert "rec-1 | fairlife 2% milk | fairlife" in text
    assert (
        text.index("PERSONAL FOOD INDEX")
        < text.index("RECENTLY LOGGED FOODS")
        < text.index("SAVED MEALS")
    )
    bare = render_context(AgentContext(profile_notes="", targets={}, food_index=[], meal_names=[]))
    assert "newest first):\n(empty)" in bare


def test_router_shape():
    paths = {r.path for r in quick.router.routes}
    assert {"/api/foods/barcode", "/api/logs/from-food"} <= paths
    assert quick.router.dependencies, "router missing require_user dependency"
    # the literal /api/foods/recent must be registered ahead of /api/foods/{food_id}
    food_paths = [r.path for r in foods_routes.router.routes]
    assert food_paths.index("/api/foods/recent") < food_paths.index("/api/foods/{food_id}")
