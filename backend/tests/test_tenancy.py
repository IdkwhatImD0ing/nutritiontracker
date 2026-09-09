"""Tenancy (migration 002) + the demo quota (services/quota.py, routes/me.py).

Every test runs inside a transaction that is ROLLED BACK (the isolated_db
harness). tenancy_harness.bind_test_tenant applies 002 inside that
transaction while the database is still pre-002, so this file is the
migration's dry-run in the suite — and it keeps passing after the real apply
(the in-transaction apply is skipped once logs.user_id exists).

The demo log quota is an ATTEMPT ledger (quota_events kind='log', recorded
with the claim of the client id — routes/logs.claim_log — before any model
spend) — not a count of logs rows, which the same client id can re-run or
empty (tests 3, 8, 9). The hardening pass (test_quota_hardening.py) covers
the atomic claim, the database clock, foreign ids and placeholders.

Run:  uv run --with pytest pytest backend/tests/test_tenancy.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from agents.tool_context import ToolContext
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import OpenAIError
from psycopg.rows import dict_row
from psycopg.types.json import Json
from starlette.datastructures import Headers, UploadFile

import auth  # noqa: E402
import db  # noqa: E402
from tenancy_harness import (  # noqa: E402
    SEED_SETTINGS_SQL,
    SQL_002,
    SQL_003,
    TEST_USER_ID,
    bind_test_tenant,
    has_002,
    has_003,
    override_user,
    switch_tenant,
)
from models import (  # noqa: E402
    FoodRef,
    LogCreate,
    Meal,
    ParsedLog,
    ResolveRequest,
    ServingRef,
    StagedItem,
    Tier3KickRequest,
)
from routes import foods as foods_routes  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes import me as me_routes  # noqa: E402
from routes import photo, quick  # noqa: E402
from routes import resolve as resolve_routes  # noqa: E402
from services import quota, tier3  # noqa: E402
from sources import fdc  # noqa: E402

TENANT_TABLES = ("aliases", "meal_macros", "logs", "item_resolutions", "entries", "settings")
RLS_TABLES = TENANT_TABLES + ("quota_events", "weights")  # + 003's weigh-in log
SEED_KEYS = {
    "meal_windows", "timezone", "cronometer_baseline_days_per_week", "food_index",
    "profile_notes", "body", "targets",
}
NOW = datetime(2026, 9, 3, 18, 30, tzinfo=timezone.utc)
SERVING = {
    "serving_id": "s",
    "description": "1 egg",
    "grams": 50,
    "macros": {"kcal": 72, "protein_g": 6.3, "carb_g": 0.4, "fat_g": 4.8},
}


def _run(coro):
    return asyncio.run(coro)


def _user(user_id: str, *, is_owner: bool = False) -> dict:
    return {"id": user_id, "email": f"{user_id}@example.test", "is_owner": is_owner}


async def _become(conn, user_id: str, *, is_owner: bool = False) -> dict:
    """Another tenant on the same rollback transaction, in-process user included."""
    await switch_tenant(conn, user_id)
    user = _user(user_id, is_owner=is_owner)
    auth.current_user.set(user)
    return user


# ---------------------------------------------------------------------------
# rollback harness (same shape as the other test modules)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
    await bind_test_tenant(conn)  # 002: app role + the fixed test tenant, inside the rollback
    orig = (db.fetchall, db.fetchone, db.execute, db.tx)

    async def fetchall(sql, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchall()

    async def fetchone(sql, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchone()

    async def execute(sql, params=()):
        cur = await conn.execute(sql, params)
        return cur.rowcount

    @asynccontextmanager
    async def tx():
        async with conn.transaction():  # SAVEPOINT under the outer transaction
            yield conn

    db.fetchall, db.fetchone, db.execute, db.tx = fetchall, fetchone, execute, tx
    try:
        yield conn
    finally:
        db.fetchall, db.fetchone, db.execute, db.tx = orig
        await outer.__aexit__(None, None, None)  # force rollback
        await conn.close()


async def _scalar(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return next(iter(row.values())) if row else None


async def _count(conn, table: str) -> int:
    return await _scalar(conn, f"SELECT count(*) FROM {table}")


async def insert_food(conn) -> str:
    return str(
        await _scalar(
            conn,
            """
            INSERT INTO foods (source, source_id, name, display_name, food_type,
                               servings, default_serving_id, payload)
            VALUES ('fdc', %s, 'egg', 'egg', 'generic', %s, 's', '{}'::jsonb)
            RETURNING id
            """,
            (uuid4().hex, Json([SERVING])),
        )
    )


async def insert_log(conn, status: str = "staged", log_id=None):
    log_id = log_id or uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status) "
        "VALUES (%s, 'text', 'two eggs', now(), %s)",
        (log_id, status),
    )
    return log_id


def staged_item(food_id) -> dict:
    """A resolved row's staged payload as stored — what a same-id POST replays."""
    return StagedItem(
        item_index=0, phrase="two eggs", status="resolved", tier=1,
        food=FoodRef(id=str(food_id), name="egg"), serving=ServingRef(id="s", description="1 egg"),
        qty=2, meal=Meal.snack, confidence="high",
    ).model_dump(mode="json")


