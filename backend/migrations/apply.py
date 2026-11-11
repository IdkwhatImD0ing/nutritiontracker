"""Apply a migration to the Neon database named by DATABASE_URL in the repo-root .env.

  python apply.py                -> 001_init.sql   (brand-new database only)
  python apply.py 002            -> 002_multiuser.sql (tenancy; needs 001, refuses if applied)
  python apply.py 002 --dry-run  -> apply 002 + verify inside one transaction, then ROLL BACK
  python apply.py 002 --check    -> re-run 002's verification against the committed schema
                                    (post-apply check; valid while the owner is the only
                                    tenant, i.e. run it right after applying)
  python apply.py 003            -> 003_weights.sql (the weigh-in log; needs 002, no-op if applied)
  python apply.py 003 --dry-run  -> apply 003 + verify inside one transaction, then ROLL BACK
  python apply.py 003 --check    -> re-run 003's verification against the committed schema
  python apply.py 004            -> 004_entries_food_idx.sql (past_foods' index; needs 002,
                                    no-op if applied)
  python apply.py 004 --dry-run  -> apply 004 + verify inside one transaction, then ROLL BACK
  python apply.py 004 --check    -> re-run 004's verification against the committed schema
  python apply.py 005            -> 005_oura.sql (the Oura connection + its synced activity;
                                    needs 002, no-op if applied)
  python apply.py 005 --dry-run  -> apply 005 + verify inside one transaction, then ROLL BACK
  python apply.py 005 --check    -> re-run 005's verification against the committed schema
  python apply.py 006            -> 006_api_keys.sql (personal API keys; needs 002, no-op if applied)
  python apply.py 006 --dry-run  -> apply 006 + verify inside one transaction, then ROLL BACK
  python apply.py 006 --check    -> re-run 006's verification against the committed schema
  python apply.py 007            -> 007_api_key_auth.sql (api_key_auth checks the account before
                                    it stamps last_used_at; needs 006, no-op if applied)
  python apply.py 007 --dry-run  -> apply 007 + verify inside one transaction, then ROLL BACK
  python apply.py 007 --check    -> re-run 007's verification against the committed schema

Every migration runs in ONE transaction. 002 is verified before COMMIT (row
counts unchanged, every existing row backfilled to the owner, RLS filters
under the app role, the ledger's one-log-a-day unique index present, an
insert with no tenant fails); a failed check rolls back. Guards:
  001 - schema public must be empty; exactly the 7 tables present -> no-op, exit 0
  002 - the 7 tables must be present and logs.user_id absent; present -> no-op, exit 0
  002 --check - logs.user_id must be present
  003 - logs.user_id must be present (002 applied) and `weights` absent; present -> no-op, exit 0
  004 - logs.user_id must be present (002 applied); the index is CREATE INDEX IF NOT EXISTS,
        so re-applying is a no-op either way
  005 - logs.user_id must be present (002 applied) and `oura_tokens` absent; present -> no-op, exit 0
  006 - logs.user_id must be present (002 applied) and `api_keys` absent; present -> no-op, exit 0
  007 - `api_keys` must be present (006 applied) and api_key_auth must not carry 007's comment
        marker; marked -> no-op, exit 0

The owner is ALLOWED_EMAIL in the same .env, the address backend/auth.py reads.
002 backfills every existing row to that account, and the 002 / 006 / 007
checks look it up, so there is no default: unset, those runs stop and
change nothing.

Never prints secret values (the owner's user id is an identifier, not a secret).
"""

import hashlib
import sys
from pathlib import Path

import psycopg
from psycopg import sql as pgsql

MIGRATIONS_DIR = Path(__file__).resolve().parent
REPO_ROOT = MIGRATIONS_DIR.parent.parent
ENV_FILE = REPO_ROOT / ".env"
SQL_FILE = MIGRATIONS_DIR / "001_init.sql"
SQL_FILE_002 = MIGRATIONS_DIR / "002_multiuser.sql"
SQL_FILE_003 = MIGRATIONS_DIR / "003_weights.sql"
SQL_FILE_004 = MIGRATIONS_DIR / "004_entries_food_idx.sql"
SQL_FILE_005 = MIGRATIONS_DIR / "005_oura.sql"
SQL_FILE_006 = MIGRATIONS_DIR / "006_api_keys.sql"
SQL_FILE_007 = MIGRATIONS_DIR / "007_api_key_auth.sql"

