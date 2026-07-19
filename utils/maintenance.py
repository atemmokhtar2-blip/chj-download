from database.db import db_cursor

_maintenance_mode: bool = False


def _load_from_db():
    global _maintenance_mode
    try:
        with db_cursor() as c:
            c.execute("SELECT value FROM bot_settings WHERE key = 'maintenance'")
            row = c.fetchone()
            _maintenance_mode = (row["value"] == "1") if row else False
    except Exception:
        _maintenance_mode = False


def is_maintenance() -> bool:
    return _maintenance_mode


def set_maintenance(on: bool):
    global _maintenance_mode
    _maintenance_mode = on
    try:
        with db_cursor() as c:
            c.execute("""
                INSERT INTO bot_settings (key, value)
                VALUES ('maintenance', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, ("1" if on else "0",))
    except Exception:
        pass


_load_from_db()
