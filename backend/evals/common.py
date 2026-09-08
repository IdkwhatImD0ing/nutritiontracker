"""Shared setup for the eval harness: Langfuse client, the owner tenant, §6.1
context snapshots."""

from __future__ import annotations

import asyncio
import os
import sys

import psycopg
from langfuse import Langfuse, get_client

from services.context import AgentContext

CORRECTIONS_DATASET = "usual-corrections"
REPLAY_ENVIRONMENT = "test"  # DESIGN.md §10: experiment runs are test traces by construction


def setup_langfuse(environment: str | None = None) -> Langfuse:
    """Read .env (tracing.init_tracing), build the Langfuse client the harness
    records to, and return it. The app itself traces to FireTrace (§10); the
    harness stays on Langfuse for its datasets and experiment runs, which
    FireTrace does not have yet (docs/FIRETRACE_FEEDBACK.md) — hence the
    opt-in `evals` dependency group in pyproject.toml.

    `environment` overrides LANGFUSE_TRACING_ENVIRONMENT *before* the first
    init — the SDK's resource manager pins the environment at first init per
    public key, so replay.py additionally propagates the environment per span
    (langfuse.propagate_attributes) for the in-process pytest case.
    """
    if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if environment:
        os.environ["LANGFUSE_TRACING_ENVIRONMENT"] = environment
    import tracing

    tracing.init_tracing()  # .env, incl. the LANGFUSE_* keys the client reads
    Langfuse(
        base_url=os.environ.get("LANGFUSE_BASE_URL"),
        environment=os.environ.get("LANGFUSE_TRACING_ENVIRONMENT"),
    )
    # the openai-agents turns and tool calls into the experiment's traces, as
    # the app's tracer did before it moved to FireTrace (idempotent: a second
    # call only warns)
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

    OpenAIAgentsInstrumentor().instrument()
    return get_client()


async def bind_owner() -> str:
    """No request, no JWT: bind db.py's tenant to the owner so every eval
    query runs under RLS as them (migration 002). The id is looked up in
    neon_auth."user" by the allowlisted email on a plain connection — the app
    role db.py switches to has no access to that schema."""
    import auth
    import db

    email = auth._env("ALLOWED_EMAIL", auth.DEFAULT_ALLOWED_EMAIL) or auth.DEFAULT_ALLOWED_EMAIL
    async with await psycopg.AsyncConnection.connect(db._database_url()) as conn:
        cur = await conn.execute(
            'SELECT id::text FROM neon_auth."user" WHERE lower(email) = lower(%s)', (email,)
        )
        rows = await cur.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one Neon Auth user for the owner, found {len(rows)}")
    db.current_user_id.set(rows[0][0])
    return rows[0][0]


def snapshot_context(ctx: AgentContext) -> dict:
    """JSON form of the §6.1 context the decompose prompt saw."""
    return {
        "profile_notes": ctx.profile_notes,
        "targets": ctx.targets,
        "food_index": [list(t) for t in ctx.food_index],
        "recent_foods": [list(t) for t in ctx.recent_foods],
        "meal_names": ctx.meal_names,
    }


def context_from_snapshot(snap: dict) -> AgentContext:
    return AgentContext(
        profile_notes=snap.get("profile_notes") or "",
        targets=snap.get("targets") or {},
        food_index=[(str(i), str(n), b) for i, n, b in snap.get("food_index") or []],
        meal_names=list(snap.get("meal_names") or []),
        # snapshots collected before 2026-09-06 carry no recent list
        recent_foods=[(str(i), str(n), b) for i, n, b in snap.get("recent_foods") or []],
    )
