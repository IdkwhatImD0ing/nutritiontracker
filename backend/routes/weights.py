"""/api/weights — the weigh-in log (DESIGN.md §4 `weights`, §5; migration 003).

One row per user per LOCAL day, stored in kg with the unit it was said in.
Saying a weight twice on one day replaces it — there is no second row to
untangle. record_weight() is the one writer: the agent lane's log_weight tool
(services/agent.py, wired in routes/logs.py) and PUT /api/weights both go
through it, and it keeps settings.body.weight_kg — the goal wizard's "current
weight" — in step whenever the weigh-in is the newest one.

- GET /api/weights?days=  → the newest entry + the last N days' entries, newest first
- PUT /api/weights        → {weight, unit, day?} manual entry / correction (upsert by day)

Tenancy is the database's (RLS on `weights`, 003) — nothing here names a user.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import db
from auth import require_user
from routes.diary import local_date

router = APIRouter(dependencies=[Depends(require_user)])

Unit = Literal["kg", "lb"]
Source = Literal["agent", "manual"]

LB_PER_KG = 2.2046226  # the same constant frontend/components/WizardMath.ts uses
# routes/settings.BodyProfile's weight_kg range — one rule for both writers
MIN_KG, MAX_KG = 30.0, 300.0
# Between a push and `apply.py 003` the table does not exist (ONBOARDING §2:
# migrations are applied by hand). The agent lane must keep staging food
# through that window, so a missing table reads as "no weigh-ins" and a
# write says why it could not happen — never a 500 on every agent log.
NOT_MIGRATED = "weight tracking is not set up on this database yet (migration 003)"


class WeightEntry(BaseModel):
    day: date_type
    weight_kg: float
    unit: Unit  # as said/entered — the client shows it back the same way
    source: Source
    log_id: UUID | None = None  # the agent utterance that logged it


class WeightsResponse(BaseModel):
    latest: WeightEntry | None
    entries: list[WeightEntry]  # newest first, within the requested window


class WeightPut(BaseModel):
    weight: float = Field(gt=0)
    unit: Unit
    day: date_type | None = None  # default: today in settings.timezone


def to_kg(weight: float, unit: Unit) -> float:
    return round(weight / LB_PER_KG if unit == "lb" else weight, 2)


def from_kg(kg: float, unit: Unit) -> float:
    return round(kg * LB_PER_KG if unit == "lb" else kg, 1)


def _entry(row: dict) -> WeightEntry:
    return WeightEntry(
        day=row["day"],
        weight_kg=float(row["weight_kg"]),
        unit=row["unit"],
        source=row["source"],
        log_id=row["log_id"],
    )


async def record_weight(
    *, weight: float, unit: Unit, day: date_type, source: Source, log_id: UUID | None = None
) -> WeightEntry:
    """Upsert the day's weigh-in. Raises ValueError outside the 30–300 kg range
    (the agent's tool turns that into a tool error the model can act on)."""
    kg = to_kg(weight, unit)
    if not MIN_KG <= kg <= MAX_KG:
        raise ValueError(
            f"{weight:g} {unit} is outside the {MIN_KG:g}–{MAX_KG:g} kg range this app accepts"
        )
    try:
        return await _upsert(kg, unit, day, source, log_id)
    except psycopg.errors.UndefinedTable:
        raise ValueError(NOT_MIGRATED)


async def _upsert(kg, unit, day, source, log_id) -> WeightEntry:
    async with db.tx() as conn:
        row = await (
            await conn.execute(
                """
                INSERT INTO weights (day, weight_kg, unit, source, log_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, day) DO UPDATE
                SET weight_kg = EXCLUDED.weight_kg, unit = EXCLUDED.unit,
                    source = EXCLUDED.source, log_id = EXCLUDED.log_id, updated_at = now()
                RETURNING day, weight_kg, unit, source, log_id
                """,
                (day, kg, unit, source, log_id),
            )
        ).fetchone()
        # The goal wizard's current weight follows the newest weigh-in (§7.4:
        # re-running the wizard starts from what the scale said last). A
        # back-dated entry behind a newer one changes nothing.
        await conn.execute(
            """
            UPDATE settings SET value = value || jsonb_build_object('weight_kg', %s::numeric)
            WHERE key = 'body'
              AND NOT EXISTS (SELECT 1 FROM weights w WHERE w.day > %s)
            """,
            (kg, day),
        )
    return _entry(row)


async def latest_entry(*, before: date_type | None = None) -> WeightEntry | None:
    """The newest weigh-in — or the newest one strictly before `before`."""
    try:
        row = await db.fetchone(
            "SELECT day, weight_kg, unit, source, log_id FROM weights "
            "WHERE (%s::date IS NULL OR day < %s) ORDER BY day DESC LIMIT 1",
            (before, before),
        )
    except psycopg.errors.UndefinedTable:
        return None  # 003 not applied yet: no weigh-ins, the agent lane goes on
    return _entry(row) if row else None


async def entry_for_log(log_id: UUID) -> WeightEntry | None:
    """The weigh-in an agent log recorded, for a same-id replay (§8.6) — None
    when it recorded none, or when a later log replaced that day's entry."""
    try:
        row = await db.fetchone(
            "SELECT day, weight_kg, unit, source, log_id FROM weights WHERE log_id = %s",
            (log_id,),
        )
    except psycopg.errors.UndefinedTable:
        return None
    return _entry(row) if row else None


@router.get("/api/weights", response_model=WeightsResponse)
async def get_weights(days: int = Query(90, ge=1, le=3660)) -> WeightsResponse:
    today = await local_date(datetime.now(timezone.utc))
    try:
        rows = await db.fetchall(
            "SELECT day, weight_kg, unit, source, log_id FROM weights "
            "WHERE day >= %s ORDER BY day DESC",
            (today - timedelta(days=days - 1),),
        )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(503, NOT_MIGRATED)  # the card renders nothing
    return WeightsResponse(latest=await latest_entry(), entries=[_entry(r) for r in rows])


@router.put("/api/weights", response_model=WeightEntry)
async def put_weight(body: WeightPut) -> WeightEntry:
    day = body.day or await local_date(datetime.now(timezone.utc))
    try:
        return await record_weight(weight=body.weight, unit=body.unit, day=day, source="manual")
    except ValueError as e:
        raise HTTPException(422, str(e))
