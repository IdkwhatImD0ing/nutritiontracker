"""Call 1 — decompose (DESIGN.md §6.2).

gpt-5.6-luna via the plain openai client's Responses API: one-shot structured
extraction, reasoning effort "none", ~300 output tokens. Prompt layout per
§6.1: [static instructions -> context block -> utterance], with an explicit
cache breakpoint after the context block and a stable prompt_cache_key.

Tracing is the caller's job (routes/logs.py wraps the call in a generation
span); an optional `trace` hook is accepted and, when given, receives the
utterance and parsed output via trace.update(input=..., output=...).
"""

from openai import AsyncOpenAI

from models import ParsedLog
from prompts.decompose_prompt import INSTRUCTIONS, render_context

MODEL = "gpt-5.6-luna"  # pinned tier id — never the bare gpt-5.6 alias (§6)
PROMPT_CACHE_KEY = "usual-decompose-v1"

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI()  # reads OPENAI_API_KEY from the environment
    return _client


def _drop_invalid_library_matches(parsed: ParsedLog, valid_ids: set[str]) -> ParsedLog:
    """§3.3 2b: an id not in the supplied index is discarded, never trusted."""
    for item in parsed.items:
        if item.library_match is not None and item.library_match not in valid_ids:
            item.library_match = None
    return parsed


async def decompose(text: str, ctx, *, trace=None) -> ParsedLog:
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
            {"role": "user", "content": [{"type": "input_text", "text": text}]},
        ],
        text_format=ParsedLog,
        reasoning={"effort": "none"},
        prompt_cache_key=PROMPT_CACHE_KEY,
    )
    parsed = _drop_invalid_library_matches(
        response.output_parsed, {index_id for index_id, _, _ in ctx.food_index}
    )
    if trace is not None:
        trace.update(input=text, output=parsed.model_dump())
    return parsed
