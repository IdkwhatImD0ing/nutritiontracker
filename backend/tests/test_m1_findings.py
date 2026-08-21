"""Phase-2 M1 fix-pass tests (adversarial-panel findings, 2026-08-20).

1. cascade `_stage_resolved` must stage a partial-core-macros serving as
   verify-me (macros=None, confidence low) — never crash with a pydantic
   ValidationError (500 on POST /api/logs and /api/resolve).
2. POST /api/logs/:id/commit must surface per-item core-macros refusals in the
   response (CommitResponse.errors) — never silently discard them (§3.5).
3. A cooked/raw toggle answer on a resolved row resolves LOCALLY (§3.4a):
   matching state persists with zero network; a differing state fires one
   targeted search_generic for the counterpart; run_tier3 is never re-entered
   and tier3_attempts never bumps.

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one force-rollback connection) — nothing persists.

Run:  uv run --with pytest pytest backend/tests/test_m1_findings.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json


# ---------------------------------------------------------------------------
# tracing stub — commit's _post_scores must not hit Langfuse from these tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _tracing_stub():
    stub = types.ModuleType("tracing")
    stub.init_tracing = lambda: None
    stub.flush = lambda: None
    stub.score_commit = lambda trace_id, name, value, comment=None: None
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
import routes.commit as commit_routes  # noqa: E402
import routes.resolve as resolve_routes  # noqa: E402
import services.tier3 as tier3_mod  # noqa: E402
import sources.fdc as fdc_mod  # noqa: E402
from models import CommitRequest, FoodRef, Meal, ParsedItem, ServingRef, StagedItem  # noqa: E402
from resolver import cascade  # noqa: E402
from resolver.norm import norm  # noqa: E402

COMPLETE = {
    "serving_id": "srv-scoop",
    "description": "1 scoop",
    "grams": 31,
    "macros": {"kcal": 120, "protein_g": 25, "carb_g": 3, "fat_g": 1.5},
}
LABEL = {
    "serving_id": "label",
    "description": "1 bottle (414 ml)",
    "grams": 414,
    "macros": {"kcal": 230, "protein_g": 42, "carb_g": 9, "fat_g": 4.5},
}
PARTIAL_100G = {  # Foundation-style: no carbs/fat mapped → guard territory
    "serving_id": "srv-100g",
    "description": "100 g",
    "grams": 100,
    "macros": {"kcal": 56, "protein_g": 10},
}


# ---------------------------------------------------------------------------
# rollback harness (same shape as test_resolver.py / test_commit_hardening.py)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(
        db._database_url(), row_factory=dict_row
    )
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


async def insert_food(
    conn, name, servings, default_serving, *, state=None, state_sensitive=False
):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, food_type,
                           state, state_sensitive, servings, default_serving_id,
                           payload)
        VALUES ('fdc', %s, %s, %s, 'generic', %s, %s, %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, state, state_sensitive, Json(servings),
         default_serving),
    )
    return str((await cur.fetchone())["id"])


async def insert_alias(conn, alias_norm, food_id):
    await conn.execute(
        "INSERT INTO aliases (alias_norm, food_id, use_count, last_used_at, origin) "
        "VALUES (%s, %s, 1, now(), 'manual')",
        (alias_norm, food_id),
    )


async def insert_log(conn, status="staged"):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status) "
        "VALUES (%s, 'text', 'x', now(), %s)",
        (log_id, status),
    )
    return log_id


async def seed_item(conn, log_id, idx, phrase, staged, *, status="resolved"):
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                      status, staged)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (log_id, idx, phrase, norm(phrase), status, Json(staged)),
    )


async def item_row(log_id, idx):
    return await db.fetchone(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s",
        (log_id, idx),
    )


def parsed(phrase, qty=None, unit=None):
    return ParsedItem(
        phrase=phrase, phrase_kind="food", qty=qty, unit=unit, state=None,
        brand=None, library_match=None, meal_ref=None,
    )


def tok() -> str:
    return "zx" + uuid4().hex[:10]


def staged_resolved(phrase, food_id, name, *, serving=COMPLETE, state_question=True):
    d = {
        "item_index": 0,
        "phrase": phrase,
        "status": "resolved",
        "tier": 1,
        "food": {"id": food_id, "name": name},
        "serving": {"id": serving["serving_id"], "description": serving["description"]},
        "qty": 1.0,
        "macros": dict(serving["macros"]),
        "meal": "snack",
        "confidence": "high",
    }
    if state_question:
        d["state_question"] = True
    return d


@asynccontextmanager
async def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


def _never_tier3(*a, **kw):
    raise AssertionError("run_tier3 must not be called by this path")


async def never_tier3(*a, **kw):
    _never_tier3()


async def never_search(*a, **kw):
    raise AssertionError("no network search expected on this path")


# ---------------------------------------------------------------------------
# 1. cascade: partial core macros stage as verify-me, never a ValidationError
# ---------------------------------------------------------------------------


def test_partial_core_macros_stage_verify_me_not_crash():
    async def run():
        async with isolated_db() as conn:
            # a) whole food is partial: tier-1 resolve must not 500
            phrase_a = f"{tok()} shake"
            partial_only = await insert_food(
                conn, "Partial Only", [PARTIAL_100G], "srv-100g"
            )
            await insert_alias(conn, phrase_a, partial_only)
            res = await cascade.resolve_item(parsed(phrase_a), 0, Meal.snack)
            assert res.staged.status == "resolved"
            assert res.staged.macros is None  # never a partial Macros
            assert res.staged.confidence == "low"  # verify-me styling

            # b) the reviewer's exact reachable case: complete 'label' serving,
            #    partial per-100g base; "40 g of X" routes the gram path onto
            #    the partial base serving.
            phrase_b = f"{tok()} bottle"
            mixed = await insert_food(
                conn, "Mixed Bottle", [LABEL, PARTIAL_100G], "label"
            )
            await insert_alias(conn, phrase_b, mixed)
            res = await cascade.resolve_item(
                parsed(phrase_b, qty=40, unit="g"), 0, Meal.snack
            )
            assert res.staged.status == "resolved"
            assert res.staged.serving.id == "srv-100g"  # gram path hit the base
            assert res.staged.qty == 0.4
            assert res.staged.macros is None
            assert res.staged.confidence == "low"

            # c) complete serving still stages complete macros (no regression)
            phrase_c = f"{tok()} scoop"
            complete = await insert_food(conn, "Complete", [COMPLETE], "srv-scoop")
            await insert_alias(conn, phrase_c, complete)
            res = await cascade.resolve_item(parsed(phrase_c), 0, Meal.snack)
            assert res.staged.macros is not None
            assert res.staged.macros.kcal == 120

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. commit route: per-item refusals ride the 200 response (CommitResponse)
# ---------------------------------------------------------------------------


def test_commit_route_surfaces_per_item_errors():
    async def run():
        async with isolated_db() as conn:
            good = await insert_food(conn, "Good Food", [COMPLETE], "srv-scoop")
            bad = await insert_food(conn, "Bad Food", [PARTIAL_100G], "srv-100g")
            log_id = await insert_log(conn)
            pg, pb = f"g{tok()}", f"b{tok()}"
            await seed_item(conn, log_id, 0, pg, staged_resolved(pg, good, "Good Food",
                                                                state_question=False))
            await seed_item(conn, log_id, 1, pb, staged_resolved(pb, bad, "Bad Food",
                                                                serving=PARTIAL_100G,
                                                                state_question=False))

            def item(idx, phrase, fid, name, serving):
                return StagedItem(
                    item_index=idx, phrase=phrase, status="resolved", tier=1,
                    food=FoodRef(id=fid, name=name),
                    serving=ServingRef(id=serving["serving_id"],
                                       description=serving["description"]),
                    qty=1.0, meal=Meal.snack,
                )

            resp = await commit_routes.commit_log(
                log_id,
                CommitRequest(items=[
                    item(0, pg, good, "Good Food", COMPLETE),
                    item(1, pb, bad, "Bad Food", PARTIAL_100G),
                ]),
            )
            assert isinstance(resp, commit_routes.CommitResponse)
            assert len(resp.errors) == 1
            assert resp.errors[0].item_index == 1
            assert "missing core macros" in resp.errors[0].error
            assert resp.kcal >= 120  # the good item still committed into today

            rows0, rows1 = await item_row(log_id, 0), await item_row(log_id, 1)
            assert rows0["committed_at"] is not None
            assert rows1["status"] == "needs_input"  # verify-me, late path open
            assert rows1["committed_at"] is None

            # idempotent replay still validates with an empty error channel
            replay = await commit_routes.commit_log(
                log_id, CommitRequest(items=[item(0, pg, good, "Good Food", COMPLETE)])
            )
            assert replay.errors == []

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. §3.4a cooked/raw toggle — local resolution, never run_tier3
# ---------------------------------------------------------------------------


def test_state_toggle_matching_state_zero_network():
    async def run():
        async with isolated_db() as conn:
            phrase = f"{tok()} chicken"
            food = await insert_food(
                conn, "Chicken breast, roasted", [COMPLETE], "srv-scoop",
                state="cooked", state_sensitive=True,
            )
            log_id = await insert_log(conn)
            await seed_item(conn, log_id, 0, phrase,
                            staged_resolved(phrase, food, "Chicken breast, roasted"))

            async with patched(tier3_mod, run_tier3=never_tier3):
                async with patched(fdc_mod, search_generic=never_search,
                                   get_food=never_search):
                    item = await resolve_routes.answer_question(
                        resolve_routes.AnswerRequest(
                            log_id=log_id, item_index=0,
                            answer=resolve_routes.Answer(text="cooked"),
                        )
                    )
            assert item.status == "resolved"
            assert item.food.id == food  # same food kept
            assert item.state_question is None

            row = await item_row(log_id, 0)
            assert row["status"] == "resolved"  # never flipped to searching
            assert row["tier3_attempts"] == 0  # never counted as a re-kick
            assert row["staged"]["state_answer"] == "cooked"
            assert "state_question" not in row["staged"]

    asyncio.run(run())


def test_state_toggle_counterpart_one_targeted_search():
    async def run():
        async with isolated_db() as conn:
            phrase = f"{tok()} chicken"
            food = await insert_food(
                conn, "Chicken breast, roasted", [COMPLETE], "srv-scoop",
                state="cooked", state_sensitive=True,
            )
            log_id = await insert_log(conn)
            await seed_item(conn, log_id, 0, phrase,
                            staged_resolved(phrase, food, "Chicken breast, roasted"))
            # simulate the original proposal on the row (a tier-1 pre-selection)
            await conn.execute(
                "UPDATE item_resolutions SET proposed_food_id = %s "
                "WHERE log_id = %s AND item_index = 0",
                (food, log_id),
            )

            searches: list[str] = []
            raw_sid = uuid4().hex[:10]

            async def fake_search(query):
                searches.append(query)
                return [
                    {"fdc_id": 111, "description": "Chicken, breaded, cooked",
                     "data_type": "SR Legacy", "protein_per_100g": 20},
                    {"fdc_id": raw_sid, "description": "Chicken, breast, meat only, raw",
                     "data_type": "SR Legacy", "protein_per_100g": 22.5},
                ]

            async def fake_get_food(source, source_id):
                assert (source, str(source_id)) == ("fdc", raw_sid)
                return {
                    "source": "fdc", "source_id": raw_sid,
                    "name": "Chicken, breast, meat only, raw",
                    "display_name": "chicken breast, raw",
                    "brand": None, "food_type": "generic",
                    "state": "raw", "state_sensitive": True,
                    "servings": [{"serving_id": "100g", "description": "100 g",
                                  "grams": 100.0,
                                  "macros": {"kcal": 120, "protein_g": 22.5,
                                             "carb_g": 0.0, "fat_g": 2.6}}],
                    "default_serving_id": "100g",
                    "per_100g": {"kcal": 120, "protein_g": 22.5,
                                 "carb_g": 0.0, "fat_g": 2.6},
                    "payload": {},
                }

            async with patched(tier3_mod, run_tier3=never_tier3):
                async with patched(fdc_mod, search_generic=fake_search,
                                   get_food=fake_get_food):
                    item = await resolve_routes.answer_question(
                        resolve_routes.AnswerRequest(
                            log_id=log_id, item_index=0,
                            answer=resolve_routes.Answer(text="raw"),
                        )
                    )
            assert searches == [f"{phrase} raw"]  # exactly ONE targeted search
            assert item.status == "resolved"
            assert item.food.id != food  # switched to the raw counterpart
            assert item.food.name == "chicken breast, raw"
            assert item.state_question is None  # answered — never re-asked

            row = await item_row(log_id, 0)
            assert row["status"] == "resolved"
            assert row["tier3_attempts"] == 0
            assert row["staged"]["state_answer"] == "raw"
            assert "state_question" not in row["staged"]
            # NON-switch semantics: proposed follows the counterpart so commit
            # won't land rejected_count on the user's own alias (§3.2/§3.5)
            assert str(row["proposed_food_id"]) == item.food.id

    asyncio.run(run())


def test_state_toggle_no_counterpart_keeps_food_records_answer():
    async def run():
        async with isolated_db() as conn:
            phrase = f"{tok()} chicken"
            food = await insert_food(
                conn, "Chicken breast, roasted", [COMPLETE], "srv-scoop",
                state="cooked", state_sensitive=True,
            )
            log_id = await insert_log(conn)
            await seed_item(conn, log_id, 0, phrase,
                            staged_resolved(phrase, food, "Chicken breast, roasted"))

            async def junk_search(query):
                return [{"fdc_id": 5, "description": "Chicken soup, canned",
                         "data_type": "SR Legacy", "protein_per_100g": 3}]

            async with patched(tier3_mod, run_tier3=never_tier3):
                async with patched(fdc_mod, search_generic=junk_search,
                                   get_food=never_search):
                    item = await resolve_routes.answer_question(
                        resolve_routes.AnswerRequest(
                            log_id=log_id, item_index=0,
                            answer=resolve_routes.Answer(text="raw"),
                        )
                    )
            assert item.status == "resolved"
            assert item.food.id == food  # degraded gracefully: food kept
            row = await item_row(log_id, 0)
            assert row["staged"]["state_answer"] == "raw"
            assert row["tier3_attempts"] == 0

    asyncio.run(run())


def test_free_text_answer_on_question_still_rekicks_once():
    async def run():
        async with isolated_db() as conn:
            phrase = f"{tok()} bar"
            log_id = await insert_log(conn)
            await seed_item(
                conn, log_id, 0, phrase,
                {"item_index": 0, "phrase": phrase, "status": "needs_input",
                 "meal": "snack",
                 "question": {"text": "Which brand?", "options": []}},
                status="needs_input",
            )

            calls: list[tuple] = []

            async def fake_run_tier3(log_id_, item_index, refinement=None):
                calls.append((str(log_id_), item_index, refinement))
                return StagedItem(item_index=0, phrase=phrase, status="not_found",
                                  meal=Meal.snack)

            async with patched(tier3_mod, run_tier3=fake_run_tier3):
                await resolve_routes.answer_question(
                    resolve_routes.AnswerRequest(
                        log_id=log_id, item_index=0,
                        answer=resolve_routes.Answer(text="the quest one"),
                    )
                )
            assert calls == [(str(log_id), 0, "the quest one")]
            row = await item_row(log_id, 0)
            assert row["tier3_attempts"] == 1  # the one re-kick, counted

            # and a STATE word answered to a needs_input (food-less) row still
            # falls through to the free-text re-kick (it is not a toggle)
            async with patched(tier3_mod, run_tier3=fake_run_tier3):
                await resolve_routes.answer_question(
                    resolve_routes.AnswerRequest(
                        log_id=log_id, item_index=0,
                        answer=resolve_routes.Answer(text="cooked"),
                    )
                )
            assert calls[-1] == (str(log_id), 0, "cooked")

    asyncio.run(run())
