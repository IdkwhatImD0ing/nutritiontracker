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
row must be confirmed before it commits. Since 2026-09-07 it also keeps the
saved meals (§8.4) — `save_meal` / `update_meal`, the Library's own writes
behind two more callbacks — because the agent can do whatever the UI can
(Bill's ruling). The tools are its only effects, and
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
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Literal

from agents import Agent, MaxTurnsExceeded, ModelSettings, Runner, WebSearchTool, function_tool
from agents.items import ToolCallItem, ToolCallOutputItem
from pydantic import BaseModel

log = logging.getLogger(__name__)

MODEL = "gpt-5.6-terra"  # pinned tier id — never the bare gpt-5.6 alias (§6)
MAX_TURNS = 10  # a search or two, up to three logging tools, the answer
MAX_DAYS_AGO = 30
MAX_ESTIMATE_ITEMS = 8
# a tool output larger than this is summarised for the chat, not shipped whole
_MAX_WIRE_OUTPUT_CHARS = 4_000

Unit = Literal["kg", "lb"]
MealName = Literal["breakfast", "lunch", "dinner", "snack"]

INSTRUCTIONS = """\
You are Tally's agent: the front door for ONE spoken or typed utterance from a
person logging their nutrition. The conversation so far may ride in with it —
what they said in earlier turns, what you replied, what each turn staged (with
its log id) and whether that log is still staged, committed, or discarded. Use
it to understand references ("the same again", "make that two"), to take the
answer to a question you asked, and to handle corrections. You act only
through your tools, then answer in one or two short sentences. You never
state calories or macros — the database supplies those and the staged list
shows them.

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
  each. Give a countable food as a COUNT of pieces (unit null — "50 shrimp";
  the database serving's own piece count is handled) WITH its total weight in
  `grams` (pieces × a typical piece weight), so it can be sized either way;
  anything else by weight ("g" or "oz") or the measure it comes in ("cup",
  "tbsp"), `grams` null. Say how you arrived at the amount in the item's note
  ("~50 shrimp, the whole 3.75 lb tray"). Each row is staged flagged as an
  estimate for the person to confirm or
  adjust; the numbers come from the database entry of each component, never
  from you. `basis` is one sentence on how you sized the whole thing (the
  product's stated weight or piece count, what comes with it). When the
  person said which meal or when ("for breakfast", "yesterday at lunch",
  "this morning", "last night"), pass the meal word as `meal_hint` and the
  time phrase as `when_hint`, exactly as said — the same words log_food keeps
  verbatim — so the rows land on the right day and meal. Call it AT MOST
  ONCE, with all such items in one call. Never for a packaged product, a
  chain menu item or a plain whole food — those go to log_food.
- web_search: research a product you need to understand before logging it —
  what it is, what it contains, how much it weighs, how many pieces, what
  comes with it. Use it to decide between log_food and log_estimate and to
  size an estimate. Never carry nutrition numbers from a web page into
  anything: you pass foods and amounts; the databases supply the numbers. One
  or two searches at most.
- discard_log(log_id): for a CORRECTION — the person changes what an earlier
  turn of this conversation staged ("actually I only had half", "make that two
  eggs", "no, the small tray"). Call it with that turn's log id (from the
  conversation) while the log is still staged, then stage the corrected version
  with log_food / log_estimate as if said fresh. A log the conversation shows
  as discarded — or that the tool reports already discarded (the person closed
  that sheet) — has nothing to replace: just stage the corrected version. A log
  that is already committed cannot be replaced: do not restage — say it is in
  the diary and can be edited there. Never stage a duplicate of something the
  conversation shows as staged or committed.
- log_weight(weight, unit, days_ago): records a body weight the person stated
  as a NUMBER ("I weigh 180", "180.4 pounds this morning", "82 kilos
  yesterday"). `unit` is the unit they said; when none was said, use their
  usual unit from the context. `days_ago` is 0 for today, 1 for yesterday.
  A weight that was not spoken as a number is not a weight — "I'm down two
  pounds" states none; do not call the tool.
- save_meal(name, trigger_phrases, log_id, items): saves a named meal — their
  "usual breakfast" — so saying its name logs it again, or puts more into one
  that exists (the saved meals are listed in the context). The items come from
  ONE source. A log: `log_id` when they point at a turn of this conversation
  ("save the lunch one as…"); null when they mean what was just logged — this
  turn's log, else the newest log in the conversation, else the last committed
  one. A log that is still staged is saved when they commit it; a committed
  one is saved now; a log REPLACES what a meal of that name held. Or `items`:
  foods in words ("make a meal called protein snack: a scoop of whey and a
  banana", "add a banana to my usual breakfast") — plain food names with the
  amount as said, looked up in the databases like log_food's — ADDED to the
  meal of that name, or making a new one. A food the databases do not know
  yet comes back unresolved: say so, and that logging it once makes it known.
  `trigger_phrases`: other ways they said they will call it, verbatim; null =
  the name.
- update_meal(name, new_name, trigger_phrases, is_active, set_qty, remove):
  changes a saved meal that exists: rename, replace its trigger phrases, set
  an item's quantity or remove an item — the item by its food name as the
  saved-meals list shows it — deactivate (`is_active` false: their "delete";
  it stays in Library as inactive and the diary keeps its context) or
  reactivate. Pass only what they asked to change; the rest null.

How to decide:
- Saving, naming, or changing a saved meal → save_meal / update_meal, never
  log_food. LOGGING a saved meal ("my usual breakfast") is log_food, like any
  food. Food and a save in one breath ("two eggs and toast, save that as my
  usual breakfast") → log_food first, its text verbatim as always, then
  save_meal once log_food has returned.
- Plain foods, packaged products with a Nutrition Facts label, chain-restaurant
  items → log_food, verbatim. Unsure whether a product carries a label? One
  web_search settles it.
- A store's prepared-foods or deli item (a warehouse-club shrimp platter, a
  rotisserie-chicken salad from the deli case), a non-chain restaurant dish,
  a homemade meal → web_search to learn its make-up and size when you do not
  already know them, then log_estimate with the components and amounts. When
  the person names the whole thing ("a platter"), size the whole thing and
  say so in `basis`; when they name a part, size that part.
- ASK before you stage when the utterance leaves open something the numbers
  hinge on and nothing in the conversation settles it: which product ("costco
  shrimp" is a 3.75 lb service-deli party platter, a ~1.5 lb refrigerated
  cocktail tray and a 32 oz frozen ring), how much of it ("a platter" — all of
  it, or a share?), or which of several very different foods a word could
  mean. Then call NO tool: put the question in `reply` and 2–4 short answers in
  `options` (the likeliest first; the last may be "something else"). Ask about
  ONE thing per turn — the one the numbers hinge on most — and make the
  options complete answers to that one thing; if a second thing is still open
  after the answer, ask it then. Do not ask about what the databases resolve on their own
  (variants of a labeled product — log_food handles those), and never ask what
  the person already said, or answered in an earlier turn.
- When the utterance answers your last question (the conversation shows it),
  proceed with what it settles; do not ask again.
- An utterance can hold several of these (call each tool it needs, each at
  most once) or none — a question, a greeting, noise means no tool call.

Then answer with `reply`: one or two short sentences for the person, in plain
words, confirming what the tools reported — and, for an estimate, what you
assumed, so they can correct it. Use only numbers the tool results gave you
(counts and amounts are fine; calories and macros never). Never mention tool
names. Plain text only — no markdown, the chat shows it verbatim. If a tool
reported an error, say plainly what could not be done.
`options` is null unless `reply` is a clarifying question.
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
    # for a count: the estimated total weight in grams (pieces × a typical
    # piece weight), so a food without a count-type serving is sized by weight
    # instead of pieces × 100 g; null for an item given by weight or measure
    grams: float | None
    note: str  # one short clause on how the amount was sized ("~50 shrimp at ~8 g each")


class MealFood(BaseModel):
    """A food in words, for save_meal.items — looked up through the same
    tiers log_estimate's components take; nothing is logged."""

    phrase: str  # a plain, database-searchable food name ("eggs", "whey")
    qty: float | None  # the amount as said; null = the usual portion, once
    unit: str | None  # "scoop", "cup", "g", "oz" … — null for a count of pieces


class MealQty(BaseModel):
    """One item's new quantity, for update_meal.set_qty."""

    food: str  # the item's food name as the saved-meals list shows it
    qty: float  # > 0


class AgentReply(BaseModel):
    reply: str
    # 2–4 short answers when `reply` is a clarifying question (the chat shows
    # them as chips); null otherwise. Required-but-nullable for strict mode.
    options: list[str] | None


Emit = Callable[[dict], Awaitable[None]]


@dataclass
class AgentDeps:
    """What the route injects: the effects, and the context the model reads."""

    # the food part of the utterance → a summary of what was staged (no macros)
    stage_food: Callable[[str], Awaitable[dict]]
    # (component items, basis, meal_hint, when_hint) → a summary of the
    # flagged rows staged (no macros)
    stage_estimate: Callable[
        [list[EstimateItem], str, MealName | None, str | None], Awaitable[dict]
    ]
    # (weight as spoken, unit, local day) → the stored entry + the change since
    # the previous one; raises ValueError when the value is out of range
    record_weight: Callable[[float, Unit, date], Awaitable[dict]]
    now_local: datetime  # the user's wall clock (tz-aware), for days_ago
    unit_pref: Unit  # the unit to assume when none was spoken
    last_weight: dict | None = None  # {"day", "weight", "unit"} of the newest weigh-in
    profile_notes: str = ""
    web_lane: bool = True  # False for a demo account (§8.9): no hosted web_search
    # a staged log from this conversation this turn replaces → its new status
    discard_log: Callable[[str], Awaitable[dict]] | None = None
    # the chat so far, oldest first: models.AgentTurn as dicts
    history: list[dict] = field(default_factory=list)
    # §8.4 through the agent: (name, trigger_phrases, log_id, items) → the
    # meal as saved, or that it saves when the log commits; (name, new_name,
    # trigger_phrases, is_active, set_qty, remove) → the meal as it is now
    save_meal: Callable[..., Awaitable[dict]] | None = None
    update_meal: Callable[..., Awaitable[dict]] | None = None
    # the saved meals as they stand, one line each (name, items, triggers)
    saved_meals: list[str] = field(default_factory=list)


@dataclass
class AgentRun:
    """What actually happened — assembled from tool-side state, not model claims."""

    reply: str
    food_calls: int  # log_food calls that staged something (0 or 1)
    estimate_calls: int  # log_estimate calls that staged something (0 or 1)
    weight: dict | None
    transcript: list
    max_turns_hit: bool = False
    options: list[str] | None = None  # the clarifying question's answers, when the reply is one
    meal_calls: int = 0  # save_meal / update_meal calls that wrote (or stamped) something


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
    state.setdefault("meal_calls", 0)

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
        state["food_done"] = True  # save_meal may now take this turn's log as its target
        return json.dumps(out, default=str)

    @function_tool(failure_error_function=_tool_error)
    async def log_estimate(
        items: list[EstimateItem],
        basis: str,
        meal_hint: MealName | None = None,
        when_hint: str | None = None,
    ) -> str:
        """Stage the component foods of something with NO nutrition label,
        each with the amount you estimate (a count of pieces with its total
        `grams`, or a weight/measure). Every row is flagged as an
        estimate for the user to confirm. `basis`: one sentence on how you
        sized it. `meal_hint` / `when_hint`: the meal word and the time phrase
        the user said, verbatim, when they said one. At most once per
        utterance."""
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
        # a count without its weight would be sized as pieces × the food's
        # mass default when the food has no count-type serving (50 shrimp ×
        # 100 g): refused here, before anything is staged
        uncounted = [it.phrase for it in items if it.unit is None and not (it.grams and it.grams > 0)]
        if uncounted:
            return json.dumps({
                "error": "missing_grams",
                "message": "a count of pieces needs its total weight in `grams` "
                           f"(pieces × a typical piece weight): {uncounted}",
            })
        items = items[:MAX_ESTIMATE_ITEMS]
        state["estimate_calls"] += 1
        try:
            out = await deps.stage_estimate(
                items, basis.strip(), meal_hint, (when_hint or "").strip() or None
            )
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

    @function_tool(failure_error_function=_tool_error)
    async def discard_log(log_id: str) -> str:
        """Discard a still-staged log from an earlier turn of this
        conversation because this turn corrects it (then stage the corrected
        version). Only a log id the conversation names; a committed log cannot
        be replaced — say it is in the diary."""
        if deps.discard_log is None:
            return json.dumps({"error": "unavailable", "message": "nothing to replace here"})
        return json.dumps(await deps.discard_log(log_id.strip()), default=str)

    @function_tool(failure_error_function=_tool_error)
    async def save_meal(
        name: str,
        trigger_phrases: list[str] | None = None,
        log_id: str | None = None,
        items: list[MealFood] | None = None,
    ) -> str:
        """Save a named meal so saying its name logs it again, or put more
        into one that exists. From ONE source: `items` (foods in words, the
        amount as said — added to the meal of that name, or a new meal) or a
        log (`log_id` from the conversation; null = this turn's log, else the
        newest in the conversation, else the last committed one; its rows
        REPLACE the meal's). A staged log is saved when they commit it.
        `trigger_phrases`: other ways they will say it, verbatim; null = the
        name."""
        name = name.strip()
        if not name:
            return json.dumps({"error": "invalid_name", "message": "a saved meal needs a name"})
        items = items or None
        log_id = (log_id or "").strip() or None
        if log_id is not None and items is not None:
            return json.dumps({
                "error": "choose_one",
                "message": "the items come from a log OR from the foods listed — not both",
            })
        bad = [it.phrase for it in items or [] if it.qty is not None and not (it.qty > 0)]
        if bad:
            return json.dumps({
                "error": "invalid_qty",
                "message": f"every amount must be above zero: {bad}",
            })
        if items is not None:
            items = items[:MAX_ESTIMATE_ITEMS]
        if log_id is None and items is None and state["food_calls"] and not state.get("food_done"):
            # the SDK runs one turn's tool calls concurrently: "this turn's
            # log" does not exist until log_food has returned
            return json.dumps({
                "error": "food_in_flight",
                "message": "log_food is still staging this turn's food — call save_meal "
                           "again once it has returned",
            })
        if deps.save_meal is None:
            return json.dumps({"error": "unavailable", "message": "saved meals cannot be changed here"})
        triggers = [t.strip() for t in trigger_phrases or [] if t.strip()] or None
        out = await deps.save_meal(name, triggers, log_id, items)
        if "error" not in out:
            state["meal_calls"] += 1
            state["meal_note"] = (
                f"'{name}' will be saved when you commit" if out.get("pending") else f"saved '{name}'"
            )
        return json.dumps(out, default=str)

    @function_tool(failure_error_function=_tool_error)
    async def update_meal(
        name: str,
        new_name: str | None = None,
        trigger_phrases: list[str] | None = None,
        is_active: bool | None = None,
        set_qty: list[MealQty] | None = None,
        remove: list[str] | None = None,
    ) -> str:
        """Change a saved meal that exists: rename (`new_name`), replace its
        trigger phrases, set an item's quantity or remove an item (the item's
        food name as the saved-meals list shows it), deactivate (`is_active`
        false — their "delete": it stays inactive, the diary keeps its
        context) or reactivate. Only what they asked to change; the rest
        null."""
        name = name.strip()
        if not name:
            return json.dumps({"error": "invalid_name", "message": "which saved meal?"})
        new_name = (new_name or "").strip() or None
        if (
            new_name is None and trigger_phrases is None and is_active is None
            and not set_qty and not remove
        ):
            return json.dumps({"error": "nothing_to_change", "message": "say what to change"})
        bad = [c.food for c in set_qty or [] if not (c.qty > 0)]
        if bad:
            return json.dumps({
                "error": "invalid_qty",
                "message": f"every quantity must be above zero: {bad}",
            })
        if deps.update_meal is None:
            return json.dumps({"error": "unavailable", "message": "saved meals cannot be changed here"})
        out = await deps.update_meal(
            name, new_name, trigger_phrases, is_active, set_qty or None, remove or None
        )
        if "error" not in out:
            state["meal_calls"] += 1
            state["meal_note"] = f"updated '{out.get('meal') or name}'"
        return json.dumps(out, default=str)

    tools: list = [log_food, log_estimate, log_weight, discard_log, save_meal, update_meal]
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


def _history_block(turns: list[dict]) -> str:
    """The conversation so far, in words the model can act on: what they
    said, what we replied, and each staged log with its id, its rows and
    its status — so a correction can name the log it replaces."""
    if not turns:
        return "Earlier in this conversation: nothing — this is the first turn."
    lines = ["Earlier in this conversation (oldest first):"]
    for t in turns:
        if t.get("role") == "user":
            lines.append(f'- They said: "{t.get("text", "")}"')
        elif t.get("log_id"):
            items = "; ".join(t.get("items") or []) or "nothing"
            lines.append(f"- Log {t['log_id']} ({t.get('state') or 'staged'}): {items}")
        else:
            lines.append(f'- You replied: "{t.get("text", "")}"')
    return "\n".join(lines)


def agent_input(text: str, deps: AgentDeps) -> str:
    """The per-run block: local time, the unit to assume, the last weigh-in,
    the profile notes (§6.1), the conversation so far, then the utterance."""
    if deps.last_weight:
        lw = deps.last_weight
        last = f"{lw['weight']:g} {lw['unit']} on {lw['day']}"
    else:
        last = "none yet"
    meals = "\n".join(f"- {line}" for line in deps.saved_meals) or "(none yet)"
    return (
        f"Now (the user's local time): {deps.now_local.strftime('%Y-%m-%d %H:%M, %A')}\n"
        f"Usual weight unit: {deps.unit_pref}\n"
        f"Last weigh-in: {last}\n"
        f"Profile notes: {deps.profile_notes or '(none)'}\n"
        f"Saved meals (name: items — say: trigger phrases):\n{meals}\n\n"
        f"{_history_block(deps.history)}\n\n"
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
    if state.get("meal_note"):
        parts.append(state["meal_note"])
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
    say which tool it belongs to. The hosted web_search is live: the raw
    Responses event that says a search is in progress puts its line on screen
    at once (query unknown yet), and the SDK's completed item — which arrives
    when the model turn ends — settles it with the query and the sources it
    consulted (possibly none), so the line never waits on a separate output."""
    if getattr(event, "type", None) == "raw_response_event":
        data = getattr(event, "data", None)
        if getattr(data, "type", None) in (
            "response.web_search_call.in_progress", "response.web_search_call.searching",
        ):
            item_id = str(getattr(data, "item_id", "") or "")
            if not item_id or item_id in names:
                return []
            names[item_id] = "web_search"
            return [{"type": "tool_call", "id": item_id, "name": "web_search",
                     "args": {"query": None}}]
        return []
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
            already_live = call_id in names  # the raw event put the line up already
            names[call_id] = "web_search"
            result = {"type": "tool_result", "id": call_id, "name": "web_search",
                      "output": {"query": query, "sources": [s for s in sources if s]}}
            if already_live:
                return [result]
            return [
                {"type": "tool_call", "id": call_id, "name": "web_search",
                 "args": {"query": query}},
                result,
            ]
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
    options: list[str] | None = None
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
        raw_options = getattr(final, "options", None) or []
        options = [o.strip() for o in raw_options if isinstance(o, str) and o.strip()][:4] or None
        transcript = result.to_input_list()
    except MaxTurnsExceeded:
        # the tools' effects stand; the reply is built from them
        max_turns_hit = True
        reply = _fallback_reply(state)
        options = None
    except Exception:
        if not (state["food_calls"] or state["estimate_calls"]):
            raise  # nothing staged: the route reports the failure and frees the claim
        # the model failed after its tools staged rows (a final output that
        # did not parse, a provider error on a later turn): the rows are in
        # the database and must be shown, so the run completes on them —
        # an error here would tell the user nothing was logged
        log.exception("agent run failed after staging; completing on the staged rows")
        reply = _fallback_reply(state)
        options = None
    if emit is not None:
        event: dict = {"type": "reply", "text": reply}
        if options:
            event["options"] = options
        await emit(event)
    return AgentRun(
        reply=reply,
        food_calls=state["food_calls"],
        estimate_calls=state["estimate_calls"],
        weight=state["weight"],
        transcript=transcript,
        max_turns_hit=max_turns_hit,
        options=options,
        meal_calls=state["meal_calls"],
    )
