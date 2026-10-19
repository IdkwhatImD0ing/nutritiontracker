"""USDA FoodData Central source adapter (DESIGN.md §3.4 adapter rules, §8.2).

All rules here are code, not prompt, and were verified against live API
responses (2026-08-20):

- nutrients mapped by stable id (1003/1004/1005/1008), Atwater fallback
  (2048 specific, then 2047 general) for Foundation foods that carry no 1008;
- null-amount category-header rows skipped;
- HTTP 404 on a searched fdcId tolerated (stale index entries observed live) —
  ``get_food`` returns None, ``lookup_barcode`` drops the hit;
- absent micronutrients are absent keys, never 0;
- Branded records normalize BOTH ``labelNutrients`` (per serving, as labeled)
  and ``foodNutrients`` (per 100 g/ml), with GS1 unit mapping (GRM/MLT/OZA)
  and a sanity check for absurd serving units (a live Clif record claimed a
  "68.0 MG" serving);
- barcode lookup uses one field-scoped ``gtinUpc:`` query (unquoted — quoting
  the value returns HTTP 400, verified live) OR-ing every rung of the
  normalization ladder: as-scanned, leading zeros stripped, zero-padded to
  13, to 14 (the OR form verified live 2026-09-06).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

_BASE = "https://api.nal.usda.gov/fdc/v1"
_TIMEOUT = 20.0

# ---------------------------------------------------------------------------
# Nutrient maps — stable FDC nutrient ids -> our macro/micro keys.
# ---------------------------------------------------------------------------

_MACRO_IDS = {1003: "protein_g", 1004: "fat_g", 1005: "carb_g", 1008: "kcal"}
_ATWATER_IDS = (2048, 2047)  # Energy, Atwater Specific then General Factors
# The four a serving needs before it can be logged at all — the same set as
# resolver.commit._CORE_MACROS, which is where the rule is documented. Kept
# here as a frozenset so this module stays dependency-free (sources must not
# import resolver).
_CORE_KEYS = frozenset(_MACRO_IDS.values())

_MICRO_IDS = {
    1079: "fiber_g",
    2000: "sugar_g",
    1093: "sodium_mg",
    1092: "potassium_mg",
    1087: "calcium_mg",
    1089: "iron_mg",
    1090: "magnesium_mg",
    1091: "phosphorus_mg",
    1095: "zinc_mg",
    1103: "selenium_ug",
    1106: "vitamin_a_ug",
    1162: "vitamin_c_mg",
    1114: "vitamin_d_ug",
    1109: "vitamin_e_mg",
    1185: "vitamin_k_ug",
    1165: "thiamin_mg",
    1166: "riboflavin_mg",
    1167: "niacin_mg",
    1175: "vitamin_b6_mg",
    1177: "folate_ug",
    1178: "vitamin_b12_ug",
    1253: "cholesterol_mg",
    1257: "trans_fat_g",
    1258: "saturated_fat_g",
    1292: "monounsaturated_fat_g",
    1293: "polyunsaturated_fat_g",
}

# FDC's own nutrient NUMBER for each id above (read off live records,
# 2026-09-19). The abridged representation of a record names its nutrients by
# `number` and carries no id at all, and since 2026-09-19 that is the only
# representation whose Branded rows name their nutrient at all —
# _abridged_nutrients is the whole reason this table exists.
_NUMBER_BY_ID = {
    1003: "203", 1004: "204", 1005: "205", 1008: "208",
    2047: "957", 2048: "958",
    1079: "291", 2000: "269", 1093: "307", 1092: "306", 1087: "301",
    1089: "303", 1090: "304", 1091: "305", 1095: "309", 1103: "317",
    1106: "320", 1162: "401", 1114: "328", 1109: "323", 1185: "430",
    1165: "404", 1166: "405", 1167: "406", 1175: "415", 1177: "417",
    1178: "418", 1253: "601", 1257: "605", 1258: "606", 1292: "645",
    1293: "646",
}
_ID_BY_NUMBER = {number: nid for nid, number in _NUMBER_BY_ID.items()}

# Branded labelNutrients keys -> our keys (per serving, as labeled).
_LABEL_KEYS = {
    "calories": "kcal",
    "protein": "protein_g",
    "carbohydrates": "carb_g",
    "fat": "fat_g",
    "fiber": "fiber_g",
    "sugars": "sugar_g",
    "addedSugar": "added_sugar_g",
    "saturatedFat": "saturated_fat_g",
    "transFat": "trans_fat_g",
    "cholesterol": "cholesterol_mg",
    "sodium": "sodium_mg",
    "calcium": "calcium_mg",
    "iron": "iron_mg",
    "potassium": "potassium_mg",
    "vitaminD": "vitamin_d_ug",
}

# GS1 serving-size units (GDSN records) plus lowercase variants seen live.
_SERVING_UNITS = {
    "GRM": "g",
    "G": "g",
    "GM": "g",
    "MLT": "ml",
    "ML": "ml",
    "OZA": "fl oz",
}
_ML_PER_FL_OZ = 29.5735

# ---------------------------------------------------------------------------
# State parsing (§3.4a: cooked/raw enforced in code, not just prompt).
# ---------------------------------------------------------------------------

_COOKED_TERMS = (
    "cooked", "roasted", "grilled", "broiled", "baked", "boiled", "stewed",
    "braised", "fried", "steamed", "poached", "microwaved", "simmered",
    "sauteed", "smoked",
)
_STATE_SENSITIVE_TERMS = (
    # meat
    "chicken", "beef", "pork", "turkey", "lamb", "veal", "duck", "steak",
    "ground meat", "bacon", "sausage",
    # fish / seafood
    "fish", "salmon", "tuna", "cod", "tilapia", "halibut", "trout", "shrimp",
    "prawn", "scallop", "sardine", "mackerel",
    # rice / pasta / grains that swell
    "rice", "pasta", "spaghetti", "macaroni", "noodle", "penne", "orzo",
    "couscous", "quinoa", "oats", "oatmeal", "barley",
    # legumes
    "lentil", "bean", "beans", "chickpea", "garbanzo", "split pea", "edamame",
)

# SR Legacy comma-chain segments that are catalog noise, not identity.
_NAME_NOISE = {
    "broilers or fryers", "broiler or fryers", "roasting", "stewing",
    "meat only", "meat and skin", "skin only", "all classes", "domesticated",
    "commercial", "commercially prepared", "boneless", "skinless", "bone-in",
    "separable lean only", "separable lean and fat", "enriched", "unenriched",
    "regular", "prepared", "ready-to-serve", "ready-to-eat", "unspecified",
    "composite of trimmed retail cuts", "trimmed to 0\" fat",
    "trimmed to 1/8\" fat",
}


def _api_key() -> str:
    """FDC_API_KEY from the environment, falling back to backend/.env or repo .env."""
    key = os.environ.get("FDC_API_KEY")
    if key:
        return key
    here = Path(__file__).resolve()
    for env_path in (here.parents[1] / ".env", here.parents[2] / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:export\s+)?FDC_API_KEY\s*=\s*(.+?)\s*$", line)
            if m:
                key = m.group(1).strip().strip('"').strip("'")
                os.environ["FDC_API_KEY"] = key
                return key
    raise RuntimeError("FDC_API_KEY is not set (env or .env)")


async def _get(path: str, **params: Any) -> httpx.Response:
    """GET with the key in the X-Api-Key HEADER (api.data.gov supports both).

    Never as a query param: httpx embeds the full URL in HTTPStatusError text,
    which flows into agent tool errors, Langfuse traces, the archived
    agent_transcript, and 500 tracebacks — none may carry the secret (§10).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        return await client.get(
            f"{_BASE}{path}", params=params, headers={"X-Api-Key": _api_key()}
        )


