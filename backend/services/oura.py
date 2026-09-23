"""Oura — the OAuth connection and the activity sync (migration 005, 2026-09-21).

Owner only (§8.9), like the web lane: routes/oura.py shows everyone else no
card and refuses them the connection and the sync.
The owner connects once from Settings. Oura sends the browser back to
/api/oura/callback with a code and our signed `state`; the code becomes an
access + refresh token pair, stored encrypted. From then on the app keeps
itself current: Today asks for a sync when it opens (skipped when the last
one is under MIN_SYNC_INTERVAL old), which refreshes the access token when
it is near expiry and re-reads the last few days — Oura keeps revising today
and finalises yesterday as the ring reports in, so every sync upserts.

Keys. The token cipher and the `state` signature are both derived from
OURA_CLIENT_SECRET (HKDF, one label each), so there is one secret to manage.
The cost: rotating the client secret makes stored tokens unreadable, which
reads as "not connected" — the owner reconnects once.

Refreshing never holds a transaction across the call to Oura: db.py has one
connection per instance behind a lock, so a slow Oura would stall every other
request. Instead the new tokens are written compare-and-swap — only if the row
still holds the refresh token that was spent. If it changed, another request
refreshed first and its tokens win. If Oura rotates refresh tokens, the loser
of such a race gets invalid_grant for a token the winner is still saving, so an
invalid_grant only ends the connection after waiting out the longest another
refresh can take (_RACE_WAIT_S) with the row still unchanged. A real revocation
costs that one wait; losing a race never costs the connection.

Each collection stands alone: the owner can untick a data type on Oura's
consent screen, and a 403 on one collection skips it (reported as `missing`)
instead of throwing away the other. Settings offers Reconnect to grant it, and
every (re)connect re-reads the whole stored period, so a collection granted
late gets its history too.

Oura API, read on cloud.ouraring.com/v2/docs 2026-09-21: authorize at
cloud.ouraring.com/oauth/authorize (scopes space-separated), tokens at
api.ouraring.com/oauth/token, data under /v2/usercollection/{daily_activity,
workout} with start_date / end_date / next_token. 401 = token expired or
revoked, 403 = scope missing or membership lapsed, 429 = rate limited.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from psycopg.types.json import Json

import db

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_URL = "https://api.ouraring.com/v2/usercollection"
SCOPES = "daily workout"  # only what the sync reads
# the collection each scope unlocks, in sync order
COLLECTIONS = (("daily", "daily_activity"), ("workout", "workout"))
# Must match the redirect URI registered on the Oura app exactly.
DEFAULT_REDIRECT_URI = "https://tally.art3m1s.me/api/oura/callback"

STATE_TTL_S = 600  # the owner has ten minutes on Oura's consent screen
REFRESH_MARGIN = timedelta(minutes=5)
BACKFILL_DAYS = 90  # the first sync reaches back this far
OVERLAP_DAYS = 3  # later syncs re-read this many days behind the newest stored one
MIN_SYNC_INTERVAL = timedelta(minutes=30)
_TIMEOUT_S = 15.0
# how long an invalid_grant waits for a concurrent refresh to land before it
# counts as a revocation: the longest that refresh's token call can take
_RACE_WAIT_S = _TIMEOUT_S + 2
_POLL_S = 0.25

NOT_CONNECTED = "Oura is not connected — connect it from Settings."
NOT_MIGRATED = "Oura sync is not set up on this database yet (migration 005)"


class OuraError(Exception):
    """Oura answered, but not with what the sync needs (5xx, 403, 429, network, ...)."""


class NotConnected(OuraError):
    """No usable tokens: never connected, revoked, or unreadable."""


class NotConfigured(OuraError):
    """OURA_CLIENT_ID / OURA_CLIENT_SECRET are not set on this deployment."""


class _Unauthorized(OuraError):
    """A data call got 401 — the access token needs a forced refresh."""


class _Forbidden(OuraError):
    """A data call got 403 — this collection's scope was not granted."""


# ---------------------------------------------------------------------------
# config and keys
# ---------------------------------------------------------------------------


