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
import os
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
# A cache key names a prompt PREFIX, so the web lane — whose developer
# message carries an extra block — gets its own. One key over two prefixes
# is two variants evicting each other with no error to notice it by.
PROMPT_CACHE_KEY = "tally-vision-v3"  # bumped with the model or the prompt
WEB_PROMPT_CACHE_KEY = f"{PROMPT_CACHE_KEY}-web"

# The web lane's two hosted tools (§3.4b, 2026-09-09). Both run on OpenAI's
# side — web_search is theirs, and the scraper is Firecrawl's own remote MCP
# server, which OpenAI calls directly. Nothing here fetches anything: this
# service has no HTTP client of its own and no page-reading code to maintain.
# `authorization` carries the key, per Firecrawl's docs — never in the URL.
_FIRECRAWL_MCP = "https://mcp.firecrawl.dev/v2/mcp"


def _web_tools() -> list[dict]:
    """The hosted tools for a web-lane call: OpenAI's own `web_search` always,
    plus Firecrawl's remote MCP server when a key is configured. The
    MCP surface is narrowed to the two tools this lane can use: the server
    offers two dozen (crawls, monitors, a paper index), and every one of them
    would be schema the model has to read past on the hot path."""
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    tools: list[dict] = [{"type": "web_search"}]
    if key:
        tools.append({
            "type": "mcp",
            "server_label": "firecrawl",
            "server_description": "Finds and reads the contents of a web page.",
            "server_url": _FIRECRAWL_MCP,
            "authorization": key,
            "require_approval": "never",
            "allowed_tools": ["firecrawl_scrape", "firecrawl_search"],
        })
    return tools


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


class MenuMatch(BaseModel):
    """A dish this item was matched to on a page the model actually read, with
    that page's own published nutrition — TRANSCRIBED, never estimated.

    Bill's ruling, 2026-09-09: the photo lane may carry a menu's numbers,
    because every photo row is confirmed by a human before it commits (§3.4a).
    That widens §3.4a's "no model-sourced macros" rule, and the widening is
    narrow on purpose — `source_url` is required, the four core macros are
    required, and routes/photo.py drops the whole block if any of them is
    missing, so a half-read page yields nothing rather than a plausible
    number. The values land in a `source='menu'` foods row with the URL in
    its provenance, so `entries.macros` still only ever comes from a foods
    row and the diary can always be traced back to the page it was read off."""

    dish: str  # the menu's own name for it, verbatim
    source_url: str  # the page the numbers were read from
    serving: str  # the serving the numbers are for, as the page states it
    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float


class VisionItem(BaseModel):
    phrase: str  # the food as you'd say it, quantity words removed
    qty: float | None  # count of pieces — countable items only
    unit: str | None  # the piece word ("egg", "slice"), else null
    est_grams: float | None  # portion estimate in grams (ml for drinks), else null
    confidence: Literal["high", "medium", "low"]  # how sure of WHAT it is
    note: str  # one short user-facing phrase: what it saw
    menu: MenuMatch | None  # the published dish + its numbers, when one was read


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

menu — null, unless a later section says otherwise. Nothing below this
line gives you anything to match against.

NEVER output calories, protein, carbohydrate, fat or any other nutrition
number anywhere — not in phrase, not in note. Nutrition comes from a food
database, not from you. Identify and measure; do not compute.
"""

# Appended when the account has the web lane (§8.9). Deliberately generic: the
# same rule serves a workplace cafeteria and a restaurant, and nothing here
# knows about either. Kept as an appended block rather than interpolated so
# INSTRUCTIONS stays the byte-stable prefix the prompt cache keys on.
_WEB_LANE = """

You may look the place up when the notes below say where this person eats and a
dish plainly looks like a specific item that place serves. Use it to NAME what
you can see more precisely — "herb roasted leg of lamb" rather than "roast
beef", a restaurant's own name for its bowl rather than a generic description.
One or two lookups at most, and none at all when the notes name no place.

