"""GET /api/diary?date= — entries grouped by meal + DayTotals vs targets (§5).

Also home of the shared day-totals helpers routes/commit.py reuses.
Dashboard = SUM over entries.macros snapshots — never a join back to foods for
numbers (§4 invariant).
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, time as time_type, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import db
from auth import require_user
from models import DayTotals, FoodRef, Macros, MacroTargets, Meal, ServingRef

router = APIRouter(dependencies=[Depends(require_user)])

_MEALS = ("breakfast", "lunch", "dinner", "snack")


class DiaryEntry(BaseModel):
    """Response-only shape (route-local; the wire contracts live in models.py)."""

    id: UUID
    meal: Meal
    food: FoodRef
    serving: ServingRef
    qty: float
    macros: Macros
    logged_at: datetime


class DiaryResponse(BaseModel):
    date: date_type
    meals: dict[str, list[DiaryEntry]]
    totals: DayTotals


async def _tzinfo() -> ZoneInfo | timezone:
    row = await db.fetchone("SELECT value FROM settings WHERE key = 'timezone'")
    try:
        return ZoneInfo(row["value"]) if row else timezone.utc
    except Exception:
        return timezone.utc


async def local_date(ts: datetime) -> date_type:
    return ts.astimezone(await _tzinfo()).date()


async def day_bounds(day: date_type) -> tuple[datetime, datetime]:
    tz = await _tzinfo()
    start = datetime.combine(day, time_type.min, tzinfo=tz)
    return start, start + timedelta(days=1)


async def compute_day_totals(day: date_type) -> DayTotals:
    start, end = await day_bounds(day)
    row = await db.fetchone(
        """
        SELECT COALESCE(SUM((macros->>'kcal')::numeric), 0)      AS kcal,
               COALESCE(SUM((macros->>'protein_g')::numeric), 0) AS protein_g,
               COALESCE(SUM((macros->>'carb_g')::numeric), 0)    AS carb_g,
               COALESCE(SUM((macros->>'fat_g')::numeric), 0)     AS fat_g
        FROM entries WHERE logged_at >= %s AND logged_at < %s
        """,
        (start, end),
    )
    trow = await db.fetchone("SELECT value FROM settings WHERE key = 'targets'")
    t = trow["value"] if trow and isinstance(trow["value"], dict) else {}
    return DayTotals(
        kcal=float(row["kcal"]),
        protein_g=float(row["protein_g"]),
        carb_g=float(row["carb_g"]),
        fat_g=float(row["fat_g"]),
        targets=MacroTargets(
            kcal=float(t.get("kcal") or 0),
            protein_g=float(t.get("protein_g") or 0),
            carb_g=float(t.get("carb_g") or 0),
            fat_g=float(t.get("fat_g") or 0),
        ),
    )


@router.get("/api/diary", response_model=DiaryResponse)
async def get_diary(date: date_type | None = None) -> DiaryResponse:
    if date is None:
        date = await local_date(datetime.now(timezone.utc))
    start, end = await day_bounds(date)
    rows = await db.fetchall(
        """
        SELECT e.id, e.meal, e.food_id, e.serving_id, e.qty, e.macros, e.logged_at,
               e.created_at, f.display_name, f.brand, f.servings
        FROM entries e JOIN foods f ON f.id = e.food_id
        WHERE e.logged_at >= %s AND e.logged_at < %s
        ORDER BY e.logged_at, e.created_at
        """,
        (start, end),
    )
    meals: dict[str, list[DiaryEntry]] = {m: [] for m in _MEALS}
    for r in rows:
        desc = next(
            (
                s.get("description", r["serving_id"])
                for s in r["servings"]
                if s.get("serving_id") == r["serving_id"]
            ),
            r["serving_id"],
        )
        meals.setdefault(r["meal"], []).append(
            DiaryEntry(
                id=r["id"],
                meal=Meal(r["meal"]),
                food=FoodRef(id=str(r["food_id"]), name=r["display_name"], brand=r["brand"]),
                serving=ServingRef(id=r["serving_id"], description=desc),
                qty=float(r["qty"]),
                macros=Macros(**r["macros"]),
                logged_at=r["logged_at"],
            )
        )
    return DiaryResponse(date=date, meals=meals, totals=await compute_day_totals(date))
