"""Shared setup for the eval harness: Langfuse client + §6.1 context snapshots."""

from __future__ import annotations

import asyncio
import os
import sys

from langfuse import Langfuse, get_client

from services.context import AgentContext

CORRECTIONS_DATASET = "usual-corrections"
REPLAY_ENVIRONMENT = "test"  # DESIGN.md §10: experiment runs are test traces by construction


def setup_langfuse(environment: str | None = None) -> Langfuse:
    """Initialize tracing (tracing.init_tracing reads .env) and return the client.

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

    tracing.init_tracing()
    return get_client()


def snapshot_context(ctx: AgentContext) -> dict:
    """JSON form of the §6.1 context the decompose prompt saw."""
    return {
        "profile_notes": ctx.profile_notes,
        "targets": ctx.targets,
        "food_index": [list(t) for t in ctx.food_index],
        "meal_names": ctx.meal_names,
    }


def context_from_snapshot(snap: dict) -> AgentContext:
    return AgentContext(
        profile_notes=snap.get("profile_notes") or "",
        targets=snap.get("targets") or {},
        food_index=[(str(i), str(n), b) for i, n, b in snap.get("food_index") or []],
        meal_names=list(snap.get("meal_names") or []),
    )
