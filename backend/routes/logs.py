"""POST/GET/PATCH/DELETE /api/logs (DESIGN.md §5, §3.0, §3.4, §7.2, §8.1, §9).

M2: POST /api/logs accepts multipart (audio blob + the same fields) alongside
the JSON body; audio runs through the Transcriber interface
(services/transcribe.py) and an empty/near-empty transcript returns the §5
capture_failure status — never an empty staged log.

Every utterance goes through services/agent.py after the §3.0 short-circuit
(§6.4; the only pipeline since decompose was removed, 2026-09-11) — the agent
names the foods itself and hands them to the cascade → staging pipeline
(_stage_items), an unlabeled dish's components to _stage_estimate (flagged
rows, §3.4a) and a spoken weight to routes/weights; every row is looked up
before it answers (_resolve_inline, §3.4); the response reports what it did
under `agent`. routes/agent.py streams the same pipeline event by event for
the chat window; the claim, transcription and pipeline helpers here are
shared with it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated, Awaitable, Callable, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.routing import APIRoute
from psycopg.types.json import Json
from pydantic import BaseModel, Field
from starlette.routing import Match

import auth
import db
import deadline
from auth import require_user
from models import LogCreate, LogResponse, Macros, Meal, ParsedItem, ServingRef, StagedItem
from resolver import cascade
from resolver import commit as commit_tx
from resolver import foods_repo
from resolver.norm import norm
from routes import weights as weights_route
from routes.commit import SavedMealNote
from services import quota

if TYPE_CHECKING:  # the service is imported lazily at call time (route tests stub it)
    from services.agent import EstimateItem, FoodItem, MealFood

# routes/agent.py's stream callback: one wire event (a dict) per call
Emit = Callable[[dict], Awaitable[None]]

log = logging.getLogger(__name__)


def cascade_summary(resolutions: list[cascade.Resolution]) -> list[dict]:
    """What the cascade's retriever span records per row (§10): the phrase,
    the outcome and the tier — never the macros."""
    return [
        {
            "phrase": r.staged.phrase,
            "status": r.staged.status,
            "tier": r.staged.tier,
            "food": r.staged.food.name if r.staged.food else None,
        }
        for r in resolutions
    ]

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
# (services/transcribe: 30s per provider) ahead of decompose. The agent lane
# (§6.4) is a different animal — up to ten gpt-5.6-terra turns with web
# searches ahead of decompose — so its logs get the run ceiling below: a retry
# that finds no live run to attach to (another instance, BACKLOG 156) must
# never claim a run that is merely still thinking and start a second one.
# Since 2026-09-11 every text and voice log IS an agent run (no decompose
# pipeline is left), so they all take the agent grace; this 90 s window is
# the plain photo route's (one vision call).
_RERUN_GRACE_SECONDS = 90
# the agent lane's grace, and routes/agent.py's presumed-dead deadline: the
# two agree so an orphan is claimed only once no live run can still own it
_AGENT_RERUN_GRACE_SECONDS = 300

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


class AgentOutcome(BaseModel):
    """What the agent lane did with the utterance (§6.4): its one-sentence
    reply, the weigh-in it recorded, if any, the answers it offers when the
    reply is a clarifying question (`options`, tappable in the chat), and the
    earlier staged logs it discarded because this turn replaces them
    (`discarded`). Staged items ride the LogResponse as on any other log.
    Assembled from what the tools did, never from what the model said."""

    reply: str
    weight: weights_route.WeightEntry | None = None
    options: list[str] | None = None
    discarded: list[UUID] | None = None
    # §3.4b/§6.4 (2026-09-08): `adjust_photo` edited an EARLIER turn's log — the
    # photographed plate. This turn's own log stages nothing, so the plate as it
    # now stands comes back here and the chat patches the card that owns it. Null
    # on every turn that did not touch a plate.
    updated: LogPollResponse | None = None


class LogCreateResponse(LogResponse):
    """POST /api/logs: LogResponse + what the agent's meal tools did (§8.4):
    `saved_meal` when a save landed, `note` when it lands at commit or could
    not — never a silently empty staging list. `agent` carries the agent's
    reply (§6.4) — with zero items it is what the client shows instead of a
    sheet."""

    saved_meal: SavedMealNote | None = None
    note: str | None = None
    agent: AgentOutcome | None = None


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

# Canonical local clock time per meal — each inside its §7.2 default window,
# so a shifted logged_at re-infers to the same meal it names.
_WHEN_MEAL_HOUR = {
    Meal.breakfast: (8, 0),
    Meal.lunch: (12, 30),
    Meal.dinner: (18, 30),
    Meal.snack: (22, 0),
}


@dataclass
class WhenShift:
    # tz-aware diary time for entries.logged_at — NEVER written to
    # logs.captured_at, the §9 instrumentation anchor (Phase-4 fix)
    logged_at: datetime | None = None
    meal: Meal | None = None  # yields to an explicit spoken meal_hint
    note: str | None = None  # set when the hint was unrecognized (ignored)


def local_time(captured_at: datetime, tz: str | None) -> datetime:
    """The utterance's local wall clock. `tz` is an unvalidated client string,
    so an unknown zone degrades to UTC rather than raising."""
    try:
        return captured_at.astimezone(ZoneInfo(tz) if tz else timezone.utc)
    except Exception:
        return captured_at.astimezone(timezone.utc)


def when_shift(
    day_offset: int | None,
    when_meal: Meal | None,
    meal_hint: Meal | None,
    captured_at: datetime,
    tz: str | None,
    *,
    when_hint: str | None = None,
) -> WhenShift:
    """§7.2 — arithmetic on what the MODEL already resolved (BACKLOG 189).

    The model reads the NOW line and answers `when_day_offset` (whole days
    back) plus, when the time words imply one, `when_meal`. Nothing here parses
    a phrase: that is exactly what used to fail, because a regex can only match
    the forms someone thought to enumerate — a bare "yesterday" alongside a
    meal word further along the sentence ("Yesterday, I also had two yogurts
    for breakfast") matched none of them and the day was silently dropped.

    The time of day comes from the meal that will actually be used (an
    explicitly named `meal_hint` outranks `when_meal`, as it always has), so a
    yesterday-breakfast row lands at breakfast o'clock rather than at whatever
    time the person happened to be talking. `meal` is returned as `when_meal`
    alone — the caller's precedence is unchanged.
    """
    if day_offset is None and when_meal is None:
        # A phrase the model transcribed but did not resolve is a model
        # failure, not an unknown format — note it for §10 and change nothing.
        if when_hint and when_hint.strip():
            return WhenShift(note=f"when_hint {when_hint!r} left unresolved — ignored")
        return WhenShift()
    local = local_time(captured_at, tz)
    # food is always already eaten; a positive offset would be a model error
    shifted = local + timedelta(days=min(int(day_offset or 0), 0))
    meal = meal_hint or when_meal
    if meal is not None:
        hour, minute = _WHEN_MEAL_HOUR[meal]
        shifted = shifted.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # "this morning" spoken at 06:00 must not push logged_at forward to 08:00
    if shifted > local:
        shifted = local
    return WhenShift(logged_at=shifted, meal=when_meal)


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


async def claim_voice_log(
    audio: UploadFile,
    log_id: UUID,
    captured_at: datetime,
    capture_fallback: bool,
    capture_error: str | None,
) -> tuple[bytes, str] | LogResponse | CaptureFailureResponse:
    """The voice lane's request checks and §8.6 claim, shared by the plain
    route and the streaming one (routes/agent.py): the audio part validated
    and read, then the claim of the client id — a replay of a stored log or
    capture failure comes back as the response to send; (audio_bytes, mime)
    means this request runs STT and the pipeline — the agent lane, with its
    longer orphan grace (§6.4)."""
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
        replay = await _duplicate_post_response(log_id, grace=_AGENT_RERUN_GRACE_SECONDS)
        if replay is not None:
            return replay
        await quota.check_log()  # this tenant's orphan, claimed for a re-run: the next attempt
    return audio_bytes, mime


async def transcribe_for_log(
    log_id: UUID, audio_bytes: bytes, mime: str, capture_error: str | None
) -> str | CaptureFailureResponse:
    """STT for a claimed voice log: the transcript, stored on the row with
    its timing — or the §5 capture_failure (a silent blob, an empty
    transcript), recorded the same way. Every provider declining is the 503
    whose detail says why; the itemless row goes with it so the same-id retry
    runs STT again instead of replaying an empty log."""
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
    return result.text


async def release_claim(log_id: UUID) -> None:
    """A failed agent run hands its itemless log back for a re-run (§8.6):
    the claim stamp — `created_at`, which nothing else reads for semantics —
    is aged past the agent lane's grace, so the next same-id POST claims the
    orphan without waiting it out. For the owner that is a re-run at once;
    a demo's attempt stands (the ledger row is not refunded, §8.9), so its
    retry meets the day's 429 like any spent attempt. Unlike _drop_empty_log
    this keeps the row: a weigh-in the run recorded before failing points at
    it (FK), and stands. A log with staged rows keeps its claim — the retry
    replays what landed (BACKLOG 155)."""
    await db.execute(
        "UPDATE logs SET created_at = now() - make_interval(secs => %s) "
        "WHERE id = %s AND status = 'staged' "
        "AND NOT EXISTS (SELECT 1 FROM item_resolutions WHERE log_id = logs.id)",
        (_AGENT_RERUN_GRACE_SECONDS + 1, log_id),
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
    import tracing  # lazy: stubbed in route tests

    # §10: one trace per run, the log id as its session (STT is its first span)
    async with tracing.run(
        "log", session_id=str(log_id), tags=["lane:voice", "lane:agent"],
    ) as trace:
        claimed = await claim_voice_log(
            audio, log_id, captured_at, capture_fallback, capture_error
        )
        if not isinstance(claimed, tuple):
            return claimed
        audio_bytes, mime = claimed
        trace.set(input={"mime": mime, "bytes": len(audio_bytes)})
        text = await transcribe_for_log(log_id, audio_bytes, mime, capture_error)
        if not isinstance(text, str):
            trace.set(output=text)
            return text
        trace.set(input=text)
        body = LogCreate(
            id=log_id,
            text=text,
            captured_at=captured_at,
            tz=tz,
            capture_fallback=capture_fallback,
            capture_error=capture_error,
        )
        response = await _run_pipeline(body)
        trace.set(output=response)
        return response


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
    import tracing  # lazy: stubbed in route tests

    # §10: one trace per run, the log id as its session; nothing is sent
    # unless a model ran (a §3.0 short-circuit or a §8.6 replay records nothing)
    async with tracing.run(
        "log", input=body.text, session_id=str(body.id), tags=["lane:agent"],
    ) as trace:
        response = await create_log(body)
        trace.set(output=response)
        return response


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


async def claim_text_log(body: LogCreate, mode: str = "text") -> LogResponse | None:
    """The text lane's §8.6 claim, shared with routes/agent.py: None when
    this request owns the row and runs the pipeline; else the replay to send.

    `mode` is what goes in `logs.input_mode`. The chat's image turn passes
    `"photo"`: what it staged was identified from pixels, so §3.5's learning
    gate (`_NO_LEARN_MODES`) has to see it as the photo row it is, or a
    portion the model read off an image becomes the next spoken log's
    default."""
    # Idempotent claim by client id (§5, §8.6), the demo's attempt with it: a
    # concurrent double-drain or retry cannot create two logs — and only the
    # request that actually inserted the row (or claimed an orphan, below)
    # runs the pipeline. A refused attempt leaves no row.
    if not await claim_log(
        _INSERT_LOG_SQL,
        (body.id, mode, body.text, body.captured_at, body.capture_fallback,
         body.capture_error, None),
    ):
        # every text log is an agent run, which may take minutes (§6.4)
        replay = await _duplicate_post_response(body.id, grace=_AGENT_RERUN_GRACE_SECONDS)
        if replay is not None:
            return replay
        # orphaned staged log claimed (earlier attempt died mid-pipeline) →
        # re-run: for a demo, the next attempt
        await quota.check_log()
    return None


async def create_log(body: LogCreate) -> LogCreateResponse:
    replay = await claim_text_log(body)
    if replay is not None:
        return replay
    return await _run_pipeline(body)


async def _run_pipeline(
    body: LogCreate, emit: Emit | None = None, image: str | None = None
) -> LogCreateResponse:
    """§3.0 short-circuit, else the front-door agent (§6.4) — the one
    pipeline since decompose was removed (2026-09-11) — on a log row this
    request owns (a fresh claim, or a claimed orphan). `emit` streams the
    agent's events to the chat (routes/agent.py); None runs silently."""
    deadline.start_if_unset()  # the chat route started it at the request's start
    meal = await infer_meal(body.captured_at, body.tz)

    # §3.0 whole-utterance short-circuit — the LLM leaves the hot path entirely.
    # Meal macro first (tier 0, N rows — "my usual breakfast"), then alias
    # (tier 1, one row); None → the agent. A memorized phrase stages in one
    # round trip whichever sheet it came from.
    # An attached image is never a memorized phrase (§6.4, 2026-09-09): the
    # words were said ABOUT a picture, so "my usual breakfast" beside a photo
    # is a question about that photo, not a saved meal. Short-circuiting would
    # stage from memory and drop the image with nothing said about it.
    sc = None if image else await cascade.short_circuit_all(body.text, meal)
    if sc is not None:
        await _persist(body.id, sc, meal, short_circuit=True)
        return LogCreateResponse(log_id=body.id, meal=meal, items=[r.staged for r in sc])

    return await _run_agent_lane(body, meal, emit, image)


# §3.4 (2026-09-11): every row is looked up before the chat answers (Bill:
# "the agent should make the list using its tools then I just approve"). One
# lookup takes at most this long, and less when the request's clock
# (deadline.py) cannot spare it; under _INLINE_MIN_S left it is not started —
# the row stays searching and the phone's kick, the old path, finishes it.
_INLINE_CAP_S = 40.0
_INLINE_MIN_S = 10.0
_INLINE_PARALLEL = 4  # tier-3 runs at once: model time overlaps, db statements queue
_AGENT_RESERVE_S = 20.0  # the agent's closing turn still has to fit after the lookups
_PHOTO_RESERVE_S = 5.0  # the photo turn's reply is built in code


def _inline_enabled() -> bool:
    """Off in the test suite (tests/conftest.py): a fake Runner drives the real
    tool closures there, and an inline lookup would reach the real tier 3."""
    return os.environ.get("TALLY_INLINE_TIER3", "1") != "0"


async def _resolve_inline(
    log_id: UUID, rows: list[StagedItem], *, reserve: float
) -> list[StagedItem]:
    """Run tier 3 now for every `searching` row, before anyone replies, and
    return the rows with the results in place (by item_index). A lookup that
    the clock cannot fit, that the demo quota refuses, or that fails leaves
    its row searching — exactly what the phone's kick (StagingSheet) picks up."""
    todo = [r for r in rows if r.status == "searching"]
    if not todo or not _inline_enabled():
        return rows
    from routes import resolve as resolve_route  # lazy: resolve → photo → here

    gate = asyncio.Semaphore(_INLINE_PARALLEL)

    async def one(item: StagedItem) -> StagedItem:
        async with gate:
            left = deadline.left()
            budget = _INLINE_CAP_S if left is None else min(_INLINE_CAP_S, left - reserve)
            if budget < _INLINE_MIN_S:
                return item
            deadline.tier3_budget.set(budget)  # this task's copy of the context only
            try:
                return await resolve_route.kick_row(log_id, item.item_index, claim=False)
            except Exception:
                log.warning("inline tier 3 left row %s/%s searching", log_id, item.item_index,
                            exc_info=True)
                return item

    done = {d.item_index: d for d in await asyncio.gather(*(one(r) for r in todo))}
    return [done.get(r.item_index, r) for r in rows]


def _replace_rows(items: list[StagedItem], fresh: list[StagedItem]) -> None:
    """Swap rows in place by item_index — the response is built from `items`,
    and a stale searching copy there would make the phone kick a second,
    paid run over a row this request already resolved."""
    by_index = {r.item_index: r for r in fresh}
    items[:] = [by_index.get(it.item_index, it) for it in items]


async def _stage_items(
    body: LogCreate,
    meal: Meal,
    items: list[FoodItem],
    *,
    start_index: int,
    meal_hint: str | None = None,
    when_hint: str | None = None,
    when_day_offset: int | None = None,
    when_meal: str | None = None,
) -> tuple[list[StagedItem], Meal, datetime | None]:
    """§6.4 log_food (2026-09-11): the foods the chat agent named, through the
    cascade — tier 0 for a saved meal it named, tier 1 for a phrase he has
    said before, tier 2 for one of his foods it named by id, else searching
    for tier 3 — then persisted. This replaced decompose in the agent lane:
    the agent already read the utterance with the whole conversation in view,
    and a second, weaker model re-reading it cost 1.5–4 s and lost what the
    conversation held (trace 30e72425: "ramen"). The meal and day come from
    the agent's meal_hint / when_* exactly as log_estimate's do.

    Returns (rows, the rows' meal, logged_at)."""
    import tracing

    try:
        shift_meal = Meal(when_meal) if when_meal else None
    except ValueError:
        shift_meal = None
    try:
        spoken_meal = Meal(meal_hint) if meal_hint else None
    except ValueError:
        spoken_meal = None
    when = when_shift(
        when_day_offset, shift_meal, spoken_meal,
        body.captured_at, body.tz, when_hint=when_hint,
    )
    row_meal = spoken_meal or when.meal or meal  # an explicit meal word outranks the shift
    # A food_id proves nothing on its own (`foods` is global): only the ids
    # that are this tenant's own diary foods may name a row (_MY_FOODS_SQL).
    # An id that fails is dropped, and its phrase takes tiers 1 and 3.
    wanted = sorted({fid for it in items if (fid := _uuid_or_none(it.food_id)) is not None})
    mine = {r["id"] for r in await db.fetchall(_MY_FOODS_SQL, (wanted,))} if wanted else set()
    parsed = [
        ParsedItem(
            phrase=it.phrase.strip(),
            phrase_kind="food",
            # a saved meal stores its own portions, and a spoken amount would
            # stand tier 0 aside (§3.1); one that matches no saved meal is a
            # plain food, never the "no saved meal called…" question row
            qty=None if it.saved_meal else (float(it.qty) if it.qty is not None else None),
            unit=None if it.saved_meal else ((it.unit or "").strip() or None),
            state=None,
            brand=None,
            library_match=_uuid_or_none(it.food_id),
            meal_ref=it.phrase.strip() if it.saved_meal else None,
        )
        for it in items
    ]
    async with tracing.run("cascade", kind="retriever", input=[p.phrase for p in parsed]) as lookup:
        resolutions = await cascade.resolve_items(parsed, row_meal, mine, start_index=start_index)
        lookup.set(output=cascade_summary(resolutions))
    # the agent's note for a food it could not name by id rides the row that
    # still needs looking up (a saved meal's expanded rows carry food names,
    # never the phrase, and are already resolved)
    notes = {it.phrase.strip(): it.note.strip() for it in items if (it.note or "").strip()}
    lookup_notes = {
        r.staged.item_index: notes[r.staged.phrase]
        for r in resolutions
        if r.staged.status == "searching" and r.staged.phrase in notes
    }
    await _persist(
        body.id, resolutions, row_meal, short_circuit=False,
        trace_id=tracing.current_trace_id(),
        logged_at=when.logged_at, when_hint_note=when.note, lookup_notes=lookup_notes,
    )
    return [r.staged for r in resolutions], row_meal, when.logged_at


def _estimate_parsed(it: EstimateItem) -> ParsedItem:
    """The cascade's input for one agent-sized component: the amount rides the
    §7.3 spoken-quantity channel, as the photo lane's does; nothing else is
    invented (no state, brand, library or meal match)."""
    return ParsedItem(
        phrase=it.phrase.strip(),
        phrase_kind="food",
        qty=float(it.qty),
        unit=(it.unit or "").strip() or None,
        state=None,
        brand=None,
        library_match=None,
        meal_ref=None,
    )


def _flag_estimate(item: StagedItem, it: EstimateItem) -> StagedItem:
    """§3.4a on an agent-sized row: the flag and the user-facing note, and a
    confidence never above medium — the amount is a guess whatever the food
    match. A searching row keeps the amount in spoken_qty/unit for tier 3
    (_persist), and KEEP_LOG_KEYS_SQL carries the flag through its rewrite."""
    update: dict = {
        "estimated": "portion",
        "estimate_note": it.note.strip() or "amount estimated by the agent",
    }
    if item.status == "resolved" and item.confidence == "high":
        update["confidence"] = "medium"
    return item.model_copy(update=update)


def _size_by_weight(item: StagedItem, grams: float) -> StagedItem:
    """A counted component whose food has no count-type serving — the cached
    shrimp record carries only "100 g" — is sized by the estimated weight
    instead of pieces × the mass default (133 shrimp would have been 133 ×
    100 g): a gram-based serving (the 100 g row first, as the photo lane's
    est_grams path picks it), macros recomputed from the food's own numbers."""
    by_grams = [s for s in (item.servings or []) if s.grams and s.grams > 0]
    if not by_grams:
        return item
    base = next((s for s in by_grams if s.grams == 100), None) or max(
        by_grams, key=lambda s: not cascade._is_count_serving(s.description)
    )
    qty = round(grams / float(base.grams), 4)
    raw = base.macros or {}
    macros = (
        Macros(**cascade._scale_macros(raw, qty))
        if all(k in raw for k in cascade._CORE_MACROS)
        else None
    )
    return item.model_copy(
        update={
            "serving": ServingRef(id=base.serving_id, description=base.description),
            "qty": qty,
            "macros": macros,
            "confidence": "medium" if macros is not None else "low",
        }
    )


async def _stage_estimate(
    body: LogCreate,
    meal: Meal,
    items: list[EstimateItem],
    *,
    start_index: int,
    meal_hint: str | None = None,
    when_hint: str | None = None,
    when_day_offset: int | None = None,
    when_meal: str | None = None,
) -> list[StagedItem]:
    """§6.4 log_estimate: the components of an unlabeled dish through the
    identical tiers 1–3 (tier 0 skipped — a saved-meal trigger is something
    you say), each row flagged estimated="portion": it renders as a guess
    (§3.4a) and it learns nothing (the flag —
    LEARNABLE_ROWS_SQL on the read side, commit's alias gate on the write
    side). The numbers are the database's; only the amount is the agent's.
    The spoken meal and time words ride in as the agent's `meal_hint` /
    `when_hint`, and the agent — a model, like decompose — resolves the time
    words itself into `when_day_offset` / `when_meal`, so "yesterday I had a
    platter…" lands on yesterday's diary, not today's (BACKLOG 189)."""
    try:
        shift_meal = Meal(when_meal) if when_meal else None
    except ValueError:
        shift_meal = None
    try:
        spoken_meal = Meal(meal_hint) if meal_hint else None
    except ValueError:
        spoken_meal = None
    when = when_shift(
        when_day_offset, shift_meal, spoken_meal,
        body.captured_at, body.tz, when_hint=when_hint,
    )
    row_meal = spoken_meal or when.meal or meal  # an explicit meal word outranks the shift
    import tracing

    async with tracing.run(
        "cascade", kind="retriever", input=[it.phrase for it in items]
    ) as lookup:
        resolutions = [
            await cascade.resolve_item(_estimate_parsed(it), start_index + i, row_meal, skip_tier0=True)
            for i, it in enumerate(items)
        ]
        lookup.set(output=cascade_summary(resolutions))
    for r, it in zip(resolutions, items):
        counted = it.unit is None and it.grams is not None and it.grams > 0
        if counted and r.staged.status == "searching":
            # tier 3 sizes by weight: a count needs a count-type serving the
            # food it picks may not have (the count stays in the note)
            r.staged = r.staged.model_copy(update={"qty": float(it.grams)})
            r.unit = "g"
        elif (
            counted
            and r.staged.status == "resolved"
            and r.staged.serving is not None
            and not cascade._is_count_serving(r.staged.serving.description)
        ):
            r.staged = _size_by_weight(r.staged, float(it.grams))
        r.staged = _flag_estimate(r.staged, it)
    await _persist(
        body.id, resolutions, row_meal, short_circuit=False,
        logged_at=when.logged_at, when_hint_note=when.note,
    )
    return [r.staged for r in resolutions]


# ---------------------------------------------------------------------------
# §8.4 through the agent (§6.4): save_meal / update_meal — the writes the
# Library and the diary's "Save as meal" make (routes/meals.py), reached from
# the chat. What the tools hand the model is words, never macros.
# ---------------------------------------------------------------------------

_LAST_COMMITTED_LOG_SQL = (
    "SELECT l.id FROM logs l WHERE l.status = 'committed' "
    "AND EXISTS (SELECT 1 FROM entries e WHERE e.log_id = l.id) "
    "ORDER BY l.committed_at DESC LIMIT 1"
)


def _meal_item_words(it) -> str:
    return f"{it.food_name or it.food_id} {it.qty:g} × {it.serving_description or it.serving_id}"


def _meal_words(meal) -> str:
    """A saved meal as the agent reads it (routes/meals.MealOut): the name,
    the items as "food qty × serving", the trigger phrases, inactive marked."""
    items = "; ".join(_meal_item_words(it) for it in meal.items) or "no items"
    state = "" if meal.is_active else " (inactive)"
    return f"{meal.name}{state}: {items} — say: {', '.join(meal.trigger_phrases)}"


def _meal_out(meal) -> dict:
    """What a meal tool answers after a write: the meal as stored, in words."""
    return {
        "meal": meal.name,
        "is_active": meal.is_active,
        "item_count": len(meal.items),
        "items": [_meal_item_words(it) for it in meal.items],
        "trigger_phrases": list(meal.trigger_phrases),
    }


def _meal_error(e: HTTPException, *, conflict: str = "duplicate_name") -> dict:
    """routes/meals.py's refusals, as the tool reports them to the model.
    `conflict` is what a 409 means at THIS call site: create/patch raise it
    only for a name already in use, meal_from_log only for a log with nothing
    committed — the model's recovery differs, so the caller names it."""
    kind = {404: "not_found", 409: conflict, 422: "invalid"}.get(e.status_code, "failed")
    return {"error": kind, "message": str(e.detail)}


async def _find_meal(name: str, *, prefer_inactive: bool = False) -> dict | None:
    """The macro of that name, most recently changed first: the active one
    normally, the INACTIVE one when the turn is a reactivation — otherwise an
    inactive meal whose name an active one also holds could never be reached
    (and patch_meal answers the honest 409 when both exist)."""
    return await db.fetchone(
        "SELECT * FROM meal_macros WHERE lower(name) = lower(%s) "
        f"ORDER BY is_active {'ASC' if prefer_inactive else 'DESC'}, updated_at DESC LIMIT 1",
        (name.strip(),),
    )


def _match_meal_item(meal, food: str) -> int | dict:
    """The one item `food` names — its food name, exact first, else a
    substring, case-insensitive — or an error listing the items."""
    key = food.strip().lower()
    names = [(it.food_name or "").lower() for it in meal.items]
    hits = [i for i, n in enumerate(names) if n == key] or [
        i for i, n in enumerate(names) if key and key in n
    ]
    if len(hits) == 1:
        return hits[0]
    return {
        "error": "ambiguous_item" if hits else "unknown_item",
        "message": f"'{food}' names {len(hits)} of the meal's items — use one of these",
        "items": [it.food_name or it.food_id for it in meal.items],
    }


# The foods this account has actually eaten. `foods` is global — no user_id,
# no RLS (migration 002) — so a food_id the model supplies proves nothing on
# its own, and commit.validate_meal_items only checks the shared cache. This
# is the food analogue of known_logs: only a food reachable through THIS
# tenant's `entries` may join a saved meal by id. One statement per call.
_MY_FOODS_SQL = (
    "SELECT DISTINCT f.id::text AS id, f.display_name, f.servings,"
    " f.default_serving_id"
    " FROM entries e JOIN foods f ON f.id = e.food_id"
    " WHERE e.food_id = ANY(%s::uuid[])"
)


def _uuid_or_none(value: str | None) -> str | None:
    """The canonical form, or None — a non-uuid from the model must become an
    unresolved row, never a psycopg error _tool_error would swallow."""
    try:
        return str(UUID((value or "").strip()))
    except (ValueError, AttributeError, TypeError):
        return None


async def _resolve_meal_ids(items: list[MealFood]) -> dict[int, dict | None]:
    """The id path (§6.4), by item index: the (food, serving, qty) row for
    every item pointing at a past_foods row, or None when this account has
    never logged that food, the id is malformed, or the serving is not on the
    food. THIS is the tenancy gate _MY_FOODS_SQL describes — a food_id alone
    proves nothing — so it stays even though validate_meal_items looks
    sufficient (BACKLOG 165). Each id is canonicalised once, here.

    A `food_id` with no `serving_id` falls back to the food's own default
    serving: the person pointed at a food, and a name search for it could
    land on a different row entirely."""
    wanted = {i: _uuid_or_none(it.food_id) for i, it in enumerate(items) if it.food_id}
    ids = sorted({fid for fid in wanted.values() if fid is not None})
    mine = {r["id"]: r for r in await db.fetchall(_MY_FOODS_SQL, (ids,))} if ids else {}
    out: dict[int, dict | None] = {}
    for i, fid in wanted.items():
        it = items[i]
        food = mine.get(fid or "")
        serving = (
            commit_tx._find_serving(
                food["servings"],
                (it.serving_id or "").strip() or food["default_serving_id"],
            )
            if food is not None
            else None
        )
        out[i] = (
            None
            if serving is None
            else {
                "food_id": food["id"], "serving_id": serving["serving_id"],
                "qty": float(it.qty) if it.qty is not None else 1.0,
                "food": food["display_name"], "serving": serving["description"],
            }
        )
    return out


def _merge_meal_rows(rows: list[dict]) -> list[dict]:
    """One row per (food, serving): the model echoing the same past_foods row
    twice must not make a meal that logs it twice. The extend path merges the
    same way; the create path would not."""
    merged: list[dict] = []
    for r in rows:
        same = next(
            (m for m in merged
             if m["food_id"] == r["food_id"] and m["serving_id"] == r["serving_id"]),
            None,
        )
        if same is None:
            merged.append(r)
        else:
            same["qty"] += r["qty"]
    return merged


async def _resolve_meal_items(
    items: list[MealFood], meal: Meal
) -> tuple[list[dict], list[dict]]:
    """Foods → (food, serving, qty) rows for a saved meal. An item pointing at
    a past_foods row by id is taken from _resolve_meal_ids — no lookup, so the
    amount is exactly the diary's. An item in words goes through the identical
    tiers 1–2 log_estimate's components take (tier 0 skipped; the amount on
    the §7.3 spoken-quantity channel, none = the usual portion). Nothing is
    persisted: tier 3 is a client-kicked handoff from a staged row, so a food
    the databases do not know yet is reported back — logging it once is what
    makes it known."""
    import tracing

    resolved: list[dict] = []
    unresolved: list[dict] = []
    by_id = await _resolve_meal_ids(items)
    async with tracing.run(
        "cascade", kind="retriever", input=[it.phrase for it in items]
    ) as lookup:
        for i, it in enumerate(items):
            if i in by_id:
                if by_id[i] is None:
                    unresolved.append({
                        "phrase": it.phrase, "status": "unknown_food",
                        "message": "not one of the foods this account has logged — "
                                   "look it up with past_foods, or name it in words",
                    })
                else:
                    resolved.append(by_id[i])
                continue
            parsed = ParsedItem(
                phrase=it.phrase.strip(), phrase_kind="food",
                qty=float(it.qty) if it.qty is not None else None,
                unit=(it.unit or "").strip() or None,
                state=None, brand=None, library_match=None, meal_ref=None,
            )
            st = (await cascade.resolve_item(parsed, i, meal, skip_tier0=True)).staged
            if st.status == "resolved" and st.food is not None and st.serving is not None:
                resolved.append({
                    "food_id": st.food.id, "serving_id": st.serving.id,
                    "qty": float(st.qty if st.qty is not None else 1),
                    "food": st.food.name, "serving": st.serving.description,
                })
            else:
                unresolved.append({
                    "phrase": it.phrase, "status": st.status,
                    "message": "not in the databases yet — log it once, then add it",
                })
        resolved = _merge_meal_rows(resolved)
        lookup.set(output={
            "resolved": [r["food"] for r in resolved],
            "unresolved": [u["phrase"] for u in unresolved],
        })
    return resolved, unresolved


async def _stamp_save_intent(log_id: str, name: str, triggers: list[str] | None) -> int:
    """§8.4 on a log that is still staged: the intent rides the rows' staged
    jsonb under the `save_intent` key, so commit step 4 builds
    the macro from the committed items. A jsonb MERGE: nothing else on the
    row is touched (no KEEP_LOG_KEYS_SQL needed), and KEEP_LOG_KEYS_SQL
    carries the object whole through every later rewrite (tier 3,
    /api/resolve, the commit). Returns the rows stamped."""
    return await db.execute(
        "UPDATE item_resolutions "
        "SET staged = staged || jsonb_build_object('save_intent', %s::jsonb) "
        "WHERE log_id = %s AND status <> 'removed'",
        (Json({"name": name, "trigger_phrases": triggers}), log_id),
    )


async def _run_agent_lane(
    body: LogCreate, meal: Meal, emit: Emit | None = None, image: str | None = None
) -> LogCreateResponse:
    """§6.4: the front-door agent. Its effects are closures over this log:
    the foods the agent names go through _stage_items (the cascade, no
    decompose), an unlabeled dish's components through
    _stage_estimate (flagged rows), a spoken weight through
    routes/weights.record_weight under this log's id. The two staging
    closures append to one log, so they allocate item indexes in turn (one
    lock — the SDK may run tool calls concurrently) and the response merges
    their rows. The response is assembled from what those closures recorded
    — the model contributes the reply and nothing else. `emit` streams the
    run's events to the chat window (routes/agent.py)."""
    import tracing
    from routes import meals as meals_route  # lazy: routes/meals → routes/photo → here
    from services import agent as agent_svc

    tracing.init_tracing()
    # This lane is about to run an openai-agents agent, so its span hook goes
    # in here — init_tracing no longer installs it (importing the SDK is ~1.1 s
    # a cold boot must not pay for the lanes that never reach a model). It must
    # be installed before the "agent" span below opens. getattr because the
    # route tests swap tests/tracing_stub.py in, which has no hook.
    tracing.install_agents_hook()
    result: dict = {
        "items": [], "food_meal": None, "logged_at": None, "weight": None,
        "basis": None, "estimate_meal": None, "next_index": 0, "discarded": [],
        "saved_meal": None, "note": None, "updated": None,
    }
    staging = asyncio.Lock()
    history = [t.model_dump(mode="json") for t in (body.history or [])]
    # the conversation's logs as the database has them now, not as the chat
    # remembers them (what each turn first staged; a sheet edit, tier 3's
    # pick or a commit from another device never reaches its copy)
    for turn in history:
        if turn.get("log_id"):
            await _refresh_turn(turn)
    # the logs the conversation names — the only ones a correction may replace
    known_logs = {t["log_id"] for t in history if t.get("log_id")}

    async def discard_staged(log_id: str) -> dict:
        """§6.4 corrections: the earlier turn's log this turn replaces — only
        a log from this conversation, only while it is still staged. A
        committed one is in the diary: the tool says so and nothing is
        restaged (the diary's own edit is the correction path)."""
        if log_id not in known_logs:
            return {"error": "unknown_log",
                    "message": "only a log from this conversation can be replaced"}
        updated = await db.execute(
            "UPDATE logs SET status = 'discarded' WHERE id = %s AND status = 'staged'",
            (log_id,),
        )
        if updated:
            result["discarded"].append(log_id)
            return {"log_id": log_id, "status": "discarded"}
        row = await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,))
        status = row["status"] if row else "unknown"
        if status == "discarded":
            # the person dismissed that sheet already (or an earlier turn
            # replaced it): nothing to replace — the correction is a fresh log
            return {"log_id": log_id, "status": "discarded", "already": True,
                    "message": "already discarded — nothing to replace; stage the "
                               "corrected version as if said fresh"}
        return {
            "error": "not_staged", "log_id": log_id, "status": status,
            "message": f"that log is {status}, not staged — do not restage it; if it is "
                       "committed, say it is in the diary and can be edited there",
        }

    def summary(items: list[StagedItem]) -> list[dict]:
        # what was staged, for the model — never the numbers: the reply must
        # not state macros, the sheet does
        return [
            {
                "phrase": it.phrase,
                "status": it.status,
                "food": it.food.name if it.food else None,
                "qty": it.qty,
                "serving": it.serving.description if it.serving else None,
                **({"estimate_note": it.estimate_note} if it.estimate_note else {}),
            }
            for it in items
        ]

    async def stage_food(
        items: list[FoodItem], meal_hint: str | None, when_hint: str | None,
        when_day_offset: int | None = None, when_meal: str | None = None,
    ) -> dict:
        async with staging:
            rows, food_meal, logged_at = await _stage_items(
                body, meal, items, start_index=result["next_index"],
                meal_hint=meal_hint, when_hint=when_hint,
                when_day_offset=when_day_offset, when_meal=when_meal,
            )
            result["next_index"] += len(rows)
            result["items"].extend(rows)
            result["food_meal"] = food_meal
            result["logged_at"] = logged_at
        # §3.4: looked up now, before the model writes its reply — outside
        # the staging lock, so the other tools are not held for the wait
        rows = await _resolve_inline(body.id, rows, reserve=_AGENT_RESERVE_S)
        async with staging:
            _replace_rows(result["items"], rows)
        return {"meal": food_meal.value, "items": summary(rows)}

    async def stage_estimate(
        items: list[EstimateItem], basis: str, meal_hint: str | None,
        when_hint: str | None, when_day_offset: int | None = None,
        when_meal: str | None = None,
    ) -> dict:
        async with staging:
            # with no hint of its own, the meal log_food's hints moved the log
            # to (when it ran first), else the clock's
            fallback = result["food_meal"] or meal
            rows = await _stage_estimate(
                body, fallback, items, start_index=result["next_index"],
                meal_hint=meal_hint, when_hint=when_hint,
                when_day_offset=when_day_offset, when_meal=when_meal,
            )
            result["next_index"] += len(rows)
            result["items"].extend(rows)
            result["basis"] = basis
            if rows:
                result["estimate_meal"] = rows[0].meal
        rows = await _resolve_inline(body.id, rows, reserve=_AGENT_RESERVE_S)  # §3.4, as log_food's
        async with staging:
            _replace_rows(result["items"], rows)
        return {
            "meal": (rows[0].meal if rows else fallback).value,
            "basis": basis,
            "items": summary(rows),
            "note": "Each row is flagged as an estimate; the user adjusts it in the list or in chat.",
        }

    async def adjust_photo(changes: list) -> dict:
        """§3.4b in the chat: the corrections land on the photographed plate
        this conversation is tuning — `body.photo_log_id`, which the client
        derives from its own chat and the tool never names. The plate as it
        now stands rides back on the outcome (`AgentOutcome.updated`), because
        this turn's own log stages nothing."""
        from routes import photo as photo_route  # lazy: photo imports this module

        async with staging:
            out = await photo_route.adjust_rows(body.photo_log_id, changes, body)
            if "error" not in out:
                # read twice on purpose: `out` is the model's view (no macros,
                # ever — the reply must not state them), this is the client's
                result["updated"] = await _load_log_response(body.photo_log_id)
        # §3.4: a food the photo missed ("add") is looked up now like any row —
        # after the lock, then the plate is read again for the card
        plate = result["updated"] if "error" not in out else None
        if plate is not None and any(r.status == "searching" for r in plate.items):
            await _resolve_inline(body.photo_log_id, plate.items, reserve=_AGENT_RESERVE_S)
            result["updated"] = await _load_log_response(body.photo_log_id)
        return out

    async def record_weight(value: float, unit: str, day) -> dict:
        previous = await weights_route.latest_entry(before=day)
        entry = await weights_route.record_weight(
            weight=value, unit=unit, day=day, source="agent", log_id=body.id,
        )
        result["weight"] = entry
        shown = weights_route.from_kg(entry.weight_kg, unit)
        out: dict = {"day": entry.day.isoformat(), "weight": shown, "unit": unit}
        if previous is not None:
            before = weights_route.from_kg(previous.weight_kg, unit)
            out["previous"] = {"day": previous.day.isoformat(), "weight": before, "unit": unit}
            out["change"] = round(shown - before, 1)  # computed here, never by the model
        return out

    def newest_conversation_log() -> str | None:
        for t in reversed(history):
            if t.get("log_id") and t.get("state") != "discarded":
                return t["log_id"]
        return None

    def note_meal(saved) -> None:
        result["saved_meal"] = SavedMealNote(
            meal_id=saved.id, name=saved.name, item_count=len(saved.items)
        )

    async def past_foods(query: str | None, limit: int) -> list[dict]:
        """§6.4's one read: a row per food this account has logged, carrying
        the amount it was last logged at. qty/grams are `numeric` — cast, or
        json.dumps(default=str) hands the model "1.5" as a string. Null keys
        are dropped: brand, grams and a dangling serving are often empty, and
        the model reads this list whole. body.tz is an unvalidated client
        string; past_foods falls back to UTC on one it cannot resolve."""
        rows = await foods_repo.past_foods(query, limit, body.tz)
        return [
            {
                k: v
                for k, v in {
                    "food_id": r["food_id"],
                    "food": r["display_name"],
                    "brand": r["brand"],
                    "qty": float(r["qty"]) if r["qty"] is not None else None,
                    "serving_id": r["serving_id"],
                    "serving": r["serving"],
                    "grams": float(r["grams"]) if r["grams"] is not None else None,
                    "day": str(r["day"]),
                    "meal": r["meal"],
                }.items()
                if v is not None
            }
            for r in rows
        ]

    # the meal writes are read-modify-write on one row: "add a banana and make
    # the eggs 3" is a save_meal and an update_meal the SDK may run at once
    meals = asyncio.Lock()

    async def save_meal(name: str, triggers, log_id: str | None, items) -> dict:
        """§8.4 through the agent: from foods in words (added to the meal of
        that name, or a new one), or from a log — this turn's when it staged
        anything, else the one the conversation names, else the last
        committed — replacing the meal's items. A staged log is stamped
        (saved at commit); a committed one is saved now, exactly as the
        diary's "Save as meal" is (routes/meals.meal_from_log)."""
        async with meals:
            try:
                if items is not None:
                    resolved, unresolved = await _resolve_meal_items(items, meal)
                    if not resolved:
                        # the headline has to match why they failed: telling
                        # them to log a food they already have, because an id
                        # was rejected, sends them the wrong way entirely
                        return {"error": "unresolved", "unresolved": unresolved,
                                "message": (
                                    "none of those is a food this account has logged — "
                                    "look them up with past_foods, or name them in words"
                                    if all(u["status"] == "unknown_food" for u in unresolved)
                                    else "none of those foods is in the databases yet — "
                                         "log them once first"
                                )}
                    rows = [{"food_id": r["food_id"], "serving_id": r["serving_id"], "qty": r["qty"]}
                            for r in resolved]
                    existing = await _find_meal(name)
                    if existing is not None and existing["is_active"]:
                        # extend: the same food and serving adds to its quantity
                        kept = [dict(i) for i in existing["items"] or []]
                        for r in rows:
                            same = next((k for k in kept if k["food_id"] == r["food_id"]
                                         and k["serving_id"] == r["serving_id"]), None)
                            if same is not None:
                                same["qty"] = float(same.get("qty") or 1) + r["qty"]
                            else:
                                kept.append(r)
                        saved = await meals_route.patch_meal(
                            existing["id"],
                            meals_route.MealPatch(
                                trigger_phrases=triggers,
                                items=[meals_route.MealItemIn(**i) for i in kept],
                            ),
                        )
                    else:
                        saved = await meals_route.create_meal(meals_route.MealCreate(
                            name=name, trigger_phrases=triggers or [],
                            items=[meals_route.MealItemIn(**i) for i in rows],
                        ))
                    note_meal(saved)
                    out = _meal_out(saved)
                    if unresolved:
                        out["unresolved"] = unresolved
                    return out
                target = log_id
                if target is not None and target not in known_logs:
                    return {"error": "unknown_log",
                            "message": "only a log from this conversation can be saved"}
                if target is None:
                    async with staging:
                        # anything this turn staged — log_food's rows OR
                        # log_estimate's
                        if result["items"]:
                            target = str(body.id)
                    target = target or newest_conversation_log()
                if target is None:
                    last = await db.fetchone(_LAST_COMMITTED_LOG_SQL)  # §8.4 path a
                    if last is None:
                        return {"error": "nothing_to_save",
                                "message": f"nothing logged yet to save as '{name}' — log the items first"}
                    target = str(last["id"])
                row = await db.fetchone("SELECT status FROM logs WHERE id = %s", (target,))
                status = row["status"] if row else "unknown"
                if status == "staged":
                    if not await _stamp_save_intent(target, name, triggers):
                        return {"error": "nothing_to_save",
                                "message": "that log has nothing staged to save"}
                    result["note"] = f"'{name}' will be saved when you commit."
                    return {"meal": name, "pending": True, "log_id": target,
                            "message": f"'{name}' is saved the moment they commit this log — say so"}
                if status != "committed":
                    return {"error": "not_saved", "log_id": target, "status": status,
                            "message": f"that log is {status} — nothing to save"}
            except HTTPException as e:
                return _meal_error(e)
            try:
                saved = await meals_route.meal_from_log(meals_route.MealFromLog(
                    log_id=UUID(target), name=name, trigger_phrases=triggers or [],
                ))
            except HTTPException as e:
                # meal_from_log's own 409 is "that log committed nothing" —
                # never a name collision, so it must not read as one
                return _meal_error(e, conflict="empty_log")
            note_meal(saved)
            return _meal_out(saved)

    async def update_meal(name: str, new_name, triggers, is_active, set_qty, remove) -> dict:
        """The Library sheet's edits (routes/meals.patch_meal), by name."""
        async with meals:
            # a reactivation means the inactive namesake, when there is one
            row = await _find_meal(name, prefer_inactive=is_active is True)
            if row is None:
                names = [r["name"] for r in await db.fetchall(
                    "SELECT name FROM meal_macros ORDER BY is_active DESC, name"
                )]
                return {"error": "not_found", "message": f"no saved meal called '{name}'",
                        "meals": names}
            current = (await meals_route._hydrate([row]))[0]
            patch: dict = {}
            if new_name is not None:
                patch["name"] = new_name
            if triggers is not None:
                patch["trigger_phrases"] = triggers
            if is_active is not None:
                patch["is_active"] = is_active
            if set_qty or remove:
                items = [it.model_dump() for it in current.items]
                for change in set_qty or []:
                    hit = _match_meal_item(current, change.food)
                    if isinstance(hit, dict):
                        return hit
                    items[hit]["qty"] = float(change.qty)
                drop: set[int] = set()
                for food in remove or []:
                    hit = _match_meal_item(current, food)
                    if isinstance(hit, dict):
                        return hit
                    drop.add(hit)
                patch["items"] = [
                    meals_route.MealItemIn(food_id=it["food_id"], serving_id=it["serving_id"], qty=it["qty"])
                    for i, it in enumerate(items) if i not in drop
                ]
            try:
                saved = await meals_route.patch_meal(row["id"], meals_route.MealPatch(**patch))
            except HTTPException as e:
                return _meal_error(e)
            if "items" in patch:
                note_meal(saved)
            return _meal_out(saved)

    settings = {
        r["key"]: r["value"]
        for r in await db.fetchall(
            "SELECT key, value FROM settings WHERE key IN ('body', 'profile_notes')"
        )
    }
    last = await weights_route.latest_entry()
    # the unit to assume when none is spoken: how the last weigh-in was said,
    # else the wizard's display preference, else pounds
    body_units = (settings.get("body") or {}).get("units")
    unit_pref = last.unit if last is not None else ("kg" if body_units == "metric" else "lb")
    try:
        now_local = body.captured_at.astimezone(ZoneInfo(body.tz))
    except Exception:
        now_local = body.captured_at.astimezone(timezone.utc)
    deps = agent_svc.AgentDeps(
        stage_food=stage_food,
        stage_estimate=stage_estimate,
        record_weight=record_weight,
        discard_log=discard_staged,
        save_meal=save_meal,
        update_meal=update_meal,
        past_foods=past_foods,
        # §3.4b: a conversation with a staged plate in it runs on the photo
        # tier and can edit that plate; every other one is unchanged
        # §6.4: an image attached to this turn — the agent looks at it
        image=image,
        photo_chat=body.photo_log_id is not None,
        adjust_photo=adjust_photo if body.photo_log_id is not None else None,
        saved_meals=[_meal_words(m) for m in await meals_route.list_meals(include_inactive=True)],
        # §6.4: their diary foods, any lane — what a misheard name is matched
        # against and what log_food names by id
        my_foods=[
            f"{r['id']}: {r['display_name']}" + (f" ({r['brand']})" if r["brand"] else "")
            for r in await foods_repo.recent_foods(agent_svc.MY_FOODS_IN_CONTEXT)
        ],
        history=history,
        now_local=now_local,
        unit_pref=unit_pref,
        last_weight=(
            {
                "day": last.day.isoformat(),
                "weight": weights_route.from_kg(last.weight_kg, last.unit),
                "unit": last.unit,
            }
            if last is not None
            else None
        ),
        profile_notes=settings.get("profile_notes") or "",
        web_lane=auth.is_owner(),  # §8.9: the hosted search is billed — the owner's
    )
    # §10: the agent's step in this run's trace — the SDK's model turns and
    # tool calls nest under it, the cascade under log_food
    async with tracing.run(
        "agent", kind="agent", provider="openai",
        # the tier this run actually used — a photo conversation is Sol, not
        # Terra, and a trace that said otherwise would make the §10 cost and
        # latency reads wrong for the lane that changed
        model=agent_svc.PHOTO_MODEL if deps.photo_chat else agent_svc.MODEL,
        input=body.text,
        attributes={"web_lane": deps.web_lane, "photo_chat": deps.photo_chat},
    ) as gen:
        run = await agent_svc.run_agent(body.text, deps, emit)
        gen.set(
            output={
                "reply": run.reply,
                "food_calls": run.food_calls,
                "estimate_calls": run.estimate_calls,
                "estimate_basis": result["basis"],
                "weight": run.weight,
                "options": run.options,
                "discarded": result["discarded"],
                "meal_calls": run.meal_calls,
                "photo_calls": run.photo_calls,
                "history_turns": len(history),
                "max_turns_hit": run.max_turns_hit,
            },
        )

    outcome = AgentOutcome(
        reply=run.reply, weight=result["weight"], options=run.options,
        discarded=[UUID(x) for x in result["discarded"]] or None,
        updated=result["updated"],
    )
    if (
        run.options and not result["items"]
        and result["weight"] is None and not result["discarded"] and not run.meal_calls
        and not run.photo_calls
    ):
        # the turn only asked — nothing staged, discarded or weighed: a demo's
        # one-a-day log attempt goes back so the answer (the next turn, a new
        # client id) can claim it, a capped number of times a day (§8.9)
        await quota.refund_question()
    items = sorted(result["items"], key=lambda it: it.item_index)
    # the §8.4 outcome: what the meal tools did (code-side)
    saved_meal, note = result["saved_meal"], result["note"]
    if not items:
        # nothing staged: the row stays an itemless staged log, as the §8.4
        # save-intent path leaves it — nothing counts it (§9)
        return LogCreateResponse(
            log_id=body.id, meal=meal, items=[], saved_meal=saved_meal, note=note, agent=outcome
        )
    return LogCreateResponse(
        log_id=body.id,
        meal=result["food_meal"] or result["estimate_meal"] or meal,
        items=items,
        saved_meal=saved_meal,
        note=note,
        logged_at=result["logged_at"],
        agent=outcome,
    )