def _fmt(x: float | int | None) -> str:
    if x is None:
        return ""
    return str(int(x)) if float(x) == int(x) else str(x)


def _round(macros: dict[str, float], ndigits: int = 2) -> dict[str, float]:
    return {k: round(v, ndigits) for k, v in macros.items()}


def _scale(macros: dict[str, float], factor: float) -> dict[str, float]:
    return _round({k: v * factor for k, v in macros.items()})


# ---------------------------------------------------------------------------
# Nutrient extraction
# ---------------------------------------------------------------------------


def _nutrient_id(row: dict) -> int | None:
    """The FDC nutrient id one foodNutrients row is about, whichever way the
    API named it: /food/{id} nests it (``nutrient.id``) and ?format=abridged
    carries only its ``number``, which _ID_BY_NUMBER maps back. None when the
    row names no nutrient the maps above know.
    """
    nested = row.get("nutrient") or {}
    nid = nested.get("id")
    if nid is not None:
        return nid
    number = nested.get("number") or row.get("number")
    return _ID_BY_NUMBER.get(str(number)) if number is not None else None


def _nutrients_from_detail(food_nutrients: list[dict]) -> dict[str, float]:
    """Map /food/{id} foodNutrients by stable id. Skips null-amount header rows.

    Absent nutrients stay absent — never 0. kcal falls back to the Atwater
    energy ids (Foundation foods often carry no 1008).
    """
    out: dict[str, float] = {}
    atwater: dict[int, float] = {}
    for row in food_nutrients:
        amount = row.get("amount")
        if amount is None:  # category-header rows — skip
            continue
        nid = _nutrient_id(row)
        if nid in _MACRO_IDS:
            out[_MACRO_IDS[nid]] = float(amount)
        elif nid in _MICRO_IDS:
            out[_MICRO_IDS[nid]] = float(amount)
        elif nid in _ATWATER_IDS:
            atwater[nid] = float(amount)
    if "kcal" not in out:
        for nid in _ATWATER_IDS:
            if nid in atwater:
                out["kcal"] = atwater[nid]
                break
    return _round(out)


