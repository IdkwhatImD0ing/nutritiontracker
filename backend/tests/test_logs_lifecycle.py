"""W3 tests — logs route lifecycle (watchdog, timings, POST race, discard).

DESIGN.md §3.4 failure lifecycle, §5 routes, §9 staged_ms; BACKLOG 1/2/10/11.

Every test runs inside a transaction that is ROLLED BACK (isolated_db patches
backend/db.py onto one connection with force_rollback) — the live Neon DB is
never dirtied. Cross-workstream modules (tracing, services.*) are stubbed at
run time via sys.modules, same pattern as test_resolver.py.

Run:  uv run --with pytest pytest backend/tests/test_logs_lifecycle.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

# ---------------------------------------------------------------------------
# stubs for parallel workstreams — installed per-module by the autouse fixture
# (routes resolve tracing/services lazily at call time, so a run-time
# sys.modules swap is sufficient; see test_resolver.py for the rationale).
# ---------------------------------------------------------------------------

# Controllable decompose: tests set _decompose["fn"]; default refuses.
_decompose: dict = {"fn": None}


async def _decompose_dispatch(text, ctx):
    fn = _decompose["fn"]
    if fn is None:
        raise AssertionError("decompose must not be called by this test")
    return await fn(text, ctx)


def _build_stubs() -> dict:
    tracing_mod = types.ModuleType("tracing")

    class _Gen:
        trace_id = "trace-test"

        def update(self, **kw):
            pass

    @asynccontextmanager
    async def generation(name):
        yield _Gen()

    tracing_mod.init_tracing = lambda: None
    tracing_mod.generation = generation
    tracing_mod.flush = lambda: None
    tracing_mod.score_commit = lambda *a, **kw: None

    services_mod = types.ModuleType("services")
    services_mod.__path__ = []

    ctx_mod = types.ModuleType("services.context")

    async def build_agent_context():
        return SimpleNamespace(profile_notes="", targets={}, food_index=[], meal_names=[])

    ctx_mod.build_agent_context = build_agent_context

    dec_mod = types.ModuleType("services.decompose")
    dec_mod.decompose = _decompose_dispatch

    t3_mod = types.ModuleType("services.tier3")

    async def run_tier3(log_id, item_index, refinement=None):
        raise AssertionError("tier3 is not exercised by these tests")

    t3_mod.run_tier3 = run_tier3

    services_mod.context = ctx_mod
    services_mod.decompose = dec_mod
    services_mod.tier3 = t3_mod
    return {
        "tracing": tracing_mod,
        "services": services_mod,
        "services.context": ctx_mod,
        "services.decompose": dec_mod,
        "services.tier3": t3_mod,
    }


@pytest.fixture(scope="module", autouse=True)
def _workstream_stubs():
    stubs = _build_stubs()
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture(autouse=True)
def _reset_decompose():
    _decompose["fn"] = None
    yield
    _decompose["fn"] = None


import db  # noqa: E402
from models import (  # noqa: E402
    CommitRequest,
    LogCreate,
    Meal,
    ParsedItem,
    ParsedLog,
    StagedItem,
)
from routes.commit import commit_log as commit_route  # noqa: E402
from routes.logs import (  # noqa: E402
    TimingsPatch,
    create_log,
    discard_log,
    get_log,
    patch_timings,
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


# ---------------------------------------------------------------------------
# rollback harness — patches backend/db.py onto one force-rollback connection
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def insert_log(conn, *, status="staged", created_ago_s: float = 0.0, raw="x"):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status, created_at) "
        "VALUES (%s, 'text', %s, now(), %s, %s)",
        (log_id, raw, status, NOW() - timedelta(seconds=created_ago_s)),
    )
    return log_id


async def insert_searching_item(
    conn,
    log_id,
    item_index,
    *,
    started_ago_s: float | None,
    attempts: int,
    created_ago_s: float = 0.0,
    extra_staged: dict | None = None,
):
    staged = StagedItem(
        item_index=item_index, phrase=f"mystery{item_index}", status="searching",
        meal=Meal.snack,
    ).model_dump(mode="json")
    staged.update(extra_staged or {})
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                      staged, tier3_started_at, tier3_attempts, created_at)
        VALUES (%s, %s, %s, %s, 'searching', %s, %s, %s, %s)
        """,
        (
            log_id,
            item_index,
            f"mystery{item_index}",
            f"mystery{item_index}",
            Json(staged),
            None if started_ago_s is None else NOW() - timedelta(seconds=started_ago_s),
            attempts,
            NOW() - timedelta(seconds=created_ago_s),
        ),
    )


