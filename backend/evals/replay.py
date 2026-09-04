"""Replay a Langfuse dataset through the production resolver, as an experiment run.

Each dataset item goes through the SAME code path as POST /api/logs
(routes/logs.py): cascade.short_circuit_all -> services.decompose.decompose
(inside a tracing.generation, as production wraps it) -> cascade.resolve_items
(tiers 1–2), then the staged row this dataset item is about is picked out of
the resulting list — a tier-0 macro expands into N rows, so item_index no
longer lines up (§3.1). Tier 3 (services.tier3.run_tier3) is opt-in (--tier3)
and write-gated (USUAL_EVAL_DB_WRITES=1, or a local DATABASE_URL): it needs a
staged row, so a scratch log (input_mode='eval') + item_resolutions row is
inserted and deleted around the run; it caches foods like production does.
Nothing here persists staging state, commits, or learns - entries and aliases
are never written (test_evals_live asserts this by row count).

Recording: one Langfuse trace per item under environment=test
(LANGFUSE_TRACING_ENVIRONMENT + propagate_attributes), linked to the dataset
item as a dataset run item (the v4 experiments API: api.dataset_run_items),
with per-item scores food_match / serving_match / qty_match / match and a
run-level match_rate. The SDK's Langfuse.run_experiment is deliberately not
used: v4 stamps every experiment span with the fixed environment
"sdk-experiment" (langfuse/_client/client.py, constants.py), which would defeat
§10's production/test split - the same primitives are wired by hand instead.

Usage (from backend/):
  uv run python -m evals.replay --dataset usual-corrections --limit N [--tier3] [--run-name NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from langfuse import Langfuse, propagate_attributes
from psycopg.types.json import Json

from evals.common import (
    CORRECTIONS_DATASET,
    REPLAY_ENVIRONMENT,
    bind_owner,
    context_from_snapshot,
    setup_langfuse,
)
from models import Meal, StagedItem
from resolver import cascade
from resolver.norm import norm
from services.context import AgentContext

Score = tuple[str, float, str]  # (name, value, comment)

# --tier3 is the only path here that writes to the app DB. Opt-in by env var,
# and every scratch log is marked so a sweep can find orphans.
EVAL_WRITES_ENV = "USUAL_EVAL_DB_WRITES"
EVAL_INPUT_MODE = "eval"


# ---------------------------------------------------------------------------
# the task: production's staging path, minus persistence
# ---------------------------------------------------------------------------


def _output(item: StagedItem | None, **extra) -> dict:
    if item is None:
        return {"status": "missing", **extra}
    return {
        "status": item.status,
        "tier": item.tier,
        "food": item.food.model_dump() if item.food else None,
        "serving_id": item.serving.id if item.serving else None,
        "qty": item.qty,
        "confidence": item.confidence,
        **extra,
    }


def _pick_resolution(
    rows: list[cascade.Resolution], phrase: str, item_index: int, expected: dict | None
) -> cascade.Resolution | None:
    """The staged row that corresponds to the dataset item: same phrase
    (normalized) first, then the expected food, else the same position.

    Position alone is not enough: a tier-0 macro expands into one row per macro
    item (§3.1), so every later item_index shifts."""
    key = norm(phrase)
    for r in rows:
        if norm(r.staged.phrase) == key:
            return r
    exp_food = ((expected or {}).get("food") or {}).get("id")
    if exp_food:
        for r in rows:
            if r.staged.food is not None and r.staged.food.id == str(exp_food):
                return r
    return rows[item_index] if 0 <= item_index < len(rows) else None


def _check_tier3_writes() -> None:
    """--tier3 inserts a scratch log + item_resolutions row and lets the agent
    cache the foods it resolves, into whatever DATABASE_URL resolves to — which
    today is the production Neon pooler (BACKLOG 18: no dev branch yet), and a
    killed process leaves the scratch rows behind. Writes are therefore opt-in
    unless the database is local."""
    import db

    if os.environ.get(EVAL_WRITES_ENV) == "1":
        return
    host = (urlsplit(db._database_url()).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    raise RuntimeError(
        f"refusing --tier3: it writes to the database at {host!r}, which is production "
        f"today (BACKLOG 18). Point DATABASE_URL at a dev branch, or set "
        f"{EVAL_WRITES_ENV}=1 to accept the writes. Orphaned scratch rows from a killed "
        f"run are the logs with input_mode = '{EVAL_INPUT_MODE}' (and their "
        f"item_resolutions)."
    )


async def _tier3(phrase: str, meal: str, qty: float | None, unit: str | None,
                 utterance: str) -> StagedItem:
    """Production's cold-start path needs a staged row (§3.4): insert a scratch
    log + item, run the agent, delete both. Foods it resolves stay cached,
    exactly as in production; entries/aliases are untouched. The log is marked
    input_mode='eval' so a run killed mid-flight leaves findable orphans."""
    import db
    from services.tier3 import run_tier3

    log_id = str(uuid4())
    staged = {"item_index": 0, "phrase": phrase, "status": "searching", "meal": meal,
              "qty": qty, "spoken_qty": qty, "spoken_unit": unit}
    await db.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at) VALUES (%s, %s, %s, now())",
        (log_id, EVAL_INPUT_MODE, utterance),
    )
    await db.execute(
        "INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status, staged) "
        "VALUES (%s, 0, %s, %s, 'searching', %s)",
        (log_id, phrase, norm(phrase), Json(staged)),
    )
    try:
        return await run_tier3(log_id, 0)
    finally:
        await db.execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
        await db.execute("DELETE FROM logs WHERE id = %s", (log_id,))


async def stage(
    utterance: str,
    meal: Meal,
    ctx: AgentContext,
    item_index: int,
    phrase: str,
    *,
    expected: dict | None = None,
    tier3: bool = False,
) -> dict:
    """routes/logs.py's create path for one dataset item, returning the staged
    row for `phrase` as a plain dict (the experiment output)."""
    import tracing
    from services.decompose import decompose

    # §3.0 whole-utterance short-circuit: meal macro (tier 0, N rows) before
    # alias (tier 1, one row) — logs.py's short_circuit_all, not the alias-only
    # single-row variant.
    sc = await cascade.short_circuit_all(utterance, meal)
    if sc is not None:
        picked = _pick_resolution(sc, phrase, item_index, expected)
        return _output(
            picked.staged if picked else None, path="short_circuit", rows=len(sc)
        )

    async with tracing.generation("decompose") as gen:  # §10, as logs.py wraps it
        parsed = await decompose(utterance, ctx)
        gen.update(input=utterance, output=parsed.model_dump(mode="json"))
    if parsed.meal_hint is not None:
        meal = parsed.meal_hint

    # Per-log cascade (§3.1): a tier-0 expansion occupies one item_index per
    # macro item, so the staged list is not 1:1 with parsed.items.
    resolutions = await cascade.resolve_items(parsed.items, meal)
    picked = _pick_resolution(resolutions, phrase, item_index, expected)
    if picked is None:
        return _output(None, path="decompose", parsed=parsed.model_dump(mode="json"))

    staged = picked.staged
    if staged.status == "searching" and tier3:
        staged = await _tier3(
            staged.phrase, meal.value, staged.qty, picked.unit, utterance
        )
    return _output(staged, path="decompose", rows=len(resolutions))


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def evaluate(output: dict, expected: dict | None) -> list[Score]:
    expected = expected or {}
    exp_food = (expected.get("food") or {}).get("id")
    got_food = (output.get("food") or {}).get("id")
    food_ok = got_food is not None and got_food == exp_food
    serving_ok = food_ok and output.get("serving_id") == expected.get("serving_id")
    qty_ok = (
        food_ok
        and output.get("qty") is not None
        and expected.get("qty") is not None
        and abs(float(output["qty"]) - float(expected["qty"])) < 1e-6
    )
    got = f"{output.get('status')} {(output.get('food') or {}).get('name')!r} " \
          f"{output.get('serving_id')} x {output.get('qty')}"
    exp = f"{(expected.get('food') or {}).get('name')!r} {expected.get('serving_id')} x {expected.get('qty')}"
    return [
        ("food_match", float(food_ok), f"got {got} | expected {exp}"),
        ("serving_match", float(serving_ok), f"got {output.get('serving_id')} | expected {expected.get('serving_id')}"),
        ("qty_match", float(qty_ok), f"got {output.get('qty')} | expected {expected.get('qty')}"),
        ("match", float(food_ok and serving_ok and qty_ok), "food, serving and qty all match"),
    ]


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


@dataclass
class ItemRun:
    dataset_item_id: str
    trace_id: str
    observation_id: str
    output: dict
    scores: list[Score]


@dataclass
class ReplayRun:
    dataset: str
    run_name: str
    dataset_run_id: str | None
    items: list[ItemRun] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        vals = [v for it in self.items for n, v, _ in it.scores if n == "match"]
        return sum(vals) / len(vals) if vals else 0.0


async def replay(
    *,
    dataset: str = CORRECTIONS_DATASET,
    limit: int | None = None,
    tier3: bool = False,
    run_name: str | None = None,
    lf: Langfuse | None = None,
) -> ReplayRun:
    if tier3:
        _check_tier3_writes()  # before any run is created
    await bind_owner()  # the cascade's reads (and --tier3's scratch rows) are the owner's (002)
    lf = lf or setup_langfuse(REPLAY_ENVIRONMENT)
    ds = lf.get_dataset(dataset)
    items = [i for i in ds.items if i.status == "ACTIVE"]
    if limit is not None:
        items = items[:limit]
    run_name = run_name or f"replay-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    description = f"evals.replay over '{dataset}' (tier3={'on' if tier3 else 'off'})"
    run = ReplayRun(dataset=dataset, run_name=run_name, dataset_run_id=None)
    print(f"[replay] {len(items)} item(s) from '{dataset}' -> run '{run_name}' "
          f"(environment={REPLAY_ENVIRONMENT}, tier3={'on' if tier3 else 'off'})")

    for item in items:
        inp = item.input or {}
        meta = {"dataset_id": item.dataset_id, "dataset_item_id": item.id, "run_name": run_name}
        with propagate_attributes(environment=REPLAY_ENVIRONMENT):
            with lf.start_as_current_observation(
                name="replay-item", as_type="span", input=inp, metadata=meta
            ) as span:
                try:
                    ctx = (context_from_snapshot(inp["context"]) if inp.get("context")
                           else await _fresh_context())
                    output = await stage(
                        inp["utterance"], Meal(inp.get("meal") or "snack"), ctx,
                        int(inp.get("item_index") or 0), inp.get("phrase") or inp["utterance"],
                        expected=item.expected_output, tier3=tier3,
                    )
                except Exception as e:  # one bad item never sinks the run
                    output = {"status": "error", "error": f"{type(e).__name__}: {e}"}
                    span.update(level="ERROR", status_message=str(e))
                span.update(output=output)
                scores = evaluate(output, item.expected_output)
                trace_id, observation_id = span.trace_id, span.id

        run_item = lf.api.dataset_run_items.create(
            run_name=run_name,
            run_description=description,
            dataset_item_id=item.id,
            trace_id=trace_id,
            observation_id=observation_id,
            metadata={"tier3": tier3, "environment": REPLAY_ENVIRONMENT},
        )
        run.dataset_run_id = run_item.dataset_run_id
        for name, value, comment in scores:
            lf.create_score(
                trace_id=trace_id, observation_id=observation_id,
                name=name, value=value, comment=comment, environment=REPLAY_ENVIRONMENT,
            )
        run.items.append(ItemRun(item.id, trace_id, observation_id, output, scores))
        match = next(v for n, v, _ in scores if n == "match")
        print(f"[replay] {item.id}: {inp.get('utterance')!r} -> {output.get('status')} "
              f"{(output.get('food') or {}).get('name')!r} match={match:.0f}")

    if run.dataset_run_id:
        lf.create_score(
            dataset_run_id=run.dataset_run_id, name="match_rate", value=run.match_rate,
            comment=f"{len(run.items)} items", environment=REPLAY_ENVIRONMENT,
        )
    lf.flush()
    print(f"[replay] match_rate={run.match_rate:.2f} over {len(run.items)} item(s); "
          f"dataset_run_id={run.dataset_run_id}")
    return run


async def _fresh_context() -> AgentContext:
    from services.context import build_agent_context

    return await build_agent_context()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", default=CORRECTIONS_DATASET)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--tier3", action="store_true", help="also run the tier-3 agent on searching rows")
    p.add_argument("--run-name", default=None)
    args = p.parse_args(argv)
    setup_langfuse(REPLAY_ENVIRONMENT)  # before asyncio.run: sets the Windows loop policy
    asyncio.run(replay(dataset=args.dataset, limit=args.limit, tier3=args.tier3,
                       run_name=args.run_name))


if __name__ == "__main__":
    main()