def _core_macros_in_hit(hit: dict) -> set[str]:
    """Which of the four core macros a /foods/search hit already carries.

    Search rows are FLAT (``nutrientId`` + ``value``), unlike /food/{id}'s
    nested ``nutrient`` objects — so this cannot reuse _nutrients_from_detail.
    _MACRO_IDS is exactly the core four; kcal also arrives as an Atwater id.

    ``amount`` is read as well as ``value``, and the nested nutrient id as well
    as the flat one: this feeds a FILTER, so a shape this function fails to
    understand does not degrade one column — it drops every hit and turns the
    whole search into a silent "no results".
    """
    have: set[str] = set()
    for row in hit.get("foodNutrients") or []:
        if row.get("value") is None and row.get("amount") is None:
            continue
        nid = row.get("nutrientId")
        if nid is None:
            nid = (row.get("nutrient") or {}).get("id")
        if nid in _MACRO_IDS:
            have.add(_MACRO_IDS[nid])
        elif nid in _ATWATER_IDS:
            have.add("kcal")
    return have


def _hit_is_loggable(hit: dict) -> bool:
    """§3.4: a hit whose own nutrients lack a core macro is dropped from
    results (BACKLOG 184). FDC Branded carries plenty of thin records — live
    "Oikos Triple Zero yogurt" returned 25 hits of which 5-6 lacked at least
    one of kcal/protein/carb/fat (the count shifts with FDC's relevance
    order), 2756792 (Mixed Berry) among them. Such a row can never satisfy
    commit's core-macros guard, so offering it hands the user — or the tier-3
    agent — a food that stages with no numbers and refuses at commit, while
    complete records for the same product sit in the same result set (2775572
    is the same flavor, fully populated). Generic hits are barely touched
    (1 drop in 165 live hits over 7 whole-food queries) but run the same rule:
    an unloggable row is noise there too. The search payload already carries
    the nutrients, so this costs no extra request.
    """
    return _CORE_KEYS <= _core_macros_in_hit(hit)


def _hit_nutrient(hit: dict, *ids: int) -> float | None:
    """The value of the first of ``ids`` a /foods/search hit carries, read
    the way _core_macros_in_hit reads a row: per 100 g, or per 100 ml for a
    Branded beverage (_per_100ml). kcal asks for (1008, *_ATWATER_IDS), the
    fallback order _nutrients_from_detail takes. None when absent — never 0.
    """
    found: dict[int, float] = {}
    for row in hit.get("foodNutrients") or []:
        value = row.get("value")
        if value is None:
            value = row.get("amount")
        nid = row.get("nutrientId")
        if nid is None:
            nid = (row.get("nutrient") or {}).get("id")
        if value is not None and nid in ids:
            found.setdefault(nid, round(float(value), 2))
    return next((found[i] for i in ids if i in found), None)


