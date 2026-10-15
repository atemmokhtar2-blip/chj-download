"""Pytest session configuration — PostgreSQL-only test suite.

Production stores EVERYTHING in PostgreSQL (NEON_DATABASE_URL); the SQLite
fallback has been removed entirely. To guarantee the tests exercise the real
backend, this conftest makes a PostgreSQL server available BEFORE any
project module is imported:

  * when ``TEST_PG_DSN`` is set (e.g. a CI service container) it is used;
  * otherwise a throwaway embedded PostgreSQL (``embedded-postgres``
    package) is booted under ``temp/`` and stopped at session end.

NEON_DATABASE_URL is then pointed at a dedicated suite database. Test
modules that need full isolation create their own dedicated database with
``pgtest.create_test_database(...)`` and pin ``database.pg.PG_DSN``
(restoring it afterwards), exactly like the old per-module SQLite files.
"""

import atexit
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pgtest  # noqa: E402

# Must happen before config.settings (and thus database.pg) is imported.
pgtest.bootstrap()

# Referral/clone defaults shared by the whole suite (production values).
os.environ.setdefault("REFERRAL_TARGET", "70")
os.environ.setdefault("REFERRAL_QUALIFIED_DAYS", "30")
os.environ.setdefault("CLONE_PRO_TARGET_MULTIPLIER", "2")
os.environ.setdefault("CLONE_PLAN_BASIC_DAYS", "30")
os.environ.setdefault("CLONE_PLAN_PRO_DAYS", "40")

atexit.register(pgtest.stop)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    pgtest.stop()
