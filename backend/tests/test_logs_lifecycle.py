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
from zoneinfo import ZoneInfo

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


# Controllable transcribe (same pattern): tests set _transcribe["fn"].
_transcribe: dict = {"fn": None}


async def _transcribe_dispatch(audio_bytes, mime):
    fn = _transcribe["fn"]
    if fn is None:
        raise AssertionError("transcribe must not be called by this test")
    return await fn(audio_bytes, mime)


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

    # transcribe: the dispatch is stubbed (no live STT in these tests) but the
    # validation helpers and result types are the REAL ones — the route's
    # near-empty gate must be exercised against real logic, not a copy.
    from services.transcribe import Transcription, TranscriptionError, is_near_empty

    stt_mod = types.ModuleType("services.transcribe")
    stt_mod.transcribe = _transcribe_dispatch
    stt_mod.Transcription = Transcription
    stt_mod.TranscriptionError = TranscriptionError
    stt_mod.is_near_empty = is_near_empty

    services_mod.context = ctx_mod
    services_mod.decompose = dec_mod
    services_mod.tier3 = t3_mod
    services_mod.transcribe = stt_mod
    return {
        "tracing": tracing_mod,
        "services": services_mod,
        "services.context": ctx_mod,
        "services.decompose": dec_mod,
        "services.tier3": t3_mod,
        "services.transcribe": stt_mod,
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
    _transcribe["fn"] = None
    yield
    _decompose["fn"] = None
    _transcribe["fn"] = None


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
    _RERUN_GRACE_SECONDS,
    TimingsPatch,
    create_log,
    discard_log,
    get_log,
    interpret_when_hint,
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
            # (created_ago_s tracks the constant — BACKLOG 30 bumped it to 90s)
            orphan_id = await insert_log(conn, created_ago_s=_RERUN_GRACE_SECONDS + 30)
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


# ---------------------------------------------------------------------------
# 5. BACKLOG 26 — §7.2 when_hint interpreter (deterministic table test)
# ---------------------------------------------------------------------------

WHEN_TZ = "America/Los_Angeles"
# 2026-08-20 03:15 UTC = 2026-08-19 20:15 PDT (an evening capture)
WHEN_CAPTURED = datetime(2026, 8, 20, 3, 15, tzinfo=timezone.utc)


def test_when_hint_interpreter_table():
    local = WHEN_CAPTURED.astimezone(ZoneInfo(WHEN_TZ))
    cases = [
        # (hint, day delta, (hour, minute) local, meal)
        ("this morning", 0, (8, 0), Meal.breakfast),
        ("This morning.", 0, (8, 0), Meal.breakfast),  # norm() applied
        ("yesterday breakfast", -1, (8, 0), Meal.breakfast),
        ("yesterday lunch", -1, (12, 30), Meal.lunch),
        ("yesterday at lunch", -1, (12, 30), Meal.lunch),
        ("Yesterday, at dinner", -1, (18, 30), Meal.dinner),
        ("yesterday morning", -1, (8, 0), Meal.breakfast),
        ("yesterday evening", -1, (18, 30), Meal.dinner),
        ("yesterday snack", -1, (22, 0), Meal.snack),
        ("last night", -1, (22, 0), Meal.snack),
    ]
    for hint, day_delta, (hour, minute), meal in cases:
        shift = interpret_when_hint(hint, WHEN_CAPTURED, WHEN_TZ)
        assert shift.note is None, hint
        assert shift.meal == meal, hint
        expected = (local + timedelta(days=day_delta)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        assert shift.logged_at == expected, hint

    # no hint → nothing shifts, nothing noted
    for hint in (None, "", "   "):
        shift = interpret_when_hint(hint, WHEN_CAPTURED, WHEN_TZ)
        assert shift.logged_at is None and shift.meal is None and shift.note is None

    # unknown hints are ignored WITH a note — never guessed at
    for hint in ("two days ago", "last week", "yesterday", "earlier"):
        shift = interpret_when_hint(hint, WHEN_CAPTURED, WHEN_TZ)
        assert shift.logged_at is None and shift.meal is None
        assert shift.note is not None and hint in shift.note

    # clamp: "this morning" spoken at 06:00 local never moves logged_at
    # forward to the canonical 08:00 — a when-hint refers to the past
    early = datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)  # 06:00 PDT
    shift = interpret_when_hint("this morning", early, WHEN_TZ)
    assert shift.meal == Meal.breakfast
    assert shift.logged_at == early  # same instant, tz-normalized

    # unknown timezone falls back to UTC rather than failing the log:
    # 03:15 UTC is before the canonical 08:00, so the clamp keeps the instant
    shift = interpret_when_hint("this morning", WHEN_CAPTURED, "Not/AZone")
    assert shift.meal == Meal.breakfast
    assert shift.logged_at == WHEN_CAPTURED


# ---------------------------------------------------------------------------
# 6. BACKLOG 26 — when_hint applied through POST /api/logs
# ---------------------------------------------------------------------------


def test_when_hint_shifts_captured_at_and_meal_through_route():
    async def run():
        async with isolated_db() as conn:
            phrase = f"novel{uuid4().hex[:8]}"

            async def fake_decompose(text, ctx):
                parsed = novel_parsed_log(phrase)
                parsed.when_hint = "yesterday lunch"
                return parsed

            _decompose["fn"] = fake_decompose
            body = log_body(f"yesterday at lunch i had {phrase}")
            resp = await create_log(body)

            assert resp.meal == Meal.lunch
            assert all(i.meal == Meal.lunch for i in resp.items)
            expected = interpret_when_hint(
                "yesterday lunch", body.captured_at, body.tz
            ).logged_at
            row = await db.fetchone(
                "SELECT captured_at, meal_inferred FROM logs WHERE id = %s", (body.id,)
            )
            assert row["meal_inferred"] == "lunch"
            # entries.logged_at snapshots logs.captured_at at commit, so the
            # §7.2 shift must land on captured_at (same instant comparison)
            assert row["captured_at"] == expected
            assert row["captured_at"] < body.captured_at  # actually moved back

    asyncio.run(run())


def test_spoken_meal_hint_outranks_when_hint_meal():
    async def run():
        async with isolated_db() as conn:
            phrase = f"novel{uuid4().hex[:8]}"

            async def fake_decompose(text, ctx):
                parsed = novel_parsed_log(phrase)
                parsed.meal_hint = Meal.dinner  # explicit spoken meal
                parsed.when_hint = "yesterday lunch"
                return parsed

            _decompose["fn"] = fake_decompose
            body = log_body(f"{phrase} for dinner yesterday lunch")
            resp = await create_log(body)

            assert resp.meal == Meal.dinner  # meal_hint wins the meal…
            row = await db.fetchone(
                "SELECT captured_at FROM logs WHERE id = %s", (body.id,)
            )
            expected = interpret_when_hint(
                "yesterday lunch", body.captured_at, body.tz
            ).logged_at
            assert row["captured_at"] == expected  # …the date shift still applies

    asyncio.run(run())


def test_unknown_when_hint_ignored_with_note_in_staged():
    async def run():
        async with isolated_db() as conn:
            phrase = f"novel{uuid4().hex[:8]}"

            async def fake_decompose(text, ctx):
                parsed = novel_parsed_log(phrase)
                parsed.when_hint = "two days ago"
                return parsed

            _decompose["fn"] = fake_decompose
            body = log_body(f"two days ago i had {phrase}")
            await create_log(body)

            row = await db.fetchone(
                "SELECT captured_at FROM logs WHERE id = %s", (body.id,)
            )
            assert row["captured_at"] == body.captured_at  # untouched
            items = await db.fetchall(
                "SELECT staged FROM item_resolutions WHERE log_id = %s", (body.id,)
            )
            assert items and all(
                "two days ago" in r["staged"]["when_hint_note"] for r in items
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 7. §5/§8.1d — voice multipart path through the HTTP route (TestClient).
#    These hit the live DB (no rollback harness across the TestClient's event
#    loop) and self-clean their rows, per the workstream test contract.
# ---------------------------------------------------------------------------


def _voice_app():
    from fastapi import FastAPI

    import routes.logs as logs_routes
    from auth import require_user

    app = FastAPI()
    app.include_router(logs_routes.router)
    app.dependency_overrides[require_user] = lambda: {"email": "test", "user_id": "t"}
    return app


def _voice_form(log_id):
    return {
        "id": str(log_id),
        "captured_at": NOW().isoformat(),
        "tz": "America/Los_Angeles",
    }


async def _cleanup_log(log_id) -> None:
    await db.execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
    await db.execute("DELETE FROM logs WHERE id = %s", (log_id,))
    await db.close()


def test_voice_zero_byte_blob_is_capture_failure_via_route():
    """§13's silent blob: zero bytes of audio → capture_failure before any STT
    call (the transcribe stub's default would AssertionError if reached)."""
    from fastapi.testclient import TestClient

    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.webm", b"", "audio/webm")},
            )
            assert r.status_code == 200
            assert r.json()["status"] == "capture_failure"
            assert r.json()["log_id"] == str(log_id)
            assert r.json()["capture_error"] == "silent_blob"

            # idempotent replay of the same capture id
            r2 = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.webm", b"", "audio/webm")},
            )
            assert r2.json()["status"] == "capture_failure"

        async def verify():
            row = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
            assert row["status"] == "capture_failure"
            assert row["input_mode"] == "voice"
            assert row["capture_error"] == "silent_blob"
            items = await db.fetchall(
                "SELECT 1 FROM item_resolutions WHERE log_id = %s", (log_id,)
            )
            assert items == []  # NEVER an empty staged log
            await db.close()

        asyncio.run(verify())
    finally:
        asyncio.run(_cleanup_log(log_id))


