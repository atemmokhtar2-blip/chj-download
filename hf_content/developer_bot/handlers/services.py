import os
import signal
import subprocess
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from handlers.auth import require_owner
from database.db import log_action
from utils.logger import action_logger

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOTS = {
    "main":    {"label": "البوت الأساسي",  "script": os.path.join(_root, "bot.py")},
    "support": {"label": "بوت الدعم",      "script": os.path.join(_root, "support_bot", "bot.py")},
    "admin":   {"label": "بوت الأدمن",     "script": os.path.join(_root, "admin_bot",   "bot.py")},
}


def services_kb():
    rows = []
    for key, info in BOTS.items():
        rows.append([InlineKeyboardButton(
            f"🔄 إعادة تشغيل {info['label']}",
            callback_data=f"dv_svc_restart_{key}",
        )])
    rows.append([InlineKeyboardButton("🔄 إعادة تشغيل جميع البوتات", callback_data="dv_svc_restart_all")])
    rows.append([InlineKeyboardButton("📊 حالة الخدمات", callback_data="dv_svc_status")])
    rows.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="dv_menu")])
    return InlineKeyboardMarkup(rows)


def confirm_restart_kb(target: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تأكيد", callback_data=f"dv_svc_confirm_{target}"),
            InlineKeyboardButton("❌ إلغاء", callback_data="dv_services"),
        ]
    ])


async def show_services_menu(query, context):
    await query.edit_message_text(
        "🔄 <b>إدارة التشغيل</b>\n\nاختر البوت الذي تريد إعادة تشغيله:",
        parse_mode="HTML",
        reply_markup=services_kb(),
    )


async def prompt_restart(query, context, target: str):
    label = BOTS[target]["label"] if target != "all" else "جميع البوتات"
    await query.edit_message_text(
        f"⚠️ <b>تأكيد إعادة التشغيل</b>\n\n"
        f"هل أنت متأكد من إعادة تشغيل <b>{label}</b>?\n"
        f"سيتم إيقاف الخدمة لثوانٍ قليلة.",
        parse_mode="HTML",
        reply_markup=confirm_restart_kb(target),
    )


def _find_pid(script_path: str) -> list[int]:
    try:
        result = subprocess.check_output(
            ["pgrep", "-f", script_path], text=True
        ).strip()
        return [int(p) for p in result.split() if p.isdigit()]
    except subprocess.CalledProcessError:
        return []


def _kill_pids(pids: list[int]):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            action_logger.info("Sent SIGTERM to PID %s", pid)
        except ProcessLookupError:
            pass


def _restart_bot(script_path: str):
    pids = _find_pid(script_path)
    _kill_pids(pids)
    subprocess.Popen(
        [sys.executable, script_path],
        cwd=_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    action_logger.info("Restarted %s (killed PIDs %s)", script_path, pids)


async def do_restart(query, context, target: str):
    uid = query.from_user.id
    if target == "all":
        for key, info in BOTS.items():
            _restart_bot(info["script"])
        label = "جميع البوتات"
    else:
        info = BOTS[target]
        _restart_bot(info["script"])
        label = info["label"]

    log_action(uid, "restart", f"target={target}", "ok")
    await query.edit_message_text(
        f"✅ <b>تم إعادة تشغيل {label}</b>\n\n"
        f"تمت إعادة التشغيل بنجاح. قد تستغرق الخدمة بضع ثوانٍ لتعود نشطة.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إدارة التشغيل", callback_data="dv_services")]
        ]),
    )


async def show_service_status(query, context):
    lines = []
    for key, info in BOTS.items():
        pids = _find_pid(info["script"])
        status = f"🟢 نشط (PID: {', '.join(map(str, pids))})" if pids else "🔴 متوقف"
        lines.append(f"• {info['label']}: {status}")

    text = "📊 <b>حالة الخدمات</b>\n\n" + "\n".join(lines)
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إدارة التشغيل", callback_data="dv_services")]
        ]),
    )
