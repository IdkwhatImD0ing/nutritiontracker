"""Demo quota — what a non-owner account may spend per UTC day.

The owner (auth.is_owner) is unlimited. Every other account is a demo:

- ONE log ATTEMPT per day across every lane (POST /api/logs text + voice,
  /api/logs/photo, /api/logs/from-food). An attempt is recorded in
  quota_events (kind='log', migration 002) in the SAME transaction that
  claims the log's client id (routes/logs.claim_log) — before any STT,
  vision or decompose spend — so it stands whatever happens next: a capture
  failure, a vision or STT error that drops the itemless row
  (routes/logs._drop_empty_log), a log that decomposed to nothing. Counting
  logs rows instead would let the same client id re-run the pipeline for
  free: routes/logs._duplicate_post_response re-claims an itemless staged
  row after its grace period, and removing every item empties a row again.
  Free: a request the server answers from stored state — a replay of a row
  with live items, the 409 of a log that left 'staged', the 404 of an id
  that is not this tenant's — because the claim decides that before anything
  is recorded. The orphan re-run is the one spend without a fresh row; it
  records through check_log(). The Agent lane's clarifying question (§6.4)
  is the one attempt handed back: the turn ran the model but staged nothing,
  so its 'log' row becomes a 'question' row and the answer claims the day's
  attempt — at most DEMO_QUESTION_LIMIT times a day (refund_question), so
  ask → answer → ask is bounded model spend, not free; past the cap the
  attempt stands and the answer meets the 429.
- TEN model-spending re-resolutions per day (POST /api/resolve,
  /api/resolve/tier3, and the free-text branch of /api/resolve/answer),
  recorded the same way (kind='resolve'), after the request's own free
  refusals (an unknown or foreign item is 404 before the unit is spent; a
  tier-1 exact-alias hit on /api/resolve still costs one — BACKLOG 128).

Atomic by the database, not the app: the 'log' row is admitted by an insert
that 002 §5's partial unique index (one 'log' row per user per UTC day)
turns into rowcount 0 for the second of two concurrent attempts — never a
count followed by an insert. One clock: created_at defaults to now(), and
the day a row belongs to, today's count and resets_at are all computed from
that same now() — an app-side clock never judges the ledger's day.

Over quota -> 429 QuotaExceeded. main.py registers exception_handler so the
body is {detail, resets_at, used, limit} at the top level — the shape
lib/api.ts reads; unregistered, FastAPI's default handler still answers
{detail} with the same sentence. All counts run under RLS: the connection is
the tenant, so no query names a user. The checks read the request's user
from auth.current_user (set by require_user, or by a test's tenant binding)
so a handler needs no new parameter — tests call handlers directly. No user
bound at all counts as a demo (and the write would fail RLS anyway).
"""

from __future__ import annotations

from datetime import timezone

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import auth
import db

DEMO_LOG_LIMIT = 1  # what quota_events_log_day_key (002 §5) enforces
DEMO_RESOLVE_LIMIT = 10
DEMO_QUESTION_LIMIT = 3  # clarifying questions a day whose attempt is handed back


class QuotaExceeded(HTTPException):
    def __init__(self, detail: str, *, resets_at: str, used: int, limit: int) -> None:
        super().__init__(status_code=429, detail=detail)
        self.resets_at = resets_at
        self.used = used
        self.limit = limit


async def exception_handler(request: Request, exc: QuotaExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": exc.detail,
            "resets_at": exc.resets_at,
            "used": exc.used,
            "limit": exc.limit,
        },
    )


