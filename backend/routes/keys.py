"""/api/keys — personal API keys for scripts, agents and MCP clients (migration 006).

- GET    /api/keys           → every key the caller made, newest first (revoked
                               ones too; the Settings card dims them)
- POST   /api/keys           → {label, scopes, expires_in_days} → the key and its
                               secret, 201. The secret is shown this ONCE: only
                               its SHA-256 is stored (api_keys.py)
- DELETE /api/keys/{key_id}  → revoke; idempotent, the row stays so the list can
                               show it

Session only (auth.require_session_user): a key that could mint keys could
outlive its own revocation. Tenancy is the database's (RLS on `api_keys`) —
nothing here names a user, and another user's key id is simply not found.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StringConstraints

import api_keys
import db
from auth import require_session_user

router = APIRouter(dependencies=[Depends(require_session_user)])

MAX_ACTIVE_KEYS = 10  # unrevoked and unexpired, per user
SCOPES = {"read": ["read"], "read_write": ["read", "write"]}  # write always implies read

_COLS = "id, label, scopes, last_four, created_at, last_used_at, expires_at, revoked_at"


class ApiKeyOut(BaseModel):
    id: str
    label: str
    scopes: list[str]
    redacted: str  # tally_live_<id>_…<last four>
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None  # None = never
    revoked_at: datetime | None


class ApiKeyCreate(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    scopes: Literal["read", "read_write"] = "read_write"
    expires_in_days: Literal[30, 90, 365] | None = None  # None = never expires


class ApiKeyCreated(BaseModel):
    key: ApiKeyOut
    secret: str  # the full key; never retrievable again


def _out(row: dict) -> ApiKeyOut:
    return ApiKeyOut(
        id=row["id"],
        label=row["label"],
        scopes=row["scopes"],
        redacted=api_keys.redact(row["id"], row["last_four"]),
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


@router.get("/api/keys", response_model=list[ApiKeyOut])
async def list_keys() -> list[ApiKeyOut]:
    rows = await db.fetchall(f"SELECT {_COLS} FROM api_keys ORDER BY created_at DESC, id")
    return [_out(r) for r in rows]


@router.post("/api/keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(body: ApiKeyCreate) -> ApiKeyCreated:
    key_id, plaintext = api_keys.generate()
    # The cap and the insert are one statement: no row back means the caller
    # already has MAX_ACTIVE_KEYS live keys.
    row = await db.fetchone(
        f"""
        INSERT INTO api_keys (id, label, key_hash, last_four, scopes, expires_at)
        SELECT %s, %s, %s, %s, %s, now() + %s::int * interval '1 day'
        WHERE (SELECT count(*) FROM api_keys
               WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())) < %s
        RETURNING {_COLS}
        """,
        (
            key_id,
            body.label,
            api_keys.hash_key(plaintext),
            plaintext[-4:],
            SCOPES[body.scopes],
            body.expires_in_days,
            MAX_ACTIVE_KEYS,
        ),
    )
    if row is None:
        raise HTTPException(
            409, f"you already have {MAX_ACTIVE_KEYS} active API keys; revoke one first"
        )
    return ApiKeyCreated(key=_out(row), secret=plaintext)


@router.delete("/api/keys/{key_id}", response_model=ApiKeyOut)
async def revoke_key(key_id: str) -> ApiKeyOut:
    # coalesce keeps the first revocation's time: revoking twice is a no-op
    # that still answers with the row.
    row = await db.fetchone(
        f"UPDATE api_keys SET revoked_at = coalesce(revoked_at, now()) WHERE id = %s RETURNING {_COLS}",
        (key_id,),
    )
    if row is None:
        raise HTTPException(404, "API key not found")
    return _out(row)