EXPECTED_TABLES = {
    "foods",
    "aliases",
    "meal_macros",
    "logs",
    "item_resolutions",
    "entries",
    "settings",
}
# 002: the six tables that become user-owned (+ the ledger it creates)
TENANT_TABLES = ("aliases", "meal_macros", "logs", "item_resolutions", "entries", "settings")
RLS_TABLES = TENANT_TABLES + ("quota_events",)
APP_ROLE = "tally_app"


def read_env(env_file: Path, name: str) -> str:
    if not env_file.is_file():
        sys.exit(f"ERROR: {env_file} not found.")
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            value = value.strip().strip('"').strip("'")
            if not value:
                sys.exit(f"ERROR: {name} is empty in .env.")
            return value
    sys.exit(f"ERROR: {name} not found in .env.")


def owner_email() -> str:
    """The owner's address: ALLOWED_EMAIL in .env, or exit. Read only by the
    paths that need the owner, after their guards, so re-running an applied
    migration stays a no-op without it."""
    return read_env(ENV_FILE, "ALLOWED_EMAIL")


def existing_tables(cur) -> set[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    return {row[0] for row in cur.fetchall()}


def apply_001(conn) -> None:
    sql = SQL_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        existing = existing_tables(cur)
        if existing:
            names = ", ".join(sorted(existing))
            if existing == EXPECTED_TABLES:
                print(
                    "Guard: schema public already contains the 7 expected tables "
                    f"({names}). Migration already applied - nothing to do."
                )
                sys.exit(0)
            sys.exit(
                "ERROR: refusing to apply - schema public is not empty and does not "
                f"match an applied migration. Found {len(existing)} table(s): {names}. "
                "This migration only targets a brand-new database. No changes made."
            )

        # Empty public schema: apply the whole migration in this one transaction.
        cur.execute(sql)
    conn.commit()
    print("Applied 001_init.sql: 7 tables created, settings seeded.")


# ---------------------------------------------------------------------------
# 002
# ---------------------------------------------------------------------------


def has_002(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'logs' AND column_name = 'user_id'"
    )
    return cur.fetchone() is not None


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT count(*) FROM {table}")
    return cur.fetchone()[0]


def verify_002(cur, before: dict[str, int]) -> str:
    """Assert the post-002 invariants inside the still-open transaction.
    Returns the owner's user id. Any AssertionError rolls the migration back."""
    cur.execute(
        'SELECT id::text FROM neon_auth."user" WHERE lower(email) = lower(%s)', (owner_email(),)
    )
    owners = cur.fetchall()
    assert len(owners) == 1, f"expected exactly one Neon Auth user for the owner, found {len(owners)}"
    owner_id = owners[0][0]

    for table, n in before.items():
        assert _count(cur, table) == n, f"{table}: row count changed ({n} -> {_count(cur, table)})"
    for table in TENANT_TABLES:
        cur.execute(f"SELECT count(*) FROM {table} WHERE user_id IS DISTINCT FROM %s", (owner_id,))
        stray = cur.fetchone()[0]
        assert stray == 0, f"{table}: {stray} row(s) not backfilled to the owner"

    cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
    role = cur.fetchone()
    assert role is not None and role[0] is False, f"role {APP_ROLE} missing or BYPASSRLS"
    cur.execute(
        "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relrowsecurity AND c.relforcerowsecurity"
    )
    rls = {r[0] for r in cur.fetchall()}
    # 003's `weights`, 005's Oura tables and 006's `api_keys` are RLS-covered
    # too once they exist (a --check after 003 / 005 / 006)
    expected_rls = (
        set(RLS_TABLES)
        | ({"weights"} if has_003(cur) else set())
        | (set(OURA_TABLES) if has_005(cur) else set())
        | ({"api_keys"} if has_006(cur) else set())
    )
    assert rls == expected_rls, f"RLS enabled+forced on {sorted(rls)}, expected {sorted(expected_rls)}"
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
    assert cur.fetchone()[0] == len(expected_rls)
    # the ledger's one-log-a-day rule (002 §5): a partial unique index
    cur.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname = 'quota_events_log_day_key'"
    )
    idx = cur.fetchone()
    assert (
        idx is not None
        and idx[0].startswith("CREATE UNIQUE INDEX")
        and "WHERE (kind = 'log'::text)" in idx[0]
    ), "quota_events_log_day_key missing, or not a partial unique index"

    # Under the app role: the owner sees everything, another tenant nothing.
    cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
    cur.execute("SELECT set_config('app.user_id', %s, true)", (owner_id,))
    for table in TENANT_TABLES:
        assert _count(cur, table) == before[table], f"{table}: owner cannot see own rows under RLS"
    cur.execute("SELECT set_config('app.user_id', '002-verify-other', true)")
    for table in TENANT_TABLES:
        assert _count(cur, table) == 0, f"{table}: RLS leaks rows to another tenant"
    assert _count(cur, "foods") == before["foods"], "foods must stay global"

    # No tenant bound: an insert fails loudly, inside a savepoint — the policy's
    # WITH CHECK refuses the NULL user_id (InsufficientPrivilege) before the
    # NOT NULL constraint would; either is the fail-closed outcome.
    cur.execute("SAVEPOINT no_tenant")
    cur.execute("SELECT set_config('app.user_id', '', true)")
    try:
        cur.execute("INSERT INTO settings (key, value) VALUES ('002-verify', '1'::jsonb)")
    except (psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege):
        pass
    else:
        raise AssertionError("insert with no app.user_id must fail")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT no_tenant")
    cur.execute("SET LOCAL ROLE NONE")
    return owner_id


