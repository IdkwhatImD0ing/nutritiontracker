"""Oura — the OAuth connection and the activity sync (migration 005, 2026-09-21).

Owner only (§8.9), like the web lane: routes/oura.py shows everyone else no
card and refuses them the connection and the sync.
The owner connects once from Settings. Oura sends the browser back to
/api/oura/callback with a code and our signed `state`; the code becomes an
access + refresh token pair, stored encrypted. From then on the app keeps
itself current: Today asks for a sync when it opens (skipped when the last
one, or the last failed attempt, is under MIN_SYNC_INTERVAL old), which
refreshes the access token when it is near expiry and re-reads the last few
days of each collection — Oura keeps revising today and finalises yesterday
as the ring reports in, so every sync upserts. The two collections are
fetched at once through one HTTP client, within SYNC_BUDGET_S of network time;
whatever arrived by then is stored, and the rest is the next sync's.

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
import logging
import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import psycopg
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from psycopg.types.json import Json
from pydantic import BaseModel

import db

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_URL = "https://api.ouraring.com/v2/usercollection"

# (scope, collection, table, days an ordinary sync re-reads behind that
# table's newest stored day). Days: Oura revises today and finalises
# yesterday. Workouts: one can be logged in the Oura app days after the fact.
COLLECTIONS = (
    ("daily", "daily_activity", "oura_days", 3),
    ("workout", "workout", "oura_workouts", 14),
)
SCOPES = " ".join(c[0] for c in COLLECTIONS)  # only what the sync reads

STATE_TTL_S = 600  # the owner has ten minutes on Oura's consent screen
REFRESH_MARGIN = timedelta(minutes=5)
BACKFILL_DAYS = 90  # the first sync reaches back this far
MIN_SYNC_INTERVAL = timedelta(minutes=30)
_TIMEOUT_S = 15.0
SYNC_BUDGET_S = 60  # network time one sync may spend paging (Vercel allows 120 s)
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
    """A data call got 401 — the access token needs a forced refresh. The
    message is what the owner reads if the retry is refused too."""

    def __init__(self, path: str):
        super().__init__(f"Oura refused Tally's access to {path} — reconnect Oura from Settings")


class _Forbidden(OuraError):
    """A data call got 403 — this collection's scope was not granted."""


# ---------------------------------------------------------------------------
# config and keys
# ---------------------------------------------------------------------------


def _default_redirect() -> str | None:
    """This deployment's own callback: APP_ORIGIN's first entry (list the
    current domain first), else Vercel's production domain — never VERCEL_URL,
    a preview's own address, which the Oura app does not have registered."""
    origin = os.environ.get("APP_ORIGIN", "").split(",")[0].strip().rstrip("/")
    if not origin:
        host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip().rstrip("/")
        origin = host if not host or "://" in host else f"https://{host}"
    return f"{origin}/api/oura/callback" if origin else None


