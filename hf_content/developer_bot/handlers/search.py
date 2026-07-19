"""
🔍 البحث داخل المشروع — Project Search Engine
"""
import os
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import log_action
from utils.logger import error_logger

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_SEARCH = "dv_search_input"

SEARCH_TYPES = {
    "all":       "🔍 بحث شامل (كل الملفات)",
    "py":        "🐍 ملفات Python فقط",
    "handlers":  "⚡ البحث في الـ Handlers",
    "commands":  "📌 البحث عن أوامر /cmd",
    "text":      "📝 ملفات نصية",
}


def search_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 بحث شامل",           callback_data="dv_srch_type_all")],
        [InlineKeyboardButton("🐍 Python فقط",          callback_data="dv_srch_type_py")],
        [InlineKeyboardButton("⚡ في Handlers",          callback_data="dv_srch_type_handlers")],
        [InlineKeyboardButton("📌 أوامر /cmd",           callback_data="dv_srch_type_commands")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",    callback_data="dv_menu")],
    ])


async def show_search_menu(query, context):
    await query.edit_message_text(
        "🔍 <b>البحث داخل المشروع</b>\n\nاختر نوع البحث:",
        parse_mode="HTML",
        reply_markup=search_menu_kb(),
    )


async def prompt_search(query, context, search_type: str = "all"):
    context.user_data["dv_state"] = STATE_SEARCH
    context.user_data["dv_search_type"] = search_type
    type_label = SEARCH_TYPES.get(search_type, "بحث شامل")
    await query.edit_message_text(
        f"🔍 <b>{type_label}</b>\n\n"
        "أرسل الكلمة أو النص الذي تريد البحث عنه:\n\n"
        "<i>يدعم التعابير النمطية (regex)</i>\n"
        "<i>أرسل /cancel للإلغاء</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="dv_search")]
        ]),
    )


async def prompt_search_text(update, context):
    """Show a message asking user to type search query."""
    context.user_data["dv_state"] = STATE_SEARCH
    context.user_data["dv_search_type"] = "all"
    await update.message.reply_text(
        "🔍 <b>البحث داخل المشروع</b>\n\nأرسل الكلمة أو النص الذي تريد البحث عنه:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="dv_menu")]
        ]),
    )


def _search_files(query_str: str, search_type: str) -> list[dict]:
    results = []
    try:
        pattern = re.compile(query_str, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query_str), re.IGNORECASE)

    ext_filter = None
    dir_filter = None
    if search_type == "py":
        ext_filter = {".py"}
    elif search_type == "handlers":
        dir_filter = "handlers"
    elif search_type == "commands":
        pattern = re.compile(r"CommandHandler\s*\(\s*['\"]" + re.escape(query_str.lstrip("/")),
                             re.IGNORECASE)
    elif search_type == "text":
        ext_filter = {".txt", ".md", ".log", ".yaml", ".yml", ".json"}

    skip_dirs = {"__pycache__", ".git", ".venv", "temp", "backups"}

    for dirpath, dirnames, files in os.walk(_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        if dir_filter and dir_filter not in dirpath:
            continue
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext_filter and ext not in ext_filter:
                continue
            if ext not in {".py", ".txt", ".md", ".json", ".yaml", ".yml",
                           ".log", ".sh", ".js", ".ts", ".html", ".css", ".env", ".cfg"}:
                continue
            full = os.path.join(dirpath, fname)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                for i, line in enumerate(lines, 1):
                    if pattern.search(line):
                        rel = os.path.relpath(full, _root)
                        results.append({
                            "file": rel,
                            "line": i,
                            "text": line.strip()[:100],
                        })
                        if len(results) >= 50:
                            return results
            except (OSError, PermissionError):
                continue
    return results


async def handle_search_query(update, context):
    uid = update.effective_user.id
    query_str = (update.message.text or "").strip()
    search_type = context.user_data.pop("dv_search_type", "all")
    context.user_data.pop("dv_state", None)

    if not query_str:
        await update.message.reply_text("❌ أرسل نصًا للبحث.")
        return

    await update.message.reply_text(f"🔍 <b>جارٍ البحث عن:</b> <code>{query_str}</code>…", parse_mode="HTML")

    try:
        results = _search_files(query_str, search_type)
        log_action(uid, "search", f"{search_type}:{query_str[:50]}", f"found={len(results)}")

        if not results:
            await update.message.reply_text(
                f"🔍 لا نتائج لـ <code>{query_str}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 بحث جديد", callback_data="dv_search")],
                    [InlineKeyboardButton("🔙 رجوع",     callback_data="dv_menu")],
                ]),
            )
            return

        # Group by file
        by_file: dict[str, list] = {}
        for r in results:
            by_file.setdefault(r["file"], []).append(r)

        lines = [f"🔍 <b>نتائج البحث:</b> <code>{query_str}</code>\n"
                 f"📊 {len(results)} نتيجة في {len(by_file)} ملف\n"]

        for fname, matches in list(by_file.items())[:10]:
            lines.append(f"\n📄 <code>{fname}</code>")
            for m in matches[:3]:
                lines.append(f"  <b>سطر {m['line']}:</b> <code>{m['text']}</code>")
            if len(matches) > 3:
                lines.append(f"  <i>… و {len(matches)-3} نتيجة أخرى</i>")

        text = "\n".join(lines)
        await update.message.reply_text(
            text[:4000],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 بحث جديد", callback_data="dv_search")],
                [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")],
            ]),
        )
    except Exception as e:
        error_logger.error("Search error: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ خطأ في البحث: <code>{e}</code>", parse_mode="HTML")