def log_body(text, log_id=None):
    return LogCreate(
        id=log_id or uuid4(), text=text, captured_at=NOW(), tz="America/Los_Angeles"
    )


def novel_parsed_log(phrase):
    return ParsedLog(
        items=[
            ParsedItem(
                phrase=phrase, phrase_kind="food", qty=None, unit=None,
                state=None, brand=None, library_match=None, meal_ref=None,
            )
        ],
        meal_hint=None, when_hint=None, save_intent=None,
    )


# ---------------------------------------------------------------------------
# 1. BACKLOG 1 — watchdog state machine across the three regimes (§3.4)
# ---------------------------------------------------------------------------


def test_watchdog_three_regimes():
    async def run():
        async with isolated_db() as conn:
            log_id = await insert_log(conn)
            # fresh searching: 10s old, 1 attempt → untouched
            await insert_searching_item(conn, log_id, 0, started_ago_s=10, attempts=1)
            # stale, attempts < 2 → stale:true returned, row NOT mutated
            await insert_searching_item(conn, log_id, 1, started_ago_s=120, attempts=1)
            # stale, attempts >= 2 → server flips to not_found, payload rewritten
            await insert_searching_item(
                conn, log_id, 2, started_ago_s=120, attempts=2,
                extra_staged={"spoken_qty": 3, "spoken_unit": "g"},
            )
            # never kicked (tier3_started_at NULL): created_at is the clock
            await insert_searching_item(
                conn, log_id, 3, started_ago_s=None, attempts=0, created_ago_s=120
            )

            resp = await get_log(log_id)
            by_idx = {i.item_index: i for i in resp.items}

            assert by_idx[0].status == "searching" and by_idx[0].stale is None
            assert by_idx[1].status == "searching" and by_idx[1].stale is True
            assert by_idx[2].status == "not_found"
            assert by_idx[3].status == "searching" and by_idx[3].stale is True

            rows = {
                r["item_index"]: r
                for r in await db.fetchall(
                    "SELECT * FROM item_resolutions WHERE log_id = %s", (log_id,)
                )
            }
            # stale-once rows stay 'searching' in the DB with attempts untouched
            # (the client owns the re-kick; only the kick increments attempts)
            assert rows[1]["status"] == "searching"
            assert rows[1]["tier3_attempts"] == 1
            assert "stale" not in rows[1]["staged"]  # never persisted
            assert rows[3]["status"] == "searching"
            assert rows[3]["tier3_attempts"] == 0
            # exhausted row is flipped AND its payload rewritten, extras intact
            assert rows[2]["status"] == "not_found"
            assert rows[2]["staged"]["status"] == "not_found"
            assert rows[2]["staged"]["spoken_qty"] == 3  # §7.3 extras survive
            assert rows[2]["staged"]["spoken_unit"] == "g"

            # a second poll is stable: same answers, no further mutation
            resp2 = await get_log(log_id)
            by_idx2 = {i.item_index: i for i in resp2.items}
            assert by_idx2[1].status == "searching" and by_idx2[1].stale is True
            assert by_idx2[2].status == "not_found"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. BACKLOG 2 — PATCH /api/logs/{id}/timings sets staged_ms exactly once
# ---------------------------------------------------------------------------


def test_timings_patch_sets_once_then_conflicts():
    async def run():
        async with isolated_db() as conn:
            log_id = await insert_log(conn)

            resp = await patch_timings(log_id, TimingsPatch(staged_ms=1234))
            assert resp.staged_ms == 1234
            row = await db.fetchone("SELECT staged_ms FROM logs WHERE id = %s", (log_id,))
            assert row["staged_ms"] == 1234

            # idempotent replay of the same measurement — no-op, 200
            resp2 = await patch_timings(log_id, TimingsPatch(staged_ms=1234))
            assert resp2.staged_ms == 1234

            # a different value must not overwrite the first render's timing
            with pytest.raises(HTTPException) as exc:
                await patch_timings(log_id, TimingsPatch(staged_ms=999))
            assert exc.value.status_code == 409
            row = await db.fetchone("SELECT staged_ms FROM logs WHERE id = %s", (log_id,))
            assert row["staged_ms"] == 1234

            with pytest.raises(HTTPException) as exc:
                await patch_timings(uuid4(), TimingsPatch(staged_ms=1))
            assert exc.value.status_code == 404

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. BACKLOG 10 — concurrent same-id POST: one log row, one pipeline run
# ---------------------------------------------------------------------------


