"""GET /api/history?range=3d|w|m|y — per-day totals vs targets + streak (§7.4).

Ranges (local dates, settings.timezone; every day in range is present so the
charts draw a continuous series — unlogged days are zero with logged=false):

- 3d  today + 2 prior days, full per-day macro detail (per-meal subtotals)
- w   last 7 days     daily series
- m   last 30 days    daily series
- y   last 52 weeks   weekly averages over LOGGED days + the adherence strip
      (days_logged per week vs settings.cronometer_baseline_days_per_week, §9)

Streak = consecutive local days with ≥ 1 entry ending today, or ending
yesterday when today has nothing yet (the streak is alive until midnight).
Dashboard = SUM over entries.macros snapshots (§4 invariant).
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, time as time_type, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

import db
from auth import require_user
from models import MacroTargets
from routes.diary import load_targets, tz_name

router = APIRouter(dependencies=[Depends(require_user)])

Range = Literal["3d", "w", "m", "y"]
_DAYS = {"3d": 3, "w": 7, "m": 30}
_WEEKS = 52
_STREAK_SCAN_LIMIT = 3660


class MacroSums(BaseModel):
    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float


class DayStat(MacroSums):
    date: date_type
    entries: int
    logged: bool
    fiber_g: float | None = None  # None = no entry carried fiber (not measured)
    meals: dict[str, MacroSums] | None = None  # 3d only: per-meal subtotals


class WeekStat(BaseModel):
    week_start: date_type  # Monday
    days_logged: int
    adherence: float  # days_logged / 7
    avg: MacroSums | None = None  # mean over logged days; None when none


class HistoryResponse(BaseModel):
    range: Range
    timezone: str
    start: date_type
    end: date_type  # inclusive (today)
    targets: MacroTargets
    streak: int
    baseline_days_per_week: float
    days: list[DayStat]  # 3d / w / m
    weeks: list[WeekStat]  # y


_SUMS = """
       count(*)                                           AS n,
       COALESCE(SUM((macros->>'kcal')::numeric), 0)      AS kcal,
       COALESCE(SUM((macros->>'protein_g')::numeric), 0) AS protein_g,
       COALESCE(SUM((macros->>'carb_g')::numeric), 0)    AS carb_g,
       COALESCE(SUM((macros->>'fat_g')::numeric), 0)     AS fat_g,
       SUM((macros->>'fiber_g')::numeric)                AS fiber_g
"""


def _sums(r: dict) -> MacroSums:
    return MacroSums(
        kcal=float(r["kcal"]), protein_g=float(r["protein_g"]),
        carb_g=float(r["carb_g"]), fat_g=float(r["fat_g"]),
    )


async def _streak(tz: str, today: date_type) -> int:
    rows = await db.fetchall(
        "SELECT DISTINCT (logged_at AT TIME ZONE %s)::date AS day FROM entries "
        "ORDER BY day DESC LIMIT %s",
        (tz, _STREAK_SCAN_LIMIT),
    )
    days = {r["day"] for r in rows}
    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


@router.get("/api/history", response_model=HistoryResponse)
async def get_history(rng: Range = Query("w", alias="range")) -> HistoryResponse:
    tz = await tz_name()
    tzinfo = timezone.utc if tz == "UTC" else ZoneInfo(tz)
    today = datetime.now(timezone.utc).astimezone(tzinfo).date()
    if rng == "y":
        start = today - timedelta(days=today.weekday()) - timedelta(weeks=_WEEKS - 1)
    else:
        start = today - timedelta(days=_DAYS[rng] - 1)
    lo = datetime.combine(start, time_type.min, tzinfo=tzinfo)
    hi = datetime.combine(today + timedelta(days=1), time_type.min, tzinfo=tzinfo)

    by_meal = rng == "3d"
    rows = await db.fetchall(
        f"""
        SELECT (logged_at AT TIME ZONE %s)::date AS day,
               {"meal," if by_meal else ""}
               {_SUMS}
        FROM entries WHERE logged_at >= %s AND logged_at < %s
        GROUP BY 1{", 2" if by_meal else ""}
        ORDER BY 1
        """,
        (tz, lo, hi),
    )

    # fold rows into one DayStat per day (meal rows summed when by_meal)
    stats: dict[date_type, DayStat] = {}
    for r in rows:
        day = stats.get(r["day"])
        if day is None:
            day = stats[r["day"]] = DayStat(
                date=r["day"], entries=0, logged=True, kcal=0, protein_g=0,
                carb_g=0, fat_g=0, meals={} if by_meal else None,
            )
        day.entries += r["n"]
        for k in ("kcal", "protein_g", "carb_g", "fat_g"):
            setattr(day, k, getattr(day, k) + float(r[k]))
        if r["fiber_g"] is not None:
            day.fiber_g = (day.fiber_g or 0.0) + float(r["fiber_g"])
        if by_meal:
            day.meals[r["meal"]] = _sums(r)

    def day_or_zero(d: date_type) -> DayStat:
        return stats.get(d) or DayStat(
            date=d, entries=0, logged=False, kcal=0, protein_g=0, carb_g=0, fat_g=0
        )

    days: list[DayStat] = []
    weeks: list[WeekStat] = []
    if rng == "y":
        for i in range(_WEEKS):
            week_start = start + timedelta(weeks=i)
            logged = [
                stats[d]
                for d in (week_start + timedelta(days=j) for j in range(7))
                if d in stats
            ]
            n = len(logged)
            weeks.append(
                WeekStat(
                    week_start=week_start,
                    days_logged=n,
                    adherence=n / 7,
                    avg=MacroSums(
                        kcal=sum(d.kcal for d in logged) / n,
                        protein_g=sum(d.protein_g for d in logged) / n,
                        carb_g=sum(d.carb_g for d in logged) / n,
                        fat_g=sum(d.fat_g for d in logged) / n,
                    )
                    if n
                    else None,
                )
            )
    else:
        days = [day_or_zero(start + timedelta(days=i)) for i in range(_DAYS[rng])]

    brow = await db.fetchone(
        "SELECT value FROM settings WHERE key = 'cronometer_baseline_days_per_week'"
    )
    baseline = float(brow["value"]) if brow and isinstance(brow["value"], (int, float)) else 0.0
    return HistoryResponse(
        range=rng,
        timezone=tz,
        start=start,
        end=today,
        targets=await load_targets(),
        streak=await _streak(tz, today),
        baseline_days_per_week=baseline,
        days=days,
        weeks=weeks,
    )

