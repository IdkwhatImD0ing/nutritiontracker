"""§7.3 portion resolution against serving lists the ADAPTERS actually emit.

Issue #21's lesson: the suite's fixtures were all well-formed and the tests
were consistent with each other and wrong about the world. Every existing
`resolve_portion` fixture is a clean piece serving beside a clean mass serving
("1 large", "1 scoop", "100 g"), so three ways of getting a number wrong were
invisible. So these servings are built by `sources.fdc._generic_servings` /
`_branded_servings` from payloads shaped like the real provider records, not
typed out by hand — a fixture that drifts from what FDC returns can no longer
pass.

The invariant, and the reason this file exists:

    verify_me may be False only when the returned qty was DERIVED from the
    chosen serving's own parsed numbers — its gram weight for a mass unit, its
    parsed leading amount for a unit word, its piece count for a bare count.
    Never from a fallback, and never from a divisor of 1.0 that a failed parse
    produced.

Pure functions only: no database, no network, no model.

Run:  uv run --with pytest pytest tests/test_portion_shapes.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from resolver import cascade  # noqa: E402
from sources import fdc  # noqa: E402

PER_100 = {"kcal": 579.0, "protein_g": 21.2, "carb_g": 21.6, "fat_g": 49.9}  # almonds


def _row(servings: list[dict], default_id: str | None) -> dict:
    """The alias⋈food row shape `resolve_portion` reads, with no alias behind
    it — the spoken-quantity branch never touches the database."""
    return {
        "servings": servings,
        "food_default_serving_id": default_id,
        "default_serving_id": None,
        "default_qty": None,
        "alias_id": None,
        "food_id": None,
    }


def _resolve(row: dict, qty: float | None, unit: str | None):
    return asyncio.run(cascade.resolve_portion(row, qty, unit))


# ---------------------------------------------------------------------------
# the adapters' own output — these are the shapes the hand-written fixtures
# never had
# ---------------------------------------------------------------------------


def _generic_almonds() -> tuple[list[dict], str]:
    """An FDC Foundation/SR record whose foodPortions are volume measures
    carrying a preparation MODIFIER — `_portion_description` renders
    "{amount} {measureUnit} {modifier}", so the serving text is "1 cup whole".
    There is no piece portion: FDC does not publish "1 almond"."""
    return fdc._generic_servings(
        {
            "foodPortions": [
                {"id": 1, "amount": 1, "measureUnit": {"name": "cup"},
                 "modifier": "whole", "gramWeight": 143.0},
                {"id": 2, "amount": 1, "measureUnit": {"name": "cup"},
                 "modifier": "sliced", "gramWeight": 92.0},
            ]
        },
        PER_100,
    )


def _branded_beverage() -> tuple[list[dict], str]:
    """A branded drink measured in millilitres: `_branded_servings` sets
    grams=None on the label serving AND on the per-100 serving, because a
    volume record has no gram weight to convert into."""
    per_100 = {"kcal": 62.0, "protein_g": 6.5, "carb_g": 4.3, "fat_g": 1.8}
    return fdc._branded_servings(
        {
            "servingSize": 414,
            "servingSizeUnit": "MLT",
            "householdServingFullText": "1 bottle",
            "labelNutrients": {},
        },
        per_100,
    )


def _branded_granola() -> tuple[list[dict], str]:
    """A label whose household text is a FRACTION, the way cereal and granola
    boxes are actually printed. FDC passes householdServingFullText through
    verbatim, so the serving description reads "1/2 cup (55 g)"."""
    per_100 = {"kcal": 450.0, "protein_g": 10.0, "carb_g": 64.0, "fat_g": 18.0}
    return fdc._branded_servings(
        {
            "servingSize": 55,
            "servingSizeUnit": "G",
            "householdServingFullText": "1/2 cup",
            "labelNutrients": {},
        },
        per_100,
    )


# ---------------------------------------------------------------------------
# the invariant
# ---------------------------------------------------------------------------


def _derives_qty(serving: dict, spoken_qty: float, unit: str | None, out_qty: float) -> bool:
    """Could `out_qty` have come from this serving's OWN parsed numbers?"""
    desc = serving.get("description", "")
    grams = serving.get("grams")
    if unit:
        stem = cascade._unit_stem(unit)
        if stem in cascade._GRAMS_PER:
            if not isinstance(grams, (int, float)) or not grams > 0:
                return False
            return abs(out_qty - (spoken_qty * cascade._GRAMS_PER[stem]) / float(grams)) < 1e-6
        # a unit word only divides by a count the serving TEXT states
        return abs(out_qty - spoken_qty / cascade._units_per(desc, stem)) < 1e-6
    # a bare count needs a serving that counts pieces at all
    if not cascade._counts_pieces(desc):
        return False
    return abs(out_qty - spoken_qty / cascade._pieces_per(desc)) < 1e-6


