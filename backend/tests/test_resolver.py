"""Workstream B tests — resolver core + routes, against the live Neon DB.

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one connection with force_rollback), except
test_commit_idempotency_live which commits for real and cleans up after itself.

Cross-workstream modules (auth, tracing, services.*) are stubbed in sys.modules
— they are being built in parallel (workstream contract).

Run:  uv run --with pytest pytest backend/tests/test_resolver.py -q
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
from uuid import UUID, uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


# ---------------------------------------------------------------------------
# stubs for parallel workstreams — scoped to THIS module's test run via the
# autouse fixture below (integration fix: import-time sys.modules assignment
# leaked stubs into the other workstreams' test files and vice versa).
# ---------------------------------------------------------------------------

def _build_stubs() -> dict:
    auth_mod = types.ModuleType("auth")

    async def require_user():
        return {"email": "billzhangsc@gmail.com"}

    auth_mod.require_user = require_user

    scored: list[tuple] = []
    tracing_mod = make_tracing_stub("trace-test", scored=scored)
    tracing_mod._scored = scored

    services_mod = types.ModuleType("services")
    services_mod.__path__ = []

    ctx_mod = types.ModuleType("services.context")

    async def build_agent_context():
        return SimpleNamespace(profile_notes="", targets={}, food_index=[], meal_names=[], recent_foods=[])

    ctx_mod.build_agent_context = build_agent_context

    dec_mod = types.ModuleType("services.decompose")

    async def decompose(text, ctx):
        raise AssertionError("decompose must not be called (short-circuit path)")

    dec_mod.decompose = decompose

    t3_mod = types.ModuleType("services.tier3")

    async def run_tier3(log_id, item_index):
        raise AssertionError("tier3 is not exercised by these tests")

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
    """Install the stubs for this module's tests only, then restore whatever
    was in sys.modules before — other test files (tier3/tracing live) must
    never see them. Route handlers resolve tracing/services lazily at call
    time, so a run-time sys.modules swap is sufficient."""
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
from models import (  # noqa: E402
    CommitRequest,
    FoodRef,
    LogCreate,
    Meal,
    ParsedItem,
    ParsedLog,
    ServingRef,
    StagedItem,
)
from resolver import cascade  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402
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


async def insert_alias(
    conn, alias_norm, food_id, *, use_count=1, last_used_days_ago=0.0,
    pinned=False, last_explicit_at=None,
):
    cur = await conn.execute(
        """
        INSERT INTO aliases (alias_norm, food_id, use_count, last_used_at,
                             last_explicit_at, pinned, origin)
        VALUES (%s, %s, %s, %s, %s, %s, 'manual')
        RETURNING id
        """,
        (
            alias_norm,
            food_id,
            use_count,
            NOW() - timedelta(days=last_used_days_ago),
            last_explicit_at,
            pinned,
        ),
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


def log_body(text, log_id=None):
    return LogCreate(
        id=log_id or uuid4(), text=text, captured_at=NOW(), tz="America/Los_Angeles"
    )


def resolved_item(index, phrase, food_id, name, *, serving=SCOOP, qty=1.0):
    return StagedItem(
        item_index=index, phrase=phrase, status="resolved", tier=1,
        food=FoodRef(id=food_id, name=name),
        serving=ServingRef(id=serving["serving_id"], description=serving["description"]),
        qty=qty, meal=Meal.snack,
    )


# ---------------------------------------------------------------------------
# 1. norm() (§3.0)
# ---------------------------------------------------------------------------


def test_norm_cases():
    assert norm("My Usual Breakfast!") == "usual breakfast"
    assert norm("  a   FAIRLIFE, ") == "fairlife"
    assert norm("the eggs") == "eggs"
    assert norm("some rice") == "rice"
    assert norm("an apple") == "apple"
    assert norm("chicken-salad") == "chicken salad"
    assert norm("ＥＧＧＳ") == "eggs"  # NFKC full-width fold
    assert norm("ﬁsh ﬁllet") == "fish fillet"  # NFKC ligature fold
    assert norm("eggs") == "eggs"  # plural/singular NOT normalized away
    assert norm("") == ""
    assert norm("a") == ""  # bare article


# ---------------------------------------------------------------------------
# 2. whole-utterance short-circuit hit (§3.0) — decompose stub raises if called
# ---------------------------------------------------------------------------


def test_short_circuit_hit():
    async def run():
        async with isolated_db() as conn:
            phrase = f"shake{uuid4().hex[:8]}"
            food = await insert_food(conn, "Core Power Elite", brand="Fairlife")
            await insert_alias(conn, phrase, food)

            body = log_body(f"my {phrase}")  # article dropped by norm()
            resp = await create_log(body)

            assert len(resp.items) == 1
            item = resp.items[0]
            assert item.status == "resolved"
            assert item.tier == 1
            assert item.food.id == food
            assert item.serving.id == "srv-scoop"
            assert item.qty == 1.0
            assert item.macros.kcal == 120

            log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (body.id,))
            assert log["short_circuit"] is True
            res = await db.fetchone(
                "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (body.id,),
            )
            assert res is not None and res["status"] == "resolved" and res["tier"] == 1
            assert str(res["proposed_food_id"]) == food

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. tier-1 pick() ordering (§3.2) incl. a correction flipping the next default
# ---------------------------------------------------------------------------


def test_tier1_pick_ordering_and_correction_flip():
    async def run():
        async with isolated_db() as conn:
            phrase = f"fairlife{uuid4().hex[:8]}"
            food_a = await insert_food(conn, "Core Power Elite", brand="Fairlife")
            food_b = await insert_food(conn, "2% Milk", brand="Fairlife")
            alias_a = await insert_alias(
                conn, phrase, food_a, use_count=20, last_used_days_ago=10
            )
            await insert_alias(conn, phrase, food_b, use_count=2, last_used_days_ago=1)

            # rule 4: use_count wins with no pins/explicit/history
            log1 = log_body(f"a {phrase}")
            resp1 = await create_log(log1)
            assert resp1.items[0].food.id == food_a
            assert resp1.items[0].alternates is not None  # ambiguity is never silent
            assert resp1.items[0].alternates[0].food_id == food_b

            # user corrects the pre-selection at commit: A → B (explicit choice)
            await commit_route(
                log1.id,
                CommitRequest(items=[resolved_item(0, phrase, food_b, "2% Milk")]),
            )
            rows = await db.fetchall(
                "SELECT food_id, use_count, rejected_count, last_explicit_at "
                "FROM aliases WHERE alias_norm = %s",
                (phrase,),
            )
            by_food = {str(r["food_id"]): r for r in rows}
            assert by_food[food_b]["last_explicit_at"] is not None
            assert by_food[food_a]["rejected_count"] == 1  # negative evidence
            assert by_food[food_b]["use_count"] == 3  # 2 + committed use

            # the correction flips the very next default (rule 2)
            resp2 = await create_log(log_body(f"a {phrase}"))
            assert resp2.items[0].food.id == food_b

            # rule 1: a pinned alias beats the explicit correction
            await conn.execute("UPDATE aliases SET pinned = true WHERE id = %s", (alias_a,))
            resp3 = await create_log(log_body(f"a {phrase}"))
            assert resp3.items[0].food.id == food_a

    asyncio.run(run())


def test_pick_mode_of_last_10_committed():
    async def run():
        async with isolated_db() as conn:
            phrase = f"bar{uuid4().hex[:8]}"
            food_a = await insert_food(conn, "Quest Bar")
            food_b = await insert_food(conn, "Pure Protein Bar")
            await insert_alias(conn, phrase, food_a, use_count=50, last_used_days_ago=5)
            await insert_alias(conn, phrase, food_b, use_count=1, last_used_days_ago=2)

            # last 10 committed choices: 6×B (most recent) vs 4×A → mode beats use_count
            dummy = await insert_dummy_log(conn)
            for i in range(6):
                await insert_committed_resolution(
                    conn, dummy, i, phrase, food_b, minutes_ago=i
                )
            for i in range(6, 10):
                await insert_committed_resolution(
                    conn, dummy, i, phrase, food_a, minutes_ago=i
                )

            resp = await create_log(log_body(phrase))
            assert resp.items[0].food.id == food_b

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. decompose path: tier-1 item + unresolved item staged as searching (§3.4 seam)
# ---------------------------------------------------------------------------


def test_decompose_path_tier1_and_searching():
    async def run():
        async with isolated_db() as conn:
            phrase = f"oats{uuid4().hex[:8]}"
            food = await insert_food(conn, "Rolled Oats")
            await insert_alias(conn, phrase, food)

            def parsed(p, qty=None, unit=None):
                return ParsedItem(
                    phrase=p, phrase_kind="food", qty=qty, unit=unit,
                    state=None, brand=None, library_match=None, meal_ref=None,
                )

            async def fake_decompose(text, ctx):
                return ParsedLog(
                    items=[parsed(phrase), parsed(f"novel{uuid4().hex[:8]}", qty=3, unit="g")],
                    meal_hint=None, when_hint=None, save_intent=None,
                )

            dec = sys.modules["services.decompose"]
            orig = dec.decompose
            dec.decompose = fake_decompose
            try:
                body = log_body(f"{phrase} and something new")
                resp = await create_log(body)
            finally:
                dec.decompose = orig

            assert len(resp.items) == 2
            assert resp.items[0].tier == 1 and resp.items[0].food.id == food
            assert resp.items[1].status == "searching" and resp.items[1].tier is None
            log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (body.id,))
            assert log["short_circuit"] is False
            assert log["trace_id"] == "trace-test"
            rows = await db.fetchall(
                "SELECT status, staged, proposed_qty FROM item_resolutions "
                "WHERE log_id = %s ORDER BY item_index",
                (body.id,),
            )
            assert [r["status"] for r in rows] == ["resolved", "searching"]
            # §7.3 rule 1: the spoken qty/unit survive the tier-3 handoff in the
            # staged jsonb; the searching row carries no proposal.
            searching = rows[1]
            assert searching["staged"]["spoken_qty"] == 3
            assert searching["staged"]["spoken_unit"] == "g"
            assert searching["staged"]["qty"] == 3
            assert searching["proposed_qty"] is None

    asyncio.run(run())


def test_watchdog_flips_stale_searching_to_not_found():
    async def run():
        async with isolated_db() as conn:
            from routes.logs import get_log

            log_id = await insert_dummy_log(conn, status="staged")
            staged = StagedItem(
                item_index=0, phrase="mystery", status="searching", meal=Meal.snack
            )
            await conn.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged, tier3_started_at,
                                              tier3_attempts)
                VALUES (%s, 0, 'mystery', 'mystery', 'searching', %s, %s, %s)
                """,
                (log_id, Json(staged.model_dump(mode="json")), NOW() - timedelta(seconds=120), 2),
            )
            resp = await get_log(log_id)
            assert resp.items[0].status == "not_found"  # >60s stale, >=2 attempts
            row = await db.fetchone(
                "SELECT status, staged FROM item_resolutions WHERE log_id = %s", (log_id,)
            )
            assert row["status"] == "not_found"
            assert row["staged"]["status"] == "not_found"

            # stale but only 1 attempt → stays searching (client re-kicks once)
            log2 = await insert_dummy_log(conn, status="staged")
            await conn.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged, tier3_started_at,
                                              tier3_attempts)
                VALUES (%s, 0, 'mystery', 'mystery', 'searching', %s, %s, 1)
                """,
                (log2, Json(staged.model_dump(mode="json")), NOW() - timedelta(seconds=120)),
            )
            resp2 = await get_log(log2)
            assert resp2.items[0].status == "searching"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. portion resolution (§7.3)
# ---------------------------------------------------------------------------


def test_portion_mode_of_10_robust_to_outlier():
    async def run():
        async with isolated_db() as conn:
            phrase = f"whey{uuid4().hex[:8]}"
            food = await insert_food(conn, "Whey Isolate")
            alias = await insert_alias(conn, phrase, food)

            dummy = await insert_dummy_log(conn)
            # most recent commit is a one-off 3-scoop day; the 9 before are 1 scoop
            await insert_committed_resolution(
                conn, dummy, 0, phrase, food, qty=3, minutes_ago=0, alias_id=alias
            )
            for i in range(1, 10):
                await insert_committed_resolution(
                    conn, dummy, i, phrase, food, qty=1, minutes_ago=i, alias_id=alias
                )

            resp = await create_log(log_body(phrase))
            item = resp.items[0]
            assert item.serving.id == "srv-scoop"
            assert item.qty == 1.0  # mode holds; the outlier does not move it

    asyncio.run(run())


def test_portion_spoken_qty_and_count_without_unit():
    async def run():
        # spoken qty + gram unit → maps onto the 100 g serving
        row = {
            "servings": [SCOOP, HUNDRED_G],
            "food_default_serving_id": "srv-scoop",
            "default_serving_id": None, "default_qty": None,
            "alias_id": None, "food_id": None,
        }
        sid, qty, verify = await cascade.resolve_portion(row, 40, "grams")
        assert (sid, qty, verify) == ("srv-100g", 0.4, False)

        # qty + unit word → fuzzy-matched serving, qty as spoken
        sid, qty, verify = await cascade.resolve_portion(row, 2, "scoops")
        assert (sid, qty, verify) == ("srv-scoop", 2, False)

        # …divided by the pieces the serving holds: "two scoops" of a "2 scoops
        # (64 g)" serving is ONE serving (BACKLOG 151 — never a one-piece assumption)
        two_scoops = {**row, "servings": [{**SCOOP, "description": "2 scoops (64 g)", "grams": 62}, HUNDRED_G]}
        sid, qty, verify = await cascade.resolve_portion(two_scoops, 2, "scoops")
        assert (sid, qty, verify) == ("srv-scoop", 1, False)

        # qty + no unit → prefers the count-type household serving
        egg = {
            "servings": [
                HUNDRED_G,
                {"serving_id": "srv-large", "description": "1 large", "grams": 50,
                 "macros": {"kcal": 72, "protein_g": 6.3, "carb_g": 0.4, "fat_g": 4.8}},
            ],
            "food_default_serving_id": "srv-large",
            "default_serving_id": None, "default_qty": None,
            "alias_id": None, "food_id": None,
        }
        sid, qty, verify = await cascade.resolve_portion(egg, 2, None)
        assert (sid, qty, verify) == ("srv-large", 2, False)

        # a count serving that holds several pieces: FDC's cooked shrimp is
        # "4 large", so 50 shrimp are 12.5 servings, not 50 (BACKLOG 151)
        shrimp = {
            **egg,
            "servings": [
                HUNDRED_G,
                {"serving_id": "srv-4large", "description": "4 large", "grams": 22,
                 "macros": {"kcal": 22, "protein_g": 5, "carb_g": 0, "fat_g": 0.3}},
            ],
            "food_default_serving_id": "srv-4large",
        }
        sid, qty, verify = await cascade.resolve_portion(shrimp, 50, None)
        assert (sid, qty, verify) == ("srv-4large", 12.5, False)
        # the leading number of a measure is not a piece count: "12 fl oz can"
        assert cascade._pieces_per("12 fl oz can") == 1.0
        assert cascade._pieces_per("4 large") == 4.0
        assert cascade._pieces_per("2 slices") == 2.0
        assert cascade._pieces_per("1 bottle (414 ml)") == 1.0
        assert cascade._pieces_per("large") == 1.0
        assert cascade._pieces_per("") == 1.0
        # a leading dimension, label or provider unit code is not a piece count either
        for desc in ("6 inch sub", "8 inch tortilla (49 g)", "12 inch pizza", "100 calorie pack (21 g)",
                     "8 FLOZ (240 ml)", "8 OZA", "2 LITERS (2000 ml)"):
            assert cascade._pieces_per(desc) == 1.0, desc

        # the unit-word match divides by how many of the SPOKEN unit the serving holds —
        # a measure word too: "1 tbsp" of a "2 tbsp (32 g)" serving is half of it
        assert cascade._units_per("2 tbsp (32 g)", "tbsp") == 2.0
        assert cascade._units_per("2 cups (85 g)", "cup") == 2.0
        assert cascade._units_per("2 scoops (64 g)", "scoop") == 2.0
        assert cascade._units_per("12 fl oz can", "can") == 1.0
        peanut_butter = {
            **egg,
            "servings": [
                HUNDRED_G,
                {"serving_id": "srv-2tbsp", "description": "2 tbsp (32 g)", "grams": 32,
                 "macros": {"kcal": 190, "protein_g": 7, "carb_g": 8, "fat_g": 16}},
            ],
            "food_default_serving_id": "srv-2tbsp",
        }
        sid, qty, verify = await cascade.resolve_portion(peanut_butter, 1, "tbsp")
        assert (sid, qty, verify) == ("srv-2tbsp", 0.5, False)

        # qty + no unit but ONLY mass servings → never silently multiplied: verify-me
        mass_only = {
            "servings": [HUNDRED_G],
            "food_default_serving_id": "srv-100g",
            "default_serving_id": None, "default_qty": None,
            "alias_id": None, "food_id": None,
        }
        sid, qty, verify = await cascade.resolve_portion(mass_only, 2, None)
        assert (sid, qty, verify) == ("srv-100g", 2, True)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5b. commit semantics: removed-vs-late rows, disambiguation, correction flow
# ---------------------------------------------------------------------------


def test_commit_keeps_payload_searching_uncommitted_not_removed():
    """A row the client submits as 'searching' (it resolved server-side in the
    kick/poll race) must stay uncommitted for the §3.5 commit-item late path —
    only rows ABSENT from the payload were removed by the user."""

    async def run():
        async with isolated_db() as conn:
            from resolver import commit as commit_tx

            phrase_a = f"a{uuid4().hex[:8]}"
            phrase_b = f"b{uuid4().hex[:8]}"
            phrase_c = f"c{uuid4().hex[:8]}"
            food = await insert_food(conn, "Race Food")
            log_id = await insert_dummy_log(conn, status="staged")

            async def seed(idx, phrase, status="resolved"):
                await conn.execute(
                    """
                    INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                                  status, staged, tier, proposed_food_id,
                                                  proposed_serving_id, proposed_qty)
                    VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 3, %s, 'srv-scoop', 1)
                    """,
                    (log_id, idx, phrase, phrase, status, food),
                )

            await seed(0, phrase_a)  # committed normally
            await seed(1, phrase_b)  # resolved server-side; client still shows searching
            await seed(2, phrase_c)  # user removed the row → absent from payload

            payload = [
                resolved_item(0, phrase_a, food, "Race Food"),
                StagedItem(  # client's stale view of item 1
                    item_index=1, phrase=phrase_b, status="searching", meal=Meal.snack
                ),
            ]
            result = await commit_tx.commit_log(log_id, payload)
            assert result["replayed"] is False

            rows = {
                r["item_index"]: r
                for r in await db.fetchall(
                    "SELECT * FROM item_resolutions WHERE log_id = %s", (log_id,)
                )
            }
            assert rows[0]["status"] == "resolved" and rows[0]["committed_at"] is not None
            # the race row: NOT removed, still uncommitted → late path open
            assert rows[1]["status"] == "resolved" and rows[1]["committed_at"] is None
            # the genuinely removed row
            assert rows[2]["status"] == "removed" and rows[2]["committed_at"] is None

            # §3.5 late path: commit-item appends the straggler
            late = await commit_tx.commit_item(
                log_id, resolved_item(1, phrase_b, food, "Race Food")
            )
            assert late["replayed"] is False
            entries = await db.fetchall(
                "SELECT item_index FROM entries WHERE log_id = %s ORDER BY item_index",
                (log_id,),
            )
            assert [e["item_index"] for e in entries] == [0, 1]

    asyncio.run(run())


def test_commit_disambiguated_is_explicit_without_rejection():
    """§3.5 step 2: answering a disambiguation counts as an explicit choice
    (last_explicit_at set) but must not increment rejected_count on the food
    the user actually chose."""

    async def run():
        async with isolated_db() as conn:
            phrase = f"dis{uuid4().hex[:8]}"
            food = await insert_food(conn, "Chosen Bar")
            log_id = await insert_dummy_log(conn, status="staged")
            await conn.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged, tier, proposed_food_id,
                                              proposed_serving_id, proposed_qty,
                                              was_disambiguated)
                VALUES (%s, 0, %s, %s, 'resolved', '{}'::jsonb, 3, %s, 'srv-scoop', 1, TRUE)
                """,
                (log_id, phrase, phrase, food),
            )
            from resolver import commit as commit_tx

            await commit_tx.commit_log(
                log_id, [resolved_item(0, phrase, food, "Chosen Bar")]
            )
            alias = await db.fetchone(
                "SELECT * FROM aliases WHERE alias_norm = %s AND food_id = %s",
                (phrase, food),
            )
            assert alias is not None
            assert alias["last_explicit_at"] is not None  # explicit via disambiguation
            assert alias["rejected_count"] == 0  # never self-rejected

    asyncio.run(run())


