"""The resolver cascade — M0 scope (DESIGN.md §3).

Implemented here: whole-utterance short-circuit (aliases only for M0, §3.0),
tier-1 exact alias match with §3.2's pick() and alternates, portion resolution
per §7.3 (spoken qty > alias mode-of-10 > food mode > provider default, with
the count-without-unit "verify me" rule), and the §3.1 meal-ref guard.

Clean seams, later phases:
  - tier 0 (meal-macro expansion) slots into short_circuit() before the alias
    check and into resolve_item() for meal_ref phrases (M3).
  - tier 2 (trigram + decompose-time index match) slots into resolve_item()
    between the tier-1 miss and the tier-3 handoff (M1).

Unresolved items come back as status="searching" — the client kicks tier 3
(services.tier3.run_tier3) per §3.4.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import db
from models import (
    Alternate,
    FoodRef,
    Macros,
    Meal,
    ParsedItem,
    Question,
    ServingRef,
    StagedItem,
)
from resolver.norm import norm

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_ALIAS_SQL = """
SELECT a.id AS alias_id, a.alias_norm, a.food_id, a.default_serving_id, a.default_qty,
       a.state_answer, a.pinned, a.use_count, a.last_used_at, a.last_explicit_at,
       a.rejected_count,
       f.display_name, f.brand, f.servings, f.default_serving_id AS food_default_serving_id,
       f.state, f.state_sensitive
