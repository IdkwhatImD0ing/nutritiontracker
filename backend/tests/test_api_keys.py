"""Personal API keys (migration 006): api_keys.py, auth.py's key branch, routes/keys.py.

Offline (sections 1-2): the key format, and require_user /
require_session_user over TestClient with the database lookup faked — a key
row is whatever the fake says api_key_auth returned. The JWT path must be
untouched: a token still never reaches the database.

Against the database (sections 3-4): every test runs inside a transaction
that is ROLLED BACK (the isolated_db harness; tenancy_harness applies 006
inside it while it is not yet applied). The /api/keys handlers are called
directly under two tenants, and api_key_auth is called the way a keyed
request starts: as the app role with NO tenant bound. Its keys belong to the
real owner account — the function joins neon_auth for the email, so a made-up
tenant would name nobody — and roll back with the test.

Run:  uv run --with pytest pytest backend/tests/test_api_keys.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from contextlib import asynccontextmanager
from datetime import timedelta

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from pydantic import ValidationError
from starlette.requests import Request

import api_keys
import auth
import db
from routes import keys as keys_routes
from routes.keys import ApiKeyCreate
from tenancy_harness import bind_test_tenant, switch_tenant
from test_auth import BASE, _keypair, _token

ALLOWED = auth.DEFAULT_ALLOWED_EMAIL
INVALID = {"detail": "invalid API key"}


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. the format: generate / parse / hash / redact
# ---------------------------------------------------------------------------


def test_generate_parse_hash_redact():
    key_id, plaintext = api_keys.generate()
    assert re.fullmatch(r"tally_live_[0-9a-f]{16}_[0-9a-f]{64}", plaintext)
    assert plaintext.startswith(f"tally_live_{key_id}_")
    assert api_keys.parse(plaintext) == key_id
    assert api_keys.generate()[1] != plaintext  # random every time

    # the stored form: SHA-256 hex of the FULL plaintext, nothing else mixed in
    assert api_keys.hash_key(plaintext) == hashlib.sha256(plaintext.encode()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", api_keys.hash_key(plaintext))

    shown = api_keys.redact(key_id, plaintext[-4:])
    assert shown == f"tally_live_{key_id}_…{plaintext[-4:]}"
    assert plaintext.split("_")[-1] not in shown  # the secret never appears


@pytest.mark.parametrize(
    "token",
    [
        "tally_live_" + "a" * 16 + "_" + "b" * 63,  # secret one short
        "tally_live_" + "a" * 16 + "_" + "b" * 65,  # one long
        "tally_live_" + "a" * 15 + "_" + "b" * 64,  # id one short
        "tally_live_" + "A" * 16 + "_" + "b" * 64,  # uppercase
        "tally_live_" + "g" * 16 + "_" + "b" * 64,  # not hex
        "tally_test_" + "a" * 16 + "_" + "b" * 64,  # another family member
        "tally_live_" + "a" * 16 + "_" + "b" * 64 + "\n",  # `$` would have let this in
        "x" + "tally_live_" + "a" * 16 + "_" + "b" * 64,
        "",
    ],
)
def test_parse_rejects_malformed(token):
    assert api_keys.parse(token) is None


# ---------------------------------------------------------------------------
# 2. auth over HTTP, the lookup faked
# ---------------------------------------------------------------------------


@pytest.fixture()
def key_app(monkeypatch):
    """TestClient over a minimal app: GET and every write method behind
    require_user, GET /session behind require_session_user. db.fetchone is
    the fake api_key_auth: a registered (id, hash) pair answers its row."""
    monkeypatch.setenv("NEON_AUTH_BASE_URL", BASE)
    for name in ("ALLOWED_EMAIL", "APP_ORIGIN", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_dotenv_loaded", True)  # keep .env out of _env()
    monkeypatch.setattr(auth, "_last_failed_fetch", float("-inf"))
    auth._keys.clear()

    state = {"rows": {}, "lookups": [], "jwks": {"keys": []}}

    async def fake_fetchone(sql, params=()):
        assert sql == "SELECT * FROM api_key_auth(%s, %s)"
        state["lookups"].append((params, db.current_user_id.get()))
        return state["rows"].get(params)

    async def fake_fetch_jwks():
        return state["jwks"]

    monkeypatch.setattr(db, "fetchone", fake_fetchone)
    monkeypatch.setattr(auth, "_fetch_jwks", fake_fetch_jwks)

    def add_key(scopes=("read", "write"), email="demo@example.test", verified=True, user_id="user-k"):
        key_id, plaintext = api_keys.generate()
        state["rows"][(key_id, api_keys.hash_key(plaintext))] = {
            "user_id": user_id, "email": email, "email_verified": verified, "scopes": list(scopes),
        }
        return plaintext

    state["add_key"] = add_key

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: dict = Depends(auth.require_user)):
        return {**user, "scopes": sorted(user["scopes"])}

    @app.api_route("/write", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def write(user: dict = Depends(auth.require_user)):
        return {"ok": True}

    @app.get("/session")
    async def session(user: dict = Depends(auth.require_session_user)):
        return {"via": user["via"]}

    yield TestClient(app), state
    auth._keys.clear()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_valid_key_is_the_keys_user(key_app):
    client, state = key_app
    key = state["add_key"](scopes=("read",), email="demo@example.test")
    r = client.get("/whoami", headers=_bearer(key))
    assert r.status_code == 200
    assert r.json() == {
        "id": "user-k", "email": "demo@example.test", "is_owner": False,
        "via": "api_key", "scopes": ["read"], "key_id": api_keys.parse(key),
    }
    # the lookup got the id and the hash, never the plaintext
    [(params, _)] = state["lookups"]
    assert params == (api_keys.parse(key), api_keys.hash_key(key))

    # the owner check reads the account's email, as the JWT path reads the claim
    owner_key = state["add_key"](email=ALLOWED.upper(), user_id="owner-k")
    r = client.get("/whoami", headers=_bearer(owner_key))
    assert r.status_code == 200 and r.json()["is_owner"] is True


def test_malformed_unknown_and_wrong_secret_are_the_same_401(key_app):
    client, state = key_app
    key = state["add_key"]()
    key_id = api_keys.parse(key)
    tokens = {
        "malformed": "tally_live_not-a-key",
        "other family": "tally_test_" + "a" * 16 + "_" + "b" * 64,
        "unknown": api_keys.generate()[1],
        "wrong secret": f"tally_live_{key_id}_{'0' * 64}",
    }
    for why, token in tokens.items():
        r = client.get("/whoami", headers=_bearer(token))
        assert (r.status_code, r.json()) == (401, INVALID), why
    # only the well-formed ones cost a lookup
    assert len(state["lookups"]) == 2


def test_read_only_key_cannot_write(key_app):
    client, state = key_app
    read_only = state["add_key"](scopes=("read",))
    read_write = state["add_key"](scopes=("read", "write"))
    assert client.get("/whoami", headers=_bearer(read_only)).status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = client.request(method, "/write", headers=_bearer(read_only))
        assert (r.status_code, r.json()) == (403, {"detail": "this API key is read-only"}), method
        r = client.request(method, "/write", headers=_bearer(read_write))
        assert r.status_code == 200, method


def test_key_of_an_unverified_account_403(key_app):
    client, state = key_app
    key = state["add_key"](verified=False)
    r = client.get("/whoami", headers=_bearer(key))
    assert (r.status_code, r.json()) == (403, {"detail": "email not verified"})


def test_key_lookup_runs_with_no_tenant_bound(key_app):
    """conftest binds the test tenant for every test; a stale binding must
    not ride into the lookup, and the key's user is bound after it."""
    _, state = key_app
    key = state["add_key"](user_id="user-k")
    assert db.current_user_id.get() == "test-user"
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                       "headers": [(b"authorization", f"Bearer {key}".encode())]})
    user = _run(auth.authenticate(request))
    [(_, bound_during_lookup)] = state["lookups"]
    assert bound_during_lookup is None
    assert user["id"] == "user-k" and user["via"] == "api_key"