def test_voice_near_empty_transcript_is_capture_failure_via_route():
    """§5: an audio log whose transcript comes back (near-)empty returns
    status:"capture_failure" — with transcribe_ms recorded for §9."""
    from fastapi.testclient import TestClient

    from services.transcribe import Transcription

    async def near_silent(audio_bytes, mime):
        assert audio_bytes == b"RIFFfake"
        return Transcription(text=" . ", transcribe_ms=42, provider="deepgram")

    _transcribe["fn"] = near_silent
    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.webm", b"RIFFfake", "audio/webm")},
            )
            assert r.status_code == 200
            assert r.json()["status"] == "capture_failure"
            assert r.json()["capture_error"] == "empty_transcript"

        async def verify():
            row = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
            assert row["status"] == "capture_failure"
            assert row["capture_error"] == "empty_transcript"
            assert row["transcribe_ms"] == 42
            assert row["input_mode"] == "voice"

        asyncio.run(verify())
    finally:
        asyncio.run(_cleanup_log(log_id))


def test_voice_happy_path_stores_transcript_mode_and_timing():
    """§5 multipart happy path: audio → transcript → the identical pipeline,
    with input_mode='voice' and transcribe_ms stored on the log row."""
    from fastapi.testclient import TestClient

    from services.transcribe import Transcription

    phrase = f"novel{uuid4().hex[:8]}"
    transcript = f"one {phrase}"

    async def fake_stt(audio_bytes, mime):
        return Transcription(text=transcript, transcribe_ms=777, provider="deepgram")

    async def fake_decompose(text, ctx):
        assert text == transcript  # the transcript enters the identical path
        return novel_parsed_log(phrase)

    _transcribe["fn"] = fake_stt
    _decompose["fn"] = fake_decompose
    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.webm", b"RIFFdata", "audio/webm")},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["log_id"] == str(log_id)
            assert len(body["items"]) == 1
            assert body["items"][0]["status"] == "searching"

        async def verify():
            row = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
            assert row["input_mode"] == "voice"
            assert row["raw_input"] == transcript
            assert row["transcribe_ms"] == 777
            assert row["status"] == "staged"

        asyncio.run(verify())
    finally:
        asyncio.run(_cleanup_log(log_id))


