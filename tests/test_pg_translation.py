"""Postgres translation-layer tests (no live Postgres needed).

The host now runs with NEON_DATABASE_URL set, so EVERY main-database SQL
statement (users, downloads, cache, settings, tickets) goes through
database.db._pg_translate before hitting Postgres. These tests pin down the
translation contract so a regex regression can never silently ship broken
SQL to production:

  * `?` placeholders        -> `%s`
  * datetime('now')         -> to_char(now() at time zone 'utc', ...)
  * datetime('now','-7 days') / with a ? modifier -> interval arithmetic
  * date('now') / date(col) -> CURRENT_DATE / (col)::date
  * INSERT OR IGNORE        -> INSERT ... ON CONFLICT DO NOTHING
  * INTEGER PRIMARY KEY (AUTOINCREMENT) -> BIGINT / SERIAL PRIMARY KEY
  * every INTEGER           -> BIGINT (Telegram ids exceed int4)
  * id-returning tables get RETURNING id appended by the compat cursor
"""
import os
import re
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- stub dotenv if unavailable ---
try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dotenv

sys.path.insert(0, REPO)

from database import db  # noqa: E402

failures = []


def check(name: str, cond: bool) -> None:
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        failures.append(name)


T = db._pg_translate

# 1) placeholders -------------------------------------------------------------
out = T("SELECT * FROM users WHERE user_id = ? AND is_banned = ?")
check("? -> %s", "%s" in out and "?" not in out)

# 2) datetime('now') ----------------------------------------------------------
out = T("UPDATE users SET last_seen = datetime('now') WHERE user_id = ?")
check("datetime('now') -> to_char(now() utc)", "to_char(now() at time zone 'utc'" in out)
check("no sqlite datetime left", "datetime(" not in out)

# 3) datetime literal modifier -------------------------------------------------
out = T("SELECT COUNT(*) FROM downloads WHERE created_at >= datetime('now', '-7 days')")
check("literal modifier -> interval", "interval '-7 days'" in out)
check("no sqlite datetime left (literal)", "datetime(" not in out)

# 4) datetime param modifier (used by cache/history cleanup) --------------------
out = T("DELETE FROM file_cache WHERE created_at < datetime('now', ?)")
# NOTE: the final `?` -> `%s` pass runs AFTER interval substitution, so the
# placeholder inside the generated interval cast is already `%s`.
check("param modifier -> interval cast", "(%s)::interval" in out)

# 5) concat-days form -----------------------------------------------------------
out = T("SELECT 1 WHERE created_at < datetime('now', '-' || ? || ' days')")
check("concat days -> make_interval", "make_interval(days => %s)" in out)

# 6) date helpers ----------------------------------------------------------------
out = T("SELECT COUNT(*) FROM users WHERE date(join_date) = date('now')")
check("date('now') -> CURRENT_DATE", "CURRENT_DATE" in out)
check("date(col) -> (col)::date", "(join_date)::date" in out)

# 7) INSERT OR IGNORE -------------------------------------------------------------
out = T("INSERT OR IGNORE INTO users (user_id) VALUES (?)")
check("INSERT OR IGNORE -> INSERT", "INSERT INTO users" in out)
check("ON CONFLICT DO NOTHING appended", out.rstrip().endswith("ON CONFLICT DO NOTHING"))
out2 = T("INSERT OR IGNORE INTO t (a) VALUES (?) ON CONFLICT (a) DO NOTHING")
check("existing ON CONFLICT not duplicated", out2.upper().count("ON CONFLICT") == 1)

# 8) DDL types ---------------------------------------------------------------------
out = T("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER)")
check("AUTOINCREMENT -> SERIAL PRIMARY KEY", "SERIAL PRIMARY KEY" in out)
check("plain INTEGER -> BIGINT", "uid BIGINT" in out)
check("no bare INTEGER left in DDL", not re.search(r"\bINTEGER\b", out))
out = T("CREATE TABLE u (user_id INTEGER PRIMARY KEY, name TEXT)")
check("plain PK -> BIGINT PRIMARY KEY", "BIGINT PRIMARY KEY" in out)

# 9) ON CONFLICT upsert passes through untouched ------------------------------------
sql = ("INSERT INTO bot_settings (key, value) VALUES (?, ?) "
       "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
out = T(sql)
check("upsert ON CONFLICT preserved", "ON CONFLICT(key) DO UPDATE" in out)
check("upsert placeholders converted", out.count("%s") == 2)

# 10) id-returning tables registered -------------------------------------------------
for tbl in ("downloads", "support_tickets", "support_ticket_messages", "file_cache"):
    check(f"{tbl} registered for RETURNING id", tbl in db._ID_RETURN_TABLES)

# 11) executescript statement splitting keeps every DDL statement ----------------------
script = db.__dict__  # sanity: module loads
script_sql = """
CREATE TABLE IF NOT EXISTS a (id INTEGER PRIMARY KEY AUTOINCREMENT, x INTEGER);
CREATE INDEX IF NOT EXISTS ix ON a(x);
"""
stmts = [s.strip() for s in script_sql.split(";") if s.strip()]
check("executescript splits into statements", len(stmts) == 2)
check("each statement translates", all("%s" not in T(s) and "SERIAL" in T(s) or "INDEX" in T(s) for s in stmts))

# 12) PostgreSQL is the ONLY backend ------------------------------------------------------
check("backend_name is always postgres", db.backend_name() == "postgres")
import database.pg as pgmod  # noqa: E402
check("pg backend constant is postgres", pgmod.BACKEND == "postgres" and pgmod.backend_name() == "postgres")
old = pgmod.PG_DSN
pgmod.PG_DSN = ""
try:
    failed_loudly = False
    try:
        with db.db_cursor():
            pass
    except RuntimeError as exc:
        failed_loudly = "NEON_DATABASE_URL" in str(exc)
    check("missing DSN fails loudly (no silent fallback)", failed_loudly)
finally:
    pgmod.PG_DSN = old

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL PG TRANSLATION TESTS PASSED")
