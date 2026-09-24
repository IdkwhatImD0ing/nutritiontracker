"""MCP server at POST /api/mcp: an agent reads and logs through the app's own
route handlers (DESIGN.md §5; the keys are migration 006's, auth.py).

Not `mcp.py`: backend/ is the import root, so that name would shadow the SDK.

Shape (MCP Python SDK 2.x, pinned >=2.2 — 2.0 leaked a server task for every
cancelled stateless request):
- Stateless, JSON responses: one POST in, one JSON answer out. Nothing lives
  between requests, so any Vercel instance can answer any call, and no SSE
  stream holds the function open.
- Two fixed servers, read-only and read-write, picked per request by the
  caller's scopes. The SDK has no per-caller tool list, and adding or removing
  tools on one shared server would race between concurrent requests. A
  read-only key never sees a write tool; calling one is "Unknown tool".
- Auth runs before dispatch: auth.authenticate (API key or session JWT) binds
  the tenant contextvars, and they reach the tool on both protocol paths (the
  SDK copies the request's context into the tasks it spawns).
- A raw ASGI endpoint (handle), reached through main.py's route, which
  imports this module on the first /api/mcp request: the SDK and the two
  servers cost ~0.4 s, which every cold start paid when main imported it. A
  mount would answer POST /api/mcp with a 307. POST only; GET and DELETE get
  Starlette's 405, because in stateless mode a GET opens an SSE stream that
  never ends.
- transport_security is explicit: the SDK's default turns on a localhost-only
  Host check that would refuse every real host. auth._check_origin already
  does the Origin check.
- subscriptions/listen is refused: its answer is a stream held open until the
  client leaves, which a 120 s function cannot serve.
- The session managers run in one background task per event loop, started
  by the loop's first request (_started) and stopped by main.lifespan's
  shutdown (stop). anyio wants a task group entered and exited by the same
  task, and a request's task ends with it. A manager's run() works once, so
  each start builds fresh ones: the tests run a fresh loop per TestClient.

Every tool calls its handlers with every argument explicit: called outside
FastAPI, a handler's Query() default is a FieldInfo object, not the value. A
handler's HTTPException (404 log not found, 409, 422, the demo quota's 429)
becomes a ToolError the model can read; the SDK reports anything else as
"Error executing tool <name>" and keeps the traceback on the server.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
from contextlib import AsyncExitStack
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

import auth
import db
import deadline
from models import Alternate, DayTotals, LogCreate, Macros, MacroTargets, Meal, Question
from models import SaveIntent, StagedItem, Tier3KickRequest
from resolver import foods_repo
from routes import commit, diary, entries, foods, history, logs, me, meals, resolve, settings, weights
from services import quota

INSTRUCTIONS = """\
Tally is a personal nutrition diary. Logging takes two steps: log_food turns \
a plain sentence ("two eggs and a banana") into STAGED rows matched to foods \
in the database, and nothing is in the diary yet. Show the user the rows, then \
call commit_log to save them or discard_log to throw them away. Every number \
comes from a food database record (USDA FoodData Central, Open Food Facts, or \
a restaurant's published nutrition page), never from a model's estimate; a row \
no database matched is not saved. The read tools never change anything."""


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def _detail_text(detail) -> str:
    """An HTTPException's detail as text: a str as is, FastAPI's validation
    list as its messages, anything else as JSON. An f-string would hand the
    model a Python repr."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and all(isinstance(d, dict) and "msg" in d for d in detail):
        return "; ".join(str(d["msg"]) for d in detail)
    return json.dumps(detail, default=str)


def _handler_errors(fn):
    """A handler's HTTPException as a ToolError, so the model reads the reason
    instead of the SDK's bare "Error executing tool". functools.wraps keeps the
    signature the SDK builds the input schema from."""

    @functools.wraps(fn)
    async def run(**kwargs):
        try:
            return await fn(**kwargs)
        except quota.QuotaExceeded as e:
            # quota._refusal's "<limit> — resets at <iso>", as one sentence
            reason = str(e.detail).partition(" — ")[0]
            raise ToolError(f"{reason}; resets at {e.resets_at}") from e
        except HTTPException as e:
            raise ToolError(f"{_detail_text(e.detail)} (HTTP {e.status_code})") from e

    return run


# ---------------------------------------------------------------------------
# tool output shapes (the rest are the routes' own response models)
# ---------------------------------------------------------------------------


class Goals(BaseModel):
    targets: MacroTargets | None  # None until goals are set in the app
    body: settings.BodyProfile
    timezone: str


class PastFood(BaseModel):
    food_id: str
    food: str
    brand: str | None = None
    qty: float | None = None  # the amount it was last logged at
    serving_id: str | None = None
    serving: str | None = None
    grams: float | None = None
    day: date_type
    meal: str


class StagedRow(BaseModel):
    index: int
    status: str  # resolved | searching | needs_input | not_found
    phrase: str  # what was said for this row
    food: str | None = None
    food_id: str | None = None
    brand: str | None = None
    qty: float | None = None
    serving: str | None = None
    macros: Macros | None = None
    estimated: str = "none"
    alternates: list[Alternate] | None = None
    question: Question | None = None


class LoggedFood(BaseModel):
    log_id: UUID
    meal: Meal
    # set when "yesterday" / "last night" moved the rows off today
    logged_at: datetime | None = None
    items: list[StagedRow]
    reply: str | None = None  # the logging agent's one-line answer
    options: list[str] | None = None  # answers it offers when the reply is a question
    weight: weights.WeightEntry | None = None  # a weigh-in the sentence recorded
    note: str | None = None


class LookupsRetried(BaseModel):
    log_id: UUID
    items: list[StagedRow]


class NotSaved(BaseModel):
    index: int
    status: str
    phrase: str


class Committed(commit.CommitResponse):
    not_saved: list[NotSaved] = []  # kept rows that were not resolved, so not saved


def _row(item: StagedItem) -> StagedRow:
    return StagedRow(
        index=item.item_index,
        status=item.status,
        phrase=item.phrase,
        food=item.food.name if item.food else None,
        food_id=item.food.id if item.food else None,
        brand=item.food.brand if item.food else None,
        qty=item.qty,
        serving=item.serving.description if item.serving else None,
        macros=item.macros,
        estimated=item.estimated,
        alternates=item.alternates,
        question=item.question,
    )


# ---------------------------------------------------------------------------
# read tools
# ---------------------------------------------------------------------------

Day = Annotated[date_type | None, Field(description="YYYY-MM-DD; default today in the account's timezone")]


@_handler_errors
async def whoami() -> me.MeResponse:
    """Who this key or session belongs to: the email, whether it is the owner
    account, and for a demo account today's log quota."""
    return await me.get_me(user=auth.current_user.get())


@_handler_errors
async def get_day(date: Day = None) -> diary.DiaryResponse:
    """One day's diary: entries grouped by meal, and the day's totals against
    the daily targets."""
    return await diary.get_diary(date=date)


@_handler_errors
async def get_history(
    range: Annotated[
        history.Range, Field(description="3d, w (7 days), m (30 days) or y (52 weeks); all end today")
    ] = "w",
) -> history.HistoryResponse:
    """Daily totals over a window ending today, with the targets and the
    logging streak ("y" gives weekly averages instead)."""
    return await history.get_history(rng=range)


@_handler_errors
async def get_goals() -> Goals:
    """The daily macro targets (null until set), the body profile they were
    computed from, and the account's timezone."""
    s = await settings.get_settings()
    return Goals(targets=s.targets, body=s.body, timezone=s.timezone)


@_handler_errors
async def get_weights(
    days: Annotated[int, Field(ge=1, le=3660, description="how many days back")] = 90,
) -> weights.WeightsResponse:
    """Weigh-ins, newest first, plus the latest one."""
    return await weights.get_weights(days=days)


@_handler_errors
async def list_saved_meals(include_inactive: bool = False) -> list[meals.MealOut]:
    """The saved meals ("usual breakfast") with their foods and amounts."""
    return await meals.list_meals(include_inactive=include_inactive)


@_handler_errors
async def search_my_foods(
    query: Annotated[str | None, Field(description="name to search; omit for the most recent")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> list[PastFood]:
    """Foods this account has logged before, each with the amount it was last
    logged at. No nutrition values: get_food has those."""
    rows = await foods_repo.past_foods(query=query, limit=limit, tz=await diary.tz_name())
    return [
        PastFood(
            food_id=r["food_id"], food=r["display_name"], brand=r["brand"], qty=r["qty"],
            serving_id=r["serving_id"], serving=r["serving"], grams=r["grams"],
            day=r["day"], meal=r["meal"],
        )
        for r in rows
    ]


@_handler_errors
async def search_foods(
    query: str,
    include_usda: Annotated[
        bool, Field(description="also search USDA FoodData Central (slower)")
    ] = False,
) -> foods.FoodSearchResponse:
    """Search the food database by name. `local` hits carry a food_id for
    get_food; USDA hits are listed but not yet in the database."""
    return await foods.search_foods(q=query, source="all" if include_usda else "local")


@_handler_errors
async def get_food(food_id: UUID) -> foods.FoodDetail:
    """One food: its servings with macros, its nutrients, and where the numbers
    come from. The provider's raw record (`payload`) is left out."""
    detail = await foods.get_food_detail(food_id=food_id)
    return detail.model_copy(update={"payload": None})


# ---------------------------------------------------------------------------
# write tools
# ---------------------------------------------------------------------------


@_handler_errors
async def log_food(
    text: Annotated[str, Field(description='what was eaten, e.g. "two eggs and toast for breakfast"')],
    tz: Annotated[str | None, Field(description="IANA timezone; default the account's")] = None,
    id: Annotated[
        UUID | None,
        Field(description="any UUID you pick for this sentence; send the same one when "
                          "retrying it after a timeout, so it is logged once"),
    ] = None,
) -> LoggedFood:
    """Turn a sentence into diary rows matched to database foods. The rows are
    STAGED, not saved: show them to the user, then call commit_log with the
    log_id to save them (or discard_log). Words like "yesterday" move the rows
    to that day (logged_at). A row left `searching` ran out of time for its
    lookup: retry_lookups runs it again. Can take up to two minutes. A demo
    account can log once a day.

    A retry with the same id never logs the sentence twice. It returns the
    rows the first call has stored so far, without the reply, and does not
    count as another log; while that call is still running they may be
    incomplete or none, so try again shortly."""
    deadline.start()  # the request's 105 s budget, from the tool's first step
    body = LogCreate(
        id=id or uuid4(), text=text, captured_at=datetime.now(timezone.utc),
        tz=tz or await diary.tz_name(),
    )
    r = await logs.create_log_endpoint(body=body)
    # a same-id replay (routes/logs._duplicate_post_response) is the stored
    # log, a LogPollResponse: no agent outcome, no note
    agent = getattr(r, "agent", None)
    return LoggedFood(
        log_id=r.log_id, meal=r.meal, logged_at=r.logged_at, items=[_row(i) for i in r.items],
        reply=agent.reply if agent else None,
        options=agent.options if agent else None,
        weight=agent.weight if agent else None,
        note=getattr(r, "note", None),
    )


@_handler_errors
async def commit_log(
    log_id: UUID,
    drop_items: Annotated[
        list[int] | None, Field(description="row indexes to remove before saving")
    ] = None,
    save_as_meal: Annotated[
        str | None, Field(description="also save the rows as a saved meal with this name")
    ] = None,
) -> Committed:
    """Save a staged log's resolved rows to the diary and return the day's new
    totals. Rows still searching, needing input or not found are not saved
    (not_saved); `errors` lists rows refused because their serving lacks
    calories or a macro. For a row still searching, call retry_lookups:
    before this commit, so the row is saved with the rest, or after it, and
    retry_lookups saves the row itself once it resolves."""
    staged = await logs.get_log(log_id=log_id)
    drop = set(drop_items or ())
    unknown = drop - {i.item_index for i in staged.items}
    if unknown:
        raise ToolError(f"this log has no staged row {sorted(unknown)}")
    # Every row to keep goes back: the commit marks a stored row missing from
    # the payload as removed (resolver/commit.py).
    keep = [
        StagedItem.model_validate(i.model_dump(exclude={"stale"}))
        for i in staged.items
        if i.item_index not in drop
    ]
    saved = await commit.commit_log(
        log_id=log_id,
        body=commit.CommitRequestIn(
            items=keep, save_intent=SaveIntent(name=save_as_meal) if save_as_meal else None
        ),
    )
    return Committed(
        **saved.model_dump(),
        not_saved=[
            NotSaved(index=i.item_index, status=i.status, phrase=i.phrase)
            for i in keep
            if i.status != "resolved" or i.food is None
        ],
    )


@_handler_errors
async def retry_lookups(log_id: UUID) -> LookupsRetried:
    """Look up again every row of a log that is still `searching` (log_food
    ran out of time for it), and return the log's rows. On a staged log,
    commit_log then saves the rows that resolved. On a log already committed,
    commit_log cannot add rows, so this call saves each row it resolves to
    the diary itself. A row still searching afterwards can be retried later;
    a food no database has comes back not_found. Can take up to two minutes.
    Counts against a demo account's daily lookup limit."""
    deadline.start()  # one 105 s budget for every row this call looks up
    staged = await logs.get_log(log_id=log_id)
    log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,))
    if log["status"] not in ("staged", "committed"):
        raise ToolError(f"this log is {log['status']}; nothing in it can be saved")
    rows = {i.item_index: i for i in staged.items}
    for item in staged.items:
        if item.status != "searching":
            continue
        # The phone's kick is uncapped, one row per request; here the rows
        # share one request, so each lookup gets what is left of its clock,
        # and none starts under the inline lookups' floor. A row not started
        # stays searching for the next call.
        left = deadline.left()
        if left < logs._INLINE_MIN_S:
            break
        deadline.tier3_budget.set(left)
        found = await resolve.kick_tier3(
            body=Tier3KickRequest(log_id=log_id, item_index=item.item_index)
        )
        rows[item.item_index] = found
        # The commit left this row uncommitted (resolver/commit.py); commit-item
        # is the app's late path for it (PendingItems). A numberless photo
        # estimate cannot commit, as there.
        if (
            log["status"] == "committed"
            and found.status == "resolved"
            and found.estimated != "unresolved"
        ):
            await commit.commit_item(
                log_id=log_id,
                body=commit.CommitItemRequest(item_index=found.item_index, item=found),
            )
    return LookupsRetried(log_id=log_id, items=[_row(i) for i in rows.values()])


@_handler_errors
async def discard_log(log_id: UUID) -> logs.DiscardResponse:
    """Throw away a staged log; nothing from it reaches the diary."""
    return await logs.discard_log(log_id=log_id)


@_handler_errors
async def edit_entry(
    entry_id: UUID,
    qty: float | None = None,
    serving_id: str | None = None,
    food_id: UUID | None = None,
) -> entries.EntryPatchResponse:
    """Change a saved diary entry's amount, serving, or food (ids from get_day
    and get_food). Returns the entry and its day's new totals."""
    # str(UUID) is lowercase, the form patch_entry compares the entry's food id in
    return await entries.patch_entry(
        entry_id=entry_id,
        body=entries.EntryPatch(
            qty=qty, serving_id=serving_id, food_id=str(food_id) if food_id is not None else None
        ),
    )


@_handler_errors
async def delete_entry(entry_id: UUID) -> DayTotals:
    """Delete a saved diary entry for good. Returns its day's new totals."""
    return await entries.delete_entry(entry_id=entry_id)


@_handler_errors
async def log_weight(
    weight: Annotated[float, Field(gt=0)],
    unit: weights.Unit,
    day: Day = None,
) -> weights.WeightEntry:
    """Record a weigh-in. One per day: a second one that day replaces it."""
    return await weights.put_weight(body=weights.WeightPut(weight=weight, unit=unit, day=day))


# ---------------------------------------------------------------------------
# the two servers and the endpoint
# ---------------------------------------------------------------------------

_READ_TOOLS = (
    whoami, get_day, get_history, get_goals, get_weights,
    list_saved_meals, search_my_foods, search_foods, get_food,
)
_WRITE_TOOLS = (
    (log_food, None),
    (commit_log, None),
    (retry_lookups, None),
    (discard_log, None),
    (edit_entry, None),
    (delete_entry, ToolAnnotations(destructive_hint=True)),
    (log_weight, None),
)
_READ_ONLY = ToolAnnotations(read_only_hint=True)
_NO_HOST_CHECK = TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _build(write: bool) -> MCPServer:
    # log_level: the constructor calls logging.basicConfig, and at the default
    # INFO every httpx request in the app would be logged
    server = MCPServer("tally", instructions=INSTRUCTIONS, version="1", log_level="WARNING")
    for fn in _READ_TOOLS:
        server.add_tool(fn, annotations=_READ_ONLY)
    if write:
        for fn, annotations in _WRITE_TOOLS:
            server.add_tool(fn, annotations=annotations)
    return server


_SERVERS = {"read": _build(False), "write": _build(True)}


class _Runner:
    """One event loop's session managers: a fresh one per server, each running
    the task group that stateless requests are served in, all entered and
    exited inside this one background task."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        # an empty context: the first request creates this task, and its
        # tenant must not ride into a task that outlives it
        self.task = loop.create_task(self._run(), context=contextvars.Context())

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                for server in _SERVERS.values():
                    server.streamable_http_app(
                        stateless_http=True, json_response=True, transport_security=_NO_HOST_CHECK
                    )
                    await stack.enter_async_context(server.session_manager.run())
                self.ready.set()
                await self.stopping.wait()
        finally:
            self.ready.set()  # a failed start must not leave requests waiting


_runner: _Runner | None = None


async def _started() -> None:
    """Start the runner on this loop's first request (or after the last one
    stopped), then wait until it serves."""
    global _runner
    loop = asyncio.get_running_loop()
    runner = _runner
    # no await between the check and the assignment: two concurrent first
    # requests cannot both start one
    if runner is None or runner.loop is not loop or runner.task.done():
        runner = _runner = _Runner(loop)
    await runner.ready.wait()
    if runner.task.done():
        runner.task.result()  # it failed to start: raise why; the next request retries


async def stop() -> None:
    """main.lifespan's shutdown: end the runner a request started on this loop."""
    runner = _runner
    if runner is not None and runner.loop is asyncio.get_running_loop() and not runner.task.done():
        runner.stopping.set()
        await runner.task


async def handle(scope, receive, send) -> None:
    """POST /api/mcp, the raw ASGI call main.py's route hands over."""
    request = Request(scope, receive)
    try:
        user = await auth.authenticate(request)  # binds db.current_user_id + current_user
    except HTTPException as e:
        headers = {"WWW-Authenticate": "Bearer"} if e.status_code == 401 else None
        response = JSONResponse({"detail": e.detail}, e.status_code, headers=headers)
        return await response(scope, receive, send)
    # the SDK requires this header to match the body's method, so it is
    # enough to refuse the one method that holds a stream open
    if request.headers.get("mcp-method") == "subscriptions/listen":
        response = JSONResponse({"detail": "subscriptions/listen is not supported"}, 400)
        return await response(scope, receive, send)
    await _started()
    server = _SERVERS["write" if "write" in user["scopes"] else "read"]
    await server.session_manager.handle_request(scope, receive, send)