def apply_002(conn, dry_run: bool) -> None:
    sql = SQL_FILE_002.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        existing = existing_tables(cur)
        if existing != EXPECTED_TABLES:
            names = ", ".join(sorted(existing)) or "(none)"
            sys.exit(
                "ERROR: refusing to apply 002 - schema public does not match an applied 001. "
                f"Found: {names}. No changes made."
            )
        if has_002(cur):
            print("Guard: logs.user_id already exists - 002 already applied, nothing to do.")
            sys.exit(0)
        # 002's first block reads the owner's address from this transaction-local
        # setting: bound as a parameter, never pasted into the SQL.
        cur.execute("SELECT set_config('app.owner_email', %s, true)", (owner_email(),))
        # The locks the ALTERs take anyway, up front: the verify counts must see
        # the same rows even if the (old) app writes while this runs.
        cur.execute(f"LOCK TABLE {', '.join(sorted(EXPECTED_TABLES))} IN ACCESS EXCLUSIVE MODE")
        before = {t: _count(cur, t) for t in sorted(EXPECTED_TABLES)}
        cur.execute(sql)
        assert _count(cur, "quota_events") == 0  # just created
        owner_id = verify_002(cur, before)
    if dry_run:
        conn.rollback()
        print(
            f"DRY RUN: 002_multiuser.sql applied and verified, then ROLLED BACK. "
            f"Owner user id {owner_id}; row counts {before}."
        )
        return
    conn.commit()
    print(
        f"Applied 002_multiuser.sql: user_id on {', '.join(TENANT_TABLES)} (backfilled to "
        f"owner {owner_id}), quota_events created, RLS enabled+forced on {len(RLS_TABLES)} "
        f"tables, role {APP_ROLE} granted. Row counts {before}."
    )


def check_002(conn) -> None:
    """`--check`: 002's own verification against the committed schema, in a
    transaction that is rolled back. The checks assume one tenant (every row
    the owner's, the owner sees every row), so run it right after applying."""
    with conn.cursor() as cur:
        if not has_002(cur):
            sys.exit("ERROR: 002 is not applied (logs.user_id absent) - nothing to check.")
        counts = {t: _count(cur, t) for t in sorted(EXPECTED_TABLES)}
        owner_id = verify_002(cur, counts)
    conn.rollback()
    print(f"CHECK OK: 002 invariants hold. Owner user id {owner_id}; row counts {counts}.")


# ---------------------------------------------------------------------------
# 003 — the weigh-in log (agent mode, 2026-09-06)
# ---------------------------------------------------------------------------


def has_003(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'weights'"
    )
    return cur.fetchone() is not None


