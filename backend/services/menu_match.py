"""The café menu match — which dish on the day's menu each photo item IS
(DESIGN.md §3.4b, 2026-09-30).

When the photo falls in a meal the café published (sources/bonappetit.py),
one small model call reads what vision saw beside that meal's menu — dish id,
station, name and description, and NO numbers — and names the dish each item
is, or null. Code checks every id against the menu, and the numbers are the
menu's own, looked up by id; the model never sees or outputs one.

Tested 2026-09-30 on the 29 items of Bill's four real café lunches against
that day's menu: gpt-6-astra/medium, gpt-5.6-terra/low and gpt-5.6-luna/low
each got 54 of 58 right with 0 wrong and 4 cautious nulls, and luna took ~5 s;
harder variants (plain names, the wrong day's menu) made no real wrong match.
Asking the same call to judge whether a dish's published numbers looked right
was unreliable (4 of 8 bad cases caught, 2 false alarms), so that check lives
in code (routes/photo._stage_cafe_rows).

Best effort: a timeout, an API error or an output that names nothing on the
menu all answer None or no picks, and the photo keeps the rows the cascade
and tier 3 gave it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

import tracing

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from services.vision import VisionItem
    from sources.bonappetit import CafeMeal, Dish

log = logging.getLogger(__name__)

MODEL = "gpt-5.6-luna"  # pinned full id — never a bare family alias (§6)
_TIMEOUT_S = 20.0  # it runs beside the cascade and tier 3; ~5 s measured
_DESCRIPTION_CHARS = 80


class Pick(BaseModel):
    item_index: int
    dish_id: str | None


class Picks(BaseModel):
    picks: list[Pick]


INSTRUCTIONS = """\
You match the foods seen in one meal photo to a cafe's published menu for that meal.

For each item, return the id of the menu dish it IS, or null.
- Match only when the item clearly is that dish. A plain ingredient matches the menu's plain version of it (a salad-bar cucumber, a scoop of black beans), never a composed dish that merely contains it.
- Many foods on a cafe plate are published nowhere, and the plate may not be from this cafe at all. When no dish clearly is this item, return null. A wrong match is much worse than none: the dish's published nutrition will be logged for it.
- Judge by the item's description as well as its name.

Every item gets exactly one entry."""

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        from openai import AsyncOpenAI  # lazy: ~0.6 s of cold boot

        _client = AsyncOpenAI()
    return _client


def _item_line(i: int, v: VisionItem) -> str:
    amount = [f"{v.qty:g} {v.unit or ''}".strip()] if v.qty else []
    if v.est_grams:
        amount.append(f"~{v.est_grams:g} g")
    return f"{i}. {v.phrase}" + (f" ({', '.join(amount)})" if amount else "") + (
        f" - {v.note.strip()}" if v.note.strip() else ""
    )


def _dish_line(d: Dish) -> str:
    line = f"{d.id} | {d.station} | {d.name}"
    return line + (f" | {d.description[:_DESCRIPTION_CHARS]}" if d.description else "")


def ask(items: list[VisionItem], meal: CafeMeal) -> str:
    """The user message: the items vision saw and the meal's menu, no numbers."""
    return (
        f"Meal: {meal.label.lower()}\n\nItems seen in the photo:\n"
        + "\n".join(_item_line(i, v) for i, v in enumerate(items))
        + "\n\nMenu (id | station | dish | description):\n"
        + "\n".join(_dish_line(d) for d in meal.dishes)
    )


def picked(picks: Picks, items: list[VisionItem], meal: CafeMeal) -> dict[int, Dish]:
    """The model's picks that name a real item and a real dish on this menu —
    the first entry per item; anything else is dropped, never guessed at."""
    menu = {d.id: d for d in meal.dishes}
    out: dict[int, Dish] = {}
    for p in picks.picks:
        if 0 <= p.item_index < len(items) and p.item_index not in out and p.dish_id in menu:
            out[p.item_index] = menu[p.dish_id]
    return out


async def match(items: list[VisionItem], meal: CafeMeal) -> dict[int, Dish] | None:
    """Item index → the dish it is, for the items the model matched; None when
    the call failed (the chat line says so), never raises."""
    async with tracing.run(
        "menu_match", kind="llm", provider="openai", model=MODEL,
        input={"meal": meal.label, "url": meal.url, "items": [v.phrase for v in items]},
    ) as span:
        try:
            async with asyncio.timeout(_TIMEOUT_S):
                response = await _get_client().responses.parse(
                    model=MODEL,
                    input=[
                        {"role": "developer", "content": INSTRUCTIONS},
                        {"role": "user", "content": ask(items, meal)},
                    ],
                    text_format=Picks,
                    reasoning={"effort": "low"},
                )
            found = picked(response.output_parsed, items, meal)
        except Exception as e:  # timeout, API error, refusal: no café rows
            log.warning("menu match failed: %s", type(e).__name__)
            span.set(output={"error": type(e).__name__}, status="error")
            return None
        span.set(
            output={items[i].phrase: d.name for i, d in found.items()}, usage=response.usage
        )
        return found
