"""Tests for backend/auth.py (workstream G).

Offline tests generate an Ed25519 keypair and monkeypatch the JWKS fetch.
One live test fetches the real JWKS from NEON_AUTH_BASE_URL in .env.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth  # backend/ is on sys.path via tests/conftest.py

BASE = "https://auth.example.test"
ALLOWED = auth.DEFAULT_ALLOWED_EMAIL


def _keypair(kid: str) -> tuple[Ed25519PrivateKey, dict]:
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
        "kid": kid,
        "alg": "EdDSA",
    }
    return priv, jwk


def _token(
    priv: Ed25519PrivateKey,
    kid: str,
    email: str = ALLOWED,
    exp_delta: int = 900,
    iss: str = BASE,
    **extra_claims,
) -> str:
    now = int(time.time())
    payload = {"sub": "user-123", "email": email, "iss": iss, "iat": now, "exp": now + exp_delta}
    payload.update(extra_claims)
    return jwt.encode(payload, priv, algorithm="EdDSA", headers={"kid": kid})


@pytest.fixture()
def app_client(monkeypatch):
    """TestClient over a minimal app using require_user, JWKS fetch stubbed."""
    monkeypatch.setenv("NEON_AUTH_BASE_URL", BASE)
    monkeypatch.delenv("ALLOWED_EMAIL", raising=False)
    monkeypatch.delenv("APP_ORIGIN", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    monkeypatch.delenv("VERCEL_URL", raising=False)
    monkeypatch.setattr(auth, "_dotenv_loaded", True)  # keep .env out of _env()
    monkeypatch.setattr(auth, "_last_failed_fetch", float("-inf"))
    auth._keys.clear()

    state = {"jwks": {"keys": []}, "fetches": 0}

    async def fake_fetch():
        state["fetches"] += 1
        return state["jwks"]

    monkeypatch.setattr(auth, "_fetch_jwks", fake_fetch)

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: dict = Depends(auth.require_user)):
        return user

    @app.post("/write")
    async def write(user: dict = Depends(auth.require_user)):
        return {"ok": True}

    yield TestClient(app), state
    auth._keys.clear()


def test_valid_token_allowlisted_email_is_owner(app_client):
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv, 'kid-a')}"})
    assert r.status_code == 200
    body = r.json()
    assert sorted(body.pop("scopes")) == ["read", "write"]  # a session can do everything (006)
    assert body == {"id": "user-123", "email": ALLOWED, "is_owner": True, "via": "session"}


def test_other_email_is_a_demo_user(app_client):
    """Any verified account gets in (002 tenancy); only the allowlisted email
    is the owner. Sign-up is open, so the quota (services/quota.py) is what
    bounds a stranger, not the door."""
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    tok = _token(priv, "kid-a", email="mallory@example.com", sub="user-456")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.json()
    assert sorted(body.pop("scopes")) == ["read", "write"]
    assert body == {"id": "user-456", "email": "mallory@example.com", "is_owner": False, "via": "session"}


def test_token_without_email_or_sub_rejected(app_client):
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv, 'kid-a', email=None)}"})
    assert r.status_code == 403
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv, 'kid-a', sub=None)}"})
    assert r.status_code == 401


def test_unverified_email_claim_403(app_client):
    """A token whose email_verified/emailVerified claim is present-and-false is
    rejected: sign-up is public, so an unproven email claim must not pass."""
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    for claim in ("email_verified", "emailVerified"):
        tok = _token(priv, "kid-a", **{claim: False})
        r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, claim
        tok = _token(priv, "kid-a", **{claim: True})
        r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, claim
    # absent claim: allowed (claim shape belongs to the auth service)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv, 'kid-a')}"})
    assert r.status_code == 200


def test_audience_claim_is_accepted_when_it_names_the_auth_origin(app_client):
    """Neon's real tokens carry `aud` = the auth origin. PyJWT rejects any
    token with an `aud` claim unless an audience is supplied, which is how
    every production request 401'd with "invalid token" while these tests
    (tokens without `aud`) passed. The origin must be accepted, as a string
    or inside a list; a foreign audience must still be refused."""
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    hdr = lambda tok: {"Authorization": f"Bearer {tok}"}  # noqa: E731

    r = client.get("/whoami", headers=hdr(_token(priv, "kid-a", aud=BASE)))
    assert r.status_code == 200
    r = client.get("/whoami", headers=hdr(_token(priv, "kid-a", aud=[BASE, "https://x.test"])))
    assert r.status_code == 200
    r = client.get("/whoami", headers=hdr(_token(priv, "kid-a", aud="https://other.test")))
    assert r.status_code == 401
    r = client.get("/whoami", headers=hdr(_token(priv, "kid-a", aud=["https://other.test"])))
    assert r.status_code == 401


def test_expired_token_401(app_client):
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    tok = _token(priv, "kid-a", exp_delta=-60)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_bad_signature_401(app_client):
    client, state = app_client
    _, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    other_priv, _ = _keypair("kid-a")  # different key, same kid
    tok = _token(other_priv, "kid-a")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_kid_miss_triggers_refetch(app_client):
    client, state = app_client
    priv_a, jwk_a = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk_a]}

    # Cold cache: first request fetches once.
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv_a, 'kid-a')}"})
    assert r.status_code == 200
    assert state["fetches"] == 1

    # Cached kid: no extra fetch.
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv_a, 'kid-a')}"})
    assert r.status_code == 200
    assert state["fetches"] == 1

    # Rotate: new kid appears upstream; kid miss must refetch and then verify.
    priv_b, jwk_b = _keypair("kid-b")
    state["jwks"] = {"keys": [jwk_a, jwk_b]}
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv_b, 'kid-b')}"})
    assert r.status_code == 200
    assert state["fetches"] == 2

    # Unknown kid even after refetch -> 401.
    priv_c, _ = _keypair("kid-c")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv_c, 'kid-c')}"})
    assert r.status_code == 401
    assert state["fetches"] == 3


def test_refetch_cooldown_two_misses_one_fetch(app_client):
    """BACKLOG 14: after a refetch that still misses its kid, further unknown
    kids within the cool-down 401 without fetching (no amplification)."""
    client, state = app_client
    priv_a, jwk_a = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk_a]}

    # Warm the cache.
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv_a, 'kid-a')}"})
    assert r.status_code == 200
    assert state["fetches"] == 1

    # First unknown kid: one refetch, still missing -> 401, cool-down starts.
    priv_x, _ = _keypair("kid-x")
    tok_x = _token(priv_x, "kid-x")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok_x}"})
    assert r.status_code == 401
    assert state["fetches"] == 2

    # Same unknown kid again within a second: negative-cached, no fetch.
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok_x}"})
    assert r.status_code == 401
    assert state["fetches"] == 2

    # A different unknown kid within the cool-down: still no fetch.
    priv_y, _ = _keypair("kid-y")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv_y, 'kid-y')}"})
    assert r.status_code == 401
    assert state["fetches"] == 2

    # Cached kid keeps verifying during the cool-down.
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv_a, 'kid-a')}"})
    assert r.status_code == 200
    assert state["fetches"] == 2


def test_refetch_allowed_after_cooldown(app_client, monkeypatch):
    """Once the cool-down elapses, a kid miss refetches again (rotation heals)."""
    client, state = app_client
    priv_b, jwk_b = _keypair("kid-b")
    state["jwks"] = {"keys": []}
    tok = _token(priv_b, "kid-b")

    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401
    assert state["fetches"] == 1

    # Within the cool-down: no fetch.
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401
    assert state["fetches"] == 1

    # Simulate the cool-down elapsing; the rotated key is now upstream.
    monkeypatch.setattr(
        auth, "_last_failed_fetch", time.monotonic() - auth._REFETCH_COOLDOWN_S - 1
    )
    state["jwks"] = {"keys": [jwk_b]}
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert state["fetches"] == 2


def test_origin_default_from_vercel_env(app_client, monkeypatch):
    """BACKLOG 14: with APP_ORIGIN unset, the Origin allowlist defaults from
    Vercel's env, so deployed environments are fail-closed by default."""
    client, state = app_client
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "usual.example.app")
    monkeypatch.setenv("VERCEL_URL", "usual-abc123.vercel.example")
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    hdr = {"Authorization": f"Bearer {_token(priv, 'kid-a')}"}

    # Cross-site Origin on POST -> 403 (fail-closed without APP_ORIGIN).
    r = client.post("/write", headers={**hdr, "Origin": "https://evil.example.com"})
    assert r.status_code == 403

    # Both derived origins pass: production domain and deployment URL.
    r = client.post("/write", headers={**hdr, "Origin": "https://usual.example.app"})
    assert r.status_code == 200
    r = client.post("/write", headers={**hdr, "Origin": "https://usual-abc123.vercel.example"})
    assert r.status_code == 200

    # Absent Origin header -> allowed (non-browser clients), as before.
    r = client.post("/write", headers=hdr)
    assert r.status_code == 200

    # Explicit APP_ORIGIN overrides the derived default entirely.
    monkeypatch.setenv("APP_ORIGIN", "https://app.example.net")
    r = client.post("/write", headers={**hdr, "Origin": "https://usual.example.app"})
    assert r.status_code == 403
    r = client.post("/write", headers={**hdr, "Origin": "https://app.example.net"})
    assert r.status_code == 200