async def insert_item(conn, log_id, food_id, status="resolved", item_index=0, staged=None):
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                      staged, tier, proposed_food_id)
        VALUES (%s, %s, 'two eggs', 'two eggs', %s, %s, 1, %s)
        """,
        (log_id, item_index, status, Json(staged or {}), food_id),
    )


async def insert_entry(conn, log_id, food_id):
    await conn.execute(
        """
        INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, macros, log_id, item_index)
        VALUES (now(), 'snack', %s, 's', 2, %s, %s, 0)
        """,
        (food_id, Json(SERVING["macros"]), log_id),
    )


async def insert_alias(conn, food_id):
    await conn.execute(
        "INSERT INTO aliases (alias_norm, food_id, origin) VALUES ('two eggs', %s, 'tier3')",
        (food_id,),
    )


async def insert_meal(conn, food_id):
    await conn.execute(
        "INSERT INTO meal_macros (name, trigger_phrases, items) VALUES ('usual', %s, %s)",
        (["usual"], Json([{"food_id": food_id, "serving_id": "s", "qty": 2}])),
    )


def _upload(data: bytes, mime: str) -> UploadFile:
    return UploadFile(BytesIO(data), filename="blob", headers=Headers({"content-type": mime}))


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12  # passes sniff_image; never reaches a model here


def _next_utc_midnight() -> datetime:
    today = datetime.now(timezone.utc).date()
    return datetime.combine(today + timedelta(days=1), time.min, tzinfo=timezone.utc)


def _stub_pipeline(monkeypatch, *, decompose=None, vision=None) -> None:
    """The model calls behind the log lanes, replaced in place — the routes
    import them lazily at call time, so a module attribute swap is enough.
    Tracing is a no-op; decompose / vision run the given coroutine, else refuse."""
    import tracing
    from services import context, vision as vision_mod
    from services import decompose as decompose_mod

    @asynccontextmanager
    async def run(name, **kw):
        yield SimpleNamespace(trace_id="trace-test", span_id="span-test", set=lambda **kw: None)

    async def build_agent_context():
        return SimpleNamespace(profile_notes="", targets={}, food_index=[], meal_names=[], recent_foods=[])

    async def refuse(*args, **kwargs):
        raise AssertionError("no model call expected here")

    monkeypatch.setattr(tracing, "init_tracing", lambda: None)
    monkeypatch.setattr(tracing, "run", run)
    monkeypatch.setattr(tracing, "current_trace_id", lambda: "trace-test")
    monkeypatch.setattr(tracing, "flush", lambda: None)
    monkeypatch.setattr(context, "build_agent_context", build_agent_context)
    monkeypatch.setattr(decompose_mod, "decompose", decompose or refuse)
    monkeypatch.setattr(vision_mod, "analyze", vision or refuse)


def _tool(agent, name):
    return next(t for t in agent.tools if getattr(t, "name", None) == name)


def _ctx(name, args):
    return ToolContext(context=None, tool_name=name, tool_call_id="call_1", tool_arguments=args)


async def _fake_fdc_food(source, source_id) -> dict:
    """What sources/fdc.get_food hands back for any id: the adapter's own names."""
    return {
        "source": source, "source_id": str(source_id), "source_url": None, "provenance": None,
        "name": "Cheesecake, commercially prepared", "display_name": "cheesecake",
        "brand": None, "food_type": "generic", "state": None, "state_sensitive": False,
        "servings": [SERVING], "default_serving_id": "s", "per_100g": None, "payload": {},
    }


# ---------------------------------------------------------------------------
# 1. the migration — schema, backfill, fail-closed (a dry-run: rolled back)
# ---------------------------------------------------------------------------