def verify_003(cur) -> None:
    """003's invariants inside the still-open transaction: RLS enabled AND
    forced with one policy, one weigh-in per user per day (updated in place),
    the app role writes and reads its own rows and sees nobody else's, and a
    write with no tenant bound fails. Every probe row is rolled back to a
    savepoint, so a real apply commits nothing but the table."""
    cur.execute(
        "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = 'weights'"
    )
    assert cur.fetchone() == (True, True), "weights: RLS must be enabled and forced"
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = 'weights'")
    assert cur.fetchone()[0] == 1, "weights: expected exactly one policy"
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'weights' "
        "AND indexdef LIKE 'CREATE UNIQUE INDEX%(user_id, day)'"
    )
    assert cur.fetchone() is not None, "weights: UNIQUE (user_id, day) missing"

    cur.execute("SAVEPOINT verify_003")
    try:
        cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
        cur.execute("SELECT set_config('app.user_id', '003-verify-a', true)")
        cur.execute(
            "INSERT INTO weights (day, weight_kg, unit, source) VALUES (current_date, 80, 'kg', 'manual')"
        )
        cur.execute(
            "INSERT INTO weights (day, weight_kg, unit, source) VALUES (current_date, 81, 'kg', 'manual') "
            "ON CONFLICT (user_id, day) DO UPDATE SET weight_kg = EXCLUDED.weight_kg"
        )
        cur.execute("SELECT count(*), max(weight_kg) FROM weights")
        assert cur.fetchone() == (1, 81), "weights: one row per user per day, updated in place"
        cur.execute("SELECT set_config('app.user_id', '003-verify-b', true)")
        cur.execute("SELECT count(*) FROM weights")
        assert cur.fetchone()[0] == 0, "weights: RLS leaks rows to another tenant"
        cur.execute("SELECT set_config('app.user_id', '', true)")
        try:
            cur.execute(
                "INSERT INTO weights (day, weight_kg, unit, source) VALUES (current_date, 80, 'kg', 'manual')"
            )
        except (psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege):
            pass
        else:
            raise AssertionError("weights: insert with no app.user_id must fail")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT verify_003")  # probes gone, SET LOCAL ROLE undone with them
    cur.execute("SET LOCAL ROLE NONE")


def apply_003(conn, dry_run: bool) -> None:
    sql = SQL_FILE_003.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        if not has_002(cur):
            sys.exit("ERROR: refusing to apply 003 - 002 (logs.user_id) is not applied. No changes made.")
        if has_003(cur):
            print("Guard: table weights already exists - 003 already applied, nothing to do.")
            sys.exit(0)
        cur.execute(sql)
        verify_003(cur)
    if dry_run:
        conn.rollback()
        print("DRY RUN: 003_weights.sql applied and verified, then ROLLED BACK.")
        return
    conn.commit()
    print("Applied 003_weights.sql: weights created, RLS enabled+forced, one policy, one weigh-in a day.")


def check_003(conn) -> None:
    """`--check`: 003's own verification against the committed schema, rolled back."""
    with conn.cursor() as cur:
        if not has_003(cur):
            sys.exit("ERROR: 003 is not applied (table weights absent) - nothing to check.")
        verify_003(cur)
    conn.rollback()
    print("CHECK OK: 003 invariants hold.")


# ---------------------------------------------------------------------------
# 004 — past_foods' index (agent read tool, 2026-09-08)
# ---------------------------------------------------------------------------


def has_004(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname = 'entries_user_food_logged_idx'"
    )
    return cur.fetchone() is not None


def verify_004(cur) -> None:
    """004's one invariant: the index exists on `entries` over the columns
    past_foods' DISTINCT ON walks. Nothing else changed — no table, no policy,
    no row."""
    assert has_004(cur), "entries_user_food_logged_idx missing"
    cur.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'entries_user_food_logged_idx'"
    )
    indexdef = cur.fetchone()[0]
    for column in ("user_id", "food_id", "logged_at"):
        assert column in indexdef, f"{column} missing from the index: {indexdef}"


def apply_004(conn, dry_run: bool) -> None:
    sql = SQL_FILE_004.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        if not has_002(cur):
            sys.exit("ERROR: refusing to apply 004 - 002 (logs.user_id) is not applied. No changes made.")
        already = has_004(cur)
        cur.execute(sql)
        verify_004(cur)
    if dry_run:
        conn.rollback()
        print("DRY RUN: 004_entries_food_idx.sql applied and verified, then ROLLED BACK.")
        return
    conn.commit()
    print(
        "Applied 004_entries_food_idx.sql: entries_user_food_logged_idx "
        + ("already present, nothing to do." if already else "created.")
    )


