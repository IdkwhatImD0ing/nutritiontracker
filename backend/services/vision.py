"""Photo decompose — the M4 vision call (DESIGN.md §3.4a, §12 M4).

gpt-5.6-luna via the plain openai client's Responses API, the way
services/decompose.py does it: one-shot structured extraction (strict schema
through responses.parse), a stable prompt_cache_key, tracing left to the
caller (routes/photo.py wraps the call in a generation span).

The model identifies the foods in one plate photo and estimates how much of
each is visible — phrase, count or grams, confidence, and a user-facing note.
It NEVER outputs calories or macros: the schema has no nutrition field and
the instructions forbid numbers in the free-text ones. Nutrition comes from
the resolver — a cached foods row — never from here (§3.4a; the estimate is
mode-scoped to portions).

Model id — verified 2026-09-02 on developers.openai.com: the images-vision
guide lists gpt-5.6-sol, gpt-5.6-terra and gpt-5.6-luna as image-input
models (detail low/high/original/auto) and the gpt-5.6-luna model page says
"Input modalities: text, image". Luna is decompose's tier (§6): identifying
foods + sizing portions is a decompose-class task; terra also accepts images
if accuracy proves insufficient. Accepted formats per the same guide are
PNG, JPEG, WEBP and non-animated GIF — HEIC is not accepted, so
routes/photo.py refuses it before a byte reaches this call.
"""

from __future__ import annotations

import base64
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel

from models import Meal

MODEL = "gpt-5.6-luna"  # pinned tier id — never the bare gpt-5.6 alias (§6)
PROMPT_CACHE_KEY = "tally-vision-v1"

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI()  # reads OPENAI_API_KEY from the environment
    return _client


# ---------------------------------------------------------------------------
# Structured output — nullable fields are required-but-nullable (OpenAI strict
# mode). There is deliberately no nutrition field.
# ---------------------------------------------------------------------------


class VisionItem(BaseModel):
    phrase: str  # the food as you'd say it, quantity words removed
    qty: float | None  # count of pieces — countable items only
    unit: str | None  # the piece word ("egg", "slice"), else null
    est_grams: float | None  # portion estimate in grams (ml for drinks), else null
    confidence: Literal["high", "medium", "low"]  # how sure of WHAT it is
    note: str  # one short user-facing phrase: what it saw


class VisionResult(BaseModel):
    items: list[VisionItem]
    meal_hint: Meal | None


INSTRUCTIONS = """\
You identify the foods in one photo of a meal and estimate how much of each is
visible. Output must conform exactly to the provided schema.

items — one entry per distinct food or drink you can actually see. Skip plates,
cutlery, packaging and anything that is not food. Never list a food you cannot
see, and never merge distinct foods into one item.

phrase — the food as a person would say it when logging what they ate
("scrambled eggs", "white rice", "grilled chicken breast", "Coke Zero"): no
quantity words; the brand only when its label is legible; the cooking state
(grilled, fried, boiled, raw) when it is visible.

qty and unit — ONLY for countable items: qty is the number of pieces you can
see (2 eggs, 3 slices, 1 banana) and unit is the piece word ("egg", "slice",
"banana"). For anything measured rather than counted (rice, pasta, salad,
sauces, soups, drinks) qty and unit MUST be null.

est_grams — your best estimate of the TOTAL visible amount of that item in
grams (milliliters for drinks), using the plate, utensils and hands as scale
references. Give one whenever a portion is visible; null only when you truly
cannot judge it (the item is mostly hidden).

confidence — high when the food is unmistakable, medium when it is likely,
low when you are guessing what it is.

note — one short phrase written for the user, saying what you saw ("two fried
eggs", "about a cup of white rice", "a brown sauce, not sure which").

meal_hint — ONLY when the photo itself makes the meal explicit (a breakfast
tray, a menu card); otherwise null. Never infer it from the food.

NEVER output calories, protein, carbohydrate, fat or any other nutrition
number anywhere — not in phrase, not in note. Nutrition comes from a food
database, not from you. Identify and measure; do not compute.
"""


async def analyze(image: bytes, mime: str) -> VisionResult:
    """One photo (client-downscaled; routes/photo.py has already validated the
    bytes and capped the size) → the foods seen and their portion estimates.
    `mime` is the sniffed image type — it becomes the data URL's media type."""
    data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
    response = await _get_client().responses.parse(
        model=MODEL,
        input=[
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": INSTRUCTIONS}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Identify the foods in this photo and estimate each portion.",
                    },
                    {"type": "input_image", "image_url": data_url, "detail": "auto"},
                ],
            },
        ],
        text_format=VisionResult,
        # Sizing a portion from scale cues benefits from a little thinking;
        # decompose's effort "none" is for pure text extraction.
        reasoning={"effort": "low"},
        prompt_cache_key=PROMPT_CACHE_KEY,
    )
    return response.output_parsed