def _has_core_macros(macros: dict) -> bool:
    """All four, numeric — mirrors resolver.commit._missing_core_macros.

    A partial serving counts as unloggable too (owner's ruling, 2026-09-09):
    nothing can supply a record's missing carb and fat, so offering it only
    produces a row that cannot commit.
    """
    return all(isinstance(macros.get(k), (int, float)) for k in _CORE_KEYS)


def _has_loggable_serving(food: dict) -> bool:
    """Whether a normalized food has any serving that could be logged — the
    barcode ladder's gate (BACKLOG 192).

    lookup_barcode used to return the first record that merely wasn't a 404, so
    a thin FDC Branded row won, was cached forever (foods is cache-forever), and
    Open Food Facts — which has its own core-macro gate now — never got its
    turn. A scan that could have resolved landed on a numberless card instead.
    """
    return any(
        _has_core_macros(s.get("macros") or {}) for s in (food.get("servings") or [])
    )


def _label_nutrients(label: dict) -> dict[str, float]:
    """Map Branded labelNutrients (per serving, as labeled)."""
    out: dict[str, float] = {}
    for key, ours in _LABEL_KEYS.items():
        row = label.get(key)
        if isinstance(row, dict) and row.get("value") is not None:
            out[ours] = float(row["value"])
    return out


# ---------------------------------------------------------------------------
# Name / state parsing
# ---------------------------------------------------------------------------


def _parse_state(name: str) -> str | None:
    low = name.lower()
    if re.search(r"\braw\b", low):
        return "raw"
    if re.search(r"\b(dry|dried|uncooked)\b", low):
        return "dry"
    if any(re.search(rf"\b{t}\b", low) for t in _COOKED_TERMS):
        return "cooked"
    return None


def _state_sensitive(name: str) -> bool:
    low = name.lower()
    return any(re.search(rf"\b{re.escape(t)}\b", low) for t in _STATE_SENSITIVE_TERMS)


_TRAILING_SIZE = re.compile(
    r"^[\d\s./]+(fl\s+)?(oz|ounce|g|gram|ml|l|lb|ct|count|pack|pk|pouch|cans?|bottles?)\b",
    re.IGNORECASE,
)


def _display_name(description: str, data_type: str) -> str:
    """Short human name, lowercased; SR Legacy comma-chains trimmed.

    "Chicken, broilers or fryers, breast, meat only, cooked, roasted"
    -> "chicken breast, roasted"
    """
    if data_type == "Branded":
        parts = [p.strip() for p in description.lower().split(",")]
        while len(parts) > 1 and _TRAILING_SIZE.match(parts[-1]):
            parts.pop()
        return ", ".join(p for p in parts if p)

    parts = [p.strip().lower() for p in description.split(",") if p.strip()]
    heads: list[str] = []
    method: str | None = None
    for part in parts:
        if part in ("raw", "dry", "dried"):
            method = part
        elif any(re.fullmatch(rf"{t}(\s+\w+)?", part) for t in _COOKED_TERMS):
            # "cooked" is generic; a specific method ("roasted") overrides it
            if part != "cooked" or method is None:
                method = part
        elif part in _NAME_NOISE:
            continue
        elif len(heads) < 2:
            heads.append(part)
    name = " ".join(heads) if heads else parts[0]
    return f"{name}, {method}" if method else name


# ---------------------------------------------------------------------------
# Serving normalization
# ---------------------------------------------------------------------------


def _portion_description(p: dict) -> str:
    desc = (p.get("portionDescription") or "").strip()
    if desc and desc.lower() != "quantity not specified":
        return desc
    parts = []
    if p.get("amount"):
        parts.append(_fmt(p["amount"]))
    unit = ((p.get("measureUnit") or {}).get("name") or "").strip()
    if unit and unit.lower() != "undetermined":
        parts.append(unit)
    if (p.get("modifier") or "").strip():
        parts.append(p["modifier"].strip())
    return " ".join(parts) or f"{_fmt(p.get('gramWeight'))} g"


def _generic_servings(payload: dict, per_100g: dict[str, float]) -> tuple[list[dict], str]:
    servings = []
    for p in payload.get("foodPortions") or []:
        grams = p.get("gramWeight")
        if not grams:
            continue
        servings.append({
            "serving_id": str(p.get("id")),
            "description": _portion_description(p),
            "grams": float(grams),
            "macros": _scale(per_100g, float(grams) / 100),
        })
    servings.append({
        "serving_id": "100g",
        "description": "100 g",
        "grams": 100.0,
        "macros": dict(per_100g),
    })
    return servings, "100g"