def check_004(conn) -> None:
    """`--check`: 004's own verification against the committed schema, rolled back."""
    with conn.cursor() as cur:
        if not has_004(cur):
            sys.exit("ERROR: 004 is not applied (entries_user_food_logged_idx absent) - nothing to check.")
        verify_004(cur)
    conn.rollback()
    print("CHECK OK: 004 invariants hold.")


# ---------------------------------------------------------------------------
# 005 — the Oura connection and what it syncs (2026-09-21)
# ---------------------------------------------------------------------------

OURA_TABLES = ("oura_tokens", "oura_days", "oura_workouts")
# one probe row per table, written as the app role by verify_005
_OURA_PROBES = {
    "oura_tokens": "INSERT INTO oura_tokens (access_token, refresh_token, expires_at) "
    "VALUES ('a', 'r', now())",
    "oura_days": "INSERT INTO oura_days (day, raw) VALUES (current_date, '{}'::jsonb)",
    "oura_workouts": "INSERT INTO oura_workouts (oura_id, day, activity, raw) "
    "VALUES ('probe', current_date, 'walking', '{}'::jsonb)",
}


def has_005(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'oura_tokens'"
    )
    return cur.fetchone() is not None


def verify_005(cur) -> None:
    """005's invariants inside the still-open transaction, per table: RLS
    enabled AND forced with one policy, the app role writes and reads its own
    rows and sees nobody else's, and a write with no tenant bound fails. Every
    probe row is rolled back to a savepoint, so a real apply commits nothing
    but the tables."""
    for table in OURA_TABLES:
        cur.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s",
            (table,),
        )
        assert cur.fetchone() == (True, True), f"{table}: RLS must be enabled and forced"
        cur.execute(
            "SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = %s", (table,)
        )
        assert cur.fetchone()[0] == 1, f"{table}: expected exactly one policy"

    cur.execute("SAVEPOINT verify_005")
    try:
        cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
        for table, probe in _OURA_PROBES.items():
            cur.execute("SELECT set_config('app.user_id', '005-verify-a', true)")
            cur.execute(probe)
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 1, f"{table}: the owner cannot read its own row"
            cur.execute("SELECT set_config('app.user_id', '005-verify-b', true)")
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 0, f"{table}: RLS leaks rows to another tenant"
        cur.execute("SELECT set_config('app.user_id', '', true)")
        for table, probe in _OURA_PROBES.items():
            cur.execute("SAVEPOINT verify_005_untenanted")
            try:
                cur.execute(probe)
            except (psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege,
                    psycopg.errors.CheckViolation):
                cur.execute("ROLLBACK TO SAVEPOINT verify_005_untenanted")
            else:
                raise AssertionError(f"{table}: insert with no app.user_id must fail")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT verify_005")  # probes gone, SET LOCAL ROLE undone with them
    cur.execute("SET LOCAL ROLE NONE")


def apply_005(conn, dry_run: bool) -> None:
    sql = SQL_FILE_005.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        if not has_002(cur):
            sys.exit("ERROR: refusing to apply 005 - 002 (logs.user_id) is not applied. No changes made.")
        if has_005(cur):
            print("Guard: table oura_tokens already exists - 005 already applied, nothing to do.")
            sys.exit(0)
        cur.execute(sql)
        verify_005(cur)
    if dry_run:
        conn.rollback()
        print("DRY RUN: 005_oura.sql applied and verified, then ROLLED BACK.")
        return
    conn.commit()
    print("Applied 005_oura.sql: oura_tokens, oura_days, oura_workouts created, RLS enabled+forced.")


def check_005(conn) -> None:
    """`--check`: 005's own verification against the committed schema, rolled back."""
    with conn.cursor() as cur:
        if not has_005(cur):
            sys.exit("ERROR: 005 is not applied (table oura_tokens absent) - nothing to check.")
        verify_005(cur)
    conn.rollback()
    print("CHECK OK: 005 invariants hold.")


