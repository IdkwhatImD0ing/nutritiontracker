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

import auth
import db
from auth import require_user
from models import LogCreate, LogResponse, Meal, StagedItem
from resolver import cascade
from resolver import commit as commit_tx
from resolver.norm import norm
from routes.commit import SavedMealNote
from services import quota

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
# Voice and photo sit inside the window too: the row is claimed before STT /
# vision runs (the claim is what answers a replay and records the demo's
# attempt, so it cannot wait for the transcript), which puts one STT call
# (services/transcribe: 30s per provider) ahead of decompose.
_RERUN_GRACE_SECONDS = 90

# Multipart upload bounds (Phase-3 panel): a ~10s MediaRecorder clip is tens
# of KB; 15 MB is a few minutes of any container the client produces. Parts
# outside the allowlist (base type, parameters such as ;codecs=opus ignored)
# are refused before a byte is read or an STT dollar is spent.
MAX_AUDIO_BYTES = 15 * 1024 * 1024
AUDIO_MIME_ALLOWLIST = frozenset({"audio/mp4", "audio/webm", "audio/wav", "audio/mpeg"})


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


class LogCreateResponse(LogResponse):
    """POST /api/logs: LogResponse + the §8.4 itemless save path. "Save this
    as my usual breakfast" decomposes to zero items and a save_intent; the
    macro is built server-side from the most recent committed log and
    reported here (`saved_meal`), or `note` says why nothing was saved —
    never a silently empty staging list."""

    saved_meal: SavedMealNote | None = None
    note: str | None = None


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
    capture_error: str = "empty_transcript"  # the server-detected reason
    # the client's own prior-attempt failure (form field capture_error), kept
    # alongside the server reason instead of being overwritten by it
    client_capture_error: str | None = None


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
    # tz-aware diary time for entries.logged_at — NEVER written to
    # logs.captured_at, the §9 instrumentation anchor (Phase-4 fix)
    logged_at: datetime | None = None
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


