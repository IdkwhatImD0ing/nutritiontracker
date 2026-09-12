"""The request's time budget (DESIGN.md §3.4, 2026-09-11).

vercel.json caps every function at `maxDuration: 120` s, and since the chat
agent resolves every row before it replies (Bill: "the agent should make the
list using its tools then I just approve"), one request can hold a speech
transcription, two or three model turns and several tier-3 lookups. Nothing
may be cut off by the platform mid-write, so each lane starts a clock and the
slow steps read how much of it is left: an inline tier-3 lookup takes at most
what remains minus a reserve for the agent's closing turn, and the agent run
itself stops before the platform would stop it. Work that does not fit is
left `searching` for the phone's own tier-3 kick — the fallback that already
existed.

ContextVars, so a clock started in a route is inherited by the tasks it
creates (the chat's run task, asyncio.gather's lookups) and never leaks
between requests.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

# 15 s under maxDuration: the response still has to be written after the
# last step, and a cold start is not on any clock this module can see
REQUEST_BUDGET_S = 105.0

_ends_at: ContextVar[float | None] = ContextVar("request_ends_at", default=None)
# the seconds one inline tier-3 run may take (services/tier3.run_tier3 reads
# it); None = no cap — the phone's own kick, a route with no clock
tier3_budget: ContextVar[float | None] = ContextVar("tier3_budget", default=None)


def start(budget: float = REQUEST_BUDGET_S) -> None:
    """Start this request's clock."""
    _ends_at.set(time.monotonic() + budget)


def start_if_unset(budget: float = REQUEST_BUDGET_S) -> None:
    """Start the clock unless the route that owns the request already has."""
    if _ends_at.get() is None:
        start(budget)


def left() -> float | None:
    """Seconds left on this request's clock; None when no clock was started."""
    ends = _ends_at.get()
    return None if ends is None else ends - time.monotonic()