def _per_100ml(record: dict) -> bool:
    """A Branded record's foodNutrients are per 100 ml when it is served by
    volume (a beverage), else per 100 g — a /food/{id} payload or a search
    hit alike."""
    unit = _SERVING_UNITS.get((record.get("servingSizeUnit") or "").strip().upper())
    return unit in ("ml", "fl oz")


def _branded_servings(payload: dict, per_100: dict[str, float]) -> tuple[list[dict], str]:
    """Label serving (as labeled) + per-100 serving, both normalized (§3.4)."""
    size = payload.get("servingSize")
    unit = _SERVING_UNITS.get((payload.get("servingSizeUnit") or "").strip().upper())
    grams = ml = None
    if size is not None and unit == "g":
        grams = float(size)
    elif size is not None and unit == "ml":
        ml = float(size)
    elif size is not None and unit == "fl oz":
        ml = float(size) * _ML_PER_FL_OZ
    # sanity check: absurd serving units/values (live Clif record: "68.0 MG")
    if grams is not None and not 0.5 <= grams <= 2500:
        grams = None
    if ml is not None and not 0.5 <= ml <= 2500:
        ml = None

    basis_ml = _per_100ml(payload)
    quantity = grams if grams is not None else ml
    macros: dict[str, float] = {}
    if quantity is not None and per_100:
        macros = _scale(per_100, quantity / 100)
    macros.update(_label_nutrients(payload.get("labelNutrients") or {}))

    household = (payload.get("householdServingFullText") or "").strip()
    if household and size is not None and unit:
        description = f"{household} ({_fmt(size)} {unit})"
    elif household:
        description = household
    elif size is not None and unit:
        description = f"{_fmt(size)} {unit}"
    else:
        description = "1 serving"

    # A label serving short of the four core macros must not be emitted — let
    # alone as the default (BACKLOG 184, 192). Live records
    # with servingSize null and labelNutrients {} land here with grams=None
    # and macros={}: as the default it is exactly what the portion cascade
    # picks, so the card renders with no numbers and commit refuses it even
    # when the per-100 serving beside it is complete (FDC 2756849, 2756842,
    # 2757143, 2756821 — all Oikos Triple Zero, all verified live).
    servings = []
    if _has_core_macros(macros):
        servings.append({
            "serving_id": "label",
            "description": description,
            "grams": grams,
            "macros": macros,
        })
    if per_100:
        base_id = "100ml" if basis_ml else "100g"
        servings.append({
            "serving_id": base_id,
            "description": "100 ml" if basis_ml else "100 g",
            "grams": None if basis_ml else 100.0,
            "macros": dict(per_100),
        })
    # the default is whatever survived, in preference order (label first)
    return servings, (servings[0]["serving_id"] if servings else "label")


# ---------------------------------------------------------------------------
# Public interface (shared signatures — do not change)
# ---------------------------------------------------------------------------


async def search_generic(query: str) -> list[dict]:
    """Whole-food search over SR Legacy + Foundation, condensed for the agent
    and the Search lane, which shows each row's kcal and protein per 100 g."""
    r = await _get("/foods/search", query=query,
                   dataType="SR Legacy,Foundation", pageSize=25)
    r.raise_for_status()
    hits = []
    for f in r.json().get("foods", []):
        if not _hit_is_loggable(f):  # BACKLOG 184 — same rule as branded
            continue
        hits.append({
            "fdc_id": f["fdcId"],
            "description": f.get("description", ""),
            "data_type": f.get("dataType", ""),
            "protein_per_100g": _hit_nutrient(f, 1003),
            "kcal_per_100g": _hit_nutrient(f, 1008, *_ATWATER_IDS),
        })
    return hits


