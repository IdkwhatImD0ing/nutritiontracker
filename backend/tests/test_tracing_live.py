"""Live tests for backend/tracing.py against the FireTrace deployment (DESIGN.md §10).

1. One run with an llm span → the API answers 201, stored ended
   (running=false); the identical body again
   → 200 with duplicate=true; a score onto it (the way commit posts them)
   → 201.
2. ONE real decompose call (gpt-5.6-luna — cents) inside a run → a trace
   with the model's token usage, recorded (201).

Needs FIRETRACE_API_KEY; the second test also OPENAI_API_KEY. The traces
land in the environment of the key that sent them.

Run:  uv run --with pytest pytest tests/test_tracing_live.py -q -m live -s
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tracing  # noqa: E402

if not hasattr(tracing, "_send"):  # a stub another module installed — test the real one
    sys.modules.pop("tracing", None)
    import tracing  # noqa: E402

pytestmark = pytest.mark.live

PROJECT_URL = f"{tracing.BASE_URL}/projects/eaee19ae7a3106a2655fe651"


def _require_key() -> None:
    if not tracing.api_key():
        pytest.skip("FIRETRACE_API_KEY is not set")


def test_trace_duplicate_and_score_live():
    _require_key()

    async def flow():
        async with tracing.run(
            "firetrace-live-test", input={"utterance": "two eggs and toast"},
            session_id="firetrace-live-test", tags=["test"],
        ) as root:
            async with tracing.run(
                "decompose", kind="llm", provider="openai", model="gpt-5.6-luna",
                input="two eggs and toast",
            ) as d:
                d.set(
                    output={"items": ["eggs", "toast"]},
                    usage={"input_tokens": 12, "output_tokens": 6, "total_tokens": 18},
                )
            root.set(output={"items": 2})
        assert root.sent is not None, "the run should have scheduled its send"
        return root, await root.sent

    root, result = asyncio.run(flow())
    assert result is not None, "the send failed (see the firetrace warnings above)"
    assert result["status"] == 201, result
    assert result["body"]["ok"] is True and result["body"]["duplicate"] is False
    assert result["body"]["traceId"] == root.trace_id and result["body"]["spanCount"] == 2
    # the body carries endedAt, so the trace is stored ended — never left `running`
    assert result["body"]["running"] is False

    # the identical body again: 200, duplicate — nothing written twice
    again = tracing._send("resend", tracing.INGEST_URL, tracing._payload(root.trace))
    assert again is not None and again["status"] == 200 and again["body"]["duplicate"] is True

    # a score onto the stored trace, as resolver/commit.py posts them
    score = tracing._send(
        "score",
        f"{tracing.INGEST_URL}/{root.trace_id}/scores",
        tracing._dumps(
            {"name": "user_edited", "dataType": "numeric", "value": 1, "comment": "live test score"}
        ),
    )
    assert score is not None and score["status"] == 201, score
    assert score["body"]["score"]["name"] == "user_edited" and score["body"]["score"]["value"] == 1
    print(f"\n[firetrace] trace {root.trace_id} recorded (201) — {PROJECT_URL}")


def test_real_decompose_records_a_trace_live():
    _require_key()
    tracing.init_tracing()  # .env
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not set")
    from services.context import AgentContext
    from services.decompose import decompose

    ctx = AgentContext(profile_notes="", targets={}, food_index=[], meal_names=[], recent_foods=[])
    utterance = "two eggs and a slice of toast"

    async def flow():
        async with tracing.run(
            "log", input=utterance, session_id="firetrace-live-test", tags=["lane:text", "test"]
        ) as root:
            parsed = await decompose(utterance, ctx)
            root.set(output=parsed)
        return root, await root.sent, parsed

    root, result, parsed = asyncio.run(flow())
    assert parsed.items, "decompose returned no items"
    assert result is not None and result["status"] == 201, result
    spans = root.trace.spans
    assert [s.name for s in spans] == ["log", "decompose"]
    assert spans[1].kind == "llm" and spans[1].model == "gpt-5.6-luna"
    assert spans[1].usage and spans[1].usage["totalTokens"] > 0
    print(
        f"\n[firetrace] real decompose trace {root.trace_id} recorded (201), "
        f"usage {spans[1].usage} — {PROJECT_URL}"
    )
