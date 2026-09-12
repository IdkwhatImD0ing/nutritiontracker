"""Quota / tenancy hardening — the refuter's findings after the open-source
pass, each reproduced against the pre-fix code before the fix landed:

  Q1  two concurrent first attempts were BOTH admitted (count-then-insert);
      now the ledger's partial unique index (002 §5) admits one, insert-first.
  Q2  the UTC day was judged by the app clock while the ledger stamps the
      database's; now now() is the only clock (the day, the count, resets_at).
  Q3  a foreign or stored client id cost the demo its attempt, and the voice
      lane transcribed before finding the stored row (unbounded STT); now the
      claim of the id decides replay/ownership first and spends nothing when
      it refuses.
  Q4  /api/resolve on a foreign item ran a model call, then 500ed; now 404 first.
  D1  a photo placeholder's provenance (log id, capture time, raw vision
      output) was readable by any account holding the id, and a placeholder
      could be saved into a meal; now 404 for anyone but the photographer,
      the internals stripped for them, and saved meals refuse it.
  S1  a demo's tier-3 agent carried the hosted web_search tool (BACKLOG 119).

Same rollback harness as test_tenancy.py (002 applied in-transaction while
the database is pre-002; nothing is left behind).

Run:  uv run --project backend --with pytest pytest backend/tests/test_quota_hardening.py -q
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.types.json import Json

import auth  # noqa: E402
from models import (  # noqa: E402
    LogCreate,
    ResolveRequest,
    StagedItem,
    Tier3KickRequest,
)
from routes import aliases as aliases_routes  # noqa: E402
from routes import commit as commit_routes  # noqa: E402
from routes import entries as entries_routes  # noqa: E402
from routes import foods as foods_routes  # noqa: E402
from routes import logs as logs_routes  # noqa: E402
from routes import meals as meals_routes  # noqa: E402
from routes import photo, quick  # noqa: E402
from routes import resolve as resolve_routes  # noqa: E402
from services import quota, tier3  # noqa: E402
from sources import fdc  # noqa: E402
from test_tenancy import (  # noqa: E402
    JPEG,
    NOW,
    _become,
    _count,
    _scalar,
    _stub_pipeline,
    _upload,
    _user,
    insert_alias,
    insert_entry,
    insert_food,
    insert_item,
    insert_log,
    insert_meal,
    isolated_db,
    staged_item,
)

AUDIO = b"\x1aE\xdf\xa3" + b"\x00" * 64  # any non-empty blob; the STT stub never inspects it
NEXT_UTC_MIDNIGHT_SQL = "SELECT ((now() AT TIME ZONE 'UTC')::date + 1)::timestamp AT TIME ZONE 'UTC'"


def _run(coro):
    return asyncio.run(coro)


def _stub_stt(monkeypatch, text: str | None) -> list[int]:
    """services.transcribe.transcribe replaced by a counter returning `text`
    (None: every provider is down — TranscriptionError)."""
    from services import transcribe as stt_mod

    calls: list[int] = []

    async def fake(audio_bytes, mime):
        calls.append(len(audio_bytes))
        if text is None:
            raise stt_mod.TranscriptionError("deepgram: down; openai: down")
        return stt_mod.Transcription(text=text, transcribe_ms=1, provider="deepgram")

    monkeypatch.setattr(stt_mod, "transcribe", fake)
    return calls


async def _log_row(conn, log_id) -> dict | None:
    return await _scalar(conn, "SELECT row_to_json(l) FROM logs l WHERE id = %s", (log_id,))


# ---------------------------------------------------------------------------
# Q1. two first attempts in flight at once: one admitted, by the index
# ---------------------------------------------------------------------------


def test_q1_concurrent_first_attempts_admit_exactly_one():
    async def run():
        async with isolated_db() as conn:
            await _become(conn, "hard-demo-1")
            # the refuter's probe (pre-fix: admitted=2, two ledger rows)
            results = await asyncio.gather(
                quota.admit_log(conn), quota.admit_log(conn), return_exceptions=True
            )
            refused = [r for r in results if isinstance(r, quota.QuotaExceeded)]
            assert len(refused) == 1 and [r for r in results if r is None] == [None], results
            assert refused[0].status_code == 429 and refused[0].used == 1 and refused[0].limit == 1
            assert await _count(conn, "quota_events") == 1

            # it is the database that refuses, whatever the app does: a second
            # 'log' row on the same UTC day violates 002's partial unique index
            with pytest.raises(psycopg.errors.UniqueViolation):
                async with conn.transaction():
                    await conn.execute("INSERT INTO quota_events (kind) VALUES ('log')")
            indexdef = await _scalar(
                conn, "SELECT indexdef FROM pg_indexes WHERE indexname = 'quota_events_log_day_key'"
            )
            assert indexdef is not None and indexdef.startswith("CREATE UNIQUE INDEX")
            assert "WHERE (kind = 'log'::text)" in indexdef
            # 'resolve' rows are capped by count, not one-a-day
            for _ in range(2):
                await conn.execute("INSERT INTO quota_events (kind) VALUES ('resolve')")
            # yesterday's attempt is yesterday's: no collision, a fresh admit
            await conn.execute(
                "UPDATE quota_events SET created_at = created_at - interval '1 day' WHERE kind = 'log'"
            )
            await quota.check_log()
            assert await _count(conn, "quota_events") == 4

            # the owner: unrecorded and unlimited through the same calls
            await _become(conn, "hard-owner-1", is_owner=True)
            for _ in range(3):
                await quota.check_log()
            await quota.admit_log(conn)
            assert await _count(conn, "quota_events") == 0

    _run(run())


# ---------------------------------------------------------------------------
# Q2. one clock — the database's — for the ledger, the count and resets_at
# ---------------------------------------------------------------------------


def test_q2_the_database_clock_is_the_only_clock(monkeypatch):
    """An app clock two days ahead changes nothing (pre-fix: it admitted a
    second attempt and reported a reset three days out)."""
    real_now = datetime.now

    class Skewed(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now(tz) + timedelta(days=2)

    monkeypatch.setattr(quota, "datetime", Skewed, raising=False)

    async def run():
        async with isolated_db() as conn:
            demo = await _become(conn, "hard-demo-2")
            await quota.check_log()
            with pytest.raises(quota.QuotaExceeded) as ei:
                await quota.check_log()
            exc = ei.value
            next_midnight = await _scalar(conn, NEXT_UTC_MIDNIGHT_SQL)
            assert exc.used == 1
            assert datetime.fromisoformat(exc.resets_at) == next_midnight
            assert exc.resets_at.endswith("T00:00:00+00:00")
            assert exc.detail == f"Demo accounts can log once a day — resets at {exc.resets_at}"
            assert await quota.demo_status(demo) == {
                "limit": 1, "used_today": 1, "resets_at": exc.resets_at,
            }
            # the boundary is the database's UTC midnight: a row stamped one
            # second before it belongs to yesterday — not counted, not colliding
            await conn.execute(
                "UPDATE quota_events SET created_at = "
                "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' - interval '1 second'"
            )
            assert (await quota.demo_status(demo))["used_today"] == 0
            await quota.check_log()
            assert (await quota.demo_status(demo))["used_today"] == 1

            # the re-resolution cap reads the same clock
            for _ in range(quota.DEMO_RESOLVE_LIMIT):
                await quota.check_resolve()
            with pytest.raises(quota.QuotaExceeded) as ei:
                await quota.check_resolve()
            assert ei.value.used == quota.DEMO_RESOLVE_LIMIT
            assert datetime.fromisoformat(ei.value.resets_at) == next_midnight
            await conn.execute(
                "UPDATE quota_events SET created_at = created_at - interval '1 day' WHERE kind = 'resolve'"
            )
            await quota.check_resolve()

    _run(run())


# ---------------------------------------------------------------------------
# Q3. the claim of the client id decides replay / ownership before any spend
# ---------------------------------------------------------------------------


def test_q3_foreign_and_stored_ids_are_answered_before_any_spend(monkeypatch):
    runs: list[str] = []  # each utterance the chat agent is handed
    _stub_pipeline(monkeypatch, runs=runs)  # vision refuses outright
    calls = _stub_stt(monkeypatch, "two eggs")

    async def run():
        async with isolated_db() as conn:
            await _become(conn, "hard-owner-3", is_owner=True)
            food = await insert_food(conn)
            theirs = await insert_log(conn)
            await insert_item(conn, theirs, food, staged=staged_item(food))

            demo = await _become(conn, "hard-demo-3")
            # every creating lane answers a foreign id 404 — no ledger row,
            # no logs row, no STT, no vision (pre-fix: 409 after the attempt)
            with pytest.raises(HTTPException) as ei:
                await logs_routes.create_log_endpoint(
                    LogCreate(id=theirs, text="two eggs", captured_at=NOW, tz="UTC")
                )
            assert ei.value.status_code == 404
            with pytest.raises(HTTPException) as ei:
                await logs_routes.create_log_voice(
                    audio=_upload(AUDIO, "audio/webm"), log_id=theirs, captured_at=NOW, tz="UTC"
                )
            assert ei.value.status_code == 404
            with pytest.raises(HTTPException) as ei:
                await photo.create_log_photo(
                    image=_upload(JPEG, "image/jpeg"), log_id=theirs, captured_at=NOW
                )
            assert ei.value.status_code == 404
            with pytest.raises(HTTPException) as ei:
                await quick.create_log_from_food(
                    quick.FromFoodRequest(
                        id=theirs, food_id=UUID(food), input_mode="search", captured_at=NOW
                    )
                )
            assert ei.value.status_code == 404
            # a from-food pick of an unknown food: refused before the claim
            with pytest.raises(HTTPException) as ei:
                await quick.create_log_from_food(
                    quick.FromFoodRequest(
                        id=uuid4(), food_id=uuid4(), input_mode="search", captured_at=NOW
                    )
                )
            assert ei.value.status_code == 404
            assert calls == [] and runs == []
            assert (await quota.demo_status(demo))["used_today"] == 0
            assert await _count(conn, "logs") == 0

            # the day's attempt: an empty blob is a capture failure, recorded
            # at the claim; the row carries the outcome
            failed = uuid4()
            out = await logs_routes.create_log_voice(
                audio=_upload(b"", "audio/webm"), log_id=failed, captured_at=NOW, tz="UTC",
                capture_error="recorder_error",
            )
            assert out.status == "capture_failure" and out.capture_error == "silent_blob"
            assert out.client_capture_error == "recorder_error" and calls == []
            assert (await quota.demo_status(demo))["used_today"] == 1
            row = await _log_row(conn, failed)
            assert row["status"] == "capture_failure" and row["input_mode"] == "voice"
            assert row["capture_error"] == "silent_blob;client=recorder_error"
            # the same id with audio: answered from the stored row before STT,
            # every time (pre-fix: one paid transcription per replay) — as the
            # §5 idempotent replay, the same capture_failure body (the client
            # keeps the id only while an upload's response was lost), never
            # the 409 the other lanes give a log that left 'staged'
            for _ in range(3):
                again = await logs_routes.create_log_voice(
                    audio=_upload(AUDIO, "audio/webm"), log_id=failed, captured_at=NOW, tz="UTC"
                )
                assert again.status == "capture_failure" and again.capture_error == "silent_blob"
                assert again.client_capture_error == "recorder_error"
            assert calls == []
            assert (await quota.demo_status(demo))["used_today"] == 1
            with pytest.raises(HTTPException) as ei:  # the text lane: 409, as before
                await logs_routes.create_log_endpoint(
                    LogCreate(id=failed, text="two eggs", captured_at=NOW, tz="UTC")
                )
            assert ei.value.status_code == 409
            # a staged log with live items: the stored staging state, free
            staged = await insert_log(conn)
            await insert_item(conn, staged, food, staged=staged_item(food))
            replay = await logs_routes.create_log_voice(
                audio=_upload(AUDIO, "audio/webm"), log_id=staged, captured_at=NOW, tz="UTC"
            )
            assert replay.log_id == staged and len(replay.items) == 1 and calls == []
            # a new id: the quota refuses before STT and leaves no row behind
            fresh = uuid4()
            with pytest.raises(quota.QuotaExceeded):
                await logs_routes.create_log_voice(
                    audio=_upload(AUDIO, "audio/webm"), log_id=fresh, captured_at=NOW, tz="UTC"
                )
            assert calls == [] and await _log_row(conn, fresh) is None
            assert (await quota.demo_status(demo))["used_today"] == 1

            # an admitted attempt runs the lane once: STT, then the pipeline,
            # and the row carries the transcript
            demo_b = await _become(conn, "hard-demo-3b")
            new = uuid4()
            out = await logs_routes.create_log_voice(
                audio=_upload(AUDIO, "audio/webm"), log_id=new, captured_at=NOW, tz="UTC"
            )
            assert out.log_id == new and out.items == []
            assert calls == [len(AUDIO)] and runs == ["two eggs"]
            row = await _log_row(conn, new)
            assert row["raw_input"] == "two eggs" and row["transcribe_ms"] == 1
            assert row["status"] == "staged" and row["input_mode"] == "voice"
            assert (await quota.demo_status(demo_b))["used_today"] == 1

            # STT down on an admitted attempt: 503, the itemless row is dropped
            # so a retry runs STT again — and the attempt stands (the photo
            # lane's rule for a vision failure)
            demo_c = await _become(conn, "hard-demo-3c")
            _stub_stt(monkeypatch, None)
            down = uuid4()
            with pytest.raises(HTTPException) as ei:
                await logs_routes.create_log_voice(
                    audio=_upload(AUDIO, "audio/webm"), log_id=down, captured_at=NOW, tz="UTC"
                )
            assert ei.value.status_code == 503
            assert await _log_row(conn, down) is None
            assert (await quota.demo_status(demo_c))["used_today"] == 1
            with pytest.raises(quota.QuotaExceeded):
                await logs_routes.create_log_voice(
                    audio=_upload(AUDIO, "audio/webm"), log_id=down, captured_at=NOW, tz="UTC"
                )

    _run(run())


# ---------------------------------------------------------------------------
# Q4. /api/resolve (and the kick, the free-text answer) on a foreign item
# ---------------------------------------------------------------------------


def test_q4_resolve_on_a_foreign_item_is_404_before_any_model_call(monkeypatch):
    calls: list[tuple] = []

    async def run_tier3(log_id, item_index, **kw):  # the one model call /api/resolve makes
        calls.append((log_id, item_index))
        raise AssertionError("no model call expected here")

    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(tier3, "run_tier3", run_tier3)

    async def run():
        async with isolated_db() as conn:
            await _become(conn, "hard-owner-4", is_owner=True)
            food = await insert_food(conn)
            theirs = await insert_log(conn)
            await insert_item(conn, theirs, food, status="resolved")
            before = await _scalar(
                conn, "SELECT row_to_json(i) FROM item_resolutions i WHERE log_id = %s", (theirs,)
            )

            await _become(conn, "hard-demo-4")
            # the owner's row, a fresh index under the owner's log, an unknown
            # log: 404 alike (pre-fix: a model call, then a psycopg error)
            for log_id, idx in ((theirs, 0), (theirs, 7), (uuid4(), 0)):
                with pytest.raises(HTTPException) as ei:
                    await resolve_routes.resolve_item(
                        ResolveRequest(log_id=log_id, item_index=idx, phrase="a large pizza")
                    )
                assert ei.value.status_code == 404, (log_id, idx)
            with pytest.raises(HTTPException) as ei:
                await resolve_routes.kick_tier3(Tier3KickRequest(log_id=theirs, item_index=0))
            assert ei.value.status_code == 404
            with pytest.raises(HTTPException) as ei:
                await resolve_routes.answer_question(
                    resolve_routes.AnswerRequest(
                        log_id=theirs, item_index=0, answer=resolve_routes.Answer(text="the blue one")
                    )
                )
            assert ei.value.status_code == 404
            assert calls == []  # no model call
            assert await _count(conn, "quota_events") == 0  # no unit spent on a refusal
            assert await _count(conn, "item_resolutions") == 0

            # the demo's own log: a new index resolves (an exact alias — tier
            # 1, no model), and that is the unit
            await insert_alias(conn, food)
            mine = await insert_log(conn)
            item = await resolve_routes.resolve_item(
                ResolveRequest(log_id=mine, item_index=3, phrase="two eggs")
            )
            assert item.status == "resolved" and item.tier == 1 and calls == []
            assert await _count(conn, "quota_events") == 1

            await _become(conn, "hard-owner-4", is_owner=True)
            after = await _scalar(
                conn, "SELECT row_to_json(i) FROM item_resolutions i WHERE log_id = %s", (theirs,)
            )
            assert after == before, "the owner's row changed"

    _run(run())


# ---------------------------------------------------------------------------
# D1. a photo placeholder is the photographer's alone, and never a meal item
# ---------------------------------------------------------------------------


def test_d1_photo_placeholders_belong_to_the_photographer():
    vision = {
        "phrase": "grilled chicken thigh", "note": "grilled chicken thigh over rice, dinner plate",
        "qty": None, "unit": None, "est_grams": 180, "confidence": "medium",
        "model": "gpt-6-astra",
    }

    async def run():
        async with isolated_db() as conn:
            await _become(conn, "hard-owner-5", is_owner=True)
            log_id = uuid4()
            await conn.execute(
                "INSERT INTO logs (id, input_mode, raw_input, captured_at) "
                "VALUES (%s, 'photo', 'photo: grilled chicken thigh', %s)",
                (log_id, NOW),
            )
            await conn.execute(
                """
                INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                              staged, tier)
                VALUES (%s, 0, 'grilled chicken thigh', 'grilled chicken thigh', 'not_found',
                        %s, 3)
                """,
                (log_id, Json({"phrase": "grilled chicken thigh", "meal": "dinner",
                               "status": "not_found", "item_index": 0, "vision": vision})),
            )
            item = await photo.stage_estimate_food(str(log_id), 0)
            assert item is not None and item.food is not None
            placeholder = item.food.id
            real = await insert_food(conn)

            # the photographer sees the detail view — without the row's internals
            detail = await foods_routes.get_food_detail(UUID(placeholder))
            assert detail.source == "estimate" and detail.badge.label == "Photo estimate"
            # (source_id keeps the row's cache identity "<log>:<index>" — the
            # photographer's own log; the pointers and the capture time go)
            assert set(detail.provenance) == {"vision", "model"}
            assert detail.provenance["vision"]["note"] == vision["note"]
            assert detail.payload is None
            assert NOW.isoformat() not in detail.model_dump_json()

            # anyone else: the id resolves to nothing (pre-fix: the log id, the
            # capture time and the vision output, to any account holding the id)
            await _become(conn, "hard-demo-5")
            with pytest.raises(HTTPException) as ei:
                await foods_routes.get_food_detail(UUID(placeholder))
            assert ei.value.status_code == 404
            assert (await foods_routes.get_food_detail(UUID(real))).id == real  # the cache stays shared

            # a placeholder is never a saved-meal item, for anyone: item 58's
            # broken-meal guard, not a food
            for who, owner in (("hard-demo-5", False), ("hard-owner-5", True)):
                await _become(conn, who, is_owner=owner)
                with pytest.raises(HTTPException) as ei:
                    await meals_routes.create_meal(
                        meals_routes.MealCreate(
                            name=f"plate {uuid4().hex[:6]}",
                            items=[meals_routes.MealItemIn(food_id=placeholder, serving_id="photo")],
                        )
                    )
                assert ei.value.status_code == 422 and "placeholder" in ei.value.detail
            meal = await meals_routes.create_meal(
                meals_routes.MealCreate(
                    name=f"eggs {uuid4().hex[:6]}",
                    items=[meals_routes.MealItemIn(food_id=real, serving_id="s")],
                )
            )
            with pytest.raises(HTTPException) as ei:
                await meals_routes.patch_meal(
                    meal.id,
                    meals_routes.MealPatch(
                        items=[meals_routes.MealItemIn(food_id=placeholder, serving_id="photo")]
                    ),
                )
            assert ei.value.status_code == 422 and "placeholder" in ei.value.detail
            assert await _scalar(
                conn,
                "SELECT count(*) FROM meal_macros WHERE items @> %s::jsonb",
                (Json([{"food_id": placeholder}]),),
            ) == 0

    _run(run())


# ---------------------------------------------------------------------------
# S1. a demo's tier-3 agent carries no web tools
# ---------------------------------------------------------------------------


def test_s1_demo_tier3_agent_is_fdc_only():
    def build():
        agent = tier3._build_agent({"network_calls": 0}, {}, {}, {})
        return {t.name for t in agent.tools}, agent

    fdc_only = {"search_generic", "search_branded", "get_food", "get_food_history", "add_food"}
    web = {"web_search", "pin_chain_domain", "fetch_page"}

    auth.current_user.set(_user("hard-demo-6"))
    names, agent = build()
    assert names == fdc_only
    assert agent.model_settings.response_include is None
    assert "not available" in agent.instructions  # the model is told, so rule 2a ends in not_found

    auth.current_user.set(_user("hard-owner-6", is_owner=True))
    names, agent = build()
    assert names == fdc_only | web
    assert agent.model_settings.response_include == ["web_search_call.action.sources"]
    assert agent.instructions == tier3.INSTRUCTIONS


# ---------------------------------------------------------------------------
# Baseline: every id-taking route answers another tenant's id as if it did not
# exist — 404, or the 409 the same code path gives an unknown id — spends
# nothing and mutates nothing. The refuter's evidence behind "no bypass
# found", kept as a regression (a same-id POST was the one 409 that leaked
# existence; it is 404 now).
# ---------------------------------------------------------------------------


def test_baseline_foreign_ids_are_invisible(monkeypatch):
    _stub_pipeline(monkeypatch)

    async def refuse(*args, **kwargs):
        raise AssertionError("no network call expected here")

    monkeypatch.setattr(fdc, "get_food", refuse)

    async def run():
        async with isolated_db() as conn:
            await _become(conn, "hard-owner-7", is_owner=True)
            food = await insert_food(conn)
            log = await insert_log(conn)
            await insert_item(conn, log, food, status="resolved")
            committed = await insert_log(conn, status="committed")
            await insert_item(conn, committed, food, status="resolved")
            await insert_entry(conn, committed, food)
            entry_id = await _scalar(conn, "SELECT id FROM entries WHERE log_id = %s", (committed,))
            await insert_alias(conn, food)
            alias_id = await _scalar(conn, "SELECT id FROM aliases WHERE food_id = %s", (food,))
            await insert_meal(conn, food)
            meal_id = await _scalar(conn, "SELECT id FROM meal_macros LIMIT 1")
            digest = "SELECT md5(string_agg(row_to_json(t)::text, ',' ORDER BY id)) FROM {} t"
            tables = ("logs", "item_resolutions", "entries", "aliases", "meal_macros")
            snapshot = {t: await _scalar(conn, digest.format(t)) for t in tables}

            await _become(conn, "hard-demo-7")
            item = StagedItem.model_validate(staged_item(food))
            expect: dict[str, int] = {}

            async def status_of(name, coro):
                try:
                    await coro
                except HTTPException as e:
                    expect[name] = e.status_code
                else:
                    expect[name] = 200

            await status_of("get_log", logs_routes.get_log(log))
            await status_of("discard_log", logs_routes.discard_log(log))
            await status_of("remove_item", logs_routes.remove_staged_item(log, 0))
            await status_of(
                "timings", logs_routes.patch_timings(log, logs_routes.TimingsPatch(staged_ms=1))
            )
            await status_of(
                "commit", commit_routes.commit_log(log, commit_routes.CommitRequestIn(items=[]))
            )
            await status_of(
                "commit_item",
                commit_routes.commit_item(
                    log, commit_routes.CommitItemRequest(item_index=0, item=item)
                ),
            )
            await status_of(
                "answer_food",
                resolve_routes.answer_question(
                    resolve_routes.AnswerRequest(
                        log_id=log, item_index=0, answer=resolve_routes.Answer(food_id=food)
                    )
                ),
            )
            await status_of(
                "select_food_ctx",
                foods_routes.select_food(
                    foods_routes.SelectRequest(
                        source="fdc", source_id="1", log_id=log, item_index=0
                    )
                ),
            )
            await status_of(
                "patch_entry",
                entries_routes.patch_entry(entry_id, entries_routes.EntryPatch(qty=3)),
            )
            await status_of("delete_entry", entries_routes.delete_entry(entry_id))
            await status_of(
                "patch_alias",
                aliases_routes.patch_alias(alias_id, aliases_routes.AliasPatch(pinned=True)),
            )
            await status_of("delete_alias", aliases_routes.delete_alias(alias_id))
            await status_of(
                "patch_meal", meals_routes.patch_meal(meal_id, meals_routes.MealPatch(name="x"))
            )
            await status_of("delete_meal", meals_routes.delete_meal(meal_id))
            await status_of(
                "meal_from_log",
                meals_routes.meal_from_log(meals_routes.MealFromLog(log_id=committed, name="x")),
            )
            # a same-id POST collides on the global uuid; the invisible row
            # reads as no log at all (pre-fix: 409, an existence oracle)
            await status_of(
                "post_same_id",
                logs_routes.create_log_endpoint(
                    LogCreate(id=log, text="two eggs", captured_at=NOW, tz="UTC")
                ),
            )

            assert all(code in (404, 409) for code in expect.values()), expect
            assert expect["post_same_id"] == 404
            assert await _count(conn, "quota_events") == 0  # nothing spent on a refusal

            await _become(conn, "hard-owner-7", is_owner=True)
            for t, h in snapshot.items():
                assert await _scalar(conn, digest.format(t)) == h, t

    _run(run())
