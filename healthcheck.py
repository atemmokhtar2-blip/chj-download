from __future__ import annotations

import importlib
import sys
from pathlib import Path

CHECK_MODULES = [
    "bot",
    "handlers.start",
    "handlers.download",
    "handlers.admin",
    "services.downloader",
    "services.content_intelligence",
    "services.media_vault",
    "middlewares.auth",
    "middlewares.rate_limiter",
    "middlewares.concurrency",
    "database.users",
    "database.downloads",
    "database.cache",
]


def main() -> int:
    print("🔎 X Downloader healthcheck")
    print(f"Python: {sys.version.split()[0]}")

    missing = []
    for rel in ["app.py", "bot.py", "requirements.txt", "config/settings.py"]:
        if not Path(rel).exists():
            missing.append(rel)
    if missing:
        print("❌ Missing required files:", ", ".join(missing))
        return 1

    for module in CHECK_MODULES:
        try:
            importlib.import_module(module)
            print(f"✅ import {module}")
        except Exception as exc:
            print(f"❌ import {module}: {type(exc).__name__}: {exc}")
            return 1

    try:
        from bot import build_application
        app = build_application()
        print(f"✅ telegram application built: {type(app).__name__}")
    except Exception as exc:
        print(f"❌ build_application failed: {type(exc).__name__}: {exc}")
        return 1

    print("✅ Healthcheck passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
