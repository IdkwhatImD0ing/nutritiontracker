"""POST/GET/PATCH/DELETE /api/logs (DESIGN.md §5, §3.0, §3.4, §7.2, §9).

M0 text-only: audio/STT lands in M2 behind the Transcriber interface.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Json
from pydantic import BaseModel, Field

import db
from auth import require_user
from models import LogCreate, LogResponse, Meal, StagedItem
from resolver import cascade
from resolver.norm import norm

router = APIRouter(dependencies=[Depends(require_user)])

_DEFAULT_WINDOWS = {
    "breakfast": {"start": "04:00", "end": "11:00"},
    "lunch": {"start": "11:00", "end": "15:30"},
    "dinner": {"start": "15:30", "end": "21:30"},
    "default": "snack",
}

_STALE_SECONDS = 60
_MAX_TIER3_ATTEMPTS = 2
# How long a staged-but-itemless log is presumed owned by the request that
# inserted it before a duplicate POST may claim the re-run (§3.4-adjacent;
# the pipeline worst case is one decompose call, well under this).
_RERUN_GRACE_SECONDS = 30


# ---------------------------------------------------------------------------
# Route-local wire types — additive over models.py (the shared contract there
# is untouched); W5 picks these up from /openapi.json like everything else.
# ---------------------------------------------------------------------------


class StagedItemOut(StagedItem):
    """A StagedItem as returned by GET /api/logs/{id}, plus the watchdog flag.

    stale=true marks a status="searching" item whose tier-3 run started (or
    was staged) more than 60s ago with tier3_attempts < 2: the client should
    re-kick it once via POST /api/resolve/tier3 (§3.4 failure lifecycle).
    Computed at poll time, never persisted; null/absent everywhere else.
    """

    stale: bool | None = None


class LogPollResponse(LogResponse):
    """LogResponse whose items can carry the watchdog's stale flag."""

    items: list[StagedItemOut]  # type: ignore[assignment]


class TimingsPatch(BaseModel):
    """§9: client-observed capture-start → list-rendered, in milliseconds."""

    staged_ms: int = Field(ge=0)


class TimingsResponse(BaseModel):
    log_id: UUID
    staged_ms: int


class DiscardResponse(BaseModel):
    log_id: UUID
    status: Literal["discarded"] = "discarded"


class RemoveItemResponse(BaseModel):
    log_id: UUID
    item_index: int
    status: Literal["removed"] = "removed"


async def infer_meal(captured_at: datetime, tz: str | None) -> Meal:
    """§7.2: configurable clock windows from settings; no model involved."""
    row = await db.fetchone("SELECT value FROM settings WHERE key = 'meal_windows'")
    windows = row["value"] if row else _DEFAULT_WINDOWS
    tzname = tz
    if not tzname:
        trow = await db.fetchone("SELECT value FROM settings WHERE key = 'timezone'")
        tzname = trow["value"] if trow else "UTC"
    try:
        local = captured_at.astimezone(ZoneInfo(tzname))
    except Exception:
        local = captured_at.astimezone(timezone.utc)
    hhmm = local.strftime("%H:%M")
    for meal_name in ("breakfast", "lunch", "dinner"):
        w = windows.get(meal_name)
        if w and w["start"] <= hhmm < w["end"]:
            return Meal(meal_name)
    return Meal(windows.get("default", "snack"))


