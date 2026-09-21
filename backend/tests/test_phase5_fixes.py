"""Phase-5 fix pass (X1 — resolver core): E1, E2, P3, E5, E3-part.

E1  The §3.4a estimate gate is decided by STORED rows only. The reproduced
    bypass: resubmit a flagged photo row under an unused item_index (the
    payload's own flags said "none") through /commit and commit-item.
E2  Portion-default poisoning: cascade.resolve_portion step 3, pick() rule 3
    and tier3._apply_portion read committed item_resolutions.final_* with no
    input_mode filter, so a photo-guessed / barcode portion became the next
    spoken log's default (DESIGN.md §3.5 step 3 forbids exactly this).
P3  KEEP_LOG_KEYS_SQL's jsonb_strip_nulls recursed into `vision` and dropped
    its null-valued keys on the first rewrite.
E5  KEEP downgraded every "unresolved" row to "portion" on rewrite, even
    when vision had sized nothing.
E3  tier3._local_candidates offered source='estimate' foods to the text-lane
    agent.

Fix-3 (the verifier's second pass, tests through the route functions):
G1  Duplicate item_index defeated the gate: it decided on a dict (last
    duplicate wins) while the write loop iterated the raw list. Also: a
    removed stored row could be committed/resurrected by resubmitting it.
G2  Tier 2b (library_match) and tier 0 (a saved meal's item) could stage a
    source='estimate' placeholder food.
G3  from-food qty=1e309 / NaN answered 500: the non-finite input reached the
    422 body and starlette renders with allow_nan=False.
G4  from-food with serving_id null ignored §7.3 step 3 (the mode of his own
    spoken, committed portions of that food).

Every test runs inside one force-rollback transaction (isolated_db, same
shape as test_commit_hardening.py) against the live DB — no row survives.
Photo-lane rows are seeded directly (staged jsonb + logs.input_mode), so the
tests do not depend on routes/photo.py.

Run:  uv run --project backend --with pytest pytest backend/tests/test_phase5_fixes.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from tracing_stub import make_tracing_stub
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


@pytest.fixture(scope="module", autouse=True)
def _tracing_stub():
    mod = make_tracing_stub()
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


from tenancy_harness import bind_test_tenant, override_user  # noqa: E402
import services.tier3 as tier3  # noqa: E402
from models import CommitRequest, FoodRef, Meal, ParsedItem, ServingRef, StagedItem  # noqa: E402
from resolver import cascade  # noqa: E402
from resolver import commit as commit_tx  # noqa: E402
from resolver.norm import norm  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes import quick  # noqa: E402
from routes.commit import CommitItemRequest  # noqa: E402
from routes.commit import commit_item as commit_item_route  # noqa: E402
from routes.commit import commit_log as commit_route  # noqa: E402

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
EGG = {
    "serving_id": "srv-large",
    "description": "1 large",
    "grams": 50,
    "macros": {"kcal": 78, "protein_g": 6, "carb_g": 0.6, "fat_g": 5},
}
VISION_EGGS = {
    "phrase": "eggs", "qty": 2, "unit": "egg", "est_grams": 100,
    "confidence": "high", "note": "two fried eggs", "model": "gpt-6-astra",
}
VISION_UNSIZED = {
    "phrase": "mystery sauce", "qty": None, "unit": None, "est_grams": None,
    "confidence": "low", "note": "a brown sauce", "model": "gpt-6-astra",
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


async def insert_food(conn, name, servings, *, source="fdc"):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, food_type,
                           servings, default_serving_id, payload)
        VALUES (%s, %s, %s, %s, 'generic', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (source, uuid4().hex, name, name, Json(servings), servings[0]["serving_id"]),
    )
    return str((await cur.fetchone())["id"])


async def insert_log(conn, input_mode="text", status="staged"):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status, committed_at) "
        "VALUES (%s, %s, 'x', now(), %s, CASE WHEN %s = 'committed' THEN now() END)",
        (log_id, input_mode, status, status),
    )
    return log_id


async def seed_row(
    conn, log_id, idx, staged: dict, *, status="resolved", tier=1,
    food_id=None, serving_id=None, qty=None, committed_age_min=None,
):
    """One item_resolutions row. committed_age_min=None → a live staged row
    (proposed_* = the given food/serving/qty); an int → a committed row
    (final_* = them) committed that many minutes ago, so mode-of-10 recency
    is deterministic inside one transaction (now() is constant here)."""
    phrase = staged.get("phrase") or "x"
    committed = committed_age_min is not None
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                      staged, tier, proposed_food_id, proposed_serving_id,
                                      proposed_qty, final_food_id, final_serving_id,
                                      final_qty, committed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s THEN now() - (%s * interval '1 minute') END)
        """,
        (
            log_id, idx, phrase, norm(phrase), status, Json(staged), tier,
            None if committed else food_id, None if committed else serving_id,
            None if committed else qty,
            food_id if committed else None, serving_id if committed else None,
            qty if committed else None,
            committed, committed_age_min or 0,
        ),
    )


