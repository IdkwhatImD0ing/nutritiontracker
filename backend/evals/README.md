# evals — the correction → replay loop (DESIGN.md §10, §12 M3)

Three commands, all from `backend/`:

```sh
# 1. Curate: production traces the user corrected at commit (score user_edited=1)
#    → Langfuse dataset 'usual-corrections' (idempotent; re-run any time)
uv run python -m evals.collect --environment production --days 30

# 2. Change something — a prompt in prompts/, a ranking rule in resolver/cascade.py, ...

# 3. Replay: the dataset through the SAME decompose + resolver cascade as production,
#    recorded as a dataset run (experiment) under environment=test
uv run python -m evals.replay --dataset usual-corrections --limit 50
```

Then open the dataset in Langfuse → *Runs* and compare `match_rate` across runs.
Each run item links to its trace (environment `test`, never mixed into the
production series) with scores `food_match`, `serving_match`, `qty_match`, `match`.

## What a dataset item holds

| field | content |
|---|---|
| `id` | `corr-<trace_id>-<item_index>` — collect skips ids already present |
| `input` | `{utterance, item_index, phrase, meal, context}` — `context` is the §6.1 snapshot (profile notes, targets, inlined food index, meal names) taken at collection time |
| `expected_output` | `{food: {id, name, brand}, serving_id, qty}` — what the user actually committed (`item_resolutions.final_*`) |
| `metadata` | `{log_id, trace_id, tier, proposed: {...}}` — what the app proposed before the edit |
| `source_trace_id` | the scored production trace |

The Langfuse trace is only the pointer; the record comes from Postgres
(`logs.trace_id` / `item_resolutions.trace_id`), so a correction can be
collected after Langfuse's 30-day retention has dropped the trace's spans —
as long as the score itself is still queryable.

## What replay does and does not touch

- Runs `cascade.short_circuit_all → services.decompose → cascade.resolve_items`
  (tiers 1–2) — the same per-log entry points `routes/logs.py` uses, so a
  saved-meal utterance replays as its N tier-0 rows — then picks the row this
  dataset item is about by phrase, then expected food, then position
  (a tier-0 expansion shifts every later `item_index`). Decompose is a real
  `gpt-5.6-luna` call, wrapped in the same `tracing.generation` as production.
  The item's context snapshot is reused so a prompt change is the only variable.
- `--tier3` also runs `services.tier3.run_tier3` on rows that stage as
  `searching`. That path needs a staged row, so replay inserts a scratch
  `logs` (`input_mode='eval'`) + `item_resolutions` row and deletes both
  afterwards; foods the agent resolves stay cached, as in production. Because
  those writes land in whatever `DATABASE_URL` resolves to — production today
  (BACKLOG 18) — `--tier3` refuses to start unless `USUAL_EVAL_DB_WRITES=1` is
  set or the database is local. A run killed mid-flight leaves orphans behind:
  they are the `logs` rows with `input_mode = 'eval'`, plus their
  `item_resolutions`.
- Never commits, never learns: `entries` and `aliases` are never written
  (`tests/test_evals_live.py` asserts this by row count).

## Why not `Langfuse.run_experiment`

The v4 SDK stamps every experiment span with the fixed environment
`sdk-experiment` (`langfuse/_client/constants.py`, applied in
`client.py::_process_experiment_item` and `propagation.py`), which would
defeat the §10 production/test split. `replay.py` wires the same primitives
by hand — `start_as_current_observation` + `api.dataset_run_items.create` +
`create_score` — so runs land under `environment=test` and still show up as
dataset runs in the UI.

## Test

```sh
uv run --with pytest pytest backend/tests/test_evals_live.py -q -s
```

Live: seeds a scratch food + alias, creates a 2-item synthetic dataset
(`usual-evals-smoke`), replays it, verifies the run, its traces (environment
`test`) and scores via the Langfuse API, and that the rows replay could have
touched (that food/alias, its entries, logs/item_resolutions for the two
utterances) are unchanged. Cleans up the run, items, food and alias; the empty
dataset stays.
