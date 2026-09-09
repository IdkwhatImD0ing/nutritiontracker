"""POST /api/agent — the Agent lane's streaming front door (DESIGN.md §5, §6.4; 2026-09-06).

The chat window talks to this route: the same body as POST /api/logs (JSON,
or multipart with the audio blob and the same fields), the same §8.6 claim,
demo quota and pipeline (routes/logs.py's helpers) — but the answer is a
stream of newline-delimited JSON events instead of one payload, so the window
shows the transcript the moment the transcriber returns it, each tool the
agent calls as it calls it, its reply, and finally the staged log (`done`:
the LogCreateResponse POST /api/logs would have answered, or the voice lane's
capture_failure). A failure after the stream has started rides it as an
`error` event carrying the HTTP status it would have been; everything before
the first byte — the claim's 404/409, the quota's 429, a bad audio part — is
the plain HTTP error it always was. A same-id retry (§8.6) re-attaches to the
run it lost while this process still has it — streaming, or finished within
ten minutes — and gets the buffered transcript, tool lines and reply, then the
rest live; a retry that finds no run to attach to (another instance, later)
gets the stored answer as a single `done` marked `replay`.

`agent` is implied: every log through here runs the front-door agent.

Since 2026-09-08 the multipart form carries a plate photo instead of audio
(§3.4b) and the photo lane is a chat turn: the identical vision → cascade →
staging pipeline POST /api/logs/photo runs, shown as a `read_photo` tool line
while it works, and a reply built in code from what vision saw. That turn runs
no model of its own — there is no utterance to route and the agent cannot see
the image. The turns after it can: `LogCreate.photo_log_id` names the plate,
which puts the conversation on the photo tier and gives the agent the
`adjust_photo` tool, so the amounts are corrected in words.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable, Literal, Union
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, RootModel, TypeAdapter, ValidationError

import db
from auth import require_user
from models import AgentTurn, LogCreate, LogResponse, Meal
from routes.logs import (
    _AGENT_RERUN_GRACE_SECONDS,
    _MULTIPART_OPENAPI,
    CaptureFailureResponse,
    Emit,
    LogCreateResponse,
    _MultipartRoute,
    _run_pipeline,
    claim_text_log,
    claim_voice_log,
    release_claim,
    transcribe_for_log,
)

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_user)])

_TURNS = TypeAdapter(list[AgentTurn])
# the multipart form documented on the JSON operation, plus the chat's history
# and the photo part. `audio` stops being required: the form carries exactly
# one of audio (a voice turn) or image (a photo turn, §3.4b).
_AGENT_MULTIPART_OPENAPI = json.loads(json.dumps(_MULTIPART_OPENAPI))
_AGENT_FORM = _AGENT_MULTIPART_OPENAPI["requestBody"]["content"]["multipart/form-data"]["schema"]
_AGENT_FORM["required"] = [k for k in _AGENT_FORM["required"] if k != "audio"]
_AGENT_FORM["properties"]["history"] = {
    "type": "string", "description": "JSON array of AgentTurn — the chat so far (§6.4)"
}
_AGENT_FORM["properties"]["image"] = {
    "type": "string", "format": "binary",
    "description": "a plate photo — the §3.4b lane as a chat turn; exactly one of audio or image",
}
_AGENT_FORM["properties"]["photo_log_id"] = {
    "type": "string", "format": "uuid",
    "description": "the staged photo log this conversation is tuning (§3.4b)",
}


def _parse_history(raw: str | None) -> list[AgentTurn] | None:
    """The voice form's `history` field: a JSON array of AgentTurn, or 422."""
    if not raw:
        return None
    try:
        return _TURNS.validate_json(raw)
    except ValidationError as e:
        raise HTTPException(422, f"history: {e.errors()[0].get('msg', 'invalid')}")


# ---------------------------------------------------------------------------
# The wire events — one JSON object per line (typegen picks them up, §2)
# ---------------------------------------------------------------------------


class AgentTranscriptEvent(BaseModel):
    """The spoken utterance as transcribed — the chat's user bubble (voice only)."""

    type: Literal["transcript"] = "transcript"
    text: str


