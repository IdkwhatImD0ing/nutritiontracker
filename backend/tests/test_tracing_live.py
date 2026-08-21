"""Live test for backend/tracing.py against Langfuse Cloud (DESIGN.md §10).

Flow: init + one generation with input/output + flush, then poll the Langfuse
REST API (basic auth) until the trace arrives; assert environment; then
score_commit on that trace and poll until the score is queryable.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Another test file may have stubbed "tracing" in sys.modules — this file
# tests the real module, so force a fresh import from disk.
sys.modules.pop("tracing", None)
import tracing


def _poll(url: str, auth: httpx.BasicAuth, ok, timeout_s: float = 45.0):
    """GET url every 2.5s until ok(response) is truthy; return the response."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = httpx.get(url, auth=auth, timeout=10.0)
        if ok(last):
            return last
        time.sleep(2.5)
    raise AssertionError(
        f"timed out polling {url}: last status {last.status_code if last else 'n/a'}"
    )


def test_generation_trace_and_score_live():
    async def make_generation() -> str:
        async with tracing.generation("test-tracing-live") as gen:
            gen.update(
                input={"utterance": "two eggs and toast"},
                output={"items": ["eggs", "toast"]},
            )
            return gen.trace_id

    tracing.init_tracing()
    trace_id = asyncio.run(make_generation())
    assert trace_id, "generation yielded no trace_id"
    tracing.flush()

    base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    auth = httpx.BasicAuth(
        os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]
    )
    expected_env = os.environ["LANGFUSE_TRACING_ENVIRONMENT"]

    # Trace arrived, with environment set.
    resp = _poll(
        f"{base}/api/public/traces/{trace_id}", auth, lambda r: r.status_code == 200
    )
    trace = resp.json()
    assert trace["id"] == trace_id
    assert trace["environment"] == expected_env

    # Score by trace id succeeds and becomes queryable (API 200).
    tracing.score_commit(trace_id, "user_edited", 1, comment="live test score")

    def has_score(r: httpx.Response) -> bool:
        return r.status_code == 200 and any(
            s["name"] == "user_edited" and s["traceId"] == trace_id
            for s in r.json()["data"]
        )

    resp = _poll(f"{base}/api/public/v2/scores?traceId={trace_id}", auth, has_score)
    score = next(s for s in resp.json()["data"] if s["name"] == "user_edited")
    assert score["value"] == 1
    assert score["comment"] == "live test score"
