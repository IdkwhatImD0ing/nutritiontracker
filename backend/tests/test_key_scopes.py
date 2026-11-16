"""What a key may do, route by route, and the key branch's Origin and 401
rules (auth.py).

require_user decides what a read-only key may use: GET/HEAD/OPTIONS read and
every other method writes, unless the route declares its scope in its
decorator (openapi_extra={"x-tally-scope": ...}) because its method misleads.
A key skips the Origin check a session token still gets, and a bad key's 401
carries the Bearer challenge. Offline: api_key_auth is faked as in
test_api_keys.py, the JWKS as in test_auth.py; nothing reaches the database.

Run:  uv run --with pytest pytest backend/tests/test_key_scopes.py -q
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

import api_keys
import auth
import db
import main
from test_auth import ALLOWED, BASE, _keypair, _token

OK = {"ok": True}
READ_ONLY = {"detail": "this API key is read-only"}
ROUTES = (("GET", "/declared-write"), ("POST", "/declared-read"), ("GET", "/plain"), ("POST", "/plain"))


@pytest.fixture()
def scoped(monkeypatch):
    """A minimal app shaped like the real ones (an APIRouter behind
    require_user, included in the app) with one route of each kind. db.fetchone
    is the fake api_key_auth and nothing else: a handler that queried the
    database would fail the assert."""
    monkeypatch.setenv("NEON_AUTH_BASE_URL", BASE)
    monkeypatch.setenv("ALLOWED_EMAIL", ALLOWED)  # _token's default address is the owner
    for name in ("APP_ORIGIN", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL",
                 "VERCEL_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_dotenv_loaded", True)  # keep .env out of _env()
    monkeypatch.setattr(auth, "_last_failed_fetch", float("-inf"))
    auth._keys.clear()

    rows = {}
    priv, jwk = _keypair("kid-s")
    state = {"fetches": 0}

    async def fake_fetchone(sql, params=()):
        assert sql == "SELECT * FROM api_key_auth(%s, %s)"
        return rows.get(params)

    async def fake_fetch_jwks():
        state["fetches"] += 1
        return {"keys": [jwk]}

    monkeypatch.setattr(db, "fetchone", fake_fetchone)
    monkeypatch.setattr(auth, "_fetch_jwks", fake_fetch_jwks)

    def add_key(*scopes):
        key_id, plaintext = api_keys.generate()
        rows[(key_id, api_keys.hash_key(plaintext))] = {
            "user_id": "user-k", "email": "demo@example.test", "email_verified": True,
            "scopes": list(scopes),
        }
        return plaintext

    router = APIRouter(dependencies=[Depends(auth.require_user)])

    @router.get("/declared-write", openapi_extra={"x-tally-scope": "write"})
    async def declared_write():
        return OK

    @router.post("/declared-read", openapi_extra={"x-tally-scope": "read"})
    async def declared_read():
        return OK

    @router.get("/plain")
    async def plain_get():
        return OK

    @router.post("/plain")
    async def plain_post():
        return OK

    app = FastAPI()
    app.include_router(router)
    state.update(add_key=add_key, session=_token(priv, "kid-s"))
    yield TestClient(app), state
    auth._keys.clear()


def _bearer(token: str, **headers) -> dict:
    return {"Authorization": f"Bearer {token}", **headers}


def test_read_only_key_follows_the_declared_scope(scoped):
    client, state = scoped
    key = state["add_key"]("read")
    expected = {
        ("GET", "/declared-write"): (403, READ_ONLY),  # a GET that writes
        ("POST", "/declared-read"): (200, OK),  # a POST that only computes
        ("GET", "/plain"): (200, OK),  # undeclared: the method decides
        ("POST", "/plain"): (403, READ_ONLY),
    }
    for (method, path), want in expected.items():
        r = client.request(method, path, headers=_bearer(key))
        assert (r.status_code, r.json()) == want, (method, path)


def test_read_write_key_and_session_pass_every_route(scoped):
    client, state = scoped
    for who, token in (("read_write key", state["add_key"]("read", "write")),
                       ("session", state["session"])):
        for method, path in ROUTES:
            r = client.request(method, path, headers=_bearer(token))
            assert (r.status_code, r.json()) == (200, OK), (who, method, path)


def test_a_key_skips_the_origin_check_a_session_still_gets(scoped, monkeypatch):
    """A client running in a browser engine (an extension, a webview) sends
    its own Origin. With a key that is no reason to refuse it; a session token
    from a foreign Origin is still refused in production, before its JWT is
    parsed."""
    client, state = scoped
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("APP_ORIGIN", "https://usual.example.com")
    foreign = {"Origin": "https://agent.example.org"}

    r = client.post("/plain", headers=_bearer(state["add_key"]("read", "write"), **foreign))
    assert (r.status_code, r.json()) == (200, OK)
    # the key's own scope still holds from any Origin
    r = client.post("/plain", headers=_bearer(state["add_key"]("read"), **foreign))
    assert (r.status_code, r.json()) == (403, READ_ONLY)

    r = client.post("/plain", headers=_bearer(state["session"], **foreign))
    assert (r.status_code, r.json()) == (403, {"detail": "origin mismatch"})
    assert state["fetches"] == 0  # refused before any JWKS was needed
    r = client.post("/plain", headers=_bearer(state["session"], Origin="https://usual.example.com"))
    assert (r.status_code, r.json()) == (200, OK)


def test_bad_key_401_carries_the_bearer_challenge(scoped):
    """The same challenge POST /api/mcp sends (tests/test_mcp.py)."""
    client, _ = scoped
    for token in ("tally_live_nope", api_keys.generate()[1]):  # malformed, unknown
        r = client.get("/plain", headers=_bearer(token))
        assert (r.status_code, r.json()) == (401, {"detail": "invalid API key"}), token
        assert r.headers["www-authenticate"] == "Bearer", token


def test_real_routes_declare_their_scope():
    """The routes whose method misleads, read off the assembled app: the log
    poll (its watchdog) and the barcode lookup (its cache fill) write on a GET,
    goals/compute only computes on a POST. No other route declares one."""
    declared = {
        (path, method): op["x-tally-scope"]
        for path, ops in main.app.openapi()["paths"].items()
        for method, op in ops.items()
        if "x-tally-scope" in op
    }
    assert declared == {
        ("/api/logs/{log_id}", "get"): "write",
        ("/api/foods/barcode", "get"): "write",
        ("/api/settings/goals/compute", "post"): "read",
    }


def test_real_app_applies_the_declared_scope(scoped):
    """Through main.app, its routers included as production mounts them: a
    read-only key is refused the two writing GETs before their handlers run,
    and may compute goals, which reads and stores nothing."""
    _, state = scoped
    key = state["add_key"]("read")
    client = TestClient(main.app)
    for path in (f"/api/logs/{uuid4()}", "/api/foods/barcode?code=0123456789012"):
        r = client.get(path, headers=_bearer(key))
        assert (r.status_code, r.json()) == (403, READ_ONLY), path
    body = {"height_cm": 180, "weight_kg": 80, "age": 30, "sex": "male",
            "activity_level": "moderate"}
    r = client.post("/api/settings/goals/compute", headers=_bearer(key), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["targets"]["kcal"] > 0

def test_oura_connect_takes_a_session_not_a_key(scoped):
    """GET /api/oura/authorize hands out the signed state the callback stores
    Oura tokens under, so it is session-only like /api/keys: every key, even a
    read and write one, is refused before the handler runs."""
    _, state = scoped
    client = TestClient(main.app)
    for scopes in (("read",), ("read", "write")):
        r = client.get("/api/oura/authorize", headers=_bearer(state["add_key"](*scopes)))
        assert (r.status_code, r.json()) == (
            403, {"detail": "API keys cannot do this; sign in to the app"}
        ), scopes