class AgentToolCallEvent(BaseModel):
    """The agent called a tool: `name` (log_food, log_estimate, log_weight,
    discard_log, past_foods, save_meal, update_meal, adjust_photo, web_search)
    with `args` as the model passed them. `read_photo` is the exception — no
    model called it: the photo turn (§3.4b) emits the pair itself so the chat
    can show the vision call while it runs."""

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: dict[str, Any]


class AgentToolResultEvent(BaseModel):
    """What the tool answered (sanitized: never macros — the staged list shows
    those). For web_search: the sources it consulted."""

    type: Literal["tool_result"] = "tool_result"
    id: str
    name: str | None = None
    output: Any = None


class AgentReplyEvent(BaseModel):
    """The agent's reply — or its clarifying question, when `options` carries
    the 2–4 answers the chat offers as chips (§6.4)."""

    type: Literal["reply"] = "reply"
    text: str
    options: list[str] | None = None


class AgentDoneEvent(BaseModel):
    """The last event of a successful run: what POST /api/logs would have
    answered. `replay` (§8.6): a same-id retry answered from the stored log
    rather than a run this request made or attached to — no tool lines and no
    reply came with it (the reply is not stored, BACKLOG 139)."""

    type: Literal["done"] = "done"
    log: LogCreateResponse | CaptureFailureResponse
    replay: bool = False


class AgentErrorEvent(BaseModel):
    """The last event of a failed run, with the HTTP status it would have been."""

    type: Literal["error"] = "error"
    status: int
    detail: str


class AgentEvent(
    RootModel[
        Annotated[
            Union[
                AgentTranscriptEvent,
                AgentToolCallEvent,
                AgentToolResultEvent,
                AgentReplyEvent,
                AgentDoneEvent,
                AgentErrorEvent,
            ],
            Field(discriminator="type"),
        ]
    ]
):
    """One line of the POST /api/agent stream."""


NDJSON = "application/x-ndjson"
_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}

Work = Callable[[Emit], Awaitable[LogCreateResponse | CaptureFailureResponse]]


class NDJSONResponse(StreamingResponse):
    media_type = NDJSON


def _line(event: dict | BaseModel) -> bytes:
    data = AgentEvent.model_validate(event).model_dump(mode="json")
    return (json.dumps(data, default=str) + "\n").encode("utf-8")


def _as_create(replay: LogResponse | LogCreateResponse | CaptureFailureResponse):
    """A replay from the claim helpers is a LogResponse; the done event
    carries the create shape (the extra fields are simply absent)."""
    if isinstance(replay, (LogCreateResponse, CaptureFailureResponse)):
        return replay
    return LogCreateResponse.model_validate(replay.model_dump(mode="json"))


async def _single(event: BaseModel) -> AsyncIterator[bytes]:
    yield _line(event)


# ---------------------------------------------------------------------------
# Live runs — a same-id retry re-attaches to the stream it lost (§8.6)
# ---------------------------------------------------------------------------


@dataclass
class _LiveRun:
    """One run's events, buffered in this process while it streams and, when
    it succeeded, for _RUN_TTL_SECONDS after it ends. A same-id retry — a
    phone that lost the response mid-stream and sends the same client id
    again (§8.6) — follows this buffer instead of getting the stored replay:
    the transcript, the tool lines and the reply it missed, then the rest
    live, and nothing runs twice. A run that failed leaves the table the
    moment it ends (its followers see the error; the next same-id request
    re-enters the claim and runs again), and one still unfinished after
    _RUN_MAX_SECONDS is presumed dead. `changed` is replaced on every append
    so each waiter wakes exactly once. `owner` is the tenant whose log it is:
    the table sits in front of the database, so it keeps the database's rule
    itself — another tenant never sees the run (§8.9)."""

    events: list[dict] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    owner: str | None = None

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    def append(self, event: dict) -> None:
        self.events.append(event)
        if event.get("type") in ("done", "error"):
            self.finished_at = time.monotonic()
        waiters, self.changed = self.changed, asyncio.Event()
        waiters.set()