_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _brand_overlap(query_tokens: set[str], hit: dict) -> int:
    """§3.4 rule 2: query tokens matching the LEADING word of a brand field.

    brandOwner is the corporate name ("Quest Nutrition, LLC"); brandName the
    consumer-facing one ("fairlife" under brandOwner "Coca-Cola") — both count,
    or either a "quest bar" or a "fairlife" query would miss its own brand.

    Only each field's first word counts, because that word is the brand and
    what trails it is category or corporate filler — "Bar", "Nutrition",
    "Foods", "Company", "LLC". Scoring every brand word instead let the rank
    turn on words that name a product rather than a maker, and §3.4 rule 4
    tells the agent to add exactly those: "protein bar" refines to "Quest
    protein bar chocolate chip cookie dough", where (live, 2026-09-09) "The
    Truth Bar LLC" and "Perfect Bar, LLC" both scored on the word "bar" while
    Quest scored once on its own name — so refining the query pushed the brand
    the user actually named down to third. On leading words Quest ranks first,
    and 14 live queries naming their brand (fairlife, Oikos, Kirkland, Clif,
    Coca-Cola, Chobani, Hershey, Premier, Great Value, Ben & Jerry's, Trader
    Joe's, Starbucks, Nature Valley, Celsius) each still rank it first.

    A leading article is stepped over: FDC stores "The Hershey Company", and a
    live KISSES record pairs it with brandName "KISSES", so "hershey kisses"
    would otherwise match the leading word of neither field.
    """
    lead_tokens: set[str] = set()
    for field in (hit.get("brand_owner"), hit.get("brand_name")):
        tokens = _QUERY_TOKEN_RE.findall(str(field).lower()) if field else []
        if tokens:
            lead_tokens.add(tokens[0])
            if tokens[0] == "the" and len(tokens) > 1:
                lead_tokens.add(tokens[1])
    return len(query_tokens & lead_tokens)


async def search_branded(query: str) -> list[dict]:
    """FDC Branded search, ranked by brand match against the query tokens
    (§3.4 rule 2): plain name search is fuzzy-noisy — a "Quest" query returns
    123k hits with competitors ranked first, verified live. The sort is stable,
    so FDC's own relevance order is preserved within equal brand scores.

    Hits missing a core macro are dropped (_hit_is_loggable, BACKLOG 184) —
    they cannot commit, and a complete record for the same product is usually
    right there in the same page.

    kcal and protein come per 100 g, or per 100 ml where ``per_100ml`` (a
    beverage): what the Search lane shows to tell near-identical rows apart."""
    r = await _get("/foods/search", query=query, dataType="Branded", pageSize=25)
    r.raise_for_status()
    hits = [{
        "fdc_id": f["fdcId"],
        "description": f.get("description", ""),
        "brand_owner": f.get("brandOwner"),
        "brand_name": f.get("brandName"),
        "data_type": f.get("dataType", ""),
        "data_source": f.get("dataSource"),
        "published_date": f.get("publishedDate"),
        "gtin_upc": f.get("gtinUpc"),
        "kcal_per_100g": _hit_nutrient(f, 1008, *_ATWATER_IDS),
        "protein_per_100g": _hit_nutrient(f, 1003),
        "per_100ml": _per_100ml(f),
    } for f in r.json().get("foods", []) if _hit_is_loggable(f)]
    query_tokens = set(_QUERY_TOKEN_RE.findall(query.lower()))
    hits.sort(key=lambda h: -_brand_overlap(query_tokens, h))
    return hits


def gtin_variants(code: str) -> list[str]:
    """The §3.4 normalization ladder as a list, in rung order and deduplicated:
    as-scanned -> leading zeros stripped -> the stripped form zero-padded to
    13 -> to 14. Padding the *stripped* form matters: a 14-digit scan padded
    as-is never yields the 13-digit form Open Food Facts keys its records by.
    Stored gtinUpc lengths are inconsistent and matching is literal — verified
    failing in both directions otherwise — so every rung is a candidate; the
    foods cache is searched with the same list (routes/quick.py)."""
    digits = re.sub(r"\D", "", code or "")
    stripped = digits.lstrip("0")
    out: list[str] = []
    for candidate in (digits, stripped, stripped.zfill(13), stripped.zfill(14)):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