def test_correction_via_resolve_learns_original_phrase_and_flips_default():
    """§3.2 rule 2 / §3.5: an edit through /api/resolve preserves the original
    phrase and proposal, so commit learns alias(original phrase) → final food,
    rejects the abandoned pre-selection, and the very next log of the original
    phrase resolves to the correction."""

    async def run():
        async with isolated_db() as conn:
            # routes.resolve binds services.tier3 at import time; import it
            # fresh here (under this module's stubs) and drop it afterwards so
            # test_tier3_live re-imports it against the real tier3.
            sys.modules.pop("routes.resolve", None)
            import routes.resolve as resolve_routes
            from models import ResolveRequest

            phrase = f"fairlife{uuid4().hex[:8]}"
            edited = f"corepower{uuid4().hex[:8]}"
            food_a = await insert_food(conn, "2% Milk", brand="Fairlife")
            food_b = await insert_food(conn, "Core Power Elite", brand="Fairlife")
            await insert_alias(conn, phrase, food_a, use_count=20)
            await insert_alias(conn, edited, food_b)

            # original log: tier-1 pre-selects food_a for the spoken phrase
            body = log_body(phrase)
            resp = await create_log(body)
            assert resp.items[0].food.id == food_a

            # the user edits the row text → tier-1 resolves the edit to food_b
            item = await resolve_routes.resolve_item(
                ResolveRequest(log_id=body.id, item_index=0, phrase=edited)
            )
            assert item.status == "resolved" and item.food.id == food_b

            row = await db.fetchone(
                "SELECT phrase, phrase_norm, proposed_food_id, was_edited "
                "FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (body.id,),
            )
            # original phrase and proposal preserved (§3.5 learning baseline)
            assert row["phrase"] == phrase and row["phrase_norm"] == phrase
            assert str(row["proposed_food_id"]) == food_a
            assert row["was_edited"] is True

            # commit the correction
            await commit_route(
                body.id,
                CommitRequest(items=[resolved_item(0, edited, food_b, "Core Power Elite")]),
            )
            rows = await db.fetchall(
                "SELECT food_id, rejected_count, last_explicit_at FROM aliases "
                "WHERE alias_norm = %s",
                (phrase,),
            )
            by_food = {str(r["food_id"]): r for r in rows}
            # the ORIGINAL spoken phrase now maps explicitly to the correction…
            assert food_b in by_food
            assert by_food[food_b]["last_explicit_at"] is not None
            # …and the abandoned pre-selection carries the negative evidence
            assert by_food[food_a]["rejected_count"] == 1

            # the correction flips the very next default (§3.2 rule 2)
            resp2 = await create_log(log_body(phrase))
            assert resp2.items[0].food.id == food_b

            sys.modules.pop("routes.resolve", None)  # see import note above

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. commit idempotency — LIVE (commits for real, cleans up after itself)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("needs_002")  # real commit path: alias learning targets 002's constraint
def test_commit_idempotency_live():
    async def run():
        from resolver.foods_repo import persist_food

        phrase = f"live{uuid4().hex[:8]}"
        log_id = uuid4()
        food_id = None
        try:
            food_id = await persist_food(
                {
                    "source": "fdc", "source_id": f"test-{uuid4().hex}",
                    "name": "Idempotency Test Food", "display_name": "Idempotency Test Food",
                    "brand": None, "food_type": "branded",
                    "servings": [SCOOP, HUNDRED_G], "default_serving_id": "srv-scoop",
                    "per_100g": HUNDRED_G["macros"], "payload": {"test": True},
                }
            )
            # persist_food upserts on (source, source_id): re-persist returns same id
            assert await persist_food(
                {
                    "source": "fdc",
                    "source_id": (await db.fetchone(
                        "SELECT source_id FROM foods WHERE id = %s", (food_id,)
                    ))["source_id"],
                    "name": "x", "display_name": "x", "food_type": "branded",
                    "servings": [SCOOP], "default_serving_id": "srv-scoop",
                    "payload": {},
                }
            ) == food_id

            captured = datetime(2000, 1, 2, 20, 0, tzinfo=timezone.utc)
            await db.execute(
                "INSERT INTO logs (id, input_mode, raw_input, captured_at, status) "
                "VALUES (%s, 'text', %s, %s, 'staged')",
                (log_id, phrase, captured),
            )
            item = resolved_item(0, phrase, food_id, "Idempotency Test Food")
            await db.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged, tier, proposed_food_id,
                                              proposed_serving_id, proposed_qty)
                VALUES (%s, 0, %s, %s, 'resolved', %s, 1, %s, 'srv-scoop', 1)
                """,
                (log_id, phrase, norm(phrase), Json(item.model_dump(mode="json")), food_id),
            )

            totals1 = await commit_route(log_id, CommitRequest(items=[item]))
            log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
            assert log["status"] == "committed" and log["committed_at"] is not None

            entries = await db.fetchall("SELECT * FROM entries WHERE log_id = %s", (log_id,))
            assert len(entries) == 1
            assert entries[0]["macros"]["kcal"] == 120  # snapshot from the foods cache
            alias = await db.fetchone(
                "SELECT * FROM aliases WHERE alias_norm = %s", (norm(phrase),)
            )
            assert alias is not None and alias["use_count"] == 1

            # second commit: returns the stored result, no duplicate entries,
            # no double learning
            totals2 = await commit_route(log_id, CommitRequest(items=[item]))
            assert totals2 == totals1
            entries = await db.fetchall("SELECT * FROM entries WHERE log_id = %s", (log_id,))
            assert len(entries) == 1
            alias = await db.fetchone(
                "SELECT * FROM aliases WHERE alias_norm = %s", (norm(phrase),)
            )
            assert alias["use_count"] == 1
        finally:
            await db.execute("DELETE FROM entries WHERE log_id = %s", (log_id,))
            await db.execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
            await db.execute("DELETE FROM logs WHERE id = %s", (log_id,))
            if food_id is not None:
                await db.execute("DELETE FROM aliases WHERE food_id = %s", (food_id,))
                await db.execute("DELETE FROM foods WHERE id = %s", (food_id,))
            await db.close()

    asyncio.run(run())