@router.post("/api/logs", response_model=LogResponse)
async def create_log(body: LogCreate) -> LogResponse:
    # Idempotent upsert by client id (§5, §8.6): a concurrent double-drain or
    # retry cannot create two logs — and only the request that actually
    # inserted the row (or claimed an orphan, below) runs the pipeline.
    inserted = await db.execute(
        """
        INSERT INTO logs (id, input_mode, raw_input, captured_at, capture_fallback,
                          capture_error)
        VALUES (%s, 'text', %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (body.id, body.text, body.captured_at, body.capture_fallback, body.capture_error),
    )
    if inserted == 0:
        replay = await _duplicate_post_response(body.id)
        if replay is not None:
            return replay
        # orphaned staged log claimed (earlier attempt died mid-pipeline) → re-run

    meal = await infer_meal(body.captured_at, body.tz)

    # §3.0 whole-utterance short-circuit — the LLM leaves the hot path entirely
    sc = await cascade.short_circuit(body.text, meal)
    if sc is not None:
        await _persist(body.id, [sc], meal, short_circuit=True)
        return LogResponse(log_id=body.id, meal=meal, items=[sc.staged])

    # decompose (gpt-5.6-luna) wrapped in a tracing generation (§10)
    import tracing
    from services.context import build_agent_context
    from services.decompose import decompose

    tracing.init_tracing()
    ctx = await build_agent_context()
    t0 = time.monotonic()
    trace_id: str | None = None
    async with tracing.generation("decompose") as gen:
        parsed = await decompose(body.text, ctx)
        gen.update(input=body.text, output=parsed.model_dump(mode="json"))
        trace_id = gen.trace_id
    decompose_ms = int((time.monotonic() - t0) * 1000)
    try:
        tracing.flush()  # serverless flush rule (§10)
    except Exception:
        pass

    if parsed.meal_hint is not None:
        meal = parsed.meal_hint
    # Seam (M1): when_hint shifts logged_at ("this morning" → that morning, §7.2).

    resolutions = [
        await cascade.resolve_item(p, i, meal) for i, p in enumerate(parsed.items)
    ]
    await _persist(
        body.id, resolutions, meal,
        short_circuit=False, decompose_ms=decompose_ms, trace_id=trace_id,
    )
    return LogResponse(log_id=body.id, meal=meal, items=[r.staged for r in resolutions])


async def _duplicate_post_response(log_id: UUID) -> LogResponse | None:
    """Duplicate POST for a log id this request did not insert (§8.6 retries).

    Returns the response to send — the stored staging state — or None when
    this request has atomically claimed an orphaned staged log (its inserting
    request died mid-pipeline) and the caller must re-run the pipeline.

    The concurrent same-id race is closed here: while another request
    plausibly owns the row (it is younger than _RERUN_GRACE_SECONDS), a
    duplicate never re-runs decompose/resolve — it hands back whatever is
    persisted (possibly zero items) and the client polls GET for the rest.
    """
    existing = await _load_log_response(log_id)
    if existing is None:
        # the conflicting row vanished between INSERT and this read
        raise HTTPException(409, "log in transition — retry")
    if existing.items:
        return existing  # replay: the stored staging state
    log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,))
    if log is not None and log["status"] != "staged":
        raise HTTPException(409, f"log already {log['status']}")
    # Staged with no items: the owner is either still mid-pipeline or dead.
    # After the grace period, claim the re-run atomically — created_at doubles
    # as the claim stamp (nothing reads logs.created_at for semantics), so
    # exactly one of any number of concurrent retries wins the UPDATE.
    claimed = await db.execute(
        """
        UPDATE logs SET created_at = now()
        WHERE id = %s AND status = 'staged'
          AND created_at <= now() - make_interval(secs => %s)
        """,
        (log_id, _RERUN_GRACE_SECONDS),
    )
    if claimed == 0:
        return existing  # a concurrent request owns the pipeline; client polls
    return None  # orphan claimed — caller re-runs the pipeline


async def _persist(
    log_id: UUID,
    resolutions: list[cascade.Resolution],
    meal: Meal,
    *,
    short_circuit: bool,
    decompose_ms: int | None = None,
    trace_id: str | None = None,
) -> None:
    """All staged state is persisted server-side (§3): the client is a renderer."""
    async with db.tx() as conn:
        await conn.execute(
            """
            UPDATE logs SET short_circuit = %s, meal_inferred = %s, decompose_ms = %s,
                            trace_id = %s
            WHERE id = %s
            """,
            (short_circuit, meal.value, decompose_ms, trace_id, log_id),
        )
        for r in resolutions:
            item = r.staged
            staged_payload = item.model_dump(mode="json")
            if item.status == "searching":
                # §7.3 rule 1: the spoken qty/unit survive to tier 3 in the
                # staged jsonb (extra keys; StagedItem validation ignores them).
                staged_payload["spoken_qty"] = item.qty
                staged_payload["spoken_unit"] = r.unit
            if r.state_answer:
                # §3.4a: known cooked/raw intent rides the staged jsonb —
                # commit persists it to aliases.state_answer (asked once, ever)
                staged_payload["state_answer"] = r.state_answer
            await conn.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                              status, staged, tier, proposed_food_id,
                                              proposed_serving_id, proposed_qty,
                                              alias_id, resolve_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (log_id, item_index) DO NOTHING
                """,
                (
                    log_id,
                    item.item_index,
                    item.phrase,
                    norm(item.phrase),
                    item.status,
                    Json(staged_payload),
                    item.tier,
                    item.food.id if item.food else None,
                    item.serving.id if item.serving else None,
                    # proposed_* mean "the pre-selection shown to the user" —
                    # a searching row carries no proposal (§3.5 explicit-choice
                    # semantics key off these at commit).
                    item.qty if item.status == "resolved" else None,
                    r.alias_id,
                    r.resolve_ms,
                ),
            )


