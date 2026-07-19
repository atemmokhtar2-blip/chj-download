"""
🩺 فحص المشروع — Project Health Check
"""
import os
import sqlite3
import sys
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import log_action
from utils.logger import error_logger

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CRITICAL_FILES = [
    "bot.py",
    "config/settings.py",
    "database/db.py",
    "admin_bot/bot.py",
    "support_bot/bot.py",
    "developer_bot/bot.py",
    ".env",
    "requirements.txt",
]

REQUIRED_DIRS = [
    "database", "handlers", "locales", "logs",
    "admin_bot", "support_bot", "developer_bot",
]


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 ** 3:
        return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def _dir_size(path: str) -> int:
    total = 0
    if os.path.isdir(path):
        for dp, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
    return total


def _check_syntax(filepath: str) -> tuple[bool, str]:
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        compile(source, filepath, "exec")
        return True, ""
    except SyntaxError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def _check_db(db_path: str) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("SELECT 1")
        conn.close()
        return True, ""
    except Exception as e:
        return False, str(e)


def run_health_check() -> dict:
    report = {
        "missing_files": [],
        "missing_dirs": [],
        "syntax_errors": [],
        "db_status": {},
        "storage": {},
        "python_files": 0,
        "score": 100,
    }

    # Check critical files
    for f in CRITICAL_FILES:
        full = os.path.join(_root, f)
        if not os.path.exists(full):
            report["missing_files"].append(f)
            report["score"] -= 10

    # Check dirs
    for d in REQUIRED_DIRS:
        if not os.path.isdir(os.path.join(_root, d)):
            report["missing_dirs"].append(d)
            report["score"] -= 5

    # Syntax check all Python files
    py_count = 0
    for dp, dirnames, files in os.walk(_root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git", ".venv", "temp"}]
        for fname in files:
            if fname.endswith(".py"):
                py_count += 1
                full = os.path.join(dp, fname)
                ok, err = _check_syntax(full)
                if not ok:
                    rel = os.path.relpath(full, _root)
                    report["syntax_errors"].append(f"{rel}: {err}")
                    report["score"] -= 15
    report["python_files"] = py_count

    # DB checks
    dbs = {
        "bot.db": os.path.join(_root, "database", "bot.db"),
        "developer.db": os.path.join(_root, "database", "developer.db"),
    }
    for name, path in dbs.items():
        if os.path.exists(path):
            ok, err = _check_db(path)
            size = os.path.getsize(path)
            report["db_status"][name] = {"ok": ok, "err": err, "size": size}
        else:
            report["db_status"][name] = {"ok": False, "err": "غير موجود", "size": 0}

    # Storage
    report["storage"]["project"] = _dir_size(_root)
    report["storage"]["logs"] = _dir_size(os.path.join(_root, "logs"))
    report["storage"]["temp"] = _dir_size(os.path.join(_root, "temp"))
    report["storage"]["backups"] = _dir_size(os.path.join(_root, "backups"))

    report["score"] = max(0, min(100, report["score"]))
    return report


async def show_health_check(query, context):
    uid = query.from_user.id
    await query.edit_message_text("🩺 <b>جارٍ فحص المشروع…</b>", parse_mode="HTML")

    try:
        r = run_health_check()
        log_action(uid, "health_check", "", f"score={r['score']}")

        score = r["score"]
        if score >= 90:
            score_icon = "🟢"
        elif score >= 70:
            score_icon = "🟡"
        elif score >= 50:
            score_icon = "🟠"
        else:
            score_icon = "🔴"

        lines = [f"🩺 <b>تقرير فحص المشروع</b>\n\n{score_icon} <b>النتيجة: {score}/100</b>\n"]

        # Files
        if r["missing_files"]:
            lines.append("❌ <b>ملفات مفقودة:</b>")
            for f in r["missing_files"]:
                lines.append(f"  • <code>{f}</code>")
        else:
            lines.append("✅ جميع الملفات الحرجة موجودة")

        # Dirs
        if r["missing_dirs"]:
            lines.append("\n❌ <b>مجلدات مفقودة:</b>")
            for d in r["missing_dirs"]:
                lines.append(f"  • <code>{d}/</code>")
        else:
            lines.append("✅ جميع المجلدات المطلوبة موجودة")

        # Syntax
        if r["syntax_errors"]:
            lines.append(f"\n⚠️ <b>أخطاء في الكود ({len(r['syntax_errors'])}):</b>")
            for e in r["syntax_errors"][:5]:
                lines.append(f"  • <code>{e[:80]}</code>")
        else:
            lines.append(f"✅ لا أخطاء في الكود ({r['python_files']} ملف Python)")

        # DB
        lines.append("\n💾 <b>قواعد البيانات:</b>")
        for name, info in r["db_status"].items():
            st = "✅" if info["ok"] else "❌"
            lines.append(f"  {st} {name}: {_fmt_size(info['size'])}")

        # Storage
        lines.append("\n📦 <b>التخزين:</b>")
        lines.append(f"  • المشروع: {_fmt_size(r['storage']['project'])}")
        lines.append(f"  • السجلات: {_fmt_size(r['storage']['logs'])}")
        lines.append(f"  • المؤقت: {_fmt_size(r['storage']['temp'])}")
        lines.append(f"  • النسخ الاحتياطية: {_fmt_size(r['storage']['backups'])}")

        text = "\n".join(lines)
        await query.edit_message_text(
            text[:4000],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 فحص مجدد", callback_data="dv_health")],
                [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")],
            ]),
        )
    except Exception as e:
        error_logger.error("Health check error: %s", e, exc_info=True)
        await query.edit_message_text(
            f"❌ فشل الفحص: <code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="dv_menu")]
            ]),
        )
