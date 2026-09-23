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
  python apply.py 006            -> 006_api_keys.sql (personal API keys; needs 002, no-op if applied)
  python apply.py 006 --dry-run  -> apply 006 + verify inside one transaction, then ROLL BACK
  python apply.py 006 --check    -> re-run 006's verification against the committed schema

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
  006 - logs.user_id must be present (002 applied) and `api_keys` absent; present -> no-op, exit 0

Never prints secret values (the owner's user id is an identifier, not a secret).
"""

import hashlib
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent
REPO_ROOT = MIGRATIONS_DIR.parent.parent
ENV_FILE = REPO_ROOT / ".env"
SQL_FILE = MIGRATIONS_DIR / "001_init.sql"
SQL_FILE_002 = MIGRATIONS_DIR / "002_multiuser.sql"
SQL_FILE_003 = MIGRATIONS_DIR / "003_weights.sql"
SQL_FILE_004 = MIGRATIONS_DIR / "004_entries_food_idx.sql"
SQL_FILE_006 = MIGRATIONS_DIR / "006_api_keys.sql"

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
OWNER_EMAIL = "billzhangsc@gmail.com"  # auth.DEFAULT_ALLOWED_EMAIL; 002's DO block repeats it


def read_database_url(env_file: Path) -> str:
    if not env_file.is_file():
        sys.exit(f"ERROR: {env_file} not found.")
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DATABASE_URL":
            value = value.strip().strip('"').strip("'")
            if not value:
                sys.exit("ERROR: DATABASE_URL is empty in .env.")
            return value
    sys.exit("ERROR: DATABASE_URL not found in .env.")


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
        'SELECT id::text FROM neon_auth."user" WHERE lower(email) = lower(%s)', (OWNER_EMAIL,)
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
    # 003's `weights` and 006's `api_keys` are RLS-covered too once they exist
    # (a --check after 003 / 006)
    expected_rls = (
        set(RLS_TABLES)
        | ({"weights"} if has_003(cur) else set())
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
    cur.execute(
        'SELECT id::text FROM neon_auth."user" WHERE lower(email) = lower(%s)', (OWNER_EMAIL,)
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
        assert cur.fetchall() == [(owner_id, OWNER_EMAIL.lower(), True, ["read"])], (
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


def main(argv: list[str]) -> None:
    dry_run = "--dry-run" in argv
    check = "--check" in argv
    args = [a for a in argv if a not in ("--dry-run", "--check")]
    which = args[0] if args else "001"
    if which not in ("001", "002", "003", "004", "006") or (which == "001" and (dry_run or check)) or (dry_run and check):
        sys.exit(__doc__)
    dsn = read_database_url(ENV_FILE)
    with psycopg.connect(dsn) as conn:
        if which == "001":
            apply_001(conn)
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