@router.get("/api/logs/{log_id}", response_model=LogPollResponse)
async def get_log(log_id: UUID) -> LogPollResponse:
    resp = await _load_log_response(log_id, watchdog=True)
    if resp is None:
        raise HTTPException(404, "log not found")
    return resp


async def _load_log_response(
    log_id: UUID, watchdog: bool = False
) -> LogPollResponse | None:
    log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
    if log is None:
        return None
    rows = await db.fetchall(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND status != 'removed' "
        "ORDER BY item_index",
        (log_id,),
    )
    now = datetime.now(timezone.utc)
    items: list[StagedItemOut] = []
    for r in rows:
        # Mutate the raw payload, not a validated model: the staged jsonb can
        # carry extra keys (spoken_qty/spoken_unit, §7.3) a model round-trip
        # would silently drop.
        raw = dict(r["staged"])
        stale = False
        if watchdog and r["status"] == "searching":
            # §3.4 failure lifecycle: a searching item is stale after 60s.
            # attempts < 2 → return stale=true so the client re-kicks exactly
            # once; attempts >= 2 → the server flips it to not_found (manual
            # search box). No item can spin forever.
            started = r["tier3_started_at"] or r["created_at"]
            if (now - started).total_seconds() > _STALE_SECONDS:
                if r["tier3_attempts"] >= _MAX_TIER3_ATTEMPTS:
                    raw["status"] = "not_found"
                    await db.execute(
                        "UPDATE item_resolutions SET status = 'not_found', "
                        "staged = %s WHERE id = %s AND status = 'searching'",
                        (Json(raw), r["id"]),
                    )
                else:
                    stale = True
        item = StagedItemOut.model_validate(raw)
        if stale:
            item = item.model_copy(update={"stale": True})
        items.append(item)
    meal = Meal(log["meal_inferred"]) if log["meal_inferred"] else Meal.snack
    return LogPollResponse(log_id=log["id"], meal=meal, items=items)


@router.patch("/api/logs/{log_id}/timings", response_model=TimingsResponse)
async def patch_timings(log_id: UUID, body: TimingsPatch) -> TimingsResponse:
    """§9: staged_ms has exactly one write path — this one — and it writes
    only while NULL, so the first render's timing can never be overwritten."""
    updated = await db.execute(
        "UPDATE logs SET staged_ms = %s WHERE id = %s AND staged_ms IS NULL",
        (body.staged_ms, log_id),
    )
    if updated:
        return TimingsResponse(log_id=log_id, staged_ms=body.staged_ms)
    log = await db.fetchone("SELECT staged_ms FROM logs WHERE id = %s", (log_id,))
    if log is None:
        raise HTTPException(404, "log not found")
    if log["staged_ms"] == body.staged_ms:
        # idempotent replay of the same measurement — no-op
        return TimingsResponse(log_id=log_id, staged_ms=log["staged_ms"])
    raise HTTPException(409, "staged_ms already recorded")


@router.delete(
    "/api/logs/{log_id}/items/{item_index}", response_model=RemoveItemResponse
)
async def remove_staged_item(log_id: UUID, item_index: int) -> RemoveItemResponse:
    """§7.1: removing a staged row writes item_resolutions.status='removed'
    immediately, so the decompose error stays visible in telemetry without
    waiting for commit (commit-time absence remains the backstop). A removed
    row never writes an alias and never reaches entries."""
    updated = await db.execute(
        "UPDATE item_resolutions SET status = 'removed' "
        "WHERE log_id = %s AND item_index = %s AND committed_at IS NULL",
        (log_id, item_index),
    )
    if updated:  # freshly removed, or an idempotent replay of a removed row
        return RemoveItemResponse(log_id=log_id, item_index=item_index)
    row = await db.fetchone(
        "SELECT 1 AS present FROM item_resolutions WHERE log_id = %s AND item_index = %s",
        (log_id, item_index),
    )
    if row is None:
        raise HTTPException(404, "unknown log item")
    raise HTTPException(409, "item already committed")


@router.delete("/api/logs/{log_id}", response_model=DiscardResponse)
async def discard_log(log_id: UUID) -> DiscardResponse:
    """§5: discard a staged log — status 'discarded' is adherence-relevant
    data (§9), so sheet close must land here, not just drop client state."""
    updated = await db.execute(
        "UPDATE logs SET status = 'discarded' WHERE id = %s AND status = 'staged'",
        (log_id,),
    )
    if updated:
        return DiscardResponse(log_id=log_id)
    log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,))
    if log is None:
        raise HTTPException(404, "log not found")
    if log["status"] == "discarded":
        return DiscardResponse(log_id=log_id)  # idempotent replay
    raise HTTPException(409, f"log already {log['status']}")