def test_002_schema_backfill_and_fail_closed():
    async def run():
        conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
        outer = conn.transaction(force_rollback=True)
        await outer.__aenter__()
        try:
            tables = ("foods",) + TENANT_TABLES
            # the locks 002's ALTERs take, up front: the before/after counts
            # must see the same rows even while another session writes
            await conn.execute(f"LOCK TABLE {', '.join(tables)} IN ACCESS EXCLUSIVE MODE")
            before = {t: await _count(conn, t) for t in tables}
            if not await has_002(conn):
                await conn.execute(SQL_002)
            if not await has_003(conn):  # the weigh-in log rides the same transaction
                await conn.execute(SQL_003)
            assert {t: await _count(conn, t) for t in tables} == before, "002 changed row counts"
            assert await _count(conn, "quota_events") == 0

            # -- catalog: as the connecting role (no SET ROLE yet)
            cur = await conn.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'user_id'"
            )
            assert {r["table_name"] for r in await cur.fetchall()} == set(RLS_TABLES)
            cur = await conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relrowsecurity AND c.relforcerowsecurity"
            )
            assert {r["relname"] for r in await cur.fetchall()} == set(RLS_TABLES)
            assert await _scalar(conn, "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'") == len(RLS_TABLES)
            assert await _scalar(
                conn,
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'settings'::regclass AND contype = 'p'",
            ) == "PRIMARY KEY (user_id, key)"
            assert await _scalar(
                conn,
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'aliases'::regclass AND contype = 'u'",
            ) == "UNIQUE (user_id, alias_norm, food_id)"
            cur = await conn.execute(
                "SELECT conname FROM pg_constraint WHERE conname IN "
                "('item_resolutions_log_tenant_fkey', 'entries_log_tenant_fkey', 'logs_id_user_id_key')"
            )
            assert len(await cur.fetchall()) == 3
            # the ledger's one-log-a-day rule is the schema's: a partial unique index
            idx = await _scalar(
                conn, "SELECT indexdef FROM pg_indexes WHERE indexname = 'quota_events_log_day_key'"
            )
            assert idx is not None and idx.startswith("CREATE UNIQUE INDEX")
            assert "WHERE (kind = 'log'::text)" in idx
            # the reason the app role exists: Neon's connecting role carries BYPASSRLS
            # (not asserted — another instance's role may not); the app role never does
            assert await _scalar(conn, "SELECT rolbypassrls FROM pg_roles WHERE rolname = %s", (db.APP_ROLE,)) is False

            # -- backfill: every pre-existing row is the owner's (settings seeds at least)
            cur = await conn.execute(
                'SELECT id::text AS id FROM neon_auth."user" WHERE lower(email) = lower(%s)',
                (auth.DEFAULT_ALLOWED_EMAIL,),
            )
            owners = await cur.fetchall()
            assert len(owners) == 1
            owner_id = owners[0]["id"]
            cur = await conn.execute("SELECT key FROM settings WHERE user_id = %s", (owner_id,))
            owner_settings = {r["key"] for r in await cur.fetchall()}
            assert SEED_KEYS <= owner_settings
            for t in TENANT_TABLES:
                assert await _scalar(conn, f"SELECT count(*) FROM {t} WHERE user_id = '' OR user_id IS NULL") == 0

            # -- fail-closed: the app role with NO tenant sees nothing and writes nothing
            await conn.execute(db.tenant_sql(None))
            for t in TENANT_TABLES:
                assert await _count(conn, t) == 0, t
            assert await _count(conn, "foods") == before["foods"]  # global
            with pytest.raises((psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege)):
                async with conn.transaction():  # savepoint: the failed insert aborts only this
                    await conn.execute(
                        "INSERT INTO logs (id, input_mode, raw_input, captured_at) "
                        "VALUES (%s, 'text', 'x', now())",
                        (uuid4(),),
                    )
            # -- and the owner, under the app role, sees exactly their rows (once
            # 002 is live other accounts have settings too, so not before[...])
            await conn.execute(db.tenant_sql(owner_id))
            assert await _count(conn, "settings") == len(owner_settings)
        finally:
            await outer.__aexit__(None, None, None)
            await conn.close()

    _run(run())


# ---------------------------------------------------------------------------
# 2. two users cannot see each other; foods stay shared
# ---------------------------------------------------------------------------


