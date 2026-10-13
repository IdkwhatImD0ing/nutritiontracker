"""The café menu reader and matcher (DESIGN.md §3.4b, 2026-09-30).

sources/bonappetit.py reads a Bon Appétit day page's two script literals;
services/menu_match.py keeps only the picks that name a real item and a real
dish. Pure tests on a small hand-made page, the fetch through a MockTransport,
and one live parse of the real Pinterest SF page (free: a plain GET).

Run:  uv run --project backend --with pytest pytest backend/tests/test_bonappetit.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from tracing_stub import make_tracing_stub

from services import menu_match
from services.vision import VisionItem
from sources import bonappetit

PT = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _no_traces(monkeypatch):
    """The real tracing module sends with the .env's key; a test trace must
    never reach FireTrace (it keeps them forever)."""
    stub = make_tracing_stub()
    monkeypatch.setitem(sys.modules, "tracing", stub)  # bonappetit imports it lazily
    monkeypatch.setattr(menu_match, "tracing", stub)


def _panel(kcal="310", protein="28", carb="4", fat="20", oz="5.0", **extra):
    fields = {
        "calories": (kcal, ""), "proteinContent": (protein, "g"),
        "carbohydrateContent": (carb, "g"), "fatContent": (fat, "g"),
        "sodiumContent": ("640", "mg"), "fiberContent": ("< 1", "g"),
    }
    fields.update(extra)
    panel = {k: {"label": k, "value": v, "unit": u} for k, (v, u) in fields.items() if v is not None}
    if oz is not None:
        panel["servingSize"] = {"label": "Serving Size", "value": oz, "unit": "oz"}
    return panel


ITEMS = {
    "101": {"label": "herb roasted chicken", "station": "<strong>@Home</strong>",
            "description": "with lemon &amp; thyme", "nutrition_details": _panel()},
    "102": {"label": "cucumber", "station": "<strong>@Salad Bar</strong>", "description": "",
            "nutrition_details": _panel(kcal="5", protein="< 1", carb="1", fat="0", oz="1.0")},
    "103": {"label": "chef's special", "station": "@Home", "description": "",
            # protein unpublished: kept (it can still be recognized), no numbers
            "nutrition_details": _panel(protein=None)},
    "104": {"label": "scrambled eggs", "station": "@Rise", "description": "",
            "nutrition_details": _panel(kcal="160", protein="11", carb="1", fat="12", oz="3.0")},
    "105": {"label": "", "station": "@Home", "nutrition_details": []},  # nameless: dropped
    "106": {"label": "espresso", "station": "@Coffee", "nutrition_details": []},  # PHP's empty {}
}
DAYPARTS = [
    {"label": "Breakfast", "starttime": "08:15", "endtime": "09:45",
     "stations": [{"items": ["104"]}]},
    {"label": "Lunch", "starttime": "11:45", "endtime": "13:30",
     "stations": [{"items": ["101", "102"]}, {"items": ["103", "105", "106", "101", "999"]}]},
]


def page(items=ITEMS, dayparts=DAYPARTS) -> str:
    """A day page as the café ships it: the literals inside one script tag."""
    out = ["<html><script>", "Bamco.menu_items_nonce = 'ae61a6931e';"]
    if items is not None:
        out.append(f"Bamco.menu_items = {json.dumps(items)};")
    out += [f"Bamco.dayparts['{i}'] = {json.dumps(p)};" for i, p in enumerate(dayparts, 1)]
    return "\n".join(out + ["</script></html>"])


def test_site_in_reads_the_cafe_the_notes_name():
    assert bonappetit.site_in("lunch menu at https://pinterest-sf.cafebonappetit.com/") == "pinterest-sf"
    assert bonappetit.site_in("weekday lunches: Pinterest-SF.cafebonappetit.com") == "pinterest-sf"
    assert bonappetit.site_in("I eat at the office cafeteria") is None
    assert bonappetit.site_in("") is None and bonappetit.site_in(None) is None


def test_parse_reads_each_meal_and_its_published_numbers():
    meals = bonappetit.parse(page())
    assert [(label, opens, closes) for label, opens, closes, _ in meals] == [
        ("Breakfast", time(8, 15), time(9, 45)), ("Lunch", time(11, 45), time(13, 30)),
    ]
    lunch = meals[1][3]
    # page order, each dish once, nameless and unknown ids dropped
    assert [d.id for d in lunch] == ["101", "102", "103", "106"]
    chicken, cucumber, special, espresso = lunch
    assert (chicken.name, chicken.station, chicken.description) == (
        "herb roasted chicken", "Home", "with lemon & thyme",
    )
    assert chicken.serving == "5.0 oz" and chicken.grams == pytest.approx(141.7, abs=0.05)
    assert chicken.nutrients == {
        "kcal": 310, "protein_g": 28, "carb_g": 4, "fat_g": 20,
        "sodium_mg": 640, "fiber_g": 0.5,  # "< 1" is half the bound
    }
    assert cucumber.nutrients["protein_g"] == 0.5 and cucumber.grams == pytest.approx(28.3, abs=0.05)
    # a dish missing a core macro is still on the menu, with no numbers
    assert special.name == "chef's special" and special.nutrients == {}
    # an empty panel ([] from PHP) reads as no numbers and no serving
    assert espresso.nutrients == {} and espresso.grams is None and espresso.serving == ""


def test_parse_refuses_a_number_in_the_wrong_unit():
    items = {"1": {"label": "soup", "station": "@Soup",
                   "nutrition_details": _panel(proteinContent=("9", "mg"))}}
    [(_, _, _, [soup])] = bonappetit.parse(page(items, [DAYPARTS[1] | {"stations": [{"items": ["1"]}]}]))
    assert soup.nutrients == {}  # protein not in grams → not all four core → none


def test_a_closed_day_has_no_meals():
    """A weekend page ships no menu_items literal at all."""
    assert bonappetit.parse(page(items=None, dayparts=[])) == []
    assert bonappetit.parse("<html>no menu</html>") == []


def test_meal_for_takes_the_meal_whose_hours_the_photo_falls_in():
    meals = bonappetit.parse(page())

    def at(h, m):
        return datetime(2026, 9, 30, h, m, tzinfo=PT)

    # lunch 11:45–13:30, taken up to 30 min early and 60 min late
    for t in (at(11, 15), at(12, 30), at(14, 30)):
        assert bonappetit.meal_for(meals, t)[0] == "Lunch", t
    assert bonappetit.meal_for(meals, at(11, 14)) is None
    assert bonappetit.meal_for(meals, at(14, 31)) is None
    assert bonappetit.meal_for(meals, at(9, 0))[0] == "Breakfast"
    assert bonappetit.meal_for(meals, at(19, 0)) is None
    # overlapping windows: the meal that opens latest (a brunch after breakfast)
    brunch = [("Breakfast", time(8, 15), time(9, 45), meals[0][3]),
              ("Brunch", time(9, 30), time(13, 0), meals[1][3])]
    assert bonappetit.meal_for(list(reversed(brunch)), at(9, 40))[0] == "Brunch"
    assert bonappetit.meal_for(brunch, at(8, 0))[0] == "Breakfast"


def _serve(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        bonappetit, "_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=bonappetit._TIMEOUT_S),
    )


def test_meal_at_fetches_the_day_page_and_answers_the_meal(monkeypatch):
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, text=page())

    _serve(monkeypatch, handler)
    meal = asyncio.run(bonappetit.meal_at("pinterest-sf", datetime(2026, 9, 30, 12, 20, tzinfo=PT)))
    assert asked == ["https://pinterest-sf.cafebonappetit.com/cafe/2026-09-30/"]
    assert meal.url == asked[0] and meal.site == "pinterest-sf" and meal.day == date(2026, 9, 30)
    assert meal.label == "Lunch" and [d.id for d in meal.dishes] == ["101", "102", "103", "106"]
    # outside every meal's hours: nothing
    assert asyncio.run(bonappetit.meal_at("pinterest-sf", datetime(2026, 9, 30, 19, 0, tzinfo=PT))) is None


@pytest.mark.parametrize("respond", [
    lambda r: httpx.Response(403, text="no"),
    lambda r: httpx.Response(200, text="Bamco.menu_items = {broken"),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
def test_meal_at_answers_none_on_any_failure(monkeypatch, respond):
    _serve(monkeypatch, respond)
    assert asyncio.run(bonappetit.meal_at("pinterest-sf", datetime(2026, 9, 30, 12, 0, tzinfo=PT))) is None


def test_meal_at_has_a_total_time_cap(monkeypatch):
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, text=page())

    _serve(monkeypatch, slow)
    monkeypatch.setattr(bonappetit, "_TIMEOUT_S", 0.05)
    assert asyncio.run(bonappetit.meal_at("pinterest-sf", datetime(2026, 9, 30, 12, 0, tzinfo=PT))) is None


# ---------------------------------------------------------------------------
# services/menu_match — the model's picks, checked in code
# ---------------------------------------------------------------------------


def _vitem(phrase, est_grams=None, note=""):
    return VisionItem(phrase=phrase, qty=None, unit=None, est_grams=est_grams,
                      confidence="high", note=note, menu=None)


def _meal():
    [_, (_, _, _, dishes)] = bonappetit.parse(page())
    return bonappetit.CafeMeal(url="https://x.cafebonappetit.com/cafe/2026-09-30/",
                               site="x", day=date(2026, 9, 30), label="Lunch", dishes=dishes)


def test_the_matcher_is_shown_names_and_never_numbers():
    text = menu_match.ask([_vitem("roast chicken", 150, "two slices of roast chicken")], _meal())
    assert "0. roast chicken (~150 g) - two slices of roast chicken" in text
    assert "101 | Home | herb roasted chicken | with lemon & thyme" in text
    assert "310" not in text and "kcal" not in text  # the dish's own numbers stay out


def test_only_picks_naming_a_real_item_and_a_real_dish_survive():
    items = [_vitem("chicken"), _vitem("cucumber"), _vitem("rice")]
    picks = menu_match.Picks(picks=[
        menu_match.Pick(item_index=0, dish_id="101"),
        menu_match.Pick(item_index=0, dish_id="102"),  # a second entry for one item: the first wins
        menu_match.Pick(item_index=1, dish_id="777"),  # not on this menu
        menu_match.Pick(item_index=2, dish_id=None),
        menu_match.Pick(item_index=9, dish_id="102"),  # no such item
    ])
    found = menu_match.picked(picks, items, _meal())
    assert {i: d.id for i, d in found.items()} == {0: "101"}


def test_a_failed_match_answers_none(monkeypatch):
    class Broken:
        class responses:
            @staticmethod
            async def parse(**kw):
                assert kw["model"] == "gpt-5.6-luna" and kw["reasoning"] == {"effort": "low"}
                raise RuntimeError("api down")

    monkeypatch.setattr(menu_match, "_get_client", lambda: Broken)
    assert asyncio.run(menu_match.match([_vitem("chicken")], _meal())) is None


def test_a_match_returns_the_checked_picks(monkeypatch):
    class Ok:
        class responses:
            @staticmethod
            async def parse(**kw):
                assert kw["text_format"] is menu_match.Picks
                return types.SimpleNamespace(
                    output_parsed=menu_match.Picks(picks=[menu_match.Pick(item_index=0, dish_id="101")]),
                    usage=None,
                )

    monkeypatch.setattr(menu_match, "_get_client", lambda: Ok)
    found = asyncio.run(menu_match.match([_vitem("chicken")], _meal()))
    assert found[0].name == "herb roasted chicken"


# ---------------------------------------------------------------------------
# live — the real page (a plain GET, no spend)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_the_real_pinterest_page_parses():
    """The most recent weekday's lunch at the Pinterest SF café: the literals
    are still there and still carry the four core macros."""
    day = datetime.now(PT).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    noon = datetime.combine(day, time(12, 30), tzinfo=PT)
    meal = asyncio.run(bonappetit.meal_at("pinterest-sf", noon))
    assert meal is not None, f"no lunch read for {day}"
    assert meal.label in ("Lunch", "Brunch") and len(meal.dishes) > 10
    with_numbers = [d for d in meal.dishes if d.nutrients]
    assert len(with_numbers) >= 0.9 * len(meal.dishes)
    assert all(d.grams and d.serving.endswith("oz") for d in with_numbers)