def _config() -> tuple[str, str, str]:
    client_id = os.environ.get("OURA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OURA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise NotConfigured("OURA_CLIENT_ID and OURA_CLIENT_SECRET must both be set")
    redirect = os.environ.get("OURA_REDIRECT_URI", "").strip() or _default_redirect()
    if not redirect:
        raise NotConfigured("set OURA_REDIRECT_URI (or APP_ORIGIN) so Oura knows where to send you back")
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
    try:
        seconds = int(tokens.get("expires_in") or 86400)
    except (TypeError, ValueError):
        seconds = 86400  # Oura's documented lifetime; a refresh sorts out anything shorter
    expires_at = _now() + timedelta(seconds=seconds)
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


async def connected() -> bool:
    """Whether this user has an Oura grant this deployment can use — what
    /api/me tells Today, so it only asks for a sync that can happen."""
    if not configured():
        return False
    try:
        return await db.fetchone("SELECT 1 AS ok FROM oura_tokens") is not None
    except psycopg.errors.UndefinedTable:
        return False  # 005 not applied yet


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


async def _get_all(
    client: httpx.AsyncClient, path: str, token: str, start: date, end: date, deadline: float
) -> tuple[list[dict], bool]:
    """Every page of one collection, and whether it got them all: past
    `deadline` no further page is asked for, and what arrived is returned."""
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    docs: list[dict] = []
    try:
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
                return docs, True
            if time.monotonic() > deadline:
                return docs, False
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


def _rows(docs: list[dict], build, what: str) -> list[tuple]:
    """The rows of the documents Tally can read; an odd one is skipped, not fatal."""
    rows = []
    for doc in docs:
        try:
            rows.append(build(doc))
        except (KeyError, TypeError, ValueError, OverflowError):
            log.warning("oura: skipped a %s document Tally could not read", what)
    return rows


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


class OuraSync(BaseModel):
    """One sync's outcome — also POST /api/oura/sync's answer."""

    skipped: bool  # the last sync, or failed attempt, was under half an hour ago
    days: int
    workouts: int
    last_synced_at: datetime | None
    missing: list[str] = []  # scopes Oura refused this time (403)


# When the last sync attempt failed, by user, on this instance: a failing Oura
# (429, 5xx, network) is not asked again on every open. In memory, so a cold
# start forgets it — enough for a backoff, and no column for it.
_failed_at: dict[str, datetime] = {}


async def sync(*, today: date, force: bool = False, backfill: bool = False) -> OuraSync:
    """Pull daily activity and workouts into oura_days / oura_workouts.
    `today` is the owner's local day (settings.timezone) — the window runs
    through it. `backfill` (every (re)connect) re-reads the whole stored period
    and at least BACKFILL_DAYS: a collection refused before and granted now
    must get its history, not only the last few days. `force` (Sync now, the
    callback) skips both half-hour waits."""
    row = await db.fetchone("SELECT last_synced_at FROM oura_tokens")
    if row is None:
        raise NotConnected(NOT_CONNECTED)
    last = row["last_synced_at"]
    user = db.current_user_id.get() or ""
    if not force and any(
        t is not None and _now() - t < MIN_SYNC_INTERVAL for t in (last, _failed_at.get(user))
    ):
        return OuraSync(skipped=True, days=0, workouts=0, last_synced_at=last)
    try:
        result = await _sync(today, backfill)
    except (NotConnected, NotConfigured):
        raise
    except OuraError:
        _failed_at[user] = _now()
        raise
    _failed_at.pop(user, None)
    return result


async def _sync(today: date, backfill: bool) -> OuraSync:
    floor = today - timedelta(days=BACKFILL_DAYS)
    starts: dict[str, date] = {}
    if backfill:
        oldest = (
            await db.fetchone(
                "SELECT least((SELECT min(day) FROM oura_days), "
                "(SELECT min(day) FROM oura_workouts)) AS day"
            )
        )["day"]
        starts = {path: min(oldest or floor, floor) for _, path, _, _ in COLLECTIONS}
    else:
        # each collection from its own newest row: a workout-only grant does
        # not re-read 90 days every time, and a late-logged workout is caught
        for _, path, table, overlap in COLLECTIONS:
            newest = (await db.fetchone(f"SELECT max(day) AS day FROM {table}"))["day"]
            starts[path] = newest - timedelta(days=overlap) if newest else floor
    end = today + timedelta(days=1)  # harmless if Oura's end_date is inclusive
    deadline = time.monotonic() + SYNC_BUDGET_S

    async def fetch(token: str) -> list:
        async with _client() as client:

            async def one(path: str):
                try:
                    return await _get_all(client, path, token, starts[path], end, deadline)
                except _Forbidden:
                    return None

            results = await asyncio.gather(
                *(one(path) for _, path, _, _ in COLLECTIONS), return_exceptions=True
            )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:  # a 401 first: it is the one a refresh can fix
            raise next((e for e in errors if isinstance(e, _Unauthorized)), errors[0])
        return results

    try:
        results = await fetch(await _access_token())
    except _Unauthorized:
        results = await fetch(await _access_token(force=True))
    got = {c[1]: r for c, r in zip(COLLECTIONS, results) if r is not None}
    missing = [c[0] for c, r in zip(COLLECTIONS, results) if r is None]
    if not got:
        raise OuraError("Oura refused every collection: reconnect and allow them, or check the membership")
    for path, (_, complete) in got.items():
        if not complete:
            log.warning("oura: %s ran past the %ss budget; kept what arrived", path, SYNC_BUDGET_S)

    days = _rows(got.get("daily_activity", ([], True))[0], day_row, "daily_activity")
    workouts = _rows(got.get("workout", ([], True))[0], workout_row, "workout")
    async with db.tx() as conn:
        async with conn.cursor() as cur:
            if days:
                await cur.executemany(_UPSERT_DAY, days)
            if workouts:
                await cur.executemany(_UPSERT_WORKOUT, workouts)
        cur = await conn.execute(
            "UPDATE oura_tokens SET last_synced_at = now() RETURNING last_synced_at"
        )
        synced = await cur.fetchone()
    return OuraSync(
        skipped=False,
        days=len(days),
        workouts=len(workouts),
        last_synced_at=synced["last_synced_at"] if synced else None,
        missing=missing,
    )
