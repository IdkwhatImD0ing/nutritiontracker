-- Usual — initial schema. Translated verbatim from DESIGN.md §4 (Rev 6, 2026-08-20).
-- Applied by apply.py in one transaction against a brand-new database.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Cached food identities + nutrition. The only source of macro numbers in the system.
CREATE TABLE foods (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source          text NOT NULL,              -- 'fdc' | 'off' | 'web'
  source_id       text NOT NULL,              -- fdcId, OFF barcode, or a hash for web-transcribed foods
  source_url      text,                       -- web foods: the official page the values were read from
  provenance      jsonb,                      -- web foods: url_citation annotations + consulted sources
  name            text NOT NULL,              -- provider name, verbatim
  display_name    text NOT NULL,              -- short human name ("chicken breast, roasted"); trigram target
  brand           text,
  food_type       text NOT NULL,              -- 'branded' | 'generic'
  state           text,                       -- 'cooked' | 'raw' | 'dry' | NULL — parsed from entry name by adapter
  state_sensitive boolean NOT NULL DEFAULT false,  -- deterministic term list, set by adapter
  servings        jsonb NOT NULL,             -- [{serving_id, description, grams?, macros:{kcal,protein_g,carb_g,fat_g,...}}]
  default_serving_id text NOT NULL,
  per_100g        jsonb,                      -- normalized macros when gram weights known
  payload         jsonb NOT NULL,             -- full raw provider response
  refreshed_at    timestamptz NOT NULL DEFAULT now(),  -- informational; every source is cache-forever (no refresh jobs)
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);
CREATE INDEX foods_name_trgm ON foods USING gin ((display_name || ' ' || coalesce(brand,'')) gin_trgm_ops);

-- The personal memory layer. Multiple rows per alias_norm = ambiguity (the "fairlife" case).
CREATE TABLE aliases (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  alias_norm      text NOT NULL,
  food_id         uuid NOT NULL REFERENCES foods(id),
  default_serving_id text,
  default_qty     numeric,
  state_answer    text,                       -- persisted cooked/raw answer for this phrase
  pinned          boolean NOT NULL DEFAULT false,
  use_count       integer NOT NULL DEFAULT 1,
  last_used_at    timestamptz NOT NULL DEFAULT now(),
  last_explicit_at timestamptz,               -- last time the user actively chose this mapping
  rejected_count  integer NOT NULL DEFAULT 0, -- times the user switched away from this pre-selection
  created_at      timestamptz NOT NULL DEFAULT now(),
  origin          text NOT NULL,              -- 'tier3' | 'edit' | 'manual'
  UNIQUE (alias_norm, food_id)
);
CREATE INDEX aliases_norm_idx  ON aliases (alias_norm);
CREATE INDEX aliases_trgm_idx  ON aliases USING gin (alias_norm gin_trgm_ops);