async def committed_row(conn, input_mode, food_id, serving_id, qty, *, age_min, staged=None,
                        phrase="x"):
    """A committed item on a fresh log of the given input_mode."""
    log_id = await insert_log(conn, input_mode, status="committed")
    await seed_row(
        conn, log_id, 0, {"phrase": phrase, "status": "resolved", **(staged or {})},
        food_id=food_id, serving_id=serving_id, qty=qty, committed_age_min=age_min,
    )
    return log_id


def item(idx, phrase, food_id, name, *, serving=EGG, qty=2.0, status="resolved",
         estimated="none", meal=Meal.lunch):
    resolved = status == "resolved"
    return StagedItem(
        item_index=idx, phrase=phrase, status=status, tier=1 if resolved else None,
        food=FoodRef(id=food_id, name=name) if resolved else None,
        serving=ServingRef(id=serving["serving_id"], description=serving["description"])
        if resolved else None,
        qty=qty if resolved else None, meal=meal,
        estimated=estimated, estimate_note=f"Photo: {phrase}" if estimated != "none" else None,
    )


def photo_staged(it: StagedItem, vision=VISION_EGGS, **extra) -> dict:
    """The staged jsonb routes/photo.py leaves behind: the item (exclude_none)
    + the raw vision output + the spoken-quantity channel."""
    return {**it.model_dump(mode="json", exclude_none=True), "vision": vision, **extra}


async def photo_log(conn, eggs, *, with_searching=False):
    """A photo log with row 0 = eggs ×2 flagged 'portion', optionally row 1 =
    a flagged row still in tier 3 (searching)."""
    log_id = await insert_log(conn, "photo")
    tag = uuid4().hex[:6]
    row0 = item(0, f"eggs {tag}", eggs, "egg", estimated="portion")
    staged0 = photo_staged(row0, spoken_qty=2)
    await seed_row(conn, log_id, 0, staged0, food_id=eggs, serving_id="srv-large", qty=2)
    if with_searching:
        row1 = item(1, f"sauce {tag}", None, None, status="searching", estimated="portion")
        await seed_row(
            conn, log_id, 1,
            photo_staged(row1, vision={**VISION_UNSIZED, "est_grams": 30},
                         spoken_qty=30, spoken_unit="g"),
            status="searching", tier=None,
        )
    return log_id, row0


async def entries_of(log_id):
    return await db.fetchall(
        "SELECT item_index, qty, macros FROM entries WHERE log_id = %s ORDER BY item_index",
        (log_id,),
    )


async def row_of(log_id, idx):
    return await db.fetchone(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s", (log_id, idx)
    )


async def log_status(log_id):
    return (await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,)))["status"]


# ---------------------------------------------------------------------------
# E1 — the stored row decides what a commit records
#
# 2026-09-09: the per-row estimate CONFIRM is gone (the commit is the
# confirmation), so these no longer test a gate that refuses. What they still
# test is the property that gate was protecting: a client cannot relabel,
# re-index or un-flag its way into a commit the server did not stage. An
# estimate commits — flagged, as the server stored it.
# ---------------------------------------------------------------------------


def test_e1_index_shift_attack_refused_on_commit():
    async def run():
        async with isolated_db() as conn:
            eggs = await insert_food(conn, "egg", [EGG])
            log_id, row0 = await photo_log(conn, eggs)

            # the reproduced attack: the flagged row resubmitted under an unused
            # index, with the client's own flags saying "none". A photo log has
            # no stored row for index 10, and nothing commits around one.
            shifted = row0.model_copy(update={"item_index": 10, "estimated": "none",
                                              "estimate_note": None})
            with pytest.raises(commit_tx.LogNotCommittable, match="no staged row"):
                await commit_tx.commit_log(log_id, [shifted])
            assert await entries_of(log_id) == []
            assert await log_status(log_id) == "staged"
            assert (await row_of(log_id, 0))["status"] == "resolved"  # not marked removed

            # a removed row is gone: neither the shifted copy nor its own index
            await conn.execute(
                "UPDATE item_resolutions SET status = 'removed' WHERE log_id = %s", (log_id,)
            )
            with pytest.raises(commit_tx.LogNotCommittable, match="no staged row"):
                await commit_tx.commit_log(log_id, [shifted])
            with pytest.raises(commit_tx.LogNotCommittable, match="removed"):
                await commit_tx.commit_log(log_id, [row0])
            assert await entries_of(log_id) == []
            assert await log_status(log_id) == "staged"

    asyncio.run(run())