# ---------------------------------------------------------------------------
# 006 — personal API keys (REST + MCP, 2026-09-23)
# ---------------------------------------------------------------------------


def has_006(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'api_keys'"
    )
    return cur.fetchone() is not None


def _probe_hash(key_id: str) -> str:
    """The stored hash of a well-formed probe key (backend/api_keys.py's format)."""
    return hashlib.sha256(f"tally_live_{key_id}_{'0' * 64}".encode("utf-8")).hexdigest()


def verify_006(cur) -> None:
    """006's invariants inside the still-open transaction: RLS enabled AND
    forced with one policy; the app role reads and writes its own keys and
    sees nobody else's; a write with no tenant bound fails; and api_key_auth,
    called as the app role with NO tenant bound (how a keyed request starts),
    names the key's owner for the right hash and nothing for a wrong hash, a
    revoked key or an expired key. The probe keys belong to the owner — the
    function joins neon_auth for the email, so the user must be real — and
    are rolled back to a savepoint, so a real apply commits no key."""
    cur.execute(
        "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = 'api_keys'"
    )
    assert cur.fetchone() == (True, True), "api_keys: RLS must be enabled and forced"
    cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = 'api_keys'")
    assert cur.fetchone()[0] == 1, "api_keys: expected exactly one policy"
    cur.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner) = current_user, "
        "has_function_privilege(%s, p.oid, 'EXECUTE'), "
        # a NULL acl is the default one, which lets PUBLIC execute
        "p.proacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee = 0) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'api_key_auth'",
        (APP_ROLE,),
    )
    assert cur.fetchone() == (True, True, True, False), (
        "api_key_auth: must be SECURITY DEFINER, owned by the migrating role, "
        f"executable by {APP_ROLE} and not by PUBLIC"
    )
    email = owner_email()
    cur.execute(
        'SELECT id::text FROM neon_auth."user" WHERE lower(email) = lower(%s)', (email,)
    )
    owners = cur.fetchall()
    assert len(owners) == 1, f"expected exactly one Neon Auth user for the owner, found {len(owners)}"
    owner_id = owners[0][0]
    live_hash = _probe_hash("006c0ffee0000001")
    revoked_hash = _probe_hash("006c0ffee0000002")
    expired_hash = _probe_hash("006c0ffee0000003")

    cur.execute("SAVEPOINT verify_006")
    try:
        cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
        cur.execute("SELECT set_config('app.user_id', %s, true)", (owner_id,))
        cur.execute(
            "INSERT INTO api_keys (id, label, key_hash, last_four, scopes, revoked_at, expires_at) VALUES "
            "('006c0ffee0000001', '006 verify', %s, 'live', '{read}', NULL, NULL), "
            "('006c0ffee0000002', '006 verify', %s, 'revk', '{read,write}', now(), NULL), "
            "('006c0ffee0000003', '006 verify', %s, 'expd', '{read}', NULL, now() - interval '1 day')",
            (live_hash, revoked_hash, expired_hash),
        )
        cur.execute("SELECT count(*) FROM api_keys WHERE id LIKE '006c0ffee%'")
        assert cur.fetchone()[0] == 3, "api_keys: the owner cannot see their own keys under RLS"
        cur.execute("SELECT set_config('app.user_id', '006-verify-b', true)")
        cur.execute("SELECT count(*) FROM api_keys WHERE id LIKE '006c0ffee%'")
        assert cur.fetchone()[0] == 0, "api_keys: RLS leaks rows to another tenant"

        cur.execute("SELECT set_config('app.user_id', '', true)")
        cur.execute("SELECT user_id, lower(email), email_verified, scopes FROM api_key_auth(%s, %s)",
                    ("006c0ffee0000001", live_hash))
        assert cur.fetchall() == [(owner_id, email.lower(), True, ["read"])], (
            "api_key_auth: the right hash must name the key's owner with no tenant bound"
        )
        for key_id, key_hash, why in (
            ("006c0ffee0000001", revoked_hash, "a wrong hash"),
            ("006c0ffee0000002", revoked_hash, "a revoked key"),
            ("006c0ffee0000003", expired_hash, "an expired key"),
        ):
            cur.execute("SELECT count(*) FROM api_key_auth(%s, %s)", (key_id, key_hash))
            assert cur.fetchone()[0] == 0, f"api_key_auth: {why} must name nobody"
        try:
            cur.execute(
                "INSERT INTO api_keys (id, label, key_hash, last_four, scopes) "
                "VALUES ('006c0ffee0000004', '006 verify', %s, 'none', '{read}')",
                (_probe_hash("006c0ffee0000004"),),
            )
        except (psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege):
            pass
        else:
            raise AssertionError("api_keys: insert with no app.user_id must fail")
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT verify_006")  # probes gone, SET LOCAL ROLE undone with them
    cur.execute("SET LOCAL ROLE NONE")


