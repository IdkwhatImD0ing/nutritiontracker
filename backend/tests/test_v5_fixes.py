"""V5 tests — small backend fixes (BACKLOG 24, 28, 30-origin).

1. BACKLOG 24 (§5): POST /api/resolve runs the single-item decompose
   micro-call so tier 2b works for edited text — fires for novel phrases,
   skipped for exact alias matches, degrades to tiers 1/2a on LLM failure.
2. BACKLOG 28 (§3.5): entries qty-PATCH scales from a base frozen in the
   macros jsonb sidecar — repeated edits are round-trip exact; lossless
   scales keep the stored snapshot clean (no sidecar keys).
3. BACKLOG 30 origin half (§2): _check_origin rejects localhost origins when
   VERCEL_ENV=production; the dev allowance survives everywhere else.

DB tests run inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one force-rollback connection) — nothing persists.
Cross-workstream modules (tracing, services.*) are stubbed in sys.modules per
the workstream contract; auth is REAL (the origin tests exercise it).

Run:  uv run --with pytest pytest backend/tests/test_v5_fixes.py -q
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
import types
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Json


# ---------------------------------------------------------------------------
# stubs for parallel workstreams — tracing + services; auth stays REAL
# ---------------------------------------------------------------------------


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

    services_mod = types.ModuleType("services")
    services_mod.__path__ = []

    ctx_mod = types.ModuleType("services.context")

    async def build_agent_context():
        return SimpleNamespace(profile_notes="", targets={}, food_index=[], meal_names=[])

    ctx_mod.build_agent_context = build_agent_context

    dec_mod = types.ModuleType("services.decompose")

    async def decompose(text, ctx):
        raise AssertionError("test must install its own decompose fake")

    dec_mod.decompose = decompose

    t3_mod = types.ModuleType("services.tier3")

    async def run_tier3(log_id, item_index, refinement=None):
        raise AssertionError("tier 3 must not be reached by these tests")

    t3_mod.run_tier3 = run_tier3

    services_mod.context = ctx_mod
    services_mod.decompose = dec_mod
    services_mod.tier3 = t3_mod
    return {
        "tracing": tracing_mod,
        "services": services_mod,
        "services.context": ctx_mod,
        "services.decompose": dec_mod,
        "services.tier3": t3_mod,
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


import auth  # noqa: E402  (real module — origin tests need it)
import db  # noqa: E402
from tenancy_harness import bind_test_tenant  # noqa: E402
from models import ParsedItem, ParsedLog, ResolveRequest  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes.entries import EntryPatch, patch_entry  # noqa: E402

SCOOP = {
    "serving_id": "srv-scoop",
    "description": "1 scoop",
    "grams": 31,
    "macros": {"kcal": 120, "protein_g": 25, "carb_g": 3, "fat_g": 1.5},
}
HUNDRED_G = {
    "serving_id": "srv-100g",
    "description": "100 g",
    "grams": 100,
    "macros": {"kcal": 387, "protein_g": 80.6, "carb_g": 9.7, "fat_g": 4.8},
}


# ---------------------------------------------------------------------------
# rollback harness — same shape as test_tier2.py
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


@asynccontextmanager
async def resolve_routes():
    """routes.resolve binds services.tier3 at import time; import it fresh
    under this module's stubs and drop it afterwards so other test files
    re-import it against the real tier3."""
    sys.modules.pop("routes.resolve", None)
    import routes.resolve as mod

    try:
        yield mod
    finally:
        sys.modules.pop("routes.resolve", None)


@contextmanager
def llm(fake_decompose=None, food_index=None):
    """Install a per-test decompose fake and/or agent-context food index."""
    dec = sys.modules["services.decompose"]
    ctx = sys.modules["services.context"]
    saved_dec, saved_ctx = dec.decompose, ctx.build_agent_context
    if fake_decompose is not None:
        dec.decompose = fake_decompose
    if food_index is not None:

        async def build_agent_context():
            return SimpleNamespace(
                profile_notes="", targets={}, food_index=food_index, meal_names=[]
            )

        ctx.build_agent_context = build_agent_context
    try:
        yield
    finally:
        dec.decompose, ctx.build_agent_context = saved_dec, saved_ctx


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def tok() -> str:
    return "zx" + uuid4().hex[:10]


async def insert_food(conn, name, servings=None, default_serving="srv-scoop", brand=None):
    servings = servings if servings is not None else [SCOOP, HUNDRED_G]
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, brand, food_type,
                           servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, %s, 'branded', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, brand, Json(servings), default_serving),
    )
    return str((await cur.fetchone())["id"])


async def insert_alias(conn, alias_norm, food_id):
    await conn.execute(
        "INSERT INTO aliases (alias_norm, food_id, use_count, last_used_at, origin) "
        "VALUES (%s, %s, 1, now(), 'manual')",
        (alias_norm, food_id),
    )


async def insert_dummy_log(conn, status="staged"):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status) "
        "VALUES (%s, 'text', 'x', now(), %s)",
        (log_id, status),
    )
    return log_id


def one_item_log(item: ParsedItem) -> ParsedLog:
    return ParsedLog(items=[item], meal_hint=None, when_hint=None, save_intent=None)


def food_item(phrase, qty=None, unit=None, library_match=None, kind="food") -> ParsedItem:
    return ParsedItem(
        phrase=phrase, phrase_kind=kind, qty=qty, unit=unit, state=None,
        brand=None, library_match=library_match, meal_ref=None,
    )


# ---------------------------------------------------------------------------
# 1. BACKLOG 24 — /api/resolve micro-decompose (§5 tier-2b parity)
# ---------------------------------------------------------------------------


def test_micro_decompose_fires_for_novel_text():
    """Novel edited text runs the micro-call; its library_match resolves the
    item at tier 2 (2b), and its parsed qty becomes the spoken qty."""

    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                t = tok()
                food = await insert_food(conn, "Core Power Elite", brand="Fairlife")
                log_id = await insert_dummy_log(conn)
                calls: list[str] = []

                async def fake_decompose(text, ctx):
                    calls.append(text)
                    return one_item_log(
                        food_item(f"{t} shake", qty=2.0, library_match=food)
                    )

                with llm(fake_decompose, food_index=[(food, "Core Power Elite", "Fairlife")]):
                    item = await rr.resolve_item(
                        ResolveRequest(log_id=log_id, item_index=0, phrase=f"2 {t} shakes")
                    )

                assert calls == [f"2 {t} shakes"]  # micro-call fired, raw text in
                assert item.status == "resolved" and item.tier == 2
                assert item.food.id == food  # 2b library match, not the network
                assert item.confidence == "low"  # §3.3 rule 2: 2b alone = verify me
                assert item.qty == 2  # decompose-parsed qty applied (§7.3 rule 1)
                assert item.macros.kcal == 240  # 2 x 120

                row = await db.fetchone(
                    "SELECT phrase, phrase_norm, tier, status, staged "
                    "FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                    (log_id,),
                )
                # new rows store the decompose-CLEANED phrase (staging parity)
                assert row["phrase"] == f"{t} shake"
                assert row["phrase_norm"] == norm(f"{t} shake")
                assert row["tier"] == 2 and row["status"] == "resolved"
                assert row["staged"]["spoken_qty"] == 2

    asyncio.run(run())


def test_micro_decompose_skipped_for_exact_alias():
    """A phrase that exact-matches an alias resolves at tier 1 with ZERO model
    calls — the micro-call would be pure latency."""

    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                phrase = f"{tok()} bar"
                food = await insert_food(conn, "Quest Bar")
                await insert_alias(conn, phrase, food)
                log_id = await insert_dummy_log(conn)
                calls: list[str] = []

                async def fake_decompose(text, ctx):
                    calls.append(text)
                    return one_item_log(food_item(text))

                with llm(fake_decompose):
                    item = await rr.resolve_item(
                        ResolveRequest(log_id=log_id, item_index=0, phrase=phrase)
                    )

                assert calls == []  # skipped: exact alias match
                assert item.status == "resolved" and item.tier == 1
                assert item.food.id == food

    asyncio.run(run())


def test_micro_decompose_outage_degrades_to_tier2a():
    """A decompose failure must not break /api/resolve — tiers 1/2a still run
    (the pre-BACKLOG-24 behavior is the floor, not a regression)."""

    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                t = tok()
                food = await insert_food(conn, "Core Power Elite", brand="Fairlife")
                await insert_alias(conn, f"{t} protein shake", food)
                log_id = await insert_dummy_log(conn)
                calls: list[str] = []

                async def fake_decompose(text, ctx):
                    calls.append(text)
                    raise RuntimeError("LLM outage")

                with llm(fake_decompose):
                    item = await rr.resolve_item(
                        ResolveRequest(
                            log_id=log_id, item_index=0, phrase=f"{t} protein shakes"
                        )
                    )

                assert calls  # the micro-call was attempted...
                assert item.status == "resolved" and item.tier == 2  # ...and 2a resolved
                assert item.food.id == food

    asyncio.run(run())


def test_micro_decompose_meal_ref_never_enters_food_search():
    """§3.1 guard: when the micro-call tags the text as a meal_ref with no
    matching macro, the row becomes needs_input — never a tier-3 kick (the
    tier3 stub raises if touched)."""

    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                log_id = await insert_dummy_log(conn)
                name = f"usual {tok()}"

                async def fake_decompose(text, ctx):
                    return one_item_log(food_item(name, kind="meal_ref"))

                with llm(fake_decompose):
                    item = await rr.resolve_item(
                        ResolveRequest(log_id=log_id, item_index=0, phrase=name)
                    )

                assert item.status == "needs_input"
                assert "No saved meal" in item.question.text
                row = await db.fetchone(
                    "SELECT status, staged FROM item_resolutions "
                    "WHERE log_id = %s AND item_index = 0",
                    (log_id,),
                )
                assert row["status"] == "needs_input"
                assert row["staged"]["status"] == "needs_input"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. BACKLOG 28 — qty-PATCH scales from a stored base (round-trip exact)
# ---------------------------------------------------------------------------

# Odd-cent macros: scaling 2 -> 3 lands on .xx5 values, so the naive
# re-round-the-rounded-snapshot approach loses information immediately.
LOSSY_MACROS = {"kcal": 232.65, "protein_g": 46.95, "carb_g": 22.25, "fat_g": 6.65}


async def insert_manual_entry(conn, food_id, qty, macros, grams=None, serving_id="srv-scoop"):
    cur = await conn.execute(
        """
        INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, grams, macros)
        VALUES (now(), 'snack', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (food_id, serving_id, qty, grams, Json(macros)),
    )
    return (await cur.fetchone())["id"]


