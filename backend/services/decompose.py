"""Call 1 — decompose (DESIGN.md §6.2).

gpt-5.6-luna via the plain openai client's Responses API: one-shot structured
extraction, reasoning effort "none", ~300 output tokens. Prompt layout per
§6.1: [static instructions -> context block -> utterance], with an explicit
cache breakpoint after the context block and a stable prompt_cache_key.

The call records its own FireTrace span (tracing.run: kind llm, the model,
the token usage) inside whatever trace the route is recording — or as a
trace of its own when nothing is (evals, tests). An optional `trace` hook is
accepted and, when given, receives the utterance and parsed output via
trace.update(input=..., output=...).
"""

from typing import TYPE_CHECKING

import tracing
from models import ParsedLog
from prompts.decompose_prompt import INSTRUCTIONS, render_context, render_now

# The client is built on first call (_get_client), so `import openai` — ~0.6 s
# on a cold boot — stays off the boot path and off every request that never
# reaches a model. This module has no `from __future__ import annotations`,
# so the two AsyncOpenAI annotations below are written as strings.
if TYPE_CHECKING:
    from openai import AsyncOpenAI

MODEL = "gpt-5.6-luna"  # pinned tier id — never the bare gpt-5.6 alias (§6)
PROMPT_CACHE_KEY = "usual-decompose-v1"

_client: "AsyncOpenAI | None" = None


def _get_client() -> "AsyncOpenAI":
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI()  # reads OPENAI_API_KEY from the environment
    return _client


def _drop_invalid_library_matches(parsed: ParsedLog, valid_ids: set[str]) -> ParsedLog:
    """§3.3 2b: an id not in the supplied index is discarded, never trusted."""
    for item in parsed.items:
        if item.library_match is not None and item.library_match not in valid_ids:
            item.library_match = None
    return parsed


async def decompose(text: str, ctx, *, now_local=None, trace=None) -> ParsedLog:
    """`now_local`: the utterance's tz-aware LOCAL time, when the caller knows
    it. It becomes the NOW line the model resolves `when_day_offset` against
    (§7.2, BACKLOG 174), and rides with the utterance so the context block's
    cache breakpoint still matches. Omitted (the edited-phrase micro-decompose,
    evals) the model has no date to count from and leaves the when fields null."""
    async with tracing.run(
        "decompose", kind="llm", provider="openai", model=MODEL, input=text,
        attributes={
            "prompt_cache_key": PROMPT_CACHE_KEY,
            "food_index": len(ctx.food_index),
            "recent_foods": len(ctx.recent_foods),
        },
    ) as span:
        response = await _get_client().responses.parse(
            model=MODEL,
            input=[
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": INSTRUCTIONS}],
                },
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": render_context(ctx),
                            # §6.1: explicit breakpoint after the index block —
                            # GPT-5.6 cache matching is exact at breakpoints.
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": t}
                        for t in ((render_now(now_local),) if now_local else ())
                    ] + [{"type": "input_text", "text": text}],
                },
            ],
            text_format=ParsedLog,
            reasoning={"effort": "none"},
            prompt_cache_key=PROMPT_CACHE_KEY,
        )
        span.set(output=response.output_parsed, usage=response.usage)
    parsed = _drop_invalid_library_matches(
        response.output_parsed,
        {index_id for index_id, _, _ in (*ctx.food_index, *ctx.recent_foods)},
    )
    if trace is not None:
        trace.update(input=text, output=parsed.model_dump())
    return parsed
