"""X6 tests — Phase-3 panel minors (DESIGN.md §8.1d, §3.4 guards, §5).

1. is_near_empty is no longer "two alphanumeric characters": a transcript
   needs an alphabetic token of >= 3 letters ("uh", "12", "." are capture
   failures; "egg", "two eggs", "fairlife" are logs).
2. A fallback provider whose key is not configured is a clear
   TranscriptionError (named reasons for every provider), never an SDK crash;
   as primary it fails over. The route maps it to a 503 whose detail says why.
3. POST /api/logs multipart refuses parts off the MIME allowlist (415) and
   over 15 MB (413) before any STT spend.
4. The client's own capture_error rides a capture_failure alongside the
   server reason (response: client_capture_error; column:
   '<server>;client=<client>') instead of being overwritten.
5. routes/resolve.py writes carry the same live-state guard as
   tier3._write_item_row: a late /api/resolve, kick, or answer can never
   resurrect a removed row or clobber a committed one (409 up front; the
   SQL refuses the race).

Route tests hit the live DB through TestClient and self-clean; resolve tests
run inside one force-rollback transaction. Cross-workstream modules
(tracing, services.*) are stubbed in sys.modules; the transcribe stub keeps
the REAL validation helpers and error types.

Run:  uv run --with pytest pytest backend/tests/test_phase3_minors.py -q
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

import db
import services.transcribe as stt  # the REAL module, bound before the stubs go in
from models import ParsedItem, ParsedLog, ResolveRequest, Tier3KickRequest

# ---------------------------------------------------------------------------
# stubs for parallel workstreams (same shape as test_logs_lifecycle.py)
# ---------------------------------------------------------------------------

_transcribe: dict = {"fn": None}
_decompose: dict = {"fn": None}


async def _transcribe_dispatch(audio_bytes, mime):
    fn = _transcribe["fn"]
    if fn is None:
        raise AssertionError("transcribe must not be called by this test")
    return await fn(audio_bytes, mime)


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

    async def never(*a, **kw):
        raise AssertionError("tier 3 must not be reached by these tests")

    t3_mod.run_tier3 = never
    t3_mod.resolve_item_from_food = never
    t3_mod.resolve_item_from_source = never

    stt_mod = types.ModuleType("services.transcribe")
    stt_mod.transcribe = _transcribe_dispatch
    stt_mod.Transcription = stt.Transcription
    stt_mod.TranscriptionError = stt.TranscriptionError
    stt_mod.is_near_empty = stt.is_near_empty

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
def _reset_dispatch():
    _transcribe["fn"] = None
    _decompose["fn"] = None
    yield
    _transcribe["fn"] = None
    _decompose["fn"] = None


# ---------------------------------------------------------------------------
# 1. §8.1d — is_near_empty: an utterance needs a >= 3-letter token
# ---------------------------------------------------------------------------


def test_is_near_empty_requires_a_three_letter_alphabetic_token():
    for text in [None, "", ".", "  …  ", "a", "uh", "um uh", "Uh-uh.", "12", "1 2 3", "ab cd"]:
        assert stt.is_near_empty(text) is True, repr(text)
    # one real word is a real log (§3.2: a whole-utterance alias hit)
    for text in ["egg", "two eggs", "fairlife", "a banana", "Two eggs and a Fairlife.",
                 "uh, two eggs", "1 egg", "Kirkland"]:
        assert stt.is_near_empty(text) is False, repr(text)


# ---------------------------------------------------------------------------
# 2. §8.1 — missing fallback key: clear TranscriptionError, never a crash
# ---------------------------------------------------------------------------


def _env_without(monkeypatch, name: str):
    """Hide one key from _env (it falls back to .env, so delenv is not enough)."""
    real = stt._env

    def fake(n, default=None):
        return None if n == name else real(n, default)

    monkeypatch.setattr(stt, "_env", fake)


def _no_keyterms(monkeypatch):
    async def none():
        return []

    monkeypatch.setattr(stt, "build_keyterms", none)


def test_missing_openai_key_after_deepgram_outage_is_a_clear_transcription_error(monkeypatch):
    """Deepgram 5xx → failover → OPENAI_API_KEY unset. Before: the openai SDK
    raised a bare OpenAIError from its constructor (not a TranscriptionError)
    and the route crashed with a 500. Now: one TranscriptionError naming both
    reasons, in the order the providers were tried."""

    async def deepgram_down(audio_bytes, mime, params):
        return httpx.Response(503, text="down")

    monkeypatch.setattr(stt, "_post_deepgram", deepgram_down)
    _env_without(monkeypatch, "OPENAI_API_KEY")
    _no_keyterms(monkeypatch)
    monkeypatch.delenv("STT_PROVIDER", raising=False)

    with pytest.raises(stt.TranscriptionError) as ei:
        asyncio.run(stt.transcribe(b"RIFFdata", "audio/webm"))
    msg = str(ei.value)
    assert "OPENAI_API_KEY" in msg
    assert "deepgram: HTTP 503" in msg
    assert msg.index("deepgram") < msg.index("openai")
    # the terminal error is the plain kind — nothing left to fail over to
    assert type(ei.value) is stt.TranscriptionError


def test_missing_openai_key_as_primary_fails_over_to_deepgram(monkeypatch):
    """STT_PROVIDER=openai with no key: unavailable, not fatal — Deepgram
    transcribes (§8.1: the swap is one env var, and a half-configured swap
    must degrade, not break voice)."""

    async def deepgram_ok(audio_bytes, mime, params):
        return httpx.Response(
            200,
            json={"results": {"channels": [{"alternatives": [{"transcript": "two eggs"}]}]}},
        )

    monkeypatch.setattr(stt, "_post_deepgram", deepgram_ok)
    _env_without(monkeypatch, "OPENAI_API_KEY")
    _no_keyterms(monkeypatch)
    monkeypatch.setenv("STT_PROVIDER", "openai")

    result = asyncio.run(stt.transcribe(b"RIFFdata", "audio/webm"))
    assert result.provider == "deepgram" and result.text == "two eggs"


# ---------------------------------------------------------------------------
# 3./4. §5 multipart route — upload bound, MIME allowlist, 503 detail, and
# the client's capture_error surviving a capture_failure (live DB, self-clean)
# ---------------------------------------------------------------------------


def _voice_app():
    from fastapi import FastAPI

    import routes.logs as logs_routes
    from auth import require_user

    app = FastAPI()
    app.include_router(logs_routes.router)
    app.dependency_overrides[require_user] = lambda: {"email": "test", "user_id": "t"}
    return app


def _voice_form(log_id, **extra):
    return {
        "id": str(log_id),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "tz": "America/Los_Angeles",
        **extra,
    }


async def _log_row(log_id):
    row = await db.fetchone("SELECT * FROM logs WHERE id = %s", (log_id,))
    await db.close()
    return row


async def _cleanup_log(log_id) -> None:
    await db.execute("DELETE FROM item_resolutions WHERE log_id = %s", (log_id,))
    await db.execute("DELETE FROM logs WHERE id = %s", (log_id,))
    await db.close()


async def _near_silent(audio_bytes, mime):
    return stt.Transcription(text=" . ", transcribe_ms=42, provider="deepgram")


def test_voice_unsupported_mime_is_415_before_any_stt_or_row():
    from fastapi.testclient import TestClient

    import routes.logs as logs_routes

    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            for mime in ("audio/ogg", "application/octet-stream", "text/plain", "video/webm"):
                r = client.post(
                    "/api/logs",
                    data=_voice_form(log_id),
                    files={"audio": ("clip.bin", b"RIFFdata", mime)},
                )
                assert r.status_code == 415, (mime, r.text)
                assert mime in r.json()["detail"]
                for allowed in sorted(logs_routes.AUDIO_MIME_ALLOWLIST):
                    assert allowed in r.json()["detail"]
        # the transcribe stub would have AssertionError'd (500) if reached;
        # and a refused upload leaves no logs row
        assert asyncio.run(_log_row(log_id)) is None
    finally:
        asyncio.run(_cleanup_log(log_id))


def test_voice_mime_parameters_are_ignored_by_the_allowlist():
    """MediaRecorder's real type is 'audio/webm;codecs=opus' — the base type
    is what the allowlist sees."""
    from fastapi.testclient import TestClient

    _transcribe["fn"] = _near_silent
    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.webm", b"RIFFdata", "audio/webm;codecs=opus")},
            )
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "capture_failure"
    finally:
        asyncio.run(_cleanup_log(log_id))


def test_voice_upload_bound_is_15mb_inclusive():
    from fastapi.testclient import TestClient

    import routes.logs as logs_routes

    assert logs_routes.MAX_AUDIO_BYTES == 15 * 1024 * 1024
    _transcribe["fn"] = _near_silent
    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.wav", b"x" * (logs_routes.MAX_AUDIO_BYTES + 1), "audio/wav")},
            )
            assert r.status_code == 413, r.text
            assert asyncio.run(_log_row(log_id)) is None  # refused before any row

            # exactly the bound is accepted and reaches STT (the stub answers)
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.wav", b"x" * logs_routes.MAX_AUDIO_BYTES, "audio/wav")},
            )
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "capture_failure"
    finally:
        asyncio.run(_cleanup_log(log_id))


def test_capture_failure_keeps_the_clients_capture_error_alongside_the_servers():
    """§9 counts client-detected failures AND server-detected empty
    transcripts; a retry that fails server-side must not erase the client's
    earlier reason."""
    from fastapi.testclient import TestClient

    _transcribe["fn"] = _near_silent
    empty_id, silent_id, plain_id = uuid4(), uuid4(), uuid4()
    try:
        with TestClient(_voice_app()) as client:
            # empty transcript after a client-reported prior failure
            r = client.post(
                "/api/logs",
                data=_voice_form(empty_id, capture_error="too_short"),
                files={"audio": ("clip.webm", b"RIFFdata", "audio/webm")},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "capture_failure"
            assert body["capture_error"] == "empty_transcript"  # server reason intact
            assert body["client_capture_error"] == "too_short"  # client reason kept

            # silent blob after a client-reported prior failure
            r = client.post(
                "/api/logs",
                data=_voice_form(silent_id, capture_error="recorder_error"),
                files={"audio": ("clip.webm", b"", "audio/webm")},
            )
            assert r.json()["capture_error"] == "silent_blob"
            assert r.json()["client_capture_error"] == "recorder_error"

            # no client reason: the column keeps its bare value (counters
            # and the existing lifecycle tests read it unchanged)
            r = client.post(
                "/api/logs",
                data=_voice_form(plain_id),
                files={"audio": ("clip.webm", b"RIFFdata", "audio/webm")},
            )
            assert r.json()["capture_error"] == "empty_transcript"
            assert r.json()["client_capture_error"] is None

        assert asyncio.run(_log_row(empty_id))["capture_error"] == "empty_transcript;client=too_short"
        assert asyncio.run(_log_row(silent_id))["capture_error"] == "silent_blob;client=recorder_error"
        assert asyncio.run(_log_row(plain_id))["capture_error"] == "empty_transcript"
    finally:
        for log_id in (empty_id, silent_id, plain_id):
            asyncio.run(_cleanup_log(log_id))


def test_stt_failure_reason_rides_the_503_detail():
    from fastapi.testclient import TestClient

    async def nobody_home(audio_bytes, mime):
        raise stt.TranscriptionError(
            "no transcription provider available: deepgram: HTTP 503; "
            "openai: OPENAI_API_KEY is not set — OpenAI transcription unavailable"
        )

    _transcribe["fn"] = nobody_home
    log_id = uuid4()
    try:
        with TestClient(_voice_app()) as client:
            r = client.post(
                "/api/logs",
                data=_voice_form(log_id),
                files={"audio": ("clip.webm", b"RIFFdata", "audio/webm")},
            )
            assert r.status_code == 503
            detail = r.json()["detail"]
            assert detail.startswith("transcription unavailable")
            assert "OPENAI_API_KEY" in detail and "deepgram: HTTP 503" in detail
        assert asyncio.run(_log_row(log_id)) is None  # nothing persisted
    finally:
        asyncio.run(_cleanup_log(log_id))


# ---------------------------------------------------------------------------
# 5. §3.4 guards on routes/resolve.py — rollback harness
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
        async with conn.transaction():
            yield conn

    db.fetchall, db.fetchone, db.execute, db.tx = fetchall, fetchone, execute, tx
    try:
        yield conn
    finally:
        db.fetchall, db.fetchone, db.execute, db.tx = orig
        await outer.__aexit__(None, None, None)
        await conn.close()


@asynccontextmanager
async def resolve_routes():
    """routes.resolve binds services.tier3 at import time — import it fresh
    under this module's stubs and drop it afterwards."""
    sys.modules.pop("routes.resolve", None)
    import routes.resolve as mod

    try:
        yield mod
    finally:
        sys.modules.pop("routes.resolve", None)


