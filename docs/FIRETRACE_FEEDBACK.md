# FireTrace feedback — what Tally still needs from it

Written 2026-09-06 while moving Tally's tracing from Langfuse Cloud to FireTrace
(`backend/tracing.py`, DESIGN.md §10). Everything the app records at request time moved
over. The items below are the Langfuse features the improvement loop and the eval
harness (`backend/evals/`) still depend on and FireTrace's API (`/api/v1/openapi.json`,
read 2026-09-06) does not offer. Each says what the code does today and the smallest API
that would let it move. In priority order.

## 1. Find traces by score — blocks the §10 loop

**Today.** At commit, `resolver/commit.py` posts `user_edited` (0/1), `was_disambiguated`
(0/1) and `tier` per item onto the trace with `POST /api/v1/traces/{id}/scores`, comment
`item N`. The workflow the design is built around: open the viewer, filter to the traces
where `user_edited = 1`, read what the model saw and chose, fix the prompt or the ranking
rule, watch the rate fall.

**FireTrace now.** `GET /api/v1/traces` has no score filter. `GET /api/v1/scores` filters by
`name`, `environment`, `from`, `to` only — not by value — and returns scores, not traces.
The trace's `scores` summary keeps the newest score per name, so with per-item scores
of the same name the summary shows the last item's verdict, not "any item was edited".

**Ask.** (a) A score filter on trace listing, `GET /api/v1/traces?score=user_edited&scoreValue=1`
(or `scoreMin`/`scoreMax` for numeric), and the same control in the dashboard's filter
bar. (b) A `value` filter (and a `spanId` filter) on `GET /api/v1/scores`. (c) Either an
"any score of this name" option for the summary, or a documented convention — we can
post `user_edited.3` per item and `user_edited` as the per-trace maximum if that is the
intended shape; say which.

## 2. Datasets and dataset items — blocks `evals/collect.py`

**Today.** `evals/collect.py` turns each corrected production trace into a dataset item in
the dataset `usual-corrections`: `id` (`corr-<trace_id>-<item_index>`), `input`
(`{utterance, item_index, phrase, meal, context}` — the §6.1 context snapshot the prompt
saw), `expected_output` (`{food: {id, name, brand}, serving_id, qty}`), `metadata`
(`{log_id, trace_id, tier, proposed}`), `source_trace_id`. Create-if-missing dataset,
idempotent by item id, list existing items by source trace id.

**Ask.** `POST/GET /api/v1/datasets` (name, description), `PUT/GET
/api/v1/datasets/{name}/items` (upsert by `id`; list with a `sourceTraceId` filter), items
with free-JSON `input`, `expectedOutput`, `metadata` and a `sourceTraceId` that links
back to the trace in the viewer.

## 3. Experiment runs over a dataset — blocks `evals/replay.py`

**Today.** `evals/replay.py` runs every item through the production pipeline (decompose →
cascade, optionally tier 3), records one trace per item, links that trace (and its top
span) to the item under a named run (`replay-<timestamp>`, a description, metadata
`{tier3, environment}`), scores each item (`food_match`, `serving_match`, `qty_match`,
`match`) and posts one run-level score (`match_rate`). Runs are compared across prompt
versions item by item.

**Ask.** `POST /api/v1/datasets/{name}/runs` (name, description, metadata),
`POST /api/v1/datasets/{name}/runs/{run}/items` (`datasetItemId`, `traceId`, optional
`spanId`), run-level scores, and a dashboard page that lays runs side by side per item
(trace link + the score columns). Per-item scores already exist as trace scores with
`spanId`; nothing new is needed there.

## 4. Per-request environment override — nice to have

**Today (Langfuse).** `propagate_attributes(environment="test")` stamped eval traces `test`
from the same process and key as `development`. **FireTrace.** The key decides; a process
that mixes environments needs a second key and a client that swaps keys per call. Works,
just clumsier for a replay harness.

**Ask.** An optional `X-FireTrace-Environment` request header, honoured only when the key's
settings allow that environment (an allow-list per key), else 400.

## 5. Span-level error message — small

**Today (Langfuse).** Spans carried `level=ERROR` and a `status_message`. **FireTrace.**
Spans and traces have `status: error` only, so the message rides in
`attributes.error` / `attributes.error_type`, which the inspector shows as plain
attributes.

**Ask.** An optional `statusMessage` (≤ 2,000 chars) on spans and traces, surfaced next to
the status in the inspector and the trace list's error count.

## 6. Hosted tool calls have no timing — inspector

OpenAI's hosted `web_search` runs inside a model turn and reports no timing of its own.
Tally records each one as a child `tool` span of the `response` span, bounded by the
turn, with `attributes.hosted = true`, `input.query` and `output.sources`. If the
waterfall could render such spans as "within parent" (dashed, or flagged), the timing
would read honestly; otherwise nothing to change.

## 7. Size limits — which document was too big

A tier-3 run carries the growing conversation on every model turn, so six turns of
`response` input approach the 768 kB per-document limit before the 2 MiB request limit.
Tally caps strings at 4,000 characters and lists at 100 items, and truncates every span's
input/output when the body passes 1.5 MiB. A `413` whose `message` names the offending
span id would let the client truncate only that span.

## 8. Streaming fixes a trace's input at the start — blocks streaming

Added 2026-09-11, after the streaming routes shipped. **Today.** Tally still sends one
whole trace per run (DESIGN.md §10). It records a trace only once a model runs, so the
earliest it could send a streamed start is the first model span. In the voice lanes
that span is `transcribe`, and its result, the transcript, becomes the trace's `input`.
**FireTrace now.** `input`, `sessionId` and `userId` go only in the start request; the
end request has no `input`, and `PATCH /api/v1/traces/{id}` merges `metadata` only. A
streamed voice trace would keep `{mime, bytes}` as its input for good.

**Ask.** Accept `input` (and `sessionId`, `userId`) on `POST /api/v1/traces/{id}/end`,
settable once if the start left them out.

## What moved without any gap

- Traces with a nested span tree; `kind`, `status`, ISO timestamps, `provider`/`model`
  and token `usage` per span; `sessionId` (the log id) and `userId` (the Neon Auth `sub`)
  as the dashboard's filters; `tags` for the lane and owner/demo.
- Scores by trace id at commit time, append-only, with a comment —
  `POST /api/v1/traces/{id}/scores`.
- Idempotent ingest (same id and body → `200 duplicate: true`), which keeps the client's
  retry loop simple.
- Environments decided by the key, stamped server-side — one key per Vercel scope.
- Strict `additionalProperties: false` and the machine-readable schema at
  `/api/v1/openapi.json` — the offline test suite checks payloads against the allowed
  keys, so drift shows up before a 400 does.
