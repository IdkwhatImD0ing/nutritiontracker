from datetime import date
from uuid import UUID

from fastapi import FastAPI, HTTPException

from models import (
    CommitRequest,
    DayTotals,
    LogCreate,
    LogResponse,
    ResolveRequest,
    StagedItem,
    Tier3KickRequest,
)

app = FastAPI(title="Usual API")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "usual"}


# ---------------------------------------------------------------------------
# §5 contract surface — stubs (HTTP 501) with the real request/response types,
# so the OpenAPI schema (and the generated frontend TS) is complete from day one.
# ---------------------------------------------------------------------------


@app.post("/api/logs", response_model=LogResponse)
def create_log(body: LogCreate):
    raise HTTPException(status_code=501, detail="not implemented")


@app.get("/api/logs/{log_id}", response_model=LogResponse)
def get_log(log_id: UUID):
    raise HTTPException(status_code=501, detail="not implemented")


@app.post("/api/logs/{log_id}/commit", response_model=DayTotals)
def commit_log(log_id: UUID, body: CommitRequest):
    raise HTTPException(status_code=501, detail="not implemented")


@app.post("/api/resolve", response_model=StagedItem)
def resolve_item(body: ResolveRequest):
    raise HTTPException(status_code=501, detail="not implemented")


@app.post("/api/resolve/tier3", response_model=StagedItem)
def kick_tier3(body: Tier3KickRequest):
    raise HTTPException(status_code=501, detail="not implemented")


@app.get("/api/diary", response_model=DayTotals)
def get_diary(date: date | None = None):
    raise HTTPException(status_code=501, detail="not implemented")