def test_confidence_is_never_high_on_a_qty_the_serving_did_not_produce():
    """The one assertion. Every case below is a real adapter output crossed
    with something a person actually says."""
    almond_servings, almond_default = _generic_almonds()
    drink_servings, drink_default = _branded_beverage()
    granola_servings, granola_default = _branded_granola()

    cases = [
        # (label, servings, default_id, spoken qty, spoken unit)
        ("eight almonds — no piece serving exists", almond_servings, almond_default, 8, None),
        ("40 g of almonds", almond_servings, almond_default, 40, "g"),
        ("a cup of almonds", almond_servings, almond_default, 1, "cup"),
        ("eight ounces of the drink", drink_servings, drink_default, 8, "oz"),
        ("two of the drink", drink_servings, drink_default, 2, None),
        ("a quarter cup of granola", granola_servings, granola_default, 0.25, "cup"),
        ("30 g of granola", granola_servings, granola_default, 30, "g"),
    ]
    for label, servings, default_id, qty, unit in cases:
        sid, out_qty, verify = _resolve(_row(servings, default_id), qty, unit)
        if verify:
            continue  # a verify-me row is asking, which is always allowed
        chosen = cascade._serving_by_id(servings, sid)
        assert chosen is not None, label
        assert _derives_qty(chosen, qty, unit, out_qty), (
            f"{label}: staged {out_qty} × {chosen['description']!r} at confidence high, "
            f"but that number was not derived from the serving's own numbers"
        )


# ---------------------------------------------------------------------------
# the three specific ways it went wrong, so a regression names itself
# ---------------------------------------------------------------------------


def test_bare_count_against_a_measuring_serving_asks_instead_of_multiplying():
    """"eight almonds" against a record whose only household serving is
    "1 cup whole". `_is_count_serving` says True — "whole" is not a measure
    word — so the cup used to read as one countable piece and eight of them
    staged as eight CUPS (about 1.1 kg) at confidence high with no badge."""
    servings, default_id = _generic_almonds()
    assert [s["description"] for s in servings] == ["1 cup whole", "1 cup sliced", "100 g"]
    assert cascade._is_count_serving("1 cup whole") is True  # unchanged: still not a mass
    assert cascade._counts_pieces("1 cup whole") is False  # but it counts nothing

    sid, qty, verify = _resolve(_row(servings, default_id), 8, None)
    assert verify is True, "a bare count on a measuring serving must ask"


def test_a_fraction_in_the_serving_text_is_read_as_a_fraction():
    """"1/2 cup (55 g)" is what a granola label says. The leading-amount regex
    wanted digits then a space, so the match failed, the divisor fell back to
    1.0, and a quarter cup staged as a quarter of a HALF cup — half the food."""
    servings, default_id = _branded_granola()
    label = next(s for s in servings if s["serving_id"] == "label")
    assert label["description"] == "1/2 cup (55 g)"
    assert cascade._units_per(label["description"], "cup") == 0.5
    assert cascade._units_per("1 1/2 cups (85 g)", "cup") == 1.5

    sid, qty, verify = _resolve(_row(servings, default_id), 0.25, "cup")
    assert (sid, qty, verify) == ("label", 0.5, False)  # 0.5 × 55 g = 27.5 g


def test_a_millilitre_record_carries_no_gram_weight_on_any_serving():
    """Every serving of a ml-basis record has grams None, so a spoken mass has
    nothing to map onto. It may not stage as a confident count of bottles."""
    servings, default_id = _branded_beverage()
    assert all(s["grams"] is None for s in servings), servings

    sid, qty, verify = _resolve(_row(servings, default_id), 8, "oz")
    assert verify is True, "eight ounces is not eight bottles"


def test_a_real_piece_serving_still_stages_confidently():
    """The fixes must not make the common case ask. FDC's cooked shrimp
    serving is "4 large", so 50 shrimp are 12.5 servings — BACKLOG 151."""
    shrimp = [
        {"serving_id": "srv-4large", "description": "4 large", "grams": 22.0,
         "macros": {"kcal": 22, "protein_g": 5, "carb_g": 0, "fat_g": 0.3}},
        {"serving_id": "100g", "description": "100 g", "grams": 100.0,
         "macros": {"kcal": 99, "protein_g": 24, "carb_g": 0.2, "fat_g": 1.4}},
    ]
    assert cascade._counts_pieces("4 large") is True
    assert _resolve(_row(shrimp, "srv-4large"), 50, None) == ("srv-4large", 12.5, False)