_RUNS: dict[UUID, _LiveRun] = {}
_RUN_TTL_SECONDS = 600.0  # a finished run stays attachable this long
# a run with no terminal event after this is presumed dead (Fluid caps an
# invocation at 120 s). The same span is the agent lane's §8.6 orphan grace
# (routes/logs), so a retry on another instance claims a run's log only once
# no live run can still own it.
_RUN_MAX_SECONDS = float(_AGENT_RERUN_GRACE_SECONDS)


def _interrupted(detail: str = "The agent run was interrupted — try again.") -> dict:
    return AgentErrorEvent(status=500, detail=detail).model_dump()


def live_run(log_id: UUID) -> _LiveRun | None:
    """The run a same-id request attaches to — streaming, or finished within
    the TTL — from this process's table. Expired entries are dropped here,
    and a run still unfinished past _RUN_MAX_SECONDS is closed with an error
    (its followers end; the database state stands and the §8.6 orphan claim
    takes over) and dropped too. Another instance's run is invisible
    (BACKLOG 156): that retry gets the stored replay — and so is another
    tenant's run on this one: the request falls through to the claim, whose
    answer for a foreign id is the 404 (§8.9)."""
    now = time.monotonic()
    for key, run in list(_RUNS.items()):
        if run.finished:
            # a failed run is never handed out (its own cleanup removes it the
            # moment it ends; this is the belt to that brace)
            if now - run.finished_at > _RUN_TTL_SECONDS or run.events[-1].get("type") == "error":
                del _RUNS[key]
        elif now - run.started_at > _RUN_MAX_SECONDS:
            run.append(_interrupted("The agent run went quiet — try again."))
            del _RUNS[key]
            if run.task is not None:
                # a zombie must not keep writing into a log a replacement run
                # may claim next; its cleanup below sees it no longer owns the slot
                run.task.cancel()
    run = _RUNS.get(log_id)
    if run is not None and run.owner != db.current_user_id.get():
        return None
    return run


async def _follow(run: _LiveRun) -> AsyncIterator[bytes]:
    """Yield the run's events from the first, then live, until done/error —
    or until the run has been silent for _RUN_MAX_SECONDS, which ends the
    stream with an error rather than holding the connection forever."""
    i = 0
    while True:
        # captured before the length check: an append in between is seen
        # either way (in the buffer now, or through the waiter it set)
        waiter = run.changed
        if i < len(run.events):
            event = run.events[i]
            i += 1
            yield _line(event)
            if event.get("type") in ("done", "error"):
                return
            continue
        try:
            await asyncio.wait_for(waiter.wait(), timeout=_RUN_MAX_SECONDS)
        except asyncio.TimeoutError:
            yield _line(_interrupted("The agent run went quiet — try again."))
            return


def _start(log_id: UUID, work: Work) -> _LiveRun:
    """Run `work` as its own task, feeding the run's buffer: a client that
    disconnects mid-run drops its follower, never the staging — the log is
    claimed and the same id re-attaches (§8.6). Every ending appends a
    terminal event, cancellation included, so no follower waits on a run
    that is over; a failed run leaves the table and releases its claim on
    the itemless log (routes/logs.release_claim), so the owner's same id
    runs again at once rather than replaying the failure — a demo's attempt
    stands (§8.9), so its retry is the day's 429. Only the run that still
    owns its slot cleans up: an evicted run's slot may already belong to a
    replacement, whose entry and log must stand."""
    run = _LiveRun(owner=db.current_user_id.get())
    _RUNS[log_id] = run

    async def emit(event: dict) -> None:
        run.append(event)

    async def go() -> None:
        import tracing  # lazy: stubbed in route tests

        try:
            result = await work(emit)
            run.append(AgentDoneEvent(log=result).model_dump(mode="json"))
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else json.dumps(e.detail, default=str)
            run.append(AgentErrorEvent(status=e.status_code, detail=detail).model_dump())
        except Exception:
            log.exception("agent stream failed")
            run.append(AgentErrorEvent(status=500, detail="The agent run failed — try again.").model_dump())
        finally:
            if not run.finished:  # cancelled, or anything that is not an Exception
                run.append(_interrupted())
            if run.events[-1].get("type") == "error" and _RUNS.get(log_id) is run:
                del _RUNS[log_id]
                try:
                    await release_claim(log_id)
                except Exception:
                    log.exception("could not release the failed run's log")
            # §10: the request's middleware may have returned already (a
            # disconnect, the silence timeout) — this task waits for its own send
            await tracing.drain_pending()

    run.task = asyncio.create_task(go())
    return run


