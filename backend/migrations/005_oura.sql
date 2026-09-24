-- Tally — migration 005: the Oura connection and what it syncs (2026-09-21).
-- Applied by apply.py in ONE transaction (`python apply.py 005`; `--dry-run`
-- applies + verifies + rolls back). Additive: three new user-owned tables.
--
-- oura_tokens   one row per user: the OAuth tokens, stored as Fernet
--               ciphertext (services/oura.py) — never plaintext. Oura's docs
--               ask for refresh tokens "in an encrypted database"; a database
--               read alone (a leaked connection string, a backup, an admin
--               query) must not be enough to call Oura as the user.
-- oura_days     one row per user per Oura day: the daily activity summary.
--               The numbers Tally reads get their own columns; `raw` keeps
--               the whole document so a later question needs no re-fetch.
-- oura_workouts one row per Oura workout id, same shape.
--
-- Syncing re-fetches the last few days and upserts: Oura keeps revising today
-- (and finalises yesterday) as the ring reports in.
--
-- Tenancy exactly as 003 (ONBOARDING §5 rule 8): user_id defaults to the
-- transaction's app.user_id, RLS is enabled AND forced, one policy for every
-- command. The app role's privileges come from 002's default privileges.

CREATE TABLE oura_tokens (
  user_id        text PRIMARY KEY DEFAULT nullif(current_setting('app.user_id', true), '')
                 CONSTRAINT oura_tokens_user_id_nonempty CHECK (user_id <> ''),
  access_token   text NOT NULL,          -- Fernet ciphertext
  refresh_token  text NOT NULL,          -- Fernet ciphertext
  expires_at     timestamptz NOT NULL,   -- when the access token stops working
  scope          text,                   -- as granted, space-separated
  connected_at   timestamptz NOT NULL DEFAULT now(),
  last_synced_at timestamptz,
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE oura_days (
  user_id         text NOT NULL DEFAULT nullif(current_setting('app.user_id', true), '')
                  CONSTRAINT oura_days_user_id_nonempty CHECK (user_id <> ''),
  day             date NOT NULL,         -- Oura's own day for the summary
  total_calories  integer,               -- the whole day's burn, resting included
  active_calories integer,
  steps           integer,
  score           integer,               -- Oura's activity score
  raw             jsonb NOT NULL,
  synced_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, day)
);

CREATE TABLE oura_workouts (
  user_id    text NOT NULL DEFAULT nullif(current_setting('app.user_id', true), '')
             CONSTRAINT oura_workouts_user_id_nonempty CHECK (user_id <> ''),
  oura_id    text NOT NULL,              -- Oura's document id
  day        date NOT NULL,
  activity   text NOT NULL,              -- e.g. "walking", "strengthTraining"
  start_at   timestamptz,
  end_at     timestamptz,
  calories   numeric,
  intensity  text,                       -- easy | moderate | hard
  source     text,                       -- manual | autodetected | confirmed | workout_heart_rate
  raw        jsonb NOT NULL,
  synced_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, oura_id)
);
CREATE INDEX oura_workouts_user_day_idx ON oura_workouts (user_id, day DESC);

ALTER TABLE oura_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE oura_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY oura_tokens_tenant ON oura_tokens
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));

ALTER TABLE oura_days ENABLE ROW LEVEL SECURITY;
ALTER TABLE oura_days FORCE ROW LEVEL SECURITY;
CREATE POLICY oura_days_tenant ON oura_days
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));

ALTER TABLE oura_workouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE oura_workouts FORCE ROW LEVEL SECURITY;
CREATE POLICY oura_workouts_tenant ON oura_workouts
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));
