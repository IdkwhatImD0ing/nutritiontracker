"""Oura (migration 005, services/oura.py, routes/oura.py).

Pure checks first (state, the token cipher, parsing), then the connection and
the sync against the live database inside one force-rollback transaction
(isolated_db), with Oura itself faked by an httpx MockTransport — no test
here reaches the network or leaves a row behind.

Run:  cd backend && uv run --with pytest pytest tests/test_oura.py -q
"""

from __future__ import annotations

import asyncio
import copy
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.types.json import Json  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from test_tenancy import TEST_USER_ID, _become, _scalar, isolated_db  # noqa: E402

import db  # noqa: E402
from routes import diary as diary_route  # noqa: E402
from routes import history as history_route  # noqa: E402
from routes import me as me_route  # noqa: E402
from routes import oura as oura_route  # noqa: E402
from services import oura  # noqa: E402

SECRET = "test-client-secret"


@pytest.fixture(autouse=True)
def _oura_env(monkeypatch):
    monkeypatch.setenv("OURA_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OURA_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("OURA_REDIRECT_URI", raising=False)
    monkeypatch.setenv("APP_ORIGIN", "https://tally.art3m1s.me")  # the callback derives from it
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    monkeypatch.setattr(oura, "_failed_at", {})  # the failure backoff, per test
    # a revocation waits out a possible concurrent refresh; keep that short here
    monkeypatch.setattr(oura, "_RACE_WAIT_S", 0.3)
    monkeypatch.setattr(oura, "_POLL_S", 0.02)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# a fake Oura: the token endpoint and the two collections, calls recorded
# ---------------------------------------------------------------------------

DAY_PAGE_1 = {
    "data": [
        {"id": "a1", "day": "2026-09-19", "total_calories": 2864.4, "active_calories": 512,
         "steps": 8120, "score": 81}
    ],
    "next_token": "p2",
}
DAY_PAGE_2 = {
    "data": [
        {"id": "a2", "day": "2026-09-20", "total_calories": 2701, "active_calories": 400,
         "steps": 6500, "score": 77}
    ],
    "next_token": None,
}
WORKOUTS = {
    "data": [
        {"id": "w1", "activity": "strengthTraining", "calories": 212.5, "day": "2026-09-19",
         "distance": None, "start_datetime": "2026-09-19T18:00:00-07:00",
         "end_datetime": "2026-09-19T18:50:00-07:00", "intensity": "moderate",
         "label": None, "source": "manual"}
    ],
    "next_token": None,
}


class FakeOura:
    def __init__(self, *, tokens: list | None = None, data_401_once: bool = False,
                 forbidden: tuple[str, ...] = (), data_error: type | None = None,
                 on_token=None, always_401: bool = False):
        # each token-endpoint call pops the next answer: dict = 200 JSON,
        # int = that status with an invalid_grant body, str = 200 with that
        # (non-JSON) body, an httpx exception class = that network failure
        self.tokens = list(tokens or [])
        self.data_401_once = data_401_once
        self.always_401 = always_401  # every data call answers 401, even after a refresh
        self.forbidden = set(forbidden)  # collections that answer 403
        self.data_error = data_error  # an httpx exception class every data call raises
        self.on_token = on_token  # async hook run before a token answer (races)
        # per-instance copies, so a test can revise what Oura serves next
        self.day_pages = [copy.deepcopy(DAY_PAGE_1), copy.deepcopy(DAY_PAGE_2)]
        self.workouts = copy.deepcopy(WORKOUTS)
        self.token_forms: list[dict] = []
        self.data_calls: list[tuple[str, dict, str]] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            if self.on_token:
                await self.on_token()
            answer = self.tokens.pop(0)
            if isinstance(answer, type):
                raise answer("fake network failure", request=request)
            if isinstance(answer, int):
                return httpx.Response(answer, json={"error": "invalid_grant"})
            if isinstance(answer, str):
                return httpx.Response(200, text=answer)
            return httpx.Response(200, json=answer)
        auth = request.headers.get("authorization", "")
        params = dict(request.url.params)
        self.data_calls.append((request.url.path, params, auth))
        if self.data_error:
            raise self.data_error("fake network failure", request=request)
        if self.data_401_once or self.always_401:
            self.data_401_once = False
            return httpx.Response(401, json={"detail": "expired"})
        name = request.url.path.rsplit("/", 1)[1]
        if name in self.forbidden:
            return httpx.Response(403, json={"detail": "missing scope"})
        if name == "daily_activity":
            return httpx.Response(200, json=self.day_pages[1 if params.get("next_token") else 0])
        if name == "workout":
            return httpx.Response(200, json=self.workouts)
        return httpx.Response(404)

    def install(self, monkeypatch) -> FakeOura:
        monkeypatch.setattr(
            oura, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        )
        return self


def _calls(fake: FakeOura, path: str) -> list[dict]:
    """The query params of each call to one collection, in order. The two
    collections are fetched at once, so their calls interleave."""
    return [c[1] for c in fake.data_calls if c[0].endswith(f"/{path}")]


def _tokens(n: int, *, refresh: bool = True, scope: str = "daily workout") -> dict:
    t = {"access_token": f"acc-{n}", "expires_in": 86400, "token_type": "Bearer", "scope": scope}
    if refresh:
        t["refresh_token"] = f"ref-{n}"
    return t


async def _stored_tokens(conn) -> tuple[str | None, str | None]:
    cur = await conn.execute("SELECT access_token, refresh_token FROM oura_tokens")
    row = await cur.fetchone()
    return (oura.decrypt(row["access_token"]), oura.decrypt(row["refresh_token"])) if row else (None, None)


async def _expire(conn) -> None:
    await conn.execute("UPDATE oura_tokens SET expires_at = now() - interval '1 minute'")


# ---------------------------------------------------------------------------
# 1. pure: state, cipher, parsing, the authorize URL
# ---------------------------------------------------------------------------


def test_state_round_trips_and_refuses_forged_or_expired(monkeypatch):
    s = oura.make_state("user-1", now=1_000_000)
    assert oura.read_state(s, now=1_000_000 + 60) == "user-1"
    with pytest.raises(ValueError):
        oura.read_state(s, now=1_000_000 + oura.STATE_TTL_S + 1)  # too late
    payload, _, sig = s.partition(".")
    forged = oura._b64(b'{"u":"someone-else","exp":9999999999,"n":"x"}')
    with pytest.raises(ValueError):
        oura.read_state(f"{forged}.{sig}", now=1_000_000)  # someone else's id, old signature
    for bad in ("", "nodot", f"{payload}.", f".{sig}", "a.é", f"{payload}.éé"):
        with pytest.raises(ValueError):  # non-ASCII included: never a TypeError
            oura.read_state(bad, now=1_000_000)
    monkeypatch.setenv("OURA_CLIENT_SECRET", "a-different-secret")
    with pytest.raises(ValueError):
        oura.read_state(s, now=1_000_000)  # signed under another secret


def test_token_cipher_round_trips_and_a_new_secret_cannot_read_it(monkeypatch):
    sealed = oura.encrypt("refresh-abc")
    assert "refresh-abc" not in sealed and oura.decrypt(sealed) == "refresh-abc"
    monkeypatch.setenv("OURA_CLIENT_SECRET", "rotated")
    assert oura.decrypt(sealed) is None  # reads as "not connected", never raises


def test_not_configured(monkeypatch):
    monkeypatch.delenv("OURA_CLIENT_SECRET")
    assert not oura.configured()
    with pytest.raises(oura.NotConfigured):
        oura.authorize_url("user-1")


def test_authorize_url_asks_only_for_what_the_sync_reads():
    url = urlsplit(oura.authorize_url("user-1"))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == oura.AUTHORIZE_URL
    assert q["response_type"] == "code" and q["client_id"] == "test-client-id"
    assert q["redirect_uri"] == "https://tally.art3m1s.me/api/oura/callback"
    assert q["scope"] == "daily workout"
    assert oura.read_state(q["state"]) == "user-1"


def test_the_redirect_uri_comes_from_this_deployment(monkeypatch):
    assert oura._config()[2] == "https://tally.art3m1s.me/api/oura/callback"  # APP_ORIGIN, from the fixture
    monkeypatch.setenv("APP_ORIGIN", "https://new.example/, https://old.example")
    assert oura._config()[2] == "https://new.example/api/oura/callback"  # its first entry
    monkeypatch.delenv("APP_ORIGIN")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "prod.example")
    monkeypatch.setenv("VERCEL_URL", "tally-git-branch.vercel.app")  # a preview: never used
    assert oura._config()[2] == "https://prod.example/api/oura/callback"
    monkeypatch.setenv("OURA_REDIRECT_URI", "https://pinned.example/cb")
    assert oura._config()[2] == "https://pinned.example/cb"  # an explicit one wins
    monkeypatch.delenv("OURA_REDIRECT_URI")
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL")
    with pytest.raises(oura.NotConfigured):
        oura._config()  # nothing to derive it from
    assert not oura.configured()