def test_two_tenants_are_invisible_to_each_other():
    async def run():
        async with isolated_db() as conn:
            await _become(conn, "tenant-a")
            food = await insert_food(conn)
            log = await insert_log(conn)
            await insert_item(conn, log, food)
            await insert_entry(conn, log, food)
            await insert_alias(conn, food)
            await insert_meal(conn, food)
            await conn.execute("INSERT INTO settings (key, value) VALUES ('timezone', '\"Europe/Oslo\"'::jsonb)")
            mine = {t: await _count(conn, t) for t in TENANT_TABLES}
            assert all(n == 1 for n in mine.values()), mine

            await _become(conn, "tenant-b")
            for t in TENANT_TABLES:
                assert await _count(conn, t) == 0, f"{t} leaks across tenants"
            assert await _scalar(conn, "SELECT 1 FROM foods WHERE id = %s", (food,)) == 1  # shared cache
            # by id, by phrase, by key: nothing of A's is reachable
            assert await _scalar(conn, "SELECT count(*) FROM logs WHERE id = %s", (log,)) == 0
            assert await _scalar(conn, "SELECT count(*) FROM aliases WHERE alias_norm = 'two eggs'") == 0
            assert await _scalar(conn, "SELECT count(*) FROM settings WHERE key = 'timezone'") == 0
            cur = await conn.execute("UPDATE logs SET status = 'discarded' WHERE id = %s", (log,))
            assert cur.rowcount == 0
            cur = await conn.execute("DELETE FROM entries WHERE log_id = %s", (log,))
            assert cur.rowcount == 0
            # B's own rows with the same phrase/key: the constraints are per-tenant
            await insert_alias(conn, food)
            await conn.execute("INSERT INTO settings (key, value) VALUES ('timezone', '\"Asia/Tokyo\"'::jsonb)")
            # a child row under A's log is impossible even with the uuid in hand
            # (a fresh item_index, so it is the tenant FK that refuses, not the
            # global (log_id, item_index) uniqueness)
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                async with conn.transaction():
                    await insert_item(conn, log, food, item_index=1)

            await _become(conn, "tenant-a")
            assert {t: await _count(conn, t) for t in TENANT_TABLES} == mine
            assert await _scalar(conn, "SELECT value FROM settings WHERE key = 'timezone'") == "Europe/Oslo"

    _run(run())


# ---------------------------------------------------------------------------
# 3. the demo log quota — one ATTEMPT a day across every lane, recorded in
#    quota_events when admitted; the owner unlimited
# ---------------------------------------------------------------------------


