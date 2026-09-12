"""Speech-to-text behind the Transcriber interface (DESIGN.md §8.1, §2).

    await transcribe(audio_bytes, mime) -> Transcription {text, transcribe_ms, provider}

Blob in, text out — nothing downstream knows which provider ran (§8.1a).

Primary: **Deepgram nova-3 prerecorded** — one raw-binary POST to /v1/listen
with ?model=nova-3&smart_format=true plus repeated ?keyterm= params fed from
the brands, top aliases and diary foods (§8.1b: transcription improves as the
library grows).
Fallback: **OpenAI gpt-transcribe** via POST /v1/audio/transcriptions
(multipart) with the same vocabulary as a free-text prompt.

STT_PROVIDER picks the primary ("deepgram" default — the §11 swap is one env
var); a 5xx/timeout/connection failure fails over to the other provider
automatically. Any 4xx is a hard TranscriptionError (a bad request will not
get better on a different vendor). A provider whose API key is not configured
is "unavailable" the same way (fail over, never crash); when no provider is
left the error names every reason, so the route's 503 says why.

Each call records one FireTrace span (tracing.run: the provider and model
that produced the text, the transcript, the wall time, any failover).
"""

from __future__ import annotations

import re
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

import tracing

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-3"
OPENAI_STT_MODEL = "gpt-transcribe"  # §8.1: current-gen, verified Aug 2026

# §8.1: Deepgram caps keyterm biasing at 500 tokens total — stay well under it.
# ~60 alias/brand terms fit comfortably; the token budget is the hard wall.
KEYTERM_MAX_TERMS = 60
KEYTERM_TOKEN_BUDGET = 300

_TIMEOUT_S = 30.0  # §11 budgets STT at 1–4.4s on a ~10s clip; 30s is generous


class TranscriptionError(Exception):
    """STT failed for good (bad audio, bad key, both providers down)."""


class ProviderUnavailable(TranscriptionError):
    """5xx / timeout / connection failure — retriable on the other provider."""


@dataclass
class Transcription:
    text: str
    transcribe_ms: int  # total wall time across the transcribe() call
    provider: str  # "deepgram" | "openai" — whichever actually produced text


# ---------------------------------------------------------------------------
# env (.env fallback for local dev, same lookup order as auth.py; values are
# read, never logged — Iron Rule 3)
# ---------------------------------------------------------------------------


