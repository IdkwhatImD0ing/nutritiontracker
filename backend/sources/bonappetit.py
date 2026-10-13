"""Bon Appétit café day menus — the photo lane's café menu (DESIGN.md §3.4b).

A Bon Appétit café (the Pinterest SF cafeteria is one; the company runs
hundreds on the same software) publishes each day's menu at
``https://<site>.cafebonappetit.com/cafe/<YYYY-MM-DD>/``. The rendered page
shows dish names and calories only, and only after its JavaScript runs and
the reader scrolls; protein, carbohydrate and fat sit behind a per-dish popup.
The whole menu — every dish with its published serving and nutrition panel —
ships in the HTML as two script literals the page renders from:

- ``Bamco.menu_items = {"<dish id>": {label, station, description,
  nutrition_details: {calories, proteinContent, …, servingSize}}, …};``
- ``Bamco.dayparts['<n>'] = {label, starttime, endtime, stations: [{items:
  ["<dish id>", …]}]};`` — one per meal (Breakfast, Lunch, Brunch).

Checked 2026-09-30 against the Pinterest SF site: past days stay published at
their date URL, and 336 of 339 dishes across five days carried all four core
macros. No hosted tool reaches this data — the hosted web_search opened a
stale copy and found nothing for the day, and a page reader sees only what
renders — which is why this is the one menu the app reads itself (an approved
exception to §3.4b's hosted-tools rule).

A number is transcribed, never inferred: ``"< 1"`` (a label's "less than
1 g") is read as half the bound, and a dish missing any of the four core
macros is kept (so it can still be recognized) but carries no numbers to
stage. Best effort throughout — no site in the notes, no meal at the photo's
time, a slow or changed page: each answers None and the photo goes on exactly
as before.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx

log = logging.getLogger(__name__)

# the scheme is optional: a note may name the site bare, as §3.4b's example does
_SITE_RE = re.compile(r"\b(?:https?://)?([a-z0-9-]+)\.cafebonappetit\.com\b", re.I)
_ITEMS_RE = re.compile(r"Bamco\.menu_items\s*=\s*")
_DAYPART_RE = re.compile(r"Bamco\.dayparts\['\d+'\]\s*=\s*")
_TAG_RE = re.compile(r"<[^>]+>")
_TIMEOUT_S = 5.0  # the whole fetch: it runs before vision, on the slowest lane in the app
_MAX_BYTES = 4_000_000  # a day page is ~1 MB
_USER_AGENT = "TallyNutritionLogger/0.1 (personal single-user nutrition app)"
# A tray is photographed after it is filled, and eaten after that: the meal
# a photo belongs to is the one whose hours it falls in, give or take.
_EARLY = timedelta(minutes=30)
_LATE = timedelta(minutes=60)
_OZ_G = 28.349523125

# the page's nutrition panel → Macros keys (sources/fdc.py's names), with the
# unit each is published in; a panel in any other unit is not read
_NUTRIENTS = {
    "calories": ("kcal", ""),
    "proteinContent": ("protein_g", "g"),
    "carbohydrateContent": ("carb_g", "g"),
    "fatContent": ("fat_g", "g"),
    "fiberContent": ("fiber_g", "g"),
    "sugarContent": ("sugar_g", "g"),
    "saturatedFatContent": ("saturated_fat_g", "g"),
    "transFatContent": ("trans_fat_g", "g"),
    "cholesterolContent": ("cholesterol_mg", "mg"),
    "sodiumContent": ("sodium_mg", "mg"),
}
CORE = ("kcal", "protein_g", "carb_g", "fat_g")


@dataclass(frozen=True)
class Dish:
    id: str
    name: str
    station: str
    description: str
    serving: str  # as published: "5.0 oz"
    grams: float | None  # the serving in grams, when it is published by weight
    nutrients: dict[str, float]  # per serving; empty when the core four are not all there


@dataclass(frozen=True)
class CafeMeal:
    """One daypart of one day's menu: what the café served at that meal."""

    url: str  # the day page — the citation every row staged from it carries
    site: str  # "pinterest-sf"
    day: date
    label: str  # "Lunch", as the café names it
    dishes: list[Dish]


def site_in(text: str) -> str | None:
    """The Bon Appétit site a profile note names — "pinterest-sf" for
    https://pinterest-sf.cafebonappetit.com/ — else None."""
    m = _SITE_RE.search(text or "")
    return m.group(1).lower() if m else None


def _client() -> httpx.AsyncClient:  # the seam the tests hand a MockTransport through
    return httpx.AsyncClient(
        timeout=_TIMEOUT_S, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    )


