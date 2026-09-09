"""sources/off — the Open Food Facts barcode fallback.

The normalization tests run on fixtures cut from live records (2026-09-06:
Cheerios 0016000275287 per 100 g, a 355 ml Coca-Cola per 100 ml) — no
network, no DB. The live tests (-m live) hit the real endpoint; OFF is free,
so they cost nothing but a request against its per-IP read limit.

Run:  uv run --project backend --with pytest pytest backend/tests/test_off.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sources import off  # noqa: E402

CHEERIOS = {
    "code": "0016000275287",
    "product_name": "Cheerios",
    "product_name_en": "My Bff",
    "brands": "Cheerios",
    "brand_owner": "GENERAL MILLS SALES INC.",
    "serving_size": "39g",
    "serving_quantity": 39,
    "serving_quantity_unit": "g",
    "nutrition_data_per": "100g",
    "completeness": 0.75,
    "last_modified_t": 1772637584,
    "data_quality_warnings_tags": ["en:ingredients-90-percent-unknown"],
    "nutriments": {
        "energy-kcal_100g": 358.974358974359,
        "energy-kcal_serving": 140,
        "energy_100g": 1801.28205128205,
        "proteins_100g": 12.8205128205128,
        "proteins_serving": 5,
        "carbohydrates_100g": 74.3589743589744,
        "carbohydrates_serving": 29,
        "fat_100g": 6.41025641025641,
        "fat_serving": 2.5,
        "fiber_100g": 10.2564102564103,
        "fiber_serving": 4,
        "sugars_100g": 5.12820512820513,
        "sugars_serving": 2,
        "sodium_100g": 0.487179487179487,
        "sodium_serving": 0.19,
        "sodium_unit": "g",
        "salt_100g": 1.21794871794872,  # not ours — must not leak in
        "nova-group_100g": 1,  # not a nutrient
    },
}

COKE_355ML = {
    "code": "0049000006346",
    "product_name": "Coca cola can cokes LG",
    "brands": "Coca cola",
    "serving_size": "355ml",
    "serving_quantity": 355,
    "serving_quantity_unit": "ml",
    "nutrition_data_per": "100ml",
    "nutriments": {
        "energy-kcal_100g": 39.4366197183099,
        "energy-kcal_serving": 140,
        "carbohydrates_100g": 10.9859154929577,
        "carbohydrates_serving": 39,
        "fat_100g": 0,
        "fat_serving": 0,
        "proteins_100g": 0,
        "proteins_serving": 0,
        "sugars_100g": 10.9859154929577,
        "sodium_100g": 0.0126760563380282,
    },
}


def test_normalize_cheerios_maps_both_bases_in_our_units():
    food = off.normalize(CHEERIOS, "016000275287")
    assert food is not None
    assert food["source"] == "off" and food["source_id"] == "0016000275287"
    assert food["name"] == "Cheerios" and food["display_name"] == "cheerios"
    assert food["brand"] == "Cheerios"
    assert food["food_type"] == "branded" and food["state"] is None
    assert food["payload"] is CHEERIOS  # the detail view reads completeness etc. off it

    per_100 = food["per_100g"]
    assert per_100["kcal"] == 358.97 and per_100["protein_g"] == 12.82
    assert per_100["carb_g"] == 74.36 and per_100["fat_g"] == 6.41
    assert per_100["fiber_g"] == 10.26 and per_100["sugar_g"] == 5.13
    assert per_100["sodium_mg"] == 487.18  # grams -> mg
    assert "salt_g" not in per_100 and not any("nova" in k for k in per_100)

    label, hundred = food["servings"]
    assert food["default_serving_id"] == "label"
    assert label["serving_id"] == "label" and label["description"] == "39 g"
    assert label["grams"] == 39.0
    # OFF's own per-serving figures, not a per-100 rescale
    assert label["macros"]["kcal"] == 140 and label["macros"]["protein_g"] == 5
    assert label["macros"]["sodium_mg"] == 190
    assert hundred["serving_id"] == "100g" and hundred["grams"] == 100.0
    assert hundred["macros"] == per_100


def test_normalize_beverage_is_per_100ml_with_no_gram_weight():
    food = off.normalize(COKE_355ML, "049000006346")
    assert food is not None
    label, hundred = food["servings"]
    assert label["description"] == "355 ml" and label["grams"] is None
    assert label["macros"]["kcal"] == 140 and label["macros"]["carb_g"] == 39
    assert hundred["serving_id"] == "100ml" and hundred["description"] == "100 ml"
    assert hundred["grams"] is None
    assert food["per_100g"]["sodium_mg"] == 12.68


def test_normalize_household_serving_text_is_kept():
    def description(serving_size: str) -> str:
        food = off.normalize(dict(CHEERIOS, serving_size=serving_size), "016000275287")
        return food["servings"][0]["description"]

    assert description("1 cup (39 g)") == "1 cup (39 g)"  # already carries the size
    assert description("1 cup") == "1 cup (39 g)"  # household text kept, size appended
    assert description("2 tbsp") == "2 tbsp (39 g)"
    assert description("39g") == "39 g"  # a bare metric size is normalized
    assert description("39 grams") == "39 g"
    assert description("") == "39 g"


def test_normalize_scales_per_100_when_off_has_no_serving_figures():
    nutriments = {k: v for k, v in CHEERIOS["nutriments"].items() if not k.endswith("_serving")}
    food = off.normalize(dict(CHEERIOS, nutriments=nutriments), "016000275287")
    label = food["servings"][0]
    assert label["macros"]["kcal"] == round(358.974358974359 * 0.39, 2)
    assert label["macros"]["protein_g"] == round(12.8205128205128 * 0.39, 2)


def test_normalize_kcal_falls_back_to_kj_energy():
    nutriments = {k: v for k, v in CHEERIOS["nutriments"].items() if "kcal" not in k}
    food = off.normalize(dict(CHEERIOS, nutriments=nutriments), "016000275287")
    assert food["per_100g"]["kcal"] == round(1801.28205128205 / 4.184, 2)


def test_normalize_absurd_or_unknown_serving_quantity_is_dropped():
    food = off.normalize(dict(CHEERIOS, serving_quantity=68000), "016000275287")
    label = food["servings"][0]
    assert label["grams"] is None and label["description"] == "39g"  # the text stands
    assert label["macros"]["kcal"] == 140  # OFF's serving figures still apply


def test_normalize_misses_without_a_name_or_without_numbers():
    assert off.normalize({"code": "1", "nutriments": {"energy-kcal_100g": 1}}, "1") is None
    assert off.normalize({"code": "1", "product_name": "Mystery", "nutriments": {}}, "1") is None
    # a name with only non-nutrient nutriments is still numberless
    assert off.normalize(
        {"code": "1", "product_name": "Mystery", "nutriments": {"nova-group_100g": 4}}, "1"
    ) is None


def test_normalize_brand_is_the_first_listed_else_the_owner():
    assert off.normalize(dict(CHEERIOS, brands="General Mills, Cheerios"), "1")["brand"] == "General Mills"
    assert off.normalize(dict(CHEERIOS, brands=""), "1")["brand"] == "GENERAL MILLS SALES INC."
    assert off.normalize(dict(CHEERIOS, brands="", brand_owner=None), "1")["brand"] is None


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_lookup_normalizes_the_scanned_code_and_reads_cheerios():
    """A 12-digit UPC-A scan; OFF stores the record 13-digit."""
    food = asyncio.run(off.lookup_barcode("016000275287"))
    assert food is not None
    assert food["source"] == "off" and food["source_id"] == "0016000275287"
    assert "cheerios" in food["display_name"]
    assert 340 <= food["per_100g"]["kcal"] <= 380
    assert food["payload"].get("completeness") is not None


@pytest.mark.live
def test_live_lookup_miss_is_none():
    assert asyncio.run(off.lookup_barcode("0000000000000")) is None


# ---------------------------------------------------------------------------
# BACKLOG 173 — a scanned product with no core macros is a miss, not a card
# ---------------------------------------------------------------------------


def test_normalize_refuses_a_product_with_no_loggable_serving():
    """OFF is crowd-sourced, so a partial nutriment table is common. Staging one
    renders a card with no numbers that then refuses at commit (the FDC 2756792
    story, BACKLOG 169) — a clean miss is better, because it falls through to
    Open Food Facts' sibling lanes and then manual entry."""
    base = {"code": "0000000000001", "product_name": "Mystery Yogurt",
            "brands": "Nobody"}

    thin = dict(base, nutriments={"energy-kcal_100g": 60, "proteins_100g": 10})
    assert off.normalize(thin, "0000000000001") is None  # no carb, no fat

    full = dict(base, nutriments={"energy-kcal_100g": 60, "proteins_100g": 10,
                                  "carbohydrates_100g": 5, "fat_100g": 0})
    food = off.normalize(full, "0000000000001")
    assert food is not None
    assert food["servings"], "a complete product still stages"
    assert any(s["serving_id"] == food["default_serving_id"] for s in food["servings"])


def test_normalize_repoints_the_default_when_that_serving_is_dropped():
    """A serving-size serving with no numbers must not stay the default just
    because the provider listed it first."""
    product = {
        "code": "0000000000002", "product_name": "Half Labelled Bar",
        "brands": "Nobody", "serving_size": "40 g",
        # per-serving values absent, per-100g complete: the serving-size
        # serving is scaled from per-100g, so both survive here — assert the
        # default is one that actually carries the four
        "nutriments": {"energy-kcal_100g": 400, "proteins_100g": 20,
                       "carbohydrates_100g": 40, "fat_100g": 15},
    }
    food = off.normalize(product, "0000000000002")
    assert food is not None
    default = next(s for s in food["servings"] if s["serving_id"] == food["default_serving_id"])
    for key in ("kcal", "protein_g", "carb_g", "fat_g"):
        assert isinstance(default["macros"].get(key), (int, float)), key
