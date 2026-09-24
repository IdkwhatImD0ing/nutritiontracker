"""Auth dependency (DESIGN.md §2): offline JWT verification against Neon Auth
JWKS, or a personal API key (migration 006, api_keys.py).

Any verified account is a user: ``require_user`` returns ``{id, email,
is_owner, via, scopes}`` (id = the JWT ``sub`` = the Neon Auth user id;
is_owner marks the one ALLOWED_EMAIL account — every other account is a demo,
see services/quota.py, and a demo's key carries the demo's quota because it
IS that user; via = "session" | "api_key"; scopes = frozenset of "read" /
"write", both for a session; a key's dict also carries ``key_id``) and binds
``db.current_user_id`` so every statement of the request runs as that tenant
(migration 002 RLS).

Three entry points: ``authenticate`` (who is calling, no scope check — the
MCP endpoint picks its tool set from the scopes itself), ``require_user``
(+ a read-only key may only use a read route: GET/HEAD/OPTIONS, unless the
route declares its scope) and ``require_session_user`` (/api/keys: a key
cannot manage keys).

`require_user` is a module-level function used directly as a FastAPI dependency
(``Depends(require_user)``) so ``app.dependency_overrides[require_user] = ...``
works naturally in tests (an override must bind the tenant itself:
tests/tenancy_harness.override_user). Route modules import only
``require_user`` (some tests fake this module with nothing else).
"""

from __future__ import annotations

import os
import time
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request

import api_keys
import db

DEFAULT_ALLOWED_EMAIL = "billzhangsc@gmail.com"

# The request's verified user, for code that has no handle on the dependency
# result (services/quota.py's one-line checks inside handlers tests also call
# directly). Set beside db.current_user_id; None outside a request.
current_user: ContextVar[dict | None] = ContextVar("current_user", default=None)


def is_owner() -> bool:
    """Whether the request's user is the owner. No user bound counts as a
    demo — the quota and the shared-cache write rules both fail closed."""
    user = current_user.get()
    return bool(user and user.get("is_owner"))


_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The methods a read-only key may use (require_user), unless the route declares
# its own scope. Every other method needs "write", so a method nobody planned
# for fails closed.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SESSION_SCOPES = frozenset({"read", "write"})  # a signed-in user can do everything

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
# JWKS cache: in-process, refetch on kid miss (covers cold start + rotation).
# Cool-down (BACKLOG 14): a refetch that still misses its kid starts a
# per-process cool-down during which every unknown kid 401s without fetching —
# a flood of garbage kids can't amplify into JWKS requests. This is also the
# negative kid cache, held collectively: any kid absent from the last fetched
# document stays a no-fetch 401 for the window (a per-kid structure would be
# redundant — no per-kid entry can outlive this timer). A genuine rotation
# outside an attack window still refetches immediately (no prior failed
# fetch), so legit users never see the cool-down.
# ---------------------------------------------------------------------------

_keys: dict[str, jwt.PyJWK] = {}  # kid -> key
_last_failed_fetch: float = float("-inf")  # monotonic time of last kid-missing fetch
_REFETCH_COOLDOWN_S = 60.0


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
    global _last_failed_fetch
    key = _keys.get(kid)
    if key is not None:
        return key
    now = time.monotonic()
    if now - _last_failed_fetch < _REFETCH_COOLDOWN_S:
        # Negative cache / cool-down: the last refetch didn't resolve its kid,
        # so don't fetch again yet — this kid is either known-missing or part
        # of the same unknown-kid flood.
        raise HTTPException(status_code=401, detail="unknown signing key")
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
        _last_failed_fetch = now
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


def _allowed_origins() -> frozenset[str]:
    """Origins allowed on state-changing requests. Explicit APP_ORIGIN wins;
    when unset, default from Vercel's env (BACKLOG 14) so deployed
    environments are fail-closed without per-project config. Empty = nothing
    configured (bare local dev).

    APP_ORIGIN may list several origins, comma-separated. A domain change is
    otherwise a flip-the-switch moment: the value pins exactly one origin, so
    the instant it moves, every state-changing request from the old hostname
    is refused. Listing both lets the old and new domain overlap while DNS
    and any bookmarks catch up, then the old one is dropped."""
    explicit = _env("APP_ORIGIN")
    if explicit:
        origins = {o.strip().rstrip("/") for o in explicit.split(",")}
        return frozenset(o for o in origins if o)
    derived: set[str] = set()
    for name in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        host = (_env(name) or "").strip().rstrip("/")
        if host:
            derived.add(host if "://" in host else f"https://{host}")
    return frozenset(derived)


def _check_origin(request: Request) -> None:
    if request.method.upper() not in _STATE_CHANGING:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser client; the bearer token is still required
    allowed = _allowed_origins()
    if not allowed:
        return  # nothing configured: bare local dev stays fail-open
    if origin.rstrip("/") in allowed:
        return
    if _env("VERCEL_ENV") != "production" and urlsplit(origin).hostname in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        # dev allowance: local frontend against a deployed preview/dev backend.
        # BACKLOG 30: never in production — a localhost Origin there is another
        # site on the user's machine, not our frontend.
        return
    raise HTTPException(status_code=403, detail="origin mismatch")


