"""Shared pytest config (BACKLOG 17): put backend/ on sys.path once so test
modules import backend code directly (``import auth``, ``import tracing``, ...)
without per-file shims."""

import os
import sys
from pathlib import Path

BACKEND = str(Path(__file__).resolve().parents[1])
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# §3.4 (2026-09-11): the chat and photo lanes look every searching row up
# before they answer (routes/logs._resolve_inline). The fake Runners drive the
# REAL tool closures, so without this an offline test would reach the real
# tier 3; the tests of the inline step turn it on themselves and fake the run.
os.environ.setdefault("TALLY_INLINE_TIER3", "0")

# The route tests swap the `services` package for a stub with an empty
# __path__ (no live decompose/STT/tier 3). The routes' `from services import
# quota` still resolves because the REAL module already sits in sys.modules —
# imported here, before any stub is installed (Python's from-import falls back
# to sys.modules["services.quota"]). quota itself imports only auth/db.
import services.quota  # noqa: E402, F401

import psycopg  # noqa: E402
import pytest  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
from tenancy_harness import TEST_USER_ID  # noqa: E402


@pytest.fixture(scope="session")
def live_schema() -> tuple[bool | None, str | None]:
    """(002 applied?, connecting role) on the real database; (None, None)
    without one — the pure unit tests still run."""
    try:
        with psycopg.connect(db._database_url()) as conn:
            applied = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'logs' AND column_name = 'user_id'"
            ).fetchone() is not None
            role = conn.execute("SELECT current_user").fetchone()[0]
    except (RuntimeError, psycopg.OperationalError):
        return None, None
    return applied, role


@pytest.fixture(scope="session", autouse=True)
def _real_db_path_before_002(live_schema):
    """db.py's real path (tests with no rollback harness — test_foods_search
    cleans up after itself — and TestClient requests that reach the DB) SETs
    ROLE to the app role, which only migration 002 creates. Until 002 is
    applied for real, bind to the connecting role instead: pre-002 there are
    no policies to enforce, so that is the old single-user path exactly. Once
    logs.user_id exists this fixture changes nothing. (The rollback harnesses
    apply 002 inside their own transaction and never take this path.)"""
    applied, role = live_schema
    if applied is not False:
        yield
        return
    orig = db._bind_tenant

    async def bind(conn):
        await conn.execute(db.tenant_sql(db.current_user_id.get(), role=role))

    db._bind_tenant = bind
    yield
    db._bind_tenant = orig


@pytest.fixture(scope="session")
def live_has_003() -> bool | None:
    """Is 003's `weights` table on the real database? None without one."""
    try:
        with psycopg.connect(db._database_url()) as conn:
            return conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'weights'"
            ).fetchone() is not None
    except (RuntimeError, psycopg.OperationalError):
        return None


@pytest.fixture
def needs_003(live_has_003):
    """For the real-path tests that write a weigh-in (the agent lane, live):
    skipped until 003 is applied for real. The rollback harnesses apply 003
    inside their own transaction and never need this."""
    if live_has_003 is False:
        pytest.skip("needs migration 003 applied to the database (apply.py 003)")


@pytest.fixture
def needs_002(live_schema):
    """For the few real-path tests whose statements only exist after 002
    (alias learning's ON CONFLICT (user_id, alias_norm, food_id), the settings
    upsert): skipped until the migration is applied for real, then run."""
    if live_schema[0] is False:
        pytest.skip("needs migration 002 applied to the database (apply.py 002)")


@pytest.fixture(autouse=True)
def _test_tenant():
    """Every test runs as the fixed test tenant, owner-privileged — what the
    rollback harnesses bind inside their transaction, here for db.py's real
    path too (after 002 an unbound statement sees nothing, by design)."""
    t_id = db.current_user_id.set(TEST_USER_ID)
    t_user = auth.current_user.set({"id": TEST_USER_ID, "email": "test", "is_owner": True})
    yield
    auth.current_user.reset(t_user)
    db.current_user_id.reset(t_id)
