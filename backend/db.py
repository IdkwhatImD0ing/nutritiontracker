"""Async Postgres access for Tally (DESIGN.md §2, §8.7).

One lazily-opened psycopg AsyncConnection per event loop, over the pooled
(-pooler) Neon connection string — PgBouncer transaction mode supports the
ordinary interactive transactions §3.5 needs. All rows come back as dicts.

Tenancy (migration 002): every statement runs inside a transaction that first
binds the tenant — ``SET LOCAL ROLE tally_app`` (the role without BYPASSRLS,
so the row policies apply) and ``app.user_id`` = ``current_user_id`` (the
ContextVar auth.require_user sets from the JWT ``sub``; code with no request
binds the owner itself).
Transaction-local is the only safe scope over the pooler — a session-level
SET could land on a different server connection than the next statement — so
each helper is its own short transaction (BEGIN / bind / statement / COMMIT:
a few extra round trips per call). With no bound user the setting is '' —
RLS shows nothing and the user_id DEFAULT is NULL: fail-closed.

Shared interface (workstream contract):
    await fetchall(sql, params=()) -> list[dict]
    await fetchone(sql, params=()) -> dict | None
    await execute(sql, params=())  -> int (rowcount)
    async with tx() as conn: ...   -> psycopg AsyncConnection, open transaction

Inside a ``tx()`` block use ``conn`` directly — the module-level helpers share
the same connection behind a lock and would deadlock if called re-entrantly.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

APP_ROLE = "tally_app"  # created by migrations/002_multiuser.sql
current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)

_conn: psycopg.AsyncConnection | None = None
_lock: asyncio.Lock | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL is not set (env or repo-root .env)")


def tenant_sql(user_id: str | None, role: str = APP_ROLE) -> pgsql.Composed:
    """The two SET LOCALs that bind a transaction to one tenant. One
    round trip: no server-side parameters, so both statements ride together
    (the test harnesses reuse this to bind their rollback transactions)."""
    return pgsql.SQL("SET LOCAL ROLE {}; SELECT set_config('app.user_id', {}, true)").format(
        pgsql.Identifier(role), pgsql.Literal(user_id or "")
    )


async def _bind_tenant(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(tenant_sql(current_user_id.get()))


def _state() -> asyncio.Lock:
    """Per-event-loop lock (tests run each case in a fresh loop)."""
    global _conn, _lock, _loop
    loop = asyncio.get_running_loop()
    if _loop is not loop:
        _conn = None
        _lock = asyncio.Lock()
        _loop = loop
    assert _lock is not None
    return _lock


async def _ensure_conn() -> psycopg.AsyncConnection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = await psycopg.AsyncConnection.connect(
            _database_url(), row_factory=dict_row, autocommit=True
        )
    return _conn


async def fetchall(sql, params=()):
    async with _state():
        conn = await _ensure_conn()
        async with conn.transaction():
            await _bind_tenant(conn)
            cur = await conn.execute(sql, params)
            return await cur.fetchall()


async def fetchone(sql, params=()):
    async with _state():
        conn = await _ensure_conn()
        async with conn.transaction():
            await _bind_tenant(conn)
            cur = await conn.execute(sql, params)
            return await cur.fetchone()


async def execute(sql, params=()):
    async with _state():
        conn = await _ensure_conn()
        async with conn.transaction():
            await _bind_tenant(conn)
            cur = await conn.execute(sql, params)
            return cur.rowcount


@asynccontextmanager
async def tx():
    """One interactive transaction (§3.5). Commits on exit, rolls back on error."""
    lock = _state()
    async with lock:
        conn = await _ensure_conn()
        async with conn.transaction():
            await _bind_tenant(conn)
            yield conn


async def close() -> None:
    """Close the loop's connection (test hygiene; serverless never needs it)."""
    global _conn
    if _conn is not None and not _conn.closed:
        await _conn.close()
    _conn = None
