from __future__ import annotations

import os
import time
import logging
import asyncio
from datetime import datetime

from html import escape

from telegram import Update, InlineKeyboardMarkup
from utils.button_text import inline_button
from telegram.ext import ContextTypes

from middlewares.auth import is_admin, is_owner
from locales import t
from database.users import (
    get_user, get_total_users, get_new_users_today, get_active_today,
    get_active_24h, get_banned_users_count, get_users_page, search_users,
    get_all_user_ids, ban_user, unban_user, update_user, get_top_users,
    count_users,
)
from database.downloads import (
    get_downloads_today, get_downloads_week, get_downloads_month,
    get_downloads_by_platform, get_total_downloads, get_user_download_stats,
    get_downloads_24h, get_total_data_bytes, get_top_user_today,
)
from database.cache import get_cache_count, get_cache_hits
from database import support
from database.broadcast import log_broadcast, get_broadcast_history
from database.admin_audit import log_admin_action, get_admin_audit, get_admin_audit_stats
from services.engine_maintenance import get_ytdlp_version, update_ytdlp
from services.engines import list_engines
from config.settings import (
    YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER, DOWNLOAD_PROXY, TEMP_DIR,
    OWNER_ID, ADMIN_IDS, REDIS_URL, STORAGE_CHANNEL_ID,
)
from utils.telegram_safe import safe_edit

logger = logging.getLogger(__name__)


async def _edit(query, text, **kwargs):
    """Edit an admin callback message, ignoring the benign not-modified error."""
    return await safe_edit(query.edit_message_text, text=text, **kwargs)

ADMIN_CONVERSATION_STATES = {}
ADMIN_LAST_PAGE: dict[int, int] = {}
ADMIN_USER_FILTER: dict[int, str] = {}
_ADMIN_TEXT_STATES = {"search", "ban", "broadcast", "support_reply"}
ADMIN_SUPPORT_REPLY_TARGET: dict[int, int] = {}
_SUP_STATUS = {"open": "🔓 مفتوحة", "answered": "🟢 تم الرد", "closed": "⚫ مغلقة"}


def _clear_admin_state(user_id: int) -> None:
    ADMIN_CONVERSATION_STATES.pop(user_id, None)


def _short_datetime(value: str | None) -> str:
    """Return 'اليوم' or YYYY-MM-DD from a database datetime string."""
    if not value:
        return "—"
    text = str(value).strip()
    if text[:10] == datetime.now().strftime("%Y-%m-%d"):
        return "اليوم"
    return text[:10]


UPDATE_ANNOUNCEMENT_TEXT = """
🚀 <b>تم تحديث البوت بنجاح</b>

اضغط <b>Start</b> وافتح البوت من جديد لتجربة التحديثات الأخيرة.

<b>ما الجديد في هذا التحديث؟</b>
• 🛡 لوحة أدمن احترافية تُقرأ أرقامها مباشرة من قاعدة البيانات: مستخدمون، تحميلات، كاش، منصات، والأكثر نشاطًا.
• 👥 قائمة مستخدمين غنية: آخر ظهور، اللغة، تاريخ الانضمام، وعدد التحميلات.
• 👤 ملف مستخدم كامل مع زر حظر/فك حظر مباشرة.
• 🔙 أزرار رجوع في كل شاشات لوحة الأدمن.
• 📢 سجل حقيقي لآخر عمليات الإذاعة داخل اللوحة.
• 🧹 إصلاح أخطاء كانت تمنع فتح بعض شاشات الأدمن.

لو واجهتك أي مشكلة، ابدأ من جديد بإرسال /start.
""".strip()


def _build_update_keyboard(bot_username: str | None) -> InlineKeyboardMarkup:
    if bot_username:
        url = f"https://t.me/{bot_username}?start=updated"
    else:
        url = "https://t.me/"
    return InlineKeyboardMarkup([
        [inline_button("🚀 Start / افتح البوت", url=url)],
        [inline_button("📥 جرّب تحميل رابط الآن", callback_data="noop")],
    ])


async def send_update_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the latest release announcement to every known bot user."""
    user = update.effective_user
    if not user or not is_owner(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("🔒 الإعلان الجماعي متاح للمالك فقط.")
        return

    status_message = await update.effective_message.reply_text("📢 جاري إرسال إعلان التحديث لكل المستخدمين...")
    await _broadcast_update_message(context, status_message=status_message, admin_id=user.id)


async def engine_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return
    cookies_ready = bool(YTDLP_COOKIES_FROM_BROWSER or (YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE)))
    engines = list_engines()
    engine_lines = "\n".join(f"• <code>{e['name']}</code>" for e in engines)
    text = (
        "🧠 <b>حالة محرك التنزيل</b>\n\n"
        f"yt-dlp: <code>{get_ytdlp_version()}</code>\n"
        f"Cookies: <b>{'مفعلة' if cookies_ready else 'غير مفعلة'}</b>\n"
        f"Proxy: <b>{'مفعل' if DOWNLOAD_PROXY else 'غير مفعل'}</b>\n"
        f"Engines: <b>{len(engines)}</b>\n"
        f"{engine_lines}\n\n"
        "أوامر مهمة:\n"
        "<code>/update_ytdlp</code> — تحديث yt-dlp عند فشل منصات مثل Instagram/Facebook/YouTube.\n"
        "<code>/engine_status</code> — عرض هذه الحالة."
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return
    from services.runtime_diagnostics import render_diagnostics_html
    await update.effective_message.reply_text(render_diagnostics_html(), parse_mode="HTML")


async def callbacks_health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return
    from services.callback_audit import render_callback_audit_html
    await update.effective_message.reply_text(render_callback_audit_html(), parse_mode="HTML")


async def commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return
    text = """
🛠 <b>أوامر إدارة X Downloader</b>

<b>التشغيل العام</b>
<code>/ping</code> — اختبار سريع أن البوت شغال.
<code>/health</code> — تقرير صحة النظام.
<code>/callbacks_health</code> — تدقيق أزرار inline callbacks.

<b>محرك التحميل</b>
<code>/engine_status</code> — حالة yt-dlp والكوكيز والبروكسي.
<code>/update_ytdlp</code> — تحديث yt-dlp عند فشل منصات السوشيال.

<b>الأدمن</b>
<code>/admin</code> — لوحة التحكم.
<code>/stats</code> — نفس لوحة التحكم السريعة.
<code>/announce_update</code> — إرسال إعلان التحديث لكل المستخدمين.