def test_e1_index_shift_attack_refused_on_commit_item():
    async def run():
        async with isolated_db() as conn:
            eggs = await insert_food(conn, "egg", [EGG])
            log_id, row0 = await photo_log(conn, eggs)
            # the log committed earlier (row 0 was a straggler that landed flagged)
            await conn.execute(
                "UPDATE logs SET status = 'committed', committed_at = now() WHERE id = %s",
                (log_id,),
            )
            shifted = row0.model_copy(update={"item_index": 10, "estimated": "none",
                                              "estimate_note": None})
            with pytest.raises(commit_tx.LogNotCommittable, match="no staged row"):
                await commit_tx.commit_item(log_id, shifted)
            assert await entries_of(log_id) == []
            assert await row_of(log_id, 10) is None  # nothing inserted either

            # same index: it commits now, but the STORED flag is what is
            # recorded — a client that says "none" does not un-flag its row
            await commit_tx.commit_item(
                log_id, row0.model_copy(update={"estimated": "none", "estimate_note": None})
            )
            assert [e["item_index"] for e in await entries_of(log_id)] == [0]
            assert (await row_of(log_id, 0))["staged"]["estimated"] == "portion"

    asyncio.run(run())


def test_e1_stored_flag_survives_every_relabelling_and_replay_works():
    async def run():
        async with isolated_db() as conn:
            eggs = await insert_food(conn, "egg", [EGG])
            log_id, row0 = await photo_log(conn, eggs, with_searching=True)
            sauce_searching = item(1, "sauce", None, None, status="searching")

            # a portion-sheet edit that also claims the row is not an estimate:
            # the qty is the client's (that is what the sheet is for), the flag
            # is the server's
            result = await commit_tx.commit_log(
                log_id,
                [row0.model_copy(update={"estimated": "none", "estimate_note": None}),
                 sauce_searching],
            )
            assert result["replayed"] is False and result["errors"] == []
            assert [(e["item_index"], float(e["qty"]), e["macros"]["kcal"])
                    for e in await entries_of(log_id)] == [(0, 2.0, 156)]
            stored0 = (await row_of(log_id, 0))["staged"]
            assert stored0["estimated"] == "portion"
            assert stored0["vision"] == VISION_EGGS
            # the searching flagged row never blocks the rest (§7.1) and stays
            row1 = await row_of(log_id, 1)
            assert row1["status"] == "searching" and row1["committed_at"] is None

            # §8.6 replay
            replay = await commit_tx.commit_log(log_id, [row0, sauce_searching])
            assert replay["replayed"] is True

            # late path: the searching row lands resolved+flagged via tier 3's
            # write, then commits with the DB numbers and keeps its flag
            landed = item(1, "sauce", eggs, "egg", qty=0.6, estimated="none")
            assert await tier3._write_item_row(
                str(log_id), 1, landed, tier=3, spoken_qty=30, spoken_unit="g"
            )
            assert (await row_of(log_id, 1))["staged"]["estimated"] == "portion"
            late = await commit_tx.commit_item(log_id, landed)
            assert late["replayed"] is False
            assert [e["item_index"] for e in await entries_of(log_id)] == [0, 1]
            assert (await row_of(log_id, 1))["staged"]["estimated"] == "portion"

    asyncio.run(run())