def apply_006(conn, dry_run: bool) -> None:
    sql = SQL_FILE_006.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        if not has_002(cur):
            sys.exit("ERROR: refusing to apply 006 - 002 (logs.user_id) is not applied. No changes made.")
        if has_006(cur):
            print("Guard: table api_keys already exists - 006 already applied, nothing to do.")
            sys.exit(0)
        cur.execute(sql)
        verify_006(cur)
    if dry_run:
        conn.rollback()
        print("DRY RUN: 006_api_keys.sql applied and verified, then ROLLED BACK.")
        return
    conn.commit()
    print(
        "Applied 006_api_keys.sql: api_keys created, RLS enabled+forced, one policy, "
        f"api_key_auth (SECURITY DEFINER) executable by {APP_ROLE} only."
    )


def check_006(conn) -> None:
    """`--check`: 006's own verification against the committed schema, rolled back."""
    with conn.cursor() as cur:
        if not has_006(cur):
            sys.exit("ERROR: 006 is not applied (table api_keys absent) - nothing to check.")
        verify_006(cur)
    conn.rollback()
    print("CHECK OK: 006 invariants hold.")


# ---------------------------------------------------------------------------
# 007 — api_key_auth checks the account before it stamps (2026-09-23)
# ---------------------------------------------------------------------------

# A value per type for the probe account's NOT NULL columns that have no default
_PROBE_VALUES = {
    "text": pgsql.SQL("'007-verify'"),
    "character varying": pgsql.SQL("'007-verify'"),
    "uuid": pgsql.SQL("gen_random_uuid()"),
    "boolean": pgsql.SQL("false"),
    "timestamp with time zone": pgsql.SQL("now()"),
    "timestamp without time zone": pgsql.SQL("now()"),
}


def has_007(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'api_key_auth' "
        "AND obj_description(p.oid, 'pg_proc') LIKE '007:%'"
    )
    return cur.fetchone() is not None