def _config() -> tuple[str, str, str]:
    client_id = os.environ.get("OURA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OURA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise NotConfigured("OURA_CLIENT_ID and OURA_CLIENT_SECRET must both be set")
    redirect = os.environ.get("OURA_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI
    return client_id, client_secret, redirect


def configured() -> bool:
    try:
        _config()
    except NotConfigured:
        return False
    return True


def _key(label: bytes) -> bytes:
    secret = _config()[1]
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"tally/oura/" + label
    ).derive(secret.encode())


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_key(b"tokens")))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str | None:
    """None when the ciphertext no longer opens (the client secret changed)."""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def missing_scopes(granted: str | None) -> list[str]:
    """The scopes the sync needs that a grant lacks. Unknown (no `scope` in
    Oura's answer) counts as nothing missing — the 403s will say otherwise."""
    if not granted or not isinstance(granted, str):
        return []
    have = set(granted.split())
    return [s for s in SCOPES.split() if s not in have]


# ---------------------------------------------------------------------------
# state: which user a callback belongs to. Oura's redirect carries no bearer
# token, so the user id travels inside `state`, signed and short-lived. Only
# the owner can mint one (routes/oura.py), which is also what keeps a stranger
# from binding their Oura account to someone else's diary — if Oura ever opens
# to demo accounts, bind `state` to the browser as well (a cookie nonce).
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(_key(b"state"), payload.encode(), hashlib.sha256).digest())


def make_state(user_id: str, *, now: float | None = None) -> str:
    body = {
        "u": user_id,
        "exp": int((now or time.time()) + STATE_TTL_S),
        "n": secrets.token_urlsafe(8),
    }
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def read_state(state: str, *, now: float | None = None) -> str:
    """The user id a state was minted for; ValueError when forged, expired or
    malformed — the callback's input is anyone's, so nothing else may escape."""
    payload, _, sig = state.partition(".")
    # bytes, not str: compare_digest raises TypeError on non-ASCII str
    if not payload or not sig or not hmac.compare_digest(sig.encode(), _sign(payload).encode()):
        raise ValueError("state signature does not match")
    body = json.loads(_unb64(payload))
    if body["exp"] < (now or time.time()):
        raise ValueError("state expired")
    return str(body["u"])


def authorize_url(user_id: str) -> str:
    client_id, _, redirect = _config()
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": make_state(user_id),
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    """Every Oura call goes through here (tests swap in a MockTransport)."""
    return httpx.AsyncClient(timeout=_TIMEOUT_S)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(res: httpx.Response, what: str) -> dict:
    try:
        body = res.json()
    except ValueError:
        raise OuraError(f"Oura's {what} answered {res.status_code} with a body that is not JSON")
    if not isinstance(body, dict):
        raise OuraError(f"Oura's {what} answered {res.status_code} with an unexpected body")
    return body


async def _token_request(form: dict) -> dict:
    client_id, client_secret, _ = _config()
    try:
        async with _client() as client:
            res = await client.post(
                TOKEN_URL, data={**form, "client_id": client_id, "client_secret": client_secret}
            )
    except httpx.HTTPError as e:
        raise OuraError(f"Couldn't reach Oura's token endpoint ({type(e).__name__})")
    if res.status_code == 400:
        try:
            code = res.json().get("error")
        except (ValueError, AttributeError):
            code = None
        if code == "invalid_grant":  # a used, expired or revoked code / refresh token
            raise NotConnected(NOT_CONNECTED)
    if res.status_code >= 400:
        raise OuraError(f"Oura's token endpoint answered {res.status_code}")
    return _json(res, "token endpoint")


async def exchange_code(code: str) -> dict:
    redirect = _config()[2]
    return await _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect}
    )


