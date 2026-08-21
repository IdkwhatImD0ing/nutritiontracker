"""Tests for backend/auth.py (workstream G).

Offline tests generate an Ed25519 keypair and monkeypatch the JWKS fetch.
One live test fetches the real JWKS from NEON_AUTH_BASE_URL in .env.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth  # noqa: E402

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


def test_valid_token_allowlisted_email_passes(app_client):
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    r = client.get("/whoami", headers={"Authorization": f"Bearer {_token(priv, 'kid-a')}"})
    assert r.status_code == 200
    assert r.json() == {"email": ALLOWED, "user_id": "user-123"}


def test_wrong_email_403(app_client):
    client, state = app_client
    priv, jwk = _keypair("kid-a")
    state["jwks"] = {"keys": [jwk]}
    tok = _token(priv, "kid-a", email="mallory@example.com")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


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