def test_voice_stt_unavailable_returns_503_not_empty_meal():
    """Both providers down: the client's degrade path is typing — the route
    answers 503, never a fake staged log."""
    from fastapi.testclient import TestClient

    from services.transcribe import TranscriptionError

    async def all_down(audio_bytes, mime):
        raise TranscriptionError("deepgram: HTTP 503")

    _transcribe["fn"] = all_down
    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.webm", b"RIFFdata", "audio/webm")},
            )
            assert r.status_code == 503

        async def verify():
            row = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
            assert row is None  # nothing persisted for a failed capture

        asyncio.run(verify())
    finally:
        asyncio.run(_cleanup_log(log_id))


def test_voice_multipart_missing_audio_field_is_422():
    from fastapi.testclient import TestClient

    with TestClient(_voice_app()) as client:
        # a real multipart body (a stray file forces the content type) with
        # no 'audio' field must 422, not slip into the pipeline
        r = client.post(
            "/api/logs",
            data=_voice_form(uuid4()),
            files={"not_audio": ("x.bin", b"1", "application/octet-stream")},
        )
        assert r.status_code == 422


def test_json_and_multipart_share_post_api_logs_and_one_openapi_operation():
    """§5: the same path takes a JSON body (text) or multipart (voice). The
    JSON path keeps FastAPI's own validation (422 shape) and LogCreate stays
    a named component in /openapi.json — the frontend's generated types
    reference components.schemas.LogCreate (§2 type-gen contract)."""
    from fastapi.testclient import TestClient

    phrase = f"novel{uuid4().hex[:8]}"

    async def fake_decompose(text, ctx):
        return novel_parsed_log(phrase)

    _decompose["fn"] = fake_decompose
    log_id = uuid4()
    app = _voice_app()
    try:
        with TestClient(app) as client:
            # JSON body → the text path (transcribe stub would AssertionError)
            r = client.post(
                "/api/logs",
                json={**_voice_form(log_id), "text": f"one {phrase}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["log_id"] == str(log_id)
            assert len(r.json()["items"]) == 1

            # malformed JSON body → FastAPI's standard 422, not a 400/500
            r = client.post("/api/logs", json={"id": "not-a-uuid"})
            assert r.status_code == 422
            assert r.json()["detail"][0]["loc"][0] == "body"

        async def verify():
            row = await db.fetchone("SELECT input_mode FROM logs WHERE id = %s", (log_id,))
            assert row["input_mode"] == "text"

        asyncio.run(verify())
    finally:
        asyncio.run(_cleanup_log(log_id))

    spec = app.openapi()
    assert "LogCreate" in spec["components"]["schemas"]
    content = spec["paths"]["/api/logs"]["post"]["requestBody"]["content"]
    assert content["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LogCreate"
    }
    assert content["multipart/form-data"]["schema"]["required"] == [
        "audio", "id", "captured_at", "tz",
    ]
    ok = spec["paths"]["/api/logs"]["post"]["responses"]["200"]["content"]
    refs = {s["$ref"] for s in ok["application/json"]["schema"]["anyOf"]}
    assert refs == {
        "#/components/schemas/LogResponse",
        "#/components/schemas/CaptureFailureResponse",
    }
