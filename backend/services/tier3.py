"""Tier-3 search agent — cold-start LLM-driven search over food sources.

DESIGN.md §3.4, §3.4a, §6.3. M0 scope: FDC only (web_search and OFF are later
phases). The agent only ever selects database ids — no model output carries
nutrition numbers; macros come exclusively from the cached `foods` row.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

from agents import Agent, MaxTurnsExceeded, Runner, function_tool
from pydantic import BaseModel

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
)
from resolver import cascade, foods_repo
from sources import fdc

MODEL = "gpt-5.6-terra"
MAX_TURNS = 6

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


# M0: FDC is the only live source — Literal makes an invalid source (e.g. the
# model inventing "branded") structurally impossible. Widen when OFF/web land.
Source = Literal["fdc"]


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
    source: Source | None  # chosen food, as a source pair
    source_id: str | None
    serving_id: str | None
    display_name: str | None  # short human name for the row + trigram index
    confidence: Literal["high", "medium", "low"]
    alternates: list[Tier3Alternate]  # ≤3, ids not payloads
    question: Tier3Question | None


# ---------------------------------------------------------------------------
# System instructions — §3.4 ranking/routing rules 1–5 verbatim
# (M0: web_search / OFF clauses omitted — later phases).
# ---------------------------------------------------------------------------

INSTRUCTIONS = """\
You are the tier-3 food resolver for a single-user nutrition logger. Map one spoken or
typed food phrase to exactly one food-database entry, or ask one precise question. Work
with the fewest tool calls that give a confident answer.

Tools:
- search_generic(query): USDA FDC generic/whole foods (SR Legacy + Foundation), condensed.
- search_branded(query): USDA FDC Branded (US packaged products), with brand owner.
- get_food(source, source_id): full detail (servings, per-100g macros) for one entry.
- get_food_history(n): the user's recent/frequent foods — use when the phrase might be
  shorthand for something he already eats.
- add_food(source, source_id, display_name): cache the chosen entry locally (from_source
  mode). Call it exactly once, for your final chosen food, before you answer.
  display_name is a short human name ("chicken breast, grilled") — never nutrition values.

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
3. Cooked vs raw: if the phrase or state field disambiguates, match it; otherwise prefer
   the cooked entry for state-sensitive foods (meat, fish, rice, pasta, legumes).
4. Query refinement allowed: if results are junk, refine and retry ("protein bar" ->
   "Quest protein bar chocolate chip cookie dough"; FDC's ranking is quirky — "chicken
   breast cooked" surfaces breaded tenders above plain roasted breast, and a more
   specific query fixes it). If the missing information is unknowable (which brand?),
   return a question instead of guessing.
5. Never mix sources for one food. A food identity lives in exactly one
   (source, source_id).

Local candidates: the input may list already-cached foods. If one of these
already-cached foods is the same real-world food, choose it — do not create a second
identity for the same food.