def test_missing_scopes():
    assert oura.missing_scopes("daily workout") == []
    assert oura.missing_scopes("workout daily personal") == []
    assert oura.missing_scopes("daily") == ["workout"]
    assert oura.missing_scopes("personal") == ["daily", "workout"]
    assert oura.missing_scopes(None) == [] and oura.missing_scopes("") == []  # unknown: trust the 403s
    assert oura.missing_scopes(["daily"]) == [] and oura.missing_scopes(7) == []  # not a string: unknown


def test_rows_round_calories_and_tolerate_missing_fields():
    d = oura.day_row({"day": "2026-09-19", "total_calories": 2864.6, "steps": 8120})
    assert d[:5] == (date(2026, 9, 19), 2865, None, 8120, None)
    w = oura.workout_row({"id": 7, "day": "2026-09-19", "start_datetime": "2026-09-19T18:00:00-07:00"})
    assert w[0] == "7" and w[2] == "unknown" and w[4] is None
    assert w[3] == datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 2. connect, sync, refresh — live database, rolled back; Oura faked
# ---------------------------------------------------------------------------


def test_callback_stores_encrypted_tokens_and_runs_the_first_sync(monkeypatch):
    fake = FakeOura(tokens=[_tokens(1)]).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            state = oura.make_state(TEST_USER_ID)
            db.current_user_id.set(None)  # as a bare browser request: no bearer, no tenant
            res = await oura_route.callback(code="the-code", state=state)
            assert res.status_code == 303 and res.headers["location"] == "/settings?oura=connected"
            assert db.current_user_id.get() == TEST_USER_ID  # the tenant came from `state`

            # the code exchange sent what Oura needs, including the exact redirect URI
            form = fake.token_forms[0]
            assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
            assert form["redirect_uri"] == "https://tally.art3m1s.me/api/oura/callback"
            assert form["client_secret"] == SECRET

            # stored encrypted: ciphertext in the table, the plaintext only after decrypt
            raw = await _scalar(conn, "SELECT access_token FROM oura_tokens")
            assert raw != "acc-1" and await _stored_tokens(conn) == ("acc-1", "ref-1")

            # the first sync backfilled, following next_token, with the new token
            assert len(_calls(fake, "daily_activity")) == 2 and len(_calls(fake, "workout")) == 1
            assert all(c[2] == "Bearer acc-1" for c in fake.data_calls)
            first = _calls(fake, "daily_activity")[0]
            today = await oura_route._today()
            assert first["start_date"] == (today - timedelta(days=oura.BACKFILL_DAYS)).isoformat()
            assert first["end_date"] == (today + timedelta(days=1)).isoformat()
            assert await _scalar(conn, "SELECT count(*) FROM oura_days") == 2
            assert await _scalar(
                conn, "SELECT total_calories FROM oura_days WHERE day = '2026-09-19'"
            ) == 2864
            assert await _scalar(conn, "SELECT activity FROM oura_workouts") == "strengthTraining"
            assert await _scalar(conn, "SELECT last_synced_at IS NOT NULL FROM oura_tokens")

            # another tenant sees none of it (RLS on all three tables, 005)
            await _become(conn, "oura-other")
            for table in ("oura_tokens", "oura_days", "oura_workouts"):
                assert await _scalar(conn, f"SELECT count(*) FROM {table}") == 0

    _run(scenario())


