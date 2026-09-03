"""GET /api/diary?date= — entries grouped by meal + DayTotals vs targets (§5).

Also home of the shared day/timezone/targets helpers routes/commit.py,
routes/entries.py and routes/history.py reuse.
Dashboard = SUM over entries.macros snapshots — never a join back to foods for
numbers (§4 invariant).

Wire hygiene: entries.macros carries the BACKLOG 28 ``base_*`` full-precision
sidecar (routes/entries.py); it is storage, not display, and is stripped here
so it never reaches the client. Web-transcribed foods carry their citation
(``web_source`` {url, title}) so the diary renders it as a visible, clickable
link (§3.4a).
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, time as time_type, timedelta, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import db
from auth import require_user
from models import DayTotals, FoodRef, Macros, MacroTargets, Meal, ServingRef, WebSource

router = APIRouter(dependencies=[Depends(require_user)])

_MEALS = ("breakfast", "lunch", "dinner", "snack")
_BASE_PREFIX = "base_"  # routes/entries.py sidecar keys — never on the wire


class DiaryEntry(BaseModel):
    """Response-only shape (route-local; the wire contracts live in models.py).
    The display fields default to None because routes/entries.py builds this
    model too (it has no source/web_source at hand)."""

    id: UUID
    meal: Meal
    food: FoodRef
    serving: ServingRef
    qty: float
    macros: Macros
    logged_at: datetime
    grams: float | None = None
    source: str | None = None  # 'fdc' | 'off' | 'web' — the source badge
    web_source: WebSource | None = None  # §3.4a citation, web foods only
    # §3.4a: a confirmed photo portion renders flagged like a web food — read
    # off the entry's item_resolutions row. An "unresolved" row never commits.
    estimated: Literal["none", "portion"] = "none"


class DiaryResponse(BaseModel):
    date: date_type
    meals: dict[str, list[DiaryEntry]]
    totals: DayTotals


def display_macros(stored: dict) -> dict:
    """The stored macros jsonb without the base_* sidecar (BACKLOG 28)."""
    return {k: v for k, v in stored.items() if not k.startswith(_BASE_PREFIX)}


def web_source_of(source: str, source_url: str | None, provenance: dict | None,
                  display_name: str) -> WebSource | None:
    """The citation block for a web-transcribed food (§3.4a), else None."""
    if source != "web" or not source_url:
        return None
    prov = provenance or {}
    title = prov.get("page_title") or next(
        (c["title"] for c in prov.get("citations") or [] if isinstance(c, dict) and c.get("title")),
        None,
    )
    return WebSource(url=source_url, title=title or display_name)


async def tz_name() -> str:
    row = await db.fetchone("SELECT value FROM settings WHERE key = 'timezone'")
    name = row["value"] if row and isinstance(row["value"], str) else "UTC"
    try:
        ZoneInfo(name)
    except Exception:
        return "UTC"
    return name


async def _tzinfo() -> ZoneInfo | timezone:
    name = await tz_name()
    return timezone.utc if name == "UTC" else ZoneInfo(name)


async def local_date(ts: datetime) -> date_type:
    return ts.astimezone(await _tzinfo()).date()


async def day_bounds(day: date_type) -> tuple[datetime, datetime]:
    tz = await _tzinfo()
    start = datetime.combine(day, time_type.min, tzinfo=tz)
    return start, start + timedelta(days=1)


async def load_targets() -> MacroTargets:
    trow = await db.fetchone("SELECT value FROM settings WHERE key = 'targets'")
    t = trow["value"] if trow and isinstance(trow["value"], dict) else {}
    return MacroTargets(
        kcal=float(t.get("kcal") or 0),
        protein_g=float(t.get("protein_g") or 0),
        carb_g=float(t.get("carb_g") or 0),
        fat_g=float(t.get("fat_g") or 0),
    )


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
    return DayTotals(
        kcal=float(row["kcal"]),
        protein_g=float(row["protein_g"]),
        carb_g=float(row["carb_g"]),
        fat_g=float(row["fat_g"]),
        targets=await load_targets(),
    )


@router.get("/api/diary", response_model=DiaryResponse)
async def get_diary(date: date_type | None = None) -> DiaryResponse:
    if date is None:
        date = await local_date(datetime.now(timezone.utc))
    start, end = await day_bounds(date)
    rows = await db.fetchall(
        """
        SELECT e.id, e.meal, e.food_id, e.serving_id, e.qty, e.grams, e.macros,
               e.logged_at, e.created_at,
               f.display_name, f.brand, f.servings, f.source, f.source_url, f.provenance,
               ir.staged->>'estimated' AS estimated
        FROM entries e JOIN foods f ON f.id = e.food_id
        LEFT JOIN item_resolutions ir
               ON ir.log_id = e.log_id AND ir.item_index = e.item_index
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
                macros=Macros(**display_macros(r["macros"])),
                logged_at=r["logged_at"],
                grams=float(r["grams"]) if r["grams"] is not None else None,
                source=r["source"],
                web_source=web_source_of(
                    r["source"], r["source_url"], r["provenance"], r["display_name"]
                ),
                estimated="portion" if r["estimated"] == "portion" else "none",
            )
        )
    return DiaryResponse(date=date, meals=meals, totals=await compute_day_totals(date))
