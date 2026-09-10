"""Tier-3 search agent — cold-start LLM-driven search over food sources.

DESIGN.md §3.4, §3.4a, §6.3. Sources this phase: FDC + the web lane (rule 2a —
hosted web_search pinned to the chain's official domain, fetch_page as the
deterministic fallback, add_food's web_transcribed mode — and, since
2026-09-06, rule 2b: the same lane and the same gates for a packaged/retail
product the databases miss, pinned to the brand's or retailer's official
site). OFF is a later phase. The agent only ever selects database ids — no model output carries
nutrition numbers — with exactly one carve-out (§3.4a mode 2): add_food's web
mode transcribes published values from a chain's official page. Its schema
requires source_url + citations, and CODE enforces the rest: the run must be
pinned to the chain's official domain before a web food can be added, the
source_url must sit on that domain, and after the run the page must be one
the run actually consulted (web_search sources / url_citations / fetch_page)
or the food is dropped — so estimation has no code path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel

import auth
import db
import tracing
from models import (
    Alternate,
    FoodRef,
    Macros,
    Question,
    QuestionOption,
    ServingRef,
    StagedItem,
    WebSource,
)
from resolver import cascade, foods_repo
from resolver.commit import (
    COMMITTABLE_FOOD_SQL,
    KEEP_LOG_KEYS_SQL,
    food_committable,
    with_loggable_servings,
)
from resolver.norm import norm
from sources import fdc, off, web

MODEL = "gpt-5.6-terra"
# The web lane needs seven: search_branded, an unpinned web_search to identify
# the product, pin_chain_domain, web_search the nutrition page, fetch_page when
# that cannot read it, add_food, the answer. At 6 it could not finish, and
# MaxTurnsExceeded degrades to not_found -> manual entry, i.e. the user does the
# work. Zero source='web' foods had ever been produced in production. 10 matches
# the front-door agent (services/agent.MAX_TURNS); the heaviest real run
# archived here used 5 tool calls over 4 model turns (owner's call, 2026-09-09).
MAX_TURNS = 10

# The openai-agents SDK, loaded on first use instead of at module scope.
# `import agents` pulls `openai` in with it — together ~1.1 s of the api
# service's cold boot, which Vercel pays before the first request even for
# requests that never reach tier 3, and this module IS on the boot path
# (routes/foods, routes/resolve import it). The names stay module globals
# rather than function locals for one reason: tests/test_tier3_hardening.py
# swaps tier3.Runner for an offline stub, and monkeypatch can only reach a
# module attribute. (The `-> Agent` annotation below is not a reason — line 18
# imports __future__.annotations, so it is never evaluated.)
Agent = ModelSettings = Runner = WebSearchTool = WebSearchFilters = function_tool = None
MaxTurnsExceeded = None


def _load_sdk() -> None:
    """Bind the SDK names above and install its tracing hook, once. Every entry
    point that builds or runs an agent calls this first; every call after the
    names are bound returns at the guard."""
    global Agent, MaxTurnsExceeded, ModelSettings, Runner, WebSearchTool
    global WebSearchFilters, function_tool
    bound = (
        Agent, MaxTurnsExceeded, ModelSettings, Runner,
        WebSearchTool, WebSearchFilters, function_tool,
    )
    if all(name is not None for name in bound):
        return

    import agents
    from openai.types.responses.web_search_tool import Filters

    # Every name is bound only while still unbound, so a test double installed
    # on any of them survives. tests/test_tier3_hardening.py swaps tier3.Runner
    # for an offline stub today; the seam has to hold for the rest too, or a
    # stubbed name would be quietly replaced by the real SDK object — and the
    # test would reach the network instead of failing loudly.
    if Agent is None:
        Agent = agents.Agent
    if MaxTurnsExceeded is None:
        MaxTurnsExceeded = agents.MaxTurnsExceeded
    if ModelSettings is None:
        ModelSettings = agents.ModelSettings
    if Runner is None:
        Runner = agents.Runner
    if WebSearchTool is None:
        WebSearchTool = agents.WebSearchTool
    if WebSearchFilters is None:
        WebSearchFilters = Filters
    if function_tool is None:
        function_tool = agents.function_tool
    tracing.install_agents_hook()  # §10: the SDK's spans → this run's trace


# ---------------------------------------------------------------------------
# Cooked/raw — enforced in CODE after the run (§3.4a), never left to the prompt.
# Same deterministic term list as the prompt: meat, fish, rice, pasta, legumes.
# ---------------------------------------------------------------------------

STATE_SENSITIVE_RE = re.compile(
    r"\b(chicken|beef|pork|turkey|lamb|veal|steak|meat|fish|salmon|tuna|cod|"
    r"tilapia|halibut|shrimp|prawns?|rice|pasta|noodles?|spaghetti|macaroni|"
    r"penne|lentils?|beans?|chickpeas?|legumes?)\b",
    re.IGNORECASE,
)
_COOKED_RE = re.compile(
    r"\b(cooked|roasted|grilled|boiled|braised|broiled|baked|fried|sautéed|"
    r"sauteed|steamed|stewed|seared|smoked|poached|rotisserie|microwaved)\b",
    re.IGNORECASE,
)
_RAW_RE = re.compile(r"\b(raw|uncooked)\b", re.IGNORECASE)
_DRY_RE = re.compile(r"\b(dry|dried)\b", re.IGNORECASE)


def phrase_state(phrase: str) -> str | None:
    """The user's cooked/raw/dry intent as expressed in the phrase, else None."""
    if _RAW_RE.search(phrase):
        return "raw"
    if _COOKED_RE.search(phrase):
        return "cooked"
    if _DRY_RE.search(phrase):
        return "dry"
    return None


def _needs_state_question(food: dict, phrase: str) -> bool:
    """§3.4a: state-sensitive food + user intent not disambiguated → ask once.

    Also asks when the intent is known but the chosen entry's state contradicts
    it (the toggle is the lazy correction path).
    """
    if food.get("source") == "web":
        # §3.4a: web-transcribed chain items are prepared as sold — a cooked/raw
        # toggle has no counterpart entry to swap to and would be pure noise.
        return False
    sensitive = bool(food.get("state_sensitive")) or bool(
        STATE_SENSITIVE_RE.search(phrase)
    )
    if not sensitive:
        return False
    intent = phrase_state(phrase)
    if intent is None:
        return True
    food_state = food.get("state")
    return food_state is not None and food_state != intent


# ---------------------------------------------------------------------------
# Structured output (§3.4) — the agent identifies foods by (source, source_id)
# pairs; DB uuids exist only after persistence.
# ---------------------------------------------------------------------------


# Id-bearing DB sources the agent can reference by (source, source_id) —
# alternates and question options must be re-fetchable, so they stay "fdc"
# (widen when OFF lands). The Literal makes an invalid source (e.g. the model
# inventing "branded") structurally impossible.
Source = Literal["fdc"]

# The FINAL chosen food may additionally be "web" — but only for a food the
# agent just created through add_food's web_transcribed mode (§3.4a mode 2) —
# or "off", a barcode-lane row already in the cache. "off" is here so the
# candidate list below can keep offering scanned foods: naming them is the
# whole point of showing them, and dropping them instead meant a bar he had
# already scanned came back from a fresh FDC search as a SECOND identity, with
# its own alias and its own split history.
ChosenSource = Literal["fdc", "web", "off"]

# §4/BACKLOG 29: the full foods.source domain — the whitelist applied to every
# client-supplied source entering this module.
ALLOWED_SOURCES = ("fdc", "off", "web")

# The sources a local candidate may carry, read off the output schema so the
# two cannot drift apart. The agent answers with a (source, source_id) pair,
# so a cached row from a lane ChosenSource has no name for is one it
# structurally cannot pick — and showing it is worse than not showing it,
# because the dedup instruction ("if one of these already-cached foods is the
# same real-world food, choose it") is unfollowable for exactly the rows it is
# aimed at.
CANDIDATE_SOURCES = get_args(ChosenSource)


class Tier3Alternate(BaseModel):
    source: Source
    source_id: str
    label: str


class Tier3Option(BaseModel):
    label: str
    source: Source | None
    source_id: str | None


class Tier3Question(BaseModel):
    text: str
    options: list[Tier3Option]


class Tier3Result(BaseModel):
    status: Literal["resolved", "needs_input", "not_found"]
    source: ChosenSource | None  # chosen food, as a source pair
    source_id: str | None
    serving_id: str | None
    display_name: str | None  # short human name for the row + trigram index
    confidence: Literal["high", "medium", "low"]
    alternates: list[Tier3Alternate]  # ≤3, ids not payloads
    question: Tier3Question | None


# ---------------------------------------------------------------------------
# §3.4a mode 2 — web_transcribed. Route-local (models.py untouched): the
# schema REQUIRES source_url and citations, so a web food without provenance
# is structurally impossible — estimation has no code path.
# ---------------------------------------------------------------------------