def split_base(stored: dict) -> tuple[dict, dict]:
    return (
        {k: v for k, v in stored.items() if not k.startswith("base_")},
        {k: v for k, v in stored.items() if k.startswith("base_")},
    )


def test_qty_patch_round_trip_byte_identical():
    async def run():
        async with isolated_db() as conn:
            food = await insert_food(conn, "Round Trip Food")
            entry_id = await insert_manual_entry(
                conn, food, qty=2, macros=dict(LOSSY_MACROS), grams=67.05
            )

            # 2 -> 3: lossy scale — the pre-edit snapshot freezes as the base
            resp = await patch_entry(entry_id, EntryPatch(qty=3))
            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry_id,))
            display, base = split_base(row["macros"])
            assert base["base_qty"] == 2
            assert base["base_kcal"] == 232.65 and base["base_grams"] == 67.05
            assert display == {
                k: round(v * 1.5, 2) for k, v in LOSSY_MACROS.items()
            }
            assert resp.entry.macros.kcal == display["kcal"]  # 2dp at render

            # 3 -> 7 -> 2: every hop scales from the base, so returning to the
            # base qty reproduces the ORIGINAL macros byte-for-byte
            await patch_entry(entry_id, EntryPatch(qty=7))
            resp = await patch_entry(entry_id, EntryPatch(qty=2))
            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry_id,))
            display, base = split_base(row["macros"])
            assert display == LOSSY_MACROS  # byte-identical round trip
            assert float(row["grams"]) == 67.05
            assert base["base_qty"] == 2  # base survives, un-refrozen
            assert resp.entry.macros.model_dump(exclude_none=True) == LOSSY_MACROS

    asyncio.run(run())