def test_callback_refuses_bad_state_and_reports_a_declined_consent(monkeypatch):
    fake = FakeOura(tokens=[_tokens(1)]).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            assert (await oura_route.callback(error="access_denied")).headers["location"] == (
                "/settings?oura=denied"
            )
            for state in ("forged.state", "a.é"):
                res = await oura_route.callback(code="c", state=state)
                assert res.headers["location"] == "/settings?oura=error"
            assert fake.token_forms == []  # never reached Oura with a bad state
            assert await _scalar(conn, "SELECT count(*) FROM oura_tokens") == 0

    _run(scenario())


def test_callback_redirects_when_the_token_exchange_fails(monkeypatch):
    # a timeout and a non-JSON body: both must land back on Settings, not a 500
    fake = FakeOura(tokens=[httpx.ReadTimeout, "<html>bad gateway</html>"]).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            for _ in range(2):
                res = await oura_route.callback(code="c", state=oura.make_state(TEST_USER_ID))
                assert res.status_code == 303 and res.headers["location"] == "/settings?oura=error"
            assert len(fake.token_forms) == 2  # both reached Oura: the exchange is what failed
            assert await _scalar(conn, "SELECT count(*) FROM oura_tokens") == 0

    _run(scenario())


def test_a_refused_scope_keeps_the_other_collection_and_says_what_is_missing(monkeypatch):
    # the owner unticked Workout on Oura's consent screen
    FakeOura(tokens=[_tokens(1, scope="daily")], forbidden=("workout",)).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            res = await oura_route.callback(code="c", state=oura.make_state(TEST_USER_ID))
            assert res.headers["location"] == "/settings?oura=missing_scope"
            assert await _scalar(conn, "SELECT count(*) FROM oura_days") == 2  # kept
            assert await _scalar(conn, "SELECT count(*) FROM oura_workouts") == 0
            assert (await oura_route.get_status()).missing == ["workout"]

            synced = await oura_route.post_sync(True)
            assert (synced.days, synced.workouts, synced.missing) == (2, 0, ["workout"])

    _run(scenario())