async def _store(conn, tokens: dict, *, keep_refresh: str | None = None) -> None:
    # Oura may or may not rotate the refresh token: keep whichever it sent,
    # else the one we already had.
    refresh = tokens.get("refresh_token") or keep_refresh
    if not tokens.get("access_token") or not refresh:
        raise OuraError("Oura's token answer had no access or refresh token")
    expires_at = _now() + timedelta(seconds=int(tokens.get("expires_in") or 86400))
    scope = tokens.get("scope") if isinstance(tokens.get("scope"), str) else None
    await conn.execute(
        """
        INSERT INTO oura_tokens (access_token, refresh_token, expires_at, scope)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
        SET access_token = EXCLUDED.access_token, refresh_token = EXCLUDED.refresh_token,
            expires_at = EXCLUDED.expires_at,
            scope = coalesce(EXCLUDED.scope, oura_tokens.scope), updated_at = now()
        """,
        (encrypt(tokens["access_token"]), encrypt(refresh), expires_at, scope),
    )


async def save_tokens(tokens: dict) -> None:
    async with db.tx() as conn:
        await _store(conn, tokens)


_TOKENS_SQL = "SELECT access_token, refresh_token, expires_at FROM oura_tokens"


async def _access_token(*, force: bool = False) -> str:
    """A working access token, refreshed first when it is near expiry (or
    `force`d after a 401). A refresh token Oura has revoked, or tokens that no
    longer decrypt, end the connection: the row is deleted and NotConnected
    raised, so Settings offers Connect again. See the module docstring for why
    the refresh is compare-and-swap rather than a held lock."""
    row = await db.fetchone(_TOKENS_SQL)
    if row is None:
        raise NotConnected(NOT_CONNECTED)
    access, refresh = decrypt(row["access_token"]), decrypt(row["refresh_token"])
    if access and refresh and not force and row["expires_at"] - REFRESH_MARGIN > _now():
        return access
    spent = row["refresh_token"]  # ciphertext: equal only for the very same stored value

    fresh = None
    if refresh:
        try:
            fresh = await _token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        except NotConnected:
            # Spent — by a revocation, or by a concurrent refresh that has not
            # saved yet. Give that one the time it can take before deciding.
            deadline = time.monotonic() + _RACE_WAIT_S
            while time.monotonic() < deadline:
                current = await db.fetchone(_TOKENS_SQL)
                if current is None or current["refresh_token"] != spent:
                    break  # settled below: gone, or the winner's tokens
                await asyncio.sleep(_POLL_S)

    async with db.tx() as conn:
        cur = await conn.execute(_TOKENS_SQL + " FOR UPDATE")
        now_row = await cur.fetchone()
        if now_row is None:
            raise NotConnected(NOT_CONNECTED)  # disconnected meanwhile: do not resurrect it
        if now_row["refresh_token"] != spent:
            # another request refreshed (or reconnected) while we were at Oura:
            # its tokens are the live ones
            winner = decrypt(now_row["access_token"])
            if winner:
                return winner
            raise NotConnected(NOT_CONNECTED)
        if fresh is None:
            await conn.execute("DELETE FROM oura_tokens")  # revoked or unreadable, and still ours
        else:
            await _store(conn, fresh, keep_refresh=refresh)
    if fresh is None:
        raise NotConnected(NOT_CONNECTED)
    return fresh["access_token"]


# ---------------------------------------------------------------------------
# the sync
# ---------------------------------------------------------------------------


async def _get_all(path: str, token: str, start: date, end: date) -> list[dict]:
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    docs: list[dict] = []
    try:
        async with _client() as client:
            while True:
                res = await client.get(
                    f"{API_URL}/{path}", params=params, headers={"authorization": f"Bearer {token}"}
                )
                if res.status_code == 401:
                    raise _Unauthorized(path)
                if res.status_code == 403:
                    raise _Forbidden(path)
                if res.status_code == 429:
                    raise OuraError("Oura is rate limiting this app — try again later")
                if res.status_code >= 400:
                    raise OuraError(f"Oura's {path} answered {res.status_code}")
                body = _json(res, path)
                docs.extend(body.get("data") or [])
                if not body.get("next_token"):
                    return docs
                params = {**params, "next_token": body["next_token"]}
    except httpx.HTTPError as e:
        raise OuraError(f"Couldn't reach Oura's {path} ({type(e).__name__})")


def _int(value) -> int | None:
    return round(value) if isinstance(value, (int, float)) else None


