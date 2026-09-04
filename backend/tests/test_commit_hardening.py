"""W2 tests — commit/entries hardening (BACKLOG 4, 7, 16; DESIGN.md §3.5, §9, §11).

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one force-rollback connection), except the timing test,
which commits for real against live Neon + Langfuse and cleans up after itself.

Run:  uv run --with pytest pytest backend/tests/test_commit_hardening.py -q
"""

from __future__ import annotations

import asyncio
import sys
import time
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

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json


# ---------------------------------------------------------------------------
# tracing stub — scoped to this module's tests; the timing test swaps the REAL
# tracing module back in (it measures the live Langfuse flush).
# ---------------------------------------------------------------------------


def _build_tracing_stub() -> types.ModuleType:
    mod = types.ModuleType("tracing")
    mod.init_tracing = lambda: None
    mod.flush = lambda: None
    mod.score_commit = lambda trace_id, name, value, comment=None: None
    return mod


@pytest.fixture(scope="module", autouse=True)
def _tracing_stub():
    stub = _build_tracing_stub()
    saved = sys.modules.get("tracing")
    sys.modules["tracing"] = stub
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop("tracing", None)
        else:
            sys.modules["tracing"] = saved


import db  # noqa: E402


from tenancy_harness import bind_test_tenant  # noqa: E402
from models import FoodRef, Meal, ServingRef, StagedItem  # noqa: E402
from resolver import commit as commit_tx  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes.entries import EntryPatch, delete_entry, patch_entry  # noqa: E402

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
PARTIAL = {  # kcal + protein only — must trip the core-macros guard
    "serving_id": "srv-partial",
    "description": "1 bottle",
    "grams": 414,
    "macros": {"kcal": 230, "protein_g": 42},
}


# ---------------------------------------------------------------------------
# rollback harness (same shape as test_resolver.py)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(
        db._database_url(), row_factory=dict_row
    )
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


async def insert_food(conn, name, servings=None, default_serving="srv-scoop"):
    servings = servings if servings is not None else [SCOOP, HUNDRED_G]
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, food_type,
                           servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, 'branded', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, Json(servings), default_serving),
    )
    return str((await cur.fetchone())["id"])


async def insert_log(conn, status="staged", trace_id=None):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status, trace_id) "
        "VALUES (%s, 'text', 'x', now(), %s, %s)",
        (log_id, status, trace_id),
    )
    return log_id