def _literals(page: str, pattern: re.Pattern) -> list:
    """Every JSON literal assigned by `pattern`, decoded in place — no guessing
    where a literal ends from the text after it."""
    decoder = json.JSONDecoder()
    return [decoder.raw_decode(page, m.end())[0] for m in pattern.finditer(page)]


def _dict(value: object) -> dict:
    """PHP encodes an empty object as `[]` (the page's own `"sizes": []`)."""
    return value if isinstance(value, dict) else {}


def _number(field: object) -> float | None:
    value = str(_dict(field).get("value", "")).strip()
    if value.startswith("<"):  # "< 1": less than one — half the bound
        try:
            return float(value[1:]) / 2
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def _text(raw: object) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", str(raw or ""))).split())


def _dish(dish_id: str, raw: dict) -> Dish:
    panel = _dict(raw.get("nutrition_details"))
    nutrients: dict[str, float] = {}
    for field, (key, unit) in _NUTRIENTS.items():
        entry = _dict(panel.get(field))
        value = _number(entry)
        if value is not None and value >= 0 and (entry.get("unit") or "") == unit:
            nutrients[key] = value
    if not all(k in nutrients for k in CORE):
        nutrients = {}  # the core four or nothing, as every other menu row
    size = _dict(panel.get("servingSize"))
    amount, unit = _number(size), str(size.get("unit") or "").strip().lower()
    grams = amount * _OZ_G if amount and unit == "oz" else amount if amount and unit == "g" else None
    return Dish(
        id=dish_id,
        name=_text(raw.get("label")),
        station=_text(raw.get("station")).lstrip("@").strip(),
        description=_text(raw.get("description")),
        serving=f"{size.get('value', '')} {size.get('unit', '')}".strip(),
        grams=round(grams, 1) if grams else None,
        nutrients=nutrients,
    )


def parse(page: str) -> list[tuple[str, time, time, list[Dish]]]:
    """A day page's meals: (label, opens, closes, dishes), in the page's order.
    A day the café is closed (a weekend) carries no dish literal: no meals.
    A literal that no longer decodes raises — the caller's catch."""
    found = _literals(page, _ITEMS_RE)
    if not found or not isinstance(found[0], dict):
        return []
    items = found[0]
    meals = []
    for part in _literals(page, _DAYPART_RE):
        ids = [str(i) for station in part.get("stations") or [] for i in station.get("items") or []]
        dishes = [_dish(i, items[i]) for i in dict.fromkeys(ids) if isinstance(items.get(i), dict)]
        meals.append((
            str(part.get("label") or "").strip(),
            time.fromisoformat(part["starttime"]),
            time.fromisoformat(part["endtime"]),
            [d for d in dishes if d.name],
        ))
    return meals


def meal_for(
    meals: list[tuple[str, time, time, list[Dish]]], at: datetime
) -> tuple[str, list[Dish]] | None:
    """The meal whose hours `at` (local wall time) falls in, give or take
    _EARLY/_LATE — the one that opens latest when two windows overlap, since a
    photo is taken after the tray is filled, never before."""
    wall = at.replace(tzinfo=None)
    hits = [
        (opens, label, dishes)
        for label, opens, closes, dishes in meals
        if dishes
        and datetime.combine(wall.date(), opens) - _EARLY
        <= wall
        <= datetime.combine(wall.date(), closes) + _LATE
    ]
    if not hits:
        return None
    _, label, dishes = max(hits, key=lambda h: h[0])
    return label, dishes


async def meal_at(site: str, at: datetime) -> CafeMeal | None:
    """What the café at `site` served at the meal `at` (the photo's local
    time) falls in — None when it served nothing then, or on any failure."""
    import tracing  # lazy: stubbed in route tests

    url = f"https://{site}.cafebonappetit.com/cafe/{at.date().isoformat()}/"
    async with tracing.run("cafe_menu", kind="retriever", input={"url": url, "time": f"{at:%H:%M}"}) as span:
        try:
            # a TOTAL cap: httpx's own timeout is per phase
            async with asyncio.timeout(_TIMEOUT_S), _client() as client:
                r = await client.get(url)
            r.raise_for_status()
            meals = parse(r.text[:_MAX_BYTES])
        except Exception as e:  # slow, refused, or a page that no longer has the literals
            log.warning("cafe menu %s unreadable: %s", url, type(e).__name__)
            span.set(output={"error": type(e).__name__}, status="error")
            return None
        hit = meal_for(meals, at)
        span.set(output={
            "meals": [label for label, *_ in meals],
            "meal": hit[0] if hit else None,
            "dishes": len(hit[1]) if hit else 0,
        })
    if hit is None:
        return None
    return CafeMeal(url=url, site=site, day=at.date(), label=hit[0], dishes=hit[1])
