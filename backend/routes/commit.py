"""POST /api/logs/{id}/commit and /commit-item (DESIGN.md §3.5, §5).

Both idempotent: CAS on logs.status + UNIQUE(log_id, item_index); retries
return the stored result. Returns the day's updated totals.
"""

from __future__ import annotations

from uuid import UUID

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_user
from models import CommitRequest, DayTotals, SaveIntent, StagedItem
from resolver import commit as commit_tx
from routes.diary import compute_day_totals, local_date

router = APIRouter(dependencies=[Depends(require_user)])


class CommitRequestIn(CommitRequest):
    """CommitRequest + the §3.5 step-4 / §8.4 "Save as meal" tap: the staging
    sheet names the meal and the commit creates/updates the macro from the
    items it writes (route-local, additive over models.py)."""

    save_intent: SaveIntent | None = None


class CommitItemRequest(BaseModel):
    """§5: {item_index, StagedItem} — the late-straggler append."""

    item_index: int
    item: StagedItem


class CommitItemError(BaseModel):
    """One core-macros refusal (§3.5): the item stayed uncommitted as
    needs_input; the rest of the log committed."""

    item_index: int
    error: str


class SavedMealNote(BaseModel):
    """A macro this request created/updated (§8.4) — enough for the client to
    confirm it ("Saved as 'usual breakfast' · 3 items")."""

    meal_id: UUID
    name: str
    item_count: int


class CommitResponse(DayTotals):
    """DayTotals + per-item refusals. A 200 with non-empty errors is a PARTIAL
    commit — the client must keep refused rows visible (verify-me / pending),
    never close over them (§3.5; the silent-protein-hole failure class).
    The totals are for the day the entries landed on (§7.2 when-shift aware)."""

    errors: list[CommitItemError] = []
    saved_meal: SavedMealNote | None = None
    # The diary day `totals` describes — null when this commit wrote to more
    # than one day, because then no single set of totals describes it and the
    # client must re-read the diary rather than paint. Without this the client
    # could not tell a back-dated commit from a same-day one and had to give up
    # painting entirely.
    totals_day: date | None = None


def _saved_meal(row: dict | None) -> SavedMealNote | None:
    if row is None:
        return None
    return SavedMealNote(
        meal_id=row["id"], name=row["name"], item_count=len(row["items"] or [])
    )


@router.post("/api/logs/{log_id}/commit", response_model=CommitResponse)
async def commit_log(log_id: UUID, body: CommitRequestIn) -> CommitResponse:
    # getattr: tests and in-process callers still hand in a bare CommitRequest
    save_intent = getattr(body, "save_intent", None)
    try:
        result = await commit_tx.commit_log(log_id, body.items, save_intent=save_intent)
    except commit_tx.LogNotFound:
        raise HTTPException(404, "log not found")
    except commit_tx.LogNotCommittable as e:
        raise HTTPException(409, str(e))
    except commit_tx.MealInvalid as e:
        raise HTTPException(422, str(e))
    day = await local_date(result["logged_at"])
    totals = await compute_day_totals(day)
    # More than one day written means `totals` describes only part of what just
    # happened — say so with a null day rather than let it be painted as the
    # whole picture.
    days = result.get("logged_at_days") or []
    return CommitResponse(
        **totals.model_dump(),
        errors=[CommitItemError(**e) for e in result.get("errors", [])],
        saved_meal=_saved_meal(result.get("meal")),
        totals_day=day if len(days) <= 1 else None,
    )


@router.post("/api/logs/{log_id}/commit-item", response_model=DayTotals)
async def commit_item(log_id: UUID, body: CommitItemRequest) -> DayTotals:
    item = body.item
    if item.item_index != body.item_index:
        item = item.model_copy(update={"item_index": body.item_index})
    try:
        result = await commit_tx.commit_item(log_id, item)
    except commit_tx.LogNotFound:
        raise HTTPException(404, "log not found")
    except commit_tx.LogNotCommittable as e:
        raise HTTPException(409, str(e))
    day = await local_date(result["logged_at"])
    return await compute_day_totals(day)
