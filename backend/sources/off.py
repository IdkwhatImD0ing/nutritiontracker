"""Open Food Facts source adapter — the barcode miss-fallback (DESIGN.md §3.4,
§8.2: fallback rather than primary, on evidence; §13: quality flags ride along).

Verified against live API responses (2026-09-06):

- ``GET /api/v2/product/{code}`` normalizes the code itself: an 11-, 12- or
  14-digit scan answers with the 13-digit record (``00049000006346`` ->
  ``0049000006346``), so no padding ladder is needed here;
- a miss is HTTP 200 with ``status: 0`` ("product not found" / "no code or
  invalid code"), not only a 404 — both are handled;
- ``nutriments`` ``*_100g`` values are per 100 g/ml in OFF's storage units —
  grams for everything but energy (kJ) and energy-kcal (kcal) — whatever the
  contributor's ``*_unit`` says (Cheerios: ``sodium_100g`` 0.487 = 487 mg per
  100 g, matching the label's 190 mg per 39 g); ``*_serving`` values are OFF's
  own per-serving figures, present when ``serving_quantity`` is known;
- ``serving_quantity`` is numeric with ``serving_quantity_unit`` ``g``/``ml``
  (absent on older records: grams, or ml when ``nutrition_data_per`` is 100ml);
- reads are rate-limited per IP (the docs say 15 product reads a minute) and
  OFF asks for a descriptive ``User-Agent`` — ``OFF_USER_AGENT``, defaulting
  to the string DESIGN.md §0.5 documents.

The record returned has the shape ``sources/fdc.get_food`` returns, so
foods_repo persists it unchanged and routes/foods.py's detail view reads the
``payload``'s ``nutriments`` / ``completeness`` / ``data_quality_warnings_tags``
/ ``last_modified_t`` as it already does for ``source='off'`` rows. A record
with no name, or with no nutrient numbers at all, is a miss: the barcode lane
has nothing to stage from it, and "not found — search instead" is more honest
than an empty row.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

_BASE = "https://world.openfoodfacts.org/api/v2"
_TIMEOUT = 15.0
_DEFAULT_USER_AGENT = "tally/0.1 (billzhangsc@gmail.com)"
_KCAL_PER_KJ = 1 / 4.184

# Identity, serving, the nutriment table and the §13 quality fields. The rest
# of a record (images, ingredient analysis, eco-score) is not nutrition data
# and would only bloat the cached payload.
_FIELDS = ",".join((
    "code", "product_name", "product_name_en", "generic_name", "brands",
    "brand_owner", "quantity", "serving_size", "serving_quantity",
    "serving_quantity_unit", "nutrition_data_per", "nutriments", "completeness",
    "last_modified_t", "data_quality_warnings_tags", "countries_tags",
    "no_nutrition_data",
))

# OFF nutriment ids -> our keys, with the factor from OFF's storage unit
# (grams; kcal for energy-kcal) to ours. The set mirrors what an FDC Branded
# label panel carries (fdc._LABEL_KEYS) plus the fat split.
_NUTRIENTS: dict[str, tuple[str, float]] = {
    "energy-kcal": ("kcal", 1),
    "proteins": ("protein_g", 1),
    "carbohydrates": ("carb_g", 1),
    "fat": ("fat_g", 1),
    "fiber": ("fiber_g", 1),
    "sugars": ("sugar_g", 1),
    "added-sugars": ("added_sugar_g", 1),
    "saturated-fat": ("saturated_fat_g", 1),
    "trans-fat": ("trans_fat_g", 1),
    "monounsaturated-fat": ("monounsaturated_fat_g", 1),
    "polyunsaturated-fat": ("polyunsaturated_fat_g", 1),
    "cholesterol": ("cholesterol_mg", 1000),
    "sodium": ("sodium_mg", 1000),
    "potassium": ("potassium_mg", 1000),
    "calcium": ("calcium_mg", 1000),
    "iron": ("iron_mg", 1000),
    "vitamin-d": ("vitamin_d_ug", 1_000_000),
}


def _user_agent() -> str:
    return os.environ.get("OFF_USER_AGENT") or _DEFAULT_USER_AGENT


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _nutrients(nutriments: dict, suffix: str) -> dict[str, float]:
    """Map ``<id>_<suffix>`` (``100g`` or ``serving``) to our keys, in our
    units. Absent nutrients stay absent — never 0. kcal falls back to the kJ
    energy field when a record carries only that."""
    out: dict[str, float] = {}
    for off_id, (ours, factor) in _NUTRIENTS.items():
        v = _num(nutriments.get(f"{off_id}_{suffix}"))
        if v is not None:
            out[ours] = round(v * factor, 2)
    if "kcal" not in out:
        kj = _num(nutriments.get(f"energy_{suffix}"))
        if kj is not None:
            out["kcal"] = round(kj * _KCAL_PER_KJ, 2)
    return out


# A metric size ("39g", "355 ml", "28.4 grams") as opposed to household text
# ("1 cup", "2 tbsp", "1 bar"), which is kept and gets the metric size appended
# the way an FDC label serving reads ("1 cup (39 g)").
_METRIC_SIZE = re.compile(r"[\d.,]+\s*(?:g|gr|grams?|ml|millilit(?:er|re)s?)", re.IGNORECASE)


def _servings(product: dict, per_100: dict[str, float]) -> tuple[list[dict], str]:
    """Label serving (OFF's per-serving figures, else per-100 scaled to the
    serving quantity) + the per-100 serving, the same two rows an FDC Branded
    record yields (fdc._branded_servings)."""
    nutriments = product.get("nutriments") or {}
    basis_ml = (product.get("nutrition_data_per") or "").strip().lower() == "100ml"
    unit = (product.get("serving_quantity_unit") or ("ml" if basis_ml else "g")).strip().lower()
    size = _num(product.get("serving_quantity"))
    quantity = size if size is not None and unit in ("g", "ml") and 0.5 <= size <= 2500 else None
    grams = quantity if unit == "g" else None

    macros = _nutrients(nutriments, "serving")
    if not macros and quantity is not None and per_100:
        macros = {k: round(v * quantity / 100, 2) for k, v in per_100.items()}

    text = (product.get("serving_size") or "").strip()
    if quantity is not None and (not text or _METRIC_SIZE.fullmatch(text)):
        description = f"{quantity:g} {unit}"
    elif text and quantity is not None and not _METRIC_SIZE.search(text):
        description = f"{text} ({quantity:g} {unit})"  # "1 cup" -> "1 cup (39 g)"
    elif text:
        description = text
    else:
        description = "1 serving"

    servings: list[dict] = []
    if macros:
        servings.append({
            "serving_id": "label",
            "description": description,
            "grams": grams,
            "macros": macros,
        })
    if per_100:
        servings.append({
            "serving_id": "100ml" if basis_ml else "100g",
            "description": "100 ml" if basis_ml else "100 g",
            "grams": None if basis_ml else 100.0,
            "macros": dict(per_100),
        })
    return servings, (servings[0]["serving_id"] if servings else "")


def normalize(product: dict, code: str) -> dict | None:
    """One OFF product record -> the shared food dict, or None when there is
    nothing to stage (no name, or no nutrient numbers at all)."""
    name = ""
    for key in ("product_name", "product_name_en", "generic_name"):
        candidate = str(product.get(key) or "").strip()
        if candidate:
            name = candidate
            break
    if not name:
        return None
    per_100 = _nutrients(product.get("nutriments") or {}, "100g")
    servings, default_id = _servings(product, per_100)
    if not servings:
        return None
    brands = [b.strip() for b in str(product.get("brands") or "").split(",") if b.strip()]
    brand = brands[0] if brands else (str(product.get("brand_owner") or "").strip() or None)
    return {
        "source": "off",
        "source_id": str(product.get("code") or code),
        "name": name,
        "display_name": name.lower(),
        "brand": brand,
        "food_type": "branded",
        "state": None,  # a packaged product's label is as-sold — no cooked/raw question
        "state_sensitive": False,
        "servings": servings,
        "default_serving_id": default_id,
        "per_100g": per_100 or None,
        "payload": product,
    }


async def lookup_barcode(code: str) -> dict | None:
    """The product for a scanned code, normalized — None on a miss."""
    digits = re.sub(r"\D", "", code or "")
    if not digits:
        return None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{_BASE}/product/{digits}",
            params={"fields": _FIELDS},
            headers={"User-Agent": _user_agent()},
        )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    body = r.json()
    if body.get("status") != 1 or not isinstance(body.get("product"), dict):
        return None
    return normalize(body["product"], digits)