def test_e1_client_flags_never_persist_on_a_spoken_log():
    """R1: text/voice never estimate — a client-supplied flag on a text log is
    noise, stripped before persisting whether or not the row was stored."""
    async def run():
        async with isolated_db() as conn:
            food = await insert_food(conn, "shake", [SCOOP, HUNDRED_G])
            log_id = await insert_log(conn, "text")
            p = uuid4().hex[:8]
            stored = item(0, f"a{p}", food, "shake", serving=SCOOP, qty=1.0)
            await seed_row(conn, log_id, 0, stored.model_dump(mode="json", exclude_none=True),
                           food_id=food, serving_id="srv-scoop", qty=1)
            payload = [
                stored.model_copy(update={"estimated": "portion", "estimate_note": "forged"}),
                # index 1 has no stored row on a text log — allowed, flags stripped
                item(1, f"b{p}", food, "shake", serving=SCOOP, qty=1.0,
                     estimated="portion"),
            ]
            result = await commit_tx.commit_log(log_id, payload)
            assert result["errors"] == [] and result["replayed"] is False
            assert [e["item_index"] for e in await entries_of(log_id)] == [0, 1]
            for idx in (0, 1):
                staged = (await row_of(log_id, idx))["staged"]
                assert staged["estimated"] == "none"

            # the core-macros refusal path persists the payload too — same rule
            partial = await insert_food(conn, "partial", [{
                "serving_id": "srv-partial", "description": "1 bottle", "grams": 414,
                "macros": {"kcal": 230, "protein_g": 42},
            }])
            log2 = await insert_log(conn, "text")
            forged = item(0, f"c{p}", partial, "partial", qty=1.0, estimated="portion",
                          serving={"serving_id": "srv-partial", "description": "1 bottle"})
            result = await commit_tx.commit_log(log2, [forged])
            assert [e["item_index"] for e in result["errors"]] == [0]
            refused = (await row_of(log2, 0))["staged"]
            assert refused["status"] == "needs_input" and refused["estimated"] == "none"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# E2 — learning reads ignore no-learn logs and estimated rows
# ---------------------------------------------------------------------------


def _portion_row(food_id, servings, default="srv-scoop"):
    """An alias⋈food row shaped for cascade.resolve_portion, alias-less."""
    return {
        "servings": servings, "food_default_serving_id": default,
        "default_serving_id": None, "default_qty": None, "alias_id": None,
        "food_id": food_id,
    }


def test_e2_resolve_portion_step3_ignores_no_learn_and_estimated_rows():
    async def run():
        async with isolated_db() as conn:
            servings = [SCOOP, HUNDRED_G]
            food = await insert_food(conn, "shake", servings)
            row = _portion_row(food, servings)

            # the reproduced poisoning: a confirmed photo portion (1.5 × 100 g)
            # and a barcode pick were the only history for this food
            await committed_row(conn, "photo", food, "srv-100g", 1.5, age_min=1,
                                staged={"estimated": "portion"})
            await committed_row(conn, "barcode", food, "srv-100g", 2.0, age_min=2)
            assert await cascade.resolve_portion(row, None, None) == ("srv-scoop", 1.0, False)

            # a spoken (text) commit is what the food remembers
            await committed_row(conn, "text", food, "srv-100g", 3.0, age_min=3)
            assert await cascade.resolve_portion(row, None, None) == ("srv-100g", 3.0, False)

            # a row still carrying an estimate flag is out whatever its log says
            # (a tie here would go to the most recent — this one — if it counted)
            await committed_row(conn, "text", food, "srv-scoop", 5.0, age_min=0,
                                staged={"estimated": "portion"})
            assert await cascade.resolve_portion(row, None, None) == ("srv-100g", 3.0, False)

    asyncio.run(run())


def test_e2_pick_rule3_ignores_no_learn_rows():
    async def run():
        async with isolated_db() as conn:
            key = f"fairlife {uuid4().hex[:6]}"
            a = await insert_food(conn, "Fairlife A", [SCOOP])
            b = await insert_food(conn, "Fairlife B", [SCOOP])
            for food_id, uses in ((a, 5), (b, 1)):
                await conn.execute(
                    "INSERT INTO aliases (alias_norm, food_id, origin, use_count) "
                    "VALUES (%s, %s, 'manual', %s)",
                    (key, food_id, uses),
                )
            rows = await db.fetchall(cascade._ALIAS_SQL, (key,))
            assert len(rows) == 2

            # the photo lane's phrase happens to norm() to the same key and its
            # row committed food B — it must not count as "his" choice
            await committed_row(conn, "photo", b, "srv-scoop", 1.0, age_min=1, phrase=key,
                                staged={"estimated": "portion"})
            await committed_row(conn, "search", b, "srv-scoop", 1.0, age_min=2, phrase=key)
            assert str((await cascade.pick(rows))["food_id"]) == a  # rule 4: use_count

            # a spoken commit of B is rule 3
            await committed_row(conn, "voice", b, "srv-scoop", 1.0, age_min=3, phrase=key)
            assert str((await cascade.pick(rows))["food_id"]) == b

    asyncio.run(run())


