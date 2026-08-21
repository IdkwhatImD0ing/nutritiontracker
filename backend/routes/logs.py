"""POST/GET/PATCH/DELETE /api/logs (DESIGN.md §5, §3.0, §3.4, §7.2, §8.1, §9).

M2: POST /api/logs accepts multipart (audio blob + the same fields) alongside
the JSON body; audio runs through the Transcriber interface
(services/transcribe.py) and an empty/near-empty transcript returns the §5
capture_failure status — never an empty staged log.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.routing import APIRoute
from psycopg.types.json import Json
from pydantic import BaseModel, Field
from starlette.routing import Match

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
# inserted it before a duplicate POST may claim the re-run. BACKLOG 30: the
# window must exceed the pipeline's worst case between insert and persist —
# one decompose call. Observed live: median 2.5s, worst 2.9s over 4 calls
# (test_decompose_live, 2026-08-21), so the old 30s already covered a healthy
# provider — but the openai SDK retries 5xx/timeouts internally (default
# max_retries=2, with backoff), so a degraded-provider decompose can
# legitimately run for tens of seconds before failing or succeeding. 90s
# covers that tail and matches the client's own GET-poll hard stop (§5) — a
# retry never claims a pipeline the client is still willing to wait on.
# (Voice adds nothing here: STT completes before the log row is inserted.)
_RERUN_GRACE_SECONDS = 90


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


class CaptureFailureResponse(BaseModel):
    """§5: an audio log whose transcript came back empty/near-empty. The
    client prompts retype; the logs row records the failure so the §9
    telemetry counter increments. NEVER returned as an empty staged log."""

    log_id: UUID
    status: Literal["capture_failure"] = "capture_failure"
    capture_error: str = "empty_transcript"


class DiscardResponse(BaseModel):
    log_id: UUID
    status: Literal["discarded"] = "discarded"


class RemoveItemResponse(BaseModel):
    log_id: UUID
    item_index: int
    status: Literal["removed"] = "removed"


# ---------------------------------------------------------------------------
# §7.2 when_hint interpreter (BACKLOG 26) — small, deterministic, no model
# ---------------------------------------------------------------------------

_WHEN_MEAL_WORDS = {
    "breakfast": Meal.breakfast,
    "morning": Meal.breakfast,
    "lunch": Meal.lunch,
    "dinner": Meal.dinner,
    "evening": Meal.dinner,
    "snack": Meal.snack,
}
# Canonical local clock time per meal — each inside its §7.2 default window,
# so a shifted logged_at re-infers to the same meal it names.
_WHEN_MEAL_HOUR = {
    Meal.breakfast: (8, 0),
    Meal.lunch: (12, 30),
    Meal.dinner: (18, 30),
    Meal.snack: (22, 0),
}
_YESTERDAY_RE = re.compile(
    r"^yesterday(?: at)? (breakfast|lunch|dinner|snack|morning|evening)$"
)


@dataclass
class WhenShift:
    logged_at: datetime | None = None  # tz-aware; replaces logs.captured_at
    meal: Meal | None = None  # yields to an explicit spoken meal_hint
    note: str | None = None  # set when the hint was unrecognized (ignored)


def interpret_when_hint(
    hint: str | None, captured_at: datetime, tz: str | None
) -> WhenShift:
    """§7.2 cases: "this morning" / "yesterday (at) <meal>" / "last night".
    Anything else is ignored with a note (BACKLOG 26) — never guessed at."""
    if hint is None or not hint.strip():
        return WhenShift()
    try:
        local = captured_at.astimezone(ZoneInfo(tz) if tz else timezone.utc)
    except Exception:
        local = captured_at.astimezone(timezone.utc)

    def at(day_delta: int, meal: Meal) -> WhenShift:
        hour, minute = _WHEN_MEAL_HOUR[meal]
        shifted = (local + timedelta(days=day_delta)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        # A when-hint refers to the past: "this morning" spoken at 06:00 must
        # not push logged_at forward to the canonical 08:00.
        if shifted > local:
            shifted = local
        return WhenShift(logged_at=shifted, meal=meal)

    key = norm(hint)
    if key == "this morning":
        return at(0, Meal.breakfast)
    if key == "last night":
        # the most recent night — late-night eating lands in the snack window
        return at(-1, Meal.snack)
    m = _YESTERDAY_RE.match(key)
    if m:
        return at(-1, _WHEN_MEAL_WORDS[m.group(1)])
    return WhenShift(note=f"when_hint {hint!r} not recognized — ignored")


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


# ---------------------------------------------------------------------------
# POST /api/logs — §5: one path, two content types. FastAPI cannot mix a JSON
# Body model with File/Form fields in one signature, so the voice path is a
# second route on the same path that only matches multipart/form-data; the
# JSON route stays a plain `body: LogCreate` (standard 422 shape, and LogCreate
# keeps its place in /openapi.json for the §2 type-gen). One operation is
# documented: the JSON route, with the multipart shape merged in via
# openapi_extra.
# ---------------------------------------------------------------------------


def _is_multipart(scope) -> bool:
    for key, value in scope.get("headers", ()):
        if key == b"content-type":
            return value.split(b";")[0].strip().lower() == b"multipart/form-data"
    return False


class _MultipartRoute(APIRoute):
    """An APIRoute that steps aside unless the request is multipart/form-data,
    so the next route on the same path (the JSON body) takes it."""

    def matches(self, scope) -> tuple[Match, dict]:
        match, child_scope = super().matches(scope)
        if match == Match.FULL and not _is_multipart(scope):
            return Match.NONE, {}
        return match, child_scope


async def create_log_voice(
    audio: Annotated[UploadFile, File()],
    log_id: Annotated[UUID, Form(alias="id")],
    captured_at: Annotated[datetime, Form()],
    tz: Annotated[str, Form()],
    capture_fallback: Annotated[bool, Form()] = False,
    capture_error: Annotated[str | None, Form()] = None,
) -> LogResponse | CaptureFailureResponse:
    """Audio → transcribe (§8.1) → the identical pipeline. input_mode='voice',
    transcribe_ms stored; empty/near-empty transcript → capture_failure (§5).
    The LogCreate fields ride as form fields minus `text` — the transcript
    supplies it."""
    audio_bytes = await audio.read()
    mime = audio.content_type or "application/octet-stream"

    # §13's silent-blob failure, caught server-side: a zero-byte blob is a
    # capture failure before any STT spend.
    if not audio_bytes:
        return await _capture_failure(
            log_id, captured_at, capture_fallback, "silent_blob", None, ""
        )

    from services import transcribe as stt  # lazy: stubbed in route tests

    try:
        result = await stt.transcribe(audio_bytes, mime)
    except stt.TranscriptionError:
        # both providers down (§8.1) — the client's degrade path is typing,
        # not a fake empty meal; 503 tells it to retry or fall back to text
        raise HTTPException(503, "transcription unavailable — retry or type it")

    if stt.is_near_empty(result.text):
        return await _capture_failure(
            log_id, captured_at, capture_fallback, "empty_transcript",
            result.transcribe_ms, result.text,
        )

    body = LogCreate(
        id=log_id,
        text=result.text,
        captured_at=captured_at,
        tz=tz,
        capture_fallback=capture_fallback,
        capture_error=capture_error,
    )
    return await create_log(
        body, input_mode="voice", transcribe_ms=result.transcribe_ms
    )


# Registered before the JSON route: the router tries routes in order, and this
# one declines everything that is not multipart.
router.add_api_route(
    "/api/logs",
    create_log_voice,
    methods=["POST"],
    route_class_override=_MultipartRoute,
    response_model=LogResponse | CaptureFailureResponse,
    include_in_schema=False,  # documented on the JSON operation below
)

_MULTIPART_OPENAPI = {
    "requestBody": {
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["audio", "id", "captured_at", "tz"],
                    "properties": {
                        "audio": {"type": "string", "format": "binary"},
                        "id": {"type": "string", "format": "uuid"},
                        "captured_at": {"type": "string", "format": "date-time"},
                        "tz": {"type": "string"},
                        "capture_fallback": {"type": "boolean", "default": False},
                        "capture_error": {"type": "string"},
                    },
                }
            }
        }
    }
}


@router.post(
    "/api/logs",
    response_model=LogResponse | CaptureFailureResponse,
    openapi_extra=_MULTIPART_OPENAPI,
)
async def create_log_endpoint(body: LogCreate) -> LogResponse:
    """§5: JSON body (text path) — or multipart with an audio blob + the same
    fields (voice path, create_log_voice above), which is the only way to get
    the capture_failure response."""
    return await create_log(body)


async def _capture_failure(
    log_id: UUID,
    captured_at: datetime,
    capture_fallback: bool,
    error: str,
    transcribe_ms: int | None,
    transcript: str,
) -> CaptureFailureResponse:
    """§5/§8.1d: record the failure (the §9 counter reads capture_error) and
    return capture_failure — never an empty staged log. status
    'capture_failure' keeps the row out of every staged-log lifecycle:
    commit/discard CAS on 'staged', and the orphan-claim above never re-runs
    a pipeline for it. Idempotent by client id like the happy path."""
    await db.execute(
        """
        INSERT INTO logs (id, input_mode, raw_input, captured_at, capture_fallback,
                          capture_error, transcribe_ms, status)
        VALUES (%s, 'voice', %s, %s, %s, %s, %s, 'capture_failure')
        ON CONFLICT (id) DO NOTHING
        """,
        (log_id, transcript, captured_at, capture_fallback, error, transcribe_ms),
    )
    return CaptureFailureResponse(log_id=log_id, capture_error=error)


async def create_log(
    body: LogCreate,
    *,
    input_mode: str = "text",
    transcribe_ms: int | None = None,
) -> LogResponse:
    # Idempotent upsert by client id (§5, §8.6): a concurrent double-drain or
    # retry cannot create two logs — and only the request that actually
    # inserted the row (or claimed an orphan, below) runs the pipeline.
    inserted = await db.execute(
        """
        INSERT INTO logs (id, input_mode, raw_input, captured_at, capture_fallback,
                          capture_error, transcribe_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (body.id, input_mode, body.text, body.captured_at, body.capture_fallback,
         body.capture_error, transcribe_ms),
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

    # §7.2 (BACKLOG 26): when_hint shifts logged_at/meal deterministically.
    # An explicit spoken meal_hint outranks the when-derived meal; the date
    # shift applies regardless. Unknown hints are ignored with a note.
    when = interpret_when_hint(parsed.when_hint, body.captured_at, body.tz)
    if parsed.meal_hint is not None:
        meal = parsed.meal_hint
    elif when.meal is not None:
        meal = when.meal

    resolutions = [
        await cascade.resolve_item(p, i, meal) for i, p in enumerate(parsed.items)
    ]
    await _persist(
        body.id, resolutions, meal,
        short_circuit=False, decompose_ms=decompose_ms, trace_id=trace_id,
        logged_at=when.logged_at, when_hint_note=when.note,
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
    logged_at: datetime | None = None,
    when_hint_note: str | None = None,
) -> None:
    """All staged state is persisted server-side (§3): the client is a renderer.

    logged_at, when set, replaces logs.captured_at — entries.logged_at
    snapshots logs.captured_at at commit (resolver/commit.py), so the §7.2
    when-hint shift lands here or nowhere. when_hint_note (an ignored hint)
    rides every item's staged jsonb, same extra-key mechanism as spoken_qty.
    """
    async with db.tx() as conn:
        await conn.execute(
            """
            UPDATE logs SET short_circuit = %s, meal_inferred = %s, decompose_ms = %s,
                            trace_id = %s, captured_at = coalesce(%s, captured_at)
            WHERE id = %s
            """,
            (short_circuit, meal.value, decompose_ms, trace_id, logged_at, log_id),
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
            if when_hint_note:
                # BACKLOG 26: an unrecognized when_hint is ignored, with the
                # note kept visible in staged for telemetry/debugging
                staged_payload["when_hint_note"] = when_hint_note
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