def test_a_403_is_reported_even_when_the_grant_looks_complete(monkeypatch):
    # Oura granted the full scope string, or sent none at all, yet refuses
    # workouts: the 403 alone must still reach the owner
    no_scope = _tokens(2)
    del no_scope["scope"]
    FakeOura(tokens=[_tokens(1), no_scope], forbidden=("workout",)).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            for _ in range(2):
                res = await oura_route.callback(code="c", state=oura.make_state(TEST_USER_ID))
                assert res.headers["location"] == "/settings?oura=missing_scope"
            assert await _scalar(conn, "SELECT scope FROM oura_tokens") == "daily workout"  # kept

    _run(scenario())


def test_an_odd_scope_value_does_not_break_the_callback(monkeypatch):
    odd = _tokens(1)
    odd["scope"] = ["daily", "workout"]  # not the documented string
    FakeOura(tokens=[odd]).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            res = await oura_route.callback(code="c", state=oura.make_state(TEST_USER_ID))
            assert res.headers["location"] == "/settings?oura=connected"
            assert await _stored_tokens(conn) == ("acc-1", "ref-1")
            assert await _scalar(conn, "SELECT scope FROM oura_tokens") is None

    _run(scenario())


def test_reconnecting_re_reads_the_whole_stored_period(monkeypatch):
    # connected without Workout; days kept syncing for months; then Reconnect
    # grants Workout: its history must arrive, not just the last few days
    fake = FakeOura(
        tokens=[_tokens(1, scope="daily"), _tokens(2)], forbidden=("workout",)
    ).install(monkeypatch)

    def last_workout_window() -> dict:
        return [c for c in fake.data_calls if c[0].endswith("/workout")][-1][1]

    async def scenario():
        async with isolated_db() as conn:
            res = await oura_route.callback(code="c1", state=oura.make_state(TEST_USER_ID))
            assert res.headers["location"] == "/settings?oura=missing_scope"
            today = await oura_route._today()
            oldest = today - timedelta(days=200)
            for day in (oldest, today):
                await conn.execute("INSERT INTO oura_days (day, raw) VALUES (%s, '{}'::jsonb)", (day,))

            await oura.sync(today=today, force=True)  # an ordinary sync: the last few days
            assert _calls(fake, "daily_activity")[-2]["start_date"] == (
                today - timedelta(days=oura.COLLECTIONS[0][3])
            ).isoformat()

            fake.forbidden.clear()  # the owner allows Workout this time
            # the whole stored period, whatever else the fake has stored by now
            stored_oldest = await _scalar(conn, "SELECT min(day) FROM oura_days")
            res = await oura_route.callback(code="c2", state=oura.make_state(TEST_USER_ID))
            assert res.headers["location"] == "/settings?oura=connected"
            start = date.fromisoformat(last_workout_window()["start_date"])
            assert start == min(stored_oldest, today - timedelta(days=oura.BACKFILL_DAYS))
            assert start <= oldest
            assert await _scalar(conn, "SELECT count(*) FROM oura_workouts") == 1
            assert (await oura_route.get_status()).missing == []

    _run(scenario())


