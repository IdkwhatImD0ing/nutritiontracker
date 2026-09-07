"""The resolver cascade (DESIGN.md §3).

Implemented here: whole-utterance short-circuit (§3.0: meal macro first, then
aliases — short_circuit_all), tier 0 meal-macro expansion (§3.1: one
StagedItem per macro item, stored portions, no search/LLM/portion inference),
tier-1 exact alias match with §3.2's pick() and alternates, tier 2 (§3.3: 2a
trigram against alias_norm with an explicit similarity() threshold, 2b
decompose-time library_match, combination rules 1–5), portion resolution per
§7.3 (spoken qty > alias mode-of-10 > food mode > provider default, with the
count-without-unit "verify me" rule), the §3.4a state-question rule on the
tier-1/tier-2 paths, and the §3.1 meal-ref guard.

Entry points: short_circuit_all() + resolve_items() are the list-returning
per-log paths (a tier-0 expansion occupies one item_index per macro item);
short_circuit() (aliases only) and resolve_item() (one Resolution) remain for
single-row callers such as /api/resolve.

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
from resolver.commit import LEARNABLE_ROWS_SQL
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
# (alias fields NULL) so the same staging/portion code path applies. Never
# the photo lane's numberless placeholder (R1: a source='estimate' row is not
# a pick target on any lane — routes/logs.py passes no index_ids, so without
# this "exists in the cache" would be the whole validation).
_LIB_FOOD_SQL = """
SELECT NULL AS alias_id, NULL AS alias_norm, f.id AS food_id,
       NULL AS default_serving_id, NULL AS default_qty, NULL AS state_answer,
       false AS pinned, 0 AS use_count, NULL AS last_used_at,
       NULL AS last_explicit_at, 0 AS rejected_count,
       f.display_name, f.brand, f.servings,
       f.default_serving_id AS food_default_serving_id,
       f.state, f.state_sensitive