def test_e2_tier3_apply_portion_ignores_no_learn_rows():
    async def run():
        async with isolated_db() as conn:
            servings = [SCOOP, HUNDRED_G]
            food = await insert_food(conn, "shake", servings)
            food_dict = {"servings": servings, "default_serving_id": "srv-scoop"}
            phrase = f"brand new phrase {uuid4().hex[:6]}"  # no alias → step 3

            # a barcode row seeded the next spoken phrase's portion
            await committed_row(conn, "barcode", food, "srv-100g", 2.0, age_min=1)
            assert await tier3._apply_portion(
                food_dict, None, None, None, food_row_id=food, phrase=phrase
            ) == ("srv-scoop", 1.0, False)

            await committed_row(conn, "text", food, "srv-100g", 4.0, age_min=2)
            assert await tier3._apply_portion(
                food_dict, None, None, None, food_row_id=food, phrase=phrase
            ) == ("srv-100g", 4.0, False)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# P3 / E5 — KEEP_LOG_KEYS_SQL: vision survives intact; honest downgrade
# ---------------------------------------------------------------------------


def test_p3_vision_nulls_survive_a_rewrite():
    async def run():
        async with isolated_db() as conn:
            log_id = await insert_log(conn, "photo")
            searching = item(0, "mystery sauce", None, None, status="searching",
                             estimated="portion")
            await seed_row(conn, log_id, 0, photo_staged(searching, vision=VISION_UNSIZED),
                           status="searching", tier=None)
            not_found = item(0, "mystery sauce", None, None, status="not_found")
            stored = await tier3._write_item_row(str(log_id), 0, not_found, tier=3)
            assert stored is not None
            assert stored["vision"] == VISION_UNSIZED  # null qty/unit/est_grams still there
            assert stored["estimated"] == "portion" and stored["status"] == "not_found"
            # and a second rewrite (the same row, again) keeps it too
            stored = await tier3._write_item_row(str(log_id), 0, not_found, tier=3)
            assert stored["vision"] == VISION_UNSIZED

    asyncio.run(run())


def test_e5_unresolved_downgrades_by_what_vision_sized():
    async def run():
        async with isolated_db() as conn:
            rice = await insert_food(conn, "rice", [HUNDRED_G, SCOOP])

            async def unresolved_row(vision):
                log_id = await insert_log(conn, "photo")
                estimate = item(0, "mystery sauce", uuid4().hex, "mystery sauce",
                                serving={"serving_id": "photo", "description": "1 portion"},
                                qty=1.0, estimated="portion")
                staged = photo_staged(estimate, vision=vision)
                staged["estimated"] = "unresolved"  # the numberless estimate row
                staged["search_instead"] = True
                await seed_row(conn, log_id, 0, staged, tier=None)
                return log_id

            # (a) vision sized it (~30 g): the search pick applies that portion → 'portion'
            sized = await unresolved_row({**VISION_UNSIZED, "est_grams": 30})
            picked = item(0, "mystery sauce", rice, "rice", serving=HUNDRED_G, qty=0.3)
            stored = await tier3._write_item_row(str(sized), 0, picked, tier=3,
                                                 spoken_qty=30, spoken_unit="g")
            assert stored["estimated"] == "portion"

            # (b) vision could not size it: the pick's qty is the resolver's own → 'none'
            unsized = await unresolved_row(VISION_UNSIZED)
            picked = item(0, "mystery sauce", rice, "rice", serving=HUNDRED_G, qty=1.0)
            stored = await tier3._write_item_row(str(unsized), 0, picked, tier=3)
            assert stored["estimated"] == "none"
            assert stored["vision"] == VISION_UNSIZED  # provenance kept for the eval loop
            # a 'none' row is not an estimate: commit takes it without a confirm
            await conn.execute(
                "UPDATE item_resolutions SET proposed_food_id = %s WHERE log_id = %s",
                (rice, unsized),
            )
            result = await commit_tx.commit_log(unsized, [picked])
            assert result["errors"] == [] and len(result["scores"]) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# E3 (tier3 part) — estimate foods never reach the text-lane agent's pool
# ---------------------------------------------------------------------------


def test_e3_local_candidates_exclude_estimate_foods():
    async def run():
        async with isolated_db() as conn:
            name = f"zzq phantom sauce {uuid4().hex[:6]}"
            await insert_food(conn, name, [{"serving_id": "photo", "description": "1 portion",
                                            "grams": 30, "macros": {}}], source="estimate")
            real = await insert_food(conn, name, [SCOOP])
            cands = await tier3._local_candidates(name)
            assert all(c["source"] != "estimate" for c in cands)
            assert any(c["source"] == "fdc" and c["display_name"] == name for c in cands)
            assert real  # the database twin is what the agent may pick

    asyncio.run(run())


# ---------------------------------------------------------------------------
# G1 — one payload view: the gate and the write loop see the same rows
# ---------------------------------------------------------------------------


