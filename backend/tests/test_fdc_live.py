"""Live tests for the FDC source adapter (DESIGN.md §3.4, §8.2).

These hit api.nal.usda.gov for real (FDC_API_KEY from env/.env) and are
rate-limit-kind: ~10 requests total, well inside the 1,000 req/hr key limit,
with a short pause between tests.

fdcIds pinned here were verified live 2026-08-20:
- 2742751  Core Power Protein Chocolate Elite 42g, 14 fl oz bottle (GDSN,
           gtinUpc 00811620020633, servingSize 414 MLT, label: 232 kcal / 42 g
           protein; per-100ml foodNutrients: 56 kcal / 10.14 g protein)
- 2646170  Foundation "Chicken, breast, boneless, skinless, raw" — carries NO
           nutrient 1008; energy only via Atwater ids 2047/2048.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ on path

from sources import fdc  # noqa: E402

pytestmark = pytest.mark.live

CORE_POWER_FDC_ID = "2742751"
CORE_POWER_GTIN = "00811620020633"
FOUNDATION_CHICKEN_FDC_ID = "2646170"


@pytest.fixture(autouse=True)
def _rate_limit_pause():
    yield
    time.sleep(0.5)


def test_search_generic_returns_hits():
    hits = asyncio.run(fdc.search_generic("chicken breast cooked"))
    assert hits, "expected hits for 'chicken breast cooked'"
    for h in hits:
        assert set(h) == {"fdc_id", "description", "data_type", "protein_per_100g"}
        assert h["data_type"] in ("SR Legacy", "Foundation")
    assert any("breast" in h["description"].lower() for h in hits)
    assert any(h["protein_per_100g"] is not None for h in hits)


def test_barcode_ladder_both_forms_resolve():
    """As-scanned 14-digit AND leading-zero-stripped 12-digit must both land.

    The 12-digit form gets zero hits as-scanned (verified live) and only
    resolves via the zero-pad-to-14 rung — this is the ladder proof.
    """
    as_scanned = asyncio.run(fdc.lookup_barcode(CORE_POWER_GTIN))
    stripped = asyncio.run(fdc.lookup_barcode(CORE_POWER_GTIN.lstrip("0")))
    assert as_scanned is not None, "14-digit as-scanned barcode did not resolve"
    assert stripped is not None, "zero-stripped barcode did not resolve (ladder broken)"
    assert as_scanned["source_id"] == stripped["source_id"] == CORE_POWER_FDC_ID
    assert as_scanned["food_type"] == "branded"


def test_get_food_core_power_label_serving_and_per_100():
    food = asyncio.run(fdc.get_food("fdc", CORE_POWER_FDC_ID))
    assert food is not None
    assert food["source"] == "fdc" and food["source_id"] == CORE_POWER_FDC_ID
    assert food["food_type"] == "branded"
    assert food["brand"]
    assert food["payload"]["dataType"] == "Branded"

    label = next(s for s in food["servings"] if s["serving_id"] == "label")
    assert food["default_serving_id"] == "label"
    assert "bottle" in label["description"].lower()
    # per-bottle: label says 232 kcal / 42 g; per-100ml x 4.14 gives 231.8 / 41.98
    assert 225 <= label["macros"]["kcal"] <= 240
    assert 41 <= label["macros"]["protein_g"] <= 43
    assert "carb_g" in label["macros"] and "fat_g" in label["macros"]

    # both bases normalized: per_100g present from foodNutrients (per 100 ml here)
    per_100 = food["per_100g"]
    assert per_100 is not None
    assert 50 <= per_100["kcal"] <= 62
    assert 9 <= per_100["protein_g"] <= 11
    # per-100 basis also exposed as a serving
    assert any(s["serving_id"] in ("100ml", "100g") for s in food["servings"])


def test_foundation_food_energy_via_atwater_fallback():
    food = asyncio.run(fdc.get_food("fdc", FOUNDATION_CHICKEN_FDC_ID))
    assert food is not None
    assert food["payload"]["dataType"] == "Foundation"
    # record carries no nutrient 1008 — kcal must come from Atwater ids
    ids_present = {
        (n.get("nutrient") or {}).get("id")
        for n in food["payload"]["foodNutrients"]
        if n.get("amount") is not None
    }
    assert 1008 not in ids_present and (2047 in ids_present or 2048 in ids_present)
    assert food["per_100g"] is not None
    assert 100 <= food["per_100g"]["kcal"] <= 120  # Atwater: 106.0 / 112.2
    assert 20 <= food["per_100g"]["protein_g"] <= 25
    # generic normalization: 100 g serving exists, state parsed from name
    assert any(s["serving_id"] == "100g" for s in food["servings"])
    assert food["food_type"] == "generic"
    assert food["state"] == "raw"
    assert food["state_sensitive"] is True


def test_get_food_tolerates_stale_fdcid_404():
    assert asyncio.run(fdc.get_food("fdc", "9999999")) is None


def test_search_branded_carries_brand_owner():
    hits = asyncio.run(fdc.search_branded("core power elite chocolate"))
    assert hits
    assert any((h["brand_owner"] or "").lower().startswith("coca-cola") for h in hits)
    top = hits[0]
    assert {"fdc_id", "description", "brand_owner", "published_date"} <= set(top)