FROM foods f
WHERE f.id = %s AND f.source <> 'estimate'
"""

# Words that denote a measure rather than a countable household unit (§7.3) —
# including the dimensions, labels and provider unit codes a serving text may
# lead with ("6 inch sub", "100 calorie pack", "8 FLOZ"), whose leading number
# is not a piece count either.
_MEASURE_WORDS = frozenset(
    {
        "g", "gram", "grams", "grm", "kg", "kilogram", "kilograms", "mg", "milligram", "milligrams",
        "ml", "milliliter", "milliliters", "millilitre", "millilitres", "mlt",
        "l", "liter", "liters", "litre", "litres",
        "oz", "ounce", "ounces", "oza", "fl", "floz", "lb", "lbs", "pound", "pounds",
        "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
        "serving", "servings", "portion", "portions",
        "inch", "inches", "cm", "mm", "calorie", "calories", "cal", "kcal", "percent",
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


_LEADING_PIECES_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+([a-z]+)")


def _pieces_per(description: str) -> float:
    """How many pieces one count-type serving holds: the leading number of
    its description when the word after it is not a measure, a dimension or
    a label ("4 large" → 4, "2 slices" → 2, "1 large" → 1, "large" → 1;
    "12 fl oz can" → 1, the 12 measures ounces, not cans; "6 inch sub" → 1,
    "100 calorie pack" → 1). A spoken count of pieces is divided by it —
    FDC's cooked shrimp serving is "4 large", so 50 shrimp are 12.5 servings,
    not 50 (BACKLOG 151; Bill, 2026-09-06: never assume a one-piece serving)."""
    m = _LEADING_PIECES_RE.match((description or "").lower())
    if not m or m.group(2) in _MEASURE_WORDS:
        return 1.0
    n = float(m.group(1))
    return n if n > 0 else 1.0


def _units_per(description: str, stem: str) -> float:
    """How many of the SPOKEN unit one serving holds, for the unit-word match:
    when the word after the leading number is that unit itself — measure or
    not — the number counts it ("1 tbsp" of a "2 tbsp (32 g)" serving is half
    a serving, "1 cup" of "2 cups (85 g)" too); otherwise the serving's piece
    count ("two scoops" of "2 scoops (64 g)" → 2, "1 can" of "12 fl oz can" → 1)."""
    m = _LEADING_PIECES_RE.match((description or "").lower())
    if m and _unit_stem(m.group(2)) == stem and float(m.group(1)) > 0:
        return float(m.group(1))
    return _pieces_per(description)


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

    # rule 3 counts what he SAID and committed — never a photo/barcode/search
    # row whose phrase happens to norm() to this key (commit.LEARNABLE_ROWS_SQL)
    hist = await db.fetchall(
        f"""
        SELECT final_food_id FROM item_resolutions
        WHERE phrase_norm = %s AND committed_at IS NOT NULL AND final_food_id IS NOT NULL
          {LEARNABLE_ROWS_SQL}
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
            # fuzzy: unit word appears in a serving description — divided by
            # how many of that unit the serving holds ("two scoops" of a "2
            # scoops" serving, "1 tbsp" of a "2 tbsp" serving: §7.3, never a
            # one-unit assumption)
            for s in servings:
                desc = s.get("description", "")
                tokens = re.findall(r"[a-z]+", desc.lower())
                if stem in tokens or stem + "s" in tokens:
                    return s["serving_id"], qty / _units_per(desc, stem), False
            return default_id, qty, True  # unit didn't map — verify me
        # qty + no unit ("two eggs", "50 shrimp"): prefer a count-type
        # household serving, divided by the pieces it holds ("4 large")
        count_servings = [s for s in servings if _is_count_serving(s.get("description", ""))]
        if count_servings:
            base = _serving_by_id(count_servings, default_id) or count_servings[0]
            return base["serving_id"], qty / _pieces_per(base.get("description", "")), False
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
    """Mode of the last 10 committed portions matching `where` — spoken logs
    only, never an estimated row (commit.LEARNABLE_ROWS_SQL): a photo-guessed
    1.5 × 100 g or a barcode pick must not become the next log's default."""
    hist = await db.fetchall(
        f"""
        SELECT final_serving_id, final_qty FROM item_resolutions
        WHERE {where} AND committed_at IS NOT NULL AND final_serving_id IS NOT NULL
          {LEARNABLE_ROWS_SQL}
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
# tier 0 — meal macro expansion (§3.1)
# ---------------------------------------------------------------------------

# trigger_phrases are stored post-norm() (the meals API applies norm() on
# write), so the match is plain equality against norm()'d keys. §6.1 shows the
# decompose context saved-meal NAMES, so a meal_ref can arrive as a name the
# triggers no longer carry (the Library can edit triggers away from
# norm(name)) — norm(name) is matched too, but only after every trigger match.
# Two active macros matching the same way: the most recently saved one wins.
# The scan is the whole active-macro set (single-user: a handful of rows) so
# both matches cost the one round-trip §3.0 budgets.
_MACRO_SQL = """
SELECT id, name, items, trigger_phrases FROM meal_macros
WHERE is_active
ORDER BY updated_at DESC
"""

# A saved meal pointing at a source='estimate' placeholder (it has no numbers
# to log — R1) expands as a missing food: MEAL_BROKEN_GUARD, fix it in Library.
_MACRO_FOODS_SQL = """
SELECT id AS food_id, display_name, brand, servings,
       default_serving_id AS food_default_serving_id
FROM foods
WHERE id = ANY(%s::uuid[]) AND source <> 'estimate'
"""

MEAL_REF_GUARD = "No saved meal called '{phrase}' — log the items, then tap Save as meal."

# §3.1: the saved meal matched, but stored items point at foods rows that are
# gone. Never silently log the remainder, and never claim the meal is missing.
MEAL_BROKEN_GUARD = (
    "Saved meal '{name}' is missing {missing} of its {total} foods — "
    "fix it in Library, then log it again."
)


async def find_macro(keys: list[str]) -> dict | None:
    """The active macro whose trigger_phrases — failing that, whose norm()'d
    name — contain any of `keys` (already norm()'d), else None."""
    keyset = {k for k in keys if k}
    if not keyset:
        return None
    by_name: dict | None = None
    for row in await db.fetchall(_MACRO_SQL):  # updated_at DESC
        if keyset & set(row["trigger_phrases"] or ()):
            return row
        if by_name is None and norm(row["name"]) in keyset:
            by_name = row
    return by_name


def _macro_keys(parsed: ParsedItem) -> list[str]:
    # norm(phrase) for every item (a trigger said verbatim needs no meal_ref
    # tag); decompose's meal_ref (the saved meal's name) as a second key.
    # A macro is a meal reference, not a food (§3.1): a spoken quantity on a
    # food-tagged phrase ("two eggs") is about the food, and the macro stores
    # its own portions, so tier 0 stands aside and tiers 1–3 take the item.
    if parsed.phrase_kind == "food" and parsed.qty is not None:
        return []
    keys = [norm(parsed.phrase)]
    if parsed.meal_ref:
        keys.append(norm(parsed.meal_ref))
    return keys


def _stage_tier0_item(
    food: dict, item: dict, item_index: int, meal: Meal, t0: float
) -> Resolution:
    """One expanded macro item as a resolved StagedItem: the stored
    {serving_id, qty} verbatim — no portion inference — with the §4
    dangling-serving and partial-core-macros guards (verify-me, never a
    zero-macro row). phrase = the food's display name (§5)."""
    servings: list[dict] = food["servings"]
    serving_id = item.get("serving_id")
    qty = float(item.get("qty") or 1)
    verify = False
    serving = _serving_by_id(servings, serving_id)
    if serving is None:  # dangling id never renders (§4)
        serving = _serving_by_id(servings, food["food_default_serving_id"])
        if serving is None and servings:
            serving = servings[0]
        verify = True
    if serving is not None:
        serving_id = serving["serving_id"]
    macros = None
    if serving is not None:
        scaled = _scale_macros(serving.get("macros") or {}, qty)
        if all(k in scaled for k in _CORE_MACROS):
            macros = Macros(**scaled)
        else:
            verify = True
    staged = StagedItem(
        item_index=item_index,
        phrase=food["display_name"],
        status="resolved",
        tier=0,
        food=FoodRef(id=str(food["food_id"]), name=food["display_name"], brand=food["brand"]),
        serving=ServingRef(
            id=serving_id or food["food_default_serving_id"],
            description=serving["description"] if serving else (serving_id or ""),
        ),
        qty=qty,
        macros=macros,
        meal=meal,
        confidence="low" if verify else "high",
        servings=_serving_options(servings) or None,
    )
    return Resolution(
        staged=staged, alias_id=None, resolve_ms=int((time.monotonic() - t0) * 1000)
    )


async def expand_macro(
    macro: dict, meal: Meal, start_index: int, t0: float | None = None
) -> tuple[list[Resolution], int]:
    """§3.1: one Resolution per macro item, item_index = start_index.. —
    each expanded item gets its own index so per-item remove, commit, and
    telemetry work on it like any other row.

    Returns (rows, missing): `missing` counts the stored items whose foods row
    is gone. Callers must surface it — a partial expansion would silently log
    half a saved meal, and an empty one is not the same as "no such meal"."""
    t0 = time.monotonic() if t0 is None else t0
    items: list[dict] = macro["items"] or []
    food_ids = sorted({str(i["food_id"]) for i in items if i.get("food_id")})
    foods = (
        {str(r["food_id"]): r for r in await db.fetchall(_MACRO_FOODS_SQL, (food_ids,))}
        if food_ids
        else {}
    )
    out: list[Resolution] = []
    missing = 0
    for item in items:
        food = foods.get(str(item.get("food_id")))
        if food is None:  # foods are cache-forever, so this is a broken macro
            missing += 1
            continue
        out.append(_stage_tier0_item(food, item, start_index + len(out), meal, t0))
    return out, missing


def _meal_ref_guard(phrase: str, item_index: int, meal: Meal, t0: float, text: str) -> Resolution:
    return Resolution(
        staged=StagedItem(
            item_index=item_index,
            phrase=phrase,
            status="needs_input",
            meal=meal,
            question=Question(text=text, options=[]),
        ),
        alias_id=None,
        resolve_ms=int((time.monotonic() - t0) * 1000),
    )


def _macro_broken_guard(
    macro: dict, expanded: int, missing: int, phrase: str, item_index: int,
    meal: Meal, t0: float,
) -> Resolution:
    """A matched macro that cannot expand in full stops here (§3.1)."""
    return _meal_ref_guard(
        phrase, item_index, meal, t0,
        MEAL_BROKEN_GUARD.format(
            name=macro["name"], missing=missing, total=expanded + missing
        ),
    )


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


async def short_circuit_all(raw_input: str, meal: Meal) -> list[Resolution] | None:
    """§3.0 whole-utterance short-circuit, in the spec's order: meal macro
    (tier 0, N rows) before alias (tier 1, one row). None → decompose."""
    t0 = time.monotonic()
    key = norm(raw_input)
    if not key:
        return None
    macro = await find_macro([key])
    if macro is not None:
        rows, missing = await expand_macro(macro, meal, 0, t0)
        if missing:
            return [_macro_broken_guard(macro, len(rows), missing, raw_input, 0, meal, t0)]
        if rows:
            return rows
    sc = await short_circuit(raw_input, meal)
    return [sc] if sc is not None else None


async def short_circuit(raw_input: str, meal: Meal) -> Resolution | None:
    """§3.0 whole-utterance short-circuit, aliases only (tier 1). Callers that
    stage a whole log want short_circuit_all (macro before alias)."""
    t0 = time.monotonic()
    key = norm(raw_input)
    if not key:
        return None
    rows = await db.fetchall(_ALIAS_SQL, (key,))
    if not rows:
        return None
    return await _stage_tier1(rows, raw_input, 0, meal, None, None, None, t0)


async def resolve_items(
    parsed_items: list[ParsedItem],
    meal: Meal,
    index_ids: set[str] | None = None,
    *,
    start_index: int = 0,
) -> list[Resolution]:
    """Per-log cascade over the decomposed items with item_index allocation:
    a tier-0 expansion occupies one index per macro item (§3.1), so the items
    after it shift ("usual breakfast and a coffee" → N macro rows, then
    coffee at index N). `start_index` is where allocation begins — the Agent
    lane (§6.4) stages in more than one call per log, each appending after
    the rows already there."""
    out: list[Resolution] = []
    for parsed in parsed_items:
        t0 = time.monotonic()
        nxt = start_index + len(out)
        macro = await find_macro(_macro_keys(parsed))
        if macro is not None:
            rows, missing = await expand_macro(macro, meal, nxt, t0)
            if missing:
                out.append(
                    _macro_broken_guard(
                        macro, len(rows), missing, parsed.phrase, nxt, meal, t0
                    )
                )
                continue
            if rows:
                out.extend(rows)
                continue
        out.append(await resolve_item(parsed, nxt, meal, index_ids, skip_tier0=True))
    return out


async def resolve_item(
    parsed: ParsedItem,
    item_index: int,
    meal: Meal,
    index_ids: set[str] | None = None,
    *,
    skip_tier0: bool = False,
) -> Resolution:
    """Per-item cascade after decomposition: tier 0 → tier 1 → tier 2 →
    tier-3 handoff, as ONE Resolution (single-row callers: /api/resolve).

    `index_ids` is the set of foods.id strings inlined into the decompose
    prompt (§3.3 2b) — library_match is validated against it when supplied;
    when None (current logs.py wiring), validation falls back to the food
    existing in the cache. `skip_tier0` is resolve_items' flag: it already
    ran the macro check.
    """
    t0 = time.monotonic()

    # tier 0 (§3.1): a one-item macro fits a single row; a multi-item macro
    # cannot expand into one row — say so rather than fall into the
    # "no saved meal" guard, which would be false. A macro missing foods is
    # never truncated into that single row either.
    if not skip_tier0:
        macro = await find_macro(_macro_keys(parsed))
        if macro is not None:
            rows, missing = await expand_macro(macro, meal, item_index, t0)
            if missing:
                return _macro_broken_guard(
                    macro, len(rows), missing, parsed.phrase, item_index, meal, t0
                )
            if len(rows) == 1:
                return rows[0]
            if rows:
                return _meal_ref_guard(
                    parsed.phrase, item_index, meal, t0,
                    f"'{parsed.phrase}' is the saved meal '{macro['name']}' "
                    f"({len(rows)} items) — log it on its own to expand it.",
                )

    # §3.1 guard: a meal-shaped phrase with no matching macro never enters food
    # search.
    if parsed.phrase_kind == "meal_ref":
        return _meal_ref_guard(
            parsed.phrase, item_index, meal, t0, MEAL_REF_GUARD.format(phrase=parsed.phrase)
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