async def insert_log(conn):
    log_id = uuid4()
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at, status) "
        "VALUES (%s, 'text', 'x', now(), 'staged')",
        (log_id,),
    )
    return log_id


async def insert_food(conn, name, state=None):
    serving = {"serving_id": "s100", "description": "100 g", "grams": 100,
               "macros": {"kcal": 165, "protein_g": 31, "carb_g": 0, "fat_g": 3.6}}
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, state, food_type,
                           servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, %s, 'branded', %s, 's100', '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, state, Json([serving])),
    )
    return str((await cur.fetchone())["id"])


async def seed_item(conn, log_id, idx, phrase, *, status, committed=False, staged=None):
    staged = staged or {"item_index": idx, "phrase": phrase, "status": "searching", "meal": "snack"}
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                      staged, committed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (log_id, idx, phrase, phrase, status, Json(staged),
         datetime.now(timezone.utc) if committed else None),
    )
    return staged


async def item_row(conn, log_id, idx):
    cur = await conn.execute(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s",
        (log_id, idx),
    )
    return await cur.fetchone()


def _one_item(phrase):
    async def fake(text, ctx):
        return ParsedLog(
            items=[ParsedItem(phrase=phrase, phrase_kind="food", qty=None, unit=None,
                              state=None, brand=None, library_match=None, meal_ref=None)],
            meal_hint=None, when_hint=None, save_intent=None,
        )

    return fake


