"""Photo decompose — the M4 vision call (DESIGN.md §3.4a, §12 M4).

gpt-6-astra via the plain openai client's Responses API, the way
services/decompose.py does it: one-shot structured extraction (strict schema
through responses.parse), a stable prompt_cache_key, and its own FireTrace
span (tracing.run: kind llm, the model, the token usage — the image itself is
never recorded, only its type and size).

The model identifies the foods in one plate photo and estimates how much of
each is visible — phrase, count or grams, confidence, and a user-facing note.
It NEVER outputs calories or macros: the schema has no nutrition field and
the instructions forbid numbers in the free-text ones. Nutrition comes from
the resolver — a cached foods row — never from here (§3.4a; the estimate is
mode-scoped to portions).

Model id — gpt-6-astra at reasoning effort "medium" since 2026-09-08 (Bill's
direction), verified the same day on developers.openai.com: the model page
lists input modalities "text, image" and the features "structured_outputs",
"image_input" and "prompt_caching", supports the Responses endpoint, and
accepts reasoning.effort low/medium/high/xhigh/max. It replaces gpt-5.6-luna
at effort "low" (§3.4b, 2026-09-02), which was decompose's tier chosen on the
theory that identifying foods is decompose-class work. It is not: a wrong
amount is the one thing the §3.4a flag asks the user to catch, and the photo
lane is now the front of a chat where the whole point is correcting what the
plate said (§6.4). Astra bills $10/$50 per MTok against Luna's $0.20/$1.20,
so a plate costs cents rather than a fraction of one — deliberate, and the
one call per photo is what makes it affordable. Accepted formats are
unchanged (PNG, JPEG, WEBP, non-animated GIF) — HEIC is not accepted, so
routes/photo.py refuses it before a byte reaches this call.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

import tracing
from models import Meal

# The client is built on first call (_get_client), so `import openai` — ~0.6 s
# on a cold boot — stays off the boot path and off every request that never
# looks at a photo. `from __future__ import annotations` above keeps the
# AsyncOpenAI annotations below strings.
if TYPE_CHECKING:
    from openai import AsyncOpenAI

MODEL = "gpt-6-astra"  # pinned full id — never a bare family alias (§6)
PROMPT_CACHE_KEY = "tally-vision-v2"  # bumped with the model: a cache key names a prompt prefix

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        from openai import AsyncOpenAI

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
    async with tracing.run(
        "vision", kind="llm", provider="openai", model=MODEL,
        input={"mime": mime, "bytes": len(image)},  # what was sent, never the image
        attributes={"prompt_cache_key": PROMPT_CACHE_KEY},
    ) as span:
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
            # Naming a food and sizing it from scale cues is the one place
            # thinking buys accuracy we cannot get back later — a wrong amount
            # is what the §3.4a flag asks the user to catch. decompose's
            # effort "none" is for pure text extraction.
            reasoning={"effort": "medium"},
            prompt_cache_key=PROMPT_CACHE_KEY,
        )
        span.set(output=response.output_parsed, usage=response.usage)
    return response.output_parsed