def _item_words(it: StagedItem) -> str:
    """A staged row as the agent reads it — the food and the amount, never
    the numbers (the same words the chat's history uses)."""
    amount = f" — {it.qty:g} × {it.serving.description}" if it.qty is not None and it.serving else ""
    return f"{it.food.name if it.food else it.phrase}{amount}"


async def _refresh_turn(turn: dict) -> None:
    """A history turn that names a log, brought up to date from the database
    (§6.4): its status and rows as they are now — a sheet edit, a serving
    switch, tier 3's pick or a commit from another device never reach the
    chat's copy — and a log RLS hides is not this tenant's (or is gone), so
    the turn stops naming it: the conversation cannot make it discardable."""
    log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (turn["log_id"],))
    if log is None:
        turn["log_id"] = None
        return
    if log["status"] in ("staged", "committed", "discarded"):
        turn["state"] = log["status"]
    current = await _load_log_response(UUID(turn["log_id"]))
    if current is not None:
        turn["items"] = [_item_words(it) for it in current.items]


async def _duplicate_post_response(
    log_id: UUID, *, grace: int = _RERUN_GRACE_SECONDS
) -> LogResponse | None:
    """Duplicate POST for a log id this request did not insert (§8.6 retries).

    Returns the response to send — the stored staging state — or None when
    this request has atomically claimed an orphaned staged log (its inserting
    request died mid-pipeline) and the caller must re-run the pipeline.

    The concurrent same-id race is closed here: while another request
    plausibly owns the row (it is younger than `grace` — the lane's worst
    case: _RERUN_GRACE_SECONDS, or the agent lane's run ceiling), a duplicate
    never re-runs the pipeline — it hands back whatever is persisted
    (possibly zero items) and the client polls GET for the rest.
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
        (log_id, grace),
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
    lookup_notes: dict[int, str] | None = None,
) -> None:
    """All staged state is persisted server-side (§3): the client is a renderer.

    logs.captured_at is never touched here — it is the §9 latency/bucketing
    anchor. logged_at (the §7.2 when-hint shift) rides every item's staged
    jsonb as `logged_at` (ISO); resolver/commit.py reads it back for
    entries.logged_at and every per-item rewrite preserves it
    (commit.KEEP_LOG_KEYS_SQL). when_hint_note (an ignored hint) rides the
    same extra-key mechanism as spoken_qty. (A save intent reaches the rows
    through the agent's save_meal — _stamp_save_intent — not through here.)
    """
    async with db.tx() as conn:
        await conn.execute(
            """
            UPDATE logs SET short_circuit = %s, meal_inferred = %s,
                            decompose_ms = coalesce(%s, decompose_ms),
                            trace_id = coalesce(%s, trace_id)
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
                if lookup_notes and lookup_notes.get(item.item_index):
                    # §6.4: what the chat agent knew about this food (the
                    # store, the dish) — tier 3 reads it as "what the person
                    # meant". Not in KEEP_LOG_KEYS_SQL: a rewrite drops it.
                    staged_payload["lookup_note"] = lookup_notes[item.item_index]
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
    shifted: datetime | None = None  # §7.2: the diary day, if it moved
    for r in rows:
        # Mutate the raw payload, not a validated model: the staged jsonb can
        # carry extra keys (spoken_qty/spoken_unit, §7.3) a model round-trip
        # would silently drop.
        raw = dict(r["staged"])
        if shifted is None and raw.get("logged_at"):
            try:
                shifted = datetime.fromisoformat(raw["logged_at"])
            except ValueError:
                pass
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
    return LogPollResponse(log_id=log["id"], meal=meal, items=items, logged_at=shifted)


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
    immediately, so the staging error stays visible in telemetry without
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
