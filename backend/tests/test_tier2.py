"""W1 (M1) tests — tier 2 (§3.3 rules 1–5), resolver unification (BACKLOG 6),
and §3.4a state-question alignment (BACKLOG 13), against the live Neon DB.

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one connection with force_rollback) — nothing persists.

Cross-workstream modules (auth, tracing, services.*) are stubbed in sys.modules
per the workstream contract; tier-3 and decompose stubs RAISE, proving these
paths never reach the network.

Run:  uv run --with pytest pytest backend/tests/test_tier2.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from tracing_stub import make_tracing_stub
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
from psycopg.rows import dict_row


# ---------------------------------------------------------------------------
# stubs for parallel workstreams — scoped to this module via the fixture below
# ---------------------------------------------------------------------------


def _build_stubs() -> dict:
    auth_mod = types.ModuleType("auth")

    async def require_user():
        return {"email": "billzhangsc@gmail.com"}

    auth_mod.require_user = require_user

    tracing_mod = make_tracing_stub("trace-test")

    services_mod = types.ModuleType("services")
    services_mod.__path__ = []

    ctx_mod = types.ModuleType("services.context")

    async def build_agent_context():
        return SimpleNamespace(profile_notes="", targets={}, food_index=[], meal_names=[], recent_foods=[])

    ctx_mod.build_agent_context = build_agent_context

    dec_mod = types.ModuleType("services.decompose")

    async def decompose(text, ctx):
        raise AssertionError("decompose must not be called by these tests")

    dec_mod.decompose = decompose

    t3_mod = types.ModuleType("services.tier3")

    async def run_tier3(log_id, item_index, refinement=None):
        raise AssertionError("tier 3 must not be reached by these tests")

    t3_mod.run_tier3 = run_tier3

    services_mod.context = ctx_mod
    services_mod.decompose = dec_mod
    services_mod.tier3 = t3_mod
    return {
        "auth": auth_mod,
        "tracing": tracing_mod,
        "services": services_mod,
        "services.context": ctx_mod,
        "services.decompose": dec_mod,
        "services.tier3": t3_mod,
    }


import pytest  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _workstream_stubs():
    """Install stubs for this module only, restore sys.modules afterwards."""
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


import db  # noqa: E402


from tenancy_harness import bind_test_tenant  # noqa: E402
from models import LogCreate, Meal, ParsedItem, ResolveRequest  # noqa: E402
from resolver import cascade  # noqa: E402
from routes.logs import create_log  # noqa: E402

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

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


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


async def insert_food(
    conn, name, servings=None, default_serving="srv-scoop", brand=None,
    state=None, state_sensitive=False,
):
    servings = servings if servings is not None else [SCOOP, HUNDRED_G]
    from psycopg.types.json import Json

    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, brand, food_type,
                           state, state_sensitive, servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, %s, 'branded', %s, %s, %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, brand, state, state_sensitive, Json(servings),
         default_serving),
    )
    return str((await cur.fetchone())["id"])


async def insert_alias(
    conn, alias_norm, food_id, *, use_count=1, last_used_days_ago=0.0,
    state_answer=None,
):
    cur = await conn.execute(
        """
        INSERT INTO aliases (alias_norm, food_id, use_count, last_used_at,
                             state_answer, origin)
        VALUES (%s, %s, %s, %s, %s, 'manual')
        RETURNING id
        """,
        (alias_norm, food_id, use_count, NOW() - timedelta(days=last_used_days_ago),
         state_answer),
    )
    return str((await cur.fetchone())["id"])


async def insert_committed_resolution(
    conn, log_id, item_index, phrase_norm, food_id, *,
    serving_id="srv-scoop", qty=1, minutes_ago=0, alias_id=None,
):
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                      staged, final_food_id, final_serving_id,
                                      final_qty, alias_id, committed_at)
        VALUES (%s, %s, %s, %s, 'resolved', '{}'::jsonb, %s, %s, %s, %s, %s)
        """,
        (
            log_id, item_index, phrase_norm, phrase_norm, food_id,
            serving_id, qty, alias_id, NOW() - timedelta(minutes=minutes_ago),
        ),
    )


async def insert_dummy_log(conn, status="committed"):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status) "
        "VALUES (%s, 'text', 'x', now(), %s)",
        (log_id, status),
    )
    return log_id


