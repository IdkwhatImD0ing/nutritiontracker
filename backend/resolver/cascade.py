"""The resolver cascade (DESIGN.md §3).

Implemented here: whole-utterance short-circuit (§3.0: meal macro first, then
aliases — short_circuit_all), tier 0 meal-macro expansion (§3.1: one
StagedItem per macro item, stored portions, no search/LLM/portion inference),
tier-1 exact alias match with §3.2's pick() and alternates, tier 2 (§3.3:
decompose-time library_match — the trigram matcher that used to sit beside it
was removed 2026-09-10), portion resolution per
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
from resolver.commit import (
    COMMITTABLE_FOOD_SQL,
    LEARNABLE_ROWS_SQL,
    food_committable,
    with_loggable_servings,
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
  AND {loggable}
""".format(loggable=COMMITTABLE_FOOD_SQL.format(t="f"))

# §3.3 tier 2 — the validated library_match food, shaped like an alias row
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
  AND {loggable}
""".format(loggable=COMMITTABLE_FOOD_SQL.format(t="f"))

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


# The leading amount accepts the fraction and mixed-number forms a label
# actually prints ("1/2 cup", "1 1/2 cups"): FDC passes householdServingFullText
# through verbatim (sources/fdc.py _branded_servings), so "1/2 cup (55 g)" is
# the serving text, and a digits-then-space-only pattern matched none of it —
# the divisor fell back to 1.0 and a spoken cup amount staged at half.
_LEADING_PIECES_RE = re.compile(r"^\s*(\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s+([a-z]+)")


def _amount(text: str) -> float:
    """The leading amount as a number: "2" → 2.0, "1/2" → 0.5, "1 1/2" → 1.5.

    0.0 for anything that will not divide — a "1/0" in provider text reaches
    here verbatim (fdc.py passes householdServingFullText straight through), and
    both callers already treat a non-positive amount as "no usable count" and
    fall back to 1.0. Crashing the whole staging request over one malformed
    label would be the one response neither of them can recover from."""
    total = 0.0
    for part in text.split():
        if "/" in part:
            num, _, den = part.partition("/")
            if float(den) == 0:
                return 0.0
            total += float(num) / float(den)
        else:
            total += float(part)
    return total


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
    n = _amount(m.group(1))
    return n if n > 0 else 1.0


def _units_per(description: str, stem: str) -> float:
    """How many of the SPOKEN unit one serving holds, for the unit-word match:
    when the word after the leading number is that unit itself — measure or
    not — the number counts it ("1 tbsp" of a "2 tbsp (32 g)" serving is half
    a serving, "1 cup" of "2 cups (85 g)" too); else the PARENTHETICAL measure
    when it is in that unit ("1 serving (325 ml)" holds 325 ml, "1 bottle
    (414 ml)" 414); otherwise the serving's piece count ("two scoops" of
    "2 scoops (64 g)" → 2, "1 can" of "12 fl oz can" → 1).

    The parenthetical arm is not a nicety: the fuzzy branch in resolve_portion
    matches on the unit word appearing ANYWHERE in the description, so
    "1 serving (325 ml)" matched a spoken "ml" and then divided by the leading
    1 — making 325 ml of a 325 ml shake THREE HUNDRED AND TWENTY-FIVE
    servings. Same shape as the weight bug beside it (Bill, 2026-09-09), and
    the parenthetical is exactly where FDC and OFF put the measure."""
    m = _LEADING_PIECES_RE.match((description or "").lower())
    if m and _unit_stem(m.group(2)) == stem:
        amount = _amount(m.group(1))
        if amount > 0:
            return amount
    paren = _paren_measure(description, stem)
    if paren is not None:
        return paren
    return _pieces_per(description)


# A measure inside a serving description's parentheses, in the unit asked for:
# "1 serving (325 ml)" holds 325 ml, "1 bottle (16.9 fl oz)" 16.9 oz. Every
# occurrence is scanned and only one whose unit MATCHES is returned, so a
# description carrying two ("1 cup (240 ml) (8 fl oz)") answers whichever was
# asked rather than whichever came first. The unit is captured as up to two
# words because "fl oz" is two, and both the joined form ("floz") and the last
# word ("oz") are tried against the stem — `[a-z]+` alone silently missed
# every fl-oz serving.
_PAREN_MEASURE_RE = re.compile(r"\((\d+(?:\.\d+)?)\s*([a-z]+(?:\s+[a-z]+)?)")


def _paren_measure(description: str, stem: str) -> float | None:
    for num, phrase in _PAREN_MEASURE_RE.findall((description or "").lower()):
        n = float(num)
        if n <= 0:
            continue
        words = phrase.split()
        forms = {phrase.replace(" ", ""), words[-1]}
        if any(_unit_stem(f) == stem for f in forms):
            return n
    return None


def _is_count_serving(description: str) -> bool:
    """True when a serving describes a countable household unit ("1 large",
    "1 bottle (414 ml)"), not a bare mass/volume measure ("100 g")."""
    main = re.sub(r"\(.*?\)", " ", description.lower())
    tokens = re.findall(r"[a-z]+", main)
    return any(t not in _MEASURE_WORDS for t in tokens)


_LEADING_UNIT_RE = re.compile(r"^\s*(?:\d+(?:[./]\d+)?(?:\s+\d+/\d+)?\s+)?([a-z]+)")


def _counts_pieces(description: str) -> bool:
    """True when a serving counts household PIECES ("1 large", "1 bottle"), as
    opposed to measuring ("1 cup, whole", "12 fl oz can").

    Narrower than `_is_count_serving`, and only the §7.3 spoken-count rule uses
    it. `_is_count_serving` asks whether SOME token is not a measure word, which
    is the right question for ordering gram servings but the wrong one here:
    FDC builds a portion as "{amount} {measureUnit} {modifier}"
    (sources/fdc.py _portion_description), so "1 cup, whole" keeps a
    preparation modifier that is not a measure word, and a cup read as a
    countable piece silently multiplied ("eight almonds" → eight cups). The
    unit HEAD decides instead: a measure word there means the leading number
    measures that unit, not pieces of it."""
    if not _is_count_serving(description):
        return False
    m = _LEADING_UNIT_RE.match(re.sub(r"\(.*?\)", " ", (description or "").lower()))
    return bool(m) and m.group(1) not in _MEASURE_WORDS


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

    Order: the spoken quantity wins WHEN IT MAPS onto a serving (with the
    count-without-unit rule) → alias mode-of-last-10 → food's most-logged
    portion → provider default × 1.

    "When it maps" is the whole safety property: a spoken amount that nothing
    can size — 30 g of a food whose every serving is measured in millilitres,
    50 of a food with no count-type serving — must NOT be used as the serving
    multiplier. Doing that is how "a 30 gram protein shake" became thirty
    shakes and 11 photographed shrimp became 11 × 100 g (Bill, 2026-09-09).
    Both fall back to ONE serving, flagged verify-me: wrong by a serving at
    worst instead of by 30×.
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
                # No serving carries a `grams` COLUMN — but plenty state the
                # weight in the description instead ("2 tbsp (32 g)", common
                # in OFF records), so read that before giving up.
                for s_ in servings:
                    per = _paren_measure(s_.get("description", ""), stem)
                    if per:
                        return s_["serving_id"], round(qty / per, 4), False
                # Nothing at all to map the weight onto — a millilitre drink,
                # say. The number is a WEIGHT, and using it as the serving
                # multiplier is how "a 30 gram protein shake" became THIRTY
                # shakes: 3,900 kcal and 900 g of protein, one tap from the
                # diary (Bill, 2026-09-09). One serving, flagged for him to
                # check, is wrong by a serving at worst instead of by 30x.
                return default_id, 1.0, True
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
            desc = base.get("description", "")
            # §7.3: only a serving that really counts pieces may stage
            # unverified. A measuring serving still gets picked — it is the
            # best guess and the portion sheet opens on it — but the count is
            # a guess, so it rides as verify-me rather than confidence "high".
            # Same reasoning as the mass-default note below, one step earlier:
            # "1 cup, whole" is a measure wearing a descriptor, and multiplying
            # it by a spoken count is the 5 kg-of-shrimp mistake in miniature.
            return base["serving_id"], qty / _pieces_per(desc), not _counts_pieces(desc)
        # No count-type serving to divide by, so the count multiplies the mass
        # default and the row is flagged verify-me. The comment here used to
        # claim it "never silently multiplies", which was simply false — but
        # the behaviour is deliberate, not an oversight: BACKLOG's thin-default
        # case wants "two of those yogurts" to stage 2 × 100 g with REAL
        # numbers rather than a blank card, and for a food whose piece is
        # roughly one serving that is a fair guess.
        #
        # It is a fair guess only while the count is small. A piece of shrimp
        # is ~9 g against a 100 g serving, so "fifty shrimp" resolves to 5 kg,
        # and nothing here can know a piece weight to catch that. What DOES
        # know one is vision: both lanes that carry an est_grams re-size the
        # row through routes/logs._size_by_weight (the photo lane finally does
        # too, 2026-09-09). The uncovered case is a large spoken count with no
        # weight anywhere, where the verify-me flag is the only defence.
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
) -> Resolution:
    """Stage one resolved item from an alias⋈food row (alias fields may be NULL
    for a tier-2b library match). Shared by tiers 1 and 2 — same portion
    resolution, dangling-serving guard, and §3.4a state question."""
    # BACKLOG 184: drop any serving that could not be logged BEFORE the portion
    # cascade chooses, or a stored phantom default is what it picks
    picked = with_loggable_servings(picked, default_key="food_default_serving_id")
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
    )
    return Resolution(
        staged=staged,
        alias_id=str(picked["alias_id"]) if picked["alias_id"] is not None else None,
        resolve_ms=int((time.monotonic() - t0) * 1000),
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


async def _stage_tier2(
    parsed: ParsedItem,
    item_index: int,
    meal: Meal,
    index_ids: set[str] | None,
    t0: float,
) -> Resolution | None:
    """§3.3: the decompose-tagged library match, resolved "verify me". Returns
    None when there is none (→ tier 3).

    This was one of two matchers. The other, a pg_trgm similarity() test of the
    phrase against his alias phrases, was removed 2026-09-10 (Bill: "we should
    remove fuzzy matching in its entirety"): spelling distance is not what a
    person meant — "Tonkatsu" scores 0.33 against the "tonkotsu ramen" he meant,
    under the 0.55 bar — so a food is now named only by an exact phrase
    (tier 1), a model reading his foods (here), or tier 3.
    """
    lib = await _library_food(parsed.library_match, index_ids)
    if lib is None:
        return None
    return await _stage_resolved(
        lib, parsed.phrase, item_index, meal, parsed.qty, parsed.unit, parsed.state, t0,
        tier=2, confidence="low",
    )


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
# A food with no loggable serving is broken the same way, but the row is READ
# here and judged in Python (food_committable, expand_macro below) rather than
# filtered out in SQL. Filtering hid it: the guard could then say only how MANY
# foods were wrong, and the one thing it tells the user to do — fix it in
# Library — needs the name of the food to fix. Reading it costs nothing; the
# rows are fetched by id either way.
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
    "Saved meal '{name}' can't log {missing} of its {total} foods ({which}) — "
    "fix it in Library, then log it again."
)
# What `which` says for a stored item whose foods row is gone: there is no name
# left to give. An unloggable food still present is named outright.
MEAL_MISSING_FOOD = "a food that is no longer in your library"


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
    stored_servings: list[dict] = food.get("servings") or []  # before the filter
    food = with_loggable_servings(food, default_key="food_default_serving_id")  # BACKLOG 184
    servings: list[dict] = food["servings"]
    serving_id = item.get("serving_id")
    qty = float(item.get("qty") or 1)
    verify = False
    serving = _serving_by_id(servings, serving_id)
    if serving is None:  # dangling id never renders (§4)
        dropped = _serving_by_id(stored_servings, serving_id)
        serving = _serving_by_id(servings, food["food_default_serving_id"])
        if serving is None and servings:
            serving = servings[0]
        # The substitute is a different SIZE. Carrying the stored qty across
        # untouched logs a different AMOUNT than the meal was saved at — a
        # meal saved as one 414 ml bottle became one 100 g serving, a quarter
        # of it, while Library still showed the bottle. Rescale by gram weight
        # when both sides have one; when they do not, the qty is a guess and
        # the verify-me badge below is the only honest answer.
        if serving is not None and dropped is not None:
            was, now = dropped.get("grams"), serving.get("grams")
            if isinstance(was, (int, float)) and isinstance(now, (int, float)) and was > 0 and now > 0:
                qty = round(qty * float(was) / float(now), 4)
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

    Returns (rows, broken): `broken` NAMES the stored items that cannot be
    logged — the foods row is gone, or it survives with no serving carrying the
    core four, which is the same dead end to the person holding the phone.
    Callers must surface it — a partial expansion would silently log half a
    saved meal, and an empty one is not the same as "no such meal"."""
    t0 = time.monotonic() if t0 is None else t0
    items: list[dict] = macro["items"] or []
    food_ids = sorted({str(i["food_id"]) for i in items if i.get("food_id")})
    foods = (
        {str(r["food_id"]): r for r in await db.fetchall(_MACRO_FOODS_SQL, (food_ids,))}
        if food_ids
        else {}
    )
    out: list[Resolution] = []
    broken: list[str] = []
    for item in items:
        food = foods.get(str(item.get("food_id")))
        if food is None:  # foods are cache-forever, so this is a broken macro
            broken.append(MEAL_MISSING_FOOD)
            continue
        if not food_committable(food):
            # present, but nothing on it can be logged — name it, because
            # "fix it in Library" is useless without knowing which one
            broken.append(food["display_name"])
            continue
        out.append(_stage_tier0_item(food, item, start_index + len(out), meal, t0))
    return out, broken


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
    macro: dict, expanded: int, broken: list[str], phrase: str, item_index: int,
    meal: Meal, t0: float,
) -> Resolution:
    """A matched macro that cannot expand in full stops here (§3.1)."""
    return _meal_ref_guard(
        phrase, item_index, meal, t0,
        MEAL_BROKEN_GUARD.format(
            name=macro["name"],
            missing=len(broken),
            total=expanded + len(broken),
            which=", ".join(broken),
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
        rows, broken = await expand_macro(macro, meal, 0, t0)
        if broken:
            return [_macro_broken_guard(macro, len(rows), broken, raw_input, 0, meal, t0)]
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
            rows, broken = await expand_macro(macro, meal, nxt, t0)
            if broken:
                out.append(
                    _macro_broken_guard(
                        macro, len(rows), broken, parsed.phrase, nxt, meal, t0
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
            rows, broken = await expand_macro(macro, meal, item_index, t0)
            if broken:
                return _macro_broken_guard(
                    macro, len(rows), broken, parsed.phrase, item_index, meal, t0
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

    # tier 2 — novel phrasing, known food: decompose's library match (§3.3)
    tier2 = await _stage_tier2(parsed, item_index, meal, index_ids, t0)
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