def test_jwt_path_unchanged(key_app):
    client, state = key_app
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    tok = _token(priv, "kid-a")
    r = client.get("/whoami", headers=_bearer(tok))
    assert r.status_code == 200
    assert r.json() == {
        "id": "user-123", "email": ALLOWED, "is_owner": True,
        "via": "session", "scopes": ["read", "write"],
    }
    assert client.post("/write", headers=_bearer(tok)).status_code == 200
    r = client.get("/whoami", headers=_bearer("not-a-jwt"))
    assert (r.status_code, r.json()) == (401, {"detail": "invalid token"})
    assert state["lookups"] == []  # a JWT never reaches the key lookup


def test_session_only_dependency_refuses_keys(key_app):
    client, state = key_app
    key = state["add_key"]()
    r = client.get("/session", headers=_bearer(key))
    assert (r.status_code, r.json()) == (
        403, {"detail": "API keys cannot manage API keys; sign in to the app"}
    )
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    r = client.get("/session", headers=_bearer(_token(priv, "kid-a")))
    assert (r.status_code, r.json()) == (200, {"via": "session"})


def test_keys_router_is_session_only():
    deps = [d.dependency for d in keys_routes.router.dependencies]
    assert deps == [auth.require_session_user]


def test_create_body_rules():
    assert ApiKeyCreate(label="  laptop  ").label == "laptop"  # stripped
    assert ApiKeyCreate(label="x").scopes == "read_write"  # the default
    for bad in ({"label": "   "}, {"label": "x" * 81}, {"label": "x", "scopes": "write"},
                {"label": "x", "expires_in_days": 7}):
        with pytest.raises(ValidationError):
            ApiKeyCreate(**bad)


