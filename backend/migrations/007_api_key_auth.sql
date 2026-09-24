-- Tally — migration 007: api_key_auth checks the account before it stamps
-- (2026-09-23). Applied by apply.py in ONE transaction (`python apply.py 007`;
-- `--dry-run` applies + verifies + rolls back). Replaces one function; no
-- table, policy or row changes. Needs 006.
--
-- 006's api_key_auth stamped last_used_at as soon as the key matched and only
-- then joined neon_auth to refuse a banned (or deleted) account, so a refused
-- key still recorded a use. This version finds the live key AND its non-banned
-- account in one query, returns nothing if either is missing, and only then
-- stamps (the same 5-minute throttle). Same signature, still SECURITY DEFINER
-- with a pinned search_path. CREATE OR REPLACE keeps the owner and the grants;
-- they are restated below so this file reads whole. The COMMENT is the marker
-- apply.py and the test harness read to tell that 007 is applied.

CREATE OR REPLACE FUNCTION api_key_auth(p_key_id text, p_key_hash text)
RETURNS TABLE (user_id text, email text, email_verified boolean, scopes text[])
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  v_user_id   text;
  v_scopes    text[];
  v_last_used timestamptz;
  v_email     text;
  v_verified  boolean;
BEGIN
  SELECT a.user_id, a.scopes, a.last_used_at, u.email, u."emailVerified"
    INTO v_user_id, v_scopes, v_last_used, v_email, v_verified
    FROM public.api_keys a
    JOIN neon_auth."user" u ON u.id::text = a.user_id
   WHERE a.id = p_key_id AND a.key_hash = p_key_hash
     AND a.revoked_at IS NULL AND (a.expires_at IS NULL OR a.expires_at > now())
     AND NOT coalesce(u.banned, false);
  IF NOT FOUND THEN
    RETURN;
  END IF;
  IF v_last_used IS NULL OR v_last_used < now() - interval '5 minutes' THEN
    UPDATE public.api_keys a SET last_used_at = now() WHERE a.id = p_key_id;
  END IF;
  RETURN QUERY SELECT v_user_id, v_email, v_verified, v_scopes;
END;
$$;

REVOKE ALL ON FUNCTION api_key_auth(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION api_key_auth(text, text) TO tally_app;
COMMENT ON FUNCTION api_key_auth(text, text) IS
  '007: finds the live key and its non-banned account before stamping last_used_at';