def test_demo_log_quota_across_lanes_and_owner_unlimited(monkeypatch):
    decomposed: list[str] = []

    async def decompose(text, ctx, **kw):
        decomposed.append(text)
        return ParsedLog(items=[], meal_hint=None, when_hint=None, when_day_offset=None, when_meal=None, save_intent=None)

    _stub_pipeline(monkeypatch, decompose=decompose)

    def post(log_id, text="two eggs"):
        return logs_routes.create_log_endpoint(
            LogCreate(id=log_id, text=text, captured_at=NOW, tz="UTC")
        )

    async def run():
        async with isolated_db() as conn:
            demo = await _become(conn, "demo-1")
            status = await quota.demo_status(demo)
            assert (status["limit"], status["used_today"]) == (1, 0)
            assert datetime.fromisoformat(status["resets_at"]) == _next_utc_midnight()
            first = uuid4()
            out = await post(first, "nothing edible")  # the day's one attempt: admitted and recorded
            assert out.items == [] and decomposed == ["nothing edible"]
            assert await _count(conn, "quota_events") == 1
            assert (await quota.demo_status(demo))["used_today"] == 1

            with pytest.raises(quota.QuotaExceeded) as ei:
                await post(uuid4())
            exc = ei.value
            assert exc.status_code == 429
            assert exc.used == 1 and exc.limit == 1
            assert datetime.fromisoformat(exc.resets_at) == _next_utc_midnight()
            assert exc.detail == f"Demo accounts can log once a day — resets at {exc.resets_at}"
            resp = await quota.exception_handler(None, exc)  # the body main.py's handler sends
            assert resp.status_code == 429
            assert json.loads(resp.body) == {
                "detail": exc.detail, "resets_at": exc.resets_at, "used": 1, "limit": 1,
            }
            assert await _count(conn, "quota_events") == 1  # a refusal is not recorded ...
            assert await _count(conn, "logs") == 1  # ... and leaves no row behind

            # The same client id is free only once the server answers it from
            # stored state — a row with live items, or one that left 'staged'.
            # Everything else a same-id POST can re-run (routes/logs
            # _duplicate_post_response's orphan claim), so it is an attempt.
            past_grace = "UPDATE logs SET created_at = now() - interval '100 seconds' WHERE id = %s"
            await conn.execute(past_grace, (first,))
            with pytest.raises(quota.QuotaExceeded):
                await post(first, "a large pizza")  # an itemless staged row past its grace gets re-run
            food = await insert_food(conn)
            await insert_item(conn, first, food, staged=staged_item(food))
            replay = await post(first, "a large pizza")  # a replay of stored staging state: free
            assert len(replay.items) == 1
            await conn.execute(
                "UPDATE item_resolutions SET status = 'removed' WHERE log_id = %s", (first,)
            )
            await conn.execute(past_grace, (first,))  # the refused re-run stamped the claim
            with pytest.raises(quota.QuotaExceeded):
                await post(first, "a large pizza")  # every item removed: re-runnable again
            await conn.execute("UPDATE logs SET status = 'committed' WHERE id = %s", (first,))
            with pytest.raises(HTTPException) as ei:
                await post(first, "a large pizza")  # left 'staged': a replay is a 409, never a run
            assert ei.value.status_code == 409
            assert decomposed == ["nothing edible"]  # no re-run, ever
            assert await _count(conn, "quota_events") == 1  # free replays record nothing

            # every creating lane refuses before any read, write or model spend
            with pytest.raises(quota.QuotaExceeded):
                await post(uuid4())
            with pytest.raises(quota.QuotaExceeded):
                await logs_routes.create_log_voice(
                    audio=_upload(b"", "audio/webm"), log_id=uuid4(), captured_at=NOW, tz="UTC"
                )
            with pytest.raises(quota.QuotaExceeded):
                await photo.create_log_photo(
                    image=_upload(JPEG, "image/jpeg"), log_id=uuid4(), captured_at=NOW
                )
            with pytest.raises(quota.QuotaExceeded):
                await quick.create_log_from_food(
                    quick.FromFoodRequest(
                        id=uuid4(), food_id=UUID(food), input_mode="search", captured_at=NOW
                    )
                )
            assert await _count(conn, "logs") == 1  # nothing slipped through

            # a capture failure is an attempt (recorded with the claim, before
            # the STT gate), while the free request checks (type, size) spend nothing
            await _become(conn, "demo-2")
            with pytest.raises(HTTPException) as ei:
                await logs_routes.create_log_voice(
                    audio=_upload(b"x", "audio/ogg"), log_id=uuid4(), captured_at=NOW, tz="UTC"
                )
            assert ei.value.status_code == 415
            with pytest.raises(HTTPException) as ei:
                await photo.create_log_photo(
                    image=_upload(b"not an image", "image/jpeg"), log_id=uuid4(), captured_at=NOW
                )
            assert ei.value.status_code == 415
            assert await _count(conn, "quota_events") == 0
            out = await logs_routes.create_log_voice(
                audio=_upload(b"", "audio/webm"), log_id=uuid4(), captured_at=NOW, tz="UTC"
            )
            assert out.status == "capture_failure"
            with pytest.raises(quota.QuotaExceeded):
                await quota.check_log()

            # the owner: unlimited and unrecorded, and no demo block on /api/me
            owner = await _become(conn, "owner-1", is_owner=True)
            for _ in range(3):
                await quota.check_log()
            assert await _count(conn, "quota_events") == 0
            assert await quota.demo_status(owner) is None

    _run(run())


# ---------------------------------------------------------------------------
# 4. the demo re-resolution cap — 10 a day, recorded in quota_events
# ---------------------------------------------------------------------------


