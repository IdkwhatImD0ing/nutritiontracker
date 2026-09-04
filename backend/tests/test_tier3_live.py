"""Live tests for the tier-3 search agent (Workstream D — DESIGN.md §3.4, §6.3).

These hit the real Neon DB, the real USDA FDC API, and the real OpenAI API
(gpt-5.6-terra via openai-agents). Self-cleaning: every row seeded or created
(logs, item_resolutions, foods) is deleted in a finally block.

Cross-workstream modules (db, sources.fdc, resolver.foods_repo, tracing, auth)
are being built in parallel; per the workstream contract they are STUBBED here —
registered in sys.modules before services.tier3 is imported — with minimal live
implementations that honor the shared interface signatures exactly.

Run:  uv run --with pytest pytest backend/tests/test_tier3_live.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import types
import unicodedata
from contextlib import asynccontextmanager
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":
    # psycopg async cannot run on the default ProactorEventLoop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_env() -> None:
    env_path = BACKEND.parent / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# ---------------------------------------------------------------------------
# db stub — the shared interface (fetchall/fetchone/execute/tx) over psycopg,
# one short-lived connection per call against DATABASE_URL.
# ---------------------------------------------------------------------------

import psycopg  # noqa: E402

_DB_URL = os.environ["DATABASE_URL"]


async def _db_fetchall(sql, params=()):
    async with await psycopg.AsyncConnection.connect(_DB_URL) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def _db_fetchone(sql, params=()):
    async with await psycopg.AsyncConnection.connect(_DB_URL) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def _db_execute(sql, params=()):
    # returns rowcount like db.execute — routes/resolve.py's live guard reads it
    async with await psycopg.AsyncConnection.connect(_DB_URL) as conn:
        cur = await conn.execute(sql, params)
        return cur.rowcount


@asynccontextmanager
async def _db_tx():
    conn = await psycopg.AsyncConnection.connect(_DB_URL)
    try:
        async with conn.transaction():
            yield conn
    finally:
        await conn.close()


db_stub = types.ModuleType("db")
db_stub.fetchall = _db_fetchall
db_stub.fetchone = _db_fetchone
db_stub.execute = _db_execute
db_stub.tx = _db_tx

# ---------------------------------------------------------------------------
# sources.fdc stub — minimal live FDC adapter honoring the shared interface
# (search_generic / search_branded / lookup_barcode / get_food normalized dict).
# ---------------------------------------------------------------------------

import httpx  # noqa: E402

_FDC = "https://api.nal.usda.gov/fdc/v1"
_FDC_KEY = os.environ["FDC_API_KEY"]

_COOKED_RE = re.compile(
    r"\b(cooked|roasted|grilled|boiled|braised|broiled|baked|fried|steamed|"
    r"stewed|rotisserie|microwaved|smoked|poached)\b",
    re.I,
)
_RAW_RE = re.compile(r"\braw\b", re.I)
_DRY_RE = re.compile(r"\b(dry|dried)\b", re.I)
_SENSITIVE_RE = re.compile(
    r"\b(chicken|beef|pork|turkey|lamb|veal|meat|fish|salmon|tuna|cod|tilapia|"
    r"shrimp|rice|pasta|noodles?|spaghetti|macaroni|lentils?|beans?|chickpeas?|legumes?)\b",
    re.I,
)
_NUTRIENTS = {1008: "kcal", 1003: "protein_g", 1005: "carb_g", 1004: "fat_g", 1079: "fiber_g"}


async def _fdc_get(path, **params):
    params["api_key"] = _FDC_KEY
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.get(f"{_FDC}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def _fdc_search_generic(query):
    d = await _fdc_get(
        "/foods/search", query=query, dataType="SR Legacy,Foundation", pageSize=12
    )
    out = []
    for f in d.get("foods", []):
        prot = next(
            (n.get("value") for n in f.get("foodNutrients", []) if n.get("nutrientId") == 1003),
            None,
        )
        out.append(
            {
                "fdc_id": f["fdcId"],
                "description": f["description"],
                "data_type": f["dataType"],
                "protein_per_100g": prot,
            }
        )
    return out


async def _fdc_search_branded(query):
    d = await _fdc_get("/foods/search", query=query, dataType="Branded", pageSize=12)
    return [
        {
            "fdc_id": f["fdcId"],
            "description": f["description"],
            "data_type": f["dataType"],
            "brand_owner": f.get("brandOwner"),
            "brand_name": f.get("brandName"),
            "serving_size": f.get("servingSize"),
            "serving_size_unit": f.get("servingSizeUnit"),
        }
        for f in d.get("foods", [])
    ]


async def _fdc_lookup_barcode(code):  # interface completeness; unused in these tests
    for candidate in (code, code.lstrip("0"), code.zfill(13), code.zfill(14)):
        d = await _fdc_get("/foods/search", query=f"gtinUpc:{candidate}", dataType="Branded", pageSize=1)
        foods = d.get("foods", [])
        if foods:
            return await _fdc_get_food("fdc", str(foods[0]["fdcId"]))
    return None


def _parse_state(name: str):
    if _RAW_RE.search(name):
        return "raw"
    if _COOKED_RE.search(name):
        return "cooked"
    if _DRY_RE.search(name):
        return "dry"
    return None


def _per100_from(food_nutrients):
    m = {}
    for fn in food_nutrients or []:
        nid = (fn.get("nutrient") or {}).get("id") or fn.get("nutrientId")
        amount = fn.get("amount")
        if amount is None:  # skip null-amount category-header rows
            continue
        key = _NUTRIENTS.get(nid)
        if key and key not in m:
            m[key] = round(float(amount), 2)
    if "kcal" not in m:  # Atwater fallback for Foundation foods
        for fn in food_nutrients or []:
            nid = (fn.get("nutrient") or {}).get("id") or fn.get("nutrientId")
            if nid in (2047, 2048) and fn.get("amount") is not None:
                m["kcal"] = round(float(fn["amount"]), 2)
                break
    return m


async def _fdc_get_food(source, source_id):
    data = await _fdc_get(f"/food/{source_id}")
    name = data.get("description", "")
    data_type = data.get("dataType", "")
    branded = data_type == "Branded"
    per100 = _per100_from(data.get("foodNutrients"))
    servings = []
    if branded:
        label = data.get("labelNutrients") or {}
        lm = {
            "kcal": (label.get("calories") or {}).get("value"),
            "protein_g": (label.get("protein") or {}).get("value"),
            "carb_g": (label.get("carbohydrates") or {}).get("value"),
            "fat_g": (label.get("fat") or {}).get("value"),
            "fiber_g": (label.get("fiber") or {}).get("value"),
        }
        lm = {k: round(float(v), 2) for k, v in lm.items() if v is not None}
        ss, ssu = data.get("servingSize"), (data.get("servingSizeUnit") or "").strip()
        unit = {"GRM": "g", "MLT": "ml", "OZA": "fl oz", "g": "g", "ml": "ml", "MC": "mcg"}.get(
            ssu, ssu
        )
        grams = float(ss) if ss is not None and unit in ("g", "ml") else None
        if {"kcal", "protein_g", "carb_g", "fat_g"} <= set(lm):
            desc = f"1 serving ({ss} {unit})" if ss is not None else "1 serving"
            hh = (data.get("householdServingFullText") or "").strip()
            if hh:
                desc = f"{hh} ({ss} {unit})" if ss is not None else hh
            servings.append(
                {"serving_id": "label", "description": desc, "grams": grams, "macros": lm}
            )
        if {"kcal", "protein_g", "carb_g", "fat_g"} <= set(per100):
            servings.append(
                {"serving_id": "100g", "description": "100 g", "grams": 100.0, "macros": per100}
            )
        default_serving_id = servings[0]["serving_id"] if servings else "label"
    else:
        if {"kcal", "protein_g", "carb_g", "fat_g"} <= set(per100):
            servings.append(
                {"serving_id": "100g", "description": "100 g", "grams": 100.0, "macros": per100}
            )
        for i, p in enumerate(data.get("foodPortions") or []):
            gw = p.get("gramWeight")
            if not gw:
                continue
            mu = ((p.get("measureUnit") or {}).get("name") or "").strip()
            parts = [
                str(p.get("amount") or "").rstrip("0").rstrip("."),
                mu if mu and mu != "undetermined" else "",
                (p.get("modifier") or "").strip(),
                (p.get("portionDescription") or "").strip(),
            ]
            desc = " ".join(x for x in parts if x) or f"{gw} g"
            servings.append(
                {
                    "serving_id": f"p{p.get('id', i)}",
                    "description": desc,
                    "grams": float(gw),
                    "macros": {k: round(v * float(gw) / 100, 2) for k, v in per100.items()},
                }
            )
        default_serving_id = "100g" if servings else "100g"
    if not servings:
        raise ValueError(f"fdc {source_id}: no usable macros")
    return {
        "source": "fdc",
        "source_id": str(source_id),
        "name": name,
        "display_name": name[:80],
        "brand": data.get("brandName") or data.get("brandOwner"),
        "food_type": "branded" if branded else "generic",
        "state": _parse_state(name),
        "state_sensitive": bool(_SENSITIVE_RE.search(name)),
        "servings": servings,
        "default_serving_id": default_serving_id,
        "per_100g": per100 or None,
        "payload": data,
    }


sources_pkg = types.ModuleType("sources")
sources_pkg.__path__ = []  # package marker
fdc_stub = types.ModuleType("sources.fdc")
fdc_stub.search_generic = _fdc_search_generic
fdc_stub.search_branded = _fdc_search_branded
fdc_stub.lookup_barcode = _fdc_lookup_barcode
fdc_stub.get_food = _fdc_get_food
sources_pkg.fdc = fdc_stub

# ---------------------------------------------------------------------------
# resolver.foods_repo stub — live upsert on (source, source_id); tracks created
# ids for cleanup.
# ---------------------------------------------------------------------------

CREATED_FOOD_IDS: list[str] = []


async def _persist_food(food: dict) -> str:
    row = await _db_fetchone(
        """
        INSERT INTO foods (source, source_id, source_url, provenance, name, display_name,
                           brand, food_type, state, state_sensitive, servings,
                           default_serving_id, per_100g, payload)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (source, source_id) DO UPDATE
        SET name = EXCLUDED.name, display_name = EXCLUDED.display_name,
            brand = EXCLUDED.brand, food_type = EXCLUDED.food_type,
            state = EXCLUDED.state, state_sensitive = EXCLUDED.state_sensitive,
            servings = EXCLUDED.servings, default_serving_id = EXCLUDED.default_serving_id,
            per_100g = EXCLUDED.per_100g, payload = EXCLUDED.payload, refreshed_at = now()
        RETURNING id
        """,
        (
            food["source"],
            str(food["source_id"]),
            food.get("source_url"),
            json.dumps(food["provenance"]) if food.get("provenance") else None,
            food["name"],
            food["display_name"],
            food.get("brand"),
            food["food_type"],
            food.get("state"),
            bool(food.get("state_sensitive")),
            json.dumps(food["servings"]),
            food["default_serving_id"],
            json.dumps(food["per_100g"]) if food.get("per_100g") else None,
            json.dumps(food.get("payload") or {}),
        ),
    )
    fid = str(row[0])
    if fid not in CREATED_FOOD_IDS:
        CREATED_FOOD_IDS.append(fid)
    return fid


def _apply_label(food: dict, display_name: str | None) -> None:
    """The real rule for the owner (these live runs are the owner's)."""
    if display_name:
        food["display_name"] = display_name
    elif not food.get("display_name"):
        food["display_name"] = food.get("name", "")


resolver_pkg = types.ModuleType("resolver")
resolver_pkg.__path__ = []
foods_repo_stub = types.ModuleType("resolver.foods_repo")
foods_repo_stub.persist_food = _persist_food
foods_repo_stub.apply_label = _apply_label
resolver_pkg.foods_repo = foods_repo_stub

# ---------------------------------------------------------------------------
# tracing + auth stubs (no-op, interface-shaped)
# ---------------------------------------------------------------------------

tracing_stub = types.ModuleType("tracing")
tracing_stub.init_tracing = lambda: None
tracing_stub.flush = lambda: None
tracing_stub.score_commit = lambda trace_id, name, value, comment=None: None


@asynccontextmanager
async def _generation(name):
    yield SimpleNamespace(trace_id=f"test-{uuid4().hex}", update=lambda **kw: None)


tracing_stub.generation = _generation

auth_stub = types.ModuleType("auth")


async def _require_user():
    return {"email": "test@example.com"}


auth_stub.require_user = _require_user
auth_stub.is_owner = lambda: True  # the web lane and labels are the owner's

# ---------------------------------------------------------------------------
# Register stubs, then import the modules under test through synthetic packages
# (real __path__ for services/ and routes/ so tier3.py / resolve.py load from
# disk while any parallel-workstream __init__ side effects are bypassed).
# ---------------------------------------------------------------------------

_SWAPPED = (
    "db", "sources", "sources.fdc", "resolver", "resolver.foods_repo",
    "tracing", "auth", "services", "services.tier3", "routes", "routes.resolve",
)
_saved_modules = {n: sys.modules.get(n) for n in _SWAPPED}

sys.modules["db"] = db_stub
sys.modules["sources"] = sources_pkg
sys.modules["sources.fdc"] = fdc_stub
sys.modules["resolver"] = resolver_pkg
sys.modules["resolver.foods_repo"] = foods_repo_stub
sys.modules["tracing"] = tracing_stub
sys.modules["auth"] = auth_stub

services_pkg = types.ModuleType("services")
services_pkg.__path__ = [str(BACKEND / "services")]
sys.modules["services"] = services_pkg
routes_pkg = types.ModuleType("routes")
routes_pkg.__path__ = [str(BACKEND / "routes")]
sys.modules["routes"] = routes_pkg

# Force a fresh load from disk against THIS file's stubs — another test file
# may already have placed a (stub or real) module under these names.
sys.modules.pop("services.tier3", None)
sys.modules.pop("routes.resolve", None)

import services.tier3 as tier3  # noqa: E402
import routes.resolve as resolve_routes  # noqa: E402

# Integration fix: restore sys.modules immediately — tier3/resolve_routes hold
# the stub bindings they need, and leaving the stubs installed poisons every
# other test file collected or run after this one.
for _n, _m in _saved_modules.items():
    if _m is None:
        sys.modules.pop(_n, None)
    else:
        sys.modules[_n] = _m

# ---------------------------------------------------------------------------
# Seed / cleanup helpers
# ---------------------------------------------------------------------------


async def _seed(log_id: str, phrase: str, meal: str = "lunch", qty: float = 1) -> None:
    await _db_execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at) VALUES (%s, 'text', %s, now())",
        (log_id, phrase),
    )
    staged = {
        "item_index": 0,
        "phrase": phrase,
        "status": "searching",
        "meal": meal,
        "qty": qty,
    }
    await _db_execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status, staged)
        VALUES (%s, 0, %s, %s, 'searching', %s::jsonb)
        """,
        (log_id, phrase, phrase.lower(), json.dumps(staged)),
    )


async def _cleanup(log_id: str, foods_created: list[str]) -> None:
    try:
        await _db_execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
        await _db_execute("DELETE FROM logs WHERE id = %s", (log_id,))
    except Exception as e:  # pragma: no cover
        print(f"[cleanup] log rows: {e}")
    for fid in foods_created:
        try:
            await _db_execute("DELETE FROM foods WHERE id = %s", (fid,))
        except Exception as e:  # referenced elsewhere — leave it, report
            print(f"[cleanup] foods {fid}: {e}")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_grilled_chicken_breast_resolves_sr_legacy_cooked():
    async def scenario():
        log_id = str(uuid4())
        foods_before = len(CREATED_FOOD_IDS)
        await _seed(log_id, "grilled chicken breast")
        try:
            t0 = time.perf_counter()
            item = await tier3.run_tier3(log_id, 0)
            latency = time.perf_counter() - t0
            print(
                f"\n[tier3 live] 'grilled chicken breast': {latency:.1f}s "
                f"status={item.status} confidence={item.confidence}"
            )

            assert item.status == "resolved", f"expected resolved, got {item.status}"
            assert item.tier == 3
            assert item.food is not None and item.serving is not None
            assert item.macros is not None

            frow = await _db_fetchone(
                """
                SELECT source, food_type, state, state_sensitive,
                       payload->>'dataType', display_name, name
                FROM foods WHERE id = %s
                """,
                (item.food.id,),
            )
            assert frow is not None, "chosen food was not persisted to foods"
            assert frow[0] == "fdc"
            assert frow[1] == "generic"
            assert frow[4] == "SR Legacy", f"expected SR Legacy, got {frow[4]}: {frow[6]}"
            # state handling: cooked entry, not raw, and 'grilled' in the phrase
            # disambiguates intent so no cooked/raw question is asked
            assert frow[2] == "cooked", f"expected cooked state, got {frow[2]}: {frow[6]}"
            assert "raw" not in (frow[6] or "").lower()
            assert frow[3] is True  # chicken is state-sensitive
            assert not item.state_question
            assert "chicken" in (frow[6] or "").lower()
            # cooked chicken breast: protein-dense
            assert item.macros.protein_g > 10, f"macros look wrong: {item.macros}"

            irow = await _db_fetchone(
                """
                SELECT status, tier, proposed_food_id::text, proposed_serving_id,
                       trace_id, agent_transcript, resolve_ms, network_calls
                FROM item_resolutions WHERE log_id = %s AND item_index = 0
                """,
                (log_id,),
            )
            assert irow[0] == "resolved" and irow[1] == 3
            assert irow[2] == item.food.id
            assert irow[3] == item.serving.id
            assert irow[4], "trace_id not stamped"
            assert irow[5], "agent_transcript not archived"
            assert irow[6] and irow[6] > 0
            assert irow[7] >= 1, "network_calls not recorded"
            print(
                f"[tier3 live] chicken: food='{frow[5]}' serving='{item.serving.description}' "
                f"macros={item.macros.model_dump(exclude_none=True)} "
                f"resolve_ms={irow[6]} network_calls={irow[7]}"
            )
        finally:
            await _cleanup(log_id, CREATED_FOOD_IDS[foods_before:])

    _run(scenario())


def test_quest_bar_branded_or_question():
    async def scenario():
        log_id = str(uuid4())
        foods_before = len(CREATED_FOOD_IDS)
        await _seed(log_id, "quest bar", meal="snack")
        try:
            t0 = time.perf_counter()
            item = await tier3.run_tier3(log_id, 0)
            latency = time.perf_counter() - t0
            print(
                f"\n[tier3 live] 'quest bar': {latency:.1f}s "
                f"status={item.status} confidence={item.confidence}"
            )

            assert item.status in ("resolved", "needs_input"), item.status
            assert item.tier == 3

            if item.status == "resolved":
                assert item.food is not None and item.macros is not None
                frow = await _db_fetchone(
                    """
                    SELECT source, food_type, brand, payload->>'brandOwner', name
                    FROM foods WHERE id = %s
                    """,
                    (item.food.id,),
                )
                assert frow is not None, "chosen food was not persisted to foods"
                assert frow[0] == "fdc"
                assert frow[1] == "branded", f"expected branded, got {frow[1]}: {frow[4]}"
                haystack = f"{frow[2] or ''} {frow[3] or ''}".lower()
                assert "quest" in haystack, f"brand/brandOwner missing Quest: {haystack!r}"
                print(
                    f"[tier3 live] quest: food='{frow[4]}' brand='{frow[2]}' "
                    f"owner='{frow[3]}' macros={item.macros.model_dump(exclude_none=True)}"
                )
            else:
                assert item.question is not None, "needs_input without a question"
                assert len(item.question.options) >= 2
                id_bearing = [
                    o for o in item.question.options if o.source and o.source_id
                ]
                assert id_bearing, "question options carry no (source, source_id) ids"
                print(
                    f"[tier3 live] quest question: '{item.question.text}' "
                    f"options={[(o.label, o.source, o.source_id) for o in item.question.options]}"
                )

            irow = await _db_fetchone(
                "SELECT status, tier, trace_id, agent_transcript FROM item_resolutions "
                "WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )
            assert irow[0] == item.status and irow[1] == 3
            assert irow[2] and irow[3]
        finally:
            await _cleanup(log_id, CREATED_FOOD_IDS[foods_before:])

    _run(scenario())


def test_answer_id_option_resolves_locally_without_agent():
    """§5 /api/resolve/answer: a tapped id-bearing option = one get_food + persist,
    no second agent run (fast — FDC latency only)."""

    async def scenario():
        log_id = str(uuid4())
        foods_before = len(CREATED_FOOD_IDS)
        await _seed(log_id, "quest bar", meal="snack")
        try:
            body = resolve_routes.AnswerRequest(
                log_id=log_id,
                item_index=0,
                answer=resolve_routes.Answer(
                    label="Quest chocolate chip cookie dough",
                    source="fdc",
                    source_id="2629232",
                ),
            )
            t0 = time.perf_counter()
            item = await resolve_routes.answer_question(body)
            latency = time.perf_counter() - t0
            print(f"\n[answer live] id-option: {latency:.1f}s status={item.status}")

            assert item.status == "resolved" and item.tier == 3
            assert item.food is not None and item.food.name == "Quest chocolate chip cookie dough"
            assert item.macros is not None
            frow = await _db_fetchone(
                "SELECT source, source_id, food_type FROM foods WHERE id = %s",
                (item.food.id,),
            )
            assert frow == ("fdc", "2629232", "branded")
            irow = await _db_fetchone(
                "SELECT status, tier, was_disambiguated, proposed_food_id::text "
                "FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )
            assert irow[0] == "resolved" and irow[1] == 3
            assert irow[2] is True
            assert irow[3] == item.food.id
            assert latency < 15, "local resolve should not have run an agent"
        finally:
            await _cleanup(log_id, CREATED_FOOD_IDS[foods_before:])

    _run(scenario())


def test_resolve_edited_phrase_hits_tier1():
    """§5 POST /api/resolve (M0): an edited phrase with an exact alias match
    resolves tier 1 locally and writes the item row."""

    async def scenario():
        log_id = str(uuid4())
        alias_norm = f"protein shake test {uuid4().hex[:8]}"
        food_id = None
        alias_id = None
        try:
            await _db_execute(
                "INSERT INTO logs (id, input_mode, raw_input, captured_at) "
                "VALUES (%s, 'text', %s, now())",
                (log_id, alias_norm),
            )
            food_id = await _persist_food(
                {
                    "source": "fdc",
                    "source_id": f"test-{uuid4().hex[:12]}",
                    "name": "Core Power Elite chocolate 42g",
                    "display_name": "Core Power Elite chocolate",
                    "brand": "Fairlife",
                    "food_type": "branded",
                    "state": None,
                    "state_sensitive": False,
                    "servings": [
                        {
                            "serving_id": "bottle",
                            "description": "1 bottle (414 ml)",
                            "grams": 414.0,
                            "macros": {"kcal": 230.0, "protein_g": 42.0, "carb_g": 6.0, "fat_g": 4.5},
                        }
                    ],
                    "default_serving_id": "bottle",
                    "per_100g": None,
                    "payload": {"test": True},
                }
            )
            arow = await _db_fetchone(
                "INSERT INTO aliases (alias_norm, food_id, default_serving_id, default_qty, origin) "
                "VALUES (%s, %s, 'bottle', 1, 'manual') RETURNING id",
                (alias_norm, food_id),
            )
            alias_id = str(arow[0])

            body = resolve_routes.ResolveRequest(
                log_id=log_id,
                item_index=0,
                phrase=f"my {alias_norm.title()}!",  # norm() must strip this back down
            )
            t0 = time.perf_counter()
            item = await resolve_routes.resolve_item(body)
            latency = time.perf_counter() - t0
            print(f"\n[resolve live] tier1 edited phrase: {latency:.1f}s tier={item.tier}")

            assert item.status == "resolved" and item.tier == 1
            assert item.food is not None and item.food.id == food_id
            assert item.serving is not None and item.serving.id == "bottle"
            assert item.macros is not None and item.macros.protein_g == 42.0
            irow = await _db_fetchone(
                "SELECT status, tier, alias_id::text, was_edited, phrase_norm "
                "FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )
            assert irow[0] == "resolved" and irow[1] == 1
            assert irow[2] == alias_id
            assert irow[4] == alias_norm
        finally:
            await _db_execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
            await _db_execute("DELETE FROM logs WHERE id = %s", (log_id,))
            if alias_id:
                await _db_execute("DELETE FROM aliases WHERE id = %s", (alias_id,))
            if food_id:
                await _db_execute("DELETE FROM foods WHERE id = %s", (food_id,))

    _run(scenario())


def test_router_shape():
    paths = {r.path for r in resolve_routes.router.routes}
    assert {"/api/resolve", "/api/resolve/tier3", "/api/resolve/answer"} <= paths
    assert resolve_routes.router.dependencies, "router missing require_user dependency"
