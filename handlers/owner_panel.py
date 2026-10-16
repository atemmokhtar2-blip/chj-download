"""👑 Owner Control Center — لوحة إدارة النظام للمالك الأساسي فقط.

Design contract (implemented exactly as specified):
  * OWNER-ONLY **IN CODE** — every command, every callback and every text
    state re-verifies ``is_owner(user.id)`` server-side. Hiding buttons is
    cosmetic; the gate here is what actually protects the panel. Any denied
    attempt is written to the security audit trail.
  * The panel only runs in PRIVATE chat of the MAIN bot (never inside a
    clone instance).
  * Every sensitive action follows the confirmed path:
      ban          → reason (text) → confirmation → execute + audit
      broadcast    → text → preview + recipients count → confirm → report
      stop clone   → shows instance name + owner → confirm → execute + audit
      edit admins  → confirm the exact change → execute + audit
      change rules → shows current vs new value → confirm → save + audit
      cleanup      → shows targeted size → confirm → execute + result + audit

Callback namespace: ``own_*`` (registered before the generic admin handler).
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import time
from datetime import datetime
from html import escape

from telegram import Update, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes

from middlewares.auth import is_owner, is_admin
from utils.button_text import inline_button
from utils.telegram_safe import safe_edit
from config.settings import OWNER_ID, ADMIN_IDS, TEMP_DIR
from database.admin_audit import log_admin_action, get_admin_audit, get_admin_audit_stats
from database.users import (
    get_user, get_total_users, get_new_users_today, get_new_users_week,
    get_new_users_month, get_active_today, get_active_24h,
    get_banned_users_count, get_users_page, search_users, get_all_user_ids,
    ban_user, unban_user, get_top_users, count_users, get_banned_users,
    export_users_csv_rows,
)
from database.downloads import (
    get_downloads_today, get_downloads_week, get_downloads_month,
    get_downloads_by_platform, get_total_downloads, get_downloads_24h,
    get_total_data_bytes, get_user_download_stats,
)
from database.broadcast import log_broadcast, get_broadcast_history
from database.bot_settings import (
    get_setting, set_setting, list_extra_admins, add_extra_admin,
    remove_extra_admin,
)
from database import support
from services import referral
from services.instance_manager import is_clone_bot, spawn_instance, stop_instance

logger = logging.getLogger(__name__)

_STARTED_AT = time.time()

# Owner text-input states. Keyed by owner id; values are short state names
# with an optional payload after ":" (e.g. "ban_reason:123456").
OWNER_STATES: dict[int, str] = {}
# Pending confirmation payloads (ban reason, broadcast draft, target...).
OWNER_PENDING: dict[int, dict] = {}

_SUP_STATUS = {"open": "🔓 مفتوحة", "answered": "🟢 تم الرد", "closed": "⚫ مغلقة"}
_INST_STATUS = {
    "pending_token": "🟡 بانتظار التوكن",
    "active": "🟢 نشطة",
    "disabled": "⚫ موقوفة بقرار المالك",
    "expired": "⌛ منتهية المدة",
    "error": "🔴 خطأ",
}


def _uptime() -> str:
    secs = int(time.time() - _STARTED_AT)
    h, m = secs // 3600, (secs % 3600) // 60
    if h >= 24:
        return f"{h // 24} يوم {h % 24} ساعة"
    return f"{h} ساعة {m} دقيقة" if h else f"{m} دقيقة"


def _fmt_bytes(size: int) -> str:
    size = int(size or 0)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _format_timestamp(value, length: int = 16) -> str:
    """Format SQLite text or PostgreSQL datetime values consistently."""
    return str(value or "—")[:length]


def _temp_dir_stats() -> tuple[int, int]:
    """(bytes, file_count) currently sitting in TEMP_DIR."""
    total, count = 0, 0
    try:
        for dirpath, _d, files in os.walk(TEMP_DIR):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                    count += 1
                except OSError:
                    continue
    except Exception:
        pass
    return total, count


async def _edit(query, text, **kwargs):
    return await safe_edit(query.edit_message_text, text=text, **kwargs)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _back(cb: str = "own_home", label: str = "🔙 مركز التحكم") -> list:
    return [[inline_button(label, callback_data=cb)]]


def _suffix_int(data: str) -> int | None:
    try:
        return int(data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        return None


# ---- the gate ------------------------------------------------------------------

def _owner_gate(update: Update) -> bool:
    """THE protection layer: verified in code on EVERY interaction.

    Denied attempts are recorded in the security audit trail so any probing
    is visible to the owner.
    """
    user = update.effective_user
    if user and is_owner(user.id):
        # Private chat of the MAIN bot only — never inside a clone.
        bot_username = getattr(update.effective_chat, "username", None)
        chat_type = (update.effective_chat.type if update.effective_chat else "")
        if chat_type != "private":
            return False
        return True
    try:
        log_admin_action(
            int(user.id) if user else 0,
            "owner_panel_denied",
            detail="محاولة وصول غير مصرح بها للوحة المالك",
        )
    except Exception:
        pass
    return False


def _clone_guard(update: Update) -> bool:
    """Owner panel must not run inside clone instances."""
    try:
        bot = update.effective_message.bot if update.effective_message else None
        if bot and is_clone_bot(getattr(bot, "username", None)):
            return False
    except Exception:
        pass
    return True


# ---- command ---------------------------------------------------------------------

async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner_gate(update) or not _clone_guard(update):
        await update.effective_message.reply_text(
            "⛔ هذه اللوحة محمية ومخصصة للمالك الأساسي فقط."
        )
        return
    OWNER_STATES.pop(update.effective_user.id, None)
    text, keyboard = _build_home()
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


# ---- home dashboard ----------------------------------------------------------------

def _build_home() -> tuple[str, InlineKeyboardMarkup]:
    """Button-only owner home. Telegram requires a message to hold inline buttons,
    so use an invisible separator instead of a wall of dashboard text."""
    text = "\u2063"
    keyboard = _kb([
        [inline_button("👥 المستخدمون", callback_data="own_users"),
         inline_button("🎁 الإحالات", callback_data="own_refs")],
        [inline_button("🤖 إدارة النسخ", callback_data="own_inst"),
         inline_button("📥 التحميلات", callback_data="own_dl")],
        [inline_button("📢 الرسائل والإذاعة", callback_data="own_bc"),
         inline_button("🛟 الدعم الفني", callback_data="own_sup")],
        [inline_button("📈 التقارير", callback_data="own_rep"),
         inline_button("⚙️ الإعدادات", callback_data="own_set")],
        [inline_button("🎟️ أكواد التفعيل", callback_data="own_codes")],
        [inline_button("🔄 تحديث الأرقام", callback_data="own_home")],
    ])
    return text, keyboard

# ---- users section --------------------------------------------------------------------

def _users_home_text() -> str:
    return (
        "👥 <b>إدارة المستخدمين</b>\n"
        "البحث والتحكم في كل حساب\n\n"
        f"• الإجمالي: <b>{get_total_users()}</b>\n"
        f"• نشط اليوم: <b>{get_active_today()}</b> • آخر 24 ساعة: <b>{get_active_24h()}</b>\n"
        f"• جديد اليوم: <b>{get_new_users_today()}</b> • الأسبوع: <b>{get_new_users_week()}</b> • الشهر: <b>{get_new_users_month()}</b>\n"
        f"• محظور: <b>{get_banned_users_count()}</b>"
    )


def _users_home_kb() -> InlineKeyboardMarkup:
    return _kb([
        [inline_button("📃 كل المستخدمين", callback_data="own_users_page_0"),
         inline_button("🔍 بحث عن مستخدم", callback_data="own_users_search")],
        [inline_button("✅ النشطون", callback_data="own_users_active"),
         inline_button("🆕 المستخدمون الجدد", callback_data="own_users_new")],
        [inline_button("🚫 المحظورون", callback_data="own_users_banned"),
         inline_button("🏆 أعلى المستخدمين", callback_data="own_users_top")],
        [inline_button("📤 تصدير القائمة (CSV)", callback_data="own_users_export")],
        *_back(),
    ])


async def _show_users_page(query, page: int = 0, status: str = "all"):
    per_page = 8
    if status not in ("all", "active", "banned"):
        status = "all"
    total = count_users(status)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page or 0, total_pages - 1))
    users = get_users_page(offset=page * per_page, limit=per_page, status=status)
    label = {"all": "الكل", "active": "النشطون", "banned": "المحظورون"}[status]

    if not users:
        await _edit(query,
            f"👥 <b>المستخدمون — {label}</b>\n\nلا يوجد مستخدمون في هذه الفئة.",
            parse_mode="HTML", reply_markup=_users_home_kb())
        return

    lines = []
    for u in users:
        icon = "🚫" if u.get("is_banned") else "✅"
        name = escape(u.get("first_name") or "بدون اسم")
        uname = escape(u.get("username") or "—")
        lines.append(
            f"{icon} <b>{name}</b> @{uname}\n"
            f"    🆔 <code>{u['user_id']}</code> • 📥 <b>{u.get('downloads', 0)}</b> • 🕑 {(u.get('last_seen') or '—')[:10]}"
        )
    text = (
        f"👥 <b>قائمة المستخدمين — {label}</b>\n"
        f"الصفحة <b>{page + 1}</b>/<b>{total_pages}</b> • العدد: <b>{total}</b>\n\n"
        + "\n".join(lines)
    )
    rows = []
    row = []
    for u in users:
        row.append(inline_button(f"👤 {u['user_id']}", callback_data=f"own_user_{u['user_id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(inline_button("⬅️ السابق", callback_data=f"own_users_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(inline_button("التالي ➡️", callback_data=f"own_users_page_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([inline_button("🔙 إدارة المستخدمين", callback_data="own_users")])
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _show_banned(query):
    banned = get_banned_users(30)
    if not banned:
        await _edit(query, "🚫 <b>المحظورون</b>\n\nلا يوجد مستخدمون محظورون حاليًا.",
                    parse_mode="HTML", reply_markup=_users_home_kb())
        return
    lines = []
    for b in banned:
        name = escape(b.get("first_name") or "بدون اسم")
        reason = escape(b.get("ban_reason") or "بدون سبب مسجل")
        date = (b.get("banned_at") or "—")[:16]
        lines.append(f"🚫 <b>{name}</b> • <code>{b['user_id']}</code>\n    السبب: {reason} • 🕑 {date}")
    rows = [[inline_button(f"👤 {b['user_id']}", callback_data=f"own_user_{b['user_id']}")] for b in banned[:12]]
    rows.append([inline_button("🔙 إدارة المستخدمين", callback_data="own_users")])
    await _edit(query,
        f"🚫 <b>المحظورون ({get_banned_users_count()})</b>\n\n" + "\n".join(lines),
        parse_mode="HTML", reply_markup=_kb(rows))


async def _show_new_users(query):
    users = get_users_page(offset=0, limit=10, status="all")  # ordered by join_date DESC
    lines = [
        f"🆕 <b>{escape(u.get('first_name') or 'بدون اسم')}</b> • <code>{u['user_id']}</code>"
        f" • 🕑 {(u.get('join_date') or '—')[:16]}"
        for u in users
    ] or ["• لا يوجد مستخدمون بعد."]
    text = (
        "🆕 <b>المستخدمون الجدد</b>\n\n"
        f"• اليوم: <b>{get_new_users_today()}</b>\n"
        f"• آخر ٧ أيام: <b>{get_new_users_week()}</b>\n"
        f"• آخر ٣٠ يومًا: <b>{get_new_users_month()}</b>\n\n"
        "<b>أحدث التسجيلات</b>\n" + "\n".join(lines)
    )
    rows = [[inline_button(f"👤 {u['user_id']}", callback_data=f"own_user_{u['user_id']}")] for u in users[:8]]
    rows.append([inline_button("🔙 إدارة المستخدمين", callback_data="own_users")])
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _show_top_users(query):
    top_dl = get_top_users(8)
    dl_lines = [
        f"{i}. <b>{escape(str(u.get('first_name') or u.get('user_id')))}</b>"
        f" • <code>{u['user_id']}</code> — <b>{u.get('downloads', 0)}</b> تحميل"
        for i, u in enumerate(top_dl, 1)
    ] or ["• لا يوجد نشاط بعد."]
    try:
        top_ref = referral.get_top_referrers(8)
    except Exception:
        top_ref = []
    ref_lines = [
        f"{i}. <code>{r['referrer_id']}</code> — <b>{int(r.get('qualified') or 0)}</b> مؤهل"
        f" من {int(r.get('total') or 0)} إحالة"
        for i, r in enumerate(top_ref, 1)
    ] or ["• لا توجد إحالات بعد."]
    text = (
        "🏆 <b>أعلى المستخدمين</b>\n\n"
        "📥 <b>الأكثر تحميلًا</b>\n" + "\n".join(dl_lines)
        + "\n\n🎁 <b>الأكثر إحالات (مؤهلون)</b>\n" + "\n".join(ref_lines)
    )
    rows = [[inline_button(f"👤 {u['user_id']}", callback_data=f"own_user_{u['user_id']}")] for u in top_dl[:6]]
    rows.append([inline_button("🔙 إدارة المستخدمين", callback_data="own_users")])
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _export_csv(context, query):
    rows = export_users_csv_rows()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "first_name", "last_name", "join_date",
                "last_seen", "is_banned", "ban_reason", "downloads"])
    for r in rows:
        w.writerow([r.get(k, "") for k in ("user_id", "username", "first_name", "last_name",
                                           "join_date", "last_seen", "is_banned", "ban_reason", "downloads")])
    data = buf.getvalue().encode("utf-8-sig")
    await context.bot.send_document(
        chat_id=query.from_user.id,
        document=InputFile(io.BytesIO(data), filename=f"users_{datetime.now():%Y%m%d_%H%M}.csv"),
        caption=f"📤 تصدير {len(rows)} مستخدم — الحقول المسموح بها للمالك فقط.",
    )
    log_admin_action(query.from_user.id, "owner_export_users", detail=f"rows={len(rows)}")
    await query.answer("✅ تم تجهيز الملف وإرساله.", show_alert=False)


# ---- user profile ----------------------------------------------------------------------

def _user_profile(target_id: int, tab: str = "main") -> tuple[str, InlineKeyboardMarkup] | None:
    user = get_user(target_id)
    if not user:
        return None
    full_name = " ".join(str(p) for p in [user.get("first_name"), user.get("last_name")] if p) or "—"
    stats = get_user_download_stats(target_id)
    is_banned = bool(user.get("is_banned"))

    header = (
        f"👤 <b>الملف الكامل</b>\n\n"
        f"🆔 <code>{target_id}</code>\n"
        f"👤 الاسم: <b>{escape(full_name)}</b>\n"
        f"🔖 اليوزر: @{escape(user.get('username') or '—')}\n"
        f"📅 التسجيل: <code>{escape((user.get('join_date') or '—')[:16])}</code>\n"
        f"🕑 آخر ظهور: <code>{escape((user.get('last_seen') or '—')[:16])}</code>\n"
        f"📌 الحالة: <b>{'🚫 محظور' if is_banned else '✅ نشط'}</b>\n"
    )
    if is_banned and user.get("ban_reason"):
        header += f"🚫 سبب الحظر: <b>{escape(user.get('ban_reason'))}</b>\n"

    if tab == "refs":
        try:
            counts = referral.get_referrer_counts(target_id)
            referred_by = referral.get_referrer_of(target_id)
        except Exception:
            counts, referred_by = {}, None
        body = (
            "\n🎁 <b>الإحالات</b>\n"
            f"• أحال: <b>{counts.get('total', 0)}</b> (مؤهلون: <b>{counts.get('qualified', 0)}</b>"
            f" • معلقون: {counts.get('pending', 0)} • مشبوهون: {counts.get('suspicious', 0)})\n"
        )
        if referred_by:
            body += (
                f"• أُحيل بواسطة: <code>{referred_by.get('referrer_id')}</code>"
                f" • الحالة: {referred_by.get('status')}\n"
            )
        else:
            body += "• لم يُحَل بواسطة أحد (تسجيل مباشر).\n"
    elif tab == "inst":
        try:
            inst = referral.get_instance_by_owner(target_id)
        except Exception:
            inst = None
        if inst:
            plan_badge = "👑 Pro" if (inst.get('plan') or 'basic') == 'pro' else "🆓 Basic"
            exp_raw = inst.get('expires_at')
            exp_txt = str(exp_raw)[:10] if exp_raw else "—"
            body = (
                "\n🤖 <b>النسخة الخاصة</b>\n"
                f"• البوت: <b>@{escape(inst.get('bot_username') or '—')}</b>\n"
                f"• الحالة: {_INST_STATUS.get(inst.get('status'), inst.get('status'))}\n"
                f"• الخطة: {plan_badge}\n"
                f"• الانتهاء: <code>{escape(exp_txt)}</code>\n"
                f"• آخر خطأ: <code>{escape(inst.get('last_error') or 'لا يوجد')}</code>\n"
                f"• التفعيل: <code>{escape((inst.get('activated_at') or '—')[:16] if inst.get('activated_at') else '—')}</code>\n"
            )
        else:
            body = "\n🤖 <b>النسخة الخاصة</b>\n• لا يملك نسخة خاصة.\n"
    else:
        body = (
            "\n📥 <b>نشاط التحميل</b>\n"
            f"• الإجمالي: <b>{stats.get('total', 0)}</b>\n"
            f"• فيديو: <b>{stats.get('video', 0)}</b> • صوت: <b>{stats.get('audio', 0)}</b>\n"
        )

    rows = [
        [inline_button("📋 النشاط والتسجيل", callback_data=f"own_user_{target_id}"),
         inline_button("🎁 الإحالات", callback_data=f"own_user_{target_id}_refs")],
        [inline_button("🤖 النسخة الخاصة", callback_data=f"own_user_{target_id}_inst")],
    ]
    if is_banned:
        rows.append([inline_button("✅ فك الحظر", callback_data=f"own_unban_{target_id}")])
    else:
        rows.append([inline_button("🚫 حظر المستخدم", callback_data=f"own_ban_{target_id}")])
    rows.append([inline_button("🔙 المستخدمون", callback_data="own_users")])
    return header + body, _kb(rows)


# ---- referrals section ------------------------------------------------------------------

def _refs_screen() -> tuple[str, InlineKeyboardMarkup]:
    try:
        ov = referral.get_network_overview()
    except Exception:
        ov = {}
    by = ov.get("by_status", {})
    text = (
        "🎁 <b>مركز الإحالات</b>\n"
        "متابعة شبكة الدعوات والمكافآت\n\n"
        f"• إجمالي الإحالات: <b>{ov.get('total', 0)}</b>\n"
        f"• ✅ مؤهلون: <b>{by.get('qualified', 0)}</b> • ⏳ معلقون: <b>{by.get('pending', 0)}</b>"
        f" • 🚩 مشبوهون: <b>{by.get('suspicious', 0)}</b>\n"
        f"• نسبة التأهيل: <b>{ov.get('conversion', 0)}%</b>\n"
        f"• أكواد الدعوة الصادرة: <b>{ov.get('codes', 0)}</b>\n"
        f"• الهدف الحالي: <b>{ov.get('target', 0)}</b> مستخدمًا مؤهلًا\n\n"
        "🏆 <b>المكافآت</b>\n"
        f"• قيد التقدم: {ov.get('rewards', {}).get('progress', 0)}"
        f" • مؤهلون للفتح: {ov.get('rewards', {}).get('eligible', 0)}"
        f" • بانتظار توكن: {ov.get('rewards', {}).get('awaiting_token', 0)}"
        f" • مستلمون: {ov.get('rewards', {}).get('claimed', 0)}"
    )
    keyboard = _kb([
        [inline_button("🏆 ترتيب المحيلين", callback_data="own_refs_top")],
        [inline_button("📜 تغيير قواعد الإحالات", callback_data="own_set_target")],
        *_back(),
    ])
    return text, keyboard


async def _refs_top(query):
    try:
        top = referral.get_top_referrers(12)
    except Exception:
        top = []
    lines = [
        f"{i}. <code>{r['referrer_id']}</code> — <b>{int(r.get('qualified') or 0)}</b> مؤهل"
        f" / {int(r.get('total') or 0)} إحالة"
        + (f" • 🚩 {int(r.get('suspicious') or 0)}" if int(r.get("suspicious") or 0) else "")
        for i, r in enumerate(top, 1)
    ] or ["• لا توجد إحالات مسجلة بعد."]
    rows = [[inline_button(f"👤 {r['referrer_id']}", callback_data=f"own_user_{r['referrer_id']}")] for r in top[:8]]
    rows.append([inline_button("🔙 مركز الإحالات", callback_data="own_refs")])
    await _edit(query, "🏆 <b>ترتيب المحيلين</b>\n\n" + "\n".join(lines),
                parse_mode="HTML", reply_markup=_kb(rows))


# ---- redemption codes section -------------------------------------------------------------
# Owner-only code center: generate / list (status tabs) / usage log / revoke.
# Full code text is shown EXACTLY once at creation; everywhere else it is
# masked. Every action is written to the security audit trail.

_CODE_STATUS_AR = {
    "active": "🟢 نشط",
    "exhausted": "🈳 مستهلك",
    "revoked": "⛔ ملغى",
    "expired": "⌛ منتهي",
}

_CODE_AUDIT_ACTION_AR = {
    "created": "➕ إنشاء",
    "reserved": "🔒 حجز استخدام",
    "used": "✅ استخدام ناجح",
    "released": "↩️ إفراج (تفعيل فشل/أُلغي)",
    "revoked": "⛔ إلغاء",
    "exhausted": "🈳 استنفاد",
}

# Wizard presets (the owner can always pick «مخصص» and type any value).
_CODE_WIZ_DAYS = [(1, "1 يوم"), (7, "7 أيام"), (30, "30 يومًا"), (40, "40 يومًا")]
_CODE_WIZ_USES = [(1, "مرة واحدة"), (2, "مرتان"), (5, "5 مرات")]
_CODE_WIZ_VALID = [(7, "7 أيام"), (14, "14 يومًا"), (30, "30 يومًا")]


def _code_effective_status(c: dict) -> str:
    """Stored status, except an 'active' code past expires_at shows منتهي."""
    st = c.get("status") or "active"
    if st == "active":
        exp = c.get("expires_at")
        if exp and datetime.utcnow() > exp:
            return "expired"
    return st


def _codes_screen(status: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    from services import redemption
    counts = redemption.code_status_counts()
    codes = redemption.list_codes(10, None if status == "all" else status)
    lines = []
    for c in codes:
        eff = _code_effective_status(c)
        badge = "👑" if c.get("plan") == "pro" else "🆓"
        target = f" • 👤 <code>{c['targeted_user_id']}</code>" if c.get("targeted_user_id") else ""
        lines.append(
            f"• <code>{redemption.mask_code(c['code'])}</code> {badge}"
            f" — {_CODE_STATUS_AR.get(eff, eff)}"
            f" • <b>{int(c.get('uses_consumed') or 0)}/{int(c.get('max_uses') or 1)}</b>"
            f"{target}"
        )
    body = "\n".join(lines) or "• لا توجد أكواد في هذا القسم."
    text = (
        "🎟️ <b>مركز أكواد التفعيل</b>\n\n"
        f"🟢 نشطة: <b>{counts['active']}</b> • 🈳 مستهلكة: <b>{counts['exhausted']}</b>"
        f" • ⌛ منتهية: <b>{counts['expired']}</b> • ⛔ ملغية: <b>{counts['revoked']}</b>\n\n"
        f"{body}\n\n"
        "🔒 الإنشاء والإلغاء وتعديل الإعدادات متاح لك وحدك، وكل عملية تُسجل في سجل التدقيق."
    )
    rows = [
        [inline_button("➕ توليد رمز جديد", callback_data="own_codes_new")],
        [inline_button(f"🗂 الكل ({counts['all']})", callback_data="own_codes_tab_all"),
         inline_button(f"🟢 نشطة ({counts['active']})", callback_data="own_codes_tab_active")],
        [inline_button(f"🈳 مستهلكة ({counts['exhausted']})", callback_data="own_codes_tab_exhausted"),
         inline_button(f"⌛ منتهية ({counts['expired']})", callback_data="own_codes_tab_expired"),
         inline_button(f"⛔ ملغية ({counts['revoked']})", callback_data="own_codes_tab_revoked")],
    ]
    for c in codes[:8]:
        rows.append([inline_button(
            f"🎫 {redemption.mask_code(c['code'])}", callback_data=f"own_codes_detail_{c['id']}")])
    rows += _back()
    return text, _kb(rows)


async def _code_detail(query, code_id: int):
    from services import redemption
    c = redemption.get_code(code_id)
    if not c:
        await _edit(query, "❌ الكود غير موجود.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_codes", "🔙 الأكواد")))
        return
    eff = _code_effective_status(c)
    badge = "👑 Pro" if c.get("plan") == "pro" else "🆓 Basic"
    target_line = (f"• مخصص للمستخدم: <code>{c['targeted_user_id']}</code>\n"
                   if c.get("targeted_user_id") else "• متاح لأي مستخدم\n")
    uses = redemption.code_uses(code_id, 10)
    if uses:
        use_lines = []
        for u in uses:
            bot = f" — @{escape(u.get('bot_username') or '—')}" if u.get("bot_username") else ""
            use_lines.append(
                f"• 👤 <code>{u['user_id']}</code>{bot} • 🕑 {str(u.get('created_at') or '—')[:16]}"
            )
        uses_body = "\n".join(use_lines)
    else:
        uses_body = "• لم يستخدمه أحد بعد."
    text = (
        f"🎫 <b>تفاصيل الكود #{code_id}</b>\n\n"
        f"• الكود: <code>{redemption.mask_code(c['code'])}</code>\n"
        f"• الحالة: {_CODE_STATUS_AR.get(eff, eff)}\n"
        f"• نوع النسخة: {badge}\n"
        f"• مدة التشغيل: <b>{int(c.get('runtime_days') or 0)} يومًا</b>\n"
        f"• الاستخدامات: <b>{int(c.get('uses_consumed') or 0)} / {int(c.get('max_uses') or 1)}</b>\n"
        f"• صالح للإدخال حتى: <code>{str(c.get('expires_at') or '—')[:16]}</code>\n"
        f"{target_line}"
        + (f"• ملاحظة: {escape(str(c.get('note') or ''))}\n" if c.get("note") else "")
        + f"\n📋 <b>سجل الاستخدام</b>\n{uses_body}"
    )
    rows = []
    for u in uses[:5]:
        if u.get("user_id"):
            rows.append([inline_button(f"👤 {u['user_id']}", callback_data=f"own_user_{u['user_id']}")])
    if eff in ("active", "expired"):
        rows.append([inline_button("⛔ إلغاء الكود", callback_data=f"own_codes_revoke_{code_id}")])
    rows.append([inline_button("🧾 سجل التدقيق الكامل", callback_data=f"own_codes_audit_{code_id}")])
    rows += _back("own_codes", "🔙 الأكواد")
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _code_audit(query, code_id: int):
    from services import redemption
    events = redemption.code_audit_trail(code_id, 20)
    lines = [
        f"• {_CODE_AUDIT_ACTION_AR.get(r.get('action'), r.get('action'))}"
        f" — 👤 <code>{r.get('user_id') or '—'}</code>"
        + (f" • 🤖 #{r['instance_id']}" if r.get("instance_id") else "")
        + f" • 🕑 {str(r.get('created_at') or '')[:16]}"
        for r in events
    ] or ["• لا توجد أحداث مسجلة."]
    await _edit(query,
        f"🧾 <b>سجل تدقيق الكود #{code_id}</b>\n\n" + "\n".join(lines)
        + "\n\n🔐 لا يحتوي السجل على نصوص الأكواد أو التوكنات أبدًا.",
        parse_mode="HTML",
        reply_markup=_kb([
            [inline_button("🎫 تفاصيل الكود", callback_data=f"own_codes_detail_{code_id}")],
            *_back("own_codes", "🔙 الأكواد"),
        ]))


async def _code_revoke_confirm(query, code_id: int):
    from services import redemption
    c = redemption.get_code(code_id)
    if not c:
        await _edit(query, "❌ الكود غير موجود.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_codes", "🔙 الأكواد")))
        return
    await _edit(query,
        "⚠️ <b>تأكيد إلغاء الكود — قبل التنفيذ</b>\n\n"
        f"• الكود: <code>{redemption.mask_code(c['code'])}</code>\n"
        f"• الاستهلاك الحالي: <b>{int(c.get('uses_consumed') or 0)}/{int(c.get('max_uses') or 1)}</b>\n\n"
        "بعد التنفيذ: لا يستطيع أي مستخدم استخدام هذا الكود فورًا، "
        "وتُسجل العملية في سجل التدقيق.",
        parse_mode="HTML",
        reply_markup=_kb([
            [inline_button("✅ تأكيد الإلغاء", callback_data=f"own_codes_revdo_{code_id}"),
             inline_button("↩️ تراجع", callback_data=f"own_codes_detail_{code_id}")],
        ]))


async def _code_revoke_do(query, code_id: int):
    from services import redemption
    c = redemption.get_code(code_id) or {}
    ok, reason = redemption.revoke_code(code_id, by_owner=query.from_user.id)
    if ok:
        log_admin_action(query.from_user.id, "owner_code_revoke", code_id,
                         detail=f"prefix={redemption.code_prefix(c.get('code') or '')}")
        msg = ("⛔ <b>تم إلغاء الكود</b> — لا يمكن استخدامه من الآن فصاعدًا، "
               "وسُجلت العملية في سجل التدقيق.")
    else:
        msg = f"ℹ️ تعذر الإلغاء: <code>{escape(str(reason))}</code>"
    await _edit(query, msg, parse_mode="HTML",
                reply_markup=_kb([
                    [inline_button("🎫 تفاصيل الكود", callback_data=f"own_codes_detail_{code_id}")],
                    *_back("own_codes", "🔙 الأكواد"),
                ]))


# ---- code creation wizard ------------------------------------------------------------------
# Step chain: plan → runtime days → uses → code validity → user restriction
# → note → confirmation. The draft lives in OWNER_PENDING[uid]["code_draft"].

def _code_wiz_draft(uid: int) -> dict:
    return (OWNER_PENDING.get(uid) or {}).get("code_draft") or {}


def _code_wiz_summary(d: dict) -> str:
    parts = []
    if d.get("plan"):
        parts.append("• نوع النسخة: " + ("👑 Pro" if d["plan"] == "pro" else "🆓 Basic"))
    if d.get("days"):
        parts.append(f"• مدة التفعيل: <b>{d['days']} يومًا</b>")
    if d.get("uses"):
        parts.append(f"• عدد مرات الاستخدام: <b>{d['uses']}</b> (نسخ يمكن تفعيلها)")
    if d.get("valid"):
        parts.append(f"• صلاحية الكود قبل الاستخدام: <b>{d['valid']} يومًا</b>")
    if d.get("target") == "open":
        parts.append("• متاح لأي مستخدم")
    elif d.get("targeted_user_id"):
        parts.append(f"• مخصص للمستخدم: <code>{d['targeted_user_id']}</code>")
    if d.get("note"):
        parts.append(f"• ملاحظة: {escape(str(d['note']))}")
    return ("\n".join(parts) + "\n\n") if parts else ""


def _code_wiz_plan_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "➕ <b>توليد رمز جديد — خطوة 1: نوع النسخة</b>\n\n"
        "🆓 <b>Basic</b> — نسخة أساسية (كود بادئته <code>BASIC-</code>)\n"
        "👑 <b>Pro</b> — نسخة برو (كود بادئته <code>PRO-</code>)\n\n"
        "اختر نوع النسخة التي يمنحها الكود:"
    )
    kb = _kb([
        [inline_button("🆓 كود Basic", callback_data="own_codes_wiz_plan_basic"),
         inline_button("👑 كود Pro", callback_data="own_codes_wiz_plan_pro")],
        *_back("own_codes", "🔙 إلغاء"),
    ])
    return text, kb


def _code_wiz_step(uid: int, step: str) -> tuple[str, InlineKeyboardMarkup]:
    """(text, keyboard) for the next wizard step, with the draft summary."""
    d = _code_wiz_draft(uid)
    summary = _code_wiz_summary(d)
    if step == "days":
        rows = [[inline_button(label, callback_data=f"own_codes_wiz_days_{v}")]
                for v, label in _CODE_WIZ_DAYS]
        rows.append([inline_button("✏️ مدة مخصصة", callback_data="own_codes_wiz_days_custom")])
        text = (f"➕ <b>توليد رمز جديد — خطوة 2: مدة التفعيل</b>\n\n{summary}"
                "كم يومًا تعمل النسخة بعد تفعيل الكود؟")
    elif step == "uses":
        rows = [[inline_button(label, callback_data=f"own_codes_wiz_uses_{v}")]
                for v, label in _CODE_WIZ_USES]
        rows.append([inline_button("✏️ عدد مخصص", callback_data="own_codes_wiz_uses_custom")])
        text = (f"➕ <b>توليد رمز جديد — خطوة 3: عدد مرات الاستخدام</b>\n\n{summary}"
                "عدد <b>النسخ</b> التي يمكن تفعيلها بالكود (مستخدمون مختلفون) — "
                "ولا يُستهلك الاستخدام إلا بعد نجاح إنشاء النسخة.")
    elif step == "valid":
        rows = [[inline_button(label, callback_data=f"own_codes_wiz_valid_{v}")]
                for v, label in _CODE_WIZ_VALID]
        rows.append([inline_button("✏️ صلاحية مخصصة", callback_data="own_codes_wiz_valid_custom")])
        text = (f"➕ <b>توليد رمز جديد — خطوة 4: صلاحية الكود</b>\n\n{summary}"
                "الكود ينتهي تلقائيًا بعد هذه المدة من إنشائه — حتى لو لم يستخدمه أحد.")
    elif step == "target":
        rows = [
            [inline_button("🌐 مفتوح لأي مستخدم", callback_data="own_codes_wiz_target_open")],
            [inline_button("👤 مخصص لمستخدم محدد", callback_data="own_codes_wiz_target_user")],
        ]
        text = (f"➕ <b>توليد رمز جديد — خطوة 5: تقييد المستخدم</b>\n\n{summary}"
                "هل الكود مفتوح لأي مستخدم، أم مخصص لمستخدم محدد؟")
    elif step == "note":
        rows = [
            [inline_button("📝 إضافة ملاحظة", callback_data="own_codes_wiz_note")],
            [inline_button("⏭ تخطي", callback_data="own_codes_wiz_note_skip")],
        ]
        text = (f"➕ <b>توليد رمز جديد — خطوة 6: ملاحظة</b>\n\n{summary}"
                "ملاحظة داخلية اختيارية تساعدك على تذكر سبب إصدار الكود.")
    else:  # confirm
        d = _code_wiz_draft(uid)
        pending = OWNER_PENDING.setdefault(uid, {})
        pending.update({
            "code_plan": d.get("plan", "basic"),
            "code_days": int(d.get("days") or 30),
            "code_uses": int(d.get("uses") or 1),
            "code_valid": int(d.get("valid") or 7),
            "code_note": d.get("note", ""),
            "code_target": d.get("targeted_user_id"),
        })
        rows = [
            [inline_button("✅ تأكيد الإنشاء", callback_data="own_codes_do"),
             inline_button("↩️ إلغاء", callback_data="own_codes")],
        ]
        text = ("⚠️ <b>تأكيد إنشاء الكود — قبل التنفيذ</b>\n\n" + _code_wiz_summary(d)
                + "\nبعد التنفيذ: يولَّد كود عشوائي قوي ويُعرض <b>مرة واحدة فقط</b>، "
                  "ويُسجل في سجل التدقيق.")
    rows += _back("own_codes", "🔙 إلغاء")
    return text, _kb(rows)


# ---- instances section -------------------------------------------------------------------

async def _inst_list(query):
    try:
        insts = referral.list_all_instances(20)
    except Exception:
        insts = []
    def _inst_line(i: dict) -> str:
        plan = "👑 Pro" if i.get("plan") == "pro" else "🆓 Basic"
        left = referral.instance_days_left(i)
        validity = ""
        if i.get("status") == "active" and left is not None:
            validity = f" • باقي <b>{left}</b> يومًا"
        elif i.get("status") == "expired":
            validity = f" • انتهت {escape(str(i.get('expires_at') or '')[:10])}"
        return (
            f"• <b>@{escape(i.get('bot_username') or '—')}</b> (#<code>{i['id']}</code>)"
            f" — {_INST_STATUS.get(i.get('status'), i.get('status'))}"
            f" • {plan}{validity}"
            f" • المالك <code>{i.get('owner_id')}</code>"
        )

    lines = [_inst_line(i) for i in insts] or ["• لا توجد نسخ مسجلة في النظام."]
    rows = [[inline_button(f"🤖 #{i['id']} @{(i.get('bot_username') or '—')[:20]}",
                           callback_data=f"own_inst_{i['id']}")] for i in insts[:10]]
    rows.append([inline_button("🔙 مركز التحكم", callback_data="own_home")])
    await _edit(query,
        f"🤖 <b>إدارة النسخ الخاصة ({len(insts)})</b>\n"
        "كل بوت أنشأه مستخدم من خلال النظام\n\n" + "\n".join(lines),
        parse_mode="HTML", reply_markup=_kb(rows))


async def _inst_detail(query, inst_id: int):
    inst = referral.get_instance(inst_id)
    if not inst:
        await _edit(query, "❌ النسخة غير موجودة.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    status = inst.get("status")
    plan = inst.get("plan") or "basic"
    plan_badge = "👑 Pro" if plan == "pro" else "🆓 Basic"
    days_left = referral.instance_days_left(inst)
    text = (
        f"🤖 <b>النسخة #{inst_id}</b>\n\n"
        f"• البوت: <b>@{escape(inst.get('bot_username') or '—')}</b>\n"
        f"• المالك: <code>{inst.get('owner_id')}</code>\n"
        f"• الحالة: {_INST_STATUS.get(status, status)}\n"
        f"• الخطة: <b>{plan_badge}</b> (مدة {referral.clone_plan_days(plan)} يومًا)\n"
        f"• فُعّلت: <code>{escape(str(inst.get('activated_at') or '—')[:16])}</code>\n"
        f"• تنتهي في: <code>{escape(str(inst.get('expires_at') or '—')[:16])}</code>\n"
        + (f"• الأيام المتبقية: <b>{days_left}</b>\n" if days_left is not None else "")
        + f"• آخر خطأ: <code>{escape(inst.get('last_error') or 'لا يوجد')}</code>\n"
        f"• أُنشئت: <code>{escape(str(inst.get('created_at') or '—')[:16])}</code>\n"
    )
    rows = [[inline_button("👤 ملف المالك", callback_data=f"own_user_{inst.get('owner_id')}")]]
    if status == "active":
        rows.append([inline_button("🛑 إيقاف النسخة", callback_data=f"own_inst_stop_{inst_id}")])
        rows.append([inline_button("⏳ تمديد المدة", callback_data=f"own_inst_extend_{inst_id}"),
                     inline_button("🔁 تبديل الخطة", callback_data=f"own_inst_plansw_{inst_id}")])
    elif status == "expired":
        # The ONLY path back online: an owner-approved renewal.
        rows.append([inline_button("🔄 تجديد المدة وإعادة التشغيل", callback_data=f"own_inst_extend_{inst_id}"),
                     inline_button("🔁 تبديل الخطة", callback_data=f"own_inst_plansw_{inst_id}")])
    elif status in ("disabled", "error"):
        rows.append([inline_button("▶️ إعادة التشغيل", callback_data=f"own_inst_start_{inst_id}")])
    # PERMANENT delete — offered for EVERY status (an expired/disabled clone
    # is exactly the one an owner wants gone for good). Confirm-gated below.
    rows.append([inline_button("🗑️ حذف النسخة تمامًا", callback_data=f"own_inst_del_{inst_id}")])
    rows.append([inline_button("🔙 إدارة النسخ", callback_data="own_inst")])
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _inst_stop_confirm(query, inst_id: int):
    """Pre-execution: show instance name + owner, exactly as the contract says."""
    inst = referral.get_instance(inst_id)
    if not inst:
        await _edit(query, "❌ النسخة غير موجودة.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    text = (
        "⚠️ <b>تأكيد إيقاف نسخة</b>\n\n"
        f"• اسم النسخة: <b>@{escape(inst.get('bot_username') or '—')}</b>\n"
        f"• المالك: <code>{inst.get('owner_id')}</code>\n"
        f"• المعرف: #<code>{inst_id}</code>\n\n"
        "سيتم فصل النسخة من العملية الحالية ومنع إقلاعها تلقائيًا عند إعادة التشغيل."
    )
    rows = [
        [inline_button("✅ نعم، أوقف النسخة", callback_data=f"own_inst_stopdo_{inst_id}"),
         inline_button("↩️ تراجع", callback_data=f"own_inst_{inst_id}")],
    ]
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _inst_stop_do(context, query, inst_id: int):
    inst = referral.get_instance(inst_id) or {}
    await stop_instance(inst_id)
    referral.set_instance_status(inst_id, "disabled")
    referral.log_activation(inst.get("owner_id"), inst_id, "instance_disabled_by_owner", "")
    log_admin_action(query.from_user.id, "owner_stop_instance", inst_id,
                     detail=f"bot=@{inst.get('bot_username', '')} owner={inst.get('owner_id')}")
    try:
        await context.bot.send_message(
            int(inst.get("owner_id")),
            f"⚠️ تم إيقاف نسختك <b>@{escape(inst.get('bot_username') or '')}</b> بقرار إداري."
            " افتح تذكرة من 🛟 الدعم الفني للاستفسار.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await _edit(query,
        f"✅ <b>تم إيقاف النسخة #{inst_id}</b> (@{escape(inst.get('bot_username') or '—')})\n"
        "تم تحديث الحالة في النظام وإشعار المالك.",
        parse_mode="HTML", reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))


async def _inst_start_do(context, query, inst_id: int):
    inst = referral.get_instance(inst_id)
    if not inst:
        await _edit(query, "❌ النسخة غير موجودة.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    ok = await spawn_instance(inst)
    # The spawn guard may have marked the clone 'expired' (validity passed) —
    # respect that: an expired clone must be RENEWED, not merely restarted.
    fresh = referral.get_instance(inst_id) or {}
    if fresh.get("status") == "expired":
        new_status = "expired"
    else:
        new_status = "active" if ok else "error"
    referral.set_instance_status(inst_id, new_status)
    referral.log_activation(inst.get("owner_id"), inst_id,
                            "instance_started_by_owner", f"ok={ok} status={new_status}")
    log_admin_action(query.from_user.id, "owner_start_instance", inst_id,
                     detail=f"ok={ok} status={new_status} bot=@{inst.get('bot_username', '')}")
    if new_status == "expired":
        await _edit(query,
            f"⌛ <b>لا يمكن تشغيل النسخة #{inst_id}</b> — انتهت مدة خطتها.\n"
            "اضغط «🔄 تجديد المدة وإعادة التشغيل» لتجديدها بقرارك.",
            parse_mode="HTML", reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    await _edit(query,
        ("✅ <b>تم إعادة تشغيل النسخة</b>." if ok else
         "❌ <b>فشل تشغيل النسخة</b> — راجع حقل آخر خطأ في تفاصيل النسخة."),
        parse_mode="HTML", reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))


async def _inst_extend_do(context, query, inst_id: int):
    """Owner-approved renewal/extension: push the validity window forward and
    bring the clone back online. The ONLY path an expired clone returns."""
    inst = referral.get_instance(inst_id)
    if not inst:
        await _edit(query, "❌ النسخة غير موجودة.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    new_expiry = referral.extend_instance(inst_id)
    was_down = inst.get("status") in ("expired", "disabled", "error")
    started = False
    if was_down:
        fresh = referral.get_instance(inst_id) or inst
        started = await spawn_instance(fresh)
        referral.set_instance_status(inst_id, "active" if started else "error")
    referral.log_activation(inst.get("owner_id"), inst_id, "instance_renewed_by_owner",
                            f"expires_at={new_expiry} restarted={started}")
    log_admin_action(query.from_user.id, "owner_extend_instance", inst_id,
                     detail=f"expires={new_expiry} plan={inst.get('plan') or 'basic'} restarted={started}")
    try:
        await context.bot.send_message(
            int(inst.get("owner_id")),
            f"✅ <b>تم تجديد مدة نسختك</b> @{escape(inst.get('bot_username') or '')}\n"
            f"📅 الانتهاء الجديد: <code>{escape(str(new_expiry)[:16])}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await _edit(query,
        f"✅ <b>تم تمديد النسخة #{inst_id}</b> (@{escape(inst.get('bot_username') or '—')})\n"
        f"📅 تنتهي الآن في: <code>{escape(str(new_expiry)[:16])}</code>\n"
        + ("▶️ تمت إعادة تشغيل النسخة." if started else "")
        + ("\n⚠️ فشل إعادة التشغيل — راجع آخر خطأ في التفاصيل." if was_down and not started else ""),
        parse_mode="HTML", reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))


async def _inst_plansw_do(context, query, inst_id: int):
    """Owner plan switch: 🆓 basic ↔ 👑 pro. Recomputes the expiry window."""
    inst = referral.get_instance(inst_id)
    if not inst:
        await _edit(query, "❌ النسخة غير موجودة.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    new_plan = "basic" if (inst.get("plan") or "basic") == "pro" else "pro"
    new_expiry = referral.set_instance_plan(inst_id, new_plan)
    log_admin_action(query.from_user.id, "owner_instance_plan", inst_id,
                     detail=f"plan={new_plan} expires={new_expiry} bot=@{inst.get('bot_username', '')}")
    await _edit(query,
        f"🔁 <b>تم تبديل خطة النسخة #{inst_id}</b>\n\n"
        f"• الخطة الجديدة: <b>{'👑 Pro' if new_plan == 'pro' else '🆓 Basic'}</b>"
        f" (مدة {referral.clone_plan_days(new_plan)} يومًا)\n"
        f"• الانتهاء الجديد: <code>{escape(str(new_expiry)[:16])}</code>",
        parse_mode="HTML", reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))


async def _inst_del_confirm(query, inst_id: int):
    """Permanent-delete confirmation: show the clone's identity and the FULL
    blast radius BEFORE execution — the owner must see exactly what dies."""
    inst = referral.get_instance(inst_id)
    if not inst:
        await _edit(query, "❌ النسخة غير موجودة.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    plan_badge = "👑 Pro" if (inst.get("plan") or "basic") == "pro" else "🆓 Basic"
    text = (
        "🗑️ <b>تأكيد الحذف النهائي للنسخة</b>\n\n"
        f"• اسم النسخة: <b>@{escape(inst.get('bot_username') or '—')}</b>\n"
        f"• المالك: <code>{inst.get('owner_id')}</code>\n"
        f"• المعرف: #<code>{inst_id}</code>\n"
        f"• الخطة: <b>{plan_badge}</b>\n\n"
        "سيتم محو النسخة <b>وكل ما يخصها نهائيًا</b>:\n"
        "• سجل تحميلاتها وإخفاقاتها\n"
        "• إداريوها وصلاحياتهم وقائمة حظرها وسجل إدارتها\n"
        "• بثاتها وإعداداتها وتوكنها المشفر\n"
        "• سجلات تفعيلها واستردادها\n"
        "• صف النسخة نفسه من النظام\n\n"
        "📌 تبقى بصمة الاستهلاك (تجربة/مكافأة) مسجلة بلا إشارة —"
        " الحذف النهائي لا يمنح مالكها تجربة ثانية ولا مكافأة جديدة.\n\n"
        "⛔ <b>لا يمكن التراجع عن هذه العملية.</b>"
    )
    rows = [
        [inline_button("✅ نعم، احذف النسخة نهائيًا", callback_data=f"own_inst_deldo_{inst_id}"),
         inline_button("↩️ تراجع", callback_data=f"own_inst_{inst_id}")],
    ]
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _inst_del_do(context, query, inst_id: int):
    """Execute the permanent delete: detach the running process, wipe every
    instance-scoped row (DB is the source of truth), audit PERMANENTLY in the
    main admin log, and notify the clone owner. The confirm step is UI — the
    owner gate on owner_callback plus the DB wipe are the real protection."""
    inst = referral.get_instance(inst_id)
    if not inst:
        await _edit(query, "❌ النسخة غير موجودة (ربما حُذفت بالفعل).", parse_mode="HTML",
                    reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))
        return
    # Best-effort process detach — the DB wipe is the actual point.
    try:
        await stop_instance(inst_id)
    except Exception:
        pass
    counts = referral.delete_instance_hard(inst_id)
    log_admin_action(query.from_user.id, "owner_delete_instance", inst_id,
                     detail=f"bot=@{inst.get('bot_username', '')} owner={inst.get('owner_id')} counts={counts}")
    try:
        await context.bot.send_message(
            int(inst.get("owner_id")),
            f"❌ <b>تم حذف نسختك @{escape(inst.get('bot_username') or '')} نهائيًا</b> بقرار إداري.\n"
            "النسخة وكل بياناتها أُزيلت من النظام ولا يمكن استعادتها."
            " للاعتراض أو الاستفسار افتح تذكرة من 🛟 الدعم الفني.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    def _cnt(key: str) -> str:
        v = counts.get(key, 0)
        return "⚠️ فشل" if v == -1 else str(v)

    await _edit(query,
        f"🗑️ <b>تم حذف النسخة #{inst_id} نهائيًا</b>\n"
        f"(@{escape(inst.get('bot_username') or '—')})\n\n"
        "ما تم محوه:\n"
        f"• التحميلات: <b>{_cnt('downloads')}</b> • الإخفاقات: <b>{_cnt('download_failures')}</b>\n"
        f"• الإداريون: <b>{_cnt('clone_admins')}</b> • الحظر: <b>{_cnt('clone_bans')}</b>\n"
        f"• سجل الإدارة: <b>{_cnt('clone_admin_log')}</b> • البثات: <b>{_cnt('clone_broadcasts')}</b>\n"
        f"• الإعدادات: <b>{_cnt('clone_settings')}</b> • التوكن: <b>{_cnt('bot_secrets')}</b>\n"
        f"• سجل التفعيل: <b>{_cnt('activation_log')}</b> • الاسترداد: <b>{_cnt('redemption_log')}</b>\n"
        f"• صف النسخة: <b>{_cnt('bot_instances')}</b>\n\n"
        + ("✅ الحذف اكتمل — النسخة لم يعد لها أي أثر." if counts.get("bot_instances") == 1 else
           "⚠️ راجع الأرقام أعلاه — أي جدول عليه «⚠️ فشل» لم يُمحَ بالكامل."),
        parse_mode="HTML", reply_markup=_kb(_back("own_inst", "🔙 إدارة النسخ")))


# ---- downloads section --------------------------------------------------------------------

def _dl_screen() -> tuple[str, InlineKeyboardMarkup]:
    platforms = get_downloads_by_platform()
    top = sorted(platforms.items(), key=lambda kv: kv[1], reverse=True)[:8]
    plat_lines = "\n".join(
        f"• <b>{escape(str(p or 'غير معروف'))}</b>: {c}" for p, c in top
    ) or "• لا توجد تحميلات مسجلة بعد."
    text = (
        "📥 <b>مركز التحميلات</b>\n"
        "فهم استخدام البوت ومشاكل التنزيل\n\n"
        f"• اليوم: <b>{get_downloads_today()}</b> • آخر 24 ساعة: <b>{get_downloads_24h()}</b>\n"
        f"• آخر ٧ أيام: <b>{get_downloads_week()}</b> • آخر ٣٠ يومًا: <b>{get_downloads_month()}</b>\n"
        f"• الإجمالي المسجل: <b>{get_total_downloads()}</b>\n"
        f"• حجم البيانات الكلي: <b>{_fmt_bytes(get_total_data_bytes())}</b>\n\n"
        "🌍 <b>المنصات</b>\n" + plat_lines
    )
    keyboard = _kb([
        [inline_button("🩺 صحة النظام", callback_data="own_health"),
         inline_button("🧠 حالة المحرك", callback_data="own_engine")],
        *_back(),
    ])
    return text, keyboard


# ---- broadcast section ------------------------------------------------------------------------

def _bc_screen() -> tuple[str, InlineKeyboardMarkup]:
    history = get_broadcast_history(5)
    hist_lines = "\n".join(
        f"• <code>{escape(str(h.get('created_at') or '—')[:16])}</code>"
        f" — ✅ {h.get('success', 0)}/{h.get('total', 0)}"
        f" • ❌ {h.get('failed', 0)}"
        for h in history
    ) or "• لا توجد إذاعات مسجلة بعد."
    text = (
        "📢 <b>الرسائل والإذاعة</b>\n"
        "إرسال، معاينة، تقارير\n\n"
        f"👥 المستلمون المحتملون (غير محظورين): <b>{len(get_all_user_ids())}</b>\n\n"
        "<b>📜 آخر تقارير الإذاعة</b>\n" + hist_lines
    )
    keyboard = _kb([
        [inline_button("📝 إذاعة جديدة", callback_data="own_bc_new")],
        *_back(),
    ])
    return text, keyboard


async def _bc_preview(query):
    """Pre-execution: preview + recipients count, then explicit confirmation."""
    draft = (OWNER_PENDING.get(query.from_user.id) or {}).get("bc_text", "")
    recipients = len(get_all_user_ids())
    text = (
        "🔎 <b>معاينة الإذاعة قبل التنفيذ</b>\n\n"
        f"─────────────\n{escape(draft[:900])}\n─────────────\n\n"
        f"👥 عدد المستلمين: <b>{recipients}</b>\n"
        "سيتم نسخ الرسالة كما هي لكل المستخدمين (غير المحظورين)."
    )
    rows = [
        [inline_button("✅ تأكيد الإرسال", callback_data="own_bc_send")],
        [inline_button("🗑 إلغاء", callback_data="own_bc")],
    ]
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _bc_send(context, query):
    draft = (OWNER_PENDING.get(query.from_user.id) or {}).get("bc_text", "")
    if not draft:
        await _edit(query, "❌ لا توجد مسودة إذاعة. ابدأ من جديد.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_bc", "🔙 الإذاعة")))
        return
    OWNER_PENDING.pop(query.from_user.id, None)
    await _edit(query, "⏳ <b>جاري إرسال الإذاعة...</b>", parse_mode="HTML")
    all_ids = get_all_user_ids()
    success = failed = blocked = 0
    started = time.time()
    for idx, uid in enumerate(all_ids, 1):
        try:
            await context.bot.send_message(chat_id=uid, text=draft[:4096])
            success += 1
        except Exception as exc:
            failed += 1
            msg = str(exc).lower()
            if "blocked" in msg or "chat not found" in msg or "forbidden" in msg:
                blocked += 1
        if idx % 20 == 0:
            await asyncio.sleep(1)
    elapsed = round(time.time() - started, 1)
    try:
        log_broadcast(query.from_user.id, draft[:300], len(all_ids), success, failed)
    except Exception:
        pass
    log_admin_action(query.from_user.id, "owner_broadcast",
                     detail=f"success={success} failed={failed} total={len(all_ids)}")
    report = (
        "✅ <b>انتهت الإذاعة — تقرير النجاح والفشل</b>\n\n"
        f"👥 الإجمالي: <b>{len(all_ids)}</b>\n"
        f"✅ وصل: <b>{success}</b>\n"
        f"❌ فشل: <b>{failed}</b>\n"
        f"🚫 محظور/غير نشط: <b>{blocked}</b>\n"
        f"⏱ الزمن: <b>{elapsed}s</b>"
    )
    await _edit(query, report, parse_mode="HTML", reply_markup=_kb(_back()))


# ---- support section --------------------------------------------------------------------------

async def _sup_list(query, status: str | None = None):
    tickets = support.list_tickets(status=status, limit=15)
    text = (
        "🛟 <b>الدعم الفني</b>\n"
        "التذاكر والردود والتصعيد\n\n"
        f"🔓 مفتوحة: <b>{support.count_tickets('open')}</b>"
        f" • 🟢 تم الرد: <b>{support.count_tickets('answered')}</b>"
        f" • ⚫ مغلقة: <b>{support.count_tickets('closed')}</b>\n\n"
    )
    if tickets:
        text += "\n".join(
            f"• <code>{escape(tk['ticket_no'])}</code> — {escape(str(tk.get('subject') or tk.get('category')))}"
            f" — {_SUP_STATUS.get(tk.get('status'), tk.get('status'))}"
            for tk in tickets
        )
    else:
        text += "لا توجد تذاكر في هذه الفئة."
    rows = [[
        inline_button("🔓 المفتوحة", callback_data="own_sup"),
        inline_button("🟢 تم الرد", callback_data="own_sup_answered"),
        inline_button("⚫ المغلقة", callback_data="own_sup_closed"),
    ]]
    for tk in tickets[:10]:
        rows.append([inline_button(f"🎫 {tk['ticket_no']}", callback_data=f"own_sup_{tk['id']}")])
    rows.append([inline_button("🔙 مركز التحكم", callback_data="own_home")])
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _sup_detail(query, tid: int):
    tk = support.get_ticket(tid)
    if not tk:
        await _edit(query, "❌ التذكرة غير موجودة.", parse_mode="HTML",
                    reply_markup=_kb(_back("own_sup", "🔙 التذاكر")))
        return
    msgs = support.get_messages(tk["id"], limit=20)
    lines = [
        f"{'🛟 الدعم' if m['sender'] == 'admin' else '👤 المستخدم'} • <code>{_format_timestamp(m.get('created_at'))}</code>\n"
        f"{escape(str(m.get('body') or ''))}"
        for m in msgs
    ]
    text = (
        f"🎫 <b>{tk['ticket_no']}</b>\n"
        f"👤 المستخدم: <code>{tk['user_id']}</code>\n"
        f"🏷 النوع: <b>{escape(str(tk.get('subject') or tk.get('category')))}</b>\n"
        f"📌 الحالة: {_SUP_STATUS.get(tk.get('status'), tk.get('status'))}\n\n"
        + ("\n\n".join(lines) if lines else "لا توجد رسائل بعد.")
    )
    rows = [
        [inline_button("📝 رد على التذكرة", callback_data=f"own_sup_reply_{tid}")],
        [inline_button("👤 ملف المستخدم", callback_data=f"own_user_{tk['user_id']}")],
    ]
    if tk["status"] != "closed":
        rows.append([inline_button("⚫ إغلاق التذكرة", callback_data=f"own_sup_close_{tid}")])
    else:
        rows.append([inline_button("🔓 إعادة فتح", callback_data=f"own_sup_open_{tid}")])
    rows.append([inline_button("🔙 التذاكر", callback_data="own_sup")])
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


# ---- reports section ---------------------------------------------------------------------------

def _rep_screen() -> tuple[str, InlineKeyboardMarkup]:
    try:
        ov = referral.get_network_overview()
    except Exception:
        ov = {}
    text = (
        "📈 <b>التقارير والتحليلات</b>\n"
        "متابعة نمو المشروع\n\n"
        "👥 <b>نمو المستخدمين</b>\n"
        f"• اليوم: <b>{get_new_users_today()}</b> • الأسبوع: <b>{get_new_users_week()}</b>"
        f" • الشهر: <b>{get_new_users_month()}</b>\n"
        f"• الإجمالي: <b>{get_total_users()}</b> • نشط 24 ساعة: <b>{get_active_24h()}</b>\n\n"
        "📥 <b>نمو التحميلات</b>\n"
        f"• اليوم: <b>{get_downloads_today()}</b> • الأسبوع: <b>{get_downloads_week()}</b>"
        f" • الشهر: <b>{get_downloads_month()}</b>\n"
        f"• الإجمالي: <b>{get_total_downloads()}</b> • البيانات: <b>{_fmt_bytes(get_total_data_bytes())}</b>\n\n"
        "🎁 <b>نمو شبكة الإحالات</b>\n"
        f"• إجمالي الإحالات: <b>{ov.get('total', 0)}</b>"
        f" • مؤهلون: <b>{ov.get('by_status', {}).get('qualified', 0)}</b>\n"
        f"• نسبة التأهيل: <b>{ov.get('conversion', 0)}%</b>"
        f" • نسخ نشطة: <b>{ov.get('instances', {}).get('active', 0)}</b>\n\n"
        "🛟 <b>الدعم</b>\n"
        f"• تذاكر مفتوحة: <b>{support.count_tickets('open')}</b>"
        f" • تم الرد: <b>{support.count_tickets('answered')}</b>\n\n"
        "📜 <b>سجل الأمان</b>\n"
        f"• عمليات مسجلة: <b>{get_admin_audit_stats().get('total', 0)}</b>"
    )
    keyboard = _kb([
        [inline_button("📜 سجل الأمان الكامل", callback_data="own_audit"),
         inline_button("🔄 تحديث", callback_data="own_rep")],
        *_back(),
    ])
    return text, keyboard


# ---- settings & security --------------------------------------------------------------------------

def _set_screen() -> tuple[str, InlineKeyboardMarkup]:
    """Button-first settings hub. Every button maps to an existing, owner-gated action."""
    text = "\u2063"
    keyboard = _kb([
        [inline_button("🛡 الصلاحيات والأدمن", callback_data="own_set_perms")],
        [inline_button("🚧 وضع الصيانة: تفعيل / إيقاف", callback_data="own_set_maint_on"),
         inline_button("🟢 إيقاف الصيانة", callback_data="own_set_maint_off")],
        [inline_button("🎯 قواعد الإحالات والهدف", callback_data="own_set_target")],
        [inline_button("🧹 تنظيف الملفات المؤقتة", callback_data="own_set_clean")],
        [inline_button("📜 سجل الأمان والتغييرات", callback_data="own_audit")],
        [inline_button("📈 حالة النظام والتقارير", callback_data="own_rep")],
        [inline_button("🔙 مركز التحكم الرئيسي", callback_data="own_home")],
    ])
    return text, keyboard

def _perms_screen() -> tuple[str, InlineKeyboardMarkup]:
    extra = list_extra_admins()
    lines = [
        f"• <code>{a['user_id']}</code> • أُضيف: {(a.get('added_at') or '—')[:16]}"
        for a in extra
    ] or ["• لا يوجد أدمن ممنوحون حاليًا."]
    text = (
        "🛡 <b>الصلاحيات</b>\n\n"
        f"👑 المالك الأساسي: <code>{OWNER_ID}</code> — <b>ثابت من البيئة، لا يمكن تعديله من البوت</b>\n"
        f"🛡 أدمن البيئة: <code>{', '.join(str(a) for a in ADMIN_IDS) or '—'}</code>\n\n"
        "<b>أدمن ممنوحون من المالك (قابلون للسحب)</b>\n" + "\n".join(lines)
        + "\n\n⚠️ منح/سحب الأدمن يُسجل في سجل الأمان فورًا."
    )
    rows = [[inline_button("➕ منح أدمن جديد", callback_data="own_set_perm_add")]]
    for a in extra[:8]:
        rows.append([inline_button(f"❌ سحب {a['user_id']}", callback_data=f"own_set_perm_del_{a['user_id']}")])
    rows.append([inline_button("🔙 الإعدادات", callback_data="own_set")])
    return text, _kb(rows)


async def _audit_screen(query):
    stats = get_admin_audit_stats()
    entries = get_admin_audit(15)
    lines = []
    for i, e in enumerate(entries, 1):
        line = (
            f"{i}. <b>{escape(str(e.get('action') or '—'))}</b>"
            f" • <code>{escape(str(e.get('admin_id') or '—'))}</code>"
        )
        if e.get("target_id"):
            line += f" • هدف <code>{e.get('target_id')}</code>"
        line += f"\n   🕑 {escape(str(e.get('created_at') or '—')[:16])}"
        if e.get("detail"):
            line += f" • {escape(str(e.get('detail'))[:80])}"
        lines.append(line)
    text = (
        "📜 <b>سجل الأمان</b>\n\n"
        f"إجمالي العمليات: <b>{stats.get('total', 0)}</b>\n\n"
        + ("\n\n".join(lines) if lines else "لا توجد أحداث مسجلة بعد.")
    )
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(_back("own_set", "🔙 الإعدادات")))


async def _clean_confirm(query):
    """Pre-execution: show the targeted size, exactly as the contract says."""
    size, count = _temp_dir_stats()
    text = (
        "🧹 <b>تنظيف الملفات المؤقتة</b>\n\n"
        f"• الحجم المستهدف: <b>{_fmt_bytes(size)}</b>\n"
        f"• عدد الملفات: <b>{count}</b>\n\n"
        "سيتم حذف كل الملفات الأقدم من ساعة داخل مجلد temp."
    )
    rows = [
        [inline_button("✅ نفذ التنظيف", callback_data="own_set_cleando"),
         inline_button("↩️ تراجع", callback_data="own_set")],
    ]
    await _edit(query, text, parse_mode="HTML", reply_markup=_kb(rows))


async def _clean_do(query):
    now = time.time()
    freed = removed = 0
    try:
        for name in os.listdir(TEMP_DIR):
            path = os.path.join(TEMP_DIR, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > 3600:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except Exception:
        pass
    log_admin_action(query.from_user.id, "owner_cleanup",
                     detail=f"removed={removed} freed={freed}")
    await _edit(query,
        "✅ <b>تم التنظيف — نتيجة العملية</b>\n\n"
        f"• ملفات محذوفة: <b>{removed}</b>\n"
        f"• مساحة محررة: <b>{_fmt_bytes(freed)}</b>",
        parse_mode="HTML", reply_markup=_kb(_back("own_set", "🔙 الإعدادات")))


# ---- callback dispatcher -------------------------------------------------------------------------

async def owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # THE code-level owner gate — every single callback passes through here.
    if not _owner_gate(update) or not _clone_guard(update):
        try:
            await query.answer("⛔ هذه اللوحة محمية للمالك الأساسي فقط.", show_alert=True)
        except Exception:
            pass
        return
    uid = query.from_user.id
    data = query.data or ""
    await query.answer()

    if data == "own_home":
        OWNER_STATES.pop(uid, None)
        text, kb = _build_home()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)

    # ---- users ----
    elif data == "own_users":
        OWNER_STATES.pop(uid, None)
        await _edit(query, _users_home_text(), parse_mode="HTML", reply_markup=_users_home_kb())
    elif data.startswith("own_users_page_"):
        await _show_users_page(query, _suffix_int(data) or 0, "all")
    elif data == "own_users_active":
        await _show_users_page(query, 0, "active")
    elif data == "own_users_banned":
        await _show_banned(query)
    elif data == "own_users_new":
        await _show_new_users(query)
    elif data == "own_users_top":
        await _show_top_users(query)
    elif data == "own_users_search":
        OWNER_STATES[uid] = "search"
        await _edit(query,
            "🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن <code>ID</code> أو <code>@username</code> أو الاسم.",
            parse_mode="HTML", reply_markup=_kb(_back("own_users", "🔙 إلغاء")))
    elif data == "own_users_export":
        await _export_csv(context, query)

    # ---- user profile ----
    elif data.startswith("own_user_"):
        OWNER_STATES.pop(uid, None)
        parts = data.split("_")
        target_id = _suffix_int("x_" + parts[2]) if len(parts) >= 3 else None
        tab = parts[3] if len(parts) >= 4 else "main"
        if not target_id:
            return
        rendered = _user_profile(target_id, tab)
        if rendered is None:
            await _edit(query, "❌ المستخدم غير موجود.", parse_mode="HTML",
                        reply_markup=_kb(_back("own_users", "🔙 المستخدمون")))
            return
        text, kb = rendered
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)

    # ---- ban flow: reason → confirm → execute ----
    elif data.startswith("own_ban_") and not data.startswith("own_ban_do_"):
        target_id = _suffix_int(data)
        if not target_id:
            return
        OWNER_STATES[uid] = f"ban_reason:{target_id}"
        await _edit(query,
            f"🚫 <b>حظر المستخدم <code>{target_id}</code></b>\n\n"
            "أرسل الآن <b>سبب الحظر</b> (سيُحفظ في ملف المستخدم وسجل الأمان):",
            parse_mode="HTML", reply_markup=_kb(_back(f"own_user_{target_id}", "↩️ تراجع")))
    elif data.startswith("own_ban_do_"):
        target_id = _suffix_int(data)
        pending = OWNER_PENDING.pop(uid, {})
        reason = pending.get("ban_reason", "")
        if not target_id or pending.get("ban_target") != target_id or not reason:
            await _edit(query, "⚠️ انتهت صلاحية جلسة الحظر. أعد المحاولة من ملف المستخدم.",
                        parse_mode="HTML", reply_markup=_kb(_back()))
            return
        user = get_user(target_id)
        if user and user.get("is_banned"):
            await _edit(query, "ℹ️ المستخدم محظور بالفعل.", parse_mode="HTML",
                        reply_markup=_kb(_back(f"own_user_{target_id}", "🔙 الملف")))
            return
        ban_user(target_id, reason)
        log_admin_action(uid, "owner_ban_user", target_id, detail=f"reason={reason[:120]}")
        try:
            await context.bot.send_message(
                target_id,
                f"🚫 <b>تم حظر حسابك</b>\n\nالسبب: {escape(reason[:300])}\n"
                "للاعتراض تواصل مع الدعم.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await _edit(query,
            f"✅ <b>تم حظر المستخدم <code>{target_id}</code></b>\n"
            f"• السبب: {escape(reason[:200])}\n"
            f"• تم التنفيذ والتسجيل في سجل الأمان وإشعار المستخدم.",
            parse_mode="HTML", reply_markup=_kb(_back(f"own_user_{target_id}", "🔙 الملف")))
    elif data.startswith("own_unban_do_"):
        target_id = _suffix_int(data)
        if not target_id:
            return
        unban_user(target_id)
        log_admin_action(uid, "owner_unban_user", target_id)
        try:
            await context.bot.send_message(target_id, "✅ تم رفع الحظر عن حسابك.")
        except Exception:
            pass
        await _edit(query,
            f"✅ <b>تم فك حظر المستخدم <code>{target_id}</code></b> — نُفذ وسُجل في سجل الأمان.",
            parse_mode="HTML", reply_markup=_kb(_back(f"own_user_{target_id}", "🔙 الملف")))
    elif data.startswith("own_unban_"):
        target_id = _suffix_int(data)
        if not target_id:
            return
        user = get_user(target_id)
        if not user or not user.get("is_banned"):
            await _edit(query, "ℹ️ المستخدم غير محظور.", parse_mode="HTML",
                        reply_markup=_kb(_back(f"own_user_{target_id}", "🔙 الملف")))
            return
        name = escape(user.get("first_name") or str(target_id))
        rows = [
            [inline_button("✅ نعم، فك الحظر", callback_data=f"own_unban_do_{target_id}"),
             inline_button("↩️ تراجع", callback_data=f"own_user_{target_id}")],
        ]
        await _edit(query,
            f"⚠️ <b>تأكيد فك الحظر</b>\n\n• المستخدم: <b>{name}</b>\n• المعرف: <code>{target_id}</code>",
            parse_mode="HTML", reply_markup=_kb(rows))

    # ---- referrals ----
    elif data == "own_refs":
        OWNER_STATES.pop(uid, None)
        text, kb = _refs_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_refs_top":
        await _refs_top(query)

    # ---- redemption codes (owner-only, rides the existing plan system) ----
    elif data == "own_codes":
        OWNER_STATES.pop(uid, None)
        text, kb = _codes_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data.startswith("own_codes_tab_"):
        OWNER_STATES.pop(uid, None)
        text, kb = _codes_screen(data.rsplit("_", 1)[-1])
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    # ---- creation wizard: plan → days → uses → validity → restriction → note → confirm
    elif data == "own_codes_new":
        OWNER_STATES.pop(uid, None)
        OWNER_PENDING.pop(uid, None)
        text, kb = _code_wiz_plan_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data.startswith("own_codes_wiz_plan_"):
        plan = "pro" if data.endswith("pro") else "basic"
        OWNER_PENDING[uid] = {"code_draft": {"plan": plan}}
        text, kb = _code_wiz_step(uid, "days")
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data.startswith("own_codes_wiz_days_"):
        val = data.rsplit("_", 1)[-1]
        if val == "custom":
            OWNER_STATES[uid] = "code_wiz_days"
            await _edit(query,
                "✏️ <b>مدة مخصصة</b>\n\nأرسل عدد الأيام (1–365):",
                parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
        else:
            d = _code_wiz_draft(uid)
            d["days"] = int(val)
            text, kb = _code_wiz_step(uid, "uses")
            await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data.startswith("own_codes_wiz_uses_"):
        val = data.rsplit("_", 1)[-1]
        if val == "custom":
            OWNER_STATES[uid] = "code_wiz_uses"
            await _edit(query,
                "✏️ <b>عدد مخصص</b>\n\nأرسل عدد الاستخدامات (1–50):",
                parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
        else:
            d = _code_wiz_draft(uid)
            d["uses"] = int(val)
            text, kb = _code_wiz_step(uid, "valid")
            await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data.startswith("own_codes_wiz_valid_"):
        val = data.rsplit("_", 1)[-1]
        if val == "custom":
            OWNER_STATES[uid] = "code_wiz_valid"
            await _edit(query,
                "✏️ <b>صلاحية مخصصة</b>\n\nأرسل عدد الأيام التي يبقى الكود خلالها صالحًا للإدخال (1–365):",
                parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
        else:
            d = _code_wiz_draft(uid)
            d["valid"] = int(val)
            text, kb = _code_wiz_step(uid, "target")
            await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_codes_wiz_target_open":
        d = _code_wiz_draft(uid)
        d["target"] = "open"
        d["targeted_user_id"] = None
        text, kb = _code_wiz_step(uid, "note")
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_codes_wiz_target_user":
        OWNER_STATES[uid] = "code_wiz_target"
        await _edit(query,
            "👤 <b>تقييد المستخدم</b>\n\nأرسل <b>معرّف المستخدم (ID) الرقمي</b> الذي سيُخصص له الكود:",
            parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
    elif data == "own_codes_wiz_note":
        OWNER_STATES[uid] = "code_wiz_note"
        await _edit(query,
            "📝 <b>ملاحظة الكود</b>\n\nأرسل نص الملاحظة (اختياري — تظهر لك وحدك في التفاصيل):",
            parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
    elif data == "own_codes_wiz_note_skip":
        text, kb = _code_wiz_step(uid, "confirm")
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_codes_do":
        pending = OWNER_PENDING.pop(uid, {})
        if not pending.get("code_plan"):
            await _edit(query, "⚠️ انتهت صلاحية جلسة الإنشاء. أعد المحاولة من قائمة الأكواد.",
                        parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 الأكواد")))
            return
        from services import redemption
        res = redemption.create_code(
            pending["code_plan"], pending["code_days"], pending["code_uses"],
            pending["code_valid"], note=pending.get("code_note", ""), created_by=uid,
            targeted_user_id=pending.get("code_target"),
        )
        if not res.get("ok"):
            await _edit(query, f"⚠️ تعذر إنشاء الكود: <code>{escape(str(res.get('reason')))}</code>",
                        parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 الأكواد")))
            return
        log_admin_action(uid, "owner_code_create", res["code_id"],
                         detail=f"plan={res['plan']} days={res['runtime_days']} "
                                f"uses={res['max_uses']} valid={res['valid_days']} "
                                f"targeted={res.get('targeted_user_id') or 'open'} "
                                f"prefix={redemption.code_prefix(res['code'])}")
        badge = "👑 Pro" if res["plan"] == "pro" else "🆓 Basic"
        target_line = (f"• مخصص للمستخدم: <code>{res['targeted_user_id']}</code>\n"
                       if res.get("targeted_user_id") else "• متاح لأي مستخدم\n")
        await _edit(query,
            "✅ <b>تم إنشاء الكود</b>\n\n"
            f"🎟️ الكود: <code>{res['code']}</code>\n"
            f"• الخطة: {badge} • مدة التشغيل: <b>{res['runtime_days']} يومًا</b>\n"
            f"• عدد الاستخدامات: <b>{res['max_uses']}</b> (مستخدمون مختلفون)\n"
            f"• صالح للإدخال حتى: <b>{str(res['expires_at'])[:10]}</b>\n"
            f"{target_line}\n"
            "⚠️ <b>هذا هو العرض الوحيد للكود كاملًا</b> — انسخه الآن وأرسله "
            "للمستفيد. بعدها يظهر مقنعًا في القائمة.\n"
            "• سُجل الإنشاء في سجل التدقيق.",
            parse_mode="HTML",
            reply_markup=_kb([
                [inline_button("🎫 تفاصيل الكود", callback_data=f"own_codes_detail_{res['code_id']}")],
                *_back("own_codes", "🔙 الأكواد"),
            ]))
    elif data.startswith("own_codes_revdo_"):
        code_id = _suffix_int(data)
        if code_id:
            await _code_revoke_do(query, code_id)
    elif data.startswith("own_codes_revoke_"):
        code_id = _suffix_int(data)
        if code_id:
            await _code_revoke_confirm(query, code_id)
    elif data.startswith("own_codes_audit_"):
        code_id = _suffix_int(data)
        if code_id:
            await _code_audit(query, code_id)
    elif data.startswith("own_codes_detail_"):
        code_id = _suffix_int(data)
        if code_id:
            await _code_detail(query, code_id)

    # ---- instances ----
    elif data == "own_inst":
        OWNER_STATES.pop(uid, None)
        await _inst_list(query)
    elif data.startswith("own_inst_stopdo_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_stop_do(context, query, inst_id)
    elif data.startswith("own_inst_extend_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_extend_do(context, query, inst_id)
    elif data.startswith("own_inst_plansw_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_plansw_do(context, query, inst_id)
    elif data.startswith("own_inst_stop_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_stop_confirm(query, inst_id)
    elif data.startswith("own_inst_start_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_start_do(context, query, inst_id)
    elif data.startswith("own_inst_deldo_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_del_do(context, query, inst_id)
    elif data.startswith("own_inst_del_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_del_confirm(query, inst_id)
    elif data.startswith("own_inst_"):
        inst_id = _suffix_int(data)
        if inst_id:
            await _inst_detail(query, inst_id)

    # ---- downloads ----
    elif data == "own_dl":
        OWNER_STATES.pop(uid, None)
        text, kb = _dl_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_health":
        from services.runtime_diagnostics import render_diagnostics_html
        await _edit(query, render_diagnostics_html(), parse_mode="HTML",
                    reply_markup=_kb(_back("own_dl", "🔙 التحميلات")))
    elif data == "own_engine":
        # Re-render inside the owner panel with an owner back button.
        from config.settings import YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER, DOWNLOAD_PROXY
        from services.engine_maintenance import get_ytdlp_version
        from services.engines import list_engines
        cookies_ready = bool(YTDLP_COOKIES_FROM_BROWSER or (YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE)))
        engines = list_engines()
        text = (
            "🧠 <b>حالة محرك التنزيل</b>\n\n"
            f"• yt-dlp: <code>{escape(get_ytdlp_version())}</code>\n"
            f"• Cookies: <b>{'مفعّلة' if cookies_ready else 'غير مفعّلة'}</b>\n"
            f"• Proxy: <b>{'مفعّل' if DOWNLOAD_PROXY else 'غير مفعّل'}</b>\n"
            f"• المحركات النشطة: <b>{len(engines)}</b>\n\n"
            + "\n".join(f"• <code>{escape(e['name'])}</code>" for e in engines)
        )
        await _edit(query, text, parse_mode="HTML", reply_markup=_kb(_back("own_dl", "🔙 التحميلات")))

    # ---- broadcast ----
    elif data == "own_bc":
        OWNER_STATES.pop(uid, None)
        OWNER_PENDING.pop(uid, None)
        text, kb = _bc_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_bc_new":
        OWNER_STATES[uid] = "broadcast"
        await _edit(query,
            "📢 <b>إذاعة جديدة</b>\n\nأرسل الآن نص الرسالة — ستراها أولًا في <b>معاينة مع عدد المستلمين</b> قبل أي إرسال.",
            parse_mode="HTML", reply_markup=_kb(_back("own_bc", "🔙 إلغاء")))
    elif data == "own_bc_preview":
        await _bc_preview(query)
    elif data == "own_bc_send":
        await _bc_send(context, query)

    # ---- support ----
    elif data == "own_sup":
        OWNER_STATES.pop(uid, None)
        await _sup_list(query)
    elif data == "own_sup_answered":
        await _sup_list(query, "answered")
    elif data == "own_sup_closed":
        await _sup_list(query, "closed")
    elif data.startswith("own_sup_reply_"):
        tid = _suffix_int(data)
        if not tid:
            return
        tk = support.get_ticket(tid)
        if not tk:
            return
        OWNER_STATES[uid] = f"sup_reply:{tid}"
        await _edit(query,
            f"📝 <b>الرد على التذكرة {escape(tk['ticket_no'])}</b>\n\nأرسل نص الرد الآن — سيصل المستخدم إشعار فوري.",
            parse_mode="HTML", reply_markup=_kb(_back(f"own_sup_{tid}", "🔙 التذكرة")))
    elif data.startswith("own_sup_close_"):
        tid = _suffix_int(data)
        tk = support.get_ticket(tid) if tid else None
        if tk:
            support.close_ticket(tid)
            log_admin_action(uid, "owner_support_close", detail=str(tk.get("ticket_no", "")))
            try:
                await context.bot.send_message(
                    tk["user_id"],
                    f"🛟 تذكرتك <code>{tk['ticket_no']}</code> أُغلقت.", parse_mode="HTML")
            except Exception:
                pass
        await _sup_detail(query, tid)
    elif data.startswith("own_sup_open_"):
        tid = _suffix_int(data)
        tk = support.get_ticket(tid) if tid else None
        if tk:
            support.reopen_ticket(tid)
            log_admin_action(uid, "owner_support_reopen", detail=str(tk.get("ticket_no", "")))
        await _sup_detail(query, tid)
    elif data.startswith("own_sup_"):
        tid = _suffix_int(data)
        if tid:
            await _sup_detail(query, tid)

    # ---- reports ----
    elif data == "own_rep":
        OWNER_STATES.pop(uid, None)
        text, kb = _rep_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)

    # ---- settings ----
    elif data == "own_set":
        OWNER_STATES.pop(uid, None)
        text, kb = _set_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_set_perms":
        text, kb = _perms_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_set_perm_add":
        OWNER_STATES[uid] = "perm_add"
        await _edit(query,
            "➕ <b>منح أدمن جديد</b>\n\nأرسل <code>ID</code> المستخدم (أرقام فقط).\n"
            "⚠️ الأدمن يرى لوحة الأدمن العادية فقط — <b>لوحة المالك تظل لك وحدك</b>.",
            parse_mode="HTML", reply_markup=_kb(_back("own_set_perms", "🔙 إلغاء")))
    # NOTE: execution callbacks (*_do_) MUST be matched BEFORE their shorter
    # confirmation prefixes, otherwise the confirm screen swallows them.
    elif data.startswith("own_set_perm_del_do_"):
        target_id = _suffix_int(data)
        if not target_id:
            return
        remove_extra_admin(target_id)
        log_admin_action(uid, "owner_revoke_admin", target_id, detail="سحب صلاحية أدمن")
        text, kb = _perms_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data.startswith("own_set_perm_add_do_"):
        target_id = _suffix_int(data)
        if not target_id:
            return
        add_extra_admin(target_id)
        log_admin_action(uid, "owner_grant_admin", target_id, detail="منح صلاحية أدمن")
        text, kb = _perms_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data.startswith("own_set_perm_del_"):
        target_id = _suffix_int(data)
        if not target_id:
            return
        rows = [
            [inline_button("✅ نعم، اسحب الصلاحية", callback_data=f"own_set_perm_del_do_{target_id}"),
             inline_button("↩️ تراجع", callback_data="own_set_perms")],
        ]
        await _edit(query,
            f"⚠️ <b>تأكيد سحب صلاحية أدمن</b>\n\n• المستخدم: <code>{target_id}</code>\n"
            "سيفقد الوصول للوحة الأدمن فورًا.",
            parse_mode="HTML", reply_markup=_kb(rows))
    elif data == "own_audit":
        await _audit_screen(query)
    elif data == "own_set_target":
        OWNER_STATES[uid] = "target"
        current = referral.get_effective_target()
        await _edit(query,
            "🎯 <b>تغيير قواعد الإحالات</b>\n\n"
            f"• القاعدة الحالية: <b>{current}</b> مستخدمًا مؤهلًا = نسخة بوت خاصة.\n\n"
            "أرسل الرقم الجديد (سيُعرض لك مقارنة الحالي/الجديد قبل الحفظ):",
            parse_mode="HTML", reply_markup=_kb(_back("own_set", "🔙 إلغاء")))
    elif data.startswith("own_set_target_do_"):
        new_val = _suffix_int(data)
        if not new_val or new_val <= 0:
            return
        old = referral.get_effective_target()
        referral.set_target_override(new_val)
        log_admin_action(uid, "owner_change_referral_target",
                         detail=f"old={old} new={new_val}")
        await _edit(query,
            "✅ <b>تم حفظ القاعدة الجديدة</b>\n\n"
            f"• قبل: <b>{old}</b>\n• بعد: <b>{new_val}</b>\n"
            "• التعديل سُجل في سجل الأمان، ويسري فورًا على كل المستخدمين.",
            parse_mode="HTML", reply_markup=_kb(_back("own_set", "🔙 الإعدادات")))
    elif data == "own_set_clean":
        await _clean_confirm(query)
    elif data == "own_set_cleando":
        await _clean_do(query)
    elif data == "own_set_maint_on":
        from utils.maintenance import set_maintenance_mode
        set_maintenance_mode(True)
        log_admin_action(uid, "owner_maintenance", detail="enabled")
        text, kb = _set_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)
    elif data == "own_set_maint_off":
        from utils.maintenance import set_maintenance_mode
        set_maintenance_mode(False)
        log_admin_action(uid, "owner_maintenance", detail="disabled")
        text, kb = _set_screen()
        await _edit(query, text, parse_mode="HTML", reply_markup=kb)

    else:
        logger.warning("Unhandled owner callback: %s", data)


# ---- text states ---------------------------------------------------------------------------------

async def owner_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume owner text inputs (search / ban reason / broadcast / etc).

    Re-verifies ownership on EVERY message — the state dict alone is never
    trusted as authorization.
    """
    user = update.effective_user
    if not user or user.id not in OWNER_STATES:
        return False
    if not is_owner(user.id):
        OWNER_STATES.pop(user.id, None)
        OWNER_PENDING.pop(user.id, None)
        try:
            log_admin_action(user.id, "owner_panel_denied", detail="محاولة إدخال نصي غير مصرح")
        except Exception:
            pass
        return False
    uid = user.id
    state = OWNER_STATES.pop(uid)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ أرسل نصًا صالحًا.")
        return True

    if state == "search":
        results = search_users(text)
        if not results:
            await update.message.reply_text(
                "❌ لا توجد نتائج لهذا البحث.",
                reply_markup=_kb(_back("own_users", "🔙 المستخدمون")))
            return True
        if len(results) == 1:
            rendered = _user_profile(results[0]["user_id"])
            if rendered:
                t_text, kb = rendered
                await update.message.reply_text(t_text, parse_mode="HTML", reply_markup=kb)
            return True
        lines, rows = [], []
        for r in results[:10]:
            icon = "🚫" if r.get("is_banned") else "✅"
            lines.append(
                f"{icon} <b>{escape(r.get('first_name') or 'بدون اسم')}</b>"
                f" @{escape(r.get('username') or '—')} • <code>{r['user_id']}</code>"
            )
            rows.append([inline_button(f"👤 {r['user_id']}", callback_data=f"own_user_{r['user_id']}")])
        rows.append([inline_button("🔙 المستخدمون", callback_data="own_users")])
        await update.message.reply_text(
            f"🔍 <b>نتائج البحث ({len(results)})</b>\n\n" + "\n".join(lines),
            parse_mode="HTML", reply_markup=_kb(rows))
        return True

    if state.startswith("ban_reason:"):
        target_id = int(state.split(":", 1)[1])
        OWNER_PENDING[uid] = {"ban_target": target_id, "ban_reason": text[:300]}
        user_row = get_user(target_id)
        name = escape((user_row or {}).get("first_name") or str(target_id))
        rows = [
            [inline_button("✅ تأكيد الحظر", callback_data=f"own_ban_do_{target_id}"),
             inline_button("↩️ إلغاء", callback_data=f"own_user_{target_id}")],
        ]
        await update.message.reply_text(
            "⚠️ <b>تأكيد الحظر — قبل التنفيذ</b>\n\n"
            f"• المستخدم: <b>{name}</b>\n"
            f"• المعرف: <code>{target_id}</code>\n"
            f"• السبب: <b>{escape(text[:300])}</b>\n\n"
            "بعد التنفيذ: حظر + تسجيل في سجل الأمان + إشعار المستخدم.",
            parse_mode="HTML", reply_markup=_kb(rows))
        return True

    if state == "broadcast":
        OWNER_PENDING[uid] = {"bc_text": text}
        # Re-render the preview through a fresh message with confirm buttons.
        recipients = len(get_all_user_ids())
        rows = [
            [inline_button("✅ تأكيد الإرسال", callback_data="own_bc_send")],
            [inline_button("🗑 إلغاء", callback_data="own_bc")],
        ]
        await update.message.reply_text(
            "🔎 <b>معاينة الإذاعة — قبل التنفيذ</b>\n\n"
            f"─────────────\n{escape(text[:900])}\n─────────────\n\n"
            f"👥 عدد المستلمين: <b>{recipients}</b>",
            parse_mode="HTML", reply_markup=_kb(rows))
        return True

    # ---- code creation wizard: custom-value text steps ------------------------
    if state in ("code_wiz_days", "code_wiz_uses", "code_wiz_valid",
                 "code_wiz_target", "code_wiz_note"):
        d = _code_wiz_draft(uid)
        if not d.get("plan"):
            await update.message.reply_text(
                "⚠️ انتهت صلاحية جلسة الإنشاء. أعد المحاولة من مركز الأكواد.",
                reply_markup=_kb(_back("own_codes", "🔙 الأكواد")))
            return True

        if state == "code_wiz_note":
            d["note"] = text[:200]
            t2, kb2 = _code_wiz_step(uid, "confirm")
            await update.message.reply_text(t2, parse_mode="HTML", reply_markup=kb2)
            return True

        if state == "code_wiz_target":
            if not text.lstrip("-").isdigit() or int(text) <= 0:
                await update.message.reply_text(
                    "⚠️ أرسل معرّف مستخدم رقميًا فقط (مثل <code>123456789</code>).",
                    parse_mode="HTML", reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
                return True
            d["target"] = "user"
            d["targeted_user_id"] = int(text)
            t2, kb2 = _code_wiz_step(uid, "note")
            await update.message.reply_text(t2, parse_mode="HTML", reply_markup=kb2)
            return True

        # numeric steps: days / uses / valid
        if not text.isdigit():
            await update.message.reply_text(
                "⚠️ أرسل رقمًا صحيحًا فقط.",
                reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
            return True
        val = int(text)
        limits = {"code_wiz_days": ("days", 1, 365, "مدة التشغيل"),
                  "code_wiz_uses": ("uses", 1, 50, "عدد الاستخدامات"),
                  "code_wiz_valid": ("valid", 1, 365, "صلاحية الكود")}[state]
        key, lo, hi, label = limits
        if not lo <= val <= hi:
            await update.message.reply_text(
                f"⚠️ {label} يجب أن تكون بين {lo} و{hi}.",
                reply_markup=_kb(_back("own_codes", "🔙 إلغاء")))
            return True
        d[key] = val
        nxt = {"code_wiz_days": "uses", "code_wiz_uses": "valid", "code_wiz_valid": "target"}[state]
        t2, kb2 = _code_wiz_step(uid, nxt)
        await update.message.reply_text(t2, parse_mode="HTML", reply_markup=kb2)
        return True

    if state.startswith("sup_reply:"):
        tid = int(state.split(":", 1)[1])
        tk = support.get_ticket(tid)
        if not tk:
            await update.message.reply_text("❌ التذكرة غير موجودة.")
            return True
        support.add_admin_reply(tk["id"], text[:1900])
        log_admin_action(uid, "owner_support_reply", detail=str(tk.get("ticket_no", "")))
        try:
            await context.bot.send_message(
                tk["user_id"],
                f"🛟 <b>رد جديد على تذكرتك <code>{tk['ticket_no']}</code></b>\n\n{escape(text[:1500])}",
                parse_mode="HTML")
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ تم إرسال الرد لصاحب التذكرة <code>{tk['ticket_no']}</code>.",
            parse_mode="HTML",
            reply_markup=_kb([[inline_button("🎫 عرض التذكرة", callback_data=f"own_sup_{tid}")]]))
        return True

    if state == "perm_add":
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("⚠️ أرسل ID رقميًا فقط.")
            return True
        target_id = int(text)
        rows = [
            [inline_button("✅ تأكيد المنح", callback_data=f"own_set_perm_add_do_{target_id}"),
             inline_button("↩️ إلغاء", callback_data="own_set_perms")],
        ]
        await update.message.reply_text(
            "⚠️ <b>تأكيد الصلاحيات الجديدة</b>\n\n"
            f"• المستخدم: <code>{target_id}</code>\n"
            f"• الصلاحية الممنوحة: <b>أدمن عادي</b> (لوحة /admin فقط)\n"
            "• لوحة المالك: <b>لا تُمنح أبدًا</b>\n\n"
            "سيُسجل التعديل في سجل الأمان.",
            parse_mode="HTML", reply_markup=_kb(rows))
        return True

    if state == "target":
        if not text.isdigit() or int(text) <= 0 or int(text) > 100000:
            await update.message.reply_text("⚠️ أرسل رقمًا صحيحًا أكبر من صفر.")
            return True
        new_val = int(text)
        current = referral.get_effective_target()
        rows = [
            [inline_button("✅ حفظ القاعدة", callback_data=f"own_set_target_do_{new_val}"),
             inline_button("↩️ إلغاء", callback_data="own_set")],
        ]
        await update.message.reply_text(
            "🎯 <b>تأكيد تغيير قاعدة الإحالات</b>\n\n"
            f"• القاعدة الحالية: <b>{current}</b> مستخدمًا مؤهلًا\n"
            f"• القاعدة الجديدة: <b>{new_val}</b> مستخدمًا مؤهلًا\n\n"
            "بعد الحفظ: تسري فورًا + تُسجل في سجل الأمان.",
            parse_mode="HTML", reply_markup=_kb(rows))
        return True

    logger.warning("Dropped unknown owner text state: %s", state)
    return True