def test_origin_localhost_allowance_and_bare_dev(app_client, monkeypatch):
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    hdr = {"Authorization": f"Bearer {_token(priv, 'kid-a')}"}

    # Nothing configured (bare local dev): fail-open, any Origin passes.
    r = client.post("/write", headers={**hdr, "Origin": "https://evil.example.com"})
    assert r.status_code == 200

    # Deployed (Vercel env set): localhost frontend still allowed for dev...
    monkeypatch.setenv("VERCEL_URL", "usual-abc123.vercel.example")
    r = client.post("/write", headers={**hdr, "Origin": "http://localhost:3000"})
    assert r.status_code == 200
    r = client.post("/write", headers={**hdr, "Origin": "http://127.0.0.1:3000"})
    assert r.status_code == 200
    # ...but other origins are rejected.
    r = client.post("/write", headers={**hdr, "Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_missing_token_401(app_client):
    client, _ = app_client
    assert client.get("/whoami").status_code == 401


def test_origin_mismatch_on_state_changing_403(app_client, monkeypatch):
    client, state = app_client
    monkeypatch.setenv("APP_ORIGIN", "https://usual.example.com")
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    tok = _token(priv, "kid-a")

    # Mismatched Origin on POST -> 403.
    r = client.post(
        "/write",
        headers={"Authorization": f"Bearer {tok}", "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403

    # Matching Origin on POST -> 200.
    r = client.post(
        "/write",
        headers={"Authorization": f"Bearer {tok}", "Origin": "https://usual.example.com"},
    )
    assert r.status_code == 200

    # Absent Origin header -> allowed (non-browser clients).
    r = client.post("/write", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200

    # GET is not state-changing: mismatched Origin still passes.
    r = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {tok}", "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 200


def test_dependency_override_hook():
    """app.dependency_overrides works because require_user is a module function."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: dict = Depends(auth.require_user)):
        return user

    app.dependency_overrides[auth.require_user] = lambda: {"email": "t@t", "user_id": "u1"}
    r = TestClient(app).get("/whoami")
    assert r.status_code == 200
    assert r.json() == {"email": "t@t", "user_id": "u1"}


@pytest.mark.live
def test_live_jwks_has_ed25519_key():
    """LIVE: real JWKS at NEON_AUTH_BASE_URL from .env contains an Ed25519 key."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    base = None
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("NEON_AUTH_BASE_URL="):
                base = line.split("=", 1)[1].strip().strip("'\"")
                break
    if not base:
        pytest.skip("NEON_AUTH_BASE_URL not found in .env")
    resp = httpx.get(base.rstrip("/") + "/.well-known/jwks.json", timeout=15.0)
    assert resp.status_code == 200
    keys = resp.json().get("keys", [])
    assert any(k.get("kty") == "OKP" and k.get("crv") == "Ed25519" for k in keys), (
        "no Ed25519 key in live JWKS"
    )


def test_app_origin_accepts_a_comma_separated_list(monkeypatch):
    """A domain change is a cutover, not an instant: APP_ORIGIN lists both
    origins while DNS moves, then drops the old one. Without this the moment
    the value flips, every state-changing request from the old hostname 403s
    while reads keep working — 'the app loads but nothing saves'."""
    import auth

    monkeypatch.setenv("APP_ORIGIN", "https://old.example, https://new.example/")
    allowed = auth._allowed_origins()
    assert allowed == frozenset({"https://old.example", "https://new.example"})

    # A single value still behaves exactly as before, trailing slash trimmed.
    monkeypatch.setenv("APP_ORIGIN", "https://only.example/")
    assert auth._allowed_origins() == frozenset({"https://only.example"})

    # Empty entries from a trailing comma never become a wildcard "" origin.
    monkeypatch.setenv("APP_ORIGIN", "https://a.example,,")
    assert auth._allowed_origins() == frozenset({"https://a.example"})