def _is_owner_email(email: str) -> bool:
    allowed = _env("ALLOWED_EMAIL", DEFAULT_ALLOWED_EMAIL) or DEFAULT_ALLOWED_EMAIL
    return email.strip().lower() == allowed.strip().lower()


def _bind(user: dict) -> dict:
    db.current_user_id.set(user["id"])  # the tenant every statement of this request runs as
    current_user.set(user)
    return user


async def _api_key_user(token: str) -> dict:
    """The key branch: format -> SHA-256 -> api_key_auth (migration 006).
    Malformed, unknown, wrong-secret, revoked and expired keys all get the
    same 401, so the answer never says which keys exist."""
    key_id = api_keys.parse(token)
    row = None
    if key_id is not None:
        # The key is what names the tenant, so none is bound for the lookup:
        # api_key_auth is the one SECURITY DEFINER door past RLS and needs none.
        db.current_user_id.set(None)
        row = await db.fetchone(
            "SELECT * FROM api_key_auth(%s, %s)", (key_id, api_keys.hash_key(token))
        )
    if row is None:
        # the challenge a 401 owes its caller (RFC 6750), as POST /api/mcp sends it
        raise HTTPException(
            status_code=401, detail="invalid API key", headers={"WWW-Authenticate": "Bearer"}
        )
    if not row["email_verified"]:  # the JWT path's rule, read from the account itself
        raise HTTPException(status_code=403, detail="email not verified")
    return {
        "id": row["user_id"],
        "email": row["email"],
        "is_owner": _is_owner_email(row["email"]),
        "via": "api_key",
        "scopes": frozenset(row["scopes"]),
        "key_id": key_id,
    }


async def authenticate(request: Request) -> dict:
    """Who is calling (an API key or a Neon Auth JWT), bound as the request's
    tenant, or 401/403. No scope check: require_user adds the per-route one."""
    token = _bearer_token(request)
    if token.startswith(api_keys.FAMILY):  # before the JWT parse, which would say "invalid token"
        # No Origin check for a key. The allowlist pins the session token to
        # this app's own frontend, the one client that ever holds one. A key is
        # made for every other client, and those that run in a browser engine
        # (an extension, a desktop app's webview) send their own Origin, so the
        # check would refuse callers keys exist for. The key alone is the
        # credential: a browser never attaches it on its own, so there is no
        # cross-site request to forge.
        return _bind(await _api_key_user(token))
    _check_origin(request)  # still before the JWT is parsed or a JWKS fetched
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    kid = header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="token missing kid")
    key = await _signing_key(kid)
    origin = _expected_issuer()
    try:
        payload = jwt.decode(
            token,
            key=key.key,
            algorithms=["EdDSA"],
            issuer=origin,
            # Neon's tokens carry `aud` = the auth origin (their docs' example
            # payload and Python snippet both show it). PyJWT refuses ANY token
            # with an `aud` claim unless told which audience to accept, so
            # leaving this to the default 401'd every real request with
            # "invalid token" while the offline tests (no aud) stayed green.
            # Checked by hand below so a token without the claim still passes.
            options={"require": ["exp"], "verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")

    aud = payload.get("aud")
    if aud:
        audiences = aud if isinstance(aud, list) else [aud]
        if origin not in audiences:
            raise HTTPException(status_code=401, detail="invalid token")

    email = payload.get("email")
    if not email:
        raise HTTPException(status_code=403, detail="token has no email")
    # The owner check is only as strong as the email claim: sign-up is a
    # public Better Auth endpoint, so a token whose email the holder never
    # proved owning must not pass. Reject when the claim is present and
    # false; a token without the claim is allowed (claim shape is the auth
    # service's).
    verified = payload.get("email_verified")
    if verified is None:
        verified = payload.get("emailVerified")
    if verified is not None and not verified:
        raise HTTPException(status_code=403, detail="email not verified")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="token missing sub")
    return _bind(
        {
            "id": str(sub),
            "email": email,
            "is_owner": _is_owner_email(email),
            "via": "session",
            "scopes": SESSION_SCOPES,
        }
    )


async def require_user(request: Request) -> dict:
    """FastAPI dependency: the verified user or 401/403. A read-only key may
    only read: a route that writes answers 403."""
    user = await authenticate(request)
    # The method says whether a route writes, except where it doesn't: a GET
    # that writes (a watchdog, a cache fill) or a POST that only computes.
    # Such a route names its scope in its decorator,
    # openapi_extra={"x-tally-scope": "read" | "write"} (plain data, so route
    # modules still import nothing but require_user), and FastAPI has put the
    # matched route in the scope before any dependency runs.
    declared = (getattr(request.scope.get("route"), "openapi_extra", None) or {}).get(
        "x-tally-scope"
    )
    need = declared or ("read" if request.method.upper() in _READ_METHODS else "write")
    if need not in user["scopes"]:
        raise HTTPException(status_code=403, detail="this API key is read-only")
    return user


async def require_session_user(request: Request) -> dict:
    """FastAPI dependency for /api/keys: a signed-in session only. A key that
    could mint keys could outlive its own revocation."""
    user = await authenticate(request)
    if user["via"] == "api_key":
        raise HTTPException(
            status_code=403, detail="API keys cannot manage API keys; sign in to the app"
        )
    return user
