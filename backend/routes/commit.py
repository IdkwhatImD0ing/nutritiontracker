"""POST /api/logs/{id}/commit and /commit-item (DESIGN.md §3.5, §5).

Both idempotent: CAS on logs.status + UNIQUE(log_id, item_index); retries
return the stored result. Returns the day's updated totals.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_user
from models import CommitRequest, DayTotals, StagedItem
from resolver import commit as commit_tx
from routes.diary import compute_day_totals, local_date

router = APIRouter(dependencies=[Depends(require_user)])


class CommitItemRequest(BaseModel):
    """§5: {item_index, StagedItem} — the late-straggler append."""

    item_index: int
    item: StagedItem


class CommitItemError(BaseModel):
    """One core-macros refusal (§3.5): the item stayed uncommitted as
    needs_input; the rest of the log committed."""

    item_index: int
    error: str


class CommitResponse(DayTotals):
    """DayTotals + per-item refusals. A 200 with non-empty errors is a PARTIAL
    commit — the client must keep refused rows visible (verify-me / pending),
    never close over them (§3.5; the silent-protein-hole failure class)."""

    errors: list[CommitItemError] = []


@router.post("/api/logs/{log_id}/commit", response_model=CommitResponse)
async def commit_log(log_id: UUID, body: CommitRequest) -> CommitResponse:
    try:
        result = await commit_tx.commit_log(log_id, body.items)
    except commit_tx.LogNotFound:
        raise HTTPException(404, "log not found")
    except commit_tx.LogNotCommittable as e:
        raise HTTPException(409, str(e))
    day = await local_date(result["log"]["captured_at"])
    totals = await compute_day_totals(day)
    return CommitResponse(
        **totals.model_dump(),
        errors=[CommitItemError(**e) for e in result.get("errors", [])],
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
    day = await local_date(result["log"]["captured_at"])
    return await compute_day_totals(day)