def test_every_collection_refused_is_an_error_and_stores_nothing(monkeypatch):
    FakeOura(forbidden=("daily_activity", "workout")).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            with pytest.raises(HTTPException) as exc:
                await oura_route.post_sync(True)
            assert exc.value.status_code == 502
            assert await _scalar(conn, "SELECT count(*) FROM oura_days") == 0
            assert await _scalar(conn, "SELECT last_synced_at FROM oura_tokens") is None

    _run(scenario())


def test_a_network_failure_during_sync_is_a_502_not_a_500(monkeypatch):
    FakeOura(data_error=httpx.ConnectError).install(monkeypatch)

    async def scenario():
        async with isolated_db():
            await oura.save_tokens(_tokens(1))
            with pytest.raises(HTTPException) as exc:
                await oura_route.post_sync(True)
            assert exc.value.status_code == 502 and "ConnectError" in exc.value.detail

    _run(scenario())


def test_sync_throttles_then_forces_and_updates_in_place(monkeypatch):
    fake = FakeOura().install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            today = date(2026, 9, 21)
            r1 = await oura.sync(today=today)
            assert (r1.skipped, r1.days, r1.workouts, r1.missing) == (False, 2, 1, [])

            r2 = await oura.sync(today=today)  # under half an hour later
            assert r2.skipped and len(fake.data_calls) == 3

            # Oura revises the day and the workout as the ring reports in
            fake.day_pages[1]["data"][0]["total_calories"] = 2950
            fake.workouts["data"][0]["calories"] = 230
            r3 = await oura.sync(today=today, force=True)
            assert not r3.skipped
            # later syncs re-read each collection behind its own newest row:
            # 3 days for days, 14 for workouts (a workout logged late)
            assert _calls(fake, "daily_activity")[2]["start_date"] == (
                date(2026, 9, 20) - timedelta(days=3)
            ).isoformat()
            assert _calls(fake, "workout")[1]["start_date"] == (
                date(2026, 9, 19) - timedelta(days=14)
            ).isoformat()
            assert await _scalar(conn, "SELECT count(*) FROM oura_days") == 2  # no duplicates
            assert await _scalar(conn, "SELECT count(*) FROM oura_workouts") == 1
            assert await _scalar(
                conn, "SELECT total_calories FROM oura_days WHERE day = '2026-09-20'"
            ) == 2950
            assert await _scalar(
                conn, "SELECT (raw->>'total_calories')::int FROM oura_days WHERE day = '2026-09-20'"
            ) == 2950
            assert float(await _scalar(conn, "SELECT calories FROM oura_workouts")) == 230.0

    _run(scenario())


def test_a_failed_sync_backs_off_until_forced(monkeypatch):
    fake = FakeOura(data_error=httpx.ConnectError).install(monkeypatch)

    async def scenario():
        async with isolated_db():
            await oura.save_tokens(_tokens(1))
            today = date(2026, 9, 21)
            with pytest.raises(oura.OuraError):
                await oura.sync(today=today)
            tried = len(fake.data_calls)
            r = await oura.sync(today=today)  # an open right after: not asked again
            assert r.skipped and len(fake.data_calls) == tried
            with pytest.raises(oura.OuraError):
                await oura.sync(today=today, force=True)  # Sync now still asks
            assert len(fake.data_calls) > tried

            fake.data_error = None  # Oura is back
            assert not (await oura.sync(today=today, force=True)).skipped
            assert oura._failed_at == {}  # a success clears the backoff

    _run(scenario())


def test_a_sync_past_its_budget_keeps_what_arrived(monkeypatch):
    monkeypatch.setattr(oura, "SYNC_BUDGET_S", -1)  # out of time after the first page
    FakeOura().install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            r = await oura.sync(today=date(2026, 9, 21), force=True)
            assert (r.days, r.workouts) == (1, 1)  # page 1 of the days, all of the workouts
            assert await _scalar(conn, "SELECT count(*) FROM oura_days") == 1

    _run(scenario())


def test_an_unreadable_document_is_skipped_not_fatal(monkeypatch):
    fake = FakeOura().install(monkeypatch)
    fake.day_pages[0]["data"] += [{"id": "no-day"}, {"id": "bad", "day": "yesterday"}]
    fake.workouts["data"].append({"day": "2026-09-19"})  # no id

    async def scenario():
        async with isolated_db() as conn:
            odd = _tokens(1)
            odd["expires_in"] = "soon"  # not a number: the documented day, not a crash
            await oura.save_tokens(odd)
            assert await _scalar(
                conn, "SELECT expires_at > now() + interval '23 hours' FROM oura_tokens"
            )
            r = await oura.sync(today=date(2026, 9, 21), force=True)
            assert (r.days, r.workouts) == (2, 1)
            assert await _scalar(conn, "SELECT count(*) FROM oura_days") == 2

    _run(scenario())