def searching(idx, phrase):
    return item(idx, phrase, None, None, status="searching")


def test_g1_duplicate_item_index_cannot_defeat_the_gate():
    """The reproduced bypass: two payload items under one item_index. The
    gate decided on a dict (last duplicate wins — the harmless twin) while
    the write loop iterated the raw list and committed the other one."""

    async def run():
        async with isolated_db() as conn:
            eggs = await insert_food(conn, "egg", [EGG])

            # (1) the stored flagged row is still in tier 3: a resolved forgery
            # + a searching twin under its index committed the forgery unconfirmed
            log_id, row0 = await photo_log(conn, eggs, with_searching=True)
            forged = item(1, "sauce", eggs, "egg", qty=0.6)
            with pytest.raises(HTTPException) as ei:
                await commit_route(
                    log_id, CommitRequest(items=[row0, forged, searching(1, "sauce")])
                )
            assert ei.value.status_code == 409 and "duplicate item_index" in ei.value.detail
            assert await entries_of(log_id) == []  # not even the legitimate row 0
            assert await log_status(log_id) == "staged"  # the CAS rolled back
            assert (await row_of(log_id, 1))["committed_at"] is None

            # (2) a brand-new unstored item on a photo log rode the same trick
            # into the res-is-None INSERT branch
            log_id, row0 = await photo_log(conn, eggs)
            new = item(7, "brand new", eggs, "egg", qty=1.0)
            with pytest.raises(HTTPException) as ei:
                await commit_route(
                    log_id, CommitRequest(items=[row0, new, searching(7, "brand new")])
                )
            assert ei.value.status_code == 409 and "duplicate item_index" in ei.value.detail
            assert await entries_of(log_id) == [] and await row_of(log_id, 7) is None
            with pytest.raises(HTTPException) as ei:  # the lone orphan stays refused
                await commit_route(log_id, CommitRequest(items=[row0, new]))
            assert ei.value.status_code == 409 and "no staged row" in ei.value.detail
            assert await entries_of(log_id) == [] and await row_of(log_id, 7) is None

            # (3) after a legal remove, the flagged row resubmitted UNFLAGGED
            # under a shifted index + a searching twin landed as a plain entry
            log_id, row0 = await photo_log(conn, eggs)
            await logs_routes.remove_staged_item(log_id, 0)
            shifted = row0.model_copy(
                update={"item_index": 10, "estimated": "none", "estimate_note": None}
            )
            with pytest.raises(HTTPException) as ei:
                await commit_route(
                    log_id, CommitRequest(items=[shifted, searching(10, "eggs")])
                )
            assert ei.value.status_code == 409 and "duplicate item_index" in ei.value.detail
            assert await entries_of(log_id) == [] and await row_of(log_id, 10) is None
            assert await log_status(log_id) == "staged"

    asyncio.run(run())


def test_g1_removed_rows_are_never_committed_or_resurrected():
    async def run():
        async with isolated_db() as conn:
            eggs = await insert_food(conn, "egg", [EGG])

            # (4) a confirmed photo row the user then removed: resubmitted as-is
            # it passed the estimate gate and _commit_one flipped it back to
            # resolved + committed
            log_id, row0 = await photo_log(conn, eggs)
            await logs_routes.remove_staged_item(log_id, 0)
            with pytest.raises(HTTPException) as ei:
                await commit_route(log_id, CommitRequest(items=[row0]))
            assert ei.value.status_code == 409 and "removed" in ei.value.detail
            assert await entries_of(log_id) == []
            r0 = await row_of(log_id, 0)
            assert r0["status"] == "removed" and r0["committed_at"] is None
            assert await log_status(log_id) == "staged"

            # the late path on a text log: a removed straggler is not a straggler
            shake = await insert_food(conn, "shake", [SCOOP])
            p = uuid4().hex[:8]
            log2 = await insert_log(conn, "text", status="committed")
            done = item(0, f"a{p}", shake, "shake", serving=SCOOP, qty=1.0)
            await seed_row(
                conn, log2, 0, done.model_dump(mode="json", exclude_none=True),
                food_id=shake, serving_id="srv-scoop", qty=1, committed_age_min=1,
            )
            gone = item(1, f"b{p}", shake, "shake", serving=SCOOP, qty=1.0)
            await seed_row(
                conn, log2, 1, gone.model_dump(mode="json", exclude_none=True),
                status="removed", food_id=shake, serving_id="srv-scoop", qty=1,
            )
            with pytest.raises(HTTPException) as ei:
                await commit_item_route(log2, CommitItemRequest(item_index=1, item=gone))
            assert ei.value.status_code == 409 and "removed" in ei.value.detail
            r1 = await row_of(log2, 1)
            assert r1["status"] == "removed" and r1["committed_at"] is None
            assert await entries_of(log2) == []
            assert await db.fetchone(
                "SELECT 1 AS hit FROM aliases WHERE alias_norm = %s", (norm(f"b{p}"),)
            ) is None  # nothing learned for the refused row

    asyncio.run(run())


