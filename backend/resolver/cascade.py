"""The resolver cascade — M1 scope (DESIGN.md §3).

Implemented here: whole-utterance short-circuit (aliases only, §3.0), tier-1
exact alias match with §3.2's pick() and alternates, tier 2 (§3.3: 2a trigram
against alias_norm with an explicit similarity() threshold, 2b decompose-time
library_match, combination rules 1–5), portion resolution per §7.3 (spoken
qty > alias mode-of-10 > food mode > provider default, with the
count-without-unit "verify me" rule), the §3.4a state-question rule on the
tier-1/tier-2 paths, and the §3.1 meal-ref guard.

Clean seams, later phases:
  - tier 0 (meal-macro expansion) slots into short_circuit() before the alias
    check and into resolve_item() for meal_ref phrases (M3).

Unresolved items come back as status="searching" — the client kicks tier 3
(services.tier3.run_tier3) per §3.4.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

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

# §3.3 tier 2a — trigram candidates against his personal corpus only. The
# threshold lives IN THE QUERY as an explicit similarity() predicate: the `%%`
# operator (literal `%`, psycopg-escaped) reads the session-level
# similarity_threshold GUC (default 0.3), and SET does not survive PgBouncer
# transaction pooling — so `%%` serves only as the index-accelerated prefilter
# (its 0.3 default passes a superset of the 0.55 predicate).
_TIER2A_THRESHOLD = 0.55
_TIER2A_STRONG = 0.75

_TRGM_SQL = """
SELECT a.id AS alias_id, a.alias_norm, a.food_id, a.default_serving_id, a.default_qty,
       a.state_answer, a.pinned, a.use_count, a.last_used_at, a.last_explicit_at,
       a.rejected_count,
       f.display_name, f.brand, f.servings, f.default_serving_id AS food_default_serving_id,
       f.state, f.state_sensitive,
       similarity(a.alias_norm, %s) AS sim
FROM aliases a
JOIN foods f ON f.id = a.food_id
WHERE a.alias_norm %% %s
  AND similarity(a.alias_norm, %s) >= %s
ORDER BY sim DESC, a.use_count DESC
LIMIT 3
"""

# §3.3 tier 2b — the validated library_match food, shaped like an alias row
# (alias fields NULL) so the same staging/portion code path applies.
_LIB_FOOD_SQL = """
SELECT NULL AS alias_id, NULL AS alias_norm, f.id AS food_id,
       NULL AS default_serving_id, NULL AS default_qty, NULL AS state_answer,
       false AS pinned, 0 AS use_count, NULL AS last_used_at,
       NULL AS last_explicit_at, 0 AS rejected_count,
       f.display_name, f.brand, f.servings,
       f.default_serving_id AS food_default_serving_id,
       f.state, f.state_sensitive
FROM foods f
WHERE f.id = %s
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

# §3.4a phrase-level cooked/raw/dry intent — same deterministic term lists as
# services/tier3.py's phrase_state (kept in sync by hand; tier3 cannot be
# imported here without a cycle — unification into a shared module is an
# integrator cleanup).
_COOKED_RE = re.compile(
    r"\b(cooked|roasted|grilled|boiled|braised|broiled|baked|fried|sautéed|"
    r"sauteed|steamed|stewed|seared|smoked|poached|rotisserie|microwaved)\b",
    re.IGNORECASE,
)
_RAW_RE = re.compile(r"\b(raw|uncooked)\b", re.IGNORECASE)
_DRY_RE = re.compile(r"\b(dry|dried)\b", re.IGNORECASE)


def _phrase_state(phrase: str) -> str | None:
    """The user's cooked/raw/dry intent as expressed in the phrase, else None."""
    if _RAW_RE.search(phrase):
        return "raw"
    if _COOKED_RE.search(phrase):
        return "cooked"
    if _DRY_RE.search(phrase):
        return "dry"
    return None


_CORE_MACROS = ("kcal", "protein_g", "carb_g", "fat_g")


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


def _serving_options(servings: list[dict]) -> list[dict]:
    """foods.servings rows as wire ServingOption dicts (§7.1 portion sheet).

    Shared with services/tier3.py — every resolved StagedItem carries the
    food's full serving list so the portion sheet never needs a second fetch.
    """
    out: list[dict] = []
    for s in servings or []:
        sid = s.get("serving_id")
        if not sid:
            continue
        macros = {
            k: float(v)
            for k, v in (s.get("macros") or {}).items()
            if isinstance(v, (int, float))
        }
        out.append(
            {
                "serving_id": sid,
                "description": s.get("description") or sid,
                "grams": float(s["grams"])
                if isinstance(s.get("grams"), (int, float))
                else None,
                "macros": macros or None,
            }
        )
    return out


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
# tier-1 / tier-2 staging
# ---------------------------------------------------------------------------


