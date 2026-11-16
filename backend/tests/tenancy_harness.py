"""Tenancy (migration 002) for the test harnesses.

Every isolated_db copy calls bind_test_tenant(conn) right after opening its
force-rollback transaction. Inside that transaction it
  1. applies 002 when the database is still pre-002, and 003 (the weigh-in
     log), 005 (the Oura tables), 006 (API keys) and 007 (api_key_auth
     checks the account before it stamps) while they are not yet applied —
     DDL is transactional, it rolls back with the test — so the suite
     exercises RLS either way;
  2. switches to the app role and binds one fixed tenant, exactly as db.py
     does for a request (db.tenant_sql) — neondb_owner itself bypasses RLS;
  3. seeds the 001 settings rows for that tenant — a fresh tenant has none,
     and the single-user tests were written against a seeded database;
  4. marks the in-process user as the owner (auth.current_user), so handlers
     called directly run unlimited, as the single-user app did.

override_user(...) is the dependency override for TestClient tests: it binds
the tenant like auth.require_user does. A bare lambda cannot — a sync
override runs in a worker thread whose context is discarded — and it never
carries `is_owner`.
"""

from __future__ import annotations

import os
from pathlib import Path

import auth
import db

TEST_USER_ID = "test-user"
SQL_002 = (
    Path(__file__).resolve().parents[1] / "migrations" / "002_multiuser.sql"
).read_text(encoding="utf-8")
SQL_003 = (
    Path(__file__).resolve().parents[1] / "migrations" / "003_weights.sql"
).read_text(encoding="utf-8")
SQL_005 = (
    Path(__file__).resolve().parents[1] / "migrations" / "005_oura.sql"
).read_text(encoding="utf-8")
SQL_006 = (
    Path(__file__).resolve().parents[1] / "migrations" / "006_api_keys.sql"
).read_text(encoding="utf-8")
SQL_007 = (
    Path(__file__).resolve().parents[1] / "migrations" / "007_api_key_auth.sql"
).read_text(encoding="utf-8")

# 001_init.sql's seeds (DESIGN.md §4, §7.2), for the bound tenant.
SEED_SETTINGS_SQL = """
INSERT INTO settings (key, value) VALUES
  ('meal_windows', '{
     "breakfast": {"start": "04:00", "end": "11:00"},
     "lunch":     {"start": "11:00", "end": "15:30"},
     "dinner":    {"start": "15:30", "end": "21:30"},
     "default":   "snack"
   }'::jsonb),
  ('timezone', '"America/Los_Angeles"'::jsonb),
  ('cronometer_baseline_days_per_week', '0'::jsonb),
  ('food_index', '{"inline_default": 20, "max": 100}'::jsonb),
  ('profile_notes', '""'::jsonb),
  ('body', '{}'::jsonb),
  ('targets', '{}'::jsonb)
ON CONFLICT (user_id, key) DO NOTHING
"""


async def has_002(conn) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'logs' AND column_name = 'user_id'"
    )
    return await cur.fetchone() is not None


async def has_003(conn) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'weights'"
    )
    return await cur.fetchone() is not None


async def has_005(conn) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'oura_tokens'"
    )
    return await cur.fetchone() is not None


async def has_006(conn) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'api_keys'"
    )
    return await cur.fetchone() is not None


async def has_007(conn) -> bool:
    """007 replaces a function, so its marker is the function's comment."""
    cur = await conn.execute(
        "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'api_key_auth' "
        "AND obj_description(p.oid, 'pg_proc') LIKE '007:%'"
    )
    return await cur.fetchone() is not None


async def switch_tenant(conn, user_id: str) -> None:
    """(Re)bind the open transaction to one tenant, as db.py does per request."""
    await conn.execute(db.tenant_sql(user_id))
    db.current_user_id.set(user_id)


def owner_email() -> str:
    """The real owner's address (ALLOWED_EMAIL), for the tests that find that
    account in neon_auth. Read from the environment or the repo-root .env the
    way db._database_url reads its one variable: auth._env would pull the
    rest of .env into os.environ."""
    email = os.environ.get("ALLOWED_EMAIL")
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not email and env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ALLOWED_EMAIL="):
                email = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not email:
        raise RuntimeError("ALLOWED_EMAIL is not set (env or repo-root .env)")
    return email


async def apply_002(conn) -> None:
    """002 the way apply.py runs it: the owner's address goes in the
    transaction-local setting the migration's first block reads."""
    await conn.execute("SELECT set_config('app.owner_email', %s, true)", (owner_email(),))
    await conn.execute(SQL_002)


async def bind_test_tenant(conn, user_id: str = TEST_USER_ID) -> None:
    if not await has_002(conn):
        await apply_002(conn)
    if not await has_003(conn):  # the weigh-in log, the same way — rolls back with the test
        await conn.execute(SQL_003)
    if not await has_005(conn):  # the Oura tables, the same way
        await conn.execute(SQL_005)
    if not await has_006(conn):  # API keys: DDL tally_app cannot run, so before the switch
        await conn.execute(SQL_006)
    if not await has_007(conn):  # api_key_auth's account check: the same, after 006
        await conn.execute(SQL_007)
    await switch_tenant(conn, user_id)
    await conn.execute(SEED_SETTINGS_SQL)
    auth.current_user.set({"id": user_id, "email": "test", "is_owner": True})


def override_user(*, email: str = "test", user_id: str = TEST_USER_ID, is_owner: bool = True):
    """`app.dependency_overrides[auth.require_user] = override_user(...)`."""
    user = {
        "id": user_id,
        "email": email,
        "is_owner": is_owner,
        "via": "session",  # a signed-in user, the shape auth.authenticate returns
        "scopes": auth.SESSION_SCOPES,
    }

    async def _user() -> dict:
        db.current_user_id.set(user_id)
        auth.current_user.set(user)
        return user

    return _user