# The database's UTC day, judged by the database's clock — the same
# expression the ledger's unique index is built on.
_UTC_DAY = "(created_at AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date"
_STATUS_SQL = f"""
SELECT (SELECT count(*) FROM quota_events WHERE kind = %s AND {_UTC_DAY}) AS used,
       (((now() AT TIME ZONE 'UTC')::date + 1)::timestamp AT TIME ZONE 'UTC') AS resets_at
"""
# The log attempt: insert first; the partial unique index refuses a second
# 'log' row for the day, and DO NOTHING turns that into rowcount 0 (never an
# error, so the caller's transaction stays usable).
_ADMIT_LOG_SQL = """
INSERT INTO quota_events (kind) VALUES ('log')
ON CONFLICT (user_id, ((created_at AT TIME ZONE 'UTC')::date)) WHERE kind = 'log' DO NOTHING
"""
# The re-resolution: one statement, the count and the insert on the same clock.
_ADMIT_RESOLVE_SQL = f"""
INSERT INTO quota_events (kind)
SELECT 'resolve'
WHERE (SELECT count(*) FROM quota_events WHERE kind = 'resolve' AND {_UTC_DAY}) < %s
"""


def _refusal(row: dict, limit: int, detail: str) -> QuotaExceeded:
    at = row["resets_at"].astimezone(timezone.utc).isoformat()
    return QuotaExceeded(f"{detail} — resets at {at}", resets_at=at, used=int(row["used"]), limit=limit)


async def admit_log(conn) -> None:
    """Record today's log attempt for a demo inside the caller's transaction
    — the one that claims the log's client id (routes/logs.claim_log) — or
    raise QuotaExceeded, which rolls that transaction back with it. The owner
    records nothing."""
    if auth.is_owner():
        return
    if (await conn.execute(_ADMIT_LOG_SQL)).rowcount:
        return
    row = await (await conn.execute(_STATUS_SQL, ("log",))).fetchone()
    raise _refusal(row, DEMO_LOG_LIMIT, "Demo accounts can log once a day")


async def refund_question() -> None:
    """A demo turn that only asked a clarifying question (§6.4 — no row
    staged, nothing discarded, no weigh-in) ran the model but logged nothing:
    hand the day's log attempt back so the answer, the next turn, can claim
    it — by turning today's 'log' row into a 'question' row (the partial
    unique index only counts 'log'), at most DEMO_QUESTION_LIMIT times a day.
    The cap is what keeps ask → answer → ask from being unbounded model
    spend; past it the row stays 'log', the attempt stands, and the answer
    meets the 429. The turn could only have run if today's 'log' row was its
    own (an earlier log today would have refused the claim), so the row it
    converts is this turn's. The owner records nothing."""
    if auth.is_owner():
        return
    await db.execute(
        f"""
        UPDATE quota_events SET kind = 'question'
        WHERE kind = 'log' AND {_UTC_DAY}
          AND (SELECT count(*) FROM quota_events WHERE kind = 'question' AND {_UTC_DAY}) < %s
        """,
        (DEMO_QUESTION_LIMIT,),
    )


async def check_log() -> None:
    """admit_log in a transaction of its own: for the one spend that claims
    no fresh row — this tenant's orphaned staged log, re-run under its own
    id (routes/logs._duplicate_post_response)."""
    if auth.is_owner():
        return
    async with db.tx() as conn:
        await admit_log(conn)


async def check_resolve() -> None:
    """Raise QuotaExceeded when a demo account has spent its re-resolutions;
    otherwise record this one."""
    if auth.is_owner():
        return
    if await db.execute(_ADMIT_RESOLVE_SQL, (DEMO_RESOLVE_LIMIT,)):
        return
    row = await db.fetchone(_STATUS_SQL, ("resolve",))
    raise _refusal(
        row, DEMO_RESOLVE_LIMIT, f"Demo accounts can re-resolve {DEMO_RESOLVE_LIMIT} items a day"
    )


async def demo_status(user: dict) -> dict | None:
    """GET /api/me's `demo`: None for the owner, else today's log attempts
    and the next UTC midnight (ISO 8601) — the banner and the 429 show it."""
    if user["is_owner"]:
        return None
    row = await db.fetchone(_STATUS_SQL, ("log",))
    return {
        "limit": DEMO_LOG_LIMIT,
        "used_today": int(row["used"]),
        "resets_at": row["resets_at"].astimezone(timezone.utc).isoformat(),
    }