def _env(name: str, default: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    here = Path(__file__).resolve().parent.parent  # backend/
    for path in (here / ".env", here.parent / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip("'\"")
    return default


# ---------------------------------------------------------------------------
# transcript validation (§8.1d): empty/near-empty transcript = capture failure
# ---------------------------------------------------------------------------

_ALPHA_TOKEN_RE = re.compile(r"[^\W\d_]{3,}", flags=re.UNICODE)  # >=3 letters in a row


def is_near_empty(text: str | None) -> bool:
    """True when a transcript carries no plausible utterance: no alphabetic
    token of three or more letters anywhere. A silent blob yields "", ".", a
    digit, or a filler like "uh"/"um" — none of which is food. "egg", "two
    eggs" and "fairlife" all pass: a one-word log is a real log (§3.2)."""
    return _ALPHA_TOKEN_RE.search(text or "") is None


# ---------------------------------------------------------------------------
# vocabulary biasing (§8.1b): brands + top aliases + diary foods, capped under the limit
# ---------------------------------------------------------------------------


def _est_tokens(term: str) -> int:
    """Rough token estimate (~4 chars/token, floor 1) — only used to stay
    conservatively inside Deepgram's 500-token keyterm cap."""
    return max(1, (len(term) + 3) // 4)


def cap_keyterms(
    terms: list[str],
    max_terms: int = KEYTERM_MAX_TERMS,
    token_budget: int = KEYTERM_TOKEN_BUDGET,
) -> list[str]:
    """Dedupe (case-insensitive), preserve order, and cut at both caps.
    A single oversized term is skipped, not allowed to end collection."""
    out: list[str] = []
    seen: set[str] = set()
    budget = token_budget
    for term in terms:
        term = " ".join((term or "").split())
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        cost = _est_tokens(term)
        if cost > budget:
            continue
        seen.add(key)
        out.append(term)
        budget -= cost
        if len(out) >= max_terms:
            break
    return out


async def build_keyterms() -> list[str]:
    """Candidate vocabulary from the library: brands first (§8.1 — brand-name
    accuracy is the stated priority), then the most-used alias phrases, then
    the names of the foods in his diary, most-logged first. The diary names
    are there because a barcode, search or photo row learns no alias (§3.5):
    the tonkotsu ramen he had scanned was not in the list the day Deepgram
    wrote "Tom Katsu, Winston Roman" (2026-09-10; Bill: "food memory should be
    independent of source"). Biasing is an accuracy bonus, never a hard
    dependency: any DB failure degrades to an unbiased transcription."""
    import db  # lazy: shared interface, stubbed in tests

    try:
        brands = await db.fetchall(
            "SELECT brand FROM foods WHERE brand IS NOT NULL "
            "GROUP BY brand ORDER BY count(*) DESC LIMIT %s",
            (KEYTERM_MAX_TERMS,),
        )
        aliases = await db.fetchall(
            "SELECT alias_norm FROM aliases "
            "ORDER BY use_count DESC, last_used_at DESC LIMIT %s",
            (KEYTERM_MAX_TERMS,),
        )
        diary = await db.fetchall(
            "SELECT f.display_name FROM entries e JOIN foods f ON f.id = e.food_id "
            "WHERE f.source <> 'estimate' GROUP BY f.id, f.display_name "
            "ORDER BY count(*) DESC, max(e.logged_at) DESC LIMIT %s",
            (KEYTERM_MAX_TERMS,),
        )
    except Exception:
        return []
    return cap_keyterms(
        [r["brand"] for r in brands]
        + [r["alias_norm"] for r in aliases]
        + [r["display_name"] for r in diary]
    )


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


async def _post_deepgram(
    audio_bytes: bytes, mime: str, params: list[tuple[str, str]]
) -> httpx.Response:
    """The raw HTTP hop, isolated so tests can stub the wire (5xx/timeout)."""
    headers = {
        "Authorization": f"Token {_env('DEEPGRAM_API_KEY') or ''}",
        "Content-Type": mime or "application/octet-stream",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        return await client.post(
            DEEPGRAM_URL, params=params, content=audio_bytes, headers=headers
        )


async def _transcribe_deepgram(
    audio_bytes: bytes, mime: str, keyterms: list[str]
) -> str:
    params: list[tuple[str, str]] = [
        ("model", DEEPGRAM_MODEL),
        ("smart_format", "true"),
    ]
    params.extend(("keyterm", term) for term in keyterms)
    try:
        resp = await _post_deepgram(audio_bytes, mime, params)
    except httpx.TransportError as e:  # timeouts subclass TransportError
        raise ProviderUnavailable(f"deepgram: {type(e).__name__}") from e
    if resp.status_code >= 500:
        raise ProviderUnavailable(f"deepgram: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise TranscriptionError(
            f"deepgram: HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError, ValueError) as e:
        raise TranscriptionError("deepgram: unexpected response shape") from e


_EXT_BY_MIME = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "video/webm": "webm",
    "video/mp4": "mp4",
}


async def _transcribe_openai(
    audio_bytes: bytes, mime: str, keyterms: list[str]
) -> str:
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        # Without the key the SDK raises a bare OpenAIError from the client
        # constructor — which is neither of the classes below and would
        # crash the route. A missing key is the provider being unavailable.
        raise ProviderUnavailable(
            "openai: OPENAI_API_KEY is not set — OpenAI transcription unavailable"
        )
    import openai
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    base_mime = (mime or "").split(";")[0].strip().lower()
    ext = _EXT_BY_MIME.get(base_mime, "webm")
    prompt = (
        "Food log dictation. Vocabulary that may be spoken (brands and foods): "
        + ", ".join(keyterms)
        if keyterms
        else ""
    )
    try:
        result = await client.audio.transcriptions.create(
            model=OPENAI_STT_MODEL,
            file=(f"clip.{ext}", audio_bytes, base_mime or "application/octet-stream"),
            prompt=prompt,
        )
    except openai.APIConnectionError as e:  # APITimeoutError subclasses this
        raise ProviderUnavailable(f"openai: {type(e).__name__}") from e
    except openai.APIStatusError as e:
        if e.status_code >= 500:
            raise ProviderUnavailable(f"openai: HTTP {e.status_code}") from e
        raise TranscriptionError(f"openai: HTTP {e.status_code}") from e
    return result.text


async def _run_provider(
    name: str, audio_bytes: bytes, mime: str, keyterms: list[str]
) -> str:
    # dispatched through module globals (not a dict of function refs) so tests
    # can monkeypatch _transcribe_* the ordinary way
    if name == "deepgram":
        return await _transcribe_deepgram(audio_bytes, mime, keyterms)
    return await _transcribe_openai(audio_bytes, mime, keyterms)


# ---------------------------------------------------------------------------
# the Transcriber interface (§8.1a)
# ---------------------------------------------------------------------------


async def transcribe(audio_bytes: bytes, mime: str) -> Transcription:
    primary = (_env("STT_PROVIDER") or "deepgram").strip().lower()
    if primary not in ("deepgram", "openai"):
        primary = "deepgram"
    order = [primary, "openai" if primary == "deepgram" else "deepgram"]

    keyterms = await build_keyterms()  # fetched once, shared across failover
    t0 = time.monotonic()
    failures: list[str] = []
    async with tracing.run(
        "transcribe", kind="custom",
        input={"mime": mime, "bytes": len(audio_bytes)},
        attributes={"keyterms": len(keyterms)},
    ) as span:
        for name in order:
            # recorded before the attempt: an outage of every provider still
            # names the last one tried, and the trace is kept
            span.set(provider=name, model=DEEPGRAM_MODEL if name == "deepgram" else OPENAI_STT_MODEL)
            try:
                text = await _run_provider(name, audio_bytes, mime, keyterms)
            except ProviderUnavailable as e:
                failures.append(str(e))
                continue
            transcribe_ms = int((time.monotonic() - t0) * 1000)
            span.set(
                output={"text": text, "transcribe_ms": transcribe_ms},
                attributes={"failed_over": failures} if failures else None,
            )
            return Transcription(text=text, transcribe_ms=transcribe_ms, provider=name)
        # every provider declined — say why for each, in the order they were tried
        raise TranscriptionError(
            "no transcription provider available: " + "; ".join(failures)
        )