def test_g1_legitimate_commits_still_pass():
    async def run():
        async with isolated_db() as conn:
            eggs = await insert_food(conn, "egg", [EGG])

            # photo: the normal commit, the §8.6 replay, the straggler's late path
            log_id, row0 = await photo_log(conn, eggs, with_searching=True)
            payload = CommitRequest(items=[row0, searching(1, "sauce")])
            totals = await commit_route(log_id, payload)
            assert totals.errors == []
            assert [e["item_index"] for e in await entries_of(log_id)] == [0]
            assert await log_status(log_id) == "committed"
            replay = await commit_route(log_id, payload)
            assert replay.errors == []
            assert [e["item_index"] for e in await entries_of(log_id)] == [0]
            landed = item(1, "sauce", eggs, "egg", qty=0.6)
            assert await tier3._write_item_row(
                str(log_id), 1, landed, tier=3, spoken_qty=30, spoken_unit="g"
            )
            await commit_item_route(log_id, CommitItemRequest(item_index=1, item=landed))
            assert [e["item_index"] for e in await entries_of(log_id)] == [0, 1]

            # text: remove-then-commit-the-rest, plus a client-only added row
            # (the res-is-None branch's real job on a spoken log)
            shake = await insert_food(conn, "shake", [SCOOP])
            p = uuid4().hex[:8]
            log2 = await insert_log(conn, "text")
            rows = [
                item(i, f"{c}{p}", shake, "shake", serving=SCOOP, qty=1.0)
                for i, c in enumerate("ab")
            ]
            for r in rows:
                await seed_row(
                    conn, log2, r.item_index, r.model_dump(mode="json", exclude_none=True),
                    food_id=shake, serving_id="srv-scoop", qty=1,
                )
            await logs_routes.remove_staged_item(log2, 1)
            added = item(2, f"c{p}", shake, "shake", serving=SCOOP, qty=1.0)
            totals = await commit_route(log2, CommitRequest(items=[rows[0], added]))
            assert totals.errors == []
            assert [e["item_index"] for e in await entries_of(log2)] == [0, 2]
            r1 = await row_of(log2, 1)
            assert r1["status"] == "removed" and r1["committed_at"] is None
            r2 = await row_of(log2, 2)
            assert r2 is not None and r2["committed_at"] is not None  # no telemetry ghost

    asyncio.run(run())


# ---------------------------------------------------------------------------
# G2 — the estimate placeholder never stages through tier 2b or tier 0
# ---------------------------------------------------------------------------


def test_g2_estimate_food_never_resolves_at_tier_2b_or_tier_0():
    async def run():
        async with isolated_db() as conn:
            name = f"zzq phantom sauce {uuid4().hex[:6]}"
            est = await insert_food(
                conn, name,
                [{"serving_id": "photo", "description": "1 portion", "grams": 30, "macros": {}}],
                source="estimate",
            )
            # 2b: routes/logs.py passes no index_ids, so "exists in the cache"
            # was the whole validation
            assert await cascade._library_food(est, None) is None
            parsed = ParsedItem(
                phrase=name, phrase_kind="food", qty=None, unit=None, state=None,
                brand=None, library_match=est, meal_ref=None,
            )
            res = await cascade.resolve_item(parsed, 0, Meal.snack)
            assert res.staged.status == "searching"  # the tier-3 handoff, not the placeholder

            # tier 0: a saved meal pointing at it expands as broken (fix it in
            # Library), never as a numberless resolved row
            trigger = f"phantom meal {uuid4().hex[:6]}"
            macro = await commit_tx.save_meal_macro(
                conn, name=trigger, trigger_phrases=[trigger],
                items=[{"food_id": est, "serving_id": "photo", "qty": 1}],
            )
            rows, broken = await cascade.expand_macro(macro, Meal.snack, 0)
            assert rows == [] and len(broken) == 1
            res = await cascade.resolve_item(
                ParsedItem(
                    phrase=trigger, phrase_kind="meal_ref", qty=None, unit=None, state=None,
                    brand=None, library_match=None, meal_ref=None,
                ),
                0, Meal.snack,
            )
            assert res.staged.status == "needs_input" and res.staged.food is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# G3 — a non-finite qty is a 422, never a 500