async def _replay(log_id: UUID, stored, *, with_text: bool) -> AsyncIterator[bytes]:
    """The stored answer to a same-id retry that found no run to attach to
    (§8.6): for the voice lane, the transcript the row kept first (so the
    chat's pending bubble fills), then `done` marked a replay — no tool lines,
    no reply (BACKLOG 139)."""
    if with_text and not isinstance(stored, CaptureFailureResponse):
        row = await db.fetchone("SELECT raw_input FROM logs WHERE id = %s", (log_id,))
        text = ((row["raw_input"] if row else None) or "").strip()
        if text:
            yield _line(AgentTranscriptEvent(text=text))
    yield _line(AgentDoneEvent(log=_as_create(stored), replay=True))


def _stream(body: AsyncIterator[bytes]) -> NDJSONResponse:
    return NDJSONResponse(body, media_type=NDJSON, headers=_HEADERS)


# ---------------------------------------------------------------------------
# The routes — the same two-content-type arrangement as POST /api/logs
# ---------------------------------------------------------------------------


async def agent_voice(
    log_id: Annotated[UUID, Form(alias="id")],
    captured_at: Annotated[datetime, Form()],
    tz: Annotated[str, Form()],
    audio: Annotated[UploadFile | None, File()] = None,
    image: Annotated[UploadFile | None, File()] = None,
    capture_fallback: Annotated[bool, Form()] = False,
    capture_error: Annotated[str | None, Form()] = None,
    history: Annotated[str | None, Form()] = None,
    meal_hint: Annotated[Meal | None, Form()] = None,
    photo_log_id: Annotated[UUID | None, Form()] = None,
) -> NDJSONResponse:
    """The multipart turn — exactly one of `audio` or `image`.

    Audio: a same-id retry re-attaches to its live run; else the claim
    before the stream (its errors are plain HTTP), then transcribe →
    `transcript` → the agent's events → `done`. `history` is the chat so far
    as a JSON array of AgentTurn (the JSON route takes it as a field).

    Image (§3.4b, 2026-09-08): the photo lane as a chat turn. The same
    re-attach and claim, then the identical vision → cascade → staging
    pipeline `POST /api/logs/photo` runs (routes/photo._stage_photo), shown
    as a `read_photo` tool line, and a reply built in code from what vision
    saw. No agent runs on this turn — there is no utterance to route, and the
    model cannot see the photo; the tuning turns that follow are the agent's."""
    turns = _parse_history(history)
    if (audio is None) == (image is None):
        raise HTTPException(422, "send exactly one of `audio` or `image`")
    if image is not None:
        return await _agent_photo(image, log_id, captured_at, tz, meal_hint, turns)
    live = live_run(log_id)
    if live is not None:
        return _stream(_follow(live))
    claimed = await claim_voice_log(
        audio, log_id, captured_at, capture_fallback, capture_error, agent=True
    )
    if not isinstance(claimed, tuple):
        return _stream(_replay(log_id, claimed, with_text=True))
    audio_bytes, mime = claimed

    async def work(emit: Emit):
        import tracing  # lazy: stubbed in route tests

        # §10: one trace per run (the log id as its session), STT first; the
        # send follows the stream's end, never a chunk of it
        async with tracing.run(
            "agent-chat", session_id=str(log_id), tags=["lane:agent", "lane:voice"],
            input={"mime": mime, "bytes": len(audio_bytes)},
        ) as trace:
            text = await transcribe_for_log(log_id, audio_bytes, mime, capture_error)
            if not isinstance(text, str):
                trace.set(output=text)
                return text  # capture_failure — the client prompts a retype (§5)
            trace.set(input=text)
            await emit(AgentTranscriptEvent(text=text).model_dump())
            body = LogCreate(
                id=log_id, text=text, captured_at=captured_at, tz=tz,
                capture_fallback=capture_fallback, capture_error=capture_error, agent=True,
                history=turns, photo_log_id=photo_log_id,
            )
            response = await _run_pipeline(body, emit)
            trace.set(output=response)
            return response

    return _stream(_follow(_start(log_id, work)))


