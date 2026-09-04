"""Collect corrected traces into the Langfuse dataset 'usual-corrections' (DESIGN.md §10).

A correction is a trace scored user_edited=1 at commit (resolver/commit.py
_post_scores). Langfuse holds only the pointer - the record lives in Postgres
(§10: traces are the 30-day workbench, Postgres the permanent archive):
logs.trace_id (decompose trace) or item_resolutions.trace_id (tier-3 trace)
-> the utterance and the final food/serving/qty the user actually committed.

One dataset item per edited item of that log:
  id               corr-<trace_id>-<item_index>    (idempotent: existing items are skipped)
  input            {utterance, item_index, phrase, meal, context}   context = §6.1 snapshot
  expected_output  {food: {id, name, brand}, serving_id, qty}
  metadata         {log_id, trace_id, tier, proposed: {food_id, serving_id, qty}}
  source_trace_id  the scored trace

The context snapshot is taken at collection time (production traces record the
utterance, not the inlined food index), so collect soon after the correction.

Usage (from backend/):
  uv run python -m evals.collect [--environment production] [--days 30] [--limit 200] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from langfuse import Langfuse
from langfuse.api import NotFoundError

from evals.common import CORRECTIONS_DATASET, bind_owner, setup_langfuse, snapshot_context

_ITEMS_SQL = """
SELECT r.item_index, r.phrase, r.tier,
       r.proposed_food_id::text AS proposed_food_id, r.proposed_serving_id, r.proposed_qty,
       r.final_food_id::text AS final_food_id, r.final_serving_id, r.final_qty,
       f.display_name, f.brand
FROM item_resolutions r
JOIN foods f ON f.id = r.final_food_id
WHERE r.log_id = %s AND r.was_edited AND r.committed_at IS NOT NULL
ORDER BY r.item_index
"""


def corrected_trace_ids(
    lf: Langfuse, environment: str, since: datetime, limit: int
) -> list[str]:
    """Trace ids with a user_edited=1 score in `environment` since `since`."""
    seen: list[str] = []
    cursor: str | None = None
    while len(seen) < limit:
        page = lf.api.scores_v3.get_many_v3(
            name="user_edited",
            data_type="NUMERIC",
            value="1",
            environment=environment,
            from_timestamp=since,
            fields="subject",
            limit=100,
            cursor=cursor,
        )
        for score in page.data:
            subject = score.subject
            trace_id = getattr(subject, "trace_id", None) or (
                subject.id if getattr(subject, "kind", None) == "trace" else None
            )
            if trace_id and trace_id not in seen:
                seen.append(trace_id)
        cursor = page.meta.cursor
        if not cursor:
            break
    return seen[:limit]


async def corrections_for_trace(trace_id: str) -> list[dict]:
    """The committed, edited items behind one scored trace (Postgres is the record)."""
    import db

    only_index: int | None = None
    log = await db.fetchone(
        "SELECT id, raw_input, meal_inferred FROM logs WHERE trace_id = %s", (trace_id,)
    )
    if log is None:  # tier-3 trace: scoped to its one item
        res = await db.fetchone(
            "SELECT log_id, item_index FROM item_resolutions WHERE trace_id = %s",
            (trace_id,),
        )
        if res is None:
            return []
        only_index = res["item_index"]
        log = await db.fetchone(
            "SELECT id, raw_input, meal_inferred FROM logs WHERE id = %s", (res["log_id"],)
        )
        if log is None:
            return []
    rows = await db.fetchall(_ITEMS_SQL, (log["id"],))
    return [
        {
            **r,
            "log_id": str(log["id"]),
            "utterance": log["raw_input"],
            "meal": log["meal_inferred"] or "snack",
        }
        for r in rows
        if only_index is None or r["item_index"] == only_index
    ]


def dataset_item(c: dict, trace_id: str, context: dict) -> dict:
    """kwargs for Langfuse.create_dataset_item."""

    def num(v):
        return float(v) if v is not None else None

    return {
        "id": f"corr-{trace_id}-{c['item_index']}",
        "input": {
            "utterance": c["utterance"],
            "item_index": c["item_index"],
            "phrase": c["phrase"],
            "meal": c["meal"],
            "context": context,
        },
        "expected_output": {
            "food": {"id": c["final_food_id"], "name": c["display_name"], "brand": c["brand"]},
            "serving_id": c["final_serving_id"],
            "qty": num(c["final_qty"]),
        },
        "metadata": {
            "log_id": c["log_id"],
            "trace_id": trace_id,
            "tier": c["tier"],
            "proposed": {
                "food_id": c["proposed_food_id"],
                "serving_id": c["proposed_serving_id"],
                "qty": num(c["proposed_qty"]),
            },
        },
        "source_trace_id": trace_id,
    }


def ensure_dataset(lf: Langfuse, name: str) -> None:
    try:
        lf.api.datasets.get(name)
    except NotFoundError:
        lf.create_dataset(
            name=name,
            description="Production utterances the user corrected at commit "
            "(user_edited=1), with the committed food/serving/qty as expected output.",
        )


def existing_item_ids(lf: Langfuse, dataset: str, trace_id: str) -> set[str]:
    page = lf.api.dataset_items.list(dataset_name=dataset, source_trace_id=trace_id, limit=100)
    return {item.id for item in page.data}


async def collect(
    *,
    environment: str,
    days: int,
    limit: int,
    dataset: str = CORRECTIONS_DATASET,
    dry_run: bool = False,
) -> list[dict]:
    """Returns the items written (or, dry-run, the items that would be)."""
    from services.context import build_agent_context

    await bind_owner()  # Postgres reads below run as the owner's tenant (002)
    lf = setup_langfuse()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    trace_ids = corrected_trace_ids(lf, environment, since, limit)
    print(f"[collect] {len(trace_ids)} trace(s) scored user_edited=1 in '{environment}' "
          f"since {since:%Y-%m-%d}")
    if not trace_ids:
        return []

    if not dry_run:
        ensure_dataset(lf, dataset)
    context = snapshot_context(await build_agent_context())

    written: list[dict] = []
    for trace_id in trace_ids:
        corrections = await corrections_for_trace(trace_id)
        if not corrections:
            print(f"[collect] {trace_id}: no committed edited items in Postgres - skipped")
            continue
        present = set() if dry_run else existing_item_ids(lf, dataset, trace_id)
        for c in corrections:
            item = dataset_item(c, trace_id, context)
            if item["id"] in present:
                print(f"[collect] {item['id']}: already in dataset - skipped")
                continue
            print(f"[collect] {item['id']}: {c['utterance']!r} -> "
                  f"{c['display_name']} / {c['final_serving_id']} x {item['expected_output']['qty']}")
            if not dry_run:
                lf.create_dataset_item(dataset_name=dataset, **item)
            written.append(item)
    print(f"[collect] {'would write' if dry_run else 'wrote'} {len(written)} item(s) to '{dataset}'")
    return written


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--environment", default="production")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=200, help="max traces to consider")
    p.add_argument("--dataset", default=CORRECTIONS_DATASET)
    p.add_argument("--dry-run", action="store_true", help="print items, write nothing")
    p.add_argument("--json", action="store_true", help="also dump the items as JSON")
    args = p.parse_args(argv)

    setup_langfuse()  # before asyncio.run: sets the Windows selector loop policy
    items = asyncio.run(
        collect(environment=args.environment, days=args.days, limit=args.limit,
                dataset=args.dataset, dry_run=args.dry_run)
    )
    if args.json:
        print(json.dumps(items, indent=2, default=str))


if __name__ == "__main__":
    main()
