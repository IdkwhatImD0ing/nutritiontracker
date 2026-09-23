"""/api/oura — connect Oura and sync its activity (services/oura.py, migration 005).

Owner only (§8.9): GET answers anyone else `available: false` (no card), and
the routes that start or use the connection refuse them with 403.

- GET  /api/oura           → {available, connected, last_synced_at, missing, days}: the Settings card
- GET  /api/oura/authorize → {url}: where Connect (and Reconnect) sends the browser
- POST /api/oura/sync      → one sync; Today calls it on open (?force=true skips
                             the half-hour throttle — the card's Sync now)
- GET  /api/oura/callback  → Oura's redirect back. A browser navigation carries
                             no bearer token, so it sits on a router of its own
                             and the user comes from the signed `state`

Between a push and `apply.py 005` the tables do not exist: GET and POST answer
503 with why (the card shows it), and the callback lands on
/settings?oura=not_set_up — never a 500.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

import db
from auth import is_owner, require_user
from routes.diary import local_date
from services import oura

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_user)])
public = APIRouter()  # the callback alone — see the module docstring

STATUS_DAYS = 7  # how many days the Settings card lists
# The callback's sync is a full backfill (a scope granted on this connect gets
# its history); bound it well inside Vercel's 120 s so the redirect always
# happens. Tokens are saved first, so the connection stands either way.
FIRST_SYNC_TIMEOUT_S = 60


class OuraDay(BaseModel):
    day: date_type
    total_calories: int | None  # the whole day's burn, resting included
    active_calories: int | None
    steps: int | None


class OuraStatus(BaseModel):
    available: bool  # the owner, on a deployment with Oura configured
    connected: bool
    last_synced_at: datetime | None = None
    missing: list[str] = []  # scopes the stored grant lacks ("daily", "workout")
    days: list[OuraDay] = []  # the last STATUS_DAYS days, newest first


class OuraAuthorize(BaseModel):
    url: str


class OuraSync(BaseModel):
    skipped: bool  # the last sync was under half an hour ago
    days: int
    workouts: int
    last_synced_at: datetime | None
    missing: list[str] = []  # scopes Oura refused this time (403)


def _owner_only() -> None:
    if not is_owner():
        raise HTTPException(403, "Oura sync is only on the owner's account")


async def _today() -> date_type:
    return await local_date(datetime.now(timezone.utc))


@router.get("/api/oura", response_model=OuraStatus)
async def get_status() -> OuraStatus:
    if not (is_owner() and oura.configured()):
        return OuraStatus(available=False, connected=False)
    since = await _today() - timedelta(days=STATUS_DAYS)
    try:
        row = await db.fetchone("SELECT last_synced_at, scope FROM oura_tokens")
        days = await db.fetchall(
            "SELECT day, total_calories, active_calories, steps FROM oura_days "
            "WHERE day > %s ORDER BY day DESC",
            (since,),
        )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(503, oura.NOT_MIGRATED)
    return OuraStatus(
        available=True,
        connected=row is not None,
        last_synced_at=row["last_synced_at"] if row else None,
        missing=oura.missing_scopes(row["scope"]) if row else [],
        days=[OuraDay(**d) for d in days],
    )


@router.get("/api/oura/authorize", response_model=OuraAuthorize)
async def authorize(user: dict = Depends(require_user)) -> OuraAuthorize:
    _owner_only()
    try:
        return OuraAuthorize(url=oura.authorize_url(user["id"]))
    except oura.NotConfigured as e:
        raise HTTPException(503, str(e))


@router.post("/api/oura/sync", response_model=OuraSync)
async def post_sync(force: bool = Query(False)) -> OuraSync:
    _owner_only()
    try:
        result = await oura.sync(today=await _today(), force=force)
    except psycopg.errors.UndefinedTable:
        raise HTTPException(503, oura.NOT_MIGRATED)
    except oura.NotConfigured as e:
        raise HTTPException(503, str(e))
    except oura.NotConnected as e:
        raise HTTPException(409, str(e))
    except oura.OuraError as e:
        raise HTTPException(502, str(e))
    return OuraSync(**asdict(result))


@public.get("/api/oura/callback", include_in_schema=False)
async def callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Oura's redirect after its consent screen. Always sends the browser back
    to Settings, ?oura= saying how it went — never a JSON error page. Anyone can
    call this with anything, so every failure is caught here."""

    def back(outcome: str) -> RedirectResponse:
        return RedirectResponse(f"/settings?oura={outcome}", status_code=303)

    if error:
        return back("denied" if error == "access_denied" else "error")
    if not code or not state:
        return back("error")
    try:
        user_id = oura.read_state(state)
    except (ValueError, KeyError, TypeError, oura.NotConfigured):
        return back("error")
    db.current_user_id.set(user_id)  # the tenant every statement below runs as
    try:
        tokens = await oura.exchange_code(code)
        await oura.save_tokens(tokens)
    except psycopg.errors.UndefinedTable:
        return back("not_set_up")
    except oura.OuraError:
        return back("error")
    except Exception:
        log.exception("oura: callback failed after the state checked out")
        return back("error")
    outcome = "missing_scope" if oura.missing_scopes(tokens.get("scope")) else "connected"
    try:
        async with asyncio.timeout(FIRST_SYNC_TIMEOUT_S):
            result = await oura.sync(today=await _today(), force=True, backfill=True)
        if result.missing:
            outcome = "missing_scope"
    except Exception:  # connected all the same: later syncs keep the recent days
        # current, and Reconnect re-reads the whole period again
        log.warning("oura: the first sync after connecting failed", exc_info=True)
    return back(outcome)
