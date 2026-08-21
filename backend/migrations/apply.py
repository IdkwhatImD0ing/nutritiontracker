"""Apply 001_init.sql to the Neon database named by DATABASE_URL in the repo-root .env.

Guard: this migration targets a brand-new database. If ANY user table already
exists in schema public, nothing is applied:
  - exactly the 7 expected tables present  -> already applied, no-op, exit 0
  - anything else present                  -> unexpected state, abort, exit 1

Never prints secret values.
"""

import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent
REPO_ROOT = MIGRATIONS_DIR.parent.parent
ENV_FILE = REPO_ROOT / ".env"
SQL_FILE = MIGRATIONS_DIR / "001_init.sql"

EXPECTED_TABLES = {
    "foods",
    "aliases",
    "meal_macros",
    "logs",
    "item_resolutions",
    "entries",
    "settings",
}


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


def main() -> None:
    dsn = read_database_url(ENV_FILE)
    sql = SQL_FILE.read_text(encoding="utf-8")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            existing = {row[0] for row in cur.fetchall()}

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


if __name__ == "__main__":
    main()
