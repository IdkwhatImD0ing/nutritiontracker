"""M2 end-to-end (integrator) — the voice path, live (DESIGN.md §5, §8.1, §12 M2).

In-process TestClient over the assembled app (main.app, all routers wired)
against the LIVE services: Neon (production DB until the dev branch exists),
Deepgram nova-3 (OpenAI gpt-transcribe on failover), GPT-5.6 Luna decompose,
the resolver. Two clips ride POST /api/logs as multipart — §5's "audio blob +
the same fields" — with the auth dependency overridden:

  1. Deepgram's canonical sample ("Life moves pretty fast…", a real recorded
     voice, not food) → a staged log, NOT capture_failure: input_mode='voice',
     transcribe_ms recorded, raw_input = the real transcript, decompose ran.
  2. A synthesized food utterance ("two eggs and a fairlife", OpenAI TTS,
     generated once into the scratch dir) → staged items FROM THE REAL
     TRANSCRIPT: eggs + fairlife as rows, the spoken "two" carried as qty.

The web lane's live spot-check is tests/test_web_lane_live.py (V4); the text
path's regression gate is tests/test_e2e_m1.py.

Nothing commits, so no entries/aliases are written; the logs + item_resolutions
rows this test creates are deleted in the finally block. Searching rows are
never kicked here (tier 3 is the M1 story's job), so no foods are cached.

Run:  uv run --with pytest pytest backend/tests/test_e2e_m2.py -q -s
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_env() -> None:
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402

pytestmark = pytest.mark.live

CAPTURED_AT = "2000-01-05T08:15:00+00:00"  # a dead date: diary noise never lands on a real day
TZ = "America/New_York"

_SCRATCH = Path(os.environ.get("CLAUDE_SCRATCHPAD_DIR") or tempfile.gettempdir())

# 1. Deepgram's sample clip (same cache path as test_transcribe_live.py)
BUELLER_URL = "https://static.deepgram.com/examples/Bueller-Life-moves-pretty-fast.wav"
BUELLER_PATH = _SCRATCH / "Bueller-Life-moves-pretty-fast.wav"
BUELLER_LANDMARK = "life moves pretty fast"

# 2. A synthesized food utterance — OpenAI TTS, generated once, then cached
FOOD_UTTERANCE = "two eggs and a fairlife"
FOOD_CLIP_PATH = _SCRATCH / "two-eggs-fairlife.wav"
TTS_MODEL = "gpt-4o-mini-tts"  # verified live 2026-08-21


def _run(coro):
    return asyncio.run(coro)


def _bueller_bytes() -> bytes:
    if not BUELLER_PATH.is_file():
        resp = httpx.get(BUELLER_URL, follow_redirects=True, timeout=60.0)
        resp.raise_for_status()
        BUELLER_PATH.parent.mkdir(parents=True, exist_ok=True)
        BUELLER_PATH.write_bytes(resp.content)
    return BUELLER_PATH.read_bytes()


def _food_clip_bytes() -> bytes:
    if not FOOD_CLIP_PATH.is_file():
        from openai import OpenAI

        speech = OpenAI().audio.speech.create(
            model=TTS_MODEL, voice="alloy", input=FOOD_UTTERANCE, response_format="wav"
        )
        FOOD_CLIP_PATH.parent.mkdir(parents=True, exist_ok=True)
        FOOD_CLIP_PATH.write_bytes(speech.content)
    return FOOD_CLIP_PATH.read_bytes()


@pytest.fixture()
def client():
    main.app.dependency_overrides[auth.require_user] = lambda: {
        "email": "billzhangsc@gmail.com",
        "sub": "e2e-m2",
    }
    try:
        with TestClient(main.app) as c:
            yield c
    finally:
        main.app.dependency_overrides.pop(auth.require_user, None)


def _post_audio(client: TestClient, log_id: str, wav: bytes, filename: str) -> dict:
    """§5 multipart: the blob under `audio` + id/captured_at/tz form fields."""
    r = client.post(
        "/api/logs",
        files={"audio": (filename, wav, "audio/wav")},
        data={"id": log_id, "captured_at": CAPTURED_AT, "tz": TZ},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _cleanup(log_ids: list[str]) -> None:
    await db.execute(
        "DELETE FROM item_resolutions WHERE log_id = ANY(%s::uuid[])", (log_ids,)
    )
    await db.execute("DELETE FROM logs WHERE id = ANY(%s::uuid[])", (log_ids,))


def _squash(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# the story
# ---------------------------------------------------------------------------


def test_m2_voice_clip_stages_through_the_real_pipeline(client: TestClient):
    l1, l2 = str(uuid4()), str(uuid4())
    try:
        # -- 1. a real recorded voice (Deepgram's sample) -------------------
        body1 = _post_audio(client, l1, _bueller_bytes(), "bueller.wav")
        assert body1.get("status") != "capture_failure", body1
        assert body1["log_id"] == l1 and isinstance(body1["items"], list)

        row1 = _run(db.fetchone("SELECT * FROM logs WHERE id = %s", (l1,)))
        assert row1["input_mode"] == "voice"
        assert row1["status"] == "staged"
        assert row1["transcribe_ms"] and row1["transcribe_ms"] > 0
        assert BUELLER_LANDMARK in row1["raw_input"].lower(), row1["raw_input"]
        # the identical pipeline ran on the transcript: decompose (or the
        # short-circuit) stamped its timing, like any text log
        assert row1["short_circuit"] or row1["decompose_ms"] is not None
        print(
            f"\n[m2 voice] bueller: transcribe_ms={row1['transcribe_ms']} "
            f"decompose_ms={row1['decompose_ms']} items={len(body1['items'])} "
            f"transcript={row1['raw_input'][:60]!r}"
        )

        # -- 2. a food utterance → staged items from the real transcript ---
        body2 = _post_audio(client, l2, _food_clip_bytes(), "eggs.wav")
        assert body2.get("status") != "capture_failure", body2
        row2 = _run(db.fetchone("SELECT * FROM logs WHERE id = %s", (l2,)))
        transcript = row2["raw_input"]
        assert row2["input_mode"] == "voice" and row2["transcribe_ms"] > 0
        assert "egg" in transcript.lower(), transcript
        assert "fairlife" in _squash(transcript), transcript  # keyterm-biased brand

        items = body2["items"]
        assert len(items) >= 2, items
        phrases = [it["phrase"] for it in items]
        eggs = next((it for it in items if "egg" in it["phrase"].lower()), None)
        fairlife = next((it for it in items if "fairlife" in _squash(it["phrase"])), None)
        assert eggs is not None and fairlife is not None, phrases
        # §7.3 rule 1: the spoken "two" rides the eggs row whatever its tier
        assert eggs["qty"] == 2, eggs
        assert all(it["status"] in ("resolved", "searching", "needs_input") for it in items)
        assert all(it["meal"] == body2["meal"] for it in items)

        # every row is persisted server-side (§3: the client is a renderer)
        res_rows = _run(
            db.fetchall(
                "SELECT item_index, phrase, status FROM item_resolutions "
                "WHERE log_id = %s ORDER BY item_index",
                (l2,),
            )
        )
        assert [r["phrase"] for r in res_rows] == phrases
        print(
            f"[m2 voice] eggs+fairlife: transcribe_ms={row2['transcribe_ms']} "
            f"decompose_ms={row2['decompose_ms']} transcript={transcript!r} "
            f"items={[(it['phrase'], it['status'], it.get('tier'), it.get('qty')) for it in items]}"
        )
    finally:
        _run(_cleanup([l1, l2]))