async def _agent_photo(
    image: UploadFile,
    log_id: UUID,
    captured_at: datetime,
    tz: str,
    meal_hint: Meal | None,
    turns: list[AgentTurn] | None,
) -> NDJSONResponse:
    """The photo turn (§3.4b in the chat). The upload's two validations happen
    here, before the first byte of the stream, so 415/413 stay the plain HTTP
    errors the lane has always answered with; everything after rides the
    stream. `turns` is accepted and ignored: this turn runs no model, so there
    is no conversation to read — the chat keeps its own history for the tuning
    turns that follow."""
    from routes import photo as photo_route  # lazy: it imports routes/logs

    image_bytes, sniffed = await photo_route.read_image(image)
    live = live_run(log_id)
    if live is not None:
        return _stream(_follow(live))

    async def work(emit: Emit):
        import tracing  # lazy: stubbed in route tests

        # §10: one trace per run, the log id as its session — the same shape
        # the standalone photo route opens, tagged for both lanes
        async with tracing.run(
            "agent-chat", session_id=str(log_id), tags=["lane:agent", "lane:photo"],
            input={"mime": sniffed, "bytes": len(image_bytes)},
        ) as trace:
            # the §8.6 claim, the demo quota and the replay all live inside
            # _stage_photo, as they do for POST /api/logs/photo
            response = await photo_route._stage_photo(
                image_bytes, sniffed, log_id, captured_at, meal_hint, tz, emit,
                # this lane's run lives in _RUNS for _RUN_MAX_SECONDS, so the
                # §8.6 orphan window has to be the same span — at 90 s a retry
                # elsewhere would re-run vision under a run still thinking
                grace=_AGENT_RERUN_GRACE_SECONDS,
            )
            trace.set(output=response)
        # _stage_photo builds the reply itself (photo_reply) on every path that
        # actually ran vision; a §8.6 replay of an earlier attempt has none —
        # the rows in the `done` event are the answer, and the chat says so.
        reply = response.agent.reply if response.agent else None
        await emit(
            AgentReplyEvent(
                text=reply or "That photo had already gone through — here is what it staged."
            ).model_dump()
        )
        return response

    return _stream(_follow(_start(log_id, work)))


router.add_api_route(
    "/api/agent",
    agent_voice,
    methods=["POST"],
    route_class_override=_MultipartRoute,
    response_class=NDJSONResponse,
    include_in_schema=False,  # documented on the JSON operation below
)


@router.post(
    "/api/agent",
    response_class=NDJSONResponse,
    responses={
        200: {
            "model": AgentEvent,
            "description": "A stream of newline-delimited JSON events, one AgentEvent per line, "
                           "ending with `done` or `error`.",
        }
    },
    openapi_extra=_AGENT_MULTIPART_OPENAPI,
)
async def agent_text(body: LogCreate) -> NDJSONResponse:
    """Text: a same-id retry re-attaches to its live run; else the claim
    before the stream, then the agent's events → `done`. The multipart form
    (audio + the same fields) is the other content type on this path, as for
    POST /api/logs."""
    body = body.model_copy(update={"agent": True})
    live = live_run(body.id)
    if live is not None:
        return _stream(_follow(live))
    replay = await claim_text_log(body)
    if replay is not None:
        return _stream(_replay(body.id, replay, with_text=False))

    async def work(emit: Emit):
        import tracing  # lazy: stubbed in route tests

        async with tracing.run(
            "agent-chat", input=body.text, session_id=str(body.id), tags=["lane:agent"]
        ) as trace:
            response = await _run_pipeline(body, emit)
            trace.set(output=response)
            return response

    return _stream(_follow(_start(body.id, work)))
