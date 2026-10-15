"""Shared PostgreSQL test harness — the suite is PostgreSQL-only.

Production runs exclusively on PostgreSQL (NEON_DATABASE_URL), so the tests
must exercise the SAME backend. This module provides:

  * ``server_uri()``  — URI of the test PostgreSQL server. Uses an external
    server when ``TEST_PG_DSN`` / ``NEON_DATABASE_URL`` is already set
    (CI service container), otherwise boots a throwaway embedded PostgreSQL
    (``embedded-postgres`` package) under ``temp/``.
  * ``create_test_database(name)`` — DROP + CREATE a dedicated database and
    return its connection URI. Every test module gets its own database, so
    modules can never leak rows into each other.
  * ``drop_test_database(name)`` — best-effort cleanup.

conftest.py calls ``bootstrap()`` BEFORE any project module is imported so
``database.pg`` resolves ``NEON_DATABASE_URL`` to the test server exactly
once, the same way production resolves it.
"""

from __future__ import annotations

import os
import shutil
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)

_SERVER_URI: str | None = None
_EMBEDDED = None  # EmbeddedPostgres instance (kept alive for the session)


def _external_uri() -> str | None:
    # TEST_PG_DSN is the ONLY honored external override. A pre-existing
    # NEON_DATABASE_URL in the shell is deliberately ignored: it usually
    # points at the real production database, and tests must never touch it.
    val = (os.environ.get("TEST_PG_DSN") or "").strip()
    if val.startswith("postgres"):
        return val
    return None


def _swap_dbname(uri: str, dbname: str) -> str:
    """Return ``uri`` with its database name replaced by ``dbname``."""
    if "?" in uri:
        base, query = uri.split("?", 1)
        prefix = base.rstrip("/").rsplit("/", 1)[0]
        return f"{prefix}/{dbname}?{query}"
    prefix = uri.rstrip("/").rsplit("/", 1)[0]
    return f"{prefix}/{dbname}"


def server_uri() -> str:
    """URI of the session's PostgreSQL server (maintenance database)."""
    global _SERVER_URI, _EMBEDDED
    if _SERVER_URI:
        return _SERVER_URI

    external = _external_uri()
    if external:
        _SERVER_URI = external
        return _SERVER_URI

    try:
        from embedded_postgres import PostgresServer
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PostgreSQL-only test suite: set TEST_PG_DSN to a PostgreSQL "
            "server, or `pip install embedded-postgres` so the suite can "
            "boot a throwaway server automatically."
        ) from exc

    from pathlib import Path

    data_dir = os.path.join(ROOT, "temp", "test_pg_data")
    shutil.rmtree(data_dir, ignore_errors=True)

    # PostgresServer runs initdb + starts the postmaster on a unix socket
    # under the data dir (no TCP port needed — psycopg2 connects via the
    # ?host= query parameter embedded in get_uri()).
    _EMBEDDED = PostgresServer(Path(data_dir), cleanup_mode="stop")
    _EMBEDDED.ensure_pgdata_inited()
    _EMBEDDED.ensure_postgres_running()
    _SERVER_URI = _EMBEDDED.get_uri()
    return _SERVER_URI


def stop() -> None:
    """Stop the embedded server (no-op when an external server is used)."""
    global _EMBEDDED, _SERVER_URI
    if _EMBEDDED is not None:
        try:
            _EMBEDDED.cleanup()
        except Exception:
            pass
        _EMBEDDED = None
        _SERVER_URI = None


def create_test_database(name: str) -> str:
    """Recreate ``name`` from scratch; return its dedicated URI."""
    import psycopg2

    uri = server_uri()
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    return _swap_dbname(uri, name)


def drop_test_database(name: str) -> None:
    try:
        import psycopg2

        conn = psycopg2.connect(server_uri())
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            conn.close()
    except Exception:
        pass


def bootstrap() -> str:
    """Ensure the server is up and NEON_DATABASE_URL points at the suite DB.

    Must run BEFORE config.settings / database.pg are first imported.
    """
    uri = server_uri()
    suite_dsn = create_test_database("xd_test_suite")
    os.environ["NEON_DATABASE_URL"] = suite_dsn
    # Make sure nothing can accidentally resolve a stale production DSN.
    os.environ.pop("POSTGRES_URL", None)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return uri