@dataclass
class Resolution:
    staged: StagedItem
    alias_id: str | None  # which alias resolved this item
    resolve_ms: int
    unit: str | None = None  # spoken unit, carried to tier 3 (§7.3 rule 1)
    search_instead: bool = False  # §3.3 rule 3 "search instead" affordance
    # §3.4a: the user's cooked/raw/dry intent when known (parsed.state or the
    # phrase itself) — commit persists it to aliases.state_answer so the
    # question is asked at most once per phrase (seam: commit.py, W2).
    state_answer: str | None = None


def _alternate(row: dict) -> Alternate:
    return Alternate(
        food_id=str(row["food_id"]),
        label=(f"{row['brand']} " if row["brand"] else "") + row["display_name"],
    )


async def _stage_resolved(
    picked: dict,
    phrase: str,
    item_index: int,
    meal: Meal,
    qty: float | None,
    unit: str | None,
    state: str | None,
    t0: float,
    *,
    tier: int,
    confidence: str | None = None,  # None → high, downgraded to low on verify-me
    alternates: list[Alternate] | None = None,
    search_instead: bool = False,
) -> Resolution:
    """Stage one resolved item from an alias⋈food row (alias fields may be NULL
    for a tier-2b library match). Shared by tiers 1 and 2 — same portion
    resolution, dangling-serving guard, and §3.4a state question."""
    serving_id, final_qty, verify = await resolve_portion(picked, qty, unit)
    serving = _serving_by_id(picked["servings"], serving_id)
    if serving is None:  # dangling id never renders (§4)
        serving = _serving_by_id(picked["servings"], picked["food_default_serving_id"])
        if serving is None and picked["servings"]:
            serving = picked["servings"][0]
        serving_id = serving["serving_id"] if serving else serving_id
        verify = True

    # §3.4a (BACKLOG 13): ask only when the food is state-sensitive AND the
    # user's intent is unresolved (neither the parsed state, the phrase itself,
    # nor the entry's own parsed state disambiguates — "neither the parsed
    # state nor the entry name disambiguates", §3.4a verbatim) AND the alias
    # carries no persisted answer.
    intent = state or _phrase_state(phrase)
    state_question = bool(
        picked["state_sensitive"]
        and intent is None
        and picked["state_answer"] is None
        and picked["state"] is None
    )

    # §4 snapshot invariant, mirrored from tier3._staged_from_food: a cached
    # serving with partial core macros must render verify-me, never crash the
    # tier-1/2 staging path with a pydantic ValidationError (commit's
    # core-macros guard is what refuses it).
    macros = None
    if serving is not None:
        scaled = _scale_macros(serving.get("macros") or {}, final_qty)
        if all(k in scaled for k in _CORE_MACROS):
            macros = Macros(**scaled)
        else:
            verify = True

    final_confidence = "low" if verify else (confidence or "high")
    staged = StagedItem(
        item_index=item_index,
        phrase=phrase,
        status="resolved",
        tier=tier,
        food=FoodRef(
            id=str(picked["food_id"]), name=picked["display_name"], brand=picked["brand"]
        ),
        serving=ServingRef(
            id=serving_id, description=serving["description"] if serving else serving_id
        ),
        qty=final_qty,
        macros=macros,
        meal=meal,
        alternates=alternates,
        state_question=state_question or None,
        confidence=final_confidence,  # "low" = verify-me styling (§3.3)
        servings=_serving_options(picked["servings"]) or None,
        search_instead=search_instead or None,
    )
    return Resolution(
        staged=staged,
        alias_id=str(picked["alias_id"]) if picked["alias_id"] is not None else None,
        resolve_ms=int((time.monotonic() - t0) * 1000),
        search_instead=search_instead,
        state_answer=intent if picked["state_sensitive"] and intent else None,
    )


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
    others = sorted(
        (r for r in rows if r["alias_id"] != picked["alias_id"]),
        key=lambda r: (r["use_count"], r["last_used_at"]),
        reverse=True,
    )[:3]
    alternates = [_alternate(r) for r in others] or None
    return await _stage_resolved(
        picked, phrase, item_index, meal, qty, unit, state, t0,
        tier=1, alternates=alternates,
    )