def _assert_untouched(row, seeded, *, status):
    assert row["status"] == status
    assert row["staged"] == seeded
    assert row["was_edited"] is False
    assert row["tier"] is None
    assert row["tier3_attempts"] == 0


def test_late_resolve_never_resurrects_a_removed_row():
    """The user removed the row; an edit/resolve for it lands late. Before:
    the upsert's conflict branch set status='searching' unconditionally —
    the row came back from the dead. Now: 409 before any model call, row
    untouched."""

    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                log_id = await insert_log(conn)
                phrase = f"removed {uuid4().hex[:6]}"
                seeded = await seed_item(conn, log_id, 0, phrase, status="removed")
                calls: list[str] = []

                async def spy(text, ctx):
                    calls.append(text)
                    return await _one_item(phrase)(text, ctx)

                _decompose["fn"] = spy
                with pytest.raises(HTTPException) as ei:
                    await rr.resolve_item(
                        ResolveRequest(log_id=log_id, item_index=0, phrase=f"{phrase} edited")
                    )
                assert ei.value.status_code == 409 and "removed" in ei.value.detail
                assert calls == []  # no model spend on a dead row
                _assert_untouched(await item_row(conn, log_id, 0), seeded, status="removed")

    asyncio.run(run())


def test_resolve_sql_guard_refuses_the_race_after_the_precheck():
    """The row dies between the pre-check and the upsert (removal racing a
    resolve): the conflict branch's WHERE refuses the update and the route
    stops with 409 instead of running the cascade against a ghost."""

    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                log_id = await insert_log(conn)
                phrase = f"raced {uuid4().hex[:6]}"
                seeded = await seed_item(conn, log_id, 0, phrase, status="removed")

                async def live_looking(log_id_, idx):  # what the pre-check saw
                    return {"phrase": phrase, "staged": seeded, "status": "searching",
                            "committed_at": None}

                rr._item_row = live_looking
                _decompose["fn"] = _one_item(f"{phrase} edited")
                with pytest.raises(HTTPException) as ei:
                    await rr.resolve_item(
                        ResolveRequest(log_id=log_id, item_index=0, phrase=f"{phrase} edited")
                    )
                assert ei.value.status_code == 409
                row = await item_row(conn, log_id, 0)
                _assert_untouched(row, seeded, status="removed")
                assert row["phrase"] == phrase  # the insert half never fired either

    asyncio.run(run())