def test_lossless_qty_patch_keeps_snapshot_clean():
    """When nothing rounds (e.g. a doubling), the stored snapshot is still an
    exact multiple of the commit snapshot — no sidecar keys appear (which is
    also what keeps test_commit_hardening's exact-dict assertions green)."""

    async def run():
        async with isolated_db() as conn:
            food = await insert_food(conn, "Clean Double Food")
            entry_id = await insert_manual_entry(
                conn, food, qty=1,
                macros={"kcal": 120, "protein_g": 25, "carb_g": 3, "fat_g": 1.5},
                grams=31,
            )
            resp = await patch_entry(entry_id, EntryPatch(qty=2))
            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry_id,))
            assert row["macros"] == {"kcal": 240, "protein_g": 50, "carb_g": 6, "fat_g": 3}
            assert float(row["grams"]) == 62
            assert resp.entry.macros.kcal == 240

    asyncio.run(run())


def test_serving_change_resets_base():
    """A serving change re-derives from the foods cache — the stale base must
    not survive it (the next qty edit re-freezes from the new snapshot)."""

    async def run():
        async with isolated_db() as conn:
            food = await insert_food(conn, "Reset Food")
            entry_id = await insert_manual_entry(
                conn, food, qty=2, macros=dict(LOSSY_MACROS), grams=67.05
            )
            await patch_entry(entry_id, EntryPatch(qty=3))  # freezes a base
            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry_id,))
            assert split_base(row["macros"])[1]  # base present

            await patch_entry(entry_id, EntryPatch(serving_id="srv-100g"))
            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry_id,))
            display, base = split_base(row["macros"])
            assert base == {}  # reset
            assert display["kcal"] == round(387 * 3, 2)  # re-derived from cache

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. BACKLOG 30 (origin half) — localhost origins rejected in production
# ---------------------------------------------------------------------------