# ---------------------------------------------------------------------------
# rollback harness (same shape as the other test modules)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
    await bind_test_tenant(conn)  # 002/003/006 if absent + app role + the fixed test tenant
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


async def _one(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def _become_owner(conn) -> str:
    """The real owner account as the tenant (the app role cannot read
    neon_auth, so look it up as the connecting role first)."""
    await conn.execute("SET LOCAL ROLE NONE")
    row = await _one(
        conn, 'SELECT id::text AS id FROM neon_auth."user" WHERE lower(email) = lower(%s)', (ALLOWED,)
    )
    assert row is not None, "the owner account must exist in neon_auth"
    await switch_tenant(conn, row["id"])
    return row["id"]


async def _no_tenant(conn) -> None:
    """How a keyed request starts: the app role, app.user_id = ''."""
    await conn.execute(db.tenant_sql(None))
    db.current_user_id.set(None)


async def _auth(conn, key_id, key_hash):
    cur = await conn.execute("SELECT * FROM api_key_auth(%s, %s)", (key_id, key_hash))
    return await cur.fetchall()


async def _owner_key(conn, label: str, scopes: str = "read_write") -> keys_routes.ApiKeyCreated:
    """A key for the bound tenant by direct INSERT, as verify_006 does: create_key's
    10-key cap would count the owner's REAL keys once 006 is applied."""
    key_id, secret = api_keys.generate()
    row = await _one(
        conn,
        f"INSERT INTO api_keys (id, label, key_hash, last_four, scopes) "
        f"VALUES (%s, %s, %s, %s, %s) RETURNING {keys_routes._COLS}",
        (key_id, label, api_keys.hash_key(secret), secret[-4:], keys_routes.SCOPES[scopes]),
    )
    return keys_routes.ApiKeyCreated(key=keys_routes._out(row), secret=secret)


# ---------------------------------------------------------------------------
# 3. /api/keys under two tenants
# ---------------------------------------------------------------------------


def test_create_list_revoke_and_isolation():
    async def run():
        async with isolated_db() as conn:
            await switch_tenant(conn, "keys-a")
            made = await keys_routes.create_key(ApiKeyCreate(label="  laptop  ", scopes="read"))
            key = made.key
            assert api_keys.parse(made.secret) == key.id
            assert (key.label, key.scopes, key.expires_at, key.revoked_at, key.last_used_at) == (
                "laptop", ["read"], None, None, None
            )
            assert key.redacted == api_keys.redact(key.id, made.secret[-4:])
            stored = await _one(conn, "SELECT user_id, key_hash, last_four FROM api_keys WHERE id = %s", (key.id,))
            assert stored == {
                "user_id": "keys-a", "key_hash": api_keys.hash_key(made.secret), "last_four": made.secret[-4:],
            }

            later = (await keys_routes.create_key(ApiKeyCreate(label="agent", expires_in_days=30))).key
            assert later.scopes == ["read", "write"]
            assert later.expires_at - later.created_at == timedelta(days=30)
            # one transaction = one now(): age the first key so "newest first" is visible
            await conn.execute("UPDATE api_keys SET created_at = created_at - interval '1 hour' WHERE id = %s", (key.id,))
            assert [k.id for k in await keys_routes.list_keys()] == [later.id, key.id]

            revoked = await keys_routes.revoke_key(key.id)
            assert revoked.revoked_at is not None
            # one transaction = one now(): age the revocation so a second revoke that overwrote it would show
            await conn.execute("UPDATE api_keys SET revoked_at = revoked_at - interval '1 hour' WHERE id = %s", (key.id,))
            again = await keys_routes.revoke_key(key.id)  # idempotent: same row, first time kept
            assert again.revoked_at == revoked.revoked_at - timedelta(hours=1)
            listed = {k.id: k for k in await keys_routes.list_keys()}
            assert listed[key.id].revoked_at == again.revoked_at  # revoked keys stay listed

            # another tenant: sees none of it, cannot revoke it, has its own
            await switch_tenant(conn, "keys-b")
            assert await keys_routes.list_keys() == []
            with pytest.raises(HTTPException) as exc:
                await keys_routes.revoke_key(later.id)
            assert exc.value.status_code == 404
            theirs = (await keys_routes.create_key(ApiKeyCreate(label="b"))).key
            assert [k.id for k in await keys_routes.list_keys()] == [theirs.id]

            await switch_tenant(conn, "keys-a")
            listed = {k.id: k for k in await keys_routes.list_keys()}
            assert set(listed) == {key.id, later.id}
            assert listed[later.id].revoked_at is None  # B's attempt changed nothing

    _run(run())


def test_cap_counts_only_active_keys():
    async def run():
        async with isolated_db() as conn:
            await switch_tenant(conn, "keys-cap")
            made = [
                (await keys_routes.create_key(ApiKeyCreate(label=f"k{i}"))).key
                for i in range(keys_routes.MAX_ACTIVE_KEYS)
            ]
            with pytest.raises(HTTPException) as exc:
                await keys_routes.create_key(ApiKeyCreate(label="one too many"))
            assert exc.value.status_code == 409

            await keys_routes.revoke_key(made[0].id)  # a revoked key frees a slot
            await keys_routes.create_key(ApiKeyCreate(label="after revoke"))
            with pytest.raises(HTTPException) as exc:
                await keys_routes.create_key(ApiKeyCreate(label="full again"))
            assert exc.value.status_code == 409

            await conn.execute(  # so does an expired one
                "UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = %s", (made[1].id,)
            )
            await keys_routes.create_key(ApiKeyCreate(label="after expiry"))

            # the cap is per user: another tenant starts empty
            await switch_tenant(conn, "keys-cap-other")
            await keys_routes.create_key(ApiKeyCreate(label="mine"))

    _run(run())


# ---------------------------------------------------------------------------
# 4. api_key_auth: the lookup with no tenant bound
# ---------------------------------------------------------------------------


def test_api_key_auth_names_only_live_keys(monkeypatch):
    monkeypatch.delenv("ALLOWED_EMAIL", raising=False)  # the owner is the default address
    monkeypatch.setattr(auth, "_dotenv_loaded", True)

    async def run():
        async with isolated_db() as conn:
            owner_id = await _become_owner(conn)
            live = await _owner_key(conn, "live", "read")
            revoked = await _owner_key(conn, "revoked")
            expired = await _owner_key(conn, "expired")
            await keys_routes.revoke_key(revoked.key.id)
            await conn.execute(
                "UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = %s", (expired.key.id,)
            )

            await _no_tenant(conn)
            assert (await _one(conn, "SELECT count(*) AS n FROM api_keys"))["n"] == 0  # RLS still holds
            [row] = await _auth(conn, live.key.id, api_keys.hash_key(live.secret))
            assert row == {"user_id": owner_id, "email": row["email"], "email_verified": True, "scopes": ["read"]}
            assert row["email"].lower() == ALLOWED
            assert await _auth(conn, live.key.id, api_keys.hash_key(revoked.secret)) == []  # wrong hash
            assert await _auth(conn, revoked.key.id, api_keys.hash_key(revoked.secret)) == []
            assert await _auth(conn, expired.key.id, api_keys.hash_key(expired.secret)) == []

            # auth.authenticate end to end through the same SQL (db.fetchone is the harness's)
            request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                               "headers": [(b"authorization", f"Bearer {live.secret}".encode())]})
            user = await auth.authenticate(request)
            assert (user["id"], user["via"], user["scopes"], user["is_owner"]) == (
                owner_id, "api_key", frozenset({"read"}), True
            )
            with pytest.raises(HTTPException) as exc:
                await auth.authenticate(Request({**request.scope, "headers": [
                    (b"authorization", f"Bearer {revoked.secret}".encode())]}))
            assert (exc.value.status_code, exc.value.detail) == (401, "invalid API key")

    _run(run())


def test_api_key_auth_stamps_last_used_at_every_five_minutes():
    async def run():
        async with isolated_db() as conn:
            owner_id = await _become_owner(conn)
            made = await _owner_key(conn, "agent")
            key_id, key_hash = made.key.id, api_keys.hash_key(made.secret)

            async def call_then_age() -> timedelta | None:
                """Call as a keyed request would, then read last_used_at's
                age as the owner (now() is the transaction's start)."""
                await _no_tenant(conn)
                assert len(await _auth(conn, key_id, key_hash)) == 1
                await switch_tenant(conn, owner_id)
                row = await _one(conn, "SELECT now() - last_used_at AS age FROM api_keys WHERE id = %s", (key_id,))
                return row["age"]

            assert made.key.last_used_at is None
            assert await call_then_age() == timedelta(0)  # first use: stamped
            await conn.execute("UPDATE api_keys SET last_used_at = now() - interval '4 minutes' WHERE id = %s", (key_id,))
            assert await call_then_age() == timedelta(minutes=4)  # recent: left alone
            await conn.execute("UPDATE api_keys SET last_used_at = now() - interval '6 minutes' WHERE id = %s", (key_id,))
            assert await call_then_age() == timedelta(0)  # stale: stamped again

    _run(run())