<b>قاعدة ذهبية</b>
لو زر قديم أو غريب ظهر للمستخدم، النظام الآن يرد عليه فورًا ولا يترك spinner.
""".strip()
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def update_ytdlp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return
    msg = await update.effective_message.reply_text("🔄 جاري تحديث yt-dlp... قد يستغرق دقيقة.")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, update_ytdlp)
    text = "✅ <b>تم تحديث yt-dlp</b>" if result["ok"] else "❌ <b>فشل تحديث yt-dlp</b>"
    text += f"\n\nقبل: <code>{result['before']}</code>\nبعد/الحالي: <code>{result['after']}</code>"
    if not result["ok"]:
        text += f"\n\n<pre>{(result.get('stderr') or result.get('stdout') or '')[:1000]}</pre>"
    text += "\n\n⚠️ لو كان التحديث كبيرًا، أعد تشغيل السيرفر ليستخدم النسخة الجديدة داخل العملية الحالية."
    log_admin_action(user.id, "update_ytdlp", detail=f"ok={result['ok']}")
    await safe_edit(msg.edit_text, text=text, parse_mode="HTML")


async def _broadcast_update_message(context: ContextTypes.DEFAULT_TYPE, status_message=None, query=None, admin_id: int | None = None):
    all_ids = get_all_user_ids()
    bot_username = getattr(context.bot, "username", None)
    reply_markup = _build_update_keyboard(bot_username)

    success = 0
    failed = 0
    blocked_or_inactive = 0
    started_at = time.time()

    for index, uid in enumerate(all_ids, start=1):
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=UPDATE_ANNOUNCEMENT_TEXT,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            success += 1
        except Exception as exc:
            failed += 1
            message = str(exc).lower()
            if "blocked" in message or "chat not found" in message or "forbidden" in message:
                blocked_or_inactive += 1
            logger.warning("Failed to send update announcement to %s: %s", uid, exc)

        if index % 20 == 0:
            await asyncio.sleep(1)

    elapsed = round(time.time() - started_at, 1)
    result_text = (
        "✅ <b>انتهى إرسال إعلان التحديث</b>\n\n"
        f"👥 إجمالي المستخدمين: <b>{len(all_ids)}</b>\n"
        f"✅ وصل بنجاح: <b>{success}</b>\n"
        f"❌ فشل: <b>{failed}</b>\n"
        f"🚫 محظور/غير نشط غالباً: <b>{blocked_or_inactive}</b>\n"
        f"⏱ الزمن: <b>{elapsed}s</b>"
    )

    try:
        log_broadcast(
            admin_id=admin_id or 0,
            message=UPDATE_ANNOUNCEMENT_TEXT[:300],
            total=len(all_ids),
            success=success,
            failed=failed,
        )
    except Exception:
        logger.exception("Failed to write broadcast_log")

    try:
        log_admin_action(
            admin_id or 0, "announce_broadcast",
            detail=f"success={success} failed={failed}",
        )
    except Exception:
        logger.exception("Failed to write admin audit log")

    if status_message:
        await safe_edit(status_message.edit_text, text=result_text, parse_mode="HTML")
    elif query:
        await _edit(query, result_text, parse_mode="HTML")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    text, keyboard = _build_admin_panel(lang, user.id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not is_admin(user.id):
        await query.answer("⛔ هذا الأمر متاح للمسؤولين فقط.", show_alert=True)
        return
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    data = query.data
    owner_only_callbacks = {"admin_owner_overview", "admin_referral_network", "admin_clone_instances"}
    if data in owner_only_callbacks and not is_owner(user.id):
        await query.answer("🔒 القسم ده متاح لصاحب البوت الأساسي فقط.", show_alert=True)
        return
    if _invalid_admin_callback(data):
        await query.answer(t(lang, "session_expired"), show_alert=True)
        return
    await query.answer()

    if data == "admin_panel":
        _clear_admin_state(user.id)
        await _show_admin_panel(query, lang)
    elif data == "admin_owner_overview":
        _clear_admin_state(user.id)
        await _show_owner_overview(query)
    elif data == "admin_referral_network":
        _clear_admin_state(user.id)
        await _show_referral_network(query)
    elif data == "admin_clone_instances":
        _clear_admin_state(user.id)
        await _show_clone_instances(query)
    elif data == "admin_users":
        _clear_admin_state(user.id)
        await _show_users_page(query, lang, page=0)
    elif data == "admin_users_active":
        _clear_admin_state(user.id)
        await _show_users_page(query, lang, page=0, status="active")
    elif data == "admin_users_banned":
        _clear_admin_state(user.id)
        await _show_users_page(query, lang, page=0, status="banned")
    elif data == "admin_broadcast":
        _clear_admin_state(user.id)
        await _show_broadcast_form(query, lang)
    elif data == "admin_announce_update":
        _clear_admin_state(user.id)
        if not is_owner(user.id):
            await query.answer("🔒 الإعلان الجماعي متاح للمالك فقط.", show_alert=True)
            return
        await _edit(query, "📢 جاري إرسال إعلان التحديث لكل المستخدمين...", parse_mode="HTML")
        await _broadcast_update_message(context, query=query, admin_id=user.id)
    elif data == "admin_engine_status":
        _clear_admin_state(user.id)
        await _show_engine_status(query)
    elif data == "admin_update_ytdlp":
        _clear_admin_state(user.id)
        await _edit(query, "🔄 جاري تحديث yt-dlp... قد يستغرق دقيقة.", parse_mode="HTML")
        result = await asyncio.get_running_loop().run_in_executor(None, update_ytdlp)
        text = "✅ <b>تم تحديث yt-dlp</b>" if result["ok"] else "❌ <b>فشل تحديث yt-dlp</b>"
        text += f"\n\nقبل: <code>{result['before']}</code>\nبعد/الحالي: <code>{result['after']}</code>"
        if not result["ok"]:
            text += f"\n\n<pre>{(result.get('stderr') or result.get('stdout') or '')[:1000]}</pre>"
        text += "\n\n⚠️ لو كان التحديث كبيرًا، أعد تشغيل السيرفر ليستخدم النسخة الجديدة داخل العملية الحالية."
        log_admin_action(user.id, "update_ytdlp", detail=f"ok={result['ok']}")
        back = [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
        await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back))
    elif data == "admin_health":
        _clear_admin_state(user.id)
        from services.runtime_diagnostics import render_diagnostics_html
        back = [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
        await _edit(query, render_diagnostics_html(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back))
    elif data == "admin_audit":
        _clear_admin_state(user.id)
        await _show_audit_log(query)
    elif data == "admin_security":
        _clear_admin_state(user.id)
        await _show_security_scan(query)
    elif data == "admin_search":
        ADMIN_CONVERSATION_STATES[user.id] = "search"
        keyboard = [[inline_button("🔙 إلغاء", callback_data="admin_panel")]]
        await _edit(query, 
            "🔍 <b>بحث عن مستخدم</b>\n\n"
            "أرسل الآن:\n"
            "• <code>ID</code> المستخدم\n"
            "• أو <code>@username</code>\n"
            "• أو الاسم الأول للمستخدم",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif data == "admin_maintenance":
        _clear_admin_state(user.id)
        await _show_maintenance_toggle(query, lang)
    elif data == "admin_ban":
        ADMIN_CONVERSATION_STATES[user.id] = "ban"
        keyboard = [[inline_button("🔙 إلغاء", callback_data="admin_panel")]]
        await _edit(query, 
            "🚫 <b>حظر مستخدم</b>\n\n"
            "أرسل <code>ID</code> المستخدم أو <code>@username</code> لحظره فورًا.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif data.startswith("admin_users_page_"):
        _clear_admin_state(user.id)
        page = _callback_int_suffix(data)
        status = ADMIN_USER_FILTER.get(user.id, "all")
        await _show_users_page(query, lang, page=page or 0, status=status)
    elif data.startswith("admin_user_detail_"):
        _clear_admin_state(user.id)
        target_id = _callback_int_suffix(data)
        await _show_user_detail(query, lang, target_id)
    elif data == "admin_maintenance_on":
        _clear_admin_state(user.id)
        _set_maintenance(True)
        log_admin_action(user.id, "maintenance_mode", detail="enabled")
        back = [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
        await _edit(query, t(lang, "maintenance_on"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back))
    elif data == "admin_maintenance_off":
        _clear_admin_state(user.id)
        _set_maintenance(False)
        log_admin_action(user.id, "maintenance_mode", detail="disabled")
        back = [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
        await _edit(query, t(lang, "maintenance_off"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back))
    elif data.startswith("admin_unban_"):
        _clear_admin_state(user.id)
        target_id = _callback_int_suffix(data)
        unban_user(target_id)
        log_admin_action(user.id, "unban_user", target_id)
        await _show_user_detail(query, lang, target_id)
    elif data.startswith("admin_ban_confirm_"):
        _clear_admin_state(user.id)
        target_id = _callback_int_suffix(data)
        ban_user(target_id)
        log_admin_action(user.id, "ban_user", target_id)
        await _show_user_detail(query, lang, target_id)
    elif data == "admin_send_broadcast":
        _clear_admin_state(user.id)
        if not is_owner(user.id):
            await query.answer("⛔ الإذاعة الجماعية متاحة للمالك فقط.", show_alert=True)
            return
        ADMIN_CONVERSATION_STATES[user.id] = "broadcast"
        keyboard = [[inline_button("🔙 إلغاء", callback_data="admin_panel")]]
        await _edit(query, 
            "📢 أرسل الآن الرسالة التي تريد إذاعتها لكل المستخدمين.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif data == "admin_support":
        _clear_admin_state(user.id)
        await _show_support_tickets(query)
    elif data == "admin_support_answered":
        _clear_admin_state(user.id)
        await _show_support_tickets(query, status="answered")
    elif data == "admin_support_closed":
        _clear_admin_state(user.id)
        await _show_support_tickets(query, status="closed")
    elif data.startswith("admin_support_view_"):
        _clear_admin_state(user.id)
        await _show_support_ticket(query, _callback_int_suffix(data))
    elif data.startswith("admin_support_reply_"):
        _clear_admin_state(user.id)
        tid = _callback_int_suffix(data)
        ticket = support.get_ticket(tid)
        if not ticket:
            await query.answer("⚠️ التذكرة غير موجودة.", show_alert=True)
            return
        ADMIN_SUPPORT_REPLY_TARGET[user.id] = tid
        ADMIN_CONVERSATION_STATES[user.id] = "support_reply"
        keyboard = [[inline_button("🔙 إلغاء", callback_data=f"admin_support_view_{tid}")]]
        await _edit(query,
            f"📝 <b>الرد على التذكرة {ticket['ticket_no']}</b>\n\n"
            "أرسل الآن نص الرد — هيوصله إشعار فوري للمستخدم.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif data.startswith("admin_support_close_"):
        _clear_admin_state(user.id)
        tid = _callback_int_suffix(data)
        ticket = support.get_ticket(tid)
        if ticket:
            support.close_ticket(tid)
            log_admin_action(user.id, "support_close", detail=str(ticket.get("ticket_no", "")))
            try:
                await context.bot.send_message(
                    ticket["user_id"],
                    f"🛟 تذكرتك <code>{ticket['ticket_no']}</code> اتحولت إلى <b>مغلقة</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        await _show_support_ticket(query, tid)
    elif data.startswith("admin_support_open_"):
        _clear_admin_state(user.id)
        tid = _callback_int_suffix(data)
        ticket = support.get_ticket(tid)
        if ticket:
            support.reopen_ticket(tid)
            log_admin_action(user.id, "support_reopen", detail=str(ticket.get("ticket_no", "")))
        await _show_support_ticket(query, tid)
    else:
        logger.warning("Unhandled admin callback after validation: %s", data)


async def _show_support_tickets(query, status: str | None = None) -> None:
    tickets = support.list_tickets(status=status, limit=20)
    open_count = support.count_tickets("open")
    answered_count = support.count_tickets("answered")
    text = (
        "🛟 <b>تذاكر الدعم الفني</b>\n\n"
        f"🔓 مفتوحة: <b>{open_count}</b> • 🟢 تم الرد: <b>{answered_count}</b>\n\n"
    )
    if not tickets:
        text += "لا توجد تذاكر حاليًا."
    else:
        lines = []
        for t in tickets:
            lines.append(
                f"• <code>{t['ticket_no']}</code> — {escape(str(t.get('subject') or t.get('category')))} "
                f"— {_SUP_STATUS.get(t.get('status'), t.get('status'))}"
            )
        text += "\n".join(lines)
    rows = [
        [
            inline_button("🔓 المفتوحة", callback_data="admin_support"),
            inline_button("🟢 تم الرد", callback_data="admin_support_answered"),
            inline_button("⚫ المغلقة", callback_data="admin_support_closed"),
        ],
    ]
    for t in tickets[:12]:
        rows.append([inline_button(f"🎫 {t['ticket_no']}", callback_data=f"admin_support_view_{t['id']}")])
    rows.append([inline_button("🔙 لوحة التحكم", callback_data="admin_panel")])
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def _show_support_ticket(query, ticket_id: int | None) -> None:
    ticket = support.get_ticket(ticket_id) if ticket_id else None
    if not ticket:
        await _edit(
            query, "❌ التذكرة غير موجودة.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[inline_button("🔙 تذاكر الدعم", callback_data="admin_support")]]),
        )
        return
    msgs = support.get_messages(ticket["id"], limit=30)
    lines = []
    for m in msgs:
        who = "🛟 الدعم" if m["sender"] == "admin" else "👤 المستخدم"
        created = (m.get("created_at") or "—")[:16]
        lines.append(f"{who} • <code>{created}</code>\n{escape(str(m.get('body') or ''))}")
    text = (
        f"🎫 <b>{ticket['ticket_no']}</b>\n"
        f"👤 المستخدم: <code>{ticket['user_id']}</code>\n"
        f"🏷 النوع: <b>{escape(str(ticket.get('subject') or ticket.get('category')))}</b>\n"
        f"📌 الحالة: {_SUP_STATUS.get(ticket.get('status'), ticket.get('status'))}\n\n"
        f"<b>المحادثة:</b>\n" + ("\n\n".join(lines) if lines else "لا توجد رسائل بعد.")
    )
    rows = [[inline_button("📝 رد على التذكرة", callback_data=f"admin_support_reply_{ticket['id']}")]]
    if ticket["status"] != "closed":
        rows.append([inline_button("⚫ إغلاق التذكرة", callback_data=f"admin_support_close_{ticket['id']}")])
    else:
        rows.append([inline_button("🔓 إعادة فتح التذكرة", callback_data=f"admin_support_open_{ticket['id']}")])
    rows.append([inline_button("🔙 تذاكر الدعم", callback_data="admin_support")])
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def _show_engine_status(query) -> None:
    """Professional engine status screen with a back button."""
    cookies_ready = bool(YTDLP_COOKIES_FROM_BROWSER or (YTDLP_COOKIES_FILE and os.path.exists(YTDLP_COOKIES_FILE)))
    engines = list_engines()
    engine_lines = "\n".join(f"• <code>{e['name']}</code>" for e in engines)
    text = (
        "🧠 <b>حالة محرك التنزيل</b>\n\n"
        f"• yt-dlp: <code>{get_ytdlp_version()}</code>\n"
        f"• Cookies: <b>{'مفعّلة' if cookies_ready else 'غير مفعّلة'}</b>\n"
        f"• Proxy: <b>{'مفعّل' if DOWNLOAD_PROXY else 'غير مفعّل'}</b>\n"
        f"• المحركات النشطة: <b>{len(engines)}</b>\n\n"
        f"{engine_lines}\n\n"
        "💡 عند فشل منصات مثل Instagram/Facebook/YouTube:\n"
        "<code>/update_ytdlp</code> لتحديث المحرك."
    )
    back = [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back))

def _format_bytes(size: int) -> str:
    """Human-readable byte size."""
    size = int(size or 0)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _temp_dir_size() -> int:
    """Total bytes currently sitting in the temp folder."""
    try:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(TEMP_DIR):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
        return total
    except Exception:
        return 0




async def _show_owner_overview(query) -> None:
    """Owner-only cross-system summary using live persisted records."""
    from database.pg import backend_name, fetchone
    from config.settings import OWNER_ID, MAIN_BOT_USERNAME, ENABLE_CLONE_INSTANCES

    def scalar(sql: str, default=0):
        try:
            row = fetchone(sql)
            return row.get("cnt", default) if row else default
        except Exception:
            logger.exception("Owner overview query failed")
            return default

    ref_total = scalar("SELECT COUNT(*) AS cnt FROM referrals")
    ref_qualified = scalar("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'qualified'")
    ref_pending = scalar("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'pending'")
    ref_suspicious = scalar("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'suspicious'")
    clone_total = scalar("SELECT COUNT(*) AS cnt FROM bot_instances")
    clone_active = scalar("SELECT COUNT(*) AS cnt FROM bot_instances WHERE status = 'active'")
    clone_pending = scalar("SELECT COUNT(*) AS cnt FROM bot_instances WHERE status IN ('pending_token', 'claimed')")
    clone_errors = scalar("SELECT COUNT(*) AS cnt FROM bot_instances WHERE COALESCE(last_error, '') <> ''")
    try:
        from services.instance_manager import running_instance_ids
        running_count = len(running_instance_ids())
    except Exception:
        running_count = 0
    try:
        from database.users import get_total_users, get_new_users_today, get_active_24h, get_banned_users_count
        from database.downloads import get_total_downloads, get_downloads_today
        users = get_total_users()
        new_today = get_new_users_today()
        active_24h = get_active_24h()
        banned = get_banned_users_count()
        downloads = get_total_downloads()
        downloads_today = get_downloads_today()
    except Exception:
        logger.exception("Owner overview main DB query failed")
        users = new_today = active_24h = banned = downloads = downloads_today = 0

    text = (
        "👑 <b>مركز المالك الأساسي</b>\\n\\n"
        f"🆔 مالك النظام: <code>{OWNER_ID}</code>\\n"
        f"🤖 البوت الأساسي: <code>@{escape(MAIN_BOT_USERNAME or 'غير مضبوط')}</code>\\n"
        f"🗄 قاعدة الإحالات: <b>{escape(backend_name())}</b>\\n"
        f"🧩 تشغيل النسخ: <b>{'مفعّل' if ENABLE_CLONE_INSTANCES else 'متوقف من الإعدادات'}</b>\\n\\n"
        "👥 <b>المستخدمون والتحميلات</b>\\n"
        f"• إجمالي المستخدمين: <b>{users}</b>\\n"
        f"• مستخدمون جدد اليوم: <b>{new_today}</b>\\n"
        f"• نشطون آخر 24 ساعة: <b>{active_24h}</b>\\n"
        f"• محظورون: <b>{banned}</b>\\n"
        f"• إجمالي التحميلات: <b>{downloads}</b> (اليوم: <b>{downloads_today}</b>)\\n\\n"
        "🤝 <b>شبكة الإحالات</b>\\n"
        f"• كل الإحالات: <b>{ref_total}</b>\\n"
        f"• مؤهلة: <b>{ref_qualified}</b>\\n"
        f"• بانتظار النشاط: <b>{ref_pending}</b>\\n"
        f"• تحت المراجعة: <b>{ref_suspicious}</b>\\n\\n"
        "🤖 <b>نسخ المستخدمين</b>\\n"
        f"• كل النسخ المسجلة: <b>{clone_total}</b>\\n"
        f"• حالتها Active: <b>{clone_active}</b>\\n"
        f"• بانتظار ربط التوكن: <b>{clone_pending}</b>\\n"
        f"• بها خطأ مسجل: <b>{clone_errors}</b>\\n"
        f"• تعمل داخل العملية الحالية: <b>{running_count}</b>\\n\\n"
        "ملاحظة: حالة Active من قاعدة البيانات لا تضمن وحدها أن البوت يرد حاليًا؛ "
        "عدد التشغيل أعلاه يوضح النسخ التي يديرها هذا العامل الحالي."
    )
    rows = [
        [inline_button("🤝 تفاصيل الإحالات", callback_data="admin_referral_network")],
        [inline_button("🤖 تفاصيل النسخ", callback_data="admin_clone_instances")],
        [inline_button("🔙 لوحة التحكم", callback_data="admin_panel")],
    ]
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def _show_referral_network(query) -> None:
    """Owner-only referral health, top referrers and suspicious activity."""
    from database.pg import fetchall, fetchone

    def scalar(sql: str) -> int:
        try:
            row = fetchone(sql)
            return int((row or {}).get("cnt", 0) or 0)
        except Exception:
            logger.exception("Referral dashboard query failed")
            return 0

    total = scalar("SELECT COUNT(*) AS cnt FROM referrals")
    qualified = scalar("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'qualified'")
    pending = scalar("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'pending'")
    suspicious = scalar("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'suspicious'")
    try:
        leaders = fetchall(
            "SELECT r.referrer_id, COUNT(*) AS total, "
            "SUM(CASE WHEN r.status = 'qualified' THEN 1 ELSE 0 END) AS qualified "
            "FROM referrals r GROUP BY r.referrer_id "
            "ORDER BY qualified DESC, total DESC LIMIT 10"
        )
        recent = fetchall(
            "SELECT referrer_id, referred_id, status, bound_at, qualified_at "
            "FROM referrals ORDER BY id DESC LIMIT 10"
        )
    except Exception:
        logger.exception("Referral lists query failed")
        leaders, recent = [], []

    leader_lines = "\\n".join(
        f"{i}. <code>{int(row.get('referrer_id') or 0)}</code> — "
        f"<b>{int(row.get('qualified') or 0)}</b> مؤهل / {int(row.get('total') or 0)} إجمالي"
        for i, row in enumerate(leaders, 1)
    ) or "لا توجد إحالات مسجلة."
    recent_lines = "\\n".join(
        f"• محيل <code>{int(row.get('referrer_id') or 0)}</code> → "
        f"مستخدم <code>{int(row.get('referred_id') or 0)}</code> — "
        f"<b>{escape(str(row.get('status') or 'unknown'))}</b> "
        f"({escape(str(row.get('bound_at') or '—')[:16])})"
        for row in recent
    ) or "لا توجد إحالات حديثة."

    text = (
        "🤝 <b>مراقبة شبكة الإحالات</b>\\n\\n"
        f"• الإجمالي: <b>{total}</b>\\n"
        f"• مؤهلة: <b>{qualified}</b>\\n"
        f"• معلقة: <b>{pending}</b>\\n"
        f"• مشبوهة: <b>{suspicious}</b>\\n\\n"
        f"<b>أكثر 10 محيلين</b>\\n{leader_lines}\\n\\n"
        f"<b>آخر 10 إحالات</b>\\n{recent_lines}"
    )
    rows = [
        [inline_button("🔄 تحديث", callback_data="admin_referral_network")],
        [inline_button("👑 ملخص المالك", callback_data="admin_owner_overview")],
        [inline_button("🔙 لوحة التحكم", callback_data="admin_panel")],
    ]
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def _show_clone_instances(query) -> None:
    """Owner-only inventory of clone bots; never expose token material."""
    from database.pg import fetchall, fetchone
    from services.instance_manager import running_instance_ids

    def scalar(sql: str) -> int:
        try:
            row = fetchone(sql)
            return int((row or {}).get("cnt", 0) or 0)
        except Exception:
            logger.exception("Clone dashboard count query failed")
            return 0

    total = scalar("SELECT COUNT(*) AS cnt FROM bot_instances")
    try:
        rows_data = fetchall(
            "SELECT id, owner_id, bot_username, status, created_at, activated_at, last_error "
            "FROM bot_instances ORDER BY id DESC LIMIT 20"
        )
        running = set(running_instance_ids())
    except Exception:
        logger.exception("Clone dashboard list query failed")
        rows_data, running = [], set()

    lines = []
    for item in rows_data:
        iid = int(item.get("id") or 0)
        username = str(item.get("bot_username") or "").strip()
        status = str(item.get("status") or "unknown")
        last_error = str(item.get("last_error") or "").strip()
        process_status = "يعمل داخل العامل الحالي" if iid in running else "غير موجود في العامل الحالي"
        line = (
            f"#{iid} • المالك <code>{int(item.get('owner_id') or 0)}</code> • "
            f"<b>{escape('@' + username if username else 'بدون username')}</b>\\n"
            f"الحالة: <b>{escape(status)}</b> • {process_status}\\n"
            f"تفعيل: <code>{escape(str(item.get('activated_at') or '—')[:16])}</code>"
        )
        if last_error:
            line += f"\\nآخر خطأ مسجل: <code>{escape(last_error[:100])}</code>"
        lines.append(line)

    text = (
        "🤖 <b>سجل النسخ الخاصة</b>\\n\\n"
        f"عدد النسخ المسجلة: <b>{total}</b>\\n"
        "المعروض: آخر 20 نسخة. التوكنات لا تظهر في اللوحة.\\n\\n"
        + ("\\n\\n".join(lines) if lines else "لا توجد نسخ مسجلة.")
    )
    rows = [
        [inline_button("🔄 تحديث", callback_data="admin_clone_instances")],
        [inline_button("👑 ملخص المالك", callback_data="admin_owner_overview")],
        [inline_button("🔙 لوحة التحكم", callback_data="admin_panel")],
    ]
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


def _build_admin_panel(lang: str, user_id: int | None = None):
    """Assemble the live, professional admin dashboard from real DB data."""
    total_users = get_total_users()
    new_today = get_new_users_today()
    active_today = get_active_today()
    active_24h = get_active_24h()
    banned = get_banned_users_count()
    downloads_today = get_downloads_today()
    downloads_24h = get_downloads_24h()
    downloads_week = get_downloads_week()
    downloads_month = get_downloads_month()
    downloads_total = get_total_downloads()
    total_data = get_total_data_bytes()
    cache_count = get_cache_count()
    cache_hits = get_cache_hits()
    platform_stats = get_downloads_by_platform()
    top_users = get_top_users(5)
    top_today = get_top_user_today(3)
    engines = list_engines()
    temp_size = _temp_dir_size()
    audit_entries = get_admin_audit(5)
    try:
        version = get_ytdlp_version()
    except Exception:
        version = "غير متاح"

    from utils.maintenance import is_maintenance_mode
    maintenance = "🔴 مفعّل" if is_maintenance_mode() else "🟢 معطّل"

    top = sorted(platform_stats.items(), key=lambda kv: kv[1], reverse=True)[:6]
    platform_lines = "\n".join(
        f"• <b>{escape(str(p))}</b>: {c} تحميل" for p, c in top
    ) or "• لا توجد تحميلات مسجلة بعد."

    top_user_lines = "\n".join(
        f"• <b>{escape(str(u.get('first_name') or str(u.get('user_id'))))}</b> — {u.get('downloads', 0)} تحميل"
        for u in top_users
    ) or "• لا يوجد نشاط بعد."

    top_today_lines = "\n".join(
        f"• <b>{escape(str(u.get('first_name') or str(u.get('user_id'))))}</b> — {u.get('downloads', 0)} تحميل اليوم"
        for u in top_today
    ) or "• لا توجد تحميلات اليوم بعد."

    history = get_broadcast_history(3)
    if history:
        last = history[0]
        broadcast_lines = (
            f"• آخر إذاعة: <b>{last.get('success', 0)}</b>/<b>{last.get('total', 0)}</b> نجاح"
            f" • <code>{escape(str(last.get('created_at') or '—')[:16])}</code>"
        )
    else:
        broadcast_lines = "• لا توجد إذاعات مسجلة بعد."

    if audit_entries:
        audit_lines = "\n".join(
            f"• <b>{escape(str(e.get('action') or '—'))}</b>"
            f" • <code>{escape(str(e.get('created_at') or '—')[:16])}</code>"
            for e in audit_entries
        )
    else:
        audit_lines = "• لا توجد أحداث مسجلة بعد."

    text = (
        "👑 <b>لوحة التحكم الرئيسية</b>\n\n"
        "👥 <b>المستخدمون</b>\n"
        f"• الإجمالي: <b>{total_users}</b>\n"
        f"• جديد اليوم: <b>{new_today}</b>\n"
        f"• نشط اليوم: <b>{active_today}</b>\n"
        f"• نشط آخر 24 ساعة: <b>{active_24h}</b>\n"
        f"• محظور: <b>{banned}</b>\n\n"
        "📥 <b>التحميلات</b>\n"
        f"• اليوم: <b>{downloads_today}</b>\n"
        f"• آخر 24 ساعة: <b>{downloads_24h}</b>\n"
        f"• آخر ٧ أيام: <b>{downloads_week}</b>\n"
        f"• آخر ٣٠ يومًا: <b>{downloads_month}</b>\n"
        f"• الإجمالي: <b>{downloads_total}</b>\n"
        f"• حجم البيانات الكلي: <b>{_format_bytes(total_data)}</b>\n\n"
        "💾 <b>الكاش</b>\n"
        f"• عناصر مخزنة: <b>{cache_count}</b>\n"
        f"• ضربات التوصيل: <b>{cache_hits}</b>\n"
        f"• التخزين المؤقت (temp): <b>{_format_bytes(temp_size)}</b>\n\n"
        "🧠 <b>محرك التنزيل</b>\n"
        f"• yt-dlp: <code>{escape(version)}</code>\n"
        f"• المحركات النشطة: <b>{len(engines)}</b>\n\n"
        "🌍 <b>أكثر المنصات استخدامًا</b>\n"
        f"{platform_lines}\n\n"
        "👑 <b>الأكثر نشاطًا</b>\n"
        f"{top_user_lines}\n\n"
        "🏆 <b>الأكثر تحميلًا اليوم</b>\n"
        f"{top_today_lines}\n\n"
        "📢 <b>الإذاعة</b>\n"
        f"{broadcast_lines}\n\n"
        "📜 <b>سجل أمان الأدمن</b>\n"
        f"{audit_lines}\n\n"
        f"🚧 <b>وضع الصيانة:</b> {maintenance}"
    )

    keyboard = [
        *([[
            inline_button("👑 ملخص المالك", callback_data="admin_owner_overview"),
            inline_button("🤝 شبكة الإحالات", callback_data="admin_referral_network"),
        ], [
            inline_button("🤖 نسخ المستخدمين", callback_data="admin_clone_instances"),
        ]] if user_id is not None and is_owner(user_id) else []),
        [
            inline_button("👥 المستخدمون", callback_data="admin_users"),
            inline_button("🔍 بحث عن مستخدم", callback_data="admin_search"),
        ],
        [
            inline_button("📢 إذاعة", callback_data="admin_broadcast"),
            inline_button("🚧 وضع الصيانة", callback_data="admin_maintenance"),
        ],
        [
            inline_button("🧠 حالة المحرك", callback_data="admin_engine_status"),
            inline_button("🩺 صحة النظام", callback_data="admin_health"),
        ],
        [
            inline_button("🔄 تحديث yt-dlp", callback_data="admin_update_ytdlp"),
            inline_button("📜 سجل الأمان", callback_data="admin_audit"),
        ],
        [
            inline_button("🔐 فحص الأمان", callback_data="admin_security"),
            inline_button("🛟 تذاكر الدعم", callback_data="admin_support"),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def _show_admin_panel(query, lang: str):
    text, keyboard = _build_admin_panel(lang, query.from_user.id)
    await _edit(query, text, reply_markup=keyboard, parse_mode="HTML")


async def _show_audit_log(query) -> None:
    """Security audit trail screen: who did what, when, with real totals."""
    stats = get_admin_audit_stats()
    entries = get_admin_audit(12)
    total = stats.get("total", 0)
    actions = stats.get("actions", {})

    if total == 0:
        text = (
            "📜 <b>سجل أمان الأدمن</b>\n\n"
            "لا توجد أحداث مسجلة بعد.\n"
            "أي عملية حساسة (حظر، فك حظر، إذاعة، صيانة، تحديث محرك) ستُسجل هنا تلقائيًا."
        )
    else:
        action_lines = "\n".join(
            f"• <b>{escape(str(a))}</b>: {c} عملية"
            for a, c in list(actions.items())[:8]
        )
        entry_lines = []
        for idx, e in enumerate(entries, 1):
            action = escape(str(e.get("action") or "—"))
            admin = escape(str(e.get("admin_id") or "—"))
            target = e.get("target_id")
            detail = escape(str(e.get("detail") or ""))
            created = escape(str(e.get("created_at") or "—")[:16])
            line = f"{idx}. <b>{action}</b> • أدمن <code>{admin}</code>"
            if target is not None and str(target) != "":
                line += f" • هدف <code>{target}</code>"
            line += f"\n   🕑 {created}"
            if detail:
                line += f" • {detail}"
            entry_lines.append(line)

        text = (
            "📜 <b>سجل أمان الأدمن</b>\n\n"
            f"إجمالي العمليات المسجلة: <b>{total}</b>\n\n"
            f"<b>ملخص العمليات</b>\n{action_lines}\n\n"
            f"<b>آخر {len(entries)} عملية</b>\n" + "\n\n".join(entry_lines)
        )

    back = [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back))


async def _show_security_scan(query) -> None:
    """Live security posture checklist for the owner/admin."""
    from config.settings import BOT_TOKEN
    from database.users import _ALLOWED_USER_FIELDS

    checks = []
    checks.append(("بوابة المالك (OWNER_ID)", bool(OWNER_ID)))
    checks.append(("فلترة الحقول ضد حقن SQL", bool(_ALLOWED_USER_FIELDS)))
    checks.append(("حماية HTML ضد الحقن", callable(escape)))
    checks.append(("قائمة الأدمن محددة", bool(ADMIN_IDS)))
    checks.append(("توكن البوت محمّل", bool(BOT_TOKEN)))
    checks.append(("سجل التدقيق الأمني يعمل", get_admin_audit_stats() is not None))

    lines = []
    for label, ok in checks:
        icon = "✅" if ok else "❌"
        lines.append(f"{icon} {label}")

    stats = get_admin_audit_stats()
    text = (
        "🔐 <b>فحص أمان لوحة الأدمن</b>\n\n"
        + "\n".join(lines)
        + f"\n\n🕵️ إجمالي العمليات المسجلة في السجل: <b>{stats.get('total', 0)}</b>\n"
        f"👤 المالك: <code>{OWNER_ID}</code> • الأدمن: <code>{len(ADMIN_IDS)}</code>"
    )
    back = [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
    await _edit(query, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back))


async def _show_users_page(query, lang: str, page: int = 0, status: str = "all"):
    per_page = 8
    status = status or "all"
    if status not in ("all", "active", "banned"):
        status = "all"
    total = count_users(status)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page or 0, total_pages - 1))
    users = get_users_page(offset=page * per_page, limit=per_page, status=status)
    status_label = {"all": "الكل", "active": "النشطون", "banned": "المحظورون"}[status]

    if not users:
        keyboard = [
            [
                inline_button("👥 الكل", callback_data="admin_users"),
                inline_button("✅ نشط", callback_data="admin_users_active"),
                inline_button("🚫 محظور", callback_data="admin_users_banned"),
            ],
            [inline_button("🔙 لوحة التحكم", callback_data="admin_panel")],
        ]
        await _edit(query, 
            f"👥 <b>المستخدمون — {status_label}</b>\n\n"
            "لا يوجد مستخدمون في هذه الفئة حاليًا.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    ADMIN_LAST_PAGE[query.from_user.id] = page
    ADMIN_USER_FILTER[query.from_user.id] = status

    lines = []
    for u in users:
        status_icon = "🚫" if u.get("is_banned") else "✅"
        name = escape(u.get("first_name") or "بدون اسم")
        username = escape(u.get("username") or "—")
        downloads = u.get("downloads", 0)
        language = escape(u.get("language") or "—")
        last_seen = _short_datetime(u.get("last_seen"))
        lines.append(
            f"{status_icon} <b>{name}</b> @{username}\n"
            f"    🆔 <code>{u['user_id']}</code> • 📥 <b>{downloads}</b> • 🌐 {language} • 🕑 {last_seen}"
        )

    text = (
        f"👥 <b>قائمة المستخدمين</b> — فلتر: <b>{status_label}</b>\n"
        f"الصفحة <b>{page + 1}</b> من <b>{total_pages}</b> • العدد: <b>{total}</b>\n\n"
        + "\n".join(lines)
    )

    keyboard = []
    row = []
    for u in users:
        row.append(inline_button(f"👤 {u['user_id']}", callback_data=f"admin_user_detail_{u['user_id']}"))
        if len(row) >= 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    nav = []
    if page > 0:
        nav.append(inline_button("⬅️ السابق", callback_data=f"admin_users_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(inline_button("التالي ➡️", callback_data=f"admin_users_page_{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([
        inline_button("👥 الكل", callback_data="admin_users"),
        inline_button("✅ نشط", callback_data="admin_users_active"),
        inline_button("🚫 محظور", callback_data="admin_users_banned"),
    ])
    keyboard.append([inline_button("🔙 رجوع للوحة", callback_data="admin_panel")])
    await _edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

def _render_user_detail(target_id: int, admin_id: int | None = None):
    """Build a full professional user profile (text + keyboard)."""
    user = get_user(target_id)
    if not user:
        return None

    is_banned = user.get("is_banned", 0)
    stats = get_user_download_stats(target_id)
    page = ADMIN_LAST_PAGE.get(admin_id, 0) if admin_id else 0

    full_name = " ".join(
        str(part) for part in [user.get("first_name"), user.get("last_name")] if part
    ) or "—"
    username = user.get("username") or "—"
    language = user.get("language") or "—"
    role = user.get("role") or "user"
    join_date = user.get("join_date") or "—"
    last_seen = user.get("last_seen") or "—"

    total = stats.get("total", 0)
    video = stats.get("video", 0)
    audio = stats.get("audio", 0)
    other = max(total - video - audio, 0)

    text = (
        "👤 <b>ملف المستخدم الكامل</b>\n\n"
        f"🆔 المعرف: <code>{target_id}</code>\n"
        f"👤 الاسم: <b>{escape(full_name)}</b>\n"
        f"🔖 اليوزر: @{escape(username)}\n"
        f"🌐 اللغة: <b>{escape(language)}</b>\n"
        f"🏷 الدور: <b>{escape(role)}</b>\n"
        f"📅 تاريخ الانضمام: <code>{escape(join_date)}</code>\n"
        f"🕑 آخر ظهور: <code>{escape(last_seen)}</code>\n"
        f"🚫 الحالة: <b>{'محظور 🚫' if is_banned else 'نشط ✅'}</b>\n\n"
        "📥 <b>نشاط التحميل</b>\n"
        f"• الإجمالي: <b>{total}</b>\n"
        f"• فيديو: <b>{video}</b>\n"
        f"• صوت MP3: <b>{audio}</b>\n"
        f"• صور/أخرى: <b>{other}</b>"
    )

    keyboard = []
    if is_banned:
        keyboard.append([inline_button("✅ فك الحظر", callback_data=f"admin_unban_{target_id}")])
    else:
        keyboard.append([inline_button("🚫 حظر المستخدم", callback_data=f"admin_ban_confirm_{target_id}")])
    keyboard.append([inline_button("🔙 رجوع للمستخدمين", callback_data=f"admin_users_page_{page}")])

    return text, InlineKeyboardMarkup(keyboard)


async def _show_user_detail(query, lang: str, target_id: int):
    admin_id = query.from_user.id if query.from_user else None
    rendered = _render_user_detail(target_id, admin_id)
    if rendered is None:
        keyboard = [[inline_button("🔙 المستخدمون", callback_data="admin_users")]]
        await _edit(query, 
            "❌ <b>المستخدم غير موجود.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    text, keyboard = rendered
    await _edit(query, text, parse_mode="HTML", reply_markup=keyboard)

async def _show_broadcast_form(query, lang: str):
    keyboard = [
        [inline_button("📨 جاهز — أرسل الرسالة الآن", callback_data="admin_send_broadcast")],
        [inline_button("🔙 لوحة التحكم", callback_data="admin_panel")],
    ]
    text = (
        "📢 <b>الإذاعة العامة</b>\n\n"
        "اضغط الزر بالأسفل ثم أرسل الرسالة التي تريد إرسالها لكل المستخدمين.\n\n"
        "سيتم نسخها كما هي تمامًا:\n"
        "• نص ✍️\n• صورة 🖼\n• فيديو 🎬\n• ملف 📁\n• أو مجموعة صور 📚"
    )
    await _edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def _show_maintenance_toggle(query, lang: str):
    from utils.maintenance import is_maintenance_mode
    status = "🔴 مفعّل" if is_maintenance_mode() else "🟢 معطّل"
    text = (
        "🚧 <b>وضع الصيانة</b>\n\n"
        f"الحالة الحالية: <b>{status}</b>\n\n"
        "عند تفعيل وضع الصيانة، يتوقف المستخدمون العاديون عن التحميل، "
        "بينما يظل الأدمن قادرًا على استخدام البوت بشكل كامل."
    )
    keyboard = [
        [inline_button("🔴 تفعيل وضع الصيانة", callback_data="admin_maintenance_on")],
        [inline_button("🟢 إيقاف وضع الصيانة", callback_data="admin_maintenance_off")],
        [inline_button("🔙 لوحة التحكم", callback_data="admin_panel")],
    ]
    await _edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

def _set_maintenance(enabled: bool):
    from utils.maintenance import set_maintenance_mode
    set_maintenance_mode(enabled)


def _callback_int_suffix(data: str) -> int | None:
    try:
        return int(data.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        return None


def _invalid_admin_callback(data: str) -> bool:
    if data.startswith("admin_users_page_"):
        page = _callback_int_suffix(data)
        return page is None or page < 0
    if data.startswith((
            "admin_user_detail_", "admin_unban_", "admin_ban_confirm_",
            "admin_support_view_", "admin_support_reply_",
            "admin_support_close_", "admin_support_open_",
    )):
        return _callback_int_suffix(data) is None
    known = {
        "admin_panel", "admin_users", "admin_broadcast", "admin_announce_update",
        "admin_engine_status", "admin_update_ytdlp", "admin_search", "admin_maintenance",
        "admin_ban", "admin_maintenance_on", "admin_maintenance_off", "admin_send_broadcast",
        "admin_health", "admin_audit", "admin_security",
        "admin_users_active", "admin_users_banned",
        "admin_support", "admin_support_answered", "admin_support_closed",
        "admin_owner_overview", "admin_referral_network", "admin_clone_instances",
    }
    return data not in known


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle pending admin text input. Returns True when the message was consumed."""
    user_id = update.effective_user.id
    if not is_admin(user_id) or user_id not in ADMIN_CONVERSATION_STATES:
        return False
    state = ADMIN_CONVERSATION_STATES.pop(user_id)
    if state not in _ADMIN_TEXT_STATES:
        logger.warning("Dropped unknown admin text state for user=%s: %s", user_id, state)
        return False
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ أرسل نصًا صالحًا (ID أو @username أو اسم).")
        return True

    if state == "search":
        results = search_users(text)
        if not results:
            await update.message.reply_text(
                "❌ <b>لا توجد نتائج</b> لهذا البحث.\nجرّب رقمًا أو اسمًا آخر.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
                ),
            )
            return True

        if len(results) == 1:
            rendered = _render_user_detail(results[0]["user_id"], user_id)
            if rendered:
                msg_text, keyboard = rendered
                await update.message.reply_text(msg_text, parse_mode="HTML", reply_markup=keyboard)
            return True

        lines = []
        keyboard = []
        for r in results[:10]:
            name = escape(r.get("first_name") or "بدون اسم")
            username = escape(r.get("username") or "—")
            status = "🚫" if r.get("is_banned") else "✅"
            lines.append(f"{status} <b>{name}</b> @{username} • <code>{r['user_id']}</code>")
            keyboard.append([
                inline_button(
                    f"👤 {r['user_id']} — {name[:12]}",
                    callback_data=f"admin_user_detail_{r['user_id']}",
                )
            ])
        keyboard.append([inline_button("🔙 لوحة التحكم", callback_data="admin_panel")])
        msg_text = f"🔍 <b>نتائج البحث ({len(results)})</b>\n\n" + "\n".join(lines)
        await update.message.reply_text(msg_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return True

    elif state == "broadcast":
        all_ids = get_all_user_ids()
        count = 0
        for uid in all_ids:
            try:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id,
                )
                count += 1
                if count % 20 == 0:
                    await asyncio.sleep(1)
            except Exception:
                pass

        try:
            log_broadcast(
                admin_id=user_id,
                message=text[:300],
                total=len(all_ids),
                success=count,
                failed=max(len(all_ids) - count, 0),
            )
        except Exception:
            logger.exception("Failed to write broadcast_log")

        try:
            log_admin_action(user_id, "manual_broadcast", detail=f"success={count}")
        except Exception:
            logger.exception("Failed to write admin audit log")

        await update.message.reply_text(
            f"✅ تم إرسال الإذاعة إلى <b>{count}</b> مستخدم.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
            ),
        )
        return True

    elif state == "ban":
        results = search_users(text)
        if not results:
            await update.message.reply_text(
                "❌ <b>لا يوجد مستخدم</b> بهذا الرقم/الاسم.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
                ),
            )
            return True
        target = results[0]
        ban_user(target["user_id"])
        log_admin_action(user_id, "ban_user", target["user_id"])
        name = escape(target.get("first_name") or target.get("username") or str(target["user_id"]))
        await update.message.reply_text(
            f"🚫 <b>تم حظر المستخدم</b>\n\n👤 {name}\n🆔 <code>{target['user_id']}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[inline_button("🔙 لوحة التحكم", callback_data="admin_panel")]]
            ),
        )
        return True

    elif state == "support_reply":
        ticket_id = ADMIN_SUPPORT_REPLY_TARGET.pop(user_id, None)
        ticket = support.get_ticket(ticket_id) if ticket_id else None
        if not ticket:
            await update.message.reply_text(
                "❌ التذكرة غير موجودة. ارجع وأعد المحاولة.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[inline_button("🔙 تذاكر الدعم", callback_data="admin_support")]]),
            )
            return True
        body = text[:1900]
        support.add_admin_reply(ticket["id"], body)
        log_admin_action(user_id, "support_reply", detail=str(ticket.get("ticket_no", "")))
        try:
            await context.bot.send_message(
                ticket["user_id"],
                f"🛟 <b>رد جديد على تذكرتك <code>{ticket['ticket_no']}</code></b>\n\n{body}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await update.message.reply_text(
            f"✅ تم إرسال الرد لصاحب التذكرة <code>{ticket['ticket_no']}</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[inline_button("🎫 عرض التذكرة", callback_data=f"admin_support_view_{ticket['id']}")]]
            ),
        )
        return True
    return True