# ---------------------------------------------------------------------------
# tier 2 — novel phrasing, known food (§3.3)
# ---------------------------------------------------------------------------


async def _library_food(library_match: str | None, index_ids: set[str] | None) -> dict | None:
    """§3.3 2b: the decompose-tagged library match, validated. An id outside the
    passed index (when supplied), a non-uuid, or an unknown food is discarded."""
    if not library_match:
        return None
    if index_ids is not None and library_match not in index_ids:
        return None
    try:
        UUID(library_match)
    except ValueError:
        return None
    return await db.fetchone(_LIB_FOOD_SQL, (library_match,))


def _tier2_alternates(
    cands: list[dict], exclude_food_id, limit: int = 3
) -> list[Alternate] | None:
    """Candidate rows as alternates chips, deduped by food, winner excluded."""
    seen = {str(exclude_food_id)}
    alts: list[Alternate] = []
    for c in cands:
        fid = str(c["food_id"])
        if fid in seen:
            continue
        seen.add(fid)
        alts.append(_alternate(c))
        if len(alts) == limit:
            break
    return alts or None


async def _stage_tier2(
    parsed: ParsedItem,
    key: str,
    item_index: int,
    meal: Meal,
    index_ids: set[str] | None,
    t0: float,
) -> Resolution | None:
    """§3.3 combination rules 1–5. Returns None on rule 5 (→ tier 3).

    Confidence styling: rule 1 (two independent matchers agreeing) is "high";
    rules 2–4 are "low" ("verify me") — §3.3 names only those two styles, and a
    lone or disputed matcher is never more certain than rule 2's verify-me.
    Rule 3 additionally sets Resolution.search_instead.
    """
    cands = await db.fetchall(_TRGM_SQL, (key, key, key, _TIER2A_THRESHOLD)) if key else []
    lib = await _library_food(parsed.library_match, index_ids)
    qty, unit, state = parsed.qty, parsed.unit, parsed.state
    phrase = parsed.phrase

    if lib is not None:
        agree = next(
            (c for c in cands if str(c["food_id"]) == str(lib["food_id"])), None
        )
        if agree is not None:
            # rule 1 — corroboration regardless of sim strength: resolve the
            # ALIAS row (it carries the alias defaults + state_answer).
            return await _stage_resolved(
                agree, phrase, item_index, meal, qty, unit, state, t0,
                tier=2, confidence="high",
                alternates=_tier2_alternates(cands, agree["food_id"]),
            )
        if cands:
            # rule 4 — disagreement: semantic beats lexical; 2a top is the chip.
            return await _stage_resolved(
                lib, phrase, item_index, meal, qty, unit, state, t0,
                tier=2, confidence="low",
                alternates=_tier2_alternates(cands[:1], lib["food_id"]),
            )
        # rule 2 — 2b alone: resolve it, "verify me".
        return await _stage_resolved(
            lib, phrase, item_index, meal, qty, unit, state, t0,
            tier=2, confidence="low",
        )

    if cands:
        top = cands[0]
        alternates = _tier2_alternates(cands[1:], top["food_id"])
        if float(top["sim"]) >= _TIER2A_STRONG:
            # rule 2 — 2a alone, strong: resolve it, "verify me".
            return await _stage_resolved(
                top, phrase, item_index, meal, qty, unit, state, t0,
                tier=2, confidence="low", alternates=alternates,
            )
        # rule 3 — only weak 2a: resolve top + alternates chip + "search
        # instead" affordance (Resolution.search_instead; the StagedItem wire
        # field is an integrator contract change — see workstream report).
        return await _stage_resolved(
            top, phrase, item_index, meal, qty, unit, state, t0,
            tier=2, confidence="low", alternates=alternates, search_instead=True,
        )

    return None  # rule 5 — neither matcher fired → tier 3


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


async def resolve_item(
    parsed: ParsedItem,
    item_index: int,
    meal: Meal,
    index_ids: set[str] | None = None,
) -> Resolution:
    """Per-item cascade after decomposition: tier 1 → tier 2 → tier-3 handoff.

    `index_ids` is the set of foods.id strings inlined into the decompose
    prompt (§3.3 2b) — library_match is validated against it when supplied;
    when None (current logs.py wiring), validation falls back to the food
    existing in the cache.
    """
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

    # tier 2 — novel phrasing, known food (§3.3 rules 1–5)
    tier2 = await _stage_tier2(parsed, key, item_index, meal, index_ids, t0)
    if tier2 is not None:
        return tier2

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
