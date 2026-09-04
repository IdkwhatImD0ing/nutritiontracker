"""/api/settings — the §4 settings rows behind the Settings page and the
first-run goal wizard (DESIGN.md §7.4).

- GET  /api/settings                → every user-facing key
- PUT  /api/settings                → partial update (only keys present in
                                      the body are written), range-validated
- POST /api/settings/goals/compute  → wizard inputs → proposed targets + the
                                      working; NOTHING is saved — the client
                                      shows/edits and then PUTs `targets`

Exposes an APIRouter; the integrator wires it into main.py. Response/request
models are route-local (the established pattern); `targets` reuses
models.MacroTargets so the diary and settings agree on the shape.
"""

from __future__ import annotations

import re
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from psycopg.types.json import Json
from pydantic import BaseModel, Field, field_validator, model_validator

import db
from auth import require_user
from models import MacroTargets
from services import goals

router = APIRouter(dependencies=[Depends(require_user)])

Sex = Literal["male", "female"]
ActivityLevel = Literal["sedentary", "light", "moderate", "active", "very_active"]

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class BodyProfile(BaseModel):
    """settings.body — every field optional so a partially filled wizard can be
    saved; the compute endpoint requires the full set (GoalInputs)."""

    height_cm: float | None = Field(default=None, ge=100, le=250)
    weight_kg: float | None = Field(default=None, ge=30, le=300)
    age: int | None = Field(default=None, ge=13, le=120)
    sex: Sex | None = None
    activity_level: ActivityLevel | None = None
    goal_weight_kg: float | None = Field(default=None, ge=30, le=300)
    weekly_rate_kg: float | None = Field(default=None, ge=0, le=goals.MAX_WEEKLY_RATE_KG)
    units: Literal["metric", "imperial"] | None = None  # the wizard's display preference


class TargetsIn(BaseModel):
    kcal: float = Field(ge=800, le=10000)
    protein_g: float = Field(ge=0, le=1000)
    carb_g: float = Field(ge=0, le=2000)
    fat_g: float = Field(ge=0, le=1000)


class MealWindow(BaseModel):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("must be HH:MM (24h)")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> MealWindow:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class MealWindows(BaseModel):
    breakfast: MealWindow
    lunch: MealWindow
    dinner: MealWindow
    default: Literal["breakfast", "lunch", "dinner", "snack"] = "snack"


def _valid_timezone(v: str) -> str:
    try:
        ZoneInfo(v)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        raise ValueError(f"unknown timezone {v!r}")
    return v


class SettingsUpdate(BaseModel):
    """PUT body: every field optional; only fields present are written."""

    body: BodyProfile | None = None
    targets: TargetsIn | None = None
    meal_windows: MealWindows | None = None
    profile_notes: str | None = Field(default=None, max_length=4000)
    timezone: str | None = None
    cronometer_baseline_days_per_week: int | None = Field(default=None, ge=0, le=7)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return None if v is None else _valid_timezone(v)


class SettingsResponse(BaseModel):
    body: BodyProfile
    targets: MacroTargets | None  # None until the wizard has run
    meal_windows: MealWindows
    profile_notes: str
    timezone: str
    cronometer_baseline_days_per_week: int


class GoalInputs(BaseModel):
    height_cm: float = Field(ge=100, le=250)
    weight_kg: float = Field(ge=30, le=300)
    age: int = Field(ge=13, le=120)
    sex: Sex
    activity_level: ActivityLevel
    goal_weight_kg: float | None = Field(default=None, ge=30, le=300)
    weekly_rate_kg: float | None = Field(default=None, ge=0)  # clamped, not rejected
    protein_g_per_kg: float | None = Field(default=None, gt=0, le=4)
    fat_g_per_kg: float | None = Field(default=None, gt=0, le=3)


class GoalProposal(BaseModel):
    bmr: int
    tdee: int
    daily_delta_kcal: int  # signed; negative = deficit
    targets: MacroTargets
    protein_g_per_kg: float
    fat_g_per_kg: float
    notes: list[str]  # clamps applied, for the UI to show


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_KEYS = (
    "body",
    "targets",
    "meal_windows",
    "profile_notes",
    "timezone",
    "cronometer_baseline_days_per_week",
)

_DEFAULT_WINDOWS = {
    "breakfast": {"start": "04:00", "end": "11:00"},
    "lunch": {"start": "11:00", "end": "15:30"},
    "dinner": {"start": "15:30", "end": "21:30"},
    "default": "snack",
}


async def _read_settings() -> SettingsResponse:
    rows = await db.fetchall("SELECT key, value FROM settings WHERE key = ANY(%s)", (list(_KEYS),))
    s = {r["key"]: r["value"] for r in rows}
    targets = s.get("targets")
    return SettingsResponse(
        body=BodyProfile.model_construct(**(s.get("body") or {})),
        targets=MacroTargets(**targets) if targets else None,
        meal_windows=MealWindows(**(s.get("meal_windows") or _DEFAULT_WINDOWS)),
        profile_notes=s.get("profile_notes") or "",
        timezone=s.get("timezone") or "UTC",
        cronometer_baseline_days_per_week=int(s.get("cronometer_baseline_days_per_week") or 0),
    )


@router.get("/api/settings", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    return await _read_settings()


@router.put("/api/settings", response_model=SettingsResponse)
async def put_settings(body: SettingsUpdate) -> SettingsResponse:
    updates = body.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    if updates:
        async with db.tx() as conn:
            for key, value in updates.items():
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                    (key, Json(value)),
                )
    return await _read_settings()


@router.post("/api/settings/goals/compute", response_model=GoalProposal)
async def compute_goals(body: GoalInputs) -> GoalProposal:
    out = goals.compute_targets(**body.model_dump())
    return GoalProposal(**out)