def test_demo_resolve_cap():
    async def run():
        async with isolated_db() as conn:
            await _become(conn, "demo-3")
            for _ in range(quota.DEMO_RESOLVE_LIMIT):
                await quota.check_resolve()
            assert await _count(conn, "quota_events") == quota.DEMO_RESOLVE_LIMIT

            with pytest.raises(quota.QuotaExceeded) as ei:
                await quota.check_resolve()
            exc = ei.value
            assert exc.status_code == 429 and exc.used == 10 and exc.limit == 10
            assert exc.detail == f"Demo accounts can re-resolve 10 items a day — resets at {exc.resets_at}"
            assert await _count(conn, "quota_events") == quota.DEMO_RESOLVE_LIMIT  # a refusal is not recorded

            # the three model-spending calls refuse up front — on a live row of
            # the demo's; an unknown item is 404 before the cap is consulted
            # (test_quota_hardening Q3/Q4) — and the local answer paths are free
            food = await insert_food(conn)
            log = await insert_log(conn)
            await insert_item(conn, log, food, status="needs_input")
            with pytest.raises(quota.QuotaExceeded):
                await resolve_routes.kick_tier3(Tier3KickRequest(log_id=log, item_index=0))
            with pytest.raises(quota.QuotaExceeded):
                await resolve_routes.resolve_item(
                    ResolveRequest(log_id=log, item_index=0, phrase="two eggs")
                )
            with pytest.raises(quota.QuotaExceeded):  # free text = a second agent run
                await resolve_routes.answer_question(
                    resolve_routes.AnswerRequest(
                        log_id=log, item_index=0, answer=resolve_routes.Answer(text="the blue one")
                    )
                )
            with pytest.raises(HTTPException) as ei:  # unknown item: 404, the cap untouched
                await resolve_routes.kick_tier3(Tier3KickRequest(log_id=uuid4(), item_index=0))
            assert ei.value.status_code == 404
            with pytest.raises(HTTPException) as ei:  # a food pick is a local read: past the cap
                await resolve_routes.answer_question(
                    resolve_routes.AnswerRequest(
                        log_id=log, item_index=0, answer=resolve_routes.Answer(food_id=str(uuid4()))
                    )
                )
            assert ei.value.status_code == 404

            # the owner: unlimited and unrecorded
            await _become(conn, "owner-2", is_owner=True)
            for _ in range(quota.DEMO_RESOLVE_LIMIT + 2):
                await quota.check_resolve()
            assert await _count(conn, "quota_events") == 0

    _run(run())


# ---------------------------------------------------------------------------
# 5. GET /api/me — shapes for the owner and a demo account
# ---------------------------------------------------------------------------


def test_me_shapes():
    async def run():
        async with isolated_db() as conn:
            owner = await _become(conn, "owner-3", is_owner=True)
            me = await me_routes.get_me(user=owner)
            assert me.model_dump() == {"email": owner["email"], "is_owner": True, "demo": None}

            demo = await _become(conn, "demo-4")
            me = await me_routes.get_me(user=demo)
            assert me.email == demo["email"] and me.is_owner is False
            assert (me.demo.limit, me.demo.used_today) == (1, 0)
            assert datetime.fromisoformat(me.demo.resets_at) == _next_utc_midnight()
            await quota.check_log()  # an admitted attempt
            assert (await me_routes.get_me(user=demo)).demo.used_today == 1

    _run(run())


# ---------------------------------------------------------------------------
# 6. over HTTP: the 429 body and /api/me, with the claim and the quota status
#    stubbed (no DB — the harness's connection cannot cross into TestClient's loop)
# ---------------------------------------------------------------------------


