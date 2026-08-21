"""POST /api/logs + GET /api/logs/{id} (DESIGN.md §5, §3.0, §3.4, §7.2).

M0 text-only: audio/STT lands in M2 behind the Transcriber interface.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Json

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
    # retry cannot create two logs.
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
        existing = await _load_log_response(body.id)
        if existing is not None and existing.items:
            return existing  # replay: return the stored staging state
        log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (body.id,))
        if log is not None and log["status"] != "staged":
            raise HTTPException(409, f"log already {log['status']}")
        # staged log with no items (an earlier attempt died mid-pipeline) → re-run

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


@router.get("/api/logs/{log_id}", response_model=LogResponse)
async def get_log(log_id: UUID) -> LogResponse:
    resp = await _load_log_response(log_id, watchdog=True)
    if resp is None:
        raise HTTPException(404, "log not found")
    return resp


async def _load_log_response(log_id: UUID, watchdog: bool = False) -> LogResponse | None:
    log = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
    if log is None:
        return None
    rows = await db.fetchall(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND status != 'removed' "
        "ORDER BY item_index",
        (log_id,),
    )
    now = datetime.now(timezone.utc)
    items: list[StagedItem] = []
    for r in rows:
        staged = StagedItem.model_validate(r["staged"])
        if watchdog and r["status"] == "searching":
            # §3.4 failure lifecycle: stale after 60s; the client re-kicks a
            # stale item once, and at >= 2 attempts the server flips it to
            # not_found (manual search box). No item can spin forever.
            started = r["tier3_started_at"] or r["created_at"]
            if (now - started).total_seconds() > _STALE_SECONDS and (
                r["tier3_attempts"] >= _MAX_TIER3_ATTEMPTS
            ):
                staged = staged.model_copy(update={"status": "not_found"})
                await db.execute(
                    "UPDATE item_resolutions SET status = 'not_found', staged = %s "
                    "WHERE id = %s",
                    (Json(staged.model_dump(mode="json")), r["id"]),
                )
        items.append(staged)
    meal = Meal(log["meal_inferred"]) if log["meal_inferred"] else Meal.snack
    return LogResponse(log_id=log["id"], meal=meal, items=items)