- `web_search` finds a page when you do not have its address.
- `firecrawl_scrape` reads one you do. When the notes give a menu address, go
  straight to it. Read it with `onlyMainContent: false` and a `query` format
  whose prompt asks for the dishes for the right meal **with their published
  nutrition — calories, protein, carbohydrate, fat and the serving each figure
  is for**. Two things go wrong otherwise: a daily menu is almost always laid
  out as page furniture, so the default main-content extraction throws the
  dish list away and hands you a page that looks complete and names no food;
  and a query that asks only for names comes back with names, leaving you
  nothing to put in `menu`. Ask for both in one read.

What you find is evidence about NAMING WHAT YOU CAN SEE, never a list of what
is on the plate:

- NEVER add an item because a menu lists it. You must be able to see it.
- NEVER drop or rename an item you can see because no menu mentions it. Every
  cafeteria has a salad bar and every restaurant has sides that are published
  nowhere; a shrimp you can see is a shrimp.
- When nothing you find clearly matches, describe the food plainly, exactly as
  you would have without searching. A confident wrong name is worse than an
  honest vague one, because the amount is what the person is being asked to
  check.
When — and ONLY when — you have read a page that names a dish you can see,
fill that item's `menu`: the dish's name as the page writes it, the URL you
read, the serving the numbers are for, and the four figures COPIED from that
page. This is the one place a number may leave this call, and it is the only
reason `menu` exists.

- TRANSCRIBE, never estimate. Every figure must be printed on the page you
  read. If the page gives calories but no protein, you have no menu match:
  leave `menu` null. A number you worked out, adjusted for portion size, or
  remembered is a fabrication, and it will be shown to the person as fact.
- The numbers are the page's, for the page's OWN serving — do not scale them
  to what is on the plate. Say the serving exactly as the page states it and
  let the amount be corrected later.
- `menu` is per item, and most items will not have one. A salad-bar scoop, a
  side nobody published, anything you are unsure of: null.
- Still NEVER put a number in `phrase` or `note`, whatever the page said.
"""


async def analyze(
    image: bytes, mime: str, *, context: str = "", web: bool = False
) -> VisionResult:
    """One photo (client-downscaled; routes/photo.py has already validated the
    bytes and capped the size) → the foods seen and their portion estimates.
    `mime` is the sniffed image type — it becomes the data URL's media type.

    `context` is the person's §6.1 profile notes — the one place they already
    write standing facts about themselves, and the only way this call can know
    that a plate came from a particular cafeteria or a particular restaurant.
    It rides the USER message, never the developer one, so the instructions
    stay the byte-stable prefix the prompt cache keys on.

    `web` turns on the hosted web_search tool (owner only, §8.9 — it is billed
    per action). With it the model can put the kitchen's own name to a dish it
    can see; the appended rules are what keep a menu from becoming a list of
    what is on the plate. The tool is hosted, so OpenAI runs the searches
    inside this one call and the parsed result comes back as it always did."""
    data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
    cache_key = WEB_PROMPT_CACHE_KEY if web else PROMPT_CACHE_KEY
    context = (context or "").strip()
    ask = "Identify the foods in this photo and estimate each portion."
    if context:
        ask += f"\n\nWhat this person has told us about how they eat:\n{context}"
    async with tracing.run(
        "vision", kind="llm", provider="openai", model=MODEL,
        input={"mime": mime, "bytes": len(image)},  # what was sent, never the image
        attributes={"prompt_cache_key": cache_key, "web_lane": web},
    ) as span:
        response = await _get_client().responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "developer",
                    "content": [
                        {"type": "input_text", "text": INSTRUCTIONS + (_WEB_LANE if web else "")}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": ask},
                        {"type": "input_image", "image_url": data_url, "detail": "auto"},
                    ],
                },
            ],
            tools=_web_tools() if web else [],
            text_format=VisionResult,
            # Naming a food and sizing it from scale cues is the one place
            # thinking buys accuracy we cannot get back later — a wrong amount
            # is what the §3.4a flag asks the user to catch. decompose's
            # effort "none" is for pure text extraction.
            reasoning={"effort": "medium"},
            prompt_cache_key=cache_key,
        )
        span.set(output=response.output_parsed, usage=response.usage)
    return response.output_parsed