def _app(register_handler: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(logs_routes.router)
    app.include_router(me_routes.router)
    if register_handler:
        app.add_exception_handler(quota.QuotaExceeded, quota.exception_handler)
    app.dependency_overrides[auth.require_user] = override_user(
        email="demo@example.test", user_id="demo-http", is_owner=False
    )
    return app


def test_http_429_body_and_me(monkeypatch):
    at = _next_utc_midnight().isoformat()

    async def refuse(sql, params):
        raise quota.QuotaExceeded(
            f"Demo accounts can log once a day — resets at {at}", resets_at=at, used=1, limit=1
        )

    async def status(user):
        assert user == {"id": "demo-http", "email": "demo@example.test", "is_owner": False}
        return {"limit": 1, "used_today": 1, "resets_at": at}

    monkeypatch.setattr(logs_routes, "claim_log", refuse)  # the claim is where the quota refuses
    monkeypatch.setattr(quota, "demo_status", status)
    body = {"id": str(uuid4()), "text": "two eggs", "captured_at": NOW.isoformat(), "tz": "UTC"}

    with TestClient(_app(register_handler=True)) as client:
        r = client.post("/api/logs", json=body)
        assert r.status_code == 429
        assert r.json() == {
            "detail": f"Demo accounts can log once a day — resets at {at}",
            "resets_at": at, "used": 1, "limit": 1,
        }
        r = client.get("/api/me")
        assert r.status_code == 200
        assert r.json() == {
            "email": "demo@example.test", "is_owner": False,
            "demo": {"limit": 1, "used_today": 1, "resets_at": at},
        }
    # handler not registered (main.py forgot): still a 429 carrying the sentence
    with TestClient(_app(register_handler=False)) as client:
        r = client.post("/api/logs", json=body)
        assert r.status_code == 429
        assert r.json() == {"detail": f"Demo accounts can log once a day — resets at {at}"}


# ---------------------------------------------------------------------------
# 7. db.py's real path binds role + tenant per statement (needs 002 applied)
# ---------------------------------------------------------------------------


def test_db_binds_app_role_and_tenant_per_statement():
    async def run():
        conn = await psycopg.AsyncConnection.connect(db._database_url())
        try:
            applied = await has_002(conn)
        finally:
            await conn.close()
        if not applied:
            pytest.skip("002 not applied yet: db.py's real path needs role tally_app")
        probe = "SELECT current_user AS who, current_setting('app.user_id', true) AS tenant"
        try:
            db.current_user_id.set("tenant-probe")
            assert await db.fetchone(probe) == {"who": db.APP_ROLE, "tenant": "tenant-probe"}
            async with db.tx() as c:
                cur = await c.execute(probe)
                assert await cur.fetchone() == {"who": db.APP_ROLE, "tenant": "tenant-probe"}
            db.current_user_id.set(None)
            assert await db.fetchone(probe) == {"who": db.APP_ROLE, "tenant": ""}
            assert await db.fetchall("SELECT 1 FROM settings") == []  # no tenant: nothing
        finally:
            await db.close()

    _run(run())


# ---------------------------------------------------------------------------
# 8. the same client id cannot re-run the pipeline for free: a log that
#    decomposed to nothing is an itemless staged row, which a same-id POST
#    claims and re-runs on the NEW body once the §8.6 grace period is over.
#    The attempt ledger makes that the next attempt (open-source pass, S1).
# ---------------------------------------------------------------------------


def test_same_id_repost_cannot_rerun_decompose_for_free(monkeypatch):
    calls: list[str] = []

    async def decompose(text, ctx, **kw):
        calls.append(text)
        return ParsedLog(items=[], meal_hint=None, when_hint=None, when_day_offset=None, when_meal=None, save_intent=None)

    _stub_pipeline(monkeypatch, decompose=decompose)

    async def run():
        async with isolated_db() as conn:
            demo = await _become(conn, "demo-5")
            log_id = uuid4()
            out = await logs_routes.create_log_endpoint(
                LogCreate(id=log_id, text="nothing edible", captured_at=NOW, tz="UTC")
            )
            assert out.items == [] and calls == ["nothing edible"]
            # past the grace period the orphan-claim path is open …
            await conn.execute(
                "UPDATE logs SET created_at = now() - interval '100 seconds' WHERE id = %s",
                (log_id,),
            )
            # … and the same id with a new body is refused before decompose
            with pytest.raises(quota.QuotaExceeded):
                await logs_routes.create_log_endpoint(
                    LogCreate(id=log_id, text="a large pizza", captured_at=NOW, tz="UTC")
                )
            assert calls == ["nothing edible"]
            assert (await quota.demo_status(demo))["used_today"] == 1

            # the owner keeps the §8.6 re-run
            await _become(conn, "owner-5", is_owner=True)
            own = uuid4()
            await logs_routes.create_log_endpoint(
                LogCreate(id=own, text="nothing edible", captured_at=NOW, tz="UTC")
            )
            await conn.execute(
                "UPDATE logs SET created_at = now() - interval '100 seconds' WHERE id = %s",
                (own,),
            )
            await logs_routes.create_log_endpoint(
                LogCreate(id=own, text="a large pizza", captured_at=NOW, tz="UTC")
            )
            assert calls == ["nothing edible", "nothing edible", "a large pizza"]
            assert await _count(conn, "quota_events") == 0

    _run(run())


# ---------------------------------------------------------------------------
# 9. a photo attempt vision rejects still counts: _drop_empty_log removes the
#    itemless row, the ledger row stays (open-source pass, S2)
# ---------------------------------------------------------------------------


def test_photo_vision_failure_keeps_the_attempt(monkeypatch):
    calls: list[str] = []

    async def analyze(image, mime, **kw):  # kw: the lane grew context/web
        calls.append(mime)
        raise OpenAIError("vision down")

    _stub_pipeline(monkeypatch, vision=analyze)

    async def run():
        async with isolated_db() as conn:
            demo = await _become(conn, "demo-6")
            log_id = uuid4()
            with pytest.raises(HTTPException) as ei:
                await photo.create_log_photo(
                    image=_upload(JPEG, "image/jpeg"), log_id=log_id, captured_at=NOW
                )
            assert ei.value.status_code == 503 and calls == ["image/jpeg"]
            assert await _count(conn, "logs") == 0  # the itemless row was dropped
            assert (await quota.demo_status(demo))["used_today"] == 1
            # the client's retry under the same id is the next attempt
            with pytest.raises(quota.QuotaExceeded):
                await photo.create_log_photo(
                    image=_upload(JPEG, "image/jpeg"), log_id=log_id, captured_at=NOW
                )
            assert calls == ["image/jpeg"]

    _run(run())


# ---------------------------------------------------------------------------
# 10. the shared foods cache (§8.9): a demo's chosen label and web
#     transcriptions never land on it — every account reads the cache back
#     through manual search and the tier-3 candidate list (open-source pass, S3)
# ---------------------------------------------------------------------------


def test_demo_labels_and_web_foods_stay_off_the_shared_cache(monkeypatch):
    monkeypatch.setattr(fdc, "get_food", _fake_fdc_food)
    planted = f"zz planted label {uuid4().hex[:8]}"  # unique: the shared table is production's
    url = "https://www.example-chain.com/nutrition/item"
    web_args = json.dumps({
        "source": "web", "source_id": None, "display_name": planted,
        "web": {
            "source_url": url, "page_title": "Item", "brand": "Example Chain",
            "citations": [{"url": url, "title": "Item"}],
            "values": {
                "serving_description": "1 item (100 g)", "grams": 100, "kcal": 300,
                "protein_g": 10, "carb_g": 30, "fat_g": 15, "fiber_g": None,
                "sugar_g": 5, "sodium_mg": 500,
            },
        },
    })

    async def label_of(conn, food_id) -> str:
        return await _scalar(conn, "SELECT display_name FROM foods WHERE id = %s", (food_id,))

    async def run():
        async with isolated_db() as conn:
            await _become(conn, "demo-7")
            # the Library pick: the row keeps the adapter's own name
            out = await foods_routes.select_food(
                foods_routes.SelectRequest(source="fdc", source_id=uuid4().hex, display_name=planted)
            )
            assert await label_of(conn, out.food_id) == "cheesecake"
            # the id-bearing answer / manual-search pick on a staged row: same
            log = await insert_log(conn)
            await insert_item(conn, log, out.food_id, status="needs_input")
            item = await tier3.resolve_item_from_source(
                str(log), 0, "fdc", uuid4().hex, display_name=planted
            )
            assert item.food is not None and item.food.name == "cheesecake"
            assert await label_of(conn, item.food.id) == "cheesecake"

            # the tier-3 agent's cache tool: web mode is refused outright (and
            # a demo's agent carries no web tools at all — test_quota_hardening S1) …
            persisted: dict = {}
            agent = tier3._build_agent({"network_calls": 0}, {}, persisted, {})
            assert {t.name for t in agent.tools}.isdisjoint({"web_search", "pin_chain_domain", "fetch_page"})
            add_food = _tool(agent, "add_food")
            out = json.loads(await add_food.on_invoke_tool(_ctx("add_food", web_args), web_args))
            assert out["error"] == "web_lane_owner_only" and not persisted
            assert await _scalar(conn, "SELECT count(*) FROM foods WHERE source_url = %s", (url,)) == 0
            # … and from_source mode caches under the adapter's name
            fdc_args = json.dumps({
                "source": "fdc", "source_id": uuid4().hex, "display_name": planted, "web": None,
            })
            out = json.loads(await add_food.on_invoke_tool(_ctx("add_food", fdc_args), fdc_args))
            assert await label_of(conn, out["food_id"]) == "cheesecake"
            assert await _scalar(
                conn, "SELECT count(*) FROM foods WHERE display_name = %s", (planted,)
            ) == 0

            # the owner's label lands, as before
            await _become(conn, "owner-7", is_owner=True)
            out = await foods_routes.select_food(
                foods_routes.SelectRequest(source="fdc", source_id=uuid4().hex, display_name="cheesecake slice")
            )
            assert await label_of(conn, out.food_id) == "cheesecake slice"

    _run(run())
