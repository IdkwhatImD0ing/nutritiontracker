"""Goal wizard math (DESIGN.md §7.4) — pure functions, no I/O.

kcal  = Mifflin-St Jeor BMR × activity multiplier ∓ the daily equivalent of
        the weekly weight-change rate (7700 kcal per kg of bodyweight),
        clamped to a sex-specific safe floor.
macros: protein by g/kg bodyweight (default 2.0 — a training user), a fat
        floor of 0.8 g/kg, carbs as the kcal remainder. Every number is a
        proposal the UI shows and lets the user edit before PUT /api/settings.

Conventions:
- ``weekly_rate_kg`` is a magnitude; the *direction* comes from goal weight vs
  current weight (goal below → deficit, above → surplus, equal/absent →
  maintenance). Its magnitude is clamped to MAX_WEEKLY_RATE_KG.
- Rounding is half-up to whole kcal / whole grams; carbs are derived from the
  *rounded* protein and fat so the numbers shown add up on screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

ACTIVITY_MULTIPLIERS: dict[str, float] = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}
SEXES = ("male", "female")

KCAL_PER_KG = 7700.0
LB_PER_KG = 2.2046226  # routes/weights.py's constant, for the notes
MAX_WEEKLY_RATE_KG = 1.0
KCAL_FLOOR: dict[str, float] = {"male": 1500.0, "female": 1200.0}

PROTEIN_G_PER_KG_DEFAULT = 2.0
FAT_G_PER_KG_FLOOR = 0.8

KCAL_PER_G = {"protein": 4.0, "carb": 4.0, "fat": 9.0}


def round_half_up(x: float) -> int:
    """Symmetric half-up rounding (Python's round() is banker's)."""
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def bmr_mifflin_st_jeor(weight_kg: float, height_cm: float, age: float, sex: str) -> float:
    if sex not in SEXES:
        raise ValueError(f"sex must be one of {SEXES}")
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    return base + 5.0 if sex == "male" else base - 161.0


def tdee(bmr: float, activity_level: str) -> float:
    try:
        return bmr * ACTIVITY_MULTIPLIERS[activity_level]
    except KeyError:
        raise ValueError(f"activity_level must be one of {tuple(ACTIVITY_MULTIPLIERS)}")


@dataclass
class KcalTarget:
    tdee: float
    daily_delta_kcal: float  # signed: negative = deficit
    kcal: int
    notes: list[str] = field(default_factory=list)


def kcal_target(
    tdee_kcal: float,
    weight_kg: float,
    sex: str,
    goal_weight_kg: float | None = None,
    weekly_rate_kg: float | None = None,
) -> KcalTarget:
    notes: list[str] = []
    rate = abs(weekly_rate_kg or 0.0)
    if rate > MAX_WEEKLY_RATE_KG:
        notes.append(
            f"Your weekly rate was capped at {MAX_WEEKLY_RATE_KG:g} kg "
            f"({MAX_WEEKLY_RATE_KG * LB_PER_KG:.1f} lb), the safe maximum."
        )
        rate = MAX_WEEKLY_RATE_KG
    if goal_weight_kg is None or goal_weight_kg == weight_kg or rate == 0.0:
        direction = 0.0
    else:
        direction = -1.0 if goal_weight_kg < weight_kg else 1.0
    delta = direction * rate * KCAL_PER_KG / 7.0
    raw = tdee_kcal + delta
    floor = KCAL_FLOOR[sex]
    if raw < floor:
        notes.append(f"Calories were raised to {floor:,.0f}, the safe minimum for you.")
        raw = floor
    return KcalTarget(tdee=tdee_kcal, daily_delta_kcal=delta, kcal=round_half_up(raw), notes=notes)


@dataclass
class MacroSplit:
    protein_g: int
    fat_g: int
    carb_g: int
    protein_g_per_kg: float
    fat_g_per_kg: float
    notes: list[str] = field(default_factory=list)


def macro_split(
    kcal: float,
    weight_kg: float,
    protein_g_per_kg: float | None = None,
    fat_g_per_kg: float | None = None,
) -> MacroSplit:
    p_per_kg = PROTEIN_G_PER_KG_DEFAULT if protein_g_per_kg is None else protein_g_per_kg
    f_per_kg = FAT_G_PER_KG_FLOOR if fat_g_per_kg is None else fat_g_per_kg
    notes: list[str] = []
    if f_per_kg < FAT_G_PER_KG_FLOOR:
        notes.append(
            f"Fat was raised to the minimum of {FAT_G_PER_KG_FLOOR:g} g per kg of bodyweight."
        )
        f_per_kg = FAT_G_PER_KG_FLOOR
    protein_g = round_half_up(p_per_kg * weight_kg)
    fat_g = round_half_up(f_per_kg * weight_kg)
    remainder = kcal - protein_g * KCAL_PER_G["protein"] - fat_g * KCAL_PER_G["fat"]
    if remainder < 0:
        notes.append(
            "Protein and fat already use all your calories, so carbs are 0 g. "
            "Lower protein or pick a slower rate."
        )
        remainder = 0.0
    carb_g = round_half_up(remainder / KCAL_PER_G["carb"])
    return MacroSplit(
        protein_g=protein_g,
        fat_g=fat_g,
        carb_g=carb_g,
        protein_g_per_kg=p_per_kg,
        fat_g_per_kg=f_per_kg,
        notes=notes,
    )


def compute_targets(
    *,
    height_cm: float,
    weight_kg: float,
    age: float,
    sex: str,
    activity_level: str,
    goal_weight_kg: float | None = None,
    weekly_rate_kg: float | None = None,
    protein_g_per_kg: float | None = None,
    fat_g_per_kg: float | None = None,
) -> dict:
    """The wizard in one call: inputs → proposed targets + the working."""
    bmr = bmr_mifflin_st_jeor(weight_kg, height_cm, age, sex)
    maint = tdee(bmr, activity_level)
    kt = kcal_target(maint, weight_kg, sex, goal_weight_kg, weekly_rate_kg)
    ms = macro_split(kt.kcal, weight_kg, protein_g_per_kg, fat_g_per_kg)
    return {
        "bmr": round_half_up(bmr),
        "tdee": round_half_up(maint),
        "daily_delta_kcal": round_half_up(kt.daily_delta_kcal),
        "targets": {
            "kcal": kt.kcal,
            "protein_g": ms.protein_g,
            "carb_g": ms.carb_g,
            "fat_g": ms.fat_g,
        },
        "protein_g_per_kg": ms.protein_g_per_kg,
        "fat_g_per_kg": ms.fat_g_per_kg,
        "notes": kt.notes + ms.notes,
    }
