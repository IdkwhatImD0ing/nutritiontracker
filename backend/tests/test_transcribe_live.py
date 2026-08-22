"""V1 tests — the Transcriber interface (DESIGN.md §8.1, §2).

Live tests transcribe the canonical Deepgram sample clip ("Life moves pretty
fast…") — downloaded once into a scratch dir and cached — through the real
providers: Deepgram nova-3 primary, and the OpenAI gpt-transcribe fallback via
a forced-5xx failover. Keyterm construction reads the live Neon library
(read-only). Pure tests cover the keyterm caps, near-empty detection, and the
failover state machine with the wire stubbed.

Run: uv run --with pytest pytest backend/tests/test_transcribe_live.py -q -rP
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import pytest

import services.transcribe as stt  # noqa: E402

CLIP_URL = "https://static.deepgram.com/examples/Bueller-Life-moves-pretty-fast.wav"
CLIP_LANDMARK = "life moves pretty fast"

_SCRATCH = Path(os.environ.get("CLAUDE_SCRATCHPAD_DIR") or tempfile.gettempdir())
_CLIP_PATH = _SCRATCH / "Bueller-Life-moves-pretty-fast.wav"


def _clip_bytes() -> bytes:
    """Download the sample once; every later run reads the cached file."""
    if not _CLIP_PATH.is_file():
        resp = httpx.get(CLIP_URL, follow_redirects=True, timeout=60.0)
        resp.raise_for_status()
        _CLIP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CLIP_PATH.write_bytes(resp.content)
    return _CLIP_PATH.read_bytes()


# ---------------------------------------------------------------------------
# live: Deepgram nova-3 primary on a real clip (§8.1 commitments a–c)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_deepgram_live_real_clip():
    wav = _clip_bytes()
    result = asyncio.run(stt.transcribe(wav, "audio/wav"))
    assert result.provider == "deepgram"
    assert result.transcribe_ms > 0
    assert CLIP_LANDMARK in result.text.lower()
    print(
        f"\ndeepgram nova-3: {result.transcribe_ms}ms — {result.text[:80]!r}"
    )


@pytest.mark.live
def test_failover_live_deepgram_down_openai_transcribes(monkeypatch):
    """Forced Deepgram 5xx → the real OpenAI gpt-transcribe fallback runs the
    same clip end-to-end (§8.1: swap/failover must not touch anything
    downstream)."""

    async def deepgram_down(audio_bytes, mime, params):
        return httpx.Response(500, text="forced outage")

    monkeypatch.setattr(stt, "_post_deepgram", deepgram_down)
    wav = _clip_bytes()
    result = asyncio.run(stt.transcribe(wav, "audio/wav"))
    assert result.provider == "openai"
    assert CLIP_LANDMARK in result.text.lower()
    print(
        f"\nopenai {stt.OPENAI_STT_MODEL}: {result.transcribe_ms}ms — {result.text[:80]!r}"
    )


@pytest.mark.live
def test_deepgram_live_keyterms_on_the_wire(monkeypatch):
    """§8.1b: the vocabulary rides as repeated ?keyterm= params and Deepgram
    accepts them (a wrong param name would 400 — the 4xx path never fails
    over, so this is the only place the wire format is proven)."""
    sent: dict = {}
    real_post = stt._post_deepgram

    async def spy(audio_bytes, mime, params):
        sent["params"] = params
        resp = await real_post(audio_bytes, mime, params)
        sent["status"] = resp.status_code
        return resp

    async def vocab():
        return ["Bueller", "Fairlife", "Kirkland Signature"]

    monkeypatch.setattr(stt, "_post_deepgram", spy)
    monkeypatch.setattr(stt, "build_keyterms", vocab)
    monkeypatch.delenv("STT_PROVIDER", raising=False)

    result = asyncio.run(stt.transcribe(_clip_bytes(), "audio/wav"))
    assert sent["status"] == 200
    assert [v for k, v in sent["params"] if k == "keyterm"] == [
        "Bueller", "Fairlife", "Kirkland Signature",
    ]
    assert ("model", stt.DEEPGRAM_MODEL) in sent["params"]
    assert ("smart_format", "true") in sent["params"]
    assert result.provider == "deepgram"
    assert CLIP_LANDMARK in result.text.lower()


@pytest.mark.live
def test_build_keyterms_live_respects_caps():
    """Keyterms come from the real library (read-only) and always fit the
    §8.1 budget."""
    terms = asyncio.run(stt.build_keyterms())
    assert isinstance(terms, list)
    assert len(terms) <= stt.KEYTERM_MAX_TERMS
    assert sum(stt._est_tokens(t) for t in terms) <= stt.KEYTERM_TOKEN_BUDGET
    assert all(t.strip() for t in terms)
    print(f"\nkeyterms ({len(terms)}): {terms[:10]}")


@pytest.mark.live
def test_build_keyterms_db_brands_first_then_aliases_by_use():
    """§8.1b against the real schema, inside a rolled-back transaction: the
    brand of a cached food comes first, then alias phrases by use_count.
    build_keyterms degrades to [] on any DB error, so this is also the test
    that proves its SQL actually runs."""
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json

    import db

    tag = uuid.uuid4().hex[:8]
    brand, alias_hi, alias_lo = f"kt brand {tag}", f"kt alias hi {tag}", f"kt alias lo {tag}"

    async def run():
        conn = await psycopg.AsyncConnection.connect(
            db._database_url(), row_factory=dict_row
        )
        outer = conn.transaction(force_rollback=True)
        await outer.__aenter__()
        orig = db.fetchall

        async def fetchall(sql, params=()):
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

        db.fetchall = fetchall
        try:
            serving = {"serving_id": "s", "description": "1 bar", "grams": 40,
                       "macros": {"kcal": 1, "protein_g": 1, "carb_g": 1, "fat_g": 1}}
            cur = await conn.execute(
                """
                INSERT INTO foods (source, source_id, name, display_name, brand,
                                   food_type, servings, default_serving_id, payload)
                VALUES ('fdc', %s, 'kt food', 'kt food', %s, 'branded', %s, 's', '{}')
                RETURNING id
                """,
                (f"kt-{tag}", brand, Json([serving])),
            )
            food_id = (await cur.fetchone())["id"]
            for alias, use_count in ((alias_lo, 1), (alias_hi, 5)):
                await conn.execute(
                    "INSERT INTO aliases (alias_norm, food_id, use_count, origin) "
                    "VALUES (%s, %s, %s, 'tier3')",
                    (alias, food_id, use_count),
                )
            return await stt.build_keyterms()
        finally:
            db.fetchall = orig
            await outer.__aexit__(None, None, None)
            await conn.close()

    terms = asyncio.run(run())
    assert brand in terms and alias_hi in terms and alias_lo in terms
    # brands precede aliases; within aliases, higher use_count first
    assert terms.index(brand) < terms.index(alias_hi) < terms.index(alias_lo)
    assert len(terms) <= stt.KEYTERM_MAX_TERMS


# ---------------------------------------------------------------------------
# pure: keyterm capping (§8.1 — 500-token Deepgram cap, stayed well under)
# ---------------------------------------------------------------------------


def test_cap_keyterms_respects_the_caps():
    capped = stt.cap_keyterms([f"brand number {i}" for i in range(500)])
    assert len(capped) <= stt.KEYTERM_MAX_TERMS
    assert sum(stt._est_tokens(t) for t in capped) <= stt.KEYTERM_TOKEN_BUDGET
    # the budget itself sits well under Deepgram's 500-token hard cap
    assert stt.KEYTERM_TOKEN_BUDGET <= 300 < 500

    # token budget binds even when the term count doesn't
    long_terms = [f"very long descriptive brand name number {i} deluxe" for i in range(60)]
    capped = stt.cap_keyterms(long_terms)
    assert sum(stt._est_tokens(t) for t in capped) <= stt.KEYTERM_TOKEN_BUDGET
    assert len(capped) < 60

    # dedupe is case-insensitive, order is preserved, an oversized term is
    # skipped without ending collection, whitespace is normalized
    assert stt.cap_keyterms(["Fairlife", "fairlife", "x" * 5000, "  two   eggs "]) == [
        "Fairlife",
        "two eggs",
    ]
    assert stt.cap_keyterms([]) == []


# ---------------------------------------------------------------------------
# pure: transcript validation (§8.1d)
# ---------------------------------------------------------------------------


def test_is_near_empty_table():
    for text, expected in [
        (None, True),
        ("", True),
        (".", True),
        ("  …  ", True),
        ("a", True),  # one alphanumeric char is not an utterance
        ("uh", True),  # §8.1d: no alphabetic token of >= 3 letters → capture failure
        ("two eggs", False),
        ("egg", False),
    ]:
        assert stt.is_near_empty(text) is expected, repr(text)


# ---------------------------------------------------------------------------
# failover state machine, wire stubbed (§8.1: 5xx/timeout → other provider)
# ---------------------------------------------------------------------------


def _no_keyterms(monkeypatch):
    async def none():
        return []

    monkeypatch.setattr(stt, "build_keyterms", none)


def test_failover_deepgram_5xx_calls_openai(monkeypatch):
    calls: list = []

    async def fake_post(audio_bytes, mime, params):
        calls.append("deepgram")
        return httpx.Response(500, text="upstream sad")

    async def fake_openai(audio_bytes, mime, keyterms):
        calls.append(("openai", keyterms))
        return "fallback text"

    async def fake_keyterms():
        return ["Fairlife"]

    monkeypatch.setattr(stt, "_post_deepgram", fake_post)
    monkeypatch.setattr(stt, "_transcribe_openai", fake_openai)
    monkeypatch.setattr(stt, "build_keyterms", fake_keyterms)
    monkeypatch.delenv("STT_PROVIDER", raising=False)

    result = asyncio.run(stt.transcribe(b"xx", "audio/webm"))
    assert result.provider == "openai"
    assert result.text == "fallback text"
    assert result.transcribe_ms >= 0
    # deepgram was tried first, and the fallback got the same keyterm vocab
    assert calls == ["deepgram", ("openai", ["Fairlife"])]


def test_failover_deepgram_timeout_calls_openai(monkeypatch):
    async def fake_post(audio_bytes, mime, params):
        raise httpx.ConnectTimeout("boom")

    async def fake_openai(audio_bytes, mime, keyterms):
        return "made it"

    monkeypatch.setattr(stt, "_post_deepgram", fake_post)
    monkeypatch.setattr(stt, "_transcribe_openai", fake_openai)
    _no_keyterms(monkeypatch)
    monkeypatch.delenv("STT_PROVIDER", raising=False)

    result = asyncio.run(stt.transcribe(b"xx", "audio/webm"))
    assert result.provider == "openai" and result.text == "made it"


def test_deepgram_4xx_is_hard_error_no_failover(monkeypatch):
    """A 4xx means the request itself is bad — retrying it on another vendor
    would just spend money on the same failure."""
    openai_calls = []

    async def fake_post(audio_bytes, mime, params):
        return httpx.Response(400, text="corrupt container")

    async def fake_openai(audio_bytes, mime, keyterms):
        openai_calls.append(1)
        return "never"

    monkeypatch.setattr(stt, "_post_deepgram", fake_post)
    monkeypatch.setattr(stt, "_transcribe_openai", fake_openai)
    _no_keyterms(monkeypatch)
    monkeypatch.delenv("STT_PROVIDER", raising=False)

    with pytest.raises(stt.TranscriptionError):
        asyncio.run(stt.transcribe(b"xx", "audio/webm"))
    assert openai_calls == []


def test_both_providers_down_raises(monkeypatch):
    async def fake_post(audio_bytes, mime, params):
        return httpx.Response(503, text="down")

    async def fake_openai(audio_bytes, mime, keyterms):
        raise stt.ProviderUnavailable("openai: HTTP 500")

    monkeypatch.setattr(stt, "_post_deepgram", fake_post)
    monkeypatch.setattr(stt, "_transcribe_openai", fake_openai)
    _no_keyterms(monkeypatch)
    monkeypatch.delenv("STT_PROVIDER", raising=False)

    with pytest.raises(stt.TranscriptionError):
        asyncio.run(stt.transcribe(b"xx", "audio/webm"))


def test_stt_provider_env_flips_the_order(monkeypatch):
    """STT_PROVIDER=openai makes OpenAI primary (§8.1: the swap is one env
    var) — Deepgram is never touched when the primary succeeds."""
    deepgram_calls = []

    async def fake_post(audio_bytes, mime, params):
        deepgram_calls.append(1)
        return httpx.Response(200, json={})

    async def fake_openai(audio_bytes, mime, keyterms):
        return "primary now"

    monkeypatch.setattr(stt, "_post_deepgram", fake_post)
    monkeypatch.setattr(stt, "_transcribe_openai", fake_openai)
    _no_keyterms(monkeypatch)
    monkeypatch.setenv("STT_PROVIDER", "openai")

    result = asyncio.run(stt.transcribe(b"xx", "audio/webm"))
    assert result.provider == "openai" and result.text == "primary now"
    assert deepgram_calls == []
