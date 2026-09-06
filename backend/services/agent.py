"""The agent lane's front door — DESIGN.md §6.4 (2026-09-06).

Every utterance that enters the + sheet's Agent lane (spoken or typed) runs
through this agent once the §3.0 whole-utterance short-circuit has passed on
it. The agent resolves nothing itself: it reads the utterance, hands the food
part — verbatim, in ONE call — to `log_food` (the unchanged decompose →
cascade → staging pipeline, behind a callback routes/logs.py injects) and an
explicitly stated body weight to `log_weight` (routes/weights.record_weight,
behind the other callback), then answers with one short sentence. The tools
are its only effects, and the route reports what the tools actually did —
never what the model says it did: `AgentRun` is assembled from tool-side
state; the reply is the only model text that reaches the client.

Model: gpt-5.6-luna at reasoning effort "low" — a two-tool routing call sits
on the hot path (§11), and luna is the tier for it; the tier-3 search agent
stays on terra. Escalation is one constant, as for decompose (§6.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Literal

from agents import Agent, MaxTurnsExceeded, ModelSettings, Runner, function_tool
from openai.types.shared import Reasoning
from pydantic import BaseModel

MODEL = "gpt-5.6-luna"  # pinned tier id — never the bare gpt-5.6 alias (§6)
MAX_TURNS = 4  # log_food, log_weight, the answer — one turn to spare
MAX_DAYS_AGO = 30

Unit = Literal["kg", "lb"]

INSTRUCTIONS = """\
You are Tally's agent: the front door for ONE spoken or typed utterance from a
person logging their nutrition. You have two tools and no other effects.

- log_food(text): stages what they ate or drank. Pass the food part of the
  utterance VERBATIM — keep every quantity, unit, brand, meal word ("for
  breakfast") and time reference ("this morning", "yesterday lunch") exactly
  as said, in the same order; leave out only the words about body weight.
  Call it AT MOST ONCE, with all the food in one call. Never paraphrase,
  convert units, or add a food that was not said.
- log_weight(weight, unit, days_ago): records a body weight the person stated
  as a NUMBER ("I weigh 180", "180.4 pounds this morning", "82 kilos
  yesterday"). `unit` is the unit they said; when none was said, use their
  usual unit from the context. `days_ago` is 0 for today, 1 for yesterday.
  A weight that was not spoken as a number is not a weight — "I'm down two
  pounds" states none; do not call the tool.

An utterance can hold both (call both tools), one, or neither. Neither — a
question, a greeting, noise — means no tool call at all.

Then answer with `reply`: one short sentence for the person, in plain words,
confirming what the tools reported. Examples: "Logged 180.4 lb for today —
down 0.6 lb since Tuesday." / "Staged 3 items for lunch." / "Staged 2 items
and logged 82 kg." / when nothing was done: "I didn't catch any food or a
weight in that." Use only numbers the tool results gave you; never state
calories or macros (the staged list shows those); never mention tools. If a
tool reported an error, say plainly what could not be done.
"""


class AgentReply(BaseModel):
    reply: str


@dataclass
class AgentDeps:
    """What the route injects: the two effects, and the context the model reads."""

    # the food part of the utterance → a summary of what was staged (no macros)
    stage_food: Callable[[str], Awaitable[dict]]
    # (weight as spoken, unit, local day) → the stored entry + the change since
    # the previous one; raises ValueError when the value is out of range
    record_weight: Callable[[float, Unit, date], Awaitable[dict]]
    now_local: datetime  # the user's wall clock (tz-aware), for days_ago
    unit_pref: Unit  # the unit to assume when none was spoken
    last_weight: dict | None = None  # {"day", "weight", "unit"} of the newest weigh-in
    profile_notes: str = ""


@dataclass
class AgentRun:
    """What actually happened — assembled from tool-side state, not model claims."""

    reply: str
    food_calls: int  # log_food calls that staged something (0 or 1)
    weight: dict | None
    transcript: list
    max_turns_hit: bool = False


def _tool_error(ctx, error: Exception) -> str:
    """Sanitized tool failure (the SDK default embeds str(error); a database or
    provider message must never reach the model, the traces, or a reply)."""
    return f"{type(error).__name__}: that could not be saved. Tell the user it did not go through."


def build_agent(deps: AgentDeps, state: dict[str, Any]) -> Agent:
    """One agent per run: the tools close over `state`, which is what the
    route reads back (food_calls, weight)."""
    state.setdefault("food_calls", 0)
    state.setdefault("weight", None)

    @function_tool(failure_error_function=_tool_error)
    async def log_food(text: str) -> str:
        """Stage everything the user ate or drank: the food part of the
        utterance copied verbatim (quantities, brands, meal and time words
        included; the weight sentence left out). At most once per utterance."""
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

    return Agent(
        name="tally-agent",
        instructions=INSTRUCTIONS,
        model=MODEL,
        tools=[log_food, log_weight],
        model_settings=ModelSettings(reasoning=Reasoning(effort="low")),
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
    if state.get("weight"):
        parts.append("logged your weight")
    if not parts:
        return "I couldn't work that one out — try again, or type it."
    return "I " + " and ".join(parts) + " — check the list."


async def run_agent(text: str, deps: AgentDeps) -> AgentRun:
    state: dict[str, Any] = {}
    agent = build_agent(deps, state)
    transcript: list = []
    max_turns_hit = False
    try:
        result = await Runner.run(agent, agent_input(text, deps), max_turns=MAX_TURNS)
        reply = result.final_output_as(AgentReply).reply.strip() or _fallback_reply(state)
        transcript = result.to_input_list()
    except MaxTurnsExceeded:
        # the tools' effects stand; the reply is built from them
        max_turns_hit = True
        reply = _fallback_reply(state)
    return AgentRun(
        reply=reply,
        food_calls=state["food_calls"],
        weight=state["weight"],
        transcript=transcript,
        max_turns_hit=max_turns_hit,
    )
