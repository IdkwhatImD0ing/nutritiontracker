"""A stand-in for backend/tracing.py in the route tests: the same public
surface (init_tracing, install_agents_hook, run, current_trace_id, current_run,
flush, score_commit, drain, FlushMiddleware), no network — a developer .env
with a real FIRETRACE_API_KEY must never record test traces (FireTrace keeps
them forever). `trace_id` is what the routes store on logs.trace_id, so a test
can assert it; `scored` collects score_commit calls when given."""

from __future__ import annotations

import types
from contextlib import asynccontextmanager


def make_tracing_stub(trace_id: str = "trace-test", scored: list | None = None) -> types.ModuleType:
    mod = types.ModuleType("tracing")

    class _Run:
        def __init__(self) -> None:
            self.trace_id = trace_id
            self.span_id = "span-test"

        def set(self, **kw):
            return self

    @asynccontextmanager
    async def run(name, **kw):
        yield _Run()

    class _Middleware:
        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send) -> None:
            await self.app(scope, receive, send)

    async def drain(*args, **kwargs) -> None:
        return None

    async def drain_pending(*args, **kwargs) -> None:
        return None

    def score_commit(trace_id_, name, value, comment=None) -> None:
        if scored is not None:
            scored.append((trace_id_, name, value))

    mod.init_tracing = lambda: None
    # Split out of init_tracing so importing the openai-agents SDK stays off
    # the cold-boot path; the agent lanes call it at first SDK use.
    mod.install_agents_hook = lambda: None
    mod.run = run
    mod.current_run = lambda: None
    mod.current_trace_id = lambda: trace_id
    mod.flush = lambda: None
    mod.score_commit = score_commit
    mod.drain = drain
    mod.drain_pending = drain_pending
    mod.FlushMiddleware = _Middleware
    return mod
