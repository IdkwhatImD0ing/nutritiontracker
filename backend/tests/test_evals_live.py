"""Live test for backend/evals (DESIGN.md §10 datasets/experiments, §12 M3).

Creates a 2-item synthetic dataset in Langfuse, replays it through
evals.replay (the production decompose + cascade path), then asserts via the
Langfuse API that a dataset run with 2 scored items exists, each item's trace
carries environment=test, and the app DB saw no writes from replay (row counts
for the scratch food/alias, entries for that food, and logs/item_resolutions
for the two utterances unchanged — replay without --tier3 is read-only; the
counts are scoped because the DB is shared with other live tests).

Item A: a scratch food + alias seeded by this test; the alias phrase as the
whole utterance → the §3.0 short-circuit (no LLM) → the alias's default
serving × default qty → match=1.
Item B: a nonsense utterance → decompose (one real gpt-5.6-luna call) →
no food → food_match=0.

Cleans up the dataset run, both items, and the scratch food/alias; the empty
dataset stays.

Run:  uv run --with pytest pytest backend/tests/test_evals_live.py -q -s
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Json

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Before anything initializes Langfuse in this process (§10: replay traces are
# test traces). tracing._load_env uses setdefault, so this wins over .env.
os.environ["LANGFUSE_TRACING_ENVIRONMENT"] = "test"

# the harness is parked on Langfuse (DESIGN §10, docs/FIRETRACE_FEEDBACK.md):
# without the `evals` dependency group there is nothing to test here
pytest.importorskip("langfuse", reason="needs the `evals` dependency group (uv sync --group evals)")

from evals.common import setup_langfuse  # noqa: E402
from evals.replay import replay  # noqa: E402
from langfuse.api import NotFoundError  # noqa: E402

pytestmark = pytest.mark.live

SMOKE_DATASET = "usual-evals-smoke"
NONSENSE = "zorblat quxfin gribble"


def _poll(fn, ok, timeout_s: float = 90.0, every: float = 3.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if ok(last):
                return last
        except NotFoundError:
            pass
        time.sleep(every)
    raise AssertionError(f"timed out polling; last={last!r}")


async def _counts(food_id: str, utterances: tuple[str, ...]) -> dict[str, int]:
    """Rows replay could have touched, scoped to this test's fixtures — the DB
    is shared with other live tests, so whole-table counts are racy."""
    import db

    async def n(sql: str, params: tuple) -> int:
        return (await db.fetchone(sql, params))["n"]

    return {
        "foods": await n("SELECT count(*) AS n FROM foods WHERE id = %s", (food_id,)),
        "aliases": await n("SELECT count(*) AS n FROM aliases WHERE food_id = %s", (food_id,)),
        "entries": await n("SELECT count(*) AS n FROM entries WHERE food_id = %s", (food_id,)),
        "logs": await n("SELECT count(*) AS n FROM logs WHERE raw_input = ANY(%s)", (list(utterances),)),
        "item_resolutions": await n(
            "SELECT count(*) AS n FROM item_resolutions WHERE phrase = ANY(%s)", (list(utterances),)
        ),
    }


async def _seed_food_and_alias(tag: str, phrase: str) -> str:
    """A scratch food with one serving and an alias for `phrase` → food id."""
    import db

    servings = [{"serving_id": "s1", "description": "1 cup", "grams": 240,
                 "macros": {"kcal": 100, "protein_g": 8, "carb_g": 12, "fat_g": 2}}]
    row = await db.fetchone(
        "INSERT INTO foods (source, source_id, name, display_name, food_type, servings, "
        "default_serving_id, payload) VALUES ('fdc', %s, %s, %s, 'generic', %s, 's1', '{}') "
        "RETURNING id::text AS id",
        (f"test-evals-{tag}", f"Evals smoke food {tag}", f"evals smoke food {tag}", Json(servings)),
    )
    await db.execute(
        "INSERT INTO aliases (alias_norm, food_id, default_serving_id, default_qty, origin) "
        "VALUES (%s, %s, 's1', 2, 'manual')",
        (phrase, row["id"]),
    )
    return row["id"]


async def _unseed(food_id: str) -> None:
    import db

    await db.execute("DELETE FROM aliases WHERE food_id = %s", (food_id,))
    await db.execute("DELETE FROM foods WHERE id = %s", (food_id,))


def test_replay_records_scored_experiment_under_test_environment():
    lf = setup_langfuse("test")
    try:
        lf.api.datasets.get(SMOKE_DATASET)
    except NotFoundError:
        lf.create_dataset(name=SMOKE_DATASET, description="test_evals_live scratch dataset")

    tag = uuid4().hex[:8]
    phrase = f"evals smoke food {tag}"  # norm() leaves this unchanged
    food_id = asyncio.run(_seed_food_and_alias(tag, phrase))
    item_ids = [f"smoke-{tag}-a", f"smoke-{tag}-b"]
    run_name = f"test-evals-live-{tag}"
    try:
        lf.create_dataset_item(
            dataset_name=SMOKE_DATASET, id=item_ids[0],
            input={"utterance": phrase, "item_index": 0, "phrase": phrase, "meal": "lunch"},
            expected_output={"food": {"id": food_id, "name": phrase, "brand": None},
                             "serving_id": "s1", "qty": 2.0},
        )
        lf.create_dataset_item(
            dataset_name=SMOKE_DATASET, id=item_ids[1],
            input={"utterance": NONSENSE, "item_index": 0, "phrase": NONSENSE, "meal": "lunch"},
            expected_output={"food": {"id": str(uuid4()), "name": "nothing", "brand": None},
                             "serving_id": None, "qty": None},
        )
        # The dataset fetch is eventually consistent: wait until both items are listed.
        _poll(lambda: lf.get_dataset(SMOKE_DATASET),
              lambda ds: {i.id for i in ds.items} >= set(item_ids), timeout_s=60)

        before = asyncio.run(_counts(food_id, (phrase, NONSENSE)))
        assert before == {"foods": 1, "aliases": 1, "entries": 0, "logs": 0, "item_resolutions": 0}
        run = asyncio.run(replay(dataset=SMOKE_DATASET, run_name=run_name, lf=lf))
        after = asyncio.run(_counts(food_id, (phrase, NONSENSE)))
        assert after == before, f"replay wrote to the app DB: {before} -> {after}"

        # local result: A short-circuited onto the alias (serving s1 x 2), B found nothing
        by_id = {it.dataset_item_id: it for it in run.items}
        assert set(by_id) == set(item_ids)
        a, b = by_id[item_ids[0]], by_id[item_ids[1]]
        a_scores = {n: v for n, v, _ in a.scores}
        b_scores = {n: v for n, v, _ in b.scores}
        assert a.output["path"] == "short_circuit", a.output
        assert a_scores == {"food_match": 1.0, "serving_match": 1.0, "qty_match": 1.0, "match": 1.0}, a.output
        assert b_scores["food_match"] == 0.0 and b_scores["match"] == 0.0, b.output
        assert run.match_rate == 0.5
        assert run.dataset_run_id

        # Langfuse API: the dataset run holds both items, linked to their traces
        dr = _poll(lambda: lf.get_dataset_run(dataset_name=SMOKE_DATASET, run_name=run_name),
                   lambda r: len(r.dataset_run_items) == 2)
        linked = {ri.dataset_item_id: ri.trace_id for ri in dr.dataset_run_items}
        assert linked == {it.dataset_item_id: it.trace_id for it in run.items}

        # ... each trace ingested under environment=test, with the 4 item scores
        names = {"food_match", "serving_match", "qty_match", "match"}
        for it in run.items:
            trace = _poll(lambda: lf.api.trace.get(it.trace_id),
                          lambda t: {s.name for s in t.scores} >= names)
            assert trace.environment == "test", trace.environment
            got = {s.name: s.value for s in trace.scores if s.name in names}
            assert got == {n: v for n, v, _ in it.scores}, (got, it.scores)
        print(f"\n[evals live] run {run_name}: match_rate={run.match_rate:.2f}; "
              f"A={a.output.get('status')} via {a.output.get('path')}, "
              f"B={b.output.get('status')} via {b.output.get('path')}")
    finally:
        try:
            lf.delete_dataset_run(dataset_name=SMOKE_DATASET, run_name=run_name)
        except Exception as e:  # the run may not exist if replay failed early
            print(f"[cleanup] dataset run: {e}")
        for item_id in item_ids:
            try:
                lf.api.dataset_items.delete(item_id)
            except Exception as e:
                print(f"[cleanup] dataset item {item_id}: {e}")
        asyncio.run(_unseed(food_id))