def test_committed_row_refuses_a_late_resolve():
    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                log_id = await insert_log(conn)
                phrase = f"done {uuid4().hex[:6]}"
                seeded = {"item_index": 0, "phrase": phrase, "status": "resolved", "meal": "snack"}
                await seed_item(conn, log_id, 0, phrase, status="resolved", committed=True, staged=seeded)
                with pytest.raises(HTTPException) as ei:
                    await rr.resolve_item(
                        ResolveRequest(log_id=log_id, item_index=0, phrase=f"{phrase} edited")
                    )
                assert ei.value.status_code == 409 and "committed" in ei.value.detail
                row = await item_row(conn, log_id, 0)
                assert row["status"] == "resolved" and row["committed_at"] is not None
                assert row["staged"] == seeded and row["was_edited"] is False

    asyncio.run(run())


def test_kick_tier3_refuses_removed_and_committed_rows():
    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                log_id = await insert_log(conn)
                seeded0 = await seed_item(conn, log_id, 0, "gone", status="removed")
                seeded1 = await seed_item(conn, log_id, 1, "done", status="resolved", committed=True)
                for idx, word in ((0, "removed"), (1, "committed")):
                    with pytest.raises(HTTPException) as ei:
                        await rr.kick_tier3(Tier3KickRequest(log_id=log_id, item_index=idx))
                    assert ei.value.status_code == 409 and word in ei.value.detail
                # the tier3 stub would have AssertionError'd if reached; and
                # the attempts counter never moved
                _assert_untouched(await item_row(conn, log_id, 0), seeded0, status="removed")
                row1 = await item_row(conn, log_id, 1)
                assert row1["tier3_attempts"] == 0 and row1["tier3_started_at"] is None

    asyncio.run(run())