BASE = "https://auth.example.test"


def _keypair(kid: str):
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
        "kid": kid,
        "alg": "EdDSA",
    }
    return priv, jwk


def _origin_client(monkeypatch):
    """TestClient over a minimal app using the REAL require_user, JWKS stubbed."""
    monkeypatch.setenv("NEON_AUTH_BASE_URL", BASE)
    for name in ("ALLOWED_EMAIL", "APP_ORIGIN", "VERCEL_PROJECT_PRODUCTION_URL",
                 "VERCEL_URL", "VERCEL_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_dotenv_loaded", True)  # keep .env out of _env()
    monkeypatch.setattr(auth, "_last_failed_fetch", float("-inf"))
    auth._keys.clear()

    priv, jwk = _keypair("kid-v5")

    async def fake_fetch():
        return {"keys": [jwk]}

    monkeypatch.setattr(auth, "_fetch_jwks", fake_fetch)

    now = int(time.time())
    token = jwt.encode(
        {"sub": "u", "email": auth.DEFAULT_ALLOWED_EMAIL, "iss": BASE,
         "iat": now, "exp": now + 900},
        priv, algorithm="EdDSA", headers={"kid": "kid-v5"},
    )

    app = FastAPI()

    @app.post("/write")
    async def write(user: dict = Depends(auth.require_user)):
        return {"ok": True}

    return TestClient(app), {"Authorization": f"Bearer {token}"}


LOCALHOST_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000")


def test_origin_localhost_rejected_in_production(monkeypatch):
    client, hdr = _origin_client(monkeypatch)
    monkeypatch.setenv("VERCEL_URL", "usual-abc123.vercel.example")
    monkeypatch.setenv("VERCEL_ENV", "production")

    for origin in LOCALHOST_ORIGINS:  # BACKLOG 30: no dev allowance in prod
        r = client.post("/write", headers={**hdr, "Origin": origin})
        assert r.status_code == 403, origin
    # the real allowlist and non-browser clients are untouched
    r = client.post("/write", headers={**hdr, "Origin": "https://usual-abc123.vercel.example"})
    assert r.status_code == 200
    assert client.post("/write", headers=hdr).status_code == 200

    # explicit APP_ORIGIN in production: localhost still rejected
    monkeypatch.setenv("APP_ORIGIN", "https://app.example.net")
    r = client.post("/write", headers={**hdr, "Origin": "http://localhost:3000"})
    assert r.status_code == 403
    r = client.post("/write", headers={**hdr, "Origin": "https://app.example.net"})
    assert r.status_code == 200


def test_origin_localhost_allowed_outside_production(monkeypatch):
    client, hdr = _origin_client(monkeypatch)
    monkeypatch.setenv("VERCEL_URL", "usual-abc123.vercel.example")

    for env in ("preview", "development", None):
        if env is None:
            monkeypatch.delenv("VERCEL_ENV", raising=False)
        else:
            monkeypatch.setenv("VERCEL_ENV", env)
        r = client.post("/write", headers={**hdr, "Origin": "http://localhost:3000"})
        assert r.status_code == 200, env  # dev allowance intact
        r = client.post("/write", headers={**hdr, "Origin": "https://evil.example.com"})
        assert r.status_code == 403, env  # everything else still rejected