FROM aliases a
JOIN foods f ON f.id = a.food_id
WHERE a.alias_norm = %s
"""

# Words that denote a measure rather than a countable household unit (§7.3).
_MEASURE_WORDS = frozenset(
    {
        "g", "gram", "grams", "kg", "kilogram", "kilograms",
        "ml", "milliliter", "milliliters", "millilitre", "millilitres", "l", "liter", "litre",
        "oz", "ounce", "ounces", "fl", "lb", "lbs", "pound", "pounds",
        "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
        "serving", "servings", "portion", "portions",
    }
)

_GRAMS_PER = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.35, "ounce": 28.35, "ounces": 28.35,
    "lb": 453.6, "lbs": 453.6, "pound": 453.6, "pounds": 453.6,
}


def _scale_macros(macros: dict, qty: float) -> dict:
    return {
        k: round(float(v) * qty, 2)
        for k, v in macros.items()
        if isinstance(v, (int, float))
    }


def _is_count_serving(description: str) -> bool:
    """True when a serving describes a countable household unit ("1 large",
    "1 bottle (414 ml)"), not a bare mass/volume measure ("100 g")."""
    main = re.sub(r"\(.*?\)", " ", description.lower())
    tokens = re.findall(r"[a-z]+", main)
    return any(t not in _MEASURE_WORDS for t in tokens)


def _serving_by_id(servings: list[dict], serving_id: str | None) -> dict | None:
    for s in servings:
        if s.get("serving_id") == serving_id:
            return s
    return None


def _unit_stem(unit: str) -> str:
    u = unit.strip().lower()
    return u[:-1] if u.endswith("s") and len(u) > 2 else u


# ---------------------------------------------------------------------------
# §3.2 pick() — the "usual" selection
# ---------------------------------------------------------------------------


async def pick(rows: list[dict]) -> dict:
    """Recency-and-intent aware selection over the alias rows for one key.

    1. pinned wins
    2. else most recent last_explicit_at within 90 days
    3. else mode of the last 10 committed choices for this alias_norm
       (item_resolutions), tie → most recent
    4. else use_count DESC, last_used_at DESC
    """
    if len(rows) == 1:
        return rows[0]

    pinned = [r for r in rows if r["pinned"]]
    if pinned:
        return max(pinned, key=lambda r: r["last_used_at"])

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    explicit = [
        r for r in rows if r["last_explicit_at"] is not None and r["last_explicit_at"] >= cutoff
    ]
    if explicit:
        return max(explicit, key=lambda r: r["last_explicit_at"])

    hist = await db.fetchall(
        """
        SELECT final_food_id FROM item_resolutions
        WHERE phrase_norm = %s AND committed_at IS NOT NULL AND final_food_id IS NOT NULL
        ORDER BY committed_at DESC LIMIT 10
        """,
        (rows[0]["alias_norm"],),
    )
    if hist:
        counts = Counter(h["final_food_id"] for h in hist)
        top = max(counts.values())
        tied = {fid for fid, c in counts.items() if c == top}
        for h in hist:  # DESC by committed_at → first tied hit is the most recent
            if h["final_food_id"] in tied:
                row = next((r for r in rows if r["food_id"] == h["final_food_id"]), None)
                if row is not None:
                    return row
                break  # mode food has no alias row — fall through to rule 4

    return max(rows, key=lambda r: (r["use_count"], r["last_used_at"]))


# ---------------------------------------------------------------------------
# §7.3 portion resolution
# ---------------------------------------------------------------------------


async def resolve_portion(
    row: dict, qty: float | None, unit: str | None
) -> tuple[str, float, bool]:
    """Resolve (serving_id, qty, verify_me) for a picked alias row (alias ⋈ food).

    Order: spoken quantity always wins (with the count-without-unit rule) →
    alias mode-of-last-10 → food's most-logged portion → provider default × 1.
    """
    servings: list[dict] = row["servings"]
    default_id: str = row["food_default_serving_id"]

    # 1. spoken quantity
    if qty is not None:
        if unit:
            stem = _unit_stem(unit)
            if stem in _GRAMS_PER:
                grams = qty * _GRAMS_PER[stem]
                gram_servings = [
                    s for s in servings if isinstance(s.get("grams"), (int, float)) and s["grams"] > 0
                ]
                if gram_servings:
                    base = next((s for s in gram_servings if s["grams"] == 100), None) or max(
                        gram_servings, key=lambda s: not _is_count_serving(s.get("description", ""))
                    )
                    return base["serving_id"], round(grams / float(base["grams"]), 4), False
                return default_id, qty, True  # no gram-based serving to map onto
            # fuzzy: unit word appears in a serving description
            for s in servings:
                tokens = re.findall(r"[a-z]+", s.get("description", "").lower())
                if stem in tokens or stem + "s" in tokens:
                    return s["serving_id"], qty, False
            return default_id, qty, True  # unit didn't map — verify me
        # qty + no unit ("two eggs"): prefer a count-type household serving
        count_servings = [s for s in servings if _is_count_serving(s.get("description", ""))]
        if count_servings:
            base = _serving_by_id(count_servings, default_id) or count_servings[0]
            return base["serving_id"], qty, False
        # only mass servings: never silently multiply the mass default (§7.3)
        return default_id, qty, True

    # 2. alias default: mode of the last 10 committed portions for this alias
    if row.get("alias_id") is not None:
        mode = await _portion_mode(
            "alias_id = %s", (row["alias_id"],), servings
        )
        if mode:
            return mode[0], mode[1], False
        if row.get("default_serving_id") and _serving_by_id(servings, row["default_serving_id"]):
            return row["default_serving_id"], float(row["default_qty"] or 1), False

    # 3. the food's most-logged portion across all aliases
    mode = await _portion_mode("final_food_id = %s", (row["food_id"],), servings)
    if mode:
        return mode[0], mode[1], False

    # 4. provider default × 1
    return default_id, 1.0, False


async def _portion_mode(
    where: str, params: tuple, servings: list[dict]
) -> tuple[str, float] | None:
    hist = await db.fetchall(
        f"""
        SELECT final_serving_id, final_qty FROM item_resolutions
        WHERE {where} AND committed_at IS NOT NULL AND final_serving_id IS NOT NULL
        ORDER BY committed_at DESC LIMIT 10
        """,
        params,
    )
    pairs = [
        (h["final_serving_id"], float(h["final_qty"]))
        for h in hist
        if h["final_qty"] is not None
    ]
    if not pairs:
        return None
    counts = Counter(pairs)
    top = max(counts.values())
    tied = {p for p, c in counts.items() if c == top}
    for p in pairs:  # DESC by committed_at → tie goes to the most recent
        if p in tied:
            if _serving_by_id(servings, p[0]):  # render-time validation (§4)
                return p
            break
    return None


# ---------------------------------------------------------------------------
# tier-1 staging
# ---------------------------------------------------------------------------


@dataclass
class Resolution:
    staged: StagedItem
    alias_id: str | None  # which alias resolved this item
    resolve_ms: int
    unit: str | None = None  # spoken unit, carried to tier 3 (§7.3 rule 1)


async def _stage_tier1(
    rows: list[dict],
    phrase: str,
    item_index: int,
    meal: Meal,
    qty: float | None,
    unit: str | None,
    state: str | None,
    t0: float,
) -> Resolution:
    picked = await pick(rows)
    serving_id, final_qty, verify = await resolve_portion(picked, qty, unit)
    serving = _serving_by_id(picked["servings"], serving_id)
    if serving is None:  # dangling id never renders (§4)
        serving = _serving_by_id(picked["servings"], picked["food_default_serving_id"])
        if serving is None and picked["servings"]:
            serving = picked["servings"][0]
        serving_id = serving["serving_id"] if serving else serving_id
        verify = True

    others = sorted(
        (r for r in rows if r["alias_id"] != picked["alias_id"]),
        key=lambda r: (r["use_count"], r["last_used_at"]),
        reverse=True,
    )[:3]
    alternates = [
        Alternate(
            food_id=str(r["food_id"]),
            label=(f"{r['brand']} " if r["brand"] else "") + r["display_name"],
        )
        for r in others
    ] or None

    state_question = bool(
        picked["state_sensitive"]
        and picked["state"] is None
        and state is None
        and picked["state_answer"] is None
    )

    staged = StagedItem(
        item_index=item_index,
        phrase=phrase,
        status="resolved",
        tier=1,
        food=FoodRef(
            id=str(picked["food_id"]), name=picked["display_name"], brand=picked["brand"]
        ),
        serving=ServingRef(
            id=serving_id, description=serving["description"] if serving else serving_id
        ),
        qty=final_qty,
        macros=Macros(**_scale_macros(serving["macros"], final_qty)) if serving else None,
        meal=meal,
        alternates=alternates,
        state_question=state_question or None,
        confidence="low" if verify else "high",  # "low" = verify-me styling
    )
    return Resolution(
        staged=staged,
        alias_id=str(picked["alias_id"]),
        resolve_ms=int((time.monotonic() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


async def short_circuit(raw_input: str, meal: Meal) -> Resolution | None:
    """§3.0 whole-utterance short-circuit. M0: aliases only (tier 1).

    Seam: the tier-0 meal_macros.trigger_phrases check lands here first (M3).
    """
    t0 = time.monotonic()
    key = norm(raw_input)
    if not key:
        return None
    rows = await db.fetchall(_ALIAS_SQL, (key,))
    if not rows:
        return None
    return await _stage_tier1(rows, raw_input, 0, meal, None, None, None, t0)


async def resolve_item(parsed: ParsedItem, item_index: int, meal: Meal) -> Resolution:
    """Per-item cascade after decomposition. M0: tier 1, else tier-3 handoff."""
    t0 = time.monotonic()

    # §3.1 guard: a meal-shaped phrase with no matching macro never enters food
    # search. M0 has no macros at all, so every meal_ref becomes needs_input.
    if parsed.phrase_kind == "meal_ref":
        return Resolution(
            staged=StagedItem(
                item_index=item_index,
                phrase=parsed.phrase,
                status="needs_input",
                meal=meal,
                question=Question(
                    text=f"No saved meal called '{parsed.phrase}' — log the items, "
                    "then tap Save as meal.",
                    options=[],
                ),
            ),
            alias_id=None,
            resolve_ms=int((time.monotonic() - t0) * 1000),
        )

    key = norm(parsed.phrase)

    # tier 1 — exact alias match (§3.2)
    rows = await db.fetchall(_ALIAS_SQL, (key,))
    if rows:
        return await _stage_tier1(
            rows, parsed.phrase, item_index, meal, parsed.qty, parsed.unit, parsed.state, t0
        )

    # Seam: tier 2 (2a trigram vs alias_norm + 2b library_match validation) — M1.

    # tier 3 — cold start: staged as searching, client kicks /api/resolve/tier3.
    # Spoken qty/unit ride along (§7.3 rule 1: spoken quantity always wins) —
    # tier 3 applies them against the resolved food's servings.
    return Resolution(
        staged=StagedItem(
            item_index=item_index,
            phrase=parsed.phrase,
            status="searching",
            meal=meal,
            qty=parsed.qty,
        ),
        alias_id=None,
        resolve_ms=int((time.monotonic() - t0) * 1000),
        unit=parsed.unit,
    )
