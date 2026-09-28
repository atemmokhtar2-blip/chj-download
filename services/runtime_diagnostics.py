from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from config.settings import BOT_TOKEN, DATABASE_PATH, TEMP_DIR, REDIS_URL, STORAGE_CHANNEL_ID


def _ok(label: str, value: str = "OK") -> dict[str, str]:
    return {"status": "ok", "label": label, "value": value}


def _warn(label: str, value: str) -> dict[str, str]:
    return {"status": "warn", "label": label, "value": value}


def _fail(label: str, value: str) -> dict[str, str]:
    return {"status": "fail", "label": label, "value": value}


def collect_runtime_diagnostics() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    checks.append(_ok("Python", sys.version.split()[0]))
    checks.append(_ok("Telegram token", "loaded") if BOT_TOKEN else _fail("Telegram token", "missing TELEGRAM_BOT_TOKEN/token.txt"))

    db_path = Path(DATABASE_PATH)
    if db_path.exists():
        try:
            with sqlite3.connect(DATABASE_PATH) as conn:
                conn.execute("SELECT 1")
            checks.append(_ok("SQLite", str(db_path)))
        except Exception as exc:
            checks.append(_fail("SQLite", str(exc)))
    else:
        checks.append(_fail("SQLite", f"missing {db_path}"))

    temp = Path(TEMP_DIR)
    try:
        temp.mkdir(parents=True, exist_ok=True)
        probe = temp / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append(_ok("Temp directory", str(temp)))
    except Exception as exc:
        checks.append(_fail("Temp directory", str(exc)))

    try:
        import yt_dlp
        checks.append(_ok("yt-dlp", getattr(yt_dlp.version, "__version__", "installed")))
    except Exception as exc:
        checks.append(_fail("yt-dlp", str(exc)))

    try:
        from services.content_intelligence import build_content_intelligence
        sample = build_content_intelligence({"media_type": "video", "qualities": [{"label": "720p"}], "platform": "Healthcheck"}, {"duration": 42})
        checks.append(_ok("Content Intelligence", f"score {sample.get('score')}/99"))
    except Exception as exc:
        checks.append(_fail("Content Intelligence", str(exc)))

    checks.append(_ok("Redis", "configured") if REDIS_URL else _warn("Redis", "not configured; memory/SQLite fallback active"))
    checks.append(_ok("Media Vault", "configured") if STORAGE_CHANNEL_ID else _warn("Media Vault", "not configured; Telegram file_id cache only"))
    return checks


def render_diagnostics_html() -> str:
    icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    lines = ["🩺 <b>X Downloader Health</b>", ""]
    for check in collect_runtime_diagnostics():
        lines.append(f"{icon.get(check['status'], '•')} <b>{check['label']}:</b> {check['value']}")
    failed = sum(1 for c in collect_runtime_diagnostics() if c["status"] == "fail")
    warnings = sum(1 for c in collect_runtime_diagnostics() if c["status"] == "warn")
    lines.append("")
    if failed:
        lines.append(f"🚨 <b>{failed} critical issue(s)</b> need fixing before heavy traffic.")
    elif warnings:
        lines.append(f"🟡 Running with <b>{warnings} warning(s)</b>; core bot should still respond.")
    else:
        lines.append("🟢 All core systems are healthy.")
    return "\n".join(lines)
