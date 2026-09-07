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
the plain HTTP error it always was. A same-id replay is a single `done`.

`agent` is implied: every log through here runs the front-door agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable, Literal, Union
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, RootModel

from auth import require_user
from models import LogCreate, LogResponse
from routes.logs import (
    _MULTIPART_OPENAPI,
    CaptureFailureResponse,
    Emit,
    LogCreateResponse,
    _MultipartRoute,
    _run_pipeline,
    claim_text_log,
    claim_voice_log,
    transcribe_for_log,
)

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_user)])


# ---------------------------------------------------------------------------
# The wire events — one JSON object per line (typegen picks them up, §2)
# ---------------------------------------------------------------------------


class AgentTranscriptEvent(BaseModel):
    """The spoken utterance as transcribed — the chat's user bubble (voice only)."""

    type: Literal["transcript"] = "transcript"
    text: str


class AgentToolCallEvent(BaseModel):
    """The agent called a tool: `name` (log_food, log_estimate, log_weight,
    web_search) with `args` as the model passed them."""

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
    type: Literal["reply"] = "reply"
    text: str


class AgentDoneEvent(BaseModel):
    """The last event of a successful run: what POST /api/logs would have answered."""

    type: Literal["done"] = "done"
    log: LogCreateResponse | CaptureFailureResponse


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


async def _events(work: Work) -> AsyncIterator[bytes]:
    """Run `work` with an emit callback and yield its events as NDJSON lines,
    ending with `done` or `error`. The work runs as its own task: a client
    that disconnects mid-run cancels this generator, never the staging — the
    log is claimed and the client reopens it (§8.6)."""
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def run() -> None:
        try:
            result = await work(emit)
            await queue.put(AgentDoneEvent(log=result).model_dump(mode="json"))
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else json.dumps(e.detail, default=str)
            await queue.put(AgentErrorEvent(status=e.status_code, detail=detail).model_dump())
        except Exception:
            log.exception("agent stream failed")
            await queue.put(
                AgentErrorEvent(status=500, detail="The agent run failed — try again.").model_dump()
            )
        finally:
            await queue.put(None)

    asyncio.create_task(run())
    while True:
        event = await queue.get()
        if event is None:
            break
        yield _line(event)


def _stream(body: AsyncIterator[bytes]) -> NDJSONResponse:
    return NDJSONResponse(body, media_type=NDJSON, headers=_HEADERS)


# ---------------------------------------------------------------------------
# The routes — the same two-content-type arrangement as POST /api/logs
# ---------------------------------------------------------------------------


async def agent_voice(
    audio: Annotated[UploadFile, File()],
    log_id: Annotated[UUID, Form(alias="id")],
    captured_at: Annotated[datetime, Form()],
    tz: Annotated[str, Form()],
    capture_fallback: Annotated[bool, Form()] = False,
    capture_error: Annotated[str | None, Form()] = None,
) -> NDJSONResponse:
    """Audio: the claim before the stream (its errors are plain HTTP), then
    transcribe → `transcript` → the agent's events → `done`."""
    claimed = await claim_voice_log(audio, log_id, captured_at, capture_fallback, capture_error)
    if not isinstance(claimed, tuple):
        return _stream(_single(AgentDoneEvent(log=_as_create(claimed))))
    audio_bytes, mime = claimed

    async def work(emit: Emit):
        text = await transcribe_for_log(log_id, audio_bytes, mime, capture_error)
        if not isinstance(text, str):
            return text  # capture_failure — the client prompts a retype (§5)
        await emit(AgentTranscriptEvent(text=text).model_dump())
        body = LogCreate(
            id=log_id, text=text, captured_at=captured_at, tz=tz,
            capture_fallback=capture_fallback, capture_error=capture_error, agent=True,
        )
        return await _run_pipeline(body, emit)

    return _stream(_events(work))


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
    openapi_extra=_MULTIPART_OPENAPI,
)
async def agent_text(body: LogCreate) -> NDJSONResponse:
    """Text: the claim before the stream, then the agent's events → `done`.
    The multipart form (audio + the same fields) is the other content type
    on this path, as for POST /api/logs."""
    body = body.model_copy(update={"agent": True})
    replay = await claim_text_log(body)
    if replay is not None:
        return _stream(_single(AgentDoneEvent(log=_as_create(replay))))

    async def work(emit: Emit):
        return await _run_pipeline(body, emit)

    return _stream(_events(work))
