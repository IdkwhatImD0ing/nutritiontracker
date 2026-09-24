"""FastAPI app assembly (DESIGN.md §2, §5, §10).

Routers own their routes and apply auth.require_user router-wide. Two routes
are unauthenticated: /api/health, and /api/oura/callback — Oura's browser
redirect, which carries no bearer token and names its user in a signed
`state` (services/oura.py). Tracing (FireTrace, §10) initializes at
startup; each AI route records one trace, sent after its response has gone
out (tracing.FlushMiddleware holds the invocation open for it, bounded), and
shutdown drains whatever is still in flight. The demo quota's 429
(services/quota.py) gets its own handler so the body carries
{detail, resets_at, used, limit} — the shape frontend/lib/api.ts reads.
Agents reach the same handlers over MCP at POST /api/mcp (mcp_app.py), which
authenticates itself (auth.authenticate) and is imported on its first request.
"""

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

import tracing
from routes import (
    agent,
    aliases,
    commit,
    diary,
    entries,
    foods,
    history,
    keys,
    logs,
    me,
    meals,
    metrics,
    oura,
    photo,
    quick,
    resolve,
    settings,
    weights,
)
from services import quota


@asynccontextmanager
async def lifespan(app: FastAPI):
    tracing.init_tracing()
    yield
    # MCP's session managers run on a task the first /api/mcp request started
    # (mcp_app._started), if one came: close them while this loop still runs
    if "mcp_app" in sys.modules:
        await sys.modules["mcp_app"].stop()
    try:
        tracing.flush()  # queued commit scores, if any
        await tracing.drain()  # sends still in flight (bounded)
    except Exception:
        pass


# The schema is public at /api/openapi.json (under /api/, the backend's half of
# the origin) so the docs can link it; no Swagger/ReDoc pages. typegen calls
# app.openapi() directly, unaffected.
app = FastAPI(
    title="Tally API",
    lifespan=lifespan,
    openapi_url="/api/openapi.json",
    docs_url=None,
    redoc_url=None,
)
app.add_exception_handler(quota.QuotaExceeded, quota.exception_handler)
# §10: after each response has gone out, wait (bounded) for that request's
# trace sends — the client never waits, and a serverless freeze cannot lose one
app.add_middleware(tracing.FlushMiddleware)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "tally"}


app.include_router(logs.router)
app.include_router(resolve.router)
app.include_router(commit.router)
app.include_router(diary.router)
app.include_router(entries.router)
# M4 (§12): the + sheet's four lanes. quick (GET /api/foods/barcode) must
# precede foods, whose GET /api/foods/{food_id} would otherwise claim the
# path; photo owns /api/logs/photo (§3.4b).
app.include_router(quick.router)
app.include_router(photo.router)
app.include_router(foods.router)
# M3 (§12): saved meals, the alias sheet / Library list, History, Settings +
# the goal wizard, and the §9 /experiment series.
app.include_router(meals.router)
app.include_router(aliases.router)
app.include_router(history.router)
app.include_router(settings.router)
app.include_router(metrics.router)
# Open-sourcing (§8.9): who the caller is + the demo quota; the banner reads it.
app.include_router(me.router)
# Agent mode (§6.4, 2026-09-06): the weigh-in log behind the agent's log_weight
# tool and Today's weight card.
app.include_router(weights.router)
# The chat window's streaming front door (§6.4, 2026-09-06): the same claim and
# pipeline as POST /api/logs with agent=true, answered event by event.
app.include_router(agent.router)
# Oura (2026-09-21, migration 005): connect, sync, and the Settings card. The
# callback is Oura's browser redirect — no bearer token — so it has its own
# router without require_user; services/oura.py's signed `state` names the user.
app.include_router(oura.router)
app.include_router(oura.public)
# Personal API keys (migration 006): Settings mints and revokes them; a key
# then authenticates REST and MCP like a session (auth.py).
app.include_router(keys.router)


class _Mcp:
    """POST /api/mcp: mcp_app is imported on the first request, not here. The
    MCP SDK and the two servers took ~0.4 s of every cold start, MCP or not.
    A class instance, so Starlette hands it the raw ASGI call instead of
    wrapping it as a request handler."""

    async def __call__(self, scope, receive, send) -> None:
        import mcp_app  # lazy: see the docstring

        await mcp_app.handle(scope, receive, send)


# MCP (mcp_app.py): a raw ASGI route, not a mount (a mount answers POST
# /api/mcp with a 307). POST only: GET and DELETE get a 405, since a
# stateless GET would open an event stream that never ends.
app.add_route("/api/mcp", _Mcp(), methods=["POST"], include_in_schema=False)