-- "My usual breakfast."
CREATE TABLE meal_macros (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name            text NOT NULL,
  trigger_phrases text[] NOT NULL,            -- stored post-norm(): e.g. {'usual breakfast','usual'}
  items           jsonb NOT NULL,             -- [{food_id, serving_id, qty}]
  is_active       boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One capture session (voice or text). Instrumentation anchor. id is CLIENT-generated (idempotency key).
CREATE TABLE logs (
  id              uuid PRIMARY KEY,           -- client-generated; POST /api/logs upserts by id
  input_mode      text NOT NULL,              -- 'voice' | 'text'  (keyboard dictation arrives as 'text')
  raw_input       text NOT NULL,              -- transcript or typed text
  captured_at     timestamptz NOT NULL,       -- client capture-start time (offline-safe)
  capture_fallback boolean NOT NULL DEFAULT false,  -- MediaRecorder failed → fell back to text
  capture_error   text,                       -- client-reported error, or 'silent_blob' / 'empty_transcript'
  short_circuit   boolean NOT NULL DEFAULT false,   -- whole-utterance tier-0/1 hit, decompose skipped
  meal_inferred   text,
  status          text NOT NULL DEFAULT 'staged',  -- 'staged' | 'committed' | 'discarded'
  committed_at    timestamptz,                -- first commit (late items tracked per-item)
  transcribe_ms   integer,
  decompose_ms    integer,
  staged_ms       integer,                    -- client-observed: capture start → list rendered (PATCH /timings)
  edited_items    integer,
  trace_id        text,                       -- decompose-call trace, for the observability platform (§10)
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per staged item: OPERATIONAL staging state + telemetry, from resolution through commit.
-- This table is what makes the async tier-3 contract (poll / question / answer / late commit) buildable.
CREATE TABLE item_resolutions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  log_id          uuid NOT NULL REFERENCES logs(id),
  item_index      integer NOT NULL,
  phrase          text NOT NULL,
  phrase_norm     text NOT NULL,
  status          text NOT NULL,              -- 'resolved' | 'searching' | 'needs_input' | 'not_found' | 'removed'
  staged          jsonb NOT NULL,             -- current full StagedItem payload (question, alternates, styling…)
  tier            smallint,                   -- 0..3, NULL = unresolved/manual
  tier3_started_at timestamptz,
  tier3_attempts  smallint NOT NULL DEFAULT 0,
  network_calls   integer NOT NULL DEFAULT 0,
  resolve_ms      integer,
  proposed_food_id uuid REFERENCES foods(id),
  proposed_serving_id text,
  proposed_qty    numeric,
  final_food_id   uuid REFERENCES foods(id),
  final_serving_id text,
  final_qty       numeric,
  alias_id        uuid REFERENCES aliases(id),-- which alias resolved/learned this item
  was_edited      boolean NOT NULL DEFAULT false,  -- any delta: food, serving, OR qty
  was_disambiguated boolean NOT NULL DEFAULT false,
  committed_at    timestamptz,                -- per-item; NULL = staged/late/removed
  trace_id        text,                       -- tier-3 agent trace, when tier = 3
  agent_transcript jsonb,                     -- tier-3 only: full run item history, archived
                                              -- forever here — immune to any vendor's trace
                                              -- retention (§10); a few KB/run, trivial in Neon
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (log_id, item_index)
);

-- The diary. Macros snapshotted at commit; never recomputed (qty edits scale the snapshot).
CREATE TABLE entries (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  logged_at       timestamptz NOT NULL,
  meal            text NOT NULL,              -- 'breakfast' | 'lunch' | 'dinner' | 'snack'
  food_id         uuid NOT NULL REFERENCES foods(id),
  serving_id      text NOT NULL,
  qty             numeric NOT NULL,
  grams           numeric,
  macros          jsonb NOT NULL,             -- {kcal, protein_g, carb_g, fat_g, fiber_g?, micros?...}
  log_id          uuid REFERENCES logs(id),   -- NULL for manual entries
  item_index      integer,
  alias_id        uuid REFERENCES aliases(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (log_id, item_index)                 -- double-commit is a constraint violation, not a bug report
);
CREATE INDEX entries_logged_idx ON entries (logged_at);

-- App configuration AND the user-profile layer the agent reads (§6.1).
CREATE TABLE settings (
  key             text PRIMARY KEY,
  value           jsonb NOT NULL
);

-- Seeds (DESIGN.md §4 comment block + §7.2 defaults).
-- body/targets are written by the first-run goal wizard (§7.4) — seeded empty.
-- timezone is a placeholder pending confirmation.
INSERT INTO settings (key, value) VALUES
  ('meal_windows', '{
     "breakfast": {"start": "04:00", "end": "11:00"},
     "lunch":     {"start": "11:00", "end": "15:30"},
     "dinner":    {"start": "15:30", "end": "21:30"},
     "default":   "snack"
   }'::jsonb),
  ('timezone', '"America/Los_Angeles"'::jsonb),
  ('cronometer_baseline_days_per_week', '0'::jsonb),
  ('food_index', '{"inline_default": 20, "max": 100}'::jsonb),
  ('profile_notes', '""'::jsonb),
  ('body', '{}'::jsonb),
  ('targets', '{}'::jsonb);
