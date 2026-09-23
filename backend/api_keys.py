"""Personal API keys: the format and its four helpers (migration 006).

A key is ``tally_live_<keyId>_<secret>``: keyId is 16 lowercase hex (8 random
bytes) and names the row, the secret is 64 lowercase hex (32 random bytes).
The database stores only the SHA-256 hex of the FULL plaintext. The secret
carries 256 random bits, so a leaked hash cannot be brute-forced back into a
key and no pepper (no extra env var) is needed. The plaintext is handed out
exactly once, by POST /api/keys (routes/keys.py); auth.py hashes whatever a
request presents and asks the database's ``api_key_auth`` whose it is.
"""

from __future__ import annotations

import hashlib
import re
import secrets

PREFIX = "tally_live_"
# Anything starting with this is taken for a key (auth.py), so a mangled key
# gets "invalid API key" rather than the JWT path's "invalid token". JWTs
# always start "eyJ", so the two never collide.
FAMILY = "tally_"
_KEY_RE = re.compile(r"tally_live_([0-9a-f]{16})_([0-9a-f]{64})")


def generate() -> tuple[str, str]:
    """A fresh key: (key_id, plaintext)."""
    key_id = secrets.token_hex(8)
    return key_id, f"{PREFIX}{key_id}_{secrets.token_hex(32)}"


def parse(token: str) -> str | None:
    """The key id of a well-formed key, else None. fullmatch, not match: `$`
    would let a trailing newline through."""
    m = _KEY_RE.fullmatch(token)
    return m.group(1) if m else None


def hash_key(plaintext: str) -> str:
    """What the database stores and api_key_auth compares: SHA-256 hex."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def redact(key_id: str, last_four: str) -> str:
    """The form the list shows: enough to recognise a key, useless to replay."""
    return f"{PREFIX}{key_id}_…{last_four}"