async def seed_resolution(
    conn, log_id, idx, phrase, food_id, *, serving_id="srv-scoop", qty=1,
    tier=1, was_disambiguated=False, was_edited=False,
):
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                      status, staged, tier, proposed_food_id,
                                      proposed_serving_id, proposed_qty,
                                      was_disambiguated, was_edited)
        VALUES (%s, %s, %s, %s, 'resolved', '{}'::jsonb, %s, %s, %s, %s, %s, %s)
        """,
        (log_id, idx, phrase, norm(phrase), tier, food_id, serving_id, qty,
         was_disambiguated, was_edited),
    )


def resolved_item(index, phrase, food_id, name, *, serving=SCOOP, qty=1.0):
    return StagedItem(
        item_index=index, phrase=phrase, status="resolved", tier=1,
        food=FoodRef(id=food_id, name=name),
        serving=ServingRef(id=serving["serving_id"], description=serving["description"]),
        qty=qty, meal=Meal.snack,
    )


async def res_rows(log_id):
    return {
        r["item_index"]: r
        for r in await db.fetchall(
            "SELECT * FROM item_resolutions WHERE log_id = %s", (log_id,)
        )
    }


# ---------------------------------------------------------------------------
# 1. BACKLOG 4 — was_edited truth table (§9 honest edit signal)
# ---------------------------------------------------------------------------


def test_was_edited_truth_table():
    async def run():
        async with isolated_db() as conn:
            food = await insert_food(conn, "Truth Table Food")
            log_id = await insert_log(conn)
            p = uuid4().hex[:8]

            # 0: untouched — proposed == final, nothing recorded
            await seed_resolution(conn, log_id, 0, f"a{p}", food)
            # 1: qty-only delta, and NO proposed serving (the old code's blind
            #    spot: qty check was nested under proposed_serving_id NOT NULL)
            await seed_resolution(conn, log_id, 1, f"b{p}", food, serving_id=None, qty=1)
            # 2: disambiguated then committed unchanged
            await seed_resolution(conn, log_id, 2, f"c{p}", food, was_disambiguated=True)
            # 3: prior explicit edit (/api/resolve sets was_edited=TRUE) that
            #    landed back on the original proposal — must not be clobbered
            await seed_resolution(conn, log_id, 3, f"d{p}", food, was_edited=True)

            result = await commit_tx.commit_log(
                log_id,
                [
                    resolved_item(0, f"a{p}", food, "F"),
                    resolved_item(1, f"b{p}", food, "F", qty=2.0),  # qty-only edit
                    resolved_item(2, f"c{p}", food, "F"),
                    resolved_item(3, f"d{p}", food, "F"),
                ],
            )
            assert result["errors"] == []
            by_idx = {s["item_index"]: s for s in result["scores"]}
            assert by_idx[0]["was_edited"] is False  # untouched
            assert by_idx[1]["was_edited"] is True  # qty-only delta
            assert by_idx[2]["was_edited"] is False  # disambiguation is not an edit
            assert by_idx[2]["was_disambiguated"] is True
            assert by_idx[3]["was_edited"] is True  # recorded edit never clobbered

            rows = await res_rows(log_id)
            assert rows[0]["was_edited"] is False
            assert rows[1]["was_edited"] is True
            # disambiguated-then-committed keeps was_disambiguated
            assert rows[2]["was_disambiguated"] is True
            assert rows[2]["was_edited"] is False
            assert rows[3]["was_edited"] is True
            log = await db.fetchone("SELECT edited_items FROM logs WHERE id = %s", (log_id,))
            assert log["edited_items"] == 2

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. BACKLOG 7 — core-macros guard refuses partial-macro servings
# ---------------------------------------------------------------------------


def test_core_macros_guard_refuses_partial_food():
    async def run():
        async with isolated_db() as conn:
            good = await insert_food(conn, "Complete Food")
            bad = await insert_food(
                conn, "Partial Bottle", servings=[PARTIAL], default_serving="srv-partial"
            )
            log_id = await insert_log(conn)
            p = uuid4().hex[:8]
            await seed_resolution(conn, log_id, 0, f"g{p}", good)
            await seed_resolution(
                conn, log_id, 1, f"h{p}", bad, serving_id="srv-partial", tier=3
            )

            result = await commit_tx.commit_log(
                log_id,
                [
                    resolved_item(0, f"g{p}", good, "Complete Food"),
                    resolved_item(1, f"h{p}", bad, "Partial Bottle", serving=PARTIAL),
                ],
            )
            # per-item error, rest of the log commits
            assert len(result["errors"]) == 1
            err = result["errors"][0]
            assert err["item_index"] == 1
            assert "missing core macros" in err["error"]
            assert "carb_g" in err["error"] and "fat_g" in err["error"]

            entries = await db.fetchall(
                "SELECT item_index, macros FROM entries WHERE log_id = %s", (log_id,)
            )
            assert [e["item_index"] for e in entries] == [0]  # nothing partial written
            rows = await res_rows(log_id)
            assert rows[0]["committed_at"] is not None
            # refused row: verify-me state, uncommitted, late path open
            assert rows[1]["status"] == "needs_input"
            assert rows[1]["committed_at"] is None
            assert rows[1]["staged"]["status"] == "needs_input"
            assert "missing core macros" in rows[1]["staged"]["commit_error"]
            # no alias learned for the refused item
            alias = await db.fetchone(
                "SELECT 1 FROM aliases WHERE alias_norm = %s", (f"h{p}",)
            )
            assert alias is None

            # commit-item on the refused item -> clear per-item 409 error
            with pytest.raises(commit_tx.LogNotCommittable, match="missing core macros"):
                await commit_tx.commit_item(
                    log_id, resolved_item(1, f"h{p}", bad, "Partial Bottle", serving=PARTIAL)
                )

            # replay of the whole commit stays idempotent
            replay = await commit_tx.commit_log(
                log_id, [resolved_item(0, f"g{p}", good, "Complete Food")]
            )
            assert replay["replayed"] is True

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. §3.5 post-commit edits — PATCH /api/entries/:id
# ---------------------------------------------------------------------------


async def _commit_simple(conn, phrase, food_id, name, *, serving=SCOOP, qty=1.0):
    """Seed + commit one item through the real path; returns the entry row."""
    log_id = await insert_log(conn)
    await seed_resolution(
        conn, log_id, 0, phrase, food_id, serving_id=serving["serving_id"], qty=qty
    )
    result = await commit_tx.commit_log(
        log_id, [resolved_item(0, phrase, food_id, name, serving=serving, qty=qty)]
    )
    assert result["errors"] == []
    return await db.fetchone("SELECT * FROM entries WHERE log_id = %s", (log_id,))


def test_qty_patch_scales_snapshot_exactly_cache_untouched():
    async def run():
        async with isolated_db() as conn:
            p = f"scale{uuid4().hex[:8]}"
            food = await insert_food(conn, "Scale Food")
            entry = await _commit_simple(conn, p, food, "Scale Food")
            assert entry["macros"] == {"kcal": 120, "protein_g": 25, "carb_g": 3, "fat_g": 1.5}

            # poison the cache: a qty-only PATCH must never consult it
            await conn.execute(
                "UPDATE foods SET servings = %s WHERE id = %s",
                (Json([dict(SCOOP, macros={"kcal": 999, "protein_g": 999,
                                           "carb_g": 999, "fat_g": 999}), HUNDRED_G]), food),
            )

            resp = await patch_entry(entry["id"], EntryPatch(qty=2))
            assert resp.entry.qty == 2
            # x2 doubles the stored snapshot exactly
            assert resp.entry.macros.kcal == 240
            assert resp.entry.macros.protein_g == 50
            assert resp.entry.macros.carb_g == 6
            assert resp.entry.macros.fat_g == 3
            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry["id"],))
            assert row["macros"] == {"kcal": 240, "protein_g": 50, "carb_g": 6, "fat_g": 3}
            assert float(row["grams"]) == 62  # 31 x 2
            res = await db.fetchone(
                "SELECT final_qty, was_edited FROM item_resolutions WHERE log_id = %s",
                (entry["log_id"],),
            )
            assert float(res["final_qty"]) == 2 and res["was_edited"] is True

    asyncio.run(run())


def test_serving_patch_rederives_and_guards():
    async def run():
        async with isolated_db() as conn:
            p = f"srv{uuid4().hex[:8]}"
            food = await insert_food(
                conn, "Serving Food", servings=[SCOOP, HUNDRED_G, PARTIAL]
            )
            entry = await _commit_simple(conn, p, food, "Serving Food")

            resp = await patch_entry(entry["id"], EntryPatch(serving_id="srv-100g"))
            assert resp.entry.serving.id == "srv-100g"
            assert resp.entry.macros.kcal == 387  # re-derived from the cache
            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry["id"],))
            assert float(row["grams"]) == 100

            # a partial-macro serving is refused, entry untouched
            with pytest.raises(HTTPException) as e:
                await patch_entry(entry["id"], EntryPatch(serving_id="srv-partial"))
            assert e.value.status_code == 422
            assert "missing core macros" in e.value.detail
            row = await db.fetchone("SELECT serving_id FROM entries WHERE id = %s", (entry["id"],))
            assert row["serving_id"] == "srv-100g"

    asyncio.run(run())


def test_food_patch_feeds_alias_learning():
    async def run():
        async with isolated_db() as conn:
            p = f"corr{uuid4().hex[:8]}"
            food_a = await insert_food(conn, "Wrong Milk")
            food_b = await insert_food(conn, "Right Shake")
            entry = await _commit_simple(conn, p, food_a, "Wrong Milk")

            resp = await patch_entry(entry["id"], EntryPatch(food_id=food_b))
            assert resp.entry.food.id == food_b
            assert resp.entry.macros.kcal == 120  # re-derived from B's default serving

            rows = await db.fetchall(
                "SELECT food_id, rejected_count, last_explicit_at FROM aliases "
                "WHERE alias_norm = %s",
                (p,),
            )
            by_food = {str(r["food_id"]): r for r in rows}
            # the original phrase now maps explicitly to the correction...
            assert by_food[food_b]["last_explicit_at"] is not None
            # ...and the abandoned mapping carries the negative evidence
            assert by_food[food_a]["rejected_count"] == 1

            row = await db.fetchone("SELECT * FROM entries WHERE id = %s", (entry["id"],))
            assert str(row["food_id"]) == food_b
            assert str(row["alias_id"]) != str(entry["alias_id"])
            res = await db.fetchone(
                "SELECT final_food_id, was_edited FROM item_resolutions WHERE log_id = %s",
                (entry["log_id"],),
            )
            assert str(res["final_food_id"]) == food_b and res["was_edited"] is True

    asyncio.run(run())


def test_delete_entry():
    async def run():
        async with isolated_db() as conn:
            p = f"del{uuid4().hex[:8]}"
            food = await insert_food(conn, "Delete Food")
            entry = await _commit_simple(conn, p, food, "Delete Food")

            totals = await delete_entry(entry["id"])
            assert totals is not None
            assert await db.fetchone(
                "SELECT 1 FROM entries WHERE id = %s", (entry["id"],)
            ) is None
            with pytest.raises(HTTPException) as e:
                await delete_entry(entry["id"])
            assert e.value.status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. BACKLOG 16 — one Langfuse flush per commit; wall-time before/after.
#    LIVE: commits for real (production DB until the dev branch exists) and
#    posts real scores to Langfuse on a synthetic trace id; self-cleans.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("needs_002")  # real commit path: alias learning targets 002's constraint
def test_commit_timing_live_batched_flush():
    saved_stub = sys.modules.pop("tracing")  # the real module, for real flushes
    log_id = uuid4()
    food_id = None
    try:
        import tracing as real_tracing

        sys.modules["tracing"] = real_tracing

        async def run():
            nonlocal food_id
            p = f"timing{uuid4().hex[:8]}"
            trace_id = uuid4().hex  # scores post pre-ingestion by trace id (§10)
            try:
                food_id = (await db.fetchone(
                    """
                    INSERT INTO foods (source, source_id, name, display_name, food_type,
                                       servings, default_serving_id, payload)
                    VALUES ('fdc', %s, %s, %s, 'branded', %s, 'srv-scoop', '{}'::jsonb)
                    RETURNING id
                    """,
                    (uuid4().hex, "Timing Food", "Timing Food", Json([SCOOP])),
                ))["id"]
                await db.execute(
                    "INSERT INTO logs (id, input_mode, raw_input, captured_at, status, trace_id) "
                    "VALUES (%s, 'text', %s, now(), 'staged', %s)",
                    (log_id, p, trace_id),
                )
                # tier NULL -> exactly 2 scores (user_edited, was_disambiguated)
                await db.execute(
                    """
                    INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                                  status, staged, proposed_food_id,
                                                  proposed_serving_id, proposed_qty)
                    VALUES (%s, 0, %s, %s, 'resolved', '{}'::jsonb, %s, 'srv-scoop', 1)
                    """,
                    (log_id, p, p, food_id),
                )

                # BEFORE (old behavior): one blocking flush PER score
                real_tracing.init_tracing()
                t0 = time.perf_counter()
                for _ in range(2):
                    real_tracing.score_commit(
                        trace_id, "user_edited", 0, comment="timing before-style"
                    )
                    real_tracing.flush()
                before_s = time.perf_counter() - t0

                # AFTER: the full commit (tx + 2 scores batched into ONE flush)
                t0 = time.perf_counter()
                result = await commit_tx.commit_log(
                    log_id, [resolved_item(0, p, str(food_id), "Timing Food")]
                )
                after_s = time.perf_counter() - t0
                assert result["replayed"] is False and len(result["scores"]) == 1

                print(
                    f"\ncommit wall-time: before-style (flush per score, 2 scores) "
                    f"{before_s:.2f}s | batched commit incl. tx + ONE flush {after_s:.2f}s"
                )
                assert after_s < 3.0  # §11-adjacent local bound from the task
            finally:
                await db.execute("DELETE FROM entries WHERE log_id = %s", (log_id,))
                await db.execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
                await db.execute("DELETE FROM logs WHERE id = %s", (log_id,))
                if food_id is not None:
                    await db.execute("DELETE FROM aliases WHERE food_id = %s", (food_id,))
                    await db.execute("DELETE FROM foods WHERE id = %s", (food_id,))
                await db.close()

        asyncio.run(run())
    finally:
        sys.modules["tracing"] = saved_stub
