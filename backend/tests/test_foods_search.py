"""W4 tests — manual search + source hardening (DESIGN.md §3.4, §5).

Covers BACKLOG 3 (GET /api/foods/search + POST /api/foods/select), 9 (branded
brand-owner ranking), 5 (fdc.get_food None/404 tolerance in tier3), and 14
(URL-encoded source_id — exercised implicitly by every live get_food call).

Live tests: real Neon DB (production until the dev branch exists — every row
seeded or created is deleted in a finally block; foods rows that pre-existed
are left alone) and the real USDA FDC API (~6 requests total). The 404-path
tests monkeypatch fdc.get_food and touch no network.

Run:  uv run --with pytest pytest backend/tests/test_foods_search.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_env() -> None:
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

import db  # noqa: E402
from resolver import foods_repo  # noqa: E402
from routes import foods as foods_routes  # noqa: E402
from services import tier3  # noqa: E402
from sources import fdc  # noqa: E402

QUEST_COOKIE_DOUGH_FDC_ID = "2629232"  # pinned live 2026-08-20 (test_tier3_live)
FOUNDATION_CHICKEN_FDC_ID = "2646170"  # pinned live 2026-08-20 (test_fdc_live)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Seed / cleanup helpers
# ---------------------------------------------------------------------------


def _fake_food(display_name: str, brand: str | None = None) -> dict:
    return {
        "source": "fdc",
        "source_id": f"test-{uuid4().hex[:12]}",
        "name": display_name,
        "display_name": display_name,
        "brand": brand,
        "food_type": "branded" if brand else "generic",
        "state": None,
        "state_sensitive": False,
        "servings": [
            {
                "serving_id": "label",
                "description": "1 serving",
                "grams": 100.0,
                "macros": {"kcal": 200.0, "protein_g": 20.0, "carb_g": 10.0, "fat_g": 5.0},
            }
        ],
        "default_serving_id": "label",
        "per_100g": None,
        "payload": {"test": True},
    }


async def _seed_item(
    log_id: str, phrase: str, staged_extra: dict | None = None
) -> None:
    await db.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at) "
        "VALUES (%s, 'text', %s, now())",
        (log_id, phrase),
    )
    staged = {"item_index": 0, "phrase": phrase, "status": "searching", "meal": "snack"}
    if staged_extra:
        staged.update(staged_extra)
    await db.execute(
        "INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, "
        "status, staged) VALUES (%s, 0, %s, %s, 'searching', %s::jsonb)",
        (log_id, phrase, phrase.lower(), json.dumps(staged)),
    )


async def _cleanup_log(log_id: str) -> None:
    await db.execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
    await db.execute("DELETE FROM logs WHERE id = %s", (log_id,))


async def _food_row_id(source: str, source_id: str) -> str | None:
    row = await db.fetchone(
        "SELECT id FROM foods WHERE source = %s AND source_id = %s",
        (source, source_id),
    )
    return str(row["id"]) if row else None


async def _delete_food(food_id: str | None) -> None:
    if not food_id:
        return
    try:
        await db.execute("DELETE FROM foods WHERE id = %s", (food_id,))
    except Exception as e:  # referenced elsewhere — leave it, report
        print(f"[cleanup] foods {food_id}: {e}")


# ---------------------------------------------------------------------------
# BACKLOG 3 — GET /api/foods/search
# ---------------------------------------------------------------------------


def test_search_local_cache_hit_first_for_seeded_food():
    """A seeded cached food comes back in `local` — no network (source=local)."""

    async def scenario():
        marker = f"zz seeded brownie {uuid4().hex[:8]}"
        food_id = None
        try:
            food = _fake_food(marker, brand="TestBrand")
            # a beverage: its per-100 figures are per 100 ml (the 100ml serving)
            food["per_100g"] = {"kcal": 56.0, "protein_g": 10.14, "carb_g": 1.0, "fat_g": 0.5}
            food["servings"].append({
                "serving_id": "100ml", "description": "100 ml", "grams": None,
                "macros": dict(food["per_100g"]),
            })
            food_id = await foods_repo.persist_food(food)
            resp = await foods_routes.search_foods(q=marker[:17], source="local")
            assert resp.local, "seeded food not found in local cache results"
            top = resp.local[0]
            assert top.food_id == food_id
            assert top.source == "fdc" and top.source_id.startswith("test-")
            assert top.label == marker and top.brand == "TestBrand"
            assert (top.kcal_per_100g, top.protein_per_100g, top.per_100ml) == (56.0, 10.14, True)
            assert resp.generic == [] and resp.branded == []  # local-only mode
            print(f"\n[search local] '{marker[:17]}' -> {top.label} (sim-first)")
        finally:
            await _delete_food(food_id)
            await db.close()

    _run(scenario())


def test_search_full_passthrough_local_first_live():
    """Full search: seeded local hit present AND FDC passthrough populated."""

    async def scenario():
        marker = f"core power elite chocolate {uuid4().hex[:6]}"
        food_id = None
        try:
            food_id = await foods_repo.persist_food(_fake_food(marker, brand="fairlife"))
            t0 = time.perf_counter()
            resp = await foods_routes.search_foods(q="core power elite chocolate")
            latency = time.perf_counter() - t0
            assert resp.fdc_error is None, f"FDC passthrough failed: {resp.fdc_error}"
            assert any(h.food_id == food_id for h in resp.local), (
                "seeded cache row missing from local results"
            )
            assert resp.branded, "FDC branded passthrough returned nothing"
            assert all(h.source == "fdc" and h.source_id for h in resp.branded)
            assert all(h.food_id is None for h in resp.branded)  # remote = not cached
            # brand ranking: the query's own brand rows come first
            owners = " ".join((h.brand or "") for h in resp.branded).lower()
            assert "core power" in owners or "fairlife" in owners
            assert "core power" in (resp.branded[0].brand or "").lower()
            print(
                f"\n[search live] {latency:.1f}s local={len(resp.local)} "
                f"generic={len(resp.generic)} branded={len(resp.branded)} "
                f"top_branded='{resp.branded[0].label}' ({resp.branded[0].brand})"
            )
        finally:
            await _delete_food(food_id)
            await db.close()

    _run(scenario())


def test_search_hits_carry_kcal_and_protein_per_100(monkeypatch):
    """A Search row shows kcal and protein per 100 g — per 100 ml for a
    beverage — read off the search payload itself, no extra request. kcal
    falls back to the Atwater energy a Foundation hit carries in place of
    1008. No network: fdc._get is stubbed."""

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    def nutrients(kcal_id: int, kcal: float) -> list[dict]:
        return [
            {"nutrientId": kcal_id, "value": kcal},
            {"nutrientId": 1003, "value": 10.14},
            {"nutrientId": 1005, "value": 4.66},
            {"nutrientId": 1004, "value": 0.0},
        ]

    payloads = {
        "SR Legacy,Foundation": {"foods": [
            {"fdcId": 1, "description": "Yogurt, Greek, plain, nonfat",
             "dataType": "SR Legacy", "foodNutrients": nutrients(1008, 59.0)},
            {"fdcId": 2, "description": "Chicken, breast, raw",
             "dataType": "Foundation", "foodNutrients": nutrients(2047, 112.15)},
        ]},
        "Branded": {"foods": [
            {"fdcId": 3, "description": "Core Power Elite", "dataType": "Branded",
             "servingSizeUnit": "MLT", "foodNutrients": nutrients(1008, 56.0)},
            {"fdcId": 4, "description": "Oikos Pro", "dataType": "Branded",
             "servingSizeUnit": "GRM", "foodNutrients": nutrients(1008, 97.0)},
        ]},
    }

    async def fake_get(path, **params):
        return _Resp(payloads[params["dataType"]])

    monkeypatch.setattr(fdc, "_get", fake_get)
    generic = _run(fdc.search_generic("yogurt"))
    assert [(h["kcal_per_100g"], h["protein_per_100g"]) for h in generic] == [
        (59.0, 10.14), (112.15, 10.14),
    ]
    branded = _run(fdc.search_branded("core power"))
    assert [
        (h["fdc_id"], h["kcal_per_100g"], h["protein_per_100g"], h["per_100ml"])
        for h in branded
    ] == [(3, 56.0, 10.14, True), (4, 97.0, 10.14, False)]


# ---------------------------------------------------------------------------
# BACKLOG 9 — brandOwner ranking in search_branded
# ---------------------------------------------------------------------------


def test_quest_bar_ranks_quest_nutrition_first_live():
    hits = _run(fdc.search_branded("quest bar"))
    assert hits, "no branded hits for 'quest bar'"
    flags = [
        "quest" in f"{h.get('brand_owner') or ''} {h.get('brand_name') or ''}".lower()
        for h in hits
    ]
    assert flags[0], (
        f"top hit is not Quest: {hits[0]['description']!r} "
        f"owner={hits[0]['brand_owner']!r}"
    )
    # ranking property: brand-overlap scores are non-increasing down the list
    qtokens = {"quest", "bar"}
    scores = [fdc._brand_overlap(qtokens, h) for h in hits]
    assert scores == sorted(scores, reverse=True), f"not brand-ranked: {scores}"
    print(
        f"\n[brand rank live] 'quest bar': {sum(flags)}/{len(hits)} Quest rows, "
        f"top='{hits[0]['description']}' owner='{hits[0]['brand_owner']}'"
    )


# ---------------------------------------------------------------------------
# BACKLOG 3 — POST /api/foods/select
# ---------------------------------------------------------------------------


def test_select_with_log_context_persists_and_resolves_live():
    """Select with log context = answer-tap semantics: persist + resolve the
    seeded searching item, one get_food, no agent run."""

    async def scenario():
        log_id = str(uuid4())
        pre_existing = await _food_row_id("fdc", QUEST_COOKIE_DOUGH_FDC_ID)
        try:
            await _seed_item(log_id, "quest bar")
            body = foods_routes.SelectRequest(
                source="fdc",
                source_id=QUEST_COOKIE_DOUGH_FDC_ID,
                log_id=log_id,
                item_index=0,
            )
            t0 = time.perf_counter()
            resp = await foods_routes.select_food(body)
            latency = time.perf_counter() - t0

            assert resp.item is not None and resp.item.status == "resolved"
            assert resp.item.tier == 3
            assert resp.food_id and resp.item.food and resp.food_id == resp.item.food.id
            assert resp.item.macros is not None

            frow = await db.fetchone(
                "SELECT source, source_id, food_type FROM foods WHERE id = %s",
                (resp.food_id,),
            )
            assert frow is not None, "chosen food was not persisted"
            assert frow["source"] == "fdc"
            assert frow["source_id"] == QUEST_COOKIE_DOUGH_FDC_ID
            assert frow["food_type"] == "branded"

            irow = await db.fetchone(
                "SELECT status, tier, was_disambiguated, proposed_food_id::text AS pf "
                "FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )
            assert irow["status"] == "resolved" and irow["tier"] == 3
            assert irow["was_disambiguated"] is True
            assert irow["pf"] == resp.food_id
            assert latency < 15, "select must not have run an agent"
            print(
                f"\n[select live] {latency:.1f}s food='{resp.item.food.name}' "
                f"macros={resp.item.macros.model_dump(exclude_none=True)}"
            )
        finally:
            await _cleanup_log(log_id)
            if pre_existing is None:  # only delete what this test created
                await _delete_food(await _food_row_id("fdc", QUEST_COOKIE_DOUGH_FDC_ID))
            await db.close()

    _run(scenario())


def test_select_library_only_persists_live():
    """Select without log context just caches the food (Library add)."""

    async def scenario():
        pre_existing = await _food_row_id("fdc", FOUNDATION_CHICKEN_FDC_ID)
        try:
            resp = await foods_routes.select_food(
                foods_routes.SelectRequest(
                    source="fdc", source_id=FOUNDATION_CHICKEN_FDC_ID
                )
            )
            assert resp.food_id and resp.item is None
            frow = await db.fetchone(
                "SELECT source_id, food_type FROM foods WHERE id = %s", (resp.food_id,)
            )
            assert frow is not None
            assert frow["source_id"] == FOUNDATION_CHICKEN_FDC_ID
            assert frow["food_type"] == "generic"
            print(f"\n[select library] cached food_id={resp.food_id}")
        finally:
            if pre_existing is None:
                await _delete_food(await _food_row_id("fdc", FOUNDATION_CHICKEN_FDC_ID))
            await db.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# BACKLOG 5 — fdc.get_food None (404) tolerance in tier3 (monkeypatched)
# ---------------------------------------------------------------------------


def test_get_food_cached_tolerates_none(monkeypatch):
    async def gone(source, source_id):
        return None

    monkeypatch.setattr(tier3.fdc, "get_food", gone)

    async def scenario():
        counter = {"network_calls": 0}
        cache: dict = {}
        food = await tier3._get_food_cached("fdc", "9999999", counter, cache)
        assert food is None
        assert cache == {}, "None must never be cached"
        assert counter["network_calls"] == 1

    _run(scenario())


def test_first_live_food_retries_next_candidate(monkeypatch):
    """run_tier3's resolved path: a 404 on the chosen id promotes the next
    alternate instead of crashing; all-dead lists come back (None, None)."""
    alive = _fake_food("alive food")

    async def flaky(source, source_id):
        return alive if source_id == "alive" else None

    monkeypatch.setattr(tier3.fdc, "get_food", flaky)

    async def scenario():
        counter = {"network_calls": 0}
        cands = [
            ("fdc", "dead-1", "Dead choice", "label", "high"),
            ("fdc", "alive", "Live alternate", None, "low"),
        ]
        cand, food = await tier3._first_live_food(cands, counter, {})
        assert cand == cands[1] and food is alive
        assert counter["network_calls"] == 2

        cand, food = await tier3._first_live_food(
            [("fdc", "dead-1", "x", None, "low"), ("fdc", "dead-2", "y", None, "low")],
            counter, {},
        )
        assert cand is None and food is None  # -> not_found, never a crash

    _run(scenario())


def test_resolve_item_from_source_stale_id_clean_needs_input(monkeypatch):
    """An answer-tap / select whose id 404s degrades to needs_input with the
    prior question restored — never dict(None) -> 500."""

    async def gone(source, source_id):
        return None

    monkeypatch.setattr(tier3.fdc, "get_food", gone)
    question = {
        "text": "Which brand?",
        "options": [
            {"label": "Quest", "source": "fdc", "source_id": "111"},
            {"label": "Pure Protein", "source": "fdc", "source_id": "222"},
        ],
    }

    async def scenario():
        log_id = str(uuid4())
        try:
            await _seed_item(
                log_id, "protein bar",
                staged_extra={"status": "needs_input", "question": question},
            )
            item = await tier3.resolve_item_from_source(log_id, 0, "fdc", "111")
            assert item.status == "needs_input"
            assert item.question is not None and item.question.text == "Which brand?"
            assert len(item.question.options) == 2
            assert item.food is None
            irow = await db.fetchone(
                "SELECT status, staged FROM item_resolutions "
                "WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )
            assert irow["status"] == "needs_input"
            print("\n[stale id] clean needs_input, question restored")
        finally:
            await _cleanup_log(log_id)
            await db.close()

    _run(scenario())


def test_resolve_item_from_source_stale_id_no_question_not_found(monkeypatch):
    async def gone(source, source_id):
        return None

    monkeypatch.setattr(tier3.fdc, "get_food", gone)

    async def scenario():
        log_id = str(uuid4())
        try:
            await _seed_item(log_id, "mystery snack")
            item = await tier3.resolve_item_from_source(log_id, 0, "fdc", "111")
            assert item.status == "not_found"  # manual search box (§3.4)
            assert item.question is None
        finally:
            await _cleanup_log(log_id)
            await db.close()

    _run(scenario())


# ---------------------------------------------------------------------------
# Router shape
# ---------------------------------------------------------------------------


def test_router_shape():
    paths = {r.path for r in foods_routes.router.routes}
    assert {"/api/foods/search", "/api/foods/select"} <= paths
    assert foods_routes.router.dependencies, "router missing require_user dependency"


def test_select_rejects_half_log_context():
    with pytest.raises(Exception) as e:
        _run(
            foods_routes.select_food(
                foods_routes.SelectRequest(
                    source="fdc", source_id="1", log_id=str(uuid4())
                )
            )
        )
    assert getattr(e.value, "status_code", None) == 422
