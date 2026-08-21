"""Auth dependency (DESIGN.md §2): offline JWT verification against Neon Auth JWKS.

`require_user` is a module-level function used directly as a FastAPI dependency
(``Depends(require_user)``) so ``app.dependency_overrides[require_user] = ...``
works naturally in tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request

DEFAULT_ALLOWED_EMAIL = "billzhangsc@gmail.com"

_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# ---------------------------------------------------------------------------
# env (.env fallback for local dev; Vercel injects real env vars in prod)
# ---------------------------------------------------------------------------

_dotenv_loaded = False


def _load_dotenv() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    here = Path(__file__).resolve().parent
    for path in (here / ".env", here.parent / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def _env(name: str, default: str | None = None) -> str | None:
    if name not in os.environ:
        _load_dotenv()
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# JWKS cache: in-process, refetch on kid miss (covers cold start + rotation)
# ---------------------------------------------------------------------------

_keys: dict[str, jwt.PyJWK] = {}  # kid -> key


def _auth_base_url() -> str:
    base = _env("NEON_AUTH_BASE_URL")
    if not base:
        raise HTTPException(status_code=500, detail="NEON_AUTH_BASE_URL not configured")
    return base


async def _fetch_jwks() -> dict:
    """Fetch the JWKS document. Tests monkeypatch this function."""
    url = _auth_base_url().rstrip("/") + "/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _signing_key(kid: str) -> jwt.PyJWK:
    key = _keys.get(kid)
    if key is None:
        data = await _fetch_jwks()
        fresh: dict[str, jwt.PyJWK] = {}
        for k in data.get("keys", []):
            if not k.get("kid"):
                continue
            try:
                fresh[k["kid"]] = jwt.PyJWK(k)
            except jwt.exceptions.PyJWKError:
                continue
        _keys.clear()
        _keys.update(fresh)
        key = _keys.get(kid)
    if key is None:
        raise HTTPException(status_code=401, detail="unknown signing key")
    return key


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def _expected_issuer() -> str:
    parts = urlsplit(_auth_base_url())
    return f"{parts.scheme}://{parts.netloc}"


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token.strip()


def _check_origin(request: Request) -> None:
    app_origin = _env("APP_ORIGIN")
    if not app_origin or request.method.upper() not in _STATE_CHANGING:
        return
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != app_origin.rstrip("/"):
        raise HTTPException(status_code=403, detail="origin mismatch")


async def require_user(request: Request) -> dict:
    """FastAPI dependency: verified, allowlisted user or 401/403."""
    _check_origin(request)
    token = _bearer_token(request)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    kid = header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="token missing kid")
    key = await _signing_key(kid)
    try:
        payload = jwt.decode(
            token,
            key=key.key,
            algorithms=["EdDSA"],
            issuer=_expected_issuer(),
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")

    email = payload.get("email")
    allowed = _env("ALLOWED_EMAIL", DEFAULT_ALLOWED_EMAIL) or DEFAULT_ALLOWED_EMAIL
    if not email or email.strip().lower() != allowed.strip().lower():
        raise HTTPException(status_code=403, detail="email not allowed")
    # The allowlist is only as strong as the email claim: sign-up is a public
    # Better Auth endpoint, so a token whose email the holder never proved
    # owning must not pass. Reject when the claim is present and false; a token
    # without the claim is allowed (claim shape is the auth service's).
    verified = payload.get("email_verified")
    if verified is None:
        verified = payload.get("emailVerified")
    if verified is not None and not verified:
        raise HTTPException(status_code=403, detail="email not verified")
    return {"email": email, "user_id": payload.get("sub")}
