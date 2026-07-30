import os
import time
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from middlewares.auth import is_admin
from locales import t
from database.users import (
    get_user, get_total_users, get_new_users_today, get_active_today,
    get_users_page, search_users, get_all_user_ids, ban_user, unban_user,
    update_user,
)
from database.downloads import (
    get_downloads_today, get_downloads_week, get_downloads_month,
    get_downloads_by_platform, get_total_downloads, get_user_download_stats,
)
from database.cache import get_cache_count, get_cache_hits

logger = logging.getLogger(__name__)

ADMIN_CONVERSATION_STATES = {}

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    keyboard = [
        [
            InlineKeyboardButton("📊 " + t(lang, "admin_panel_btn"), callback_data="admin_panel"),
            InlineKeyboardButton("👥 " + t(lang, "admin_users_btn"), callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("📢 " + t(lang, "admin_broadcast_btn"), callback_data="admin_broadcast"),
            InlineKeyboardButton("🔍 " + t(lang, "admin_search_btn"), callback_data="admin_search"),
        ],
        [
            InlineKeyboardButton("🚫 " + t(lang, "admin_ban_btn"), callback_data="admin_ban"),
            InlineKeyboardButton("🚧 " + t(lang, "admin_maintenance_btn"), callback_data="admin_maintenance"),
        ],
    ]

    await update.message.reply_text(
        t(lang, "admin_welcome"),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not is_admin(user.id):
        await query.answer("⛔ هذا الأمر متاح للمسؤولين فقط.", show_alert=True)
        return
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    data = query.data
    await query.answer()

    if data == "admin_panel":
        await _show_admin_panel(query, lang)
    elif data == "admin_users":
        await _show_users_page(query, lang, page=0)
    elif data == "admin_broadcast":
        await _show_broadcast_form(query, lang)
    elif data == "admin_search":
        ADMIN_CONVERSATION_STATES[user.id] = "search"
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]]
        await query.edit_message_text(t(lang, "admin_search_prompt"), reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "admin_maintenance":
        await _show_maintenance_toggle(query, lang)
    elif data == "admin_ban":
        ADMIN_CONVERSATION_STATES[user.id] = "ban"
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]]
        await query.edit_message_text(t(lang, "admin_ban_prompt"), reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("admin_users_page_"):
        page = int(data.split("_")[-1])
        await _show_users_page(query, lang, page=page)
    elif data.startswith("admin_user_detail_"):
        target_id = int(data.split("_")[-1])
        await _show_user_detail(query, lang, target_id)
    elif data == "admin_maintenance_on":
        _set_maintenance(True)
        await query.edit_message_text(t(lang, "maintenance_on"))
    elif data == "admin_maintenance_off":
        _set_maintenance(False)
        await query.edit_message_text(t(lang, "maintenance_off"))
    elif data.startswith("admin_unban_"):
        target_id = int(data.split("_")[-1])
        unban_user(target_id)
        await query.answer(t(lang, "admin_unbanned"))
        await _show_user_detail(query, lang, target_id)
    elif data.startswith("admin_ban_confirm_"):
        target_id = int(data.split("_")[-1])
        ban_user(target_id)
        await query.answer(t(lang, "admin_banned"))
        await _show_user_detail(query, lang, target_id)
    elif data == "admin_send_broadcast":
        ADMIN_CONVERSATION_STATES[user.id] = "broadcast"
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")]]
        await query.edit_message_text(t(lang, "admin_broadcast_message_prompt"), reply_markup=InlineKeyboardMarkup(keyboard))

async def _show_admin_panel(query, lang: str):
    total_users = get_total_users()
    active_today = get_active_today()
    downloads_today = get_downloads_today()
    platform_stats = get_downloads_by_platform()
    cache_hits = get_cache_hits()
    
    keyboard = [
        [
            InlineKeyboardButton(t(lang, "admin_users_btn"), callback_data="admin_users"),
            InlineKeyboardButton(t(lang, "admin_broadcast_btn"), callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_search_btn"), callback_data="admin_search"),
            InlineKeyboardButton(t(lang, "admin_maintenance_btn"), callback_data="admin_maintenance"),
        ],
    ]

    text = (
        f"👑 <b>لوحة التحكم</b>\n\n"
        f"👥 إجمالي المستخدمين: <b>{total_users}</b>\n"
        f"🟢 نشط اليوم: <b>{active_today}</b>\n"
        f"📥 تحميلات اليوم: <b>{downloads_today}</b>\n"
        f"💾 ضربات الكاش: <b>{cache_hits}</b>\n\n"
        f"🎬 يوتيوب: <b>{platform_stats.get('YouTube', 0)}</b>\n"
        f"🎵 تيك توك: <b>{platform_stats.get('TikTok', 0)}</b>\n"
        f"📌 بينترست: <b>{platform_stats.get('Pinterest', 0)}</b>"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def _show_users_page(query, lang: str, page: int = 0):
    per_page = 10
    users = get_users_page(offset=page * per_page, limit=per_page)
    if not users:
        await query.edit_message_text(t(lang, "admin_no_users"))
        return
    lines = []
    for u in users:
        status = "🚫" if u.get("is_banned") else "✅"
        lines.append(f"{status} <code>{u['user_id']}</code> — {u.get('first_name', '?')} (@{u.get('username', '?')})")
    text = t(lang, "admin_users_title") + "\n\n" + "\n".join(lines)
    keyboard = []
    row = []
    for u in users:
        row.append(InlineKeyboardButton(f"👤 {u['user_id']}", callback_data=f"admin_user_detail_{u['user_id']}"))
        if len(row) >= 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton("◀️", callback_data=f"admin_users_page_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}", callback_data="noop"))
    nav_row.append(InlineKeyboardButton("▶️", callback_data=f"admin_users_page_{page + 1}"))
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def _show_user_detail(query, lang: str, target_id: int):
    user = get_user(target_id)
    if not user:
        await query.edit_message_text(t(lang, "admin_user_not_found"))
        return
    is_banned = user.get("is_banned", 0)
    text = (
        f"👤 <b>تفاصيل المستخدم</b>\n\n"
        f"🆔 المعرف: <code>{target_id}</code>\n"
        f"👤 الاسم: <b>{user.get('first_name', '?')}</b>\n"
        f"🔖 اليوزر: @{user.get('username', '-')}\n"
        f"📥 التحميلات: <b>{user.get('downloads', 0)}</b>\n"
        f"🚫 محظور: <b>{'نعم' if is_banned else 'لا'}</b>\n"
        f"📅 انضم في: {user.get('join_date', '?')}"
    )
    keyboard = []
    if is_banned:
        keyboard.append([InlineKeyboardButton("✅ فك الحظر", callback_data=f"admin_unban_{target_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🚫 حظر", callback_data=f"admin_ban_confirm_{target_id}")])
    keyboard.append([InlineKeyboardButton("🔙 المستخدمون", callback_data="admin_users")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def _show_broadcast_form(query, lang: str):
    keyboard = [
        [InlineKeyboardButton(t(lang, "admin_broadcast_send"), callback_data="admin_send_broadcast")],
        [InlineKeyboardButton("🔙", callback_data="admin_panel")],
    ]
    await query.edit_message_text(t(lang, "admin_broadcast_intro"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def _show_maintenance_toggle(query, lang: str):
    from utils.maintenance import is_maintenance_mode
    status = "🔴 مفعل" if is_maintenance_mode() else "🟢 معطل"
    text = f"🚧 <b>وضع الصيانة</b>\n\nالحالة الحالية: <b>{status}</b>"
    keyboard = [
        [InlineKeyboardButton("🔴 تفعيل", callback_data="admin_maintenance_on"),
         InlineKeyboardButton("🟢 تعطيل", callback_data="admin_maintenance_off")],
        [InlineKeyboardButton("🔙", callback_data="admin_panel")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

def _set_maintenance(enabled: bool):
    from utils.maintenance import set_maintenance_mode
    set_maintenance_mode(enabled)

async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or user_id not in ADMIN_CONVERSATION_STATES:
        return
    state = ADMIN_CONVERSATION_STATES.pop(user_id)
    text = update.message.text.strip()
    if state == "search":
        results = search_users(text)
        if not results:
            await update.message.reply_text("❌ لم يتم العثور على مستخدمين.")
            return
        await update.message.reply_text(f"🔍 تم العثور على {len(results)} مستخدمين. استخدم /admin لعرضهم.")
    elif state == "broadcast":
        all_ids = get_all_user_ids()
        count = 0
        for uid in all_ids:
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
                count += 1
                if count % 20 == 0: await asyncio.sleep(1)
            except Exception: pass
        await update.message.reply_text(f"✅ تم إرسال الإذاعة إلى {count} مستخدم.")
    elif state == "ban":
        results = search_users(text)
        if not results:
            await update.message.reply_text("❌ لم يتم العثور على المستخدم.")
            return
        target = results[0]['user_id']
        ban_user(target)
        await update.message.reply_text(f"🚫 تم حظر المستخدم {target} بنجاح.")