class WebCitation(BaseModel):
    url: str
    title: str | None


class WebServingValues(BaseModel):
    """Per-serving values EXACTLY as published on the official page."""

    serving_description: str  # e.g. "1 burger (219 g)"
    grams: float | None
    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float
    fiber_g: float | None
    sugar_g: float | None
    sodium_mg: float | None


class WebTranscription(BaseModel):
    source_url: str  # the official chain page the values were read from
    page_title: str | None
    citations: list[WebCitation]  # url_citation list from the search
    brand: str | None  # the chain, e.g. "McDonald's"
    values: WebServingValues


def _web_transcription_error(web: WebTranscription) -> str | None:
    """Code-side backstop behind the schema: provenance must be real."""
    url = (web.source_url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return "source_url must be the http(s) URL of the official page"
    live = [c for c in web.citations if (c.url or "").strip().lower().startswith(("http://", "https://"))]
    if not live:
        return "citations must carry at least one http(s) url_citation from the web search"
    v = web.values
    core = (v.kcal, v.protein_g, v.carb_g, v.fat_g)
    if any(x < 0 for x in core) or v.kcal > 5000:
        return "values look implausible — transcribe them exactly as published"
    return None


def _web_source_id(url: str, display_name: str, v: WebServingValues) -> str:
    """A web food's identity (§3.4 rule 5, §4 one row per real-world food).

    NOT the URL alone: chain nutrition pages and calculators list many items
    under one URL, and foods_repo.persist_food's ON CONFLICT (source,
    source_id) returns the EXISTING row — a URL-only id would hand the second
    item transcribed from that page the first item's row (its macros in the
    diary, its provenance merged). The id hashes the URL + the normalized item
    name + the core values as read: a re-read of the same item dedups to its
    row; a different item, or a changed label, is a different identity.
    """
    core = ",".join(f"{float(x):.2f}" for x in (v.kcal, v.protein_g, v.carb_g, v.fat_g))
    key = f"{url}\n{norm(display_name)}\n{core}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _food_from_web(web: WebTranscription, display_name: str) -> dict:
    """Normalized foods-row dict for a web-transcribed chain item (§3.4a, §4):
    source='web', source_id = _web_source_id(...), provenance jsonb."""
    url = web.source_url.strip()
    v = web.values
    name = display_name.strip() or (web.page_title or url)
    source_id = _web_source_id(url, name, v)
    macros: dict[str, float] = {
        "kcal": v.kcal, "protein_g": v.protein_g, "carb_g": v.carb_g, "fat_g": v.fat_g,
    }
    for extra in ("fiber_g", "sugar_g", "sodium_mg"):
        val = getattr(v, extra)
        if val is not None:
            macros[extra] = float(val)
    return {
        "source": "web",
        "source_id": source_id,
        "source_url": url,
        "provenance": {
            "citations": [c.model_dump() for c in web.citations],
            "page_title": web.page_title,
            "values_as_read": v.model_dump(),
            "transcribed_at": datetime.now(timezone.utc).isoformat(),
        },
        "name": name,
        "display_name": name,
        "brand": web.brand,
        "food_type": "branded",
        "state": None,
        "state_sensitive": False,
        "servings": [{
            "serving_id": "web",
            "description": v.serving_description,
            "grams": float(v.grams) if v.grams is not None else None,
            "macros": macros,
        }],
        "default_serving_id": "web",
        "per_100g": None,
        "payload": {"web_transcription": web.model_dump()},
    }


def _web_source(food: dict) -> dict | None:
    """The staged-payload citation block for a web food (§3.4a): the UI renders
    it as a visible, clickable link. Key in `item_resolutions.staged`:
    ``web_source = {"url": str, "title": str}``."""
    if food.get("source") != "web" or not food.get("source_url"):
        return None
    prov = food.get("provenance") or {}
    title = prov.get("page_title")
    if not title:
        for c in prov.get("citations") or []:
            if isinstance(c, dict) and c.get("title"):
                title = c["title"]
                break
    return {
        "url": food["source_url"],
        "title": title or food.get("display_name") or food.get("name") or food["source_url"],
    }


def _web_evidence(transcript: list | None) -> tuple[dict, int]:
    """What the run's hosted web_search actually consulted (§3.4, §4): the
    ``web_search_call.action.sources`` lists (present because of
    response_include) and every ``url_citation`` annotation on the model's
    messages. Returns (provenance fragment, search-action count); the fragment
    is merged into foods.provenance for the web food persisted this run."""
    searches: list[dict] = []
    citations: list[dict] = []
    seen: set[str] = set()
    for it in transcript or []:
        if not isinstance(it, dict):
            continue
        if it.get("type") == "web_search_call":
            action = it.get("action") or {}
            # search actions carry `sources` (response_include); open_page /
            # find actions carry the one `url` they acted on
            urls = [
                s.get("url") for s in (action.get("sources") or [])
                if isinstance(s, dict) and s.get("url")
            ]
            if action.get("url") and action["url"] not in urls:
                urls.append(action["url"])
            searches.append({"query": action.get("query"), "sources": urls})
        elif it.get("type") == "message":
            for part in it.get("content") or []:
                if not isinstance(part, dict):
                    continue
                for ann in part.get("annotations") or []:
                    if (
                        isinstance(ann, dict)
                        and ann.get("type") == "url_citation"
                        and ann.get("url")
                        and ann["url"] not in seen
                    ):
                        seen.add(ann["url"])
                        citations.append({"url": ann["url"], "title": ann.get("title")})
    fragment: dict = {}
    if searches:
        fragment["web_searches"] = searches
    if citations:
        fragment["url_citations"] = citations
    return fragment, len(searches)


def _url_host(url: str) -> str:
    """Lower-cased hostname of a URL without a leading "www." ("" if none)."""
    host = (urlsplit((url or "").strip()).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _host_within(host: str, domain: str) -> bool:
    """host is `domain` itself or a subdomain of it."""
    return bool(host) and bool(domain) and (host == domain or host.endswith("." + domain))


def _web_evidence_gap(source_url: str, web_evidence: dict, run_state: dict) -> str | None:
    """§3.4a/§0 "estimation has no code path", enforced after the run: the page
    a web food claims to be transcribed from must be one this run actually
    consulted — a host the hosted web_search listed in action.sources (or
    opened), cited in a url_citation, or that fetch_page read successfully.
    The model-supplied source_url/citations prove nothing on their own (a
    turn-1 add_food from memory, or a prompt-injected page, can name any URL).
    Returns the reason for the gap, or None when the provenance is real."""
    host = _url_host(source_url)
    consulted: set[str] = set(run_state.get("fetched_hosts") or ())
    for search in web_evidence.get("web_searches") or []:
        consulted.update(_url_host(u) for u in search.get("sources") or [])
    consulted.update(_url_host(c.get("url", "")) for c in web_evidence.get("url_citations") or [])
    consulted.discard("")
    if any(_host_within(host, h) or _host_within(h, host) for h in consulted):
        return None
    return (
        f"source_url host {host!r} was never consulted this run "
        f"(searched/cited/fetched: {sorted(consulted) or 'nothing'})"
    )


async def _discard_web_food(food_row_id: str) -> None:
    """Drop a web food that failed the evidence gate — only when nothing
    references it. A pre-existing row (same URL + item + values) that earlier
    runs committed against is left alone; this run simply doesn't use it."""
    await db.execute(
        """
        DELETE FROM foods f WHERE f.id = %s
          AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.food_id = f.id)
          AND NOT EXISTS (SELECT 1 FROM aliases a WHERE a.food_id = f.id)
          AND NOT EXISTS (SELECT 1 FROM item_resolutions r
                          WHERE r.proposed_food_id = f.id OR r.final_food_id = f.id)
        """,
        (str(food_row_id),),
    )


# ---------------------------------------------------------------------------
# System instructions — §3.4 ranking/routing rules 1–5 verbatim, including
# rule 2a (restaurant web lane; OFF clauses are a later phase).
# ---------------------------------------------------------------------------

INSTRUCTIONS = """\
You are the tier-3 food resolver for a single-user nutrition logger. Map one spoken or
typed food phrase to exactly one food-database entry, or ask one precise question. Work
with the fewest tool calls that give a confident answer.

Tools:
- search_generic(query): USDA FDC generic/whole foods (SR Legacy + Foundation), condensed.
- search_branded(query): USDA FDC Branded (US packaged products), with brand owner.
- web_search (hosted): search the public web — the web lane (rules 2a and 2b): chain
  menu items, and packaged/retail products the databases do not carry. Never for a
  generic whole food.
- pin_chain_domain(domain): pin web_search to one OFFICIAL domain — the chain's, the
  brand's or the retailer's (e.g. "mcdonalds.com", "nongshimusa.com", "costco.com").
  REQUIRED before the search that reads a nutrition page: searches after the pin only
  see that domain, and add_food's web mode refuses an unpinned run or a source_url off
  the pinned domain. An unpinned search is allowed only to identify a product (rule
  2b). Re-pin if the nutrition facts are published on a different official domain.
- fetch_page(url): plain HTTP fetch + text extraction of one page — the deterministic
  fallback when web_search cannot read a chain's nutrition page (JS calculator, blocked
  page). HTML only; PDFs cannot be extracted.
- get_food(source, source_id): full detail (servings, per-100g macros) for one entry.
- get_food_history(n): the user's recent/frequent foods — use when the phrase might be
  shorthand for something he already eats.
- add_food(...): the custom-food gate — exactly two modes; call it exactly once, for
  your final chosen food, before you answer:
  * from_source: source="fdc" with source_id — cache a database entry (the normal path).
  * web_transcribed: source="web" with the web payload — the web lane (rules 2a/2b).
    Requires source_url (the official page), citations, and per-serving values EXACTLY
    as read from the page. Use the source_id it returns in your final answer.
  display_name is a short human name ("chicken breast, grilled", "Big Mac") — never
  nutrition values.

Ranking and routing rules:
1. Generic/whole food ("chicken breast", "broccoli", "rice") -> search_generic. Prefer
   SR Legacy entries for cooked staples — Foundation entries are mostly raw commodities
   with partial nutrient panels (a Foundation raw chicken breast carries 26 nutrients,
   no B-vitamins or selenium), while SR Legacy carries ~130 including full
   vitamin/mineral panels. Foundation is acceptable for raw/simple produce.
2. Branded SKU ("fairlife", "quest bar") -> search_branded (FDC Branded). Prefer the
   branded entry even though it carries fewer micronutrients — substituting a generic
   for a specific product is worse data, not better. Prefer fresher records (dataSource
   GDSN over LI-era; surface publishedDate when it's years old).
2a. Restaurant/chain item ("a Big Mac", "the pesto thing from Sweetgreen") -> the web
   lane, for FAMOUS CHAINS ONLY — chains US menu-labeling law obliges to publish
   nutrition (roughly 20+ locations); you judge whether the phrase names one. Steps:
   (1) pin_chain_domain with the chain's official domain (required); (2) web_search the
   chain's official nutrition page for the item; (3) transcribe the published values
   EXACTLY as shown — no estimating, no inventing — and call add_food with source="web"
   and the web payload, source_url = the page you actually read this run. If web_search
   cannot read the page, call fetch_page on the official nutrition page and read the
   returned text. A web food whose source_url this run never searched, cited, or
   fetched is discarded after the run (the user gets manual entry instead). FDC's chain
   entries (SR Legacy, frozen 2018) are a sanity cross-check only — never the answer
   for a current menu item. Independent (non-chain) restaurants are OUT OF SCOPE:
   return "not_found" so the user gets manual entry — NEVER estimate nutrition from
   general knowledge.
2b. Packaged/retail product the databases miss ("costco tonkotsu ramen", a store brand,
   a new SKU): FIRST work out what the product is — the brand and the exact item (a
   retailer phrase usually names a branded product it sells; one unpinned web_search
   may identify it) — and try search_branded under that name. A record that does not
   carry all four of calories, protein, carbs and fat is NOT a match — results are
   already filtered for that, so a thin-only product comes back empty and you must go
   to the web rather than settle. If FDC Branded has no usable match,
   use the web lane exactly as in 2a: pin_chain_domain with the brand's or the
   retailer's OFFICIAL domain (the manufacturer's site, or the retailer's product
   page), web_search or fetch_page the page that shows the Nutrition Facts panel, and
   transcribe its per-serving values EXACTLY as published into add_food(source="web").
   Only the brand's or the retailer's own page counts — never a recipe site, a
   nutrition aggregator, or your memory. Nothing official found -> "not_found" (the
   user gets manual entry), never an estimate.
3. Cooked vs raw: if the phrase or state field disambiguates, match it; otherwise prefer
   the cooked entry for state-sensitive foods (meat, fish, rice, pasta, legumes).
4. Query refinement allowed: if results are junk, refine and retry ("protein bar" ->
   "Quest protein bar chocolate chip cookie dough"; FDC's ranking is quirky — "chicken
   breast cooked" surfaces breaded tenders above plain roasted breast, and a more
   specific query fixes it). If the missing information is unknowable (which brand?),
   return a question instead of guessing.
5. Never mix sources for one food. A food identity lives in exactly one
   (source, source_id).
6. An ESTIMATE row never asks. When the input says the row is a flagged estimate,
   "needs_input" is not available to you: the amount is already a guess the person
   is going to look at and correct, so a question buys precision the row does not
   have and costs them a tap per item. Pick the best entry you saw and say how sure
   you are — confidence "medium" for a refined guess, "low" for best-available —
   and put the runners-up in alternates, which is where the sheet offers them as a
   one-tap swap. Only "not_found" when nothing plausible matched at all.

Local candidates: the input may list already-cached foods. If one of these
already-cached foods is the same real-world food, choose it — do not create a second
identity for the same food.

Output contract:
- Never output nutrition numbers in your final answer. You select database ids plus
  display strings. The ONE place nutrition values ever pass through you is add_food's
  web mode, where you transcribe published facts from the chain's official page with
  the URL and citations attached.
- Sources: "fdc" for database entries (source_id = the FDC id from search results);
  "web" ONLY for a food you just added via add_food's web mode — echo the source_id
  add_food returned. Alternates and question options must always be "fdc" entries you
  actually saw.
- "resolved": set source, source_id, serving_id (pick from get_food's servings — the
  entry's default, or the serving matching the spoken quantity/unit; for a web food use
  the serving add_food created), display_name, and confidence (high = unambiguous,
  medium = refined guess, low = best available).
- alternates: up to 3 plausible runners-up as (source, source_id, label) — only entries
  you actually saw in results.
- "needs_input": never on an estimate row (rule 6). Otherwise set question with short
  text and 2–4 options; every option that refers
  to a candidate you already saw MUST carry its source and source_id so a tap resolves
  instantly without another search. Before asking a which-brand/flavor/variant question,
  run at least one search so the options are real candidates carrying ids — an id-less
  question is allowed only when no search could disambiguate (the answer exists only in
  the user's head).
- "not_found": nothing matched and no useful question exists.
"""

# Appended for a non-owner run (§8.9, BACKLOG 119): no web tools exist there.
_NO_WEB_LANE = """

This account has no web lane: web_search, pin_chain_domain and fetch_page are
not available. Restaurant/chain items (rule 2a) and products FDC Branded lacks
(rule 2b) are "not_found" here — never estimated, never an FDC stand-in for a
current menu item.
"""


# ---------------------------------------------------------------------------
# Env / small helpers
# ---------------------------------------------------------------------------


def _ensure_env() -> None:
    """Load repo .env into os.environ (setdefault) if OPENAI_API_KEY is absent."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _col(row: Any, idx: int, key: str) -> Any:
    """Read a column from a db row regardless of tuple/dict row factory."""
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return {}


def _scale_macros(macros: dict, qty: float) -> dict:
    return {
        k: round(float(v) * qty, 2)
        for k, v in macros.items()
        if isinstance(v, (int, float))
    }


def _pick_serving(food: dict, serving_id: str | None) -> dict | None:
    """Chosen serving, validated against foods.servings — dangling ids never render."""
    servings = food.get("servings") or []
    for sid in (serving_id, food.get("default_serving_id")):
        if sid:
            for s in servings:
                if s.get("serving_id") == sid:
                    return s
    return servings[0] if servings else None


async def _apply_portion(
    food: dict,
    chosen_serving_id: str | None,
    spoken_qty: float | None,
    spoken_unit: str | None,
    *,
    food_row_id: str | None = None,
    phrase: str | None = None,
) -> tuple[str | None, float, bool]:
    """The resolver's full §7.3 portion order for tier-3 paths (BACKLOG 25),
    via the SAME cascade.resolve_portion the tier-1/2 staging path uses:

    1. the spoken quantity wins WHEN IT MAPS onto a serving (incl. the
       count-without-unit rule); one that maps onto nothing falls back to a
       single serving rather than becoming the multiplier (cascade.py)
    2. alias default — mode of the last 10 committed portions for
       (norm(phrase) → this food), when that alias exists
    3. the food's most-logged portion across all aliases
    4. the chosen (agent/provider) serving × 1

    Returns (serving_id, qty, verify_me).
    """
    alias_id = alias_serving = alias_qty = None
    if food_row_id is not None and phrase:
        arow = await db.fetchone(
            "SELECT id, default_serving_id, default_qty FROM aliases "
            "WHERE alias_norm = %s AND food_id = %s",
            (norm(phrase), str(food_row_id)),
        )
        if arow is not None:
            alias_id = _col(arow, 0, "id")
            alias_serving = _col(arow, 1, "default_serving_id")
            alias_qty = _col(arow, 2, "default_qty")
    row = {
        "servings": food.get("servings") or [],
        "food_default_serving_id": chosen_serving_id or food.get("default_serving_id"),
        "default_serving_id": alias_serving,
        "default_qty": alias_qty,
        "alias_id": alias_id,
        "food_id": str(food_row_id) if food_row_id is not None else None,
    }
    qty = float(spoken_qty) if spoken_qty is not None else None
    return await cascade.resolve_portion(row, qty, spoken_unit)


# ---------------------------------------------------------------------------
# Local candidates — threshold-free (§3.4): top-N trigram matches however weak,
# plus his top-10 staples by use_count.
# ---------------------------------------------------------------------------


async def _local_candidates(phrase: str) -> list[dict]:
    # CANDIDATE_SOURCES is a whitelist where a source <> 'estimate' blacklist
    # used to be. It still keeps out the photo lane's numberless placeholders
    # (routes/photo.py stage_estimate_food — estimation is photo-only, R1), and
    # with them the two lanes whose rows the agent cannot name back at all: the
    # photo lane's 'menu' matches (§3.4b) and the barcode lane's 'off' rows.
    #
    # COMMITTABLE_FOOD_SQL drops cached rows with no loggable serving (BACKLOG
    # 169). Without it a thin row cached by an earlier run outranks everything
    # on trigram — the Oikos Triple Zero row cached at the first attempt came
    # back at sim 0.13, top of the list, and the agent resolved straight to it
    # instead of searching for the complete record.
    loggable = COMMITTABLE_FOOD_SQL.format(t="foods")
    loggable_f = COMMITTABLE_FOOD_SQL.format(t="f")  # the staples query aliases it
    trigram = await db.fetchall(
        f"""
        SELECT id, source, source_id, display_name, brand,
               similarity(display_name || ' ' || coalesce(brand, ''), %s) AS sim
        FROM foods
        WHERE source = ANY(%s) AND {loggable}
        ORDER BY sim DESC
        LIMIT 15
        """,
        (phrase, list(CANDIDATE_SOURCES)),
    )
    staples = await db.fetchall(
        f"""
        SELECT f.id, f.source, f.source_id, f.display_name, f.brand,
               sum(a.use_count) AS uses
        FROM foods f JOIN aliases a ON a.food_id = f.id
        WHERE f.source = ANY(%s) AND {loggable_f}
        GROUP BY f.id, f.source, f.source_id, f.display_name, f.brand
        ORDER BY uses DESC, max(a.last_used_at) DESC
        LIMIT 10
        """,
        (list(CANDIDATE_SOURCES),),
    )
    out: list[dict] = []
    seen: set[str] = set()
    for row in trigram:
        fid = str(_col(row, 0, "id"))
        if fid in seen:
            continue
        seen.add(fid)
        out.append(
            {
                "source": _col(row, 1, "source"),
                "source_id": _col(row, 2, "source_id"),
                "display_name": _col(row, 3, "display_name"),
                "brand": _col(row, 4, "brand"),
                "sim": float(_col(row, 5, "sim") or 0.0),
            }
        )
    for row in staples:
        fid = str(_col(row, 0, "id"))
        if fid in seen:
            continue
        seen.add(fid)
        out.append(
            {
                "source": _col(row, 1, "source"),
                "source_id": _col(row, 2, "source_id"),
                "display_name": _col(row, 3, "display_name"),
                "brand": _col(row, 4, "brand"),
                "sim": None,
            }
        )
    return out


def _agent_input(
    phrase: str,
    qty: float | None,
    unit: str | None,
    candidates: list[dict],
    refinement: str | None,
    note: str | None = None,
    estimated: bool = False,
) -> str:
    lines = [f'Resolve this food phrase to a database entry: "{phrase}"']
    if qty is not None:
        lines.append(f"Spoken quantity: {qty}" + (f" {unit}" if unit else ""))
    # The note is what the photo (or the agent's estimate) saw — including the
    # menu page it cited. Without it this call asked "homemade, or a restaurant
    # item?" about a dish vision had already tied to a cafeteria's own menu
    # (trace 886b7b91, 2026-09-09), because the phrase alone says nothing about
    # where the plate came from.
    if note:
        lines.append(f"Where this phrase came from: {note}")
    if estimated:
        lines.append(
            "This row is already a flagged ESTIMATE the person will check and "
            "adjust: rule 6 applies — resolve it, never ask."
        )
    if refinement:
        lines.append(f'The user answered a clarifying question with: "{refinement}"')
    if candidates:
        lines.append("")
        lines.append(
            "Already-cached local candidates (local foods table — threshold-free "
            "trigram matches plus frequent staples):"
        )
        for c in candidates:
            sim = f" — sim {c['sim']:.2f}" if c["sim"] is not None else " — staple"
            brand = f" ({c['brand']})" if c.get("brand") else ""
            lines.append(
                f"- source={c['source']} source_id={c['source_id']}: "
                f"{c['display_name']}{brand}{sim}"
            )
        lines.append(
            "If one of these already-cached foods is the same real-world food, choose "
            "it — do not create a second identity for the same food."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 404 tolerance (§3.4 adapter rules): fdc.get_food returns None on an fdcId
# that search just returned — observed live (stale index entries). EVERY
# caller must retry the next candidate or degrade cleanly, never crash.
# ---------------------------------------------------------------------------

_STALE_ID_ERROR = json.dumps({
    "error": "not_found",
    "message": "That source_id no longer exists (stale search-index entry). "
               "Pick a different candidate or refine the search.",
})


async def _get_food_cached(
    source: str, source_id: str, counter: dict, food_cache: dict
) -> dict | None:
    """The adapters through the run cache; None on 404, never cached as None.

    "fdc" fetches by id; "off" fetches by barcode, which is what a barcode
    row's source_id IS (routes/quick.py stores it that way) — that is how a
    scanned food the agent picks out of the candidate list gets hydrated. A
    "web" food exists solely in the run cache (put there by add_food's web
    mode), so a cache miss is a miss.
    """
    key = (source, str(source_id))
    food = food_cache.get(key)
    if food is None and source in ("fdc", "off"):
        counter["network_calls"] += 1
        food = (
            await fdc.get_food(source, str(source_id))
            if source == "fdc"
            else await off.lookup_barcode(str(source_id))
        )
        if food is not None:
            food_cache[key] = food
    return food


async def _first_live_food(
    candidates: list[tuple], counter: dict, food_cache: dict
) -> tuple[tuple | None, dict | None]:
    """First candidate whose id still fetches.

    candidates: (source, source_id, display_name, serving_id, confidence)
    tuples — the agent's chosen food first, then its alternates in order.
    Returns (candidate, food), or (None, None) when every id 404ed.

    A record with no committable serving is skipped exactly like a 404
    (BACKLOG 184): its card would render with no numbers at all
    (_staged_from_food needs the core four) and refuse at commit, so it is not
    a resolution. The search filter keeps most of these out of the agent's
    hands; this is the backstop for the ones that arrive anyway — a cached
    local candidate, or a hit whose search row was fuller than its detail
    record. Falling through to the alternates, and finally to not_found, puts
    the user in the manual search box instead of on a dead-end card.
    """
    for cand in candidates:
        food = await _get_food_cached(cand[0], cand[1], counter, food_cache)
        if food is not None and food_committable(food):
            return cand, food
    return None, None


# ---------------------------------------------------------------------------
# Agent construction — per run, so tool closures carry run-local state
# (network-call counter, get_food cache, persisted ids).
# ---------------------------------------------------------------------------


def _tool_error(ctx, error: Exception) -> str:
    """Sanitized tool-failure message. The SDK default embeds str(error), which
    for httpx errors carries the full request URL — secrets must never reach
    the model, the traces, or the archived transcript (§10)."""
    return (
        f"{type(error).__name__}: the food-data request failed. "
        "Try again, adjust the query, or answer from what you already have."
    )


def _build_agent(
    counter: dict,
    food_cache: dict,
    persisted: dict,
    run_state: dict | None = None,
) -> Agent:
    _load_sdk()  # @function_tool and Agent below are SDK names — bind them first
    # Run-local web-lane state: the pinned official domain + the hosts
    # fetch_page actually read — add_food's domain check and the post-run
    # evidence gate (_web_evidence_gap) both read it.
    run_state = run_state if run_state is not None else {}
    run_state.setdefault("pinned_domain", None)
    run_state.setdefault("fetched_hosts", set())
    run_state.setdefault("failed_fetches", set())

    @function_tool(failure_error_function=_tool_error)
    async def search_generic(query: str) -> str:
        """Search USDA generic/whole foods (SR Legacy + Foundation). Condensed results."""
        counter["network_calls"] += 1
        return json.dumps(await fdc.search_generic(query), default=str)

    @function_tool(failure_error_function=_tool_error)
    async def search_branded(query: str) -> str:
        """Search USDA FDC Branded (US packaged products). Condensed, with brand owner."""
        counter["network_calls"] += 1
        return json.dumps(await fdc.search_branded(query), default=str)

    @function_tool(failure_error_function=_tool_error)
    async def get_food(source: Source, source_id: str) -> str:
        """Full detail for one entry: servings (ids + descriptions + grams), per-100g macros, state."""
        food = await _get_food_cached(source, str(source_id), counter, food_cache)
        if food is None:  # stale index id (404) — tell the agent, don't crash
            return _STALE_ID_ERROR
        view = {
            "source": source,
            "source_id": str(source_id),
            "name": food.get("name"),
            "brand": food.get("brand"),
            "food_type": food.get("food_type"),
            "state": food.get("state"),
            "state_sensitive": food.get("state_sensitive"),
            "default_serving_id": food.get("default_serving_id"),
            "servings": [
                {
                    "serving_id": s.get("serving_id"),
                    "description": s.get("description"),
                    "grams": s.get("grams"),
                }
                for s in (food.get("servings") or [])
            ],
            "per_100g": food.get("per_100g"),
        }
        return json.dumps(view, default=str)

    @function_tool(failure_error_function=_tool_error)
    async def get_food_history(n: int = 20) -> str:
        """The user's top-n foods by recency/use (his personal history)."""
        n = max(1, min(int(n), 100))
        rows = await db.fetchall(
            """
            SELECT f.source, f.source_id, f.display_name, f.brand,
                   sum(a.use_count) AS uses, max(a.last_used_at) AS last_used
            FROM aliases a JOIN foods f ON f.id = a.food_id
            GROUP BY f.source, f.source_id, f.display_name, f.brand
            ORDER BY uses DESC, last_used DESC
            LIMIT %s
            """,
            (n,),
        )
        return json.dumps(
            [
                {
                    "source": _col(r, 0, "source"),
                    "source_id": _col(r, 1, "source_id"),
                    "display_name": _col(r, 2, "display_name"),
                    "brand": _col(r, 3, "brand"),
                    "use_count": int(_col(r, 4, "uses") or 0),
                }
                for r in rows
            ],
            default=str,
        )

    @function_tool(failure_error_function=_tool_error)
    async def add_food(
        source: ChosenSource,
        source_id: str | None,
        display_name: str,
        web: WebTranscription | None,
    ) -> str:
        """Cache the chosen food locally — the custom-food gate, two modes:
        from_source (source="fdc" + source_id) caches a database entry;
        web_transcribed (source="web" + web payload, chain items only) creates a
        web food from values read off the chain's official page. Call exactly
        once, for the final chosen food, before answering; for web mode, answer
        with the source_id this tool returns.
        """
        if source == "web":
            if not auth.is_owner():
                # §8.9: the shared cache takes web transcriptions from the
                # owner only. A demo's phrase can steer the pin to any domain,
                # and a web row this account created is a pre-existing food —
                # past the evidence gate — to every other account's tier 3.
                return json.dumps({
                    "error": "web_lane_owner_only",
                    "message": "this account cannot add web-transcribed foods — choose a "
                               "database entry (search_generic / search_branded) or "
                               "answer not_found.",
                })
            if web is None:
                return json.dumps({
                    "error": "missing_web_payload",
                    "message": 'source="web" requires the web payload: source_url, '
                               "citations, and per-serving values as read from the page.",
                })
            problem = _web_transcription_error(web)
            if problem is not None:
                return json.dumps({"error": "invalid_web_payload", "message": problem})
            # §3.4: the web lane is pinned to the chain's OFFICIAL site — in
            # code, not prompt. No pin → no web food; a source_url off the
            # pinned domain (a third-party nutrition site) → refused.
            pinned = run_state.get("pinned_domain")
            if not pinned:
                return json.dumps({
                    "error": "pin_required",
                    "message": "call pin_chain_domain with the chain's official domain "
                               'before add_food(source="web"), then read the page there.',
                })
            host = _url_host(web.source_url)
            if not _host_within(host, pinned):
                return json.dumps({
                    "error": "off_domain",
                    "message": f"source_url host {host!r} is not on the pinned official "
                               f"domain {pinned!r} — transcribe from the official page "
                               "(re-pin first if the chain publishes nutrition elsewhere).",
                })
            food = _food_from_web(web, display_name)
            food_id = await foods_repo.persist_food(food)
            key = ("web", food["source_id"])
            persisted[key] = str(food_id)
            food_cache[key] = food
            return json.dumps({
                "food_id": str(food_id),
                "source": "web",
                "source_id": food["source_id"],
                "default_serving_id": food["default_serving_id"],
            })

        if not source_id:
            return json.dumps({
                "error": "missing_source_id",
                "message": 'source="fdc" requires the source_id of the chosen entry.',
            })
        food = await _get_food_cached(source, str(source_id), counter, food_cache)
        if food is None:  # stale index id (404) — tell the agent, don't crash
            return _STALE_ID_ERROR
        food = dict(food)
        food.setdefault("source", source)
        food.setdefault("source_id", str(source_id))
        foods_repo.apply_label(food, display_name.strip())  # owner only: shared cache
        food_id = await foods_repo.persist_food(food)
        persisted[(source, str(source_id))] = str(food_id)
        food_cache[(source, str(source_id))] = food
        return json.dumps(
            {"food_id": str(food_id), "default_serving_id": food.get("default_serving_id")}
        )

    # §3.4 rule 2a — the hosted web_search tool, run-local so pin_chain_domain
    # can pin filters.allowed_domains mid-run (tools are re-serialized on every
    # model turn, so a pin set on turn N applies to every later search).
    web_search_tool = WebSearchTool(search_context_size="medium")

    @function_tool(failure_error_function=_tool_error)
    async def pin_chain_domain(domain: str) -> str:
        """Pin web_search to one OFFICIAL domain — the chain's, the brand's or
        the retailer's (e.g. "mcdonalds.com", "costco.com"). Applies to every
        web_search after this call; call again to re-pin.
        """
        d = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0]
        d = d[4:] if d.startswith("www.") else d
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", d):
            return json.dumps({
                "error": "invalid_domain",
                "message": "pass a bare domain like mcdonalds.com",
            })
        web_search_tool.filters = WebSearchFilters(allowed_domains=[d])
        run_state["pinned_domain"] = d
        return json.dumps({"pinned_domain": d})

    @function_tool(failure_error_function=_tool_error)
    async def fetch_page(url: str) -> str:
        """Fetch one web page (plain HTTP GET + text extraction, size-capped) —
        the deterministic fallback when web_search cannot read a chain's
        nutrition page. HTML only; PDFs and binaries cannot be extracted.
        """
        # Bot-hostile chain sites time out for any non-browser client (McDonald's
        # product pages: ReadTimeout at 20s, observed live 2026-08-21). A retry
        # of the same URL burns another turn + timeout for the same answer, so
        # a URL that failed this run is refused immediately.
        failed: set = run_state["failed_fetches"]
        if url in failed:
            return json.dumps({
                "url": url,
                "error": "this URL already failed to fetch this run — do not retry it; "
                         "use web_search, try a different official page, or answer "
                         "from what you already have",
            })
        counter["network_calls"] += 1
        try:
            page = await web.fetch_page(url)
        except Exception:
            failed.add(url)
            raise  # failure_error_function sanitizes the message for the model
        if "error" in page:
            failed.add(url)
        else:  # evidence for the post-run gate: this host was read
            run_state["fetched_hosts"].update(
                h for h in (_url_host(url), _url_host(page.get("url") or "")) if h
            )
        return json.dumps(page, default=str)

    if not auth.is_owner():
        # §8.9 / BACKLOG 119: the web lane is the owner's. A demo's run gets
        # no hosted web_search (every action is billed), no pin, no
        # fetch_page — FDC only — and is told so, so rule 2a ends in
        # not_found instead of turns spent looking for the tools.
        return Agent(
            name="tier3-food-resolver",
            instructions=INSTRUCTIONS + _NO_WEB_LANE,
            model=MODEL,
            tools=[search_generic, search_branded, get_food, get_food_history, add_food],
            output_type=Tier3Result,
        )
    return Agent(
        name="tier3-food-resolver",
        instructions=INSTRUCTIONS,
        model=MODEL,
        tools=[
            search_generic, search_branded, web_search_tool, pin_chain_domain,
            fetch_page, get_food, get_food_history, add_food,
        ],
        # §3.4: capture the consulted-sources list on every web_search action —
        # it lands in the run items → agent_transcript → web-food provenance.
        model_settings=ModelSettings(
            response_include=["web_search_call.action.sources"]
        ),
        output_type=Tier3Result,
    )


# ---------------------------------------------------------------------------
# StagedItem assembly + persistence — shared by run_tier3 and the local
# answer-resolution path (routes/resolve.py).
# ---------------------------------------------------------------------------


def _staged_from_food(
    item_index: int,
    phrase: str,
    meal: str,
    qty: float,
    food: dict,
    food_row_id: str,
    serving_id: str | None,
    confidence: str | None,
    alternates: list[Alternate] | None,
    question: Question | None,
    tier: int = 3,
) -> StagedItem:
    serving = _pick_serving(food, serving_id)
    macros = None
    serving_ref = None
    if serving is not None:
        serving_ref = ServingRef(
            id=serving.get("serving_id", ""),
            description=serving.get("description", ""),
        )
        raw = serving.get("macros") or {}
        if {"kcal", "protein_g", "carb_g", "fat_g"} <= set(raw):
            macros = Macros(**_scale_macros(raw, qty))
    return StagedItem(
        item_index=item_index,
        phrase=phrase,
        status="resolved",
        tier=tier,  # type: ignore[arg-type]
        food=FoodRef(
            id=str(food_row_id),
            name=food.get("display_name") or food.get("name", ""),
            brand=food.get("brand"),
        ),
        serving=serving_ref,
        qty=qty,
        macros=macros,
        meal=meal,  # type: ignore[arg-type]
        alternates=alternates or None,
        state_question=True if _needs_state_question(food, phrase) else None,
        question=question,
        confidence=confidence,  # type: ignore[arg-type]
        servings=cascade._serving_options(food.get("servings") or []) or None,
        # §3.4a: a web-transcribed food carries its citation on the wire, so
        # every path that returns the item (kick, answer, poll) renders the link
        web_source=WebSource(**ws) if (ws := _web_source(food)) else None,
    )


async def _write_item_row(
    log_id: str,
    item_index: int,
    item: StagedItem,
    *,
    network_calls: int = 0,
    resolve_ms: int | None = None,
    proposed_food_id: str | None = None,
    trace_id: str | None = None,
    agent_transcript: list | None = None,
    disambiguated: bool = False,
    spoken_qty: float | None = None,
    spoken_unit: str | None = None,
    state_answer: str | None = None,
    tier: int = 3,
    web_source: dict | None = None,
) -> dict | None:
    """Write one item's staging state. Returns the staged jsonb AS STORED —
    KEEP_LOG_KEYS_SQL carries a photo row's `estimated` flag and
    `estimate_note` over the fresh payload (§3.4b), and every caller's
    response must show what the row holds, not what was proposed — or None
    when the row refused the write: BACKLOG 27 — a tier-3 run that finishes
    after the user removed the row (status='removed') or after the item
    committed (committed_at set) must never resurrect/clobber it, so the
    UPDATE is conditional on live statuses.

    §3.1 invariant: a row that started as a tier-0 expansion keeps tier 0 in
    the column whatever re-resolves it (its phrase column is a food display
    name — commit must never learn an alias for it). The log-level extras on
    the staged jsonb (logged_at, save_intent, when_hint_note) survive the
    rewrite via resolver.commit.KEEP_LOG_KEYS_SQL.
    """
    staged_payload = item.model_dump(mode="json", exclude_none=True)
    if spoken_qty is not None or spoken_unit is not None:
        # keep the spoken qty/unit alive across needs_input → answer chains
        staged_payload["spoken_qty"] = spoken_qty
        staged_payload["spoken_unit"] = spoken_unit
    if state_answer:
        # §3.4a: the user's cooked/raw/dry intent — commit persists it to
        # aliases.state_answer so the question is asked at most once per phrase
        staged_payload["state_answer"] = state_answer
    if web_source:
        # §3.4a REQUIREMENT: the clickable-citation block for web-transcribed
        # foods — staged key `web_source = {"url", "title"}` (integrator/V2
        # surfaces it on the wire and renders it as a visible link).
        staged_payload["web_source"] = web_source
    row = await db.fetchone(
        f"""
        UPDATE item_resolutions
        SET status = %s,
            staged = %s::jsonb{KEEP_LOG_KEYS_SQL},
            tier = CASE WHEN item_resolutions.tier = 0 THEN 0 ELSE %s END,
            network_calls = network_calls + %s,
            resolve_ms = %s,
            proposed_food_id = coalesce(proposed_food_id, %s),
            proposed_serving_id = coalesce(proposed_serving_id, %s),
            proposed_qty = coalesce(proposed_qty, %s),
            trace_id = coalesce(%s, trace_id),
            agent_transcript = coalesce(%s::jsonb, agent_transcript),
            was_disambiguated = was_disambiguated OR %s
        WHERE log_id = %s AND item_index = %s
          AND status IN ('searching', 'needs_input', 'not_found', 'resolved')
          AND committed_at IS NULL
        RETURNING staged
        """,
        (
            item.status,
            json.dumps(staged_payload),
            tier,
            network_calls,
            resolve_ms,
            proposed_food_id,
            item.serving.id if item.serving else None,
            item.qty,
            trace_id,
            json.dumps(agent_transcript, default=str) if agent_transcript else None,
            disambiguated,
            str(log_id),
            item_index,
        ),
    )
    return _as_dict(_col(row, 0, "staged")) if row is not None else None


def _stored_item(stored: dict | None, item: StagedItem) -> StagedItem:
    """The row as written — what the client renders (the §3.4b flag the
    payload never carried); the in-memory item when the write was refused."""
    return StagedItem.model_validate(stored) if stored else item


async def _load_item(log_id: str, item_index: int) -> tuple[str, dict]:
    row = await db.fetchone(
        "SELECT phrase, staged FROM item_resolutions WHERE log_id = %s AND item_index = %s",
        (str(log_id), item_index),
    )
    if row is None:
        raise LookupError(f"no item_resolutions row for log {log_id} item {item_index}")
    staged = _as_dict(_col(row, 1, "staged"))
    # The staged phrase is the CURRENT text (an edit may have replaced it); the
    # column keeps the original spoken phrase for §3.5 alias learning.
    return staged.get("phrase") or _col(row, 0, "phrase"), staged


def _spoken(staged_prev: dict) -> tuple[float | None, str | None]:
    """Spoken qty/unit carried in the staged jsonb (§7.3 rule 1)."""
    qty = staged_prev.get("spoken_qty")
    unit = staged_prev.get("spoken_unit")
    return (float(qty) if qty is not None else None), unit


_FOOD_COLS = (
    "id, source, source_id, source_url, provenance, name, display_name, brand, "
    "state, state_sensitive, servings, default_serving_id"
)


def _food_row(row: Any) -> tuple[str, dict]:
    """(food_row_id, food dict) from a foods row selected with _FOOD_COLS.

    with_loggable_servings runs here because rows cached before BACKLOG 184
    still carry a phantom `label` serving as their default — hydrating one
    unfiltered would stage a card with no numbers even though the row's
    per-100 serving is fine."""
    prov = _col(row, 4, "provenance")
    food = {
        "source": _col(row, 1, "source"),
        "source_id": _col(row, 2, "source_id"),
        "source_url": _col(row, 3, "source_url"),
        "provenance": _as_dict(prov) if prov else None,
        "name": _col(row, 5, "name"),
        "display_name": _col(row, 6, "display_name"),
        "brand": _col(row, 7, "brand"),
        "state": _col(row, 8, "state"),
        "state_sensitive": _col(row, 9, "state_sensitive"),
        "servings": _col(row, 10, "servings") or [],
        "default_serving_id": _col(row, 11, "default_serving_id"),
    }
    return str(_col(row, 0, "id")), with_loggable_servings(food)


async def resolve_item_from_source(
    log_id: str,
    item_index: int,
    source: str,
    source_id: str,
    display_name: str | None = None,
) -> StagedItem:
    """Resolve an item locally from an id-bearing answer option (§5 /api/resolve/answer):
    one get_food + persist for "fdc", a local cache read for "web"/"off" —
    never a second agent run.

    BACKLOG 29: `source` is client-supplied — whitelisted against the §4
    foods.source domain BEFORE anything is read, written, or fetched.
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"invalid source {source!r} (allowed: {', '.join(ALLOWED_SOURCES)})"
        )
    t0 = time.perf_counter()
    phrase, staged_prev = await _load_item(log_id, item_index)
    meal = staged_prev.get("meal") or "snack"
    spoken_qty, spoken_unit = _spoken(staged_prev)
    # §3.4a: a cooked/raw/dry answer already given on this row is the row's
    # memory, and every write below replaces the whole staged payload — carry
    # it across like the spoken qty/unit, or it is gone: commit never reaches
    # aliases.state_answer and the question returns on the next log of the
    # same phrase.
    state_answer = staged_prev.get("state_answer")

    food: dict | None = None
    food_row_id: str | None = None
    network_calls = 0
    if source == "fdc":
        network_calls = 1
        food = await fdc.get_food(source, str(source_id))
    else:
        # "web"/"off": no remote adapter — a web food exists only because a
        # tier-3 run persisted it, so resolve from the local cache alone.
        row = await db.fetchone(
            f"SELECT {_FOOD_COLS} FROM foods WHERE source = %s AND source_id = %s",
            (source, str(source_id)),
        )
        if row is not None:
            food_row_id, food = _food_row(row)
    if food is None or not food_committable(food):
        # The tapped option's id 404ed (stale index entry, §3.4), isn't in the
        # local cache, or carries no loggable serving (BACKLOG 184 — this is
        # the path the Oikos Triple Zero flavor tap took: the option resolved,
        # then staged a card with no numbers). Clean needs_input restoring the
        # question so another option can be tapped — not_found (manual search)
        # when no question survives. Never a crash.
        prev_q = staged_prev.get("question")
        item = StagedItem(
            item_index=item_index,
            phrase=phrase,
            status="needs_input" if prev_q else "not_found",
            tier=3,
            meal=meal,  # type: ignore[arg-type]
            question=Question.model_validate(prev_q) if prev_q else None,
            confidence="low",
        )
        stored = await _write_item_row(
            log_id, item_index, item,
            network_calls=network_calls,
            resolve_ms=int((time.perf_counter() - t0) * 1000),
            spoken_qty=spoken_qty,
            spoken_unit=spoken_unit,
            state_answer=state_answer,
        )
        return _stored_item(stored, item)
    food = dict(food)
    food.setdefault("source", source)
    food.setdefault("source_id", str(source_id))
    # `display_name` is a caller's AUTHORITATIVE name for this food — the
    # Library pick's chosen label (routes/foods.select_food, §8.9) — and it is
    # the row's name for life, because persist_food's ON CONFLICT never
    # rewrites it. So a caller holding only a per-answer string must pass
    # nothing: the question-chip path deliberately does not (routes/resolve.py),
    # since "Mixed berry" as the row's name is what the diary would render and
    # what the duplicate-detection trigram would score the next mention against.
    foods_repo.apply_label(food, display_name)  # owner only: the cache is shared
    if food_row_id is None:
        food_row_id = await foods_repo.persist_food(food)
    serving_id, qty, verify = await _apply_portion(
        food, None, spoken_qty, spoken_unit,
        food_row_id=str(food_row_id), phrase=phrase,
    )
    # §3.1: a manual pick on a tier-0 expansion row stays tier 0 (the macro
    # is the memory; no alias is learned for the display-name phrase)
    tier = 0 if staged_prev.get("tier") == 0 else 3
    item = _staged_from_food(
        item_index, phrase, meal, qty, food, str(food_row_id),
        serving_id=serving_id, confidence="low" if verify else "high",
        alternates=None, question=None, tier=tier,
    )
    if state_answer and item.state_question:
        # already answered on this row — don't ask a second time (the rule
        # run_tier3 applies to a refinement that answered it)
        item = item.model_copy(update={"state_question": None})
    stored = await _write_item_row(
        log_id, item_index, item,
        network_calls=network_calls,
        resolve_ms=int((time.perf_counter() - t0) * 1000),
        proposed_food_id=str(food_row_id),
        disambiguated=True,
        spoken_qty=spoken_qty,
        spoken_unit=spoken_unit,
        state_answer=state_answer,
        tier=tier,
        web_source=_web_source(food),
    )
    return _stored_item(stored, item)


async def resolve_item_from_food(
    log_id: str,
    item_index: int,
    food_id: str,
) -> StagedItem:
    """Resolve an item locally from an already-cached food (an alternates chip
    carrying a food_id, §7.1) — one local read, no network, no agent run."""
    t0 = time.perf_counter()
    phrase, staged_prev = await _load_item(log_id, item_index)
    meal = staged_prev.get("meal") or "snack"
    spoken_qty, spoken_unit = _spoken(staged_prev)
    row = await db.fetchone(
        f"SELECT {_FOOD_COLS} FROM foods WHERE id = %s",
        (str(food_id),),
    )
    if row is None:
        raise LookupError(f"unknown food {food_id}")
    _, food = _food_row(row)
    serving_id, qty, verify = await _apply_portion(
        food, None, spoken_qty, spoken_unit,
        food_row_id=str(food_id), phrase=phrase,
    )
    # §3.1: the prior tier is kept as-is — `or 3` re-stamped a tier-0
    # expansion as tier 3 the moment the user tapped an alternates chip
    tier = staged_prev.get("tier")
    if tier is None:
        tier = 3
    item = _staged_from_food(
        item_index, phrase, meal, qty, food, str(food_id),
        serving_id=serving_id, confidence="low" if verify else "high",
        alternates=None, question=None, tier=tier,
    )
    # §3.4a: same as the id-bearing path above — an answered cooked/raw/dry
    # question survives the chip tap that rewrites the payload, and is not
    # asked a second time on a row that already carries its answer.
    state_answer = staged_prev.get("state_answer")
    if state_answer and item.state_question:
        item = item.model_copy(update={"state_question": None})
    stored = await _write_item_row(
        log_id, item_index, item,
        resolve_ms=int((time.perf_counter() - t0) * 1000),
        proposed_food_id=str(food_id),
        disambiguated=True,
        spoken_qty=spoken_qty,
        spoken_unit=spoken_unit,
        state_answer=state_answer,
        tier=tier,
        web_source=_web_source(food),
    )
    return _stored_item(stored, item)


# ---------------------------------------------------------------------------
# run_tier3 — the workstream interface.
# ---------------------------------------------------------------------------


async def run_tier3(
    log_id: str, item_index: int, refinement: str | None = None
) -> StagedItem:
    """Run the tier-3 search agent for one staged item, persist the chosen food,
    update item_resolutions, and return the StagedItem.

    `refinement` carries a free-text question answer on the single re-kick path
    (§5 /api/resolve/answer); callers using the plain interface omit it.
    """
    _ensure_env()
    t0 = time.perf_counter()
    phrase, staged_prev = await _load_item(log_id, item_index)
    meal = staged_prev.get("meal") or "snack"
    spoken_qty, spoken_unit = _spoken(staged_prev)
    # §3.4a: the user's cooked/raw/dry intent, when the refinement or phrase
    # carries it — persisted into the staged payload for commit → alias.
    answered_state = phrase_state(refinement) if refinement else None
    state_intent = answered_state or phrase_state(phrase)
    # §3.4a rule 6: a flagged estimate (a photo row, an agent log_estimate row)
    # resolves to a best guess and never asks — the amount is already one.
    is_estimate = (staged_prev.get("estimated") or "none") != "none"

    candidates = await _local_candidates(phrase)
    agent_input = _agent_input(
        phrase,
        spoken_qty,
        spoken_unit,
        candidates,
        refinement,
        note=staged_prev.get("estimate_note"),
        estimated=is_estimate,
    )

    tracing.init_tracing()
    counter = {"network_calls": 0}
    food_cache: dict = {}
    persisted: dict = {}
    run_state: dict = {}
    agent = _build_agent(counter, food_cache, persisted, run_state)

    trace_id: str | None = None
    transcript: list | None = None
    out: Tier3Result
    # §10: the run's step in the route's trace (a trace of its own from the
    # evals); the SDK's agent, model-turn and tool spans nest under it
    async with tracing.run(
        "tier3", input=agent_input, attributes={"phrase": phrase, "item_index": item_index}
    ) as span:
        trace_id = span.trace_id
        try:
            result = await Runner.run(agent, agent_input, max_turns=MAX_TURNS)
            out = result.final_output_as(Tier3Result)
            transcript = result.to_input_list()
        except MaxTurnsExceeded as exc:
            # Deterministic failure — retrying identically would burn another
            # 6-turn run; fall through to the manual-search escape hatch.
            # It is also the most expensive way a run can end — ten turns of
            # hosted web_search, every action billed — so it is the last one
            # that should have no record of what it did. The SDK hangs the run
            # items off the exception (AgentsException.run_data): they are the
            # same items result.to_input_list() converts on the success path,
            # minus the prompt, which the trace already carries as the span's
            # input. Archiving them keeps the transcript readable and lets
            # _web_evidence below count the searches this run paid for. A
            # stubbed Runner may raise without run_data.
            out = Tier3Result(
                status="not_found",
                source=None,
                source_id=None,
                serving_id=None,
                display_name=None,
                confidence="low",
                alternates=[],
                question=None,
            )
            run_data = getattr(exc, "run_data", None)
            if run_data is not None:
                transcript = [it.to_input_item() for it in run_data.new_items]
            span.set(attributes={"max_turns_hit": True})
        span.set(output=out.model_dump(mode="json"))

    # §3.4/§9: what the hosted web_search consulted, and how many search
    # actions it ran — each one is a billed network call, counted like any other.
    web_evidence, web_searches = _web_evidence(transcript)
    counter["network_calls"] += web_searches

    # §3.4a/§0 "estimation has no code path", enforced: a web food this run
    # created must come from a page this run actually consulted (search
    # sources / url_citations / fetch_page); otherwise the row goes. This runs
    # over EVERY web food add_food persisted, not just a chosen one, and
    # before the branch below, because add_food writes the row into the shared
    # foods table the moment the model calls it — while the run can still end
    # at the turn cap, with a question, or in not_found, none of which reach a
    # chosen food. An un-evidenced row left behind by one of those exits is
    # searchable, loggable and re-offered to later runs forever (BACKLOG 37's
    # fix only ever covered the resolved path). The reason is archived with
    # the transcript so the demotion is auditable (§10).
    dropped_web: set[tuple] = set()
    for key, row_id in persisted.items():
        if key[0] != "web":
            continue
        gap = _web_evidence_gap(
            (food_cache.get(key) or {}).get("source_url") or "", web_evidence, run_state
        )
        if gap is not None:
            await _discard_web_food(row_id)
            dropped_web.add(key)
            transcript = list(transcript or []) + [
                {"type": "usual_web_gate", "web_evidence_gap": gap}
            ]

    # A chip may not name a row the gate above just deleted. The model can
    # offer a web food it created as an alternate or as an option on its
    # question, and those two lists reach the user on exactly the exits the
    # gate now covers (needs_input, not_found) — tapping one would call
    # resolve_item_from_source against a foods row that no longer exists and
    # turn a question into a bare not_found with nothing said about why. The
    # estimate promotion just below reads the same two lists, so this is
    # defined before it rather than beside the mapping it also guards.
    def _alive(source: str | None, source_id) -> bool:
        return (source, str(source_id)) not in dropped_web

    # Rule 6, enforced in code the way §3.4a's cooked/raw guard is rather than
    # left to the prompt: an estimate row must never come back as a question.
    # The amount is already a flagged guess the person is going to check, so a
    # question costs a tap per item and buys precision the row does not have
    # (trace 886b7b91: three of six photo rows asked, "which lettuce mix?"
    # among them). Promote the best candidate this run actually saw — a
    # question option carrying an id first, because a model that means to ask
    # puts its real candidates there and leaves `alternates` empty — at low
    # confidence, keeping the rest as alternates so the sheet still offers the
    # one-tap swap. Nothing to promote means not_found: the manual search box,
    # never a food this run did not see. Applied before the mapping below, so
    # the StagedItem's alternates and question are built from what stands.
    if out.status == "needs_input" and is_estimate:
        pool = [
            (o.source, o.source_id, o.label)
            for o in (out.question.options if out.question else [])
            if o.source and o.source_id and _alive(o.source, o.source_id)
        ] + [
            (a.source, a.source_id, a.label)
            for a in out.alternates
            if _alive(a.source, a.source_id)
        ]
        if pool:
            src, sid, label = pool[0]
            out = out.model_copy(
                update={
                    "status": "resolved",
                    "source": src,
                    "source_id": sid,
                    "display_name": label,
                    "serving_id": None,  # the entry's default; this pick chose none
                    "confidence": "low",
                    "alternates": [
                        Tier3Alternate(source=s, source_id=i, label=lab)
                        for s, i, lab in pool[1:4]
                    ],
                    "question": None,
                }
            )
        else:
            out = out.model_copy(update={"status": "not_found", "question": None})

    # --- map the structured output to a StagedItem -------------------------
    alternates = [
        Alternate(source=a.source, source_id=str(a.source_id), label=a.label)
        for a in out.alternates[:3]
        if _alive(a.source, a.source_id)
    ] or None
    question = None
    if out.question is not None:
        options = [
            QuestionOption(
                label=o.label,
                source=o.source,
                source_id=str(o.source_id) if o.source_id else None,
            )
            for o in out.question.options
            if o.source_id is None or _alive(o.source, o.source_id)
        ]
        # every option was a dropped web food — there is nothing left to ask
        question = Question(text=out.question.text, options=options) if options else None

    status = out.status
    if status == "resolved" and not (out.source and out.source_id):
        status = "not_found"  # malformed resolution — never render a food-less resolve
    if status == "needs_input" and question is None:
        # the gate took every option the question had — asking a question with
        # nothing to tap is worse than the manual search box
        status = "not_found"

    chosen: tuple | None = None
    food: dict | None = None
    if status == "resolved":
        # The chosen id can 404 (stale index entry, §3.4 adapter rules) —
        # retry the agent's alternates in order before giving up. A promoted
        # alternate drops the dead serving choice and demotes confidence to low.
        # A food the gate deleted is not a candidate at all — filtered out
        # BEFORE the walk, not vetoed after it, so a live alternate further
        # down the list still gets its turn instead of the whole resolution
        # falling to not_found on the dead first choice.
        #
        # A promoted alternate carries None where the chosen food carries
        # out.display_name: an alternate's `label` is the text of one chip
        # ("Mixed berry"), not a name for a row every account reads back, and
        # persist_food's ON CONFLICT never rewrites display_name — so a label
        # promoted here would be that food's name, and the trigram's, for life.
        # The adapter's own name is right. out.display_name stays: that is the
        # model naming the food it went and found.
        chosen, food = await _first_live_food(
            [
                cand
                for cand in [
                    (out.source, str(out.source_id), out.display_name,
                     out.serving_id, out.confidence)
                ]
                + [(a.source, str(a.source_id), None, None, "low")
                   for a in out.alternates]
                if _alive(cand[0], cand[1])
            ],
            counter, food_cache,
        )
        if food is None:
            status = "not_found"  # every candidate 404ed — manual search box

    if status == "resolved":
        src, sid, display_name, chosen_serving_id, confidence = chosen
        food = dict(food)
        food.setdefault("source", src)
        food.setdefault("source_id", sid)
        foods_repo.apply_label(food, display_name)  # owner only: the cache is shared
        food_row_id = persisted.get((src, sid)) or await foods_repo.persist_food(food)
        if src == "web" and (src, sid) in persisted:
            # §4: provenance = the agent's citations + what web_search actually
            # consulted this run (sources lists + url_citation annotations).
            if web_evidence:
                await db.execute(
                    "UPDATE foods SET provenance = coalesce(provenance, '{}'::jsonb) "
                    "|| %s::jsonb WHERE id = %s",
                    (json.dumps(web_evidence), str(food_row_id)),
                )
        # §7.3 full order (BACKLOG 25): spoken quantity wins, else the alias/
        # food portion history, else the agent's chosen serving × 1
        serving_id, qty, verify = await _apply_portion(
            food, chosen_serving_id, spoken_qty, spoken_unit,
            food_row_id=str(food_row_id), phrase=phrase,
        )
        shown_alternates = [
            Alternate(source=a.source, source_id=str(a.source_id), label=a.label)
            for a in out.alternates[:3]
            if (a.source, str(a.source_id)) != (src, sid)
        ] or None
        item = _staged_from_food(
            item_index, phrase, meal, qty, food, str(food_row_id),
            serving_id=serving_id,
            confidence="low" if verify else confidence,
            alternates=shown_alternates, question=None,
        )
        # §3.4a: a free-text refinement can BE the cooked/raw answer (the
        # toggle sends "cooked"/"raw" as text) — don't re-ask what was just
        # answered; the intent persists to the alias at commit.
        if answered_state and item.state_question:
            item = item.model_copy(update={"state_question": None})
        proposed_food_id: str | None = str(food_row_id)
        web_source = _web_source(food)
    else:
        item = StagedItem(
            item_index=item_index,
            phrase=phrase,
            status=status,
            tier=3,
            meal=meal,  # type: ignore[arg-type]
            alternates=alternates,
            question=question if status == "needs_input" else None,
            confidence=out.confidence,
        )
        proposed_food_id = None
        web_source = None

    stored = await _write_item_row(
        log_id, item_index, item,
        network_calls=counter["network_calls"],
        resolve_ms=int((time.perf_counter() - t0) * 1000),
        proposed_food_id=proposed_food_id,
        trace_id=trace_id,
        agent_transcript=transcript,
        spoken_qty=spoken_qty,
        spoken_unit=spoken_unit,
        state_answer=state_intent if item.status == "resolved" else None,
        web_source=web_source,
    )
    return _stored_item(stored, item)