def parsed(phrase, qty=None, unit=None, state=None, library_match=None):
    return ParsedItem(
        phrase=phrase, phrase_kind="food", qty=qty, unit=unit, state=state,
        brand=None, library_match=library_match, meal_ref=None,
    )


def tok() -> str:
    """A 12-char unique lowercase token: shares enough trigrams with itself to
    dominate similarity, too few with any real-food alias to cross 0.55."""
    return "zx" + uuid4().hex[:10]


async def assert_sim_band(lo: float, hi: float, a: str, b: str):
    """Fixture sanity: the pair's live pg_trgm similarity is in [lo, hi)."""
    row = await db.fetchone("SELECT similarity(%s, %s) AS sim", (a, b))
    assert lo <= row["sim"] < hi, f"fixture drifted: sim({a!r},{b!r})={row['sim']}"


def log_body(text, log_id=None):
    return LogCreate(
        id=log_id or uuid4(), text=text, captured_at=NOW(), tz="America/Los_Angeles"
    )


# ---------------------------------------------------------------------------
# 1. tier 2a — trigram absorbs plural variance ("protein shakes" → "protein
#    shake" alias); strong 2a alone is §3.3 rule 2 → "verify me" (low)
# ---------------------------------------------------------------------------


def test_tier2a_trigram_plural_match():
    async def run():
        async with isolated_db() as conn:
            t = tok()
            alias_norm = f"{t} protein shake"
            query = f"{t} protein shakes"
            await assert_sim_band(0.75, 1.0, alias_norm, query)

            food = await insert_food(conn, "Core Power Elite", brand="Fairlife")
            alias_id = await insert_alias(conn, alias_norm, food)

            res = await cascade.resolve_item(parsed(query), 0, Meal.snack)
            item = res.staged
            assert item.status == "resolved"
            assert item.tier == 2
            assert item.food.id == food
            assert item.confidence == "low"  # rule 2: one strong matcher → verify me
            assert res.search_instead is False
            assert res.alias_id == alias_id  # the matched alias resolved it
            assert item.serving.id == "srv-scoop" and item.qty == 1.0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. §3.3 rule 1 — 2a and 2b agree on food_id → corroboration → high confidence
# ---------------------------------------------------------------------------


def test_tier2_rule1_corroboration():
    async def run():
        async with isolated_db() as conn:
            t = tok()
            alias_norm = f"{t} protein shake"
            query = f"{t} protein shakes"
            food = await insert_food(conn, "Core Power Elite", brand="Fairlife")
            await insert_alias(conn, alias_norm, food)

            # library_match validated against the passed index
            res = await cascade.resolve_item(
                parsed(query, library_match=food), 0, Meal.snack, index_ids={food}
            )
            assert res.staged.status == "resolved"
            assert res.staged.tier == 2
            assert res.staged.food.id == food
            assert res.staged.confidence == "high"  # two independent matchers agree
            assert res.alias_id is not None  # rule 1 resolves the ALIAS row

            # without a passed index, validation falls back to cache existence
            res2 = await cascade.resolve_item(
                parsed(query, library_match=food), 1, Meal.snack
            )
            assert res2.staged.confidence == "high"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. §3.3 rule 4 — disagreement: 2b (semantic) wins, 2a top is the alternate
# ---------------------------------------------------------------------------


def test_tier2_rule4_disagreement_prefers_2b():
    async def run():
        async with isolated_db() as conn:
            t = tok()
            alias_norm = f"{t} protein shake"
            query = f"{t} protein shakes"
            food_a = await insert_food(conn, "2% Milk", brand="Fairlife")
            food_b = await insert_food(conn, "Core Power Elite", brand="Fairlife")
            await insert_alias(conn, alias_norm, food_a)  # 2a → A

            res = await cascade.resolve_item(
                parsed(query, library_match=food_b),  # 2b → B
                0, Meal.snack, index_ids={food_a, food_b},
            )
            item = res.staged
            assert item.status == "resolved" and item.tier == 2
            assert item.food.id == food_b  # semantic beats lexical
            assert item.confidence == "low"
            assert item.alternates is not None
            assert item.alternates[0].food_id == food_a  # 2a top, one tap away
            assert res.alias_id is None  # resolved from the library food row

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. §3.3 rule 3 — only weak 2a (0.55–0.75): resolve top + search-instead
# ---------------------------------------------------------------------------


