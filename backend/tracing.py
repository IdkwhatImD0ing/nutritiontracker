"""Langfuse tracing (DESIGN.md §10).

Langfuse Python SDK v4, OTel-first. The OpenInference instrumentor auto-spans
tier-3 openai-agents runs into the same tracer provider. Serverless rule:
call flush() before returning from any AI route.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional, Union

from langfuse import Langfuse, get_client
from langfuse._client.span import LangfuseGeneration

_initialized = False


def _load_env() -> None:
    """Populate os.environ from backend/.env or repo-root .env (local dev only;
    deployed environments set real env vars). Existing values win."""
    here = Path(__file__).resolve().parent
    for env_path in (here / ".env", here.parent / ".env"):
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def init_tracing() -> None:
    """Initialize Langfuse + openai-agents auto-instrumentation. Idempotent."""
    global _initialized
    if _initialized:
        return
    _load_env()
    # v4 reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY from env; base_url and
    # environment are passed explicitly from our env names per DESIGN.md §10.
    Langfuse(
        base_url=os.environ.get("LANGFUSE_BASE_URL"),
        environment=os.environ.get("LANGFUSE_TRACING_ENVIRONMENT"),
    )
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

    OpenAIAgentsInstrumentor().instrument()
    _initialized = True


@asynccontextmanager
async def generation(name: str) -> AsyncIterator[LangfuseGeneration]:
    """Langfuse generation observation. The yielded object exposes .trace_id
    and .update(input=..., output=...)."""
    init_tracing()
    obs = get_client().start_observation(name=name, as_type="generation")
    try:
        yield obs
    finally:
        obs.end()


def flush() -> None:
    """Blocking flush of pending spans and scores (serverless rule, §10)."""
    if _initialized:
        get_client().flush()


def score_commit(
    trace_id: str,
    name: str,
    value: Union[float, str],
    comment: Optional[str] = None,
) -> None:
    """Post a score onto a stored trace id (works pre-ingestion). Flushes so a
    frozen serverless function can't drop the commit-time score."""
    init_tracing()
    client = get_client()
    client.create_score(trace_id=trace_id, name=name, value=value, comment=comment)
    client.flush()