def _ts(value) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def day_row(doc: dict) -> tuple:
    return (
        date.fromisoformat(doc["day"]),
        _int(doc.get("total_calories")),
        _int(doc.get("active_calories")),
        _int(doc.get("steps")),
        _int(doc.get("score")),
        Json(doc),
    )


def workout_row(doc: dict) -> tuple:
    return (
        str(doc["id"]),
        date.fromisoformat(doc["day"]),
        doc.get("activity") or "unknown",
        _ts(doc.get("start_datetime")),
        _ts(doc.get("end_datetime")),
        doc.get("calories"),
        doc.get("intensity"),
        doc.get("source"),
        Json(doc),
    )


_UPSERT_DAY = """
INSERT INTO oura_days (day, total_calories, active_calories, steps, score, raw)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (user_id, day) DO UPDATE
SET total_calories = EXCLUDED.total_calories, active_calories = EXCLUDED.active_calories,
    steps = EXCLUDED.steps, score = EXCLUDED.score, raw = EXCLUDED.raw, synced_at = now()
"""

_UPSERT_WORKOUT = """
INSERT INTO oura_workouts (oura_id, day, activity, start_at, end_at, calories, intensity, source, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (user_id, oura_id) DO UPDATE
SET day = EXCLUDED.day, activity = EXCLUDED.activity, start_at = EXCLUDED.start_at,
    end_at = EXCLUDED.end_at, calories = EXCLUDED.calories, intensity = EXCLUDED.intensity,
    source = EXCLUDED.source, raw = EXCLUDED.raw, synced_at = now()
"""


@dataclass
class SyncResult:
    skipped: bool
    days: int
    workouts: int
    last_synced_at: datetime | None
    missing: list[str] = field(default_factory=list)  # scopes Oura refused (403)


async def sync(*, today: date, force: bool = False, backfill: bool = False) -> SyncResult:
    """Pull daily activity and workouts into oura_days / oura_workouts.
    `today` is the owner's local day (settings.timezone) — the window runs
    through it. `backfill` (every (re)connect) re-reads the whole stored period
    and at least BACKFILL_DAYS: a collection refused before and granted now
    must get its history, not only the last few days."""
    row = await db.fetchone("SELECT last_synced_at FROM oura_tokens")
    if row is None:
        raise NotConnected(NOT_CONNECTED)
    last = row["last_synced_at"]
    if not force and last is not None and _now() - last < MIN_SYNC_INTERVAL:
        return SyncResult(skipped=True, days=0, workouts=0, last_synced_at=last)

    span = await db.fetchone("SELECT min(day) AS oldest, max(day) AS newest FROM oura_days")
    floor = today - timedelta(days=BACKFILL_DAYS)
    if backfill or span["newest"] is None:
        start = min(span["oldest"] or floor, floor)
    else:
        start = span["newest"] - timedelta(days=OVERLAP_DAYS)
    end = today + timedelta(days=1)  # harmless if Oura's end_date is inclusive

    async def fetch(token: str) -> tuple[dict[str, list[dict]], list[str]]:
        got: dict[str, list[dict]] = {}
        missing: list[str] = []
        for scope, path in COLLECTIONS:
            try:
                got[path] = await _get_all(path, token, start, end)
            except _Forbidden:
                missing.append(scope)
        return got, missing

    try:
        got, missing = await fetch(await _access_token())
    except _Unauthorized:
        got, missing = await fetch(await _access_token(force=True))
    if not got:
        raise OuraError("Oura refused every collection: reconnect and allow them, or check the membership")

    days, workouts = got.get("daily_activity", []), got.get("workout", [])
    async with db.tx() as conn:
        async with conn.cursor() as cur:
            if days:
                await cur.executemany(_UPSERT_DAY, [day_row(d) for d in days])
            if workouts:
                await cur.executemany(_UPSERT_WORKOUT, [workout_row(w) for w in workouts])
        cur = await conn.execute(
            "UPDATE oura_tokens SET last_synced_at = now() RETURNING last_synced_at"
        )
        synced = await cur.fetchone()
    return SyncResult(
        skipped=False,
        days=len(days),
        workouts=len(workouts),
        last_synced_at=synced["last_synced_at"] if synced else None,
        missing=missing,
    )
