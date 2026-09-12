"""Live tests for backend/tracing.py against the FireTrace deployment (DESIGN.md §10).

1. One run with an llm span → the API answers 201, stored ended
   (running=false); the identical body again
   → 200 with duplicate=true; a score onto it (the way commit posts them)
   → 201.

(A second test ran one real decompose call inside a run; it went with
decompose, 2026-09-11.)

Needs FIRETRACE_API_KEY. The traces land in the environment of the key
that sent them.

Run:  uv run --with pytest pytest tests/test_tracing_live.py -q -m live -s
"""

from __future__ import annotations

import asyncio
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