def test_answer_refuses_dead_rows_on_every_branch():
    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                log_id = await insert_log(conn)
                seeded0 = await seed_item(conn, log_id, 0, "gone", status="removed")
                seeded1 = await seed_item(conn, log_id, 1, "done", status="resolved", committed=True)
                answers = [
                    rr.Answer(food_id=str(uuid4())),  # cached-food chip
                    rr.Answer(source="fdc", source_id="123", label="x"),  # id-bearing option
                    rr.Answer(text="cooked"),  # §3.4a toggle
                    rr.Answer(text="the grilled one"),  # free-text re-kick
                ]
                for idx in (0, 1):
                    for answer in answers:
                        with pytest.raises(HTTPException) as ei:
                            await rr.answer_question(
                                rr.AnswerRequest(log_id=log_id, item_index=idx, answer=answer)
                            )
                        assert ei.value.status_code == 409, (idx, answer)
                _assert_untouched(await item_row(conn, log_id, 0), seeded0, status="removed")
                row1 = await item_row(conn, log_id, 1)
                assert row1["status"] == "resolved" and row1["staged"] == seeded1
                assert row1["tier3_attempts"] == 0

    asyncio.run(run())


def test_state_toggle_sql_guard_holds_without_the_precheck():
    """_answer_state_toggle called directly (the race past _require_live):
    the matching-state branch is zero-network and writes the answer into the
    staged jsonb — conditionally. A removed row stays exactly as it was."""

    async def run():
        async with isolated_db() as conn:
            async with resolve_routes() as rr:
                log_id = await insert_log(conn)
                fid = await insert_food(conn, "chicken breast cooked", state="cooked")
                staged = {
                    "item_index": 0, "phrase": "chicken", "status": "resolved",
                    "meal": "snack", "food": {"id": fid, "name": "chicken breast cooked"},
                    "state_question": True,
                }
                await seed_item(conn, log_id, 0, "chicken", status="removed", staged=staged)
                item = await rr._answer_state_toggle(log_id, 0, staged, "cooked")
                assert item is not None  # the function answers; the row does not change
                row = await item_row(conn, log_id, 0)
                assert row["status"] == "removed"
                assert row["staged"] == staged
                assert "state_answer" not in row["staged"]

    asyncio.run(run())