def test_tier2_rule3_weak_2a_search_instead():
    async def run():
        async with isolated_db() as conn:
            t = tok()
            alias_norm = f"{t} beef stew"
            query = f"{t} beef chop"
            await assert_sim_band(0.55, 0.75, alias_norm, query)

            food = await insert_food(conn, "Beef Stew")
            await insert_alias(conn, alias_norm, food)

            res = await cascade.resolve_item(parsed(query), 0, Meal.snack)
            item = res.staged
            assert item.status == "resolved" and item.tier == 2
            assert item.food.id == food
            assert item.confidence == "low"
            assert res.search_instead is True  # the rule-3 affordance

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. §3.3 rule 5 — neither matcher fires → tier 3; invalid 2b ids are discarded
# ---------------------------------------------------------------------------


def test_tier2_rule5_and_invalid_library_match():
    async def run():
        async with isolated_db() as conn:
            t = tok()
            nonsense = f"{t} qwvvxk"

            # neither matcher → searching (tier-3 handoff)
            res = await cascade.resolve_item(parsed(nonsense, qty=2, unit="scoop"), 0, Meal.snack)
            assert res.staged.status == "searching"
            assert res.staged.qty == 2 and res.unit == "scoop"  # §7.3 rides along

            # non-uuid library_match is discarded, never raises
            res = await cascade.resolve_item(
                parsed(nonsense, library_match="not-a-uuid"), 0, Meal.snack
            )
            assert res.staged.status == "searching"

            # a valid uuid that is no cached food is discarded (fallback mode)
            res = await cascade.resolve_item(
                parsed(nonsense, library_match=str(uuid4())), 0, Meal.snack
            )
            assert res.staged.status == "searching"

            # a real food OUTSIDE the passed index is discarded (§3.3 2b)
            food = await insert_food(conn, "Indexed Food")
            res = await cascade.resolve_item(
                parsed(nonsense, library_match=food), 0, Meal.snack,
                index_ids={str(uuid4())},
            )
            assert res.staged.status == "searching"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. §3.4a state question alignment (BACKLOG 13)
# ---------------------------------------------------------------------------


def test_state_question_alignment():
    async def run():
        async with isolated_db() as conn:
            t = tok()
            plain = f"{t} chicken"
            spoken_cooked = f"grilled {t} chicken"
            food = await insert_food(
                conn, "Chicken breast", state=None, state_sensitive=True
            )
            await insert_alias(conn, plain, food)
            await insert_alias(conn, spoken_cooked, food)

            # unresolved intent AND the entry itself is state-ambiguous → ask
            res = await cascade.resolve_item(parsed(plain), 0, Meal.snack)
            assert res.staged.state_question is True
            assert res.state_answer is None

            # §3.4a letter (Phase 2): the ENTRY's parsed state disambiguates
            # ("chicken breast, roasted") → the question is suppressed
            roasted = await insert_food(
                conn, "Chicken breast, roasted", state="cooked", state_sensitive=True
            )
            roasted_norm = f"{t} chicken dinner"
            await insert_alias(conn, roasted_norm, roasted)
            res = await cascade.resolve_item(parsed(roasted_norm), 0, Meal.snack)
            assert res.staged.state_question is None
            assert res.state_answer is None  # never fabricated from the entry

            # phrase-level intent ("grilled") resolves it — and surfaces the
            # answer for commit to persist to aliases.state_answer
            res = await cascade.resolve_item(parsed(spoken_cooked), 0, Meal.snack)
            assert res.staged.state_question is None
            assert res.state_answer == "cooked"

            # decompose-parsed state resolves it the same way
            res = await cascade.resolve_item(parsed(plain, state="raw"), 0, Meal.snack)
            assert res.staged.state_question is None
            assert res.state_answer == "raw"

            # a persisted alias answer means it is never asked again
            await conn.execute(
                "UPDATE aliases SET state_answer = 'cooked' "
                "WHERE alias_norm = %s AND food_id = %s",
                (plain, food),
            )
            res = await cascade.resolve_item(parsed(plain), 0, Meal.snack)
            assert res.staged.state_question is None

            # non-sensitive foods never ask (tier-2 path shares _stage_resolved)
            bland = await insert_food(conn, "Olive Oil")
            bland_norm = f"{t} oil blend"
            await insert_alias(conn, bland_norm, bland)
            res = await cascade.resolve_item(parsed(bland_norm), 0, Meal.snack)
            assert res.staged.state_question is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 7. BACKLOG 6 — /api/resolve honors qty AND unit through §7.3
