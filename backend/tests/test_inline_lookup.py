"""Every row is looked up before the chat answers (DESIGN.md §3.4, 2026-09-11).

Bill: "the agent should make the list using its tools then I just approve" —
trace 30e72425 had the agent reply "I logged tonkatsu" over a row that was
still `searching`, and the phone started tier 3 afterwards. routes/logs.
_resolve_inline runs tier 3 inside the request instead, under the request's
clock (deadline.py; vercel.json maxDuration is 120 s):

1. the searching rows are looked up and put back by item_index; other rows
   are untouched;
2. a lookup the clock cannot fit is not started, one the quota refuses or
   that fails leaves its row searching (the phone's kick is the fallback);
3. at most _INLINE_PARALLEL run at once, each with the time the request can
   spare as its tier-3 budget;
4. off when TALLY_INLINE_TIER3=0 (the suite's default, conftest.py).

A saved meal's words get the same treatment (_lookup_meal_words, 2026-09-19):
a food the person has never logged had no way into a meal at all before, so
the words tiers 1–2 cannot place now go to tier 3 under the same clock, the
same four-at-a-time gate and the same off switch — through
tier3.resolve_phrase, which caches the food and writes no row.

A word item also comes back with its NUMBERS and its runners-up
(_describe_meal_lookups, 2026-09-20): a saved-meal item is three ids and
Library renders a name and a portion, so without this nothing says what the
lookup chose between. The alternates are the ones the search already
returned; each is read once and one that cannot be logged is dropped rather
than offered.

Offline: routes.resolve.kick_row, services.tier3.resolve_phrase, the FDC
adapter and the foods reads are faked — no model, no database.

Run:  cd backend && uv run --with pytest pytest tests/test_inline_lookup.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import HTTPException  # noqa: E402

import deadline  # noqa: E402
from models import FoodRef, Meal, StagedItem  # noqa: E402
from routes import logs as logs_routes  # noqa: E402


def _patch_kick(monkeypatch, fake) -> None:
    """Fake the module _resolve_inline will import at call time (other test
    files re-import routes.resolve under their own stubs)."""
    import routes.resolve as rr

    monkeypatch.setattr(rr, "kick_row", fake)


def _row(idx: int, status: str = "searching") -> StagedItem:
    return StagedItem(item_index=idx, phrase=f"food {idx}", status=status, meal=Meal.lunch)


def _found(idx: int) -> StagedItem:
    return StagedItem(item_index=idx, phrase=f"food {idx}", status="resolved", tier=3,
                      food=FoodRef(id=str(uuid4()), name=f"found {idx}"), meal=Meal.lunch)


def test_rows_are_looked_up_in_place_and_failures_stay_searching(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
    budgets: dict[int, float | None] = {}

    async def kick_row(log_id, item_index, *, claim=True):
        assert claim is False  # the phone's two tries stay untouched
        budgets[item_index] = deadline.tier3_budget.get()
        if item_index == 2:
            raise HTTPException(status_code=429, detail="demo quota")
        if item_index == 3:
            raise RuntimeError("provider down")
        return _found(item_index)

    _patch_kick(monkeypatch, kick_row)

    async def scenario():
        deadline.start(100.0)
        rows = [_row(0, "resolved"), _row(1), _row(2), _row(3)]
        out = await logs_routes._resolve_inline(uuid4(), rows, reserve=20.0)
        assert [r.status for r in out] == ["resolved", "resolved", "searching", "searching"]
        assert out[0] is rows[0] and out[1].food.name == "found 1"
        assert out[2] is rows[2] and out[3] is rows[3]  # left for the phone's kick
        assert set(budgets) == {1, 2, 3}  # the resolved row was never looked up
        # the budget: the cap, since 100 s − the 20 s reserve is more
        assert budgets[1] == logs_routes._INLINE_CAP_S
        # no clock leaks out of a lookup's task
        assert deadline.tier3_budget.get() is None

    asyncio.run(scenario())


def test_no_lookup_starts_when_the_clock_cannot_fit_one(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
    calls: list[int] = []

    async def kick_row(log_id, item_index, *, claim=True):
        calls.append(item_index)
        return _found(item_index)

    _patch_kick(monkeypatch, kick_row)

    async def scenario():
        # 25 s left, 20 s reserved for the agent's closing turn: 5 s < the minimum
        deadline.start(25.0)
        rows = [_row(0), _row(1)]
        assert await logs_routes._resolve_inline(uuid4(), rows, reserve=20.0) == rows
        assert calls == []
        # with more time the budget shrinks to what the request can spare
        deadline.start(45.0)
        seen = []

        async def budgeted(log_id, item_index, *, claim=True):
            seen.append(deadline.tier3_budget.get())
            return _found(item_index)

        _patch_kick(monkeypatch, budgeted)
        await logs_routes._resolve_inline(uuid4(), [_row(0)], reserve=20.0)
        assert seen and 20.0 < seen[0] <= 25.0

    asyncio.run(scenario())


def test_at_most_four_lookups_run_at_once(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
    running = peak = 0

    async def kick_row(log_id, item_index, *, claim=True):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return _found(item_index)

    _patch_kick(monkeypatch, kick_row)

    async def scenario():
        deadline.start(100.0)
        rows = [_row(i) for i in range(7)]
        out = await logs_routes._resolve_inline(uuid4(), rows, reserve=20.0)
        assert all(r.status == "resolved" for r in out)
        assert [r.item_index for r in out] == list(range(7))
        assert peak == logs_routes._INLINE_PARALLEL

    asyncio.run(scenario())


def test_off_switch_leaves_every_row_as_it_is(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "0")

    async def kick_row(*a, **kw):
        raise AssertionError("inline lookups are off")

    _patch_kick(monkeypatch, kick_row)
    rows = [_row(0), _row(1)]
    assert asyncio.run(logs_routes._resolve_inline(uuid4(), rows, reserve=20.0)) == rows


def _patch_resolve_phrase(monkeypatch, fake) -> None:
    """Fake the module _lookup_meal_words imports at call time."""
    from services import tier3

    monkeypatch.setattr(tier3, "resolve_phrase", fake)


def _word(phrase: str, qty: float | None = None, unit: str | None = None):
    from services.agent import MealFood

    return MealFood(phrase=phrase, qty=qty, unit=unit, food_id=None, serving_id=None)


def test_meal_words_tier_three_fills_in_the_ones_tiers_one_and_two_missed(monkeypatch):
    monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
    asked: dict[str, tuple] = {}

    async def resolve_phrase(phrase, meal, qty=None, unit=None):
        asked[phrase] = (meal, qty, unit, deadline.tier3_budget.get())
        if phrase == "dashi broth":
            raise RuntimeError("provider down")
        return StagedItem(item_index=0, phrase=phrase, status="resolved", tier=3,
                          food=FoodRef(id=str(uuid4()), name=phrase), meal=Meal.lunch)

    _patch_resolve_phrase(monkeypatch, resolve_phrase)

    async def scenario():
        deadline.start(100.0)
        items = [_word("tofu"), _word("miso paste", 1.0, "tbsp"), _word("dashi broth")]
        staged = {0: _row(0, "resolved"), 1: _row(1), 2: _row(2)}
        await logs_routes._lookup_meal_words(items, staged, Meal.lunch)
        # the row tier 1 already placed is never looked up; the failure stands
        assert set(asked) == {"miso paste", "dashi broth"}
        assert [staged[i].status for i in (0, 1, 2)] == ["resolved", "resolved", "searching"]
        assert staged[1].food.name == "miso paste"
        assert asked["miso paste"] == (Meal.lunch, 1.0, "tbsp", logs_routes._INLINE_CAP_S)
        assert deadline.tier3_budget.get() is None  # no clock leaks out of a task

    asyncio.run(scenario())


def test_meal_words_respect_the_clock_the_quota_and_the_off_switch(monkeypatch):
    calls: list[str] = []

    async def resolve_phrase(phrase, meal, qty=None, unit=None):
        calls.append(phrase)
        raise HTTPException(status_code=429, detail="demo quota")

    _patch_resolve_phrase(monkeypatch, resolve_phrase)

    async def scenario():
        monkeypatch.setenv("TALLY_INLINE_TIER3", "1")
        # 25 s left, 20 s reserved for the agent's closing turn: under the minimum
        deadline.start(25.0)
        staged = {0: _row(0)}
        await logs_routes._lookup_meal_words([_word("miso paste")], staged, Meal.lunch)
        assert calls == [] and staged[0].status == "searching"
        # a refused lookup leaves its word for the caller to report, not an error
        deadline.start(100.0)
        await logs_routes._lookup_meal_words([_word("miso paste")], staged, Meal.lunch)
        assert calls == ["miso paste"] and staged[0].status == "searching"
        # and nothing runs at all with the switch off
        monkeypatch.setenv("TALLY_INLINE_TIER3", "0")
        await logs_routes._lookup_meal_words([_word("miso paste")], staged, Meal.lunch)
        assert calls == ["miso paste"]

    asyncio.run(scenario())


def _food(kcal=None, *, serving_id="100g", description="100 g", default="100g"):
    macros = {} if kcal is None else {
        "kcal": kcal, "protein_g": 1.0, "carb_g": 2.0, "fat_g": 0.5
    }
    return {
        "display_name": "a food",
        "brand": "A Brand",
        "default_serving_id": default,
        "servings": [{"serving_id": serving_id, "description": description, "macros": macros}],
    }


def test_food_in_words_reads_one_serving_and_refuses_a_partial_one():
    from services import tier3

    assert tier3.food_in_words(_food(198)) == "100 g, 198 kcal / 1 P / 2 C / 0.5 F"
    # the named serving wins over the default
    two = _food(198)
    two["servings"].append({
        "serving_id": "label", "description": "1 cup (245 g)",
        "macros": {"kcal": 39.2, "protein_g": 3.0, "carb_g": 6.0, "fat_g": 1.0},
    })
    assert tier3.food_in_words(two, "label") == "1 cup (245 g), 39.2 kcal / 3 P / 6 C / 1 F"
    # nothing loggable on it: no words, which is also "do not offer this"
    assert tier3.food_in_words(_food(None)) is None
    partial = _food(198)
    del partial["servings"][0]["macros"]["fat_g"]
    assert tier3.food_in_words(partial) is None


def test_describe_alternates_reads_each_one_and_drops_what_cannot_be_logged(monkeypatch):
    from models import Alternate
    from services import tier3

    fetched: list[str] = []

    async def get_food(source, source_id):
        fetched.append(str(source_id))
        return None if source_id == "dead" else _food(35 if source_id == "a" else None)

    async def fetchone(sql, params=()):
        return {"id": params[0], "servings": _food(145)["servings"],
                "display_name": "tofu, firm", "brand": None,
                "default_serving_id": "100g", "source": "fdc", "source_id": "174291",
                "source_url": None, "provenance": None, "name": "Tofu",
                "state": None, "state_sensitive": False}

    monkeypatch.setattr(tier3.fdc, "get_food", get_food)
    monkeypatch.setattr(tier3.db, "fetchone", fetchone)

    async def scenario():
        out = await tier3.describe_alternates([
            Alternate(source="fdc", source_id="a", label="the loggable one"),
            Alternate(source="fdc", source_id="b", label="no macros on it"),
            Alternate(source="fdc", source_id="dead", label="a 404"),
            Alternate(food_id="11111111-1111-1111-1111-111111111111", label="a cached chip"),
        ], limit=4)
        assert [a["label"] for a in out] == ["the loggable one", "a cached chip"]
        assert out[0]["nutrition"].startswith("100 g, 35 kcal")
        assert out[0]["source_id"] == "a" and out[0]["brand"] == "A Brand"
        assert out[1]["food_id"].startswith("11111111")
        assert sorted(fetched) == ["a", "b", "dead"]  # the chip was a cache read
        # the limit is a limit, and nothing to describe is not an error
        assert len(await tier3.describe_alternates(
            [Alternate(source="fdc", source_id="a", label=f"x{i}") for i in range(5)]
        )) == 3
        assert await tier3.describe_alternates(None) == []

    asyncio.run(scenario())


def test_meal_lookups_report_the_chosen_food_and_its_runners_up(monkeypatch):
    from services import tier3

    async def fetchall(sql, params=()):
        return [{"id": "f1", "servings": _food(198)["servings"], "default_serving_id": "100g"}]

    async def describe(alternates, limit=3):
        return [{"label": a.label, "nutrition": "1 tbsp, 30 kcal / 1 P / 2 C / 0 F"}
                for a in alternates or []]

    monkeypatch.setattr(logs_routes.db, "fetchall", fetchall)
    monkeypatch.setattr(tier3, "describe_alternates", describe)

    async def scenario():
        from models import Alternate

        resolved = [
            # a word item: it has a phrase and the alternates the search returned
            {"food_id": "f1", "serving_id": "100g", "qty": 1.0, "food": "miso paste",
             "serving": "100 g", "phrase": "miso paste",
             "alternates": [Alternate(source="fdc", source_id="9", label="white miso")]},
            # a past_foods item: their own choice, nothing to second-guess
            {"food_id": "f2", "serving_id": "100g", "qty": 2.0, "food": "tofu, firm",
             "serving": "100 g"},
        ]
        await logs_routes._describe_meal_lookups(resolved)
        assert resolved[0]["nutrition"] == "100 g, 198 kcal / 1 P / 2 C / 0.5 F"
        assert [a["label"] for a in resolved[0]["alternates"]] == ["white miso"]
        assert "nutrition" not in resolved[1] and "alternates" not in resolved[1]
        # only the word item is reported, and the raw Alternate objects are gone
        assert logs_routes._looked_up(resolved) == [{
            "phrase": "miso paste", "food": "miso paste", "serving": "100 g",
            "nutrition": "100 g, 198 kcal / 1 P / 2 C / 0.5 F",
            "alternates": [{"label": "white miso",
                            "nutrition": "1 tbsp, 30 kcal / 1 P / 2 C / 0 F"}],
        }]

    asyncio.run(scenario())


def test_replace_rows_swaps_by_index_in_place():
    items = [_row(0), _row(1), _row(2, "resolved")]
    ref = items
    logs_routes._replace_rows(items, [_found(1)])
    assert ref is items and [r.status for r in items] == ["searching", "resolved", "resolved"]
    assert items[1].food.name == "found 1"
