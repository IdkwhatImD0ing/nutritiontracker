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


@router.post("/api/logs/{log_id}/commit", response_model=DayTotals)
async def commit_log(log_id: UUID, body: CommitRequest) -> DayTotals:
    try:
        result = await commit_tx.commit_log(log_id, body.items)
    except commit_tx.LogNotFound:
        raise HTTPException(404, "log not found")
    except commit_tx.LogNotCommittable as e:
        raise HTTPException(409, str(e))
    day = await local_date(result["log"]["captured_at"])
    return await compute_day_totals(day)


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