# ---------------------------------------------------------------------------


def test_resolve_route_qty_unit_maps_serving():
    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                phrase = f"{tok()} whey"
                food = await insert_food(conn, "Whey Isolate")
                await insert_alias(conn, phrase, food)
                log_id = await insert_dummy_log(conn, status="staged")

                item = await rr.resolve_item(
                    ResolveRequest(log_id=log_id, item_index=0, phrase=phrase,
                                   qty=2, unit="scoop")
                )
                assert item.status == "resolved" and item.tier == 1
                assert item.serving.id == "srv-scoop"  # unit mapped, not defaulted
                assert item.qty == 2
                assert item.macros.kcal == 240  # 2 × 120

                row = await db.fetchone(
                    "SELECT tier, status, staged, proposed_serving_id, proposed_qty, "
                    "alias_id FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                    (log_id,),
                )
                assert row["tier"] == 1 and row["status"] == "resolved"
                assert row["proposed_serving_id"] == "srv-scoop"
                assert float(row["proposed_qty"]) == 2
                assert row["alias_id"] is not None
                assert row["staged"]["spoken_qty"] == 2
                assert row["staged"]["spoken_unit"] == "scoop"

    asyncio.run(run())


def test_resolve_route_count_without_unit_mass_only_verify_me():
    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                phrase = f"{tok()} breast"
                food = await insert_food(
                    conn, "Chicken Breast", servings=[HUNDRED_G],
                    default_serving="srv-100g",
                )
                await insert_alias(conn, phrase, food)
                log_id = await insert_dummy_log(conn, status="staged")

                item = await rr.resolve_item(
                    ResolveRequest(log_id=log_id, item_index=0, phrase=phrase, qty=2)
                )
                # §7.3: only mass servings → never silently multiplied
                assert item.serving.id == "srv-100g" and item.qty == 2
                assert item.confidence == "low"  # verify-me styling

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 8. BACKLOG 6 — pick() parity: cascade and /api/resolve agree on the winner
#    (mode-of-last-10, which the deleted _try_tier1 lacked)
# ---------------------------------------------------------------------------


def test_pick_parity_between_cascade_and_resolve():
    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                phrase = f"{tok()} bar"
                food_a = await insert_food(conn, "Quest Bar")
                food_b = await insert_food(conn, "Pure Protein Bar")
                await insert_alias(conn, phrase, food_a, use_count=50, last_used_days_ago=5)
                await insert_alias(conn, phrase, food_b, use_count=1, last_used_days_ago=2)

                # last 10 committed: 6×B (recent) vs 4×A → mode beats use_count
                dummy = await insert_dummy_log(conn)
                for i in range(6):
                    await insert_committed_resolution(
                        conn, dummy, i, phrase, food_b, minutes_ago=i
                    )
                for i in range(6, 10):
                    await insert_committed_resolution(
                        conn, dummy, i, phrase, food_a, minutes_ago=i
                    )

                # cascade path (staging via whole-utterance short-circuit)
                resp = await create_log(log_body(phrase))
                cascade_winner = resp.items[0].food.id

                # /api/resolve path — same fixture
                log_id = await insert_dummy_log(conn, status="staged")
                item = await rr.resolve_item(
                    ResolveRequest(log_id=log_id, item_index=0, phrase=phrase)
                )

                assert cascade_winner == food_b  # mode of last 10
                assert item.food.id == cascade_winner  # parity

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 9. /api/resolve reaches tier 2 (an edited "protein shakes" stays local)
# ---------------------------------------------------------------------------


def test_resolve_route_tier2_trigram():
    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                t = tok()
                alias_norm = f"{t} protein shake"
                food = await insert_food(conn, "Core Power Elite", brand="Fairlife")
                await insert_alias(conn, alias_norm, food)
                log_id = await insert_dummy_log(conn, status="staged")

                item = await rr.resolve_item(
                    ResolveRequest(log_id=log_id, item_index=0,
                                   phrase=f"{t} protein shakes")
                )
                assert item.status == "resolved" and item.tier == 2
                assert item.food.id == food
                row = await db.fetchone(
                    "SELECT tier, status FROM item_resolutions "
                    "WHERE log_id = %s AND item_index = 0",
                    (log_id,),
                )
                assert row["tier"] == 2 and row["status"] == "resolved"

    asyncio.run(run())
