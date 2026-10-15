"""Pytest session configuration.

This file is imported BEFORE any test module, which is the only reliable
moment to pin the database location: ``config.settings`` resolves
``DATABASE_PATH`` from the environment exactly once (at first import) and
every database module caches that value. Without this, whichever test module
happens to import settings first decides where ALL tests write — which in a
full-suite run leaked test rows into the real ``database/bot.db``.

Every test therefore runs against one disposable SQLite file
(``temp/test_suite.db``), recreated fresh each session, and the Postgres
env vars are stripped so the SQLite fallback path is always exercised.
Test modules that need their own isolated DB (e.g. test_clone_plans) pin
the module-level constants themselves and restore them afterwards.
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
SUITE_DB = os.path.join(ROOT, "temp", "test_suite.db")

# Must happen before config.settings (and thus any database module) is imported.
os.environ["DATABASE_PATH"] = SUITE_DB
for _var in ("NEON_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
    os.environ.pop(_var, None)

os.makedirs(os.path.dirname(SUITE_DB), exist_ok=True)
try:
    if os.path.exists(SUITE_DB):
        os.remove(SUITE_DB)
except OSError:
    pass

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
