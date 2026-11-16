-- Tally — migration 002: tenancy (multi-user) by Row-Level Security.
-- Applied by apply.py in ONE transaction (`python apply.py 002`; `--dry-run`
-- applies + verifies + rolls back). Additive: no column dropped, no row lost.
--
-- Why RLS and not ~100 WHERE clauses: every user-owned table gets a user_id
-- whose DEFAULT is the transaction's `app.user_id` setting, and one policy
-- that shows/accepts only rows with that user_id. backend/db.py sets the
-- setting (and the role, below) inside every transaction it opens, from the
-- verified JWT's `sub` (auth.require_user). With no setting the default is
-- NULL (NOT NULL violation on insert) and the policy matches nothing —
-- fail-closed by construction.
--
-- Why a separate role: the connecting role (neondb_owner) carries BYPASSRLS
-- on Neon, which FORCE ROW LEVEL SECURITY cannot override, and no superuser
-- is available to remove it. So the app switches to `tally_app` (NOLOGIN, no
-- BYPASSRLS, plain table privileges) with SET LOCAL ROLE for each
-- transaction. RLS is ENABLEd + FORCEd anyway, so an owner-role connection
-- without BYPASSRLS would be covered too.
--
-- foods stays global: a shared cache of public nutrition data (the photo
-- lane's source='estimate' placeholders included — no personal data).
--
-- UNIQUE (log_id, item_index) on item_resolutions and entries stays unscoped:
-- log_id is a globally unique uuid, so the constraint is already per-user,
-- and five ON CONFLICT clauses infer it. The composite FK (log_id, user_id)
-- -> logs (id, user_id) makes a child row under another tenant's log
-- impossible (FK checks bypass RLS; this does not).

-- 0. The owner: every existing row is theirs. apply.py puts ALLOWED_EMAIL (.env)
--    in the transaction-local `app.owner_email` before it runs this file.
--    Exactly one Neon Auth user must match — STRICT aborts (rolling back)
--    otherwise, and so does a run where nothing set the setting.
DO $$
DECLARE owner_id text;
BEGIN
  SELECT id::text INTO STRICT owner_id
  FROM neon_auth."user"
  WHERE lower(email) = lower(current_setting('app.owner_email'));
  PERFORM set_config('app.user_id', owner_id, true);  -- transaction-local
END $$;

-- 1. The app role (header). Membership WITH SET lets the connecting role
--    SET LOCAL ROLE to it; INHERIT FALSE keeps the two privilege sets apart.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tally_app') THEN
    CREATE ROLE tally_app NOLOGIN;
  END IF;
END $$;
GRANT tally_app TO CURRENT_USER WITH SET TRUE, INHERIT FALSE;

-- 2. user_id on every user-owned table, backfilled to the owner. The DEFAULT
--    reads the setting; nullif() turns the '' a reset placeholder leaves
--    behind into NULL, and the CHECK refuses '' outright.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['aliases', 'meal_macros', 'logs', 'item_resolutions',
                           'entries', 'settings'] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN user_id text', t);
    EXECUTE format($q$UPDATE %I SET user_id = current_setting('app.user_id')$q$, t);
    EXECUTE format(
      $q$ALTER TABLE %I
           ALTER COLUMN user_id SET NOT NULL,
           ALTER COLUMN user_id SET DEFAULT nullif(current_setting('app.user_id', true), ''),
           ADD CONSTRAINT %I CHECK (user_id <> '')$q$,
      t, t || '_user_id_nonempty');
  END LOOP;
END $$;

-- 3. Scoped uniqueness (settings key, alias phrase→food) and tenant-safe
--    child rows of a log.
ALTER TABLE settings DROP CONSTRAINT settings_pkey, ADD PRIMARY KEY (user_id, key);
ALTER TABLE aliases DROP CONSTRAINT aliases_alias_norm_food_id_key,
  ADD CONSTRAINT aliases_user_id_alias_norm_food_id_key UNIQUE (user_id, alias_norm, food_id);
ALTER TABLE logs ADD CONSTRAINT logs_id_user_id_key UNIQUE (id, user_id);
ALTER TABLE item_resolutions ADD CONSTRAINT item_resolutions_log_tenant_fkey
  FOREIGN KEY (log_id, user_id) REFERENCES logs (id, user_id);
ALTER TABLE entries ADD CONSTRAINT entries_log_tenant_fkey
  FOREIGN KEY (log_id, user_id) REFERENCES logs (id, user_id);

-- 4. Indexes for the per-user time/phrase scans.
CREATE INDEX logs_user_captured_idx    ON logs (user_id, captured_at);
CREATE INDEX entries_user_logged_idx   ON entries (user_id, logged_at);
CREATE INDEX aliases_user_norm_idx     ON aliases (user_id, alias_norm);

-- 5. Demo-quota ledger (services/quota.py): one row per ATTEMPT the quota
--    admitted — a log attempt before any model spend, a capped re-resolution.
--    Attempts are counted here and not from logs because a logs row can be
--    dropped or re-run under the same client id; the ledger row cannot.
--    The one-log-a-day rule is the schema's, not the app's: the partial
--    unique index below admits one 'log' row per user per UTC day, so two
--    concurrent first attempts cannot both pass a count — the second insert
--    collides (quota.py inserts first, ON CONFLICT DO NOTHING, and reads
--    rowcount 0 as the refusal). The day is the database's: created_at
--    defaults to now(), and the same expression judges it.
CREATE TABLE quota_events (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    text NOT NULL DEFAULT nullif(current_setting('app.user_id', true), '')
             CONSTRAINT quota_events_user_id_nonempty CHECK (user_id <> ''),
  kind       text NOT NULL,                         -- 'log' | 'resolve'
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX quota_events_user_created_idx ON quota_events (user_id, created_at);
CREATE UNIQUE INDEX quota_events_log_day_key
  ON quota_events (user_id, ((created_at AT TIME ZONE 'UTC')::date))
  WHERE kind = 'log';

-- 6. Row-Level Security: one policy per table, every command, tenant = setting.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['aliases', 'meal_macros', 'logs', 'item_resolutions',
                           'entries', 'settings', 'quota_events'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      $q$CREATE POLICY %I ON %I
           USING (user_id = current_setting('app.user_id', true))
           WITH CHECK (user_id = current_setting('app.user_id', true))$q$,
      t || '_tenant', t);
  END LOOP;
END $$;

-- 7. Privileges for the app role (after the new table exists). Default
--    privileges cover tables later migrations create.
GRANT USAGE ON SCHEMA public TO tally_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tally_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tally_app;