Output contract:
- Never output nutrition numbers. You only select database ids plus display strings.
- The only source in this phase is "fdc"; source_id is the FDC id from search results.
- "resolved": set source, source_id, serving_id (pick from get_food's servings — the
  entry's default, or the serving matching the spoken quantity/unit), display_name, and
  confidence (high = unambiguous, medium = refined guess, low = best available).
- alternates: up to 3 plausible runners-up as (source, source_id, label) — only entries
  you actually saw in results.
- "needs_input": set question with short text and 2–4 options; every option that refers
  to a candidate you already saw MUST carry its source and source_id so a tap resolves
  instantly without another search. Before asking a which-brand/flavor/variant question,
  run at least one search so the options are real candidates carrying ids — an id-less
  question is allowed only when no search could disambiguate (the answer exists only in
  the user's head).
- "not_found": nothing matched and no useful question exists.
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
) -> tuple[str | None, float, bool]:
    """§7.3 rule 1 for tier-3 resolutions: the spoken quantity always wins.

    Returns (serving_id, qty, verify_me). With no spoken quantity, the chosen
    (agent/default) serving × 1 stands.
    """
    if spoken_qty is None:
        return chosen_serving_id, 1.0, False
    row = {
        "servings": food.get("servings") or [],
        "food_default_serving_id": chosen_serving_id or food.get("default_serving_id"),
        "default_serving_id": None,
        "default_qty": None,
        "alias_id": None,
        "food_id": None,
    }
    return await cascade.resolve_portion(row, float(spoken_qty), spoken_unit)


# ---------------------------------------------------------------------------
# Local candidates — threshold-free (§3.4): top-N trigram matches however weak,
# plus his top-10 staples by use_count.
# ---------------------------------------------------------------------------


async def _local_candidates(phrase: str) -> list[dict]:
    trigram = await db.fetchall(
        """
        SELECT id, source, source_id, display_name, brand,
               similarity(display_name || ' ' || coalesce(brand, ''), %s) AS sim
        FROM foods
        ORDER BY sim DESC
        LIMIT 15
        """,
        (phrase,),
    )
    staples = await db.fetchall(
        """
        SELECT f.id, f.source, f.source_id, f.display_name, f.brand,
               sum(a.use_count) AS uses
        FROM foods f JOIN aliases a ON a.food_id = f.id
        GROUP BY f.id, f.source, f.source_id, f.display_name, f.brand
        ORDER BY uses DESC, max(a.last_used_at) DESC
        LIMIT 10
        """,
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
) -> str:
    lines = [f'Resolve this food phrase to a database entry: "{phrase}"']
    if qty is not None:
        lines.append(f"Spoken quantity: {qty}" + (f" {unit}" if unit else ""))
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
    """fdc.get_food through the run cache; None on 404, never cached as None."""
    key = (source, str(source_id))
    food = food_cache.get(key)
    if food is None:
        counter["network_calls"] += 1
        food = await fdc.get_food(source, str(source_id))
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
    """
    for cand in candidates:
        food = await _get_food_cached(cand[0], cand[1], counter, food_cache)
        if food is not None:
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
) -> Agent:
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
    async def add_food(source: Source, source_id: str, display_name: str) -> str:
        """Cache the chosen database entry into the local foods table (from_source mode).

        Call exactly once, for the final chosen food, before answering.
        """
        food = await _get_food_cached(source, str(source_id), counter, food_cache)
        if food is None:  # stale index id (404) — tell the agent, don't crash
            return _STALE_ID_ERROR
        food = dict(food)
        food.setdefault("source", source)
        food.setdefault("source_id", str(source_id))
        food["display_name"] = display_name.strip() or food.get("name", "")
        food_id = await foods_repo.persist_food(food)
        persisted[(source, str(source_id))] = str(food_id)
        food_cache[(source, str(source_id))] = food
        return json.dumps(
            {"food_id": str(food_id), "default_serving_id": food.get("default_serving_id")}
        )

    return Agent(
        name="tier3-food-resolver",
        instructions=INSTRUCTIONS,
        model=MODEL,
        tools=[search_generic, search_branded, get_food, get_food_history, add_food],
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
) -> None:
    staged_payload = item.model_dump(mode="json", exclude_none=True)
    if spoken_qty is not None or spoken_unit is not None:
        # keep the spoken qty/unit alive across needs_input → answer chains
        staged_payload["spoken_qty"] = spoken_qty
        staged_payload["spoken_unit"] = spoken_unit
    if state_answer:
        # §3.4a: the user's cooked/raw/dry intent — commit persists it to
        # aliases.state_answer so the question is asked at most once per phrase
        staged_payload["state_answer"] = state_answer
    await db.execute(
        """
        UPDATE item_resolutions
        SET status = %s,
            staged = %s::jsonb,
            tier = %s,
            network_calls = network_calls + %s,
            resolve_ms = %s,
            proposed_food_id = coalesce(proposed_food_id, %s),
            proposed_serving_id = coalesce(proposed_serving_id, %s),
            proposed_qty = coalesce(proposed_qty, %s),
            trace_id = coalesce(%s, trace_id),
            agent_transcript = coalesce(%s::jsonb, agent_transcript),
            was_disambiguated = was_disambiguated OR %s
        WHERE log_id = %s AND item_index = %s
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


async def resolve_item_from_source(
    log_id: str,
    item_index: int,
    source: str,
    source_id: str,
    display_name: str | None = None,
) -> StagedItem:
    """Resolve an item locally from an id-bearing answer option (§5 /api/resolve/answer):
    one get_food + persist, no second agent run."""
    t0 = time.perf_counter()
    phrase, staged_prev = await _load_item(log_id, item_index)
    meal = staged_prev.get("meal") or "snack"
    spoken_qty, spoken_unit = _spoken(staged_prev)
    food = await fdc.get_food(source, str(source_id))
    if food is None:
        # The tapped option's id 404ed (stale index entry, §3.4). Clean
        # needs_input restoring the question so another option can be tapped —
        # not_found (manual search) when no question survives. Never a crash.
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
        await _write_item_row(
            log_id, item_index, item,
            network_calls=1,
            resolve_ms=int((time.perf_counter() - t0) * 1000),
            spoken_qty=spoken_qty,
            spoken_unit=spoken_unit,
        )
        return item
    food = dict(food)
    food.setdefault("source", source)
    food.setdefault("source_id", str(source_id))
    if display_name:
        food["display_name"] = display_name
    elif not food.get("display_name"):
        food["display_name"] = food.get("name", "")
    food_row_id = await foods_repo.persist_food(food)
    serving_id, qty, verify = await _apply_portion(food, None, spoken_qty, spoken_unit)
    item = _staged_from_food(
        item_index, phrase, meal, qty, food, str(food_row_id),
        serving_id=serving_id, confidence="low" if verify else "high",
        alternates=None, question=None,
    )
    await _write_item_row(
        log_id, item_index, item,
        network_calls=1,
        resolve_ms=int((time.perf_counter() - t0) * 1000),
        proposed_food_id=str(food_row_id),
        disambiguated=True,
        spoken_qty=spoken_qty,
        spoken_unit=spoken_unit,
    )
    return item


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
        "SELECT name, display_name, brand, state, state_sensitive, servings, "
        "default_serving_id FROM foods WHERE id = %s",
        (str(food_id),),
    )
    if row is None:
        raise LookupError(f"unknown food {food_id}")
    food = {
        "name": _col(row, 0, "name"),
        "display_name": _col(row, 1, "display_name"),
        "brand": _col(row, 2, "brand"),
        "state": _col(row, 3, "state"),
        "state_sensitive": _col(row, 4, "state_sensitive"),
        "servings": _col(row, 5, "servings") or [],
        "default_serving_id": _col(row, 6, "default_serving_id"),
    }
    serving_id, qty, verify = await _apply_portion(food, None, spoken_qty, spoken_unit)
    tier = staged_prev.get("tier") or 3
    item = _staged_from_food(
        item_index, phrase, meal, qty, food, str(food_id),
        serving_id=serving_id, confidence="low" if verify else "high",
        alternates=None, question=None, tier=tier,
    )
    await _write_item_row(
        log_id, item_index, item,
        resolve_ms=int((time.perf_counter() - t0) * 1000),
        proposed_food_id=str(food_id),
        disambiguated=True,
        spoken_qty=spoken_qty,
        spoken_unit=spoken_unit,
        tier=tier,
    )
    return item


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

    candidates = await _local_candidates(phrase)
    agent_input = _agent_input(phrase, spoken_qty, spoken_unit, candidates, refinement)

    tracing.init_tracing()
    counter = {"network_calls": 0}
    food_cache: dict = {}
    persisted: dict = {}
    agent = _build_agent(counter, food_cache, persisted)

    trace_id: str | None = None
    transcript: list | None = None
    out: Tier3Result
    try:
        async with tracing.generation("tier3") as gen:
            trace_id = getattr(gen, "trace_id", None)
            try:
                result = await Runner.run(agent, agent_input, max_turns=MAX_TURNS)
                out = result.final_output_as(Tier3Result)
                transcript = result.to_input_list()
            except MaxTurnsExceeded:
                # Deterministic failure — retrying identically would burn another
                # 6-turn run; fall through to the manual-search escape hatch.
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
            gen.update(input=agent_input, output=out.model_dump(mode="json"))
    finally:
        tracing.flush()

    # --- map the structured output to a StagedItem -------------------------
    alternates = [
        Alternate(source=a.source, source_id=str(a.source_id), label=a.label)
        for a in out.alternates[:3]
    ] or None
    question = None
    if out.question is not None:
        question = Question(
            text=out.question.text,
            options=[
                QuestionOption(
                    label=o.label,
                    source=o.source,
                    source_id=str(o.source_id) if o.source_id else None,
                )
                for o in out.question.options
            ],
        )

    status = out.status
    if status == "resolved" and not (out.source and out.source_id):
        status = "not_found"  # malformed resolution — never render a food-less resolve

    chosen: tuple | None = None
    food: dict | None = None
    if status == "resolved":
        # The chosen id can 404 (stale index entry, §3.4 adapter rules) —
        # retry the agent's alternates in order before giving up. A promoted
        # alternate keeps its label as display name, drops the dead serving
        # choice, and demotes confidence to low.
        chosen, food = await _first_live_food(
            [(out.source, str(out.source_id), out.display_name, out.serving_id,
              out.confidence)]
            + [(a.source, str(a.source_id), a.label, None, "low")
               for a in out.alternates],
            counter, food_cache,
        )
        if food is None:
            status = "not_found"  # every candidate 404ed — manual search box

    if status == "resolved":
        src, sid, display_name, chosen_serving_id, confidence = chosen
        food = dict(food)
        food.setdefault("source", src)
        food.setdefault("source_id", sid)
        if display_name:
            food["display_name"] = display_name
        elif not food.get("display_name"):
            food["display_name"] = food.get("name", "")
        food_row_id = persisted.get((src, sid)) or await foods_repo.persist_food(food)
        # §7.3 rule 1: the spoken quantity always wins over the agent's serving
        serving_id, qty, verify = await _apply_portion(
            food, chosen_serving_id, spoken_qty, spoken_unit
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

    await _write_item_row(
        log_id, item_index, item,
        network_calls=counter["network_calls"],
        resolve_ms=int((time.perf_counter() - t0) * 1000),
        proposed_food_id=proposed_food_id,
        trace_id=trace_id,
        agent_transcript=transcript,
        spoken_qty=spoken_qty,
        spoken_unit=spoken_unit,
        state_answer=state_intent if item.status == "resolved" else None,
    )
    return item
