from contextlib import contextmanager

from database import db


def test_owner_schema_uses_backend_aware_column_lookup(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, sql, params=()):
            executed.append((sql, tuple(params)))
            return self

        def fetchall(self):
            return [
                {"column_name": "ban_reason"},
                {"column_name": "banned_at"},
            ]

    @contextmanager
    def fake_db_cursor():
        yield FakeCursor()

    # Fake cursor so the schema check needs no live database connection
    # (PostgreSQL is the only backend — table_columns always uses
    # information_schema through db_cursor).
    monkeypatch.setattr(db, "db_cursor", fake_db_cursor)

    db.ensure_owner_schema()

    sql_statements = [sql for sql, _params in executed]
    assert any("information_schema.columns" in sql for sql in sql_statements)
    assert any("CREATE TABLE IF NOT EXISTS extra_admins" in sql for sql in sql_statements)
    assert not any("PRAGMA" in sql.upper() for sql in sql_statements)