def test_a_refused_retry_tells_the_owner_to_reconnect(monkeypatch):
    FakeOura(tokens=[_tokens(2)], always_401=True).install(monkeypatch)

    async def scenario():
        async with isolated_db():
            await oura.save_tokens(_tokens(1))
            with pytest.raises(HTTPException) as exc:
                await oura_route.post_sync(True)
            assert exc.value.status_code == 502 and "reconnect" in exc.value.detail

    _run(scenario())


def test_me_says_whether_oura_is_connected(monkeypatch):
    async def scenario():
        async with isolated_db() as conn:
            owner = {"id": TEST_USER_ID, "email": "test", "is_owner": True}
            assert (await me_route.get_me(owner)).oura_connected is False
            await oura.save_tokens(_tokens(1))
            assert (await me_route.get_me(owner)).oura_connected is True
            monkeypatch.delenv("OURA_CLIENT_SECRET")
            assert (await me_route.get_me(owner)).oura_connected is False  # not usable here
            monkeypatch.setenv("OURA_CLIENT_SECRET", SECRET)
            demo = await _become(conn, "oura-demo", is_owner=False)
            assert (await me_route.get_me(demo)).oura_connected is False

    _run(scenario())


def test_refresh_keeps_the_old_refresh_token_unless_oura_sends_a_new_one(monkeypatch):
    fake = FakeOura(tokens=[_tokens(2, refresh=False), _tokens(3)]).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            await _expire(conn)
            assert await oura._access_token() == "acc-2"
            assert fake.token_forms[0]["grant_type"] == "refresh_token"
            assert fake.token_forms[0]["refresh_token"] == "ref-1"
            assert await _stored_tokens(conn) == ("acc-2", "ref-1")  # no new one sent: kept

            await _expire(conn)
            assert await oura._access_token() == "acc-3"
            assert await _stored_tokens(conn) == ("acc-3", "ref-3")  # rotated: the new one kept

            assert await oura._access_token() == "acc-3"  # fresh now: no third refresh
            assert len(fake.token_forms) == 2

    _run(scenario())


def test_losing_a_refresh_race_to_a_rotation_keeps_the_winners_tokens(monkeypatch):
    async def other_request_refreshes_first():
        await oura.save_tokens(_tokens(9))

    # the winner spent ref-1, so Oura calls our copy of it invalid_grant
    FakeOura(tokens=[400], on_token=other_request_refreshes_first).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            await _expire(conn)
            assert await oura._access_token() == "acc-9"
            assert await _stored_tokens(conn) == ("acc-9", "ref-9")  # not deleted

    _run(scenario())


def test_losing_a_refresh_race_without_rotation_keeps_the_stored_tokens(monkeypatch):
    async def other_request_refreshes_first():
        await oura.save_tokens(_tokens(9))

    FakeOura(tokens=[_tokens(2)], on_token=other_request_refreshes_first).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            await _expire(conn)
            assert await oura._access_token() == "acc-9"
            assert await _stored_tokens(conn) == ("acc-9", "ref-9")  # ours did not overwrite it

    _run(scenario())


def test_an_invalid_grant_waits_for_a_concurrent_refresh_that_has_not_saved_yet(monkeypatch):
    # The order a rotation race usually takes: A is still at Oura when B spends
    # the same refresh token, gets invalid_grant, and is handled first. B must
    # not delete the row; it waits, then takes A's tokens.
    monkeypatch.setattr(oura, "_RACE_WAIT_S", 5.0)
    loser: dict = {}

    async def a_is_at_oura():
        if "b" not in loser:  # only on A's call
            loser["b"] = asyncio.create_task(oura._access_token())
            await asyncio.sleep(0.3)  # B spends ref-1, gets invalid_grant, starts waiting

    fake = FakeOura(tokens=[400, _tokens(2)], on_token=a_is_at_oura).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            await _expire(conn)
            assert await oura._access_token() == "acc-2"  # A, the winner
            assert await loser["b"] == "acc-2"  # B, the loser, kept the connection
            assert [f["refresh_token"] for f in fake.token_forms] == ["ref-1", "ref-1"]
            assert await _stored_tokens(conn) == ("acc-2", "ref-2")

    _run(scenario())


