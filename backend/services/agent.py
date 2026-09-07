"""The agent lane's front door — DESIGN.md §6.4 (2026-09-06).

Every utterance that enters the + sheet's Agent lane (spoken or typed) runs
through this agent once the §3.0 whole-utterance short-circuit has passed on
it. The agent resolves nothing itself: it reads the utterance, hands the food
part — verbatim, in ONE call — to `log_food` (the unchanged decompose →
cascade → staging pipeline, behind a callback routes/logs.py injects), an
explicitly stated body weight to `log_weight` (routes/weights.record_weight,
behind the other callback), and — since the chat redesign the same day — a
food that has NO nutrition label anywhere (a deli platter, a non-chain
restaurant plate, a homemade dish) to `log_estimate`: the agent breaks it into
plain component foods the databases carry and estimates the AMOUNT of each,
after a hosted `web_search` or two to learn what the product is and how big.
The numbers still come only from the database entry of each component; the
amount is flagged `estimated="portion"` (the photo lane's flag, §3.4a) and the
row must be confirmed before it commits. The tools are its only effects, and
the route reports what the tools actually did — never what the model says it
did: `AgentRun` is assembled from tool-side state; the reply is the only model
text that reaches the client.

The run streams: `run_agent(..., emit=...)` hands each tool call, each tool
result and the reply to the caller as it happens (the chat window shows them,
routes/agent.py), through `Runner.run_streamed`. Without `emit` the same code
path runs silently (POST /api/logs, the tests).

Model: gpt-5.6-terra — the research-and-judgement tier (tier 3's), because
sizing an unlabeled platter is that class of work; the two-tool routing call
this used to be ran on luna (§8.10). Escalation is one constant, as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Literal

from agents import Agent, MaxTurnsExceeded, ModelSettings, Runner, WebSearchTool, function_tool
from agents.items import ToolCallItem, ToolCallOutputItem
from pydantic import BaseModel

MODEL = "gpt-5.6-terra"  # pinned tier id — never the bare gpt-5.6 alias (§6)
MAX_TURNS = 10  # a search or two, up to three logging tools, the answer
MAX_DAYS_AGO = 30
MAX_ESTIMATE_ITEMS = 8
# a tool output larger than this is summarised for the chat, not shipped whole
_MAX_WIRE_OUTPUT_CHARS = 4_000

Unit = Literal["kg", "lb"]

INSTRUCTIONS = """\
You are Tally's agent: the front door for ONE spoken or typed utterance from a
person logging their nutrition. You act only through your tools, then answer
in one or two short sentences. You never state calories or macros — the
database supplies those and the staged list shows them.

