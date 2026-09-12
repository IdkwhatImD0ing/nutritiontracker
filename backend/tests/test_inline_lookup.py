"""Every row is looked up before the chat answers (DESIGN.md §3.4, 2026-09-11).

Bill: "the agent should make the list using its tools then I just approve" —
trace 30e72425 had the agent reply "I logged tonkatsu" over a row that was
still `searching`, and the phone started tier 3 afterwards. routes/logs.
_resolve_inline runs tier 3 inside the request instead, under the request's
clock (deadline.py; vercel.json maxDuration is 120 s):

1. the searching rows are looked up and put back by item_index; other rows
   are untouched;
2. a lookup the clock cannot fit is not started, one the quota refuses or
   that fails leaves its row searching (the phone's kick is the fallback);
3. at most _INLINE_PARALLEL run at once, each with the time the request can
   spare as its tier-3 budget;
4. off when TALLY_INLINE_TIER3=0 (the suite's default, conftest.py).

Offline: routes.resolve.kick_row is faked — no model, no database.

Run:  cd backend && uv run --with pytest pytest tests/test_inline_lookup.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import HTTPException  # noqa: E402

import deadline  # noqa: E402
from models import FoodRef, Meal, StagedItem  # noqa: E402
from routes import logs as logs_routes  # noqa: E402


def _patch_kick(monkeypatch, fake) -> None:
    """Fake the module _resolve_inline will import at call time (other test
    files re-import routes.resolve under their own stubs)."""
    import routes.resolve as rr

    monkeypatch.setattr(rr, "kick_row", fake)


def _row(idx: int, status: str = "searching") -> StagedItem:
    return StagedItem(item_index=idx, phrase=f"food {idx}", status=status, meal=Meal.lunch)


def _found(idx: int) -> StagedItem:
    return StagedItem(item_index=idx, phrase=f"food {idx}", status="resolved", tier=3,
                      food=FoodRef(id=str(uuid4()), name=f"found {idx}"), meal=Meal.lunch)


def test_rows_are_looked_up_in_place_and_failures_stay_searching(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
    budgets: dict[int, float | None] = {}

    async def kick_row(log_id, item_index, *, claim=True):
        assert claim is False  # the phone's two tries stay untouched
        budgets[item_index] = deadline.tier3_budget.get()
        if item_index == 2:
            raise HTTPException(status_code=429, detail="demo quota")
        if item_index == 3:
            raise RuntimeError("provider down")
        return _found(item_index)

    _patch_kick(monkeypatch, kick_row)

    async def scenario():
        deadline.start(100.0)
        rows = [_row(0, "resolved"), _row(1), _row(2), _row(3)]
        out = await logs_routes._resolve_inline(uuid4(), rows, reserve=20.0)
        assert [r.status for r in out] == ["resolved", "resolved", "searching", "searching"]
        assert out[0] is rows[0] and out[1].food.name == "found 1"
        assert out[2] is rows[2] and out[3] is rows[3]  # left for the phone's kick
        assert set(budgets) == {1, 2, 3}  # the resolved row was never looked up
        # the budget: the cap, since 100 s − the 20 s reserve is more
        assert budgets[1] == logs_routes._INLINE_CAP_S
        # no clock leaks out of a lookup's task
        assert deadline.tier3_budget.get() is None

    asyncio.run(scenario())


def test_no_lookup_starts_when_the_clock_cannot_fit_one(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
    calls: list[int] = []

    async def kick_row(log_id, item_index, *, claim=True):
        calls.append(item_index)
        return _found(item_index)

    _patch_kick(monkeypatch, kick_row)

    async def scenario():
        # 25 s left, 20 s reserved for the agent's closing turn: 5 s < the minimum
        deadline.start(25.0)
        rows = [_row(0), _row(1)]
        assert await logs_routes._resolve_inline(uuid4(), rows, reserve=20.0) == rows
        assert calls == []
        # with more time the budget shrinks to what the request can spare
        deadline.start(45.0)
        seen = []

        async def budgeted(log_id, item_index, *, claim=True):
            seen.append(deadline.tier3_budget.get())
            return _found(item_index)

        _patch_kick(monkeypatch, budgeted)
        await logs_routes._resolve_inline(uuid4(), [_row(0)], reserve=20.0)
        assert seen and 20.0 < seen[0] <= 25.0

    asyncio.run(scenario())


def test_at_most_four_lookups_run_at_once(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
    running = peak = 0

    async def kick_row(log_id, item_index, *, claim=True):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return _found(item_index)

    _patch_kick(monkeypatch, kick_row)

    async def scenario():
        deadline.start(100.0)
        rows = [_row(i) for i in range(7)]
        out = await logs_routes._resolve_inline(uuid4(), rows, reserve=20.0)
        assert all(r.status == "resolved" for r in out)
        assert [r.item_index for r in out] == list(range(7))
        assert peak == logs_routes._INLINE_PARALLEL

    asyncio.run(scenario())


def test_off_switch_leaves_every_row_as_it_is(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "0")

    async def kick_row(*a, **kw):
        raise AssertionError("inline lookups are off")

    _patch_kick(monkeypatch, kick_row)
    rows = [_row(0), _row(1)]
    assert asyncio.run(logs_routes._resolve_inline(uuid4(), rows, reserve=20.0)) == rows


def test_replace_rows_swaps_by_index_in_place():
    items = [_row(0), _row(1), _row(2, "resolved")]
    ref = items
    logs_routes._replace_rows(items, [_found(1)])
    assert ref is items and [r.status for r in items] == ["searching", "resolved", "resolved"]
    assert items[1].food.name == "found 1"