def test_a_refresh_does_not_resurrect_a_connection_removed_meanwhile(monkeypatch):
    async def disconnected_meanwhile():
        await db.execute("DELETE FROM oura_tokens")

    FakeOura(tokens=[_tokens(2)], on_token=disconnected_meanwhile).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            await _expire(conn)
            with pytest.raises(oura.NotConnected):
                await oura._access_token()
            assert await _scalar(conn, "SELECT count(*) FROM oura_tokens") == 0

    _run(scenario())


def test_a_revoked_refresh_token_ends_the_connection(monkeypatch):
    FakeOura(tokens=[400]).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            await _expire(conn)
            with pytest.raises(oura.NotConnected):
                await oura._access_token()
            assert await _scalar(conn, "SELECT count(*) FROM oura_tokens") == 0

    _run(scenario())


def test_a_failed_refresh_keeps_the_connection(monkeypatch):
    # not invalid_grant — Oura is having a moment: a 503, a timeout, an HTML page
    FakeOura(tokens=[503, httpx.ReadTimeout, "<html>bad gateway</html>"]).install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            await oura.save_tokens(_tokens(1))
            await _expire(conn)
            for _ in range(3):
                with pytest.raises(oura.OuraError) as exc:
                    await oura._access_token()
                assert not isinstance(exc.value, oura.NotConnected)
                assert await _stored_tokens(conn) == ("acc-1", "ref-1")  # untouched

    _run(scenario())


def test_a_401_on_data_forces_one_refresh_and_retries(monkeypatch):
    fake = FakeOura(tokens=[_tokens(2)], data_401_once=True).install(monkeypatch)

    async def scenario():
        async with isolated_db():
            await oura.save_tokens(_tokens(1))
            r = await oura.sync(today=date(2026, 9, 21), force=True)
            assert (r.days, r.workouts) == (2, 1)
            assert fake.data_calls[0][2] == "Bearer acc-1"  # refused
            assert fake.data_calls[-1][2] == "Bearer acc-2"  # retried after the refresh

    _run(scenario())


# ---------------------------------------------------------------------------
# 3. the routes: owner only, status, not-migrated
# ---------------------------------------------------------------------------


def test_routes_are_owner_only(monkeypatch):
    FakeOura().install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            demo = await _become(conn, "oura-demo", is_owner=False)
            assert (await oura_route.get_status()) == oura_route.OuraStatus(
                available=False, connected=False
            )
            for call in (lambda: oura_route.authorize(demo), lambda: oura_route.post_sync(False)):
                with pytest.raises(HTTPException) as exc:
                    await call()
                assert exc.value.status_code == 403

    _run(scenario())


def test_status_and_sync_before_and_after_connecting(monkeypatch):
    FakeOura().install(monkeypatch)

    async def scenario():
        async with isolated_db() as conn:
            s = await oura_route.get_status()
            assert s.available and not s.connected and s.days == [] and s.missing == []
            with pytest.raises(HTTPException) as exc:
                await oura_route.post_sync(False)
            assert exc.value.status_code == 409  # not connected yet

            await oura.save_tokens(_tokens(1))
            synced = await oura_route.post_sync(True)
            assert not synced.skipped and synced.last_synced_at is not None
            s = await oura_route.get_status()
            assert s.connected and s.last_synced_at == synced.last_synced_at and s.missing == []

            # the card's window, against days placed around the real today:
            # newest first, the last STATUS_DAYS days only
            today = await oura_route._today()
            await conn.execute("DELETE FROM oura_days")
            for back in (0, 2, oura_route.STATUS_DAYS - 1, oura_route.STATUS_DAYS, 30):
                await conn.execute(
                    "INSERT INTO oura_days (day, total_calories, steps, raw) "
                    "VALUES (%s, %s, %s, '{}'::jsonb)",
                    (today - timedelta(days=back), 2500 + back, 7000 + back),
                )
            s = await oura_route.get_status()
            assert [d.day for d in s.days] == [
                today, today - timedelta(days=2), today - timedelta(days=oura_route.STATUS_DAYS - 1)
            ]
            assert (s.days[1].total_calories, s.days[1].steps) == (2502, 7002)

    _run(scenario())