async def lookup_barcode(code: str) -> dict | None:
    """Barcode lookup via the §3.4 normalization ladder — in ONE search call.

    Every rung goes into a single field-scoped query, ``gtinUpc:A OR
    gtinUpc:B OR ...`` (unquoted — quoting the value returns HTTP 400).
    Verified live 2026-09-06: a 12-digit UPC-A whose record is stored 14-digit
    answers from the combined query in one round trip (~0.3 s) where the
    rung-by-rung version spent four. Exact matches are preferred in rung
    order, then FDC's own rank.

    Raises httpx.HTTPStatusError on a non-2xx search: a 429/5xx is an outage,
    not a miss. Read as a miss, the route would let Open Food Facts stand in
    and cache its row, and every rescan would then answer from that row
    without FDC ever being asked again.
    """
    variants = gtin_variants(code)
    if not variants:
        return None
    r = await _get("/foods/search", query=" OR ".join(f"gtinUpc:{v}" for v in variants),
                   dataType="Branded", pageSize=5)
    r.raise_for_status()
    hits = r.json().get("foods", [])
    rank = {v: i for i, v in enumerate(variants)}
    hits.sort(key=lambda f: rank.get(f.get("gtinUpc") or "", len(rank)))
    for hit in hits[:3]:
        food = await get_food("fdc", str(hit["fdcId"]))
        if food is None:  # stale index entry (404) — drop the hit
            continue
        if not _has_loggable_serving(food):
            continue  # thin record: let Open Food Facts have its turn (192)
        return food
    return None


async def _abridged_nutrients(source_id: str) -> dict[str, float]:
    """The record's nutrients read from its ABRIDGED representation — the
    backstop for a full record whose rows name no nutrient at all.

    Since 2026-09-19 every FDC *Branded* record answers /food/{id} with
    foodNutrients rows of {id, type, amount}: the `nutrient` object is gone,
    and `labelNutrients` comes back empty beside it, so the adapter reads no
    macros, the food normalizes to zero servings, and tier 3 discards it as
    uncommittable (services/tier3._first_live_food) — every branded food the
    cache did not already hold resolved as not_found, while /foods/search
    still advertised it as loggable. ?format=abridged still names each row's
    nutrient by `number`, so one extra request recovers them. It is spent only
    on a record the full payload could not read AT ALL, so it costs nothing
    once FDC puts the field back.
    """
    r = await _get(f"/food/{quote(str(source_id), safe='')}", format="abridged")
    if r.status_code != 200:
        return {}
    return _nutrients_from_detail(r.json().get("foodNutrients") or [])


async def get_food(source: str, source_id: str) -> dict | None:
    """Fetch + normalize one FDC record. Returns None on 404 (stale index ids)."""
    if source != "fdc":
        raise ValueError(f"fdc adapter got source={source!r}")
    # URL-encode: source_id is model/client-supplied text — never raw in a path.
    r = await _get(f"/food/{quote(str(source_id), safe='')}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    payload = r.json()

    data_type = payload.get("dataType", "")
    name = payload.get("description", "")
    per_100 = _nutrients_from_detail(payload.get("foodNutrients") or [])
    if not per_100:
        per_100 = await _abridged_nutrients(source_id)

    if data_type == "Branded":
        brand = (payload.get("brandName") or payload.get("brandOwner") or "").strip() or None
        servings, default_id = _branded_servings(payload, per_100)
        food_type = "branded"
        # A packaged product has exactly one state — as sold — so §3.4a's
        # cooked/raw question has nothing to ask about, and sources/off.py
        # hardcodes these same two values for the same reason. Run over a
        # branded description the term lists misfire on the food inside the
        # box: "FROZEN ENTREE SALISBURY STEAK" came back state_sensitive with
        # no state (verified live 2026-09-09), which is exactly the shape that
        # asks the question — and the toggle then fires one search_generic for
        # a counterpart the boxed meal has none of, swapping the entree for
        # plain cooked beef.
        state, state_sensitive = None, False
    else:
        brand = None
        servings, default_id = _generic_servings(payload, per_100)
        food_type = "generic"
        state, state_sensitive = _parse_state(name), _state_sensitive(name)

    return {
        "source": "fdc",
        "source_id": str(payload.get("fdcId", source_id)),
        "name": name,
        "display_name": _display_name(name, data_type),
        "brand": brand,
        "food_type": food_type,
        "state": state,
        "state_sensitive": state_sensitive,
        "servings": servings,
        "default_serving_id": default_id,
        "per_100g": per_100 or None,
        "payload": payload,
    }