def test_concurrent_double_post_one_log_one_pipeline():
    async def run():
        async with isolated_db() as conn:
            calls: list[str] = []
            phrase = f"novel{uuid4().hex[:8]}"

            async def fake_decompose(text, ctx):
                calls.append(text)
                await asyncio.sleep(0.3)  # hold the winner mid-pipeline
                return novel_parsed_log(phrase)

            _decompose["fn"] = fake_decompose

            log_id = uuid4()
            text = f"{phrase} something"
            r1, r2 = await asyncio.gather(
                create_log(log_body(text, log_id)), create_log(log_body(text, log_id))
            )

            # exactly one pipeline run and one set of rows
            assert len(calls) == 1
            n_logs = await db.fetchone(
                "SELECT count(*) AS n FROM logs WHERE id = %s", (log_id,)
            )
            assert n_logs["n"] == 1
            items = await db.fetchall(
                "SELECT * FROM item_resolutions WHERE log_id = %s", (log_id,)
            )
            assert len(items) == 1 and items[0]["status"] == "searching"

            # the winner returned the staged item; the loser returned the
            # stored state (empty while the winner was mid-pipeline — the
            # client polls GET) — never a second pipeline run
            counts = sorted(len(r.items) for r in (r1, r2))
            assert counts in ([0, 1], [1, 1])
            assert r1.log_id == r2.log_id == log_id

            # a later sequential replay returns the stored staging state
            r3 = await create_log(log_body(text, log_id))
            assert len(r3.items) == 1 and r3.items[0].status == "searching"
            assert len(calls) == 1  # still exactly one decompose

    asyncio.run(run())


def test_orphaned_staged_log_reruns_only_after_grace():
    async def run():
        async with isolated_db() as conn:
            calls: list[str] = []
            phrase = f"novel{uuid4().hex[:8]}"

            async def fake_decompose(text, ctx):
                calls.append(text)
                return novel_parsed_log(phrase)

            _decompose["fn"] = fake_decompose

            # a fresh itemless staged log (owner presumed alive): no re-run
            fresh_id = await insert_log(conn, created_ago_s=0)
            resp = await create_log(log_body(phrase, fresh_id))
            assert resp.items == [] and calls == []

            # an orphan older than the grace period: claimed and re-run
            orphan_id = await insert_log(conn, created_ago_s=60)
            before = (
                await db.fetchone("SELECT created_at FROM logs WHERE id = %s", (orphan_id,))
            )["created_at"]
            resp = await create_log(log_body(phrase, orphan_id))
            assert len(calls) == 1
            assert len(resp.items) == 1 and resp.items[0].status == "searching"
            after = (
                await db.fetchone("SELECT created_at FROM logs WHERE id = %s", (orphan_id,))
            )["created_at"]
            assert after > before  # the claim stamp — a rival retry loses the CAS
            items = await db.fetchall(
                "SELECT * FROM item_resolutions WHERE log_id = %s", (orphan_id,)
            )
            assert len(items) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. BACKLOG 11 — DELETE /api/logs/{id}: discard lifecycle
# ---------------------------------------------------------------------------


def test_discard_lifecycle():
    async def run():
        async with isolated_db() as conn:
            log_id = await insert_log(conn)

            resp = await discard_log(log_id)
            assert resp.status == "discarded"
            row = await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,))
            assert row["status"] == "discarded"

            # idempotent replay
            resp2 = await discard_log(log_id)
            assert resp2.status == "discarded"

            # a later commit attempt is rejected cleanly (409, nothing written)
            with pytest.raises(HTTPException) as exc:
                await commit_route(log_id, CommitRequest(items=[]))
            assert exc.value.status_code == 409
            row = await db.fetchone("SELECT status FROM logs WHERE id = %s", (log_id,))
            assert row["status"] == "discarded"
            entries = await db.fetchall(
                "SELECT * FROM entries WHERE log_id = %s", (log_id,)
            )
            assert entries == []

            # only 'staged' logs can be discarded
            committed_id = await insert_log(conn, status="committed")
            with pytest.raises(HTTPException) as exc:
                await discard_log(committed_id)
            assert exc.value.status_code == 409

            with pytest.raises(HTTPException) as exc:
                await discard_log(uuid4())
            assert exc.value.status_code == 404

    asyncio.run(run())
