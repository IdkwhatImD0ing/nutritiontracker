-- Tally — migration 003: the weigh-in log (agent mode, 2026-09-06).
-- Applied by apply.py in ONE transaction (`python apply.py 003`; `--dry-run`
-- applies + verifies + rolls back). Additive: one new user-owned table and
-- one nullable column on logs.
--
-- One weigh-in per user per LOCAL day — the day is the user's (the app
-- computes it from the client's tz / settings.timezone), never the database
-- clock — so saying a weight twice on one day replaces it. Stored in kg;
-- `unit` keeps how it was said so the app shows it back the same way.
-- `log_id` names the agent utterance that logged it (NULL for a manual PUT),
-- tenant-safe through the same composite FK entries and item_resolutions use
-- (002 §3): a weigh-in cannot hang under another tenant's log.
--
-- Tenancy exactly as 002 (ONBOARDING §5 rule 8): user_id defaults to the
-- transaction's app.user_id, RLS is enabled AND forced, one policy for every
-- command. The app role's privileges come from 002's default privileges —
-- nothing to grant here.

CREATE TABLE weights (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    text NOT NULL DEFAULT nullif(current_setting('app.user_id', true), '')
             CONSTRAINT weights_user_id_nonempty CHECK (user_id <> ''),
  day        date NOT NULL,                              -- the user's local date of the weigh-in
  weight_kg  numeric NOT NULL CHECK (weight_kg > 0),
  unit       text NOT NULL CHECK (unit IN ('kg', 'lb')), -- as said/entered; display only
  source     text NOT NULL CHECK (source IN ('agent', 'manual')),
  log_id     uuid,                                       -- the agent utterance; NULL for a manual entry
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, day),                                 -- one weigh-in a day, updated in place
  CONSTRAINT weights_log_tenant_fkey FOREIGN KEY (log_id, user_id) REFERENCES logs (id, user_id)
);

ALTER TABLE weights ENABLE ROW LEVEL SECURITY;
ALTER TABLE weights FORCE ROW LEVEL SECURITY;
CREATE POLICY weights_tenant ON weights
  USING (user_id = current_setting('app.user_id', true))
  WITH CHECK (user_id = current_setting('app.user_id', true));

-- The agent's one-sentence reply, stamped on the log it answered
-- (routes/logs._run_agent_lane). A same-id replay (§8.6: a lost response)
-- answers the outcome again instead of an empty staging list — the weigh-in
-- itself is read back through weights.log_id — and an itemless agent log
-- that finished is never mistaken for an orphan and re-run. NULL on every
-- other log.
ALTER TABLE logs ADD COLUMN agent_reply text;
