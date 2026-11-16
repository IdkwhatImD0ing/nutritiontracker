"""migrations/apply.py's .env reads, offline (BACKLOG 109): the owner's address
is ALLOWED_EMAIL from the repo-root .env and has no default. The migration
itself is covered where it can run: test_tenancy.py."""

from __future__ import annotations

import pytest

from migrations import apply


def _env_file(tmp_path, monkeypatch, body: str):
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    monkeypatch.setattr(apply, "ENV_FILE", env_file)
    return env_file


def test_owner_email_is_allowed_email_from_the_env_file(tmp_path, monkeypatch):
    env_file = _env_file(
        tmp_path, monkeypatch, '# a comment\nDATABASE_URL=postgres://x\nALLOWED_EMAIL="Owner@Example.com"\n'
    )
    monkeypatch.setenv("ALLOWED_EMAIL", "process-env@example.com")  # only the file counts
    assert apply.owner_email() == "Owner@Example.com"
    assert apply.read_env(env_file, "DATABASE_URL") == "postgres://x"


@pytest.mark.parametrize(
    ("body", "said"),
    [("DATABASE_URL=postgres://x\n", "not found"), ("ALLOWED_EMAIL=\n", "is empty")],
)
def test_owner_email_has_no_default(tmp_path, monkeypatch, body, said):
    _env_file(tmp_path, monkeypatch, body)
    with pytest.raises(SystemExit) as exc:
        apply.owner_email()
    assert exc.value.code == f"ERROR: ALLOWED_EMAIL {said} in .env."


# ---------------------------------------------------------------------------
# apply_002 against a fake connection: what it sends, in what order
# ---------------------------------------------------------------------------

SQL_002 = apply.SQL_FILE_002.read_text(encoding="utf-8")


class _Reached002(Exception):
    """The fake stops the run at the migration's own SQL."""


class _Cursor:
    def __init__(self, applied: bool):
        self.applied = applied
        self.calls: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == SQL_002:
            raise _Reached002

    def fetchall(self):  # existing_tables: 001 is applied
        return [(t,) for t in apply.EXPECTED_TABLES]

    def fetchone(self):  # has_002's probe, else a row count
        if "column_name = 'user_id'" in self.calls[-1][0]:
            return (1,) if self.applied else None
        return (0,)


class _Conn:
    def __init__(self, applied: bool):
        self.cur = _Cursor(applied)

    def cursor(self):
        return self.cur


def test_apply_002_on_an_applied_database_is_a_no_op_that_needs_no_owner(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch, "DATABASE_URL=postgres://x\n")  # no ALLOWED_EMAIL
    conn = _Conn(applied=True)
    with pytest.raises(SystemExit) as exc:
        apply.apply_002(conn, dry_run=False)
    assert exc.value.code == 0
    assert len(conn.cur.calls) == 2  # the two guard reads and nothing after them


def test_apply_002_without_the_owner_stops_before_it_changes_anything(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch, "DATABASE_URL=postgres://x\n")
    conn = _Conn(applied=False)
    with pytest.raises(SystemExit) as exc:
        apply.apply_002(conn, dry_run=True)
    assert exc.value.code == "ERROR: ALLOWED_EMAIL not found in .env."
    assert len(conn.cur.calls) == 2  # no lock taken, no SQL run


def test_apply_002_binds_the_owner_as_a_parameter_before_the_sql(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch, "ALLOWED_EMAIL=o'brien@example.com\n")
    conn = _Conn(applied=False)
    with pytest.raises(_Reached002):
        apply.apply_002(conn, dry_run=True)
    statements = [sql for sql, _ in conn.cur.calls]
    bind = statements.index("SELECT set_config('app.owner_email', %s, true)")
    assert conn.cur.calls[bind][1] == ("o'brien@example.com",)
    assert bind < statements.index(SQL_002)
    assert "current_setting('app.owner_email')" in SQL_002 and "@" not in SQL_002
