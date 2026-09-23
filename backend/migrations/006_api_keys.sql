-- Tally — migration 006: personal API keys (REST + MCP, 2026-09-23).
-- Applied by apply.py in ONE transaction (`python apply.py 006`; `--dry-run`
-- applies + verifies + rolls back). Additive: one new user-owned table and
-- one function.
--
-- A key is `tally_live_<id>_<secret>` (backend/api_keys.py). Only the SHA-256
-- of the full plaintext is stored: the secret carries 256 random bits, so a
-- leaked hash cannot be brute-forced and no pepper is needed. `last_four` is
-- the plaintext's tail, for the redacted form the list shows. Revoking sets
-- revoked_at and keeps the row, so the list can still show it.
--
-- Tenancy exactly as 003 (ONBOARDING §5 rule 8): user_id defaults to the
-- transaction's app.user_id, RLS is enabled AND forced, one policy for every
-- command. No foreign keys: an FK locks the table it references for the whole
-- test while the rollback harness replays this file (the 003 deadlock). The
-- app role's table privileges come from 002's default privileges.

CREATE TABLE api_keys (
  id           text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{16}$'),
  user_id      text NOT NULL DEFAULT nullif(current_setting('app.user_id', true), '')
               CONSTRAINT api_keys_user_id_nonempty CHECK (user_id <> ''),
  label        text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 80),
  key_hash     text NOT NULL UNIQUE CHECK (key_hash ~ '^[0-9a-f]{64}$'),
  last_four    text NOT NULL,
  scopes       text[] NOT NULL CHECK (scopes = '{read}' OR scopes = '{read,write}'),
  expires_at   timestamptz,                         -- NULL = never
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,                         -- stamped at most every 5 minutes
  revoked_at   timestamptz                          -- set once; the row stays
);

CREATE INDEX api_keys_user_created_idx ON api_keys (user_id, created_at DESC);

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY api_keys_tenant ON api_keys
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));

-- The one door past RLS. A request carrying a key has no tenant yet (the key
-- is what names it) and tally_app cannot bypass the policy above. This
-- function runs as its owner, the migrating role, which carries BYPASSRLS on
-- Neon (002's header), and answers one question: whose live key is this
-- (id, hash) pair? A row only when both match, the key is not revoked and not
-- expired, and the account is not banned (its keys stop with its sessions);
-- joined to neon_auth for the account's CURRENT email, so an email change
-- (and the owner check that rides on it) applies on the next request.
-- It also stamps last_used_at, at most every 5 minutes, so a busy agent is
-- not one row write per call. The search_path is pinned, PUBLIC cannot call
-- it, only tally_app can.
CREATE FUNCTION api_key_auth(p_key_id text, p_key_hash text)
RETURNS TABLE (user_id text, email text, email_verified boolean, scopes text[])
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  k public.api_keys%ROWTYPE;
BEGIN
  SELECT * INTO k FROM public.api_keys a
   WHERE a.id = p_key_id AND a.key_hash = p_key_hash
     AND a.revoked_at IS NULL AND (a.expires_at IS NULL OR a.expires_at > now());
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF k.last_used_at IS NULL OR k.last_used_at < now() - interval '5 minutes' THEN
    UPDATE public.api_keys a SET last_used_at = now() WHERE a.id = k.id;
  END IF;
  RETURN QUERY
    SELECT k.user_id, u.email, u."emailVerified", k.scopes
      FROM neon_auth."user" u
     WHERE u.id::text = k.user_id
       AND NOT coalesce(u.banned, false);
END;
$$;

REVOKE ALL ON FUNCTION api_key_auth(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api_key_auth(text, text) TO tally_app;