def verify_007(cur) -> None:
    """007's invariants inside the still-open transaction: everything 006
    checks still holds (owner, grants, the answers for right and wrong keys);
    a banned account's live key names nobody AND is left unstamped; and the
    owner's live key still resolves and is stamped. The banned account is a
    probe row in neon_auth."user" whose NOT NULL columns are read from
    information_schema, so a Neon Auth schema change fails loudly here. It
    and the probe keys are rolled back to a savepoint, so a real apply
    commits only the function."""
    verify_006(cur)
    assert has_007(cur), "api_key_auth: the 007 comment marker is missing"
    cur.execute(
        'SELECT id::text FROM neon_auth."user" WHERE lower(email) = lower(%s)', (owner_email(),)
    )
    owners = cur.fetchall()
    assert len(owners) == 1, f"expected exactly one Neon Auth user for the owner, found {len(owners)}"
    owner_id = owners[0][0]
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'neon_auth' AND table_name = 'user' "
        "AND is_nullable = 'NO' AND column_default IS NULL"
    )
    probe = {}
    for name, dtype in cur.fetchall():
        assert dtype in _PROBE_VALUES, f'neon_auth."user".{name}: no probe value for a NOT NULL {dtype}'
        probe[name] = _PROBE_VALUES[dtype]
    probe |= {"email": pgsql.Literal("007-verify-banned@example.invalid"), "banned": pgsql.SQL("true")}
    banned_hash = _probe_hash("007c0ffee0000001")
    owner_hash = _probe_hash("007c0ffee0000002")

    cur.execute("SAVEPOINT verify_007")
    try:
        cur.execute(  # as the migrating role: the app role cannot write neon_auth
            pgsql.SQL('INSERT INTO neon_auth."user" ({}) VALUES ({}) RETURNING id::text').format(
                pgsql.SQL(", ").join(map(pgsql.Identifier, probe)), pgsql.SQL(", ").join(probe.values())
            )
        )
        banned_id = cur.fetchone()[0]
        cur.execute(f"SET LOCAL ROLE {APP_ROLE}")
        for user_id, key_id, key_hash in (
            (banned_id, "007c0ffee0000001", banned_hash),
            (owner_id, "007c0ffee0000002", owner_hash),
        ):
            cur.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
            cur.execute(
                "INSERT INTO api_keys (id, label, key_hash, last_four, scopes) "
                "VALUES (%s, '007 verify', %s, 'prob', '{read}')",
                (key_id, key_hash),
            )

        cur.execute("SELECT set_config('app.user_id', '', true)")
        cur.execute("SELECT count(*) FROM api_key_auth(%s, %s)", ("007c0ffee0000001", banned_hash))
        assert cur.fetchone()[0] == 0, "api_key_auth: a banned account's key must name nobody"
        cur.execute("SELECT user_id FROM api_key_auth(%s, %s)", ("007c0ffee0000002", owner_hash))
        assert cur.fetchall() == [(owner_id,)], "api_key_auth: the owner's live key must still resolve"

        # last_used_at = now(): NULL while unstamped, true once stamped in this transaction
        for user_id, key_id, stamped, why in (
            (banned_id, "007c0ffee0000001", None, "a refused key must stay unstamped"),
            (owner_id, "007c0ffee0000002", True, "a used key must be stamped"),
        ):
            cur.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
            cur.execute("SELECT last_used_at = now() FROM api_keys WHERE id = %s", (key_id,))
            assert cur.fetchone() == (stamped,), f"api_key_auth: {why}"
    finally:
        cur.execute("ROLLBACK TO SAVEPOINT verify_007")  # probes gone, SET LOCAL ROLE undone with them
    cur.execute("SET LOCAL ROLE NONE")


def apply_007(conn, dry_run: bool) -> None:
    sql = SQL_FILE_007.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        if not has_006(cur):
            sys.exit("ERROR: refusing to apply 007 - 006 (table api_keys) is not applied. No changes made.")
        if has_007(cur):
            print("Guard: api_key_auth already carries the 007 marker - 007 already applied, nothing to do.")
            sys.exit(0)
        cur.execute(sql)
        verify_007(cur)
    if dry_run:
        conn.rollback()
        print("DRY RUN: 007_api_key_auth.sql applied and verified, then ROLLED BACK.")
        return
    conn.commit()
    print(
        "Applied 007_api_key_auth.sql: api_key_auth refuses a banned or deleted account before "
        f"it stamps last_used_at; still SECURITY DEFINER, executable by {APP_ROLE} only."
    )


def check_007(conn) -> None:
    """`--check`: 007's own verification against the committed schema, rolled back."""
    with conn.cursor() as cur:
        if not has_007(cur):
            sys.exit("ERROR: 007 is not applied (api_key_auth has no 007 marker) - nothing to check.")
        verify_007(cur)
    conn.rollback()
    print("CHECK OK: 007 invariants hold.")


def main(argv: list[str]) -> None:
    dry_run = "--dry-run" in argv
    check = "--check" in argv
    args = [a for a in argv if a not in ("--dry-run", "--check")]
    which = args[0] if args else "001"
    if which not in ("001", "002", "003", "004", "005", "006", "007") or (which == "001" and (dry_run or check)) or (dry_run and check):
        sys.exit(__doc__)
    dsn = read_env(ENV_FILE, "DATABASE_URL")
    with psycopg.connect(dsn) as conn:
        if which == "001":
            apply_001(conn)
        elif which == "005":
            check_005(conn) if check else apply_005(conn, dry_run)
        elif which == "007":
            check_007(conn) if check else apply_007(conn, dry_run)
        elif which == "006":
            check_006(conn) if check else apply_006(conn, dry_run)
        elif which == "004":
            check_004(conn) if check else apply_004(conn, dry_run)
        elif which == "003":
            check_003(conn) if check else apply_003(conn, dry_run)
        elif check:
            check_002(conn)
        else:
            apply_002(conn, dry_run)


if __name__ == "__main__":
    main(sys.argv[1:])