Tools:
- log_food(text): stages what they ate or drank through the food pipeline,
  which looks every item up in the databases (USDA whole foods, branded
  packaged products, and — for chain restaurants and retail products — the
  maker's official nutrition page). Pass the food part of the utterance
  VERBATIM: keep every quantity, unit, brand, meal word ("for breakfast") and
  time reference ("this morning", "yesterday lunch") exactly as said, in the
  same order; leave out only the words about body weight and anything you
  route to log_estimate. Call it AT MOST ONCE, with all such food in one
  call. Never paraphrase, convert units, or add a food that was not said.
- log_estimate(items, basis): for food that has NO nutrition label anywhere —
  a store's deli or prepared-foods platter, a plate from a restaurant that is
  not a chain publishing nutrition, a homemade dish, "some of" a shared tray.
  Break it into plain component foods the databases DO have ("shrimp,
  cooked", "cocktail sauce", "white rice, cooked") and estimate the AMOUNT of
  each. Give amounts by WEIGHT ("g" or "oz") whenever the food is something
  weighed — convert a piece count with a typical piece weight and put the
  count in the item's note ("~50 shrimp at ~8 g each"); use a bare count (unit
  null) only for foods whose serving is one piece (an egg, a slice, a cookie).
  Each row is staged flagged as an estimate for the person to confirm or
  adjust; the numbers come from the database entry of each component, never
  from you. `basis` is one sentence on how you sized the whole thing (the
  product's stated weight or piece count, what comes with it). Call it AT
  MOST ONCE, with all such items in one call. Never for a packaged product, a
  chain menu item or a plain whole food — those go to log_food.
- web_search: research a product you need to understand before logging it —
  what it is, what it contains, how much it weighs, how many pieces, what
  comes with it. Use it to decide between log_food and log_estimate and to
  size an estimate. Never carry nutrition numbers from a web page into
  anything: you pass foods and amounts; the databases supply the numbers. One
  or two searches at most.
- log_weight(weight, unit, days_ago): records a body weight the person stated
  as a NUMBER ("I weigh 180", "180.4 pounds this morning", "82 kilos
  yesterday"). `unit` is the unit they said; when none was said, use their
  usual unit from the context. `days_ago` is 0 for today, 1 for yesterday.
  A weight that was not spoken as a number is not a weight — "I'm down two
  pounds" states none; do not call the tool.

How to decide:
- Plain foods, packaged products with a Nutrition Facts label, chain-restaurant
  items → log_food, verbatim. Unsure whether a product carries a label? One
  web_search settles it.
- A store's prepared-foods or deli item (a warehouse-club shrimp platter, a
  rotisserie-chicken salad from the deli case), a non-chain restaurant dish,
  a homemade meal → web_search to learn its make-up and size when you do not
  already know them, then log_estimate with the components and amounts. When
  the person names the whole thing ("a platter"), size the whole thing and
  say so in `basis`; when they name a part, size that part.
- An utterance can hold several of these (call each tool it needs, each at
  most once) or none — a question, a greeting, noise means no tool call.

Then answer with `reply`: one or two short sentences for the person, in plain
words, confirming what the tools reported — and, for an estimate, what you
assumed, so they can correct it. Use only numbers the tool results gave you
(counts and amounts are fine; calories and macros never). Never mention tool
names. If a tool reported an error, say plainly what could not be done.
"""

# Appended for a non-owner run (§8.9): the hosted web_search is billed per
# action, so a demo account's agent has no web lane and is told so.
_NO_WEB_LANE = """

web_search is not available on this account: decide between log_food and
log_estimate from what you already know, and size estimates the same way.
"""


class EstimateItem(BaseModel):
    """One component of an unlabeled food, as the agent sizes it."""

    phrase: str  # a plain, database-searchable food name ("shrimp, cooked")
    qty: float  # the estimated amount, > 0
    unit: str | None  # "g", "oz", "cup", "tbsp" … — or null for a count of pieces
    note: str  # one short clause on how the amount was sized ("~50 shrimp at ~8 g each")


class AgentReply(BaseModel):
    reply: str


Emit = Callable[[dict], Awaitable[None]]


@dataclass
class AgentDeps:
    """What the route injects: the effects, and the context the model reads."""

    # the food part of the utterance → a summary of what was staged (no macros)
    stage_food: Callable[[str], Awaitable[dict]]
    # (component items, basis) → a summary of the flagged rows staged (no macros)
    stage_estimate: Callable[[list[EstimateItem], str], Awaitable[dict]]
    # (weight as spoken, unit, local day) → the stored entry + the change since
    # the previous one; raises ValueError when the value is out of range
    record_weight: Callable[[float, Unit, date], Awaitable[dict]]
    now_local: datetime  # the user's wall clock (tz-aware), for days_ago
    unit_pref: Unit  # the unit to assume when none was spoken
    last_weight: dict | None = None  # {"day", "weight", "unit"} of the newest weigh-in
    profile_notes: str = ""
    web_lane: bool = True  # False for a demo account (§8.9): no hosted web_search


@dataclass
class AgentRun:
    """What actually happened — assembled from tool-side state, not model claims."""

    reply: str
    food_calls: int  # log_food calls that staged something (0 or 1)
    estimate_calls: int  # log_estimate calls that staged something (0 or 1)
    weight: dict | None
    transcript: list
    max_turns_hit: bool = False


def _tool_error(ctx, error: Exception) -> str:
    """Sanitized tool failure (the SDK default embeds str(error); a database or
    provider message must never reach the model, the traces, or a reply)."""
    return f"{type(error).__name__}: that could not be saved. Tell the user it did not go through."


def build_agent(deps: AgentDeps, state: dict[str, Any]) -> Agent:
    """One agent per run: the tools close over `state`, which is what the
    route reads back (food_calls, estimate_calls, weight)."""
    state.setdefault("food_calls", 0)
    state.setdefault("estimate_calls", 0)
    state.setdefault("weight", None)

    @function_tool(failure_error_function=_tool_error)
    async def log_food(text: str) -> str:
        """Stage everything the user ate or drank that has a label or a
        database entry: the food part of the utterance copied verbatim
        (quantities, brands, meal and time words included; the weight
        sentence and any unlabeled dish left out). At most once per utterance."""
        if state["food_calls"] >= 1:
            return json.dumps({
                "error": "already_called",
                "message": "log_food runs once per utterance — everything was already staged.",
            })
        # claimed before the await (two parallel calls must not both stage),
        # released on failure: the pipeline persists in one transaction, so a
        # failed call staged nothing and the model may try once more
        state["food_calls"] += 1
        try:
            out = await deps.stage_food(text)
        except Exception:
            state["food_calls"] -= 1
            raise  # failure_error_function tells the model, without the internals
        return json.dumps(out, default=str)

    @function_tool(failure_error_function=_tool_error)
    async def log_estimate(items: list[EstimateItem], basis: str) -> str:
        """Stage the component foods of something with NO nutrition label,
        each with the amount you estimate (by weight where the food is
        weighed; the piece count in its note). Every row is flagged as an
        estimate for the user to confirm. `basis`: one sentence on how you
        sized it. At most once per utterance."""
        if state["estimate_calls"] >= 1:
            return json.dumps({
                "error": "already_called",
                "message": "log_estimate runs once per utterance — the estimate was already staged.",
            })
        if not items:
            return json.dumps({"error": "empty", "message": "no items to stage"})
        bad = [it.phrase for it in items if not (it.qty > 0)]
        if bad:
            return json.dumps({
                "error": "invalid_qty",
                "message": f"every amount must be above zero: {bad}",
            })
        items = items[:MAX_ESTIMATE_ITEMS]
        state["estimate_calls"] += 1
        try:
            out = await deps.stage_estimate(items, basis.strip())
        except Exception:
            state["estimate_calls"] -= 1
            raise
        return json.dumps(out, default=str)

    @function_tool(failure_error_function=_tool_error)
    async def log_weight(weight: float, unit: Unit, days_ago: int = 0) -> str:
        """Record a body weight the user explicitly stated as a number:
        `weight` as spoken, `unit` as spoken (or the usual unit when none was
        said), days_ago 0 = today, 1 = yesterday."""
        days_ago = max(0, min(int(days_ago), MAX_DAYS_AGO))
        day = (deps.now_local - timedelta(days=days_ago)).date()
        try:
            entry = await deps.record_weight(float(weight), unit, day)
        except ValueError as e:
            return json.dumps({"error": "invalid_weight", "message": str(e)})
        state["weight"] = entry
        return json.dumps(entry, default=str)

    tools: list = [log_food, log_estimate, log_weight]
    if deps.web_lane:
        # unpinned: this search identifies a product and its size, never a
        # page to transcribe numbers from (tier 3's pinned lane does that)
        tools.append(WebSearchTool(search_context_size="medium"))
    return Agent(
        name="tally-agent",
        instructions=INSTRUCTIONS + ("" if deps.web_lane else _NO_WEB_LANE),
        model=MODEL,
        tools=tools,
        # the consulted-sources list rides each web_search action — the chat
        # shows where the agent looked
        model_settings=ModelSettings(response_include=["web_search_call.action.sources"]),
        output_type=AgentReply,
    )


def agent_input(text: str, deps: AgentDeps) -> str:
    """The per-run block: local time, the unit to assume, the last weigh-in,
    the profile notes (§6.1), then the utterance."""
    if deps.last_weight:
        lw = deps.last_weight
        last = f"{lw['weight']:g} {lw['unit']} on {lw['day']}"
    else:
        last = "none yet"
    return (
        f"Now (the user's local time): {deps.now_local.strftime('%Y-%m-%d %H:%M, %A')}\n"
        f"Usual weight unit: {deps.unit_pref}\n"
        f"Last weigh-in: {last}\n"
        f"Profile notes: {deps.profile_notes or '(none)'}\n\n"
        f'Utterance: "{text}"'
    )


def _fallback_reply(state: dict) -> str:
    """The reply when the model never produced one (max turns): what the
    tools did, said in code."""
    parts = []
    if state.get("food_calls"):
        parts.append("staged what you ate")
    if state.get("estimate_calls"):
        parts.append("staged an estimate for you to check")
    if state.get("weight"):
        parts.append("logged your weight")
    if not parts:
        return "I couldn't work that one out — try again, or type it."
    return "I " + " and ".join(parts) + " — check the list."


# ---------------------------------------------------------------------------
# The wire events (routes/agent.py streams them; the chat renders them)
# ---------------------------------------------------------------------------


def _parse_output(output: Any) -> Any:
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            pass
    text = json.dumps(output, default=str)
    if len(text) > _MAX_WIRE_OUTPUT_CHARS:
        return {"summary": text[:_MAX_WIRE_OUTPUT_CHARS] + "…", "truncated": True}
    return output


def wire_events(event: Any, names: dict[str, str]) -> list[dict]:
    """Translate one SDK stream event into the chat's events (zero, one or
    two). `names` maps call ids to tool names across the run, so a result can
    say which tool it belongs to. Hosted web_search has no separate output
    event: its call and its consulted sources arrive together."""
    if getattr(event, "type", None) != "run_item_stream_event":
        return []
    item = event.item
    if event.name == "tool_called" and isinstance(item, ToolCallItem):
        raw = item.raw_item
        kind = raw.get("type") if isinstance(raw, dict) else getattr(raw, "type", None)
        if kind == "web_search_call":
            action = getattr(raw, "action", None)
            query = getattr(action, "query", None)
            sources = [
                getattr(s, "url", None) for s in (getattr(action, "sources", None) or [])
            ]
            call_id = str(getattr(raw, "id", "") or "")
            names[call_id] = "web_search"
            out: list[dict] = [
                {"type": "tool_call", "id": call_id, "name": "web_search",
                 "args": {"query": query}},
            ]
            if sources:
                out.append({
                    "type": "tool_result", "id": call_id, "name": "web_search",
                    "output": {"sources": [s for s in sources if s]},
                })
            return out
        if kind == "function_call":
            name = item.tool_name or "tool"
            call_id = str(item.call_id or "")
            raw_args = raw.get("arguments") if isinstance(raw, dict) else getattr(raw, "arguments", None)
            try:
                args = json.loads(raw_args or "{}")
            except ValueError:
                args = {"raw": raw_args}
            names[call_id] = name
            return [{"type": "tool_call", "id": call_id, "name": name, "args": args}]
        return []
    if event.name == "tool_output" and isinstance(item, ToolCallOutputItem):
        call_id = str(item.call_id or "")
        return [{
            "type": "tool_result", "id": call_id, "name": names.get(call_id),
            "output": _parse_output(item.output),
        }]
    return []


async def run_agent(text: str, deps: AgentDeps, emit: Emit | None = None) -> AgentRun:
    """One streamed run. `emit` (optional) receives each wire event as it
    happens — tool_call, tool_result, then the reply; the route adds `done`.
    Every effect is a tool's; the reply is the model's one contribution."""
    state: dict[str, Any] = {}
    agent = build_agent(deps, state)
    transcript: list = []
    max_turns_hit = False
    names: dict[str, str] = {}
    try:
        result = Runner.run_streamed(agent, agent_input(text, deps), max_turns=MAX_TURNS)
        async for event in result.stream_events():
            if emit is None:
                continue
            for ev in wire_events(event, names):
                await emit(ev)
        final = result.final_output
        reply = (getattr(final, "reply", "") or "").strip() or _fallback_reply(state)
        transcript = result.to_input_list()
    except MaxTurnsExceeded:
        # the tools' effects stand; the reply is built from them
        max_turns_hit = True
        reply = _fallback_reply(state)
    if emit is not None:
        await emit({"type": "reply", "text": reply})
    return AgentRun(
        reply=reply,
        food_calls=state["food_calls"],
        estimate_calls=state["estimate_calls"],
        weight=state["weight"],
        transcript=transcript,
        max_turns_hit=max_turns_hit,
    )