# The claim of a client id — the lanes' idempotent insert (§8.6). Every lane's
# insert names its own columns; this is the text/voice one.
_INSERT_LOG_SQL = """
INSERT INTO logs (id, input_mode, raw_input, captured_at, capture_fallback,
                  capture_error, transcribe_ms)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""


async def claim_log(sql: str, params: tuple) -> bool:
    """The §8.6 claim of a client id and the demo's attempt, together.

    `sql` inserts the lane's log row ON CONFLICT (id) DO NOTHING. When this
    request inserted it, the demo's attempt is recorded in the same
    transaction (quota.admit_log) — a refusal rolls the row back, so a 429
    leaves nothing behind. False when the id already exists: this tenant's
    own row (a replay, or an orphan to re-run) or another tenant's, which RLS
    makes look the same here — _duplicate_post_response tells them apart,
    spending nothing. The owner records nothing, so its claim is the bare
    insert with no transaction around it.
    """
    if auth.is_owner():
        return bool(await db.execute(sql, params))
    async with db.tx() as conn:
        inserted = (await conn.execute(sql, params)).rowcount
        if inserted:
            await quota.admit_log(conn)
    return bool(inserted)


async def _drop_empty_log(log_id: UUID) -> None:
    """The lane failed before anything was staged (STT or vision down):
    remove the itemless log row. The client retries under the SAME client id
    (§5), and an itemless staged row younger than the §8.6 grace period
    would replay through _duplicate_post_response as items=[] — read as "no
    food" — instead of running the lane again. The demo quota is untouched:
    the attempt was recorded with the claim, so dropping the row does not
    hand the account another STT/vision call."""
    await db.execute(
        "DELETE FROM logs WHERE id = %s AND status = 'staged' "
        "AND NOT EXISTS (SELECT 1 FROM item_resolutions WHERE log_id = logs.id)",
        (log_id,),
    )


async def create_log_voice(
    audio: Annotated[UploadFile, File()],
    log_id: Annotated[UUID, Form(alias="id")],
    captured_at: Annotated[datetime, Form()],
    tz: Annotated[str, Form()],
    capture_fallback: Annotated[bool, Form()] = False,
    capture_error: Annotated[str | None, Form()] = None,
) -> LogCreateResponse | CaptureFailureResponse:
    """Audio → transcribe (§8.1) → the identical pipeline. input_mode='voice',
    transcribe_ms stored; empty/near-empty transcript → capture_failure (§5).
    The LogCreate fields ride as form fields minus `text` — the transcript
    supplies it."""
    mime = audio.content_type or "application/octet-stream"
    if mime.split(";")[0].strip().lower() not in AUDIO_MIME_ALLOWLIST:
        raise HTTPException(
            415,
            f"unsupported audio type {mime!r}; expected one of "
            + ", ".join(sorted(AUDIO_MIME_ALLOWLIST)),
        )
    # read one byte past the bound: a full read of an oversized part is the
    # only way to know, and the extra byte is what proves it
    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(413, f"audio exceeds {MAX_AUDIO_BYTES} bytes")

    # §8.6 before STT: the claim of the client id answers a stored log — a
    # replay of live items or of a recorded capture failure, the 409 of a
    # committed log, the 404 of an id that is not this tenant's — before a
    # byte reaches the transcriber, and records the demo's attempt with the
    # row it claims: after the free request checks above (a malformed part
    # costs nothing), before any STT spend, so a silent blob or an empty
    # transcript counts like any attempt. The row carries '' until the
    # transcript lands.
    if not await claim_log(
        _INSERT_LOG_SQL,
        (log_id, "voice", "", captured_at, capture_fallback, capture_error, None),
    ):
        failed = await _stored_capture_failure(log_id)
        if failed is not None:
            return failed  # the same outcome again (§5 idempotency), no STT
        replay = await _duplicate_post_response(log_id)
        if replay is not None:
            return replay
        await quota.check_log()  # this tenant's orphan, claimed for a re-run: the next attempt

    # §13's silent-blob failure, caught server-side: a zero-byte blob is a
    # capture failure before any STT spend.
    if not audio_bytes:
        return await _capture_failure(log_id, "silent_blob", None, "", client_error=capture_error)

    from services import transcribe as stt  # lazy: stubbed in route tests

    try:
        result = await stt.transcribe(audio_bytes, mime)
    except stt.TranscriptionError as e:
        # every provider declined (§8.1: down, or its key is not configured)
        # — the client's degrade path is typing, not a fake empty meal; 503
        # tells it to retry or fall back to text, and the detail says why.
        # The itemless row goes too: a retry under this id must run STT
        # again, not replay an empty log. The demo's attempt stands.
        await _drop_empty_log(log_id)
        raise HTTPException(503, f"transcription unavailable — retry or type it ({e})")

    if stt.is_near_empty(result.text):
        return await _capture_failure(
            log_id, "empty_transcript", result.transcribe_ms, result.text,
            client_error=capture_error,
        )

    await db.execute(
        "UPDATE logs SET raw_input = %s, transcribe_ms = %s WHERE id = %s",
        (result.text, result.transcribe_ms, log_id),
    )
    body = LogCreate(
        id=log_id,
        text=result.text,
        captured_at=captured_at,
        tz=tz,
        capture_fallback=capture_fallback,
        capture_error=capture_error,
    )
    return await _run_pipeline(body)


# Registered before the JSON route: the router tries routes in order, and this
# one declines everything that is not multipart.
router.add_api_route(
    "/api/logs",
    create_log_voice,
    methods=["POST"],
    route_class_override=_MultipartRoute,
    response_model=LogCreateResponse | CaptureFailureResponse,
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
    response_model=LogCreateResponse | CaptureFailureResponse,
    openapi_extra=_MULTIPART_OPENAPI,
)
async def create_log_endpoint(body: LogCreate) -> LogCreateResponse:
    """§5: JSON body (text path) — or multipart with an audio blob + the same
    fields (voice path, create_log_voice above), which is the only way to get
    the capture_failure response."""
    return await create_log(body)


async def _capture_failure(
    log_id: UUID,
    error: str,
    transcribe_ms: int | None,
    transcript: str,
    *,
    client_error: str | None = None,
) -> CaptureFailureResponse:
    """§5/§8.1d: record the failure (the §9 counter reads capture_error) and
    return capture_failure — never an empty staged log. The row was claimed
    by this request (claim_log) with the form fields; this stamps the outcome
    on it. status 'capture_failure' keeps the row out of every staged-log
    lifecycle: commit/discard CAS on 'staged', and the orphan-claim above
    never re-runs a pipeline for it. Idempotent by client id like the happy
    path: a same-id POST is answered from the stored row before STT
    (_stored_capture_failure).

    client_error is the client's own prior-attempt failure (the capture_error
    form field). §9 counts both classes, so neither may overwrite the other:
    logs has one text column, so both ride it as
    '<server reason>;client=<client reason>' (split_part(.., ';', 1) is the
    server reason; a row without a client reason stores the bare value)."""
    stored = f"{error};client={client_error}" if client_error else error
    await db.execute(
        """
        UPDATE logs SET raw_input = %s, capture_error = %s, transcribe_ms = %s,
                        status = 'capture_failure'
        WHERE id = %s AND status = 'staged'
        """,
        (transcript, stored, transcribe_ms, log_id),
    )
    return CaptureFailureResponse(
        log_id=log_id, capture_error=error, client_capture_error=client_error
    )


async def _stored_capture_failure(log_id: UUID) -> CaptureFailureResponse | None:
    """The voice lane's idempotent replay (§5, §8.6): a same-id POST after a
    recorded capture failure — the client keeps the id only while an upload's
    response was lost — answers the stored outcome again, 200 with the same
    body, before a byte reaches STT and without touching the quota. None
    unless this tenant's row is a capture_failure; another lane's POST on
    such an id stays the 409 of a log that left 'staged', as before."""
    row = await db.fetchone(
        "SELECT capture_error FROM logs WHERE id = %s AND status = 'capture_failure'",
        (log_id,),
    )
    if row is None:
        return None
    server, _, client = (row["capture_error"] or "empty_transcript").partition(";client=")
    return CaptureFailureResponse(
        log_id=log_id, capture_error=server, client_capture_error=client or None
    )


async def create_log(body: LogCreate) -> LogCreateResponse:
    # Idempotent claim by client id (§5, §8.6), the demo's attempt with it: a
    # concurrent double-drain or retry cannot create two logs — and only the
    # request that actually inserted the row (or claimed an orphan, below)
    # runs the pipeline. A refused attempt leaves no row.
    if not await claim_log(
        _INSERT_LOG_SQL,
        (body.id, "text", body.text, body.captured_at, body.capture_fallback,
         body.capture_error, None),
    ):
        replay = await _duplicate_post_response(body.id)
        if replay is not None:
            return replay
        # orphaned staged log claimed (earlier attempt died mid-pipeline) →
        # re-run: for a demo, the next attempt
        await quota.check_log()
    return await _run_pipeline(body)


async def _run_pipeline(body: LogCreate) -> LogCreateResponse:
    """§3.0 short-circuit, else decompose → the per-log cascade → _persist,
    on a log row this request owns (a fresh claim, or a claimed orphan)."""
    meal = await infer_meal(body.captured_at, body.tz)

    # §3.0 whole-utterance short-circuit — the LLM leaves the hot path entirely.
    # Meal macro first (tier 0, N rows — "my usual breakfast"), then alias
    # (tier 1, one row); None → decompose.
    sc = await cascade.short_circuit_all(body.text, meal)
    if sc is not None:
        await _persist(body.id, sc, meal, short_circuit=True)
        return LogCreateResponse(log_id=body.id, meal=meal, items=[r.staged for r in sc])

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

    # §8.4 "say 'save this as my usual breakfast' AFTER logging": zero items +
    # a save_intent means "this" is the most recent committed log.
    saved_meal = note = None
    if not parsed.items and parsed.save_intent is not None:
        saved_meal, note = await _save_intent_without_items(parsed.save_intent.name)

    # Per-log cascade (§3.1): a tier-0 expansion occupies one item_index per
    # macro item, so "usual breakfast and a coffee" → N macro rows, then coffee.
    resolutions = await cascade.resolve_items(parsed.items, meal)
    await _persist(
        body.id, resolutions, meal,
        short_circuit=False, decompose_ms=decompose_ms, trace_id=trace_id,
        logged_at=when.logged_at, when_hint_note=when.note,
        save_intent=parsed.save_intent.name if parsed.save_intent else None,
    )
    return LogCreateResponse(
        log_id=body.id, meal=meal, items=[r.staged for r in resolutions],
        saved_meal=saved_meal, note=note,
    )


async def _save_intent_without_items(name: str) -> tuple[SavedMealNote | None, str | None]:
    """§8.4 create path (a): the macro's items are the most recent committed
    log's entries (food, serving, qty as committed) — create-or-update by
    name, triggers defaulting to norm(name), same as the "Save as meal" tap.
    Nothing committed yet → (None, note); the log row stays an itemless
    staged log that nothing counts (§9 reads committed/discarded only)."""
    last = await db.fetchone(
        "SELECT l.id FROM logs l WHERE l.status = 'committed' "
        "AND EXISTS (SELECT 1 FROM entries e WHERE e.log_id = l.id) "
        "ORDER BY l.committed_at DESC LIMIT 1"
    )
    if last is None:
        return None, f"Nothing to save as '{name}' yet — log the items first."
    entries = await db.fetchall(
        "SELECT food_id, serving_id, qty FROM entries WHERE log_id = %s ORDER BY item_index",
        (last["id"],),
    )
    try:
        async with db.tx() as conn:
            row = await commit_tx.save_meal_macro(
                conn,
                name=name,
                trigger_phrases=None,
                items=[
                    {"food_id": str(e["food_id"]), "serving_id": e["serving_id"], "qty": float(e["qty"])}
                    for e in entries
                ],
            )
    except commit_tx.MealInvalid as e:
        return None, f"Couldn't save '{name}': {e}"
    return (
        SavedMealNote(meal_id=row["id"], name=row["name"], item_count=len(row["items"] or [])),
        None,
    )


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
        # the id exists (the insert conflicted) but no row is visible: another
        # tenant's log, which reads as no log at all (§8.9) — or, rarely, a
        # row that vanished between the insert and this read
        raise HTTPException(404, "log not found")
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
    save_intent: str | None = None,
) -> None:
    """All staged state is persisted server-side (§3): the client is a renderer.

    logs.captured_at is never touched here — it is the §9 latency/bucketing
    anchor. logged_at (the §7.2 when-hint shift) rides every item's staged
    jsonb as `logged_at` (ISO); resolver/commit.py reads it back for
    entries.logged_at and every per-item rewrite preserves it
    (commit.KEEP_LOG_KEYS_SQL). when_hint_note (an ignored hint) rides the
    same extra-key mechanism as spoken_qty. save_intent (decompose's "save
    this as my usual breakfast", §3.5 step 4) rides the same channel —
    commit reads it back from the stored rows.
    """
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
            if logged_at is not None:
                # §7.2: the diary time for this log's entries (never captured_at)
                staged_payload["logged_at"] = logged_at.isoformat()
            if when_hint_note:
                # BACKLOG 26: an unrecognized when_hint is ignored, with the
                # note kept visible in staged for telemetry/debugging
                staged_payload["when_hint_note"] = when_hint_note
            if save_intent:
                # §3.5 step 4: resolver/commit.py creates/updates the macro
                # from the committed items when a stored row carries this
                staged_payload["save_intent"] = {"name": save_intent}
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
