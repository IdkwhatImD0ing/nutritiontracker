-- Tally — migration 004: an index for past_foods (agent read tool, 2026-09-08).
-- Applied by apply.py in ONE transaction (`python apply.py 004`; `--dry-run`
-- applies + verifies + rolls back). Additive: one index, no schema change,
-- no data touched, no RLS surface.
--
-- §6.4's past_foods answers "what did I have, and how much" with
--   SELECT DISTINCT ON (food_id) ... FROM entries ORDER BY food_id, logged_at DESC
-- and `entries` carried only entries_logged_idx (001) and
-- entries_user_logged_idx (002) — neither leads on food_id, so every call
-- read and sorted the account's whole history to return 20 rows, inside the
-- model's turn. This index lets the planner walk one entry per food straight
-- off the index in the order DISTINCT ON wants.
--
-- user_id leads because RLS scopes every read to one tenant; DESC on
-- logged_at matches the query's own ordering so no sort is needed.

CREATE INDEX IF NOT EXISTS entries_user_food_logged_idx
  ON entries (user_id, food_id, logged_at DESC);