def test_the_days_targets_grow_by_ouras_activity_burn_while_connected():
    async def scenario():
        async with isolated_db() as conn:
            day = date(2026, 9, 19)

            async def set_targets(kcal):
                value = {"kcal": kcal, "protein_g": 204, "carb_g": 78, "fat_g": 82} if kcal else {}
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('targets', %s) "
                    "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                    (Json(value),),
                )

            await set_targets(1865)
            await conn.execute(
                "INSERT INTO oura_days (day, total_calories, active_calories, raw) "
                "VALUES (%s, 3830, 1187, '{}'::jsonb)",
                (day,),
            )
            t = await diary_route.compute_day_totals(day)
            assert (t.targets.kcal, t.exercise_kcal) == (1865, 0)  # synced once, now disconnected

            await oura.save_tokens(_tokens(1))
            t = await diary_route.compute_day_totals(day)
            assert t.exercise_kcal == 1187
            # the extra kcal is carbs (1187 / 4 = 296.75 → 297 g); protein and fat stay put
            assert (t.targets.kcal, t.targets.carb_g) == (3052, 78 + 297)
            assert (t.targets.protein_g, t.targets.fat_g) == (204, 82)

            # a day Oura has nothing for, and a day it has no activity number for
            t = await diary_route.compute_day_totals(day + timedelta(days=1))
            assert (t.targets.kcal, t.exercise_kcal) == (1865, 0)
            await conn.execute("UPDATE oura_days SET active_calories = NULL")
            t = await diary_route.compute_day_totals(day)
            assert (t.targets.kcal, t.exercise_kcal) == (1865, 0)

            # no goals set: no target to raise, so none appears
            await conn.execute("UPDATE oura_days SET active_calories = 1187")
            await set_targets(None)
            t = await diary_route.compute_day_totals(day)
            assert (t.targets.kcal, t.targets.carb_g, t.exercise_kcal) == (0, 0, 0)

            # before 005 the diary still loads, with the plain target. Patched
            # by hand, as in the test below: isolated_db swaps db.fetchall itself.
            await set_targets(1865)
            real = db.fetchall

            async def fetchall(sql, params=()):
                if "oura_" in sql:
                    raise psycopg.errors.UndefinedTable('relation "oura_days" does not exist')
                return await real(sql, params)

            db.fetchall = fetchall
            try:
                t = await diary_route.compute_day_totals(day)
            finally:
                db.fetchall = real
            assert (t.targets.kcal, t.exercise_kcal) == (1865, 0)

            # another account sees none of the owner's Oura data
            await _become(conn, "oura-demo", is_owner=False)
            await set_targets(1865)
            t = await diary_route.compute_day_totals(day)
            assert (t.targets.kcal, t.exercise_kcal) == (1865, 0)

    _run(scenario())


def test_history_gives_each_day_its_own_target_with_ouras_burn():
    async def scenario():
        async with isolated_db() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('targets', %s) "
                "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                (Json({"kcal": 1865, "protein_g": 204, "carb_g": 78, "fat_g": 82}),),
            )
            await oura.save_tokens(_tokens(1))
            today = await oura_route._today()
            yesterday = today - timedelta(days=1)
            await conn.execute(
                "INSERT INTO oura_days (day, active_calories, raw) VALUES (%s, 1187, '{}'::jsonb)",
                (yesterday,),
            )
            for rng in ("3d", "w", "m"):
                h = await history_route.get_history(rng=rng)
                assert h.targets.kcal == 1865  # the range's target line stays the plain one
                by_day = {d.date: d.targets for d in h.days}
                assert (by_day[yesterday].kcal, by_day[yesterday].carb_g) == (3052, 78 + 297)
                assert (by_day[today].kcal, by_day[today].carb_g) == (1865, 78)
            assert (await history_route.get_history(rng="y")).days == []

    _run(scenario())


def test_before_migration_005_the_card_says_oura_is_unavailable():
    async def scenario():
        async with isolated_db():
            # only the Oura tables go missing; settings (the timezone) still reads.
            # Patched and restored by hand: isolated_db swaps db.fetchone itself,
            # and a monkeypatch undo after it would reinstate a dead closure.
            real = db.fetchone

            async def fetchone(sql, params=()):
                if "oura_" in sql:
                    raise psycopg.errors.UndefinedTable('relation "oura_tokens" does not exist')
                return await real(sql, params)

            db.fetchone = fetchone
            try:
                with pytest.raises(HTTPException) as exc:
                    await oura_route.get_status()
                assert exc.value.status_code == 503
                assert exc.value.detail == "Oura isn't available on this server yet."
            finally:
                db.fetchone = real

    _run(scenario())