# ---------------------------------------------------------------------------


def test_g3_non_finite_qty_is_a_422_not_a_500(monkeypatch):
    """JSON 1e309 parses to inf and NaN to nan before pydantic sees them; the
    422 body echoed the input and starlette (allow_nan=False) raised → 500.
    Through the assembled app: the handler never runs, no logs row."""
    from fastapi.testclient import TestClient

    import auth
    import main

    async def never(food_id):
        raise AssertionError("the handler must not run for a non-finite qty")

    monkeypatch.setattr(quick, "_load_food", never)
    monkeypatch.setattr(main, "tracing", sys.modules["tracing"])
    main.app.dependency_overrides[auth.require_user] = override_user(email="t")
    ids: list = []
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            for raw_qty in ("1e309", "-1e309", "NaN"):
                log_id = uuid4()
                ids.append(str(log_id))
                body = (
                    f'{{"id": "{log_id}", "food_id": "{uuid4()}", "qty": {raw_qty}, '
                    '"input_mode": "search", "captured_at": "2026-01-05T20:15:00Z"}'
                )
                r = client.post(
                    "/api/logs/from-food", content=body,
                    headers={"content-type": "application/json"},
                )
                assert r.status_code == 422, (raw_qty, r.status_code, r.text[:200])
                assert r.json()["detail"][0]["loc"] == ["body", "qty"]
    finally:
        main.app.dependency_overrides.pop(auth.require_user, None)
    # the model itself refuses them too (in-process callers)
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite"):
            quick.FromFoodRequest(
                id=uuid4(), food_id=uuid4(), qty=bad, input_mode="search",
                captured_at=datetime.now(timezone.utc),
            )

    async def leftovers():
        try:
            return await db.fetchall(
                "SELECT 1 AS hit FROM logs WHERE id = ANY(%s::uuid[])", (ids,)
            )
        finally:
            await db.close()

    assert asyncio.run(leftovers()) == []


# ---------------------------------------------------------------------------
# G4 — from-food without a serving lands on his spoken portion of that food
# ---------------------------------------------------------------------------


def test_g4_from_food_without_serving_uses_his_spoken_portion_of_that_food():
    async def run():
        async with isolated_db() as conn:
            servings = [HUNDRED_G, SCOOP]  # provider default: 100 g
            shake = await insert_food(conn, "shake", servings)

            def req(food_id, **kw):
                return quick.FromFoodRequest(
                    id=uuid4(), food_id=food_id, input_mode="search",
                    captured_at=datetime.now(timezone.utc), **kw,
                )

            async def pick(body):
                return (await quick.create_log_from_food(body)).items[0]

            # no history: the provider default × 1, as before
            picked = await pick(req(shake))
            assert (picked.serving.id, picked.qty) == ("srv-100g", 1.0)

            # he has spoken this food at 2 × scoop: the pick lands there (§7.3 step 3)
            await committed_row(conn, "text", shake, "srv-scoop", 2.0, age_min=1)
            body = req(shake)
            picked = await pick(body)
            assert (picked.serving.id, picked.qty) == ("srv-scoop", 2.0)
            assert picked.macros.model_dump(exclude_none=True) == cascade._scale_macros(
                SCOOP["macros"], 2.0
            )
            stored = await db.fetchone(
                "SELECT proposed_serving_id, proposed_qty FROM item_resolutions "
                "WHERE log_id = %s",
                (body.id,),
            )
            assert (stored["proposed_serving_id"], float(stored["proposed_qty"])) == ("srv-scoop", 2.0)

            # an explicit serving still wins; an explicit qty rides the remembered serving
            picked = await pick(req(shake, serving_id="srv-100g"))
            assert (picked.serving.id, picked.qty) == ("srv-100g", 1.0)
            picked = await pick(req(shake, qty=3))
            assert (picked.serving.id, picked.qty) == ("srv-scoop", 3.0)

            # history from the no-learn lanes alone teaches nothing (R3)
            bar = await insert_food(conn, "bar", servings)
            await committed_row(conn, "barcode", bar, "srv-scoop", 2.0, age_min=1)
            await committed_row(
                conn, "photo", bar, "srv-scoop", 2.0, age_min=2,
                staged={"estimated": "portion"},
            )
            picked = await pick(req(bar))
            assert (picked.serving.id, picked.qty) == ("srv-100g", 1.0)

    asyncio.run(run())
