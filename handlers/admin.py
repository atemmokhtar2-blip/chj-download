"""
Admin handler — full admin panel for OWNER_ID / ADMIN_IDS.
Commands: /admin, /stats, /broadcast, /ban, /unban, /users, /search,
          /system, /maintenance, /points, /vip, /refstats
"""
import os
import time
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from middlewares.auth import is_admin, is_owner
from locales import t
from database.users import (
    get_user, get_total_users, get_new_users_today, get_active_today,
    get_users_page, search_users, get_all_user_ids, ban_user, unban_user,
    set_vip, adjust_points_admin, get_total_points_issued,
    update_user,
)
from database.downloads import (
    get_downloads_today, get_downloads_week, get_downloads_month,
    get_downloads_by_platform, get_total_downloads, get_user_download_stats,
)
from database.cache import get_cache_count, get_cache_hits

from utils.helpers import format_size

logger = logging.getLogger(__name__)

ADMIN_CONVERSATION_STATES = {}


# ─── /admin — Main admin panel ───

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("⛔ هذا الأمر متاح للمسؤولين فقط.")
        return

    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    keyboard = [
        [
            InlineKeyboardButton(t(lang, "admin_panel_btn"), callback_data="admin_panel"),
            InlineKeyboardButton(t(lang, "admin_users_btn"), callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_broadcast_btn"), callback_data="admin_broadcast"),
            InlineKeyboardButton(t(lang, "admin_system_btn"), callback_data="admin_system"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_search_btn"), callback_data="admin_search"),
            InlineKeyboardButton(t(lang, "admin_maintenance_btn"), callback_data="admin_maintenance"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_vip_btn"), callback_data="admin_vip"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_points_btn"), callback_data="admin_points"),
            InlineKeyboardButton(t(lang, "admin_ban_btn"), callback_data="admin_ban"),
        ],
    ]

    await update.message.reply_text(
        t(lang, "admin_welcome"),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


# ─── Callback router for admin buttons ───

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
    elif data == "admin_system":
        await _show_system_monitor(query, lang)
    elif data == "admin_search":
        ADMIN_CONVERSATION_STATES[user.id] = "search"
        await query.edit_message_text(t(lang, "admin_search_prompt"))
    elif data == "admin_maintenance":
        await _show_maintenance_toggle(query, lang)

    elif data == "admin_vip":
        ADMIN_CONVERSATION_STATES[user.id] = "vip"
        await query.edit_message_text(t(lang, "admin_vip_prompt"))
    elif data == "admin_points":
        ADMIN_CONVERSATION_STATES[user.id] = "points"
        await query.edit_message_text(t(lang, "admin_points_prompt"))
    elif data == "admin_ban":
        ADMIN_CONVERSATION_STATES[user.id] = "ban"
        await query.edit_message_text(t(lang, "admin_ban_prompt"))
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
        await query.edit_message_text(t(lang, "admin_unbanned"))
    elif data.startswith("admin_ban_confirm_"):
        target_id = int(data.split("_")[-1])
        ban_user(target_id)
        await query.answer(t(lang, "admin_banned"))
        await query.edit_message_text(t(lang, "admin_banned"))
    elif data == "admin_send_broadcast":
        ADMIN_CONVERSATION_STATES[user.id] = "broadcast"
        await query.edit_message_text(t(lang, "admin_broadcast_message_prompt"))
    elif data == "admin_broadcast_cancel":
        if user.id in ADMIN_CONVERSATION_STATES:
            del ADMIN_CONVERSATION_STATES[user.id]
        await query.edit_message_text(t(lang, "cancelled"))
    elif data == "admin_set_vip":
        ADMIN_CONVERSATION_STATES[user.id] = "vip"
        await query.edit_message_text(t(lang, "admin_vip_prompt"))
    elif data == "admin_set_points":
        ADMIN_CONVERSATION_STATES[user.id] = "points"
        await query.edit_message_text(t(lang, "admin_points_prompt"))
    elif data == "admin_ban_user":
        ADMIN_CONVERSATION_STATES[user.id] = "ban"
        await query.edit_message_text(t(lang, "admin_ban_prompt"))
    elif data == "admin_search_user":
        ADMIN_CONVERSATION_STATES[user.id] = "search"
        await query.edit_message_text(t(lang, "admin_search_prompt"))


async def _show_admin_panel(query, lang: str):
    total_users = get_total_users()
    active_today = get_active_today()
    downloads_today = get_downloads_today()
    downloads_week = get_downloads_week()
    downloads_month = get_downloads_month()
    platform_stats = get_downloads_by_platform()
    cache_hits = get_cache_hits()
    total_points = get_total_points_issued()
    total_downloads = get_total_downloads()
    keyboard = [
        [
            InlineKeyboardButton(t(lang, "admin_users_btn"), callback_data="admin_users"),
            InlineKeyboardButton(t(lang, "admin_broadcast_btn"), callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_system_btn"), callback_data="admin_system"),
            InlineKeyboardButton(t(lang, "admin_search_btn"), callback_data="admin_search"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_maintenance_btn"), callback_data="admin_maintenance"),
        ],
    ]

    text = t(lang, "admin_panel",
             total_users=total_users,
             active_today=active_today,
             downloads_today=downloads_today,
             downloads_week=downloads_week,
             downloads_month=downloads_month,
             youtube=platform_stats.get("youtube", 0),
             facebook=platform_stats.get("facebook", 0),
             pinterest=platform_stats.get("pinterest", 0),
             cache_hits=cache_hits,
             queue=0,
             points_issued=total_points,
             referrals=0,
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
        lines.append(f"{status} <code>{u['user_id']}</code> — {u.get('first_name', '?')} (@{u.get('username', '?')}) — ⭐{u.get('points', 0)} 📥{u.get('downloads', 0)}")

    text = t(lang, "admin_users_title") + "\n\n" + "\n".join(lines)

    keyboard = []
    row = []
    for u in users:
        row.append(InlineKeyboardButton(f"👤 {u['user_id']}", callback_data=f"admin_user_detail_{u['user_id']}"))
        if len(row) >= 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Pagination
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"admin_users_page_{page - 1}"))
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

    stats = get_user_download_stats(target_id)
    is_banned = user.get("is_banned", 0)
    ban_label = "🚫" if is_banned else "✅"
    is_banned_str = t(lang, "yes") if is_banned else t(lang, "no")

    text = t(lang, "admin_user_card",
             user_id=target_id,
             name=user.get("first_name", "?") + (" " + user.get("last_name", "") or ""),
             username="@" + (user.get("username") or "-"),
             points=user.get("points", 0),
             downloads=user.get("downloads", 0),
             referrals=0,
             is_banned=is_banned_str,
             join_date=user.get("join_date", "?"),
             )
    text += f"\n\n📹 فيديوهات: {stats['video']} | 🎵 صوت: {stats['audio']} | 🖼 صور: {stats['total'] - stats['video'] - stats['audio']}"

    keyboard = [
        [
            InlineKeyboardButton("💰 تعديل نقاط", callback_data="admin_set_points"),
            InlineKeyboardButton("⭐ VIP", callback_data="admin_set_vip"),
        ],
    ]

    if is_banned:
        keyboard.append([InlineKeyboardButton("✅ فك الحظر", callback_data=f"admin_unban_{target_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🚫 حظر", callback_data=f"admin_ban_confirm_{target_id}")])

    keyboard.append([
        InlineKeyboardButton("🔙 المستخدمون", callback_data="admin_users"),
        InlineKeyboardButton("🔙 الرئيسية", callback_data="admin_panel"),
    ])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def _show_system_monitor(query, lang: str):
    """Show system resource stats."""
    import shutil

    # CPU
    try:
        with open("/proc/stat") as f:
            lines = f.readlines()
        cpu_line = lines[0].split()
        idle = float(cpu_line[4])
        total = sum(float(x) for x in cpu_line[1:])
        cpu_pct = round((1 - idle / total) * 100, 1)
    except Exception:
        cpu_pct = 0

    # RAM
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
        ram_total_mb = round(meminfo.get("MemTotal", 0) / 1024, 0)
        ram_available_mb = round(meminfo.get("MemAvailable", 0) / 1024, 0)
        ram_used_mb = ram_total_mb - ram_available_mb
        ram_pct = round(ram_used_mb / ram_total_mb * 100, 1) if ram_total_mb > 0 else 0
    except Exception:
        ram_total_mb = ram_used_mb = ram_pct = 0

    # Disk
    try:
        disk_total, disk_used, disk_free = shutil.disk_usage("/")
        disk_total_gb = round(disk_total / (1024 ** 3), 1)
        disk_used_gb = round(disk_used / (1024 ** 3), 1)
        disk_pct = round(disk_used / disk_total * 100, 1) if disk_total > 0 else 0
    except Exception:
        disk_total_gb = disk_used_gb = disk_pct = 0

    # DB size
    try:
        db_size_kb = round(os.path.getsize("/tmp/bot.db") / 1024, 0) if os.path.exists("/tmp/bot.db") else 0
    except Exception:
        db_size_kb = 0

    # Uptime
    from workers.heartbeat import _format_uptime
    uptime = _format_uptime()

    # Heartbeat age
    try:
        from workers.heartbeat import HEALTH_FILE
        age = time.time() - os.path.getmtime(HEALTH_FILE) if os.path.exists(HEALTH_FILE) else 0
        heartbeat_age = f"{int(age)}s"
    except Exception:
        heartbeat_age = "?"

    text = t(lang, "system_monitor",
             cpu=cpu_pct,
             ram_used=ram_used_mb,
             ram_total=ram_total_mb,
             ram_pct=ram_pct,
             disk_used=disk_used_gb,
             disk_total=disk_total_gb,
             disk_pct=disk_pct,
             db_size=db_size_kb,
             uptime=uptime,
             heartbeat_age=heartbeat_age,
             )

    keyboard = [
        [InlineKeyboardButton("🔙", callback_data="admin_panel")]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def _show_broadcast_form(query, lang: str):
    keyboard = [
        [InlineKeyboardButton(t(lang, "admin_broadcast_send"), callback_data="admin_send_broadcast")],
        [InlineKeyboardButton("🔙", callback_data="admin_panel")],
    ]
    await query.edit_message_text(t(lang, "admin_broadcast_intro"), reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_maintenance_toggle(query, lang: str):
    from utils.maintenance import is_maintenance
    current = is_maintenance()
    state_text = t(lang, "maintenance_on_status") if current else t(lang, "maintenance_off_status")

    keyboard = [
        [
            InlineKeyboardButton("🔴 " + t(lang, "admin_maintenance_on"), callback_data="admin_maintenance_on"),
            InlineKeyboardButton("🟢 " + t(lang, "admin_maintenance_off"), callback_data="admin_maintenance_off"),
        ],
        [InlineKeyboardButton("🔙", callback_data="admin_panel")],
    ]

    await query.edit_message_text(
        t(lang, "admin_maintenance_text", status=state_text),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )





# ─── Text message handlers for admin conversations ───

async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages during admin conversations."""
    user = update.effective_user
    if not is_admin(user.id):
        return

    state = ADMIN_CONVERSATION_STATES.get(user.id)
    if not state:
        return

    text = update.message.text.strip()

    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if state == "search":
        results = search_users(text)
        if not results:
            await update.message.reply_text(t(lang, "admin_user_not_found"))
        else:
            lines = []
            keyboard = []
            for u in results:
                status = "🚫" if u.get("is_banned") else "✅"
                lines.append(f"{status} <code>{u['user_id']}</code> — {u.get('first_name', '?')} (@{u.get('username', '?')})")
                keyboard.append([InlineKeyboardButton(
                    f"👤 {u['user_id']}",
                    callback_data=f"admin_user_detail_{u['user_id']}"
                )])
            keyboard.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
            await update.message.reply_text(
                t(lang, "admin_search_results") + "\n\n" + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        if user.id in ADMIN_CONVERSATION_STATES:
            del ADMIN_CONVERSATION_STATES[user.id]

    elif state == "broadcast":
        if user.id in ADMIN_CONVERSATION_STATES:
            del ADMIN_CONVERSATION_STATES[user.id]

        all_ids = get_all_user_ids()
        total = len(all_ids)
        success = 0
        failed = 0

        status_msg = await update.message.reply_text(
            t(lang, "admin_broadcast_progress", total=total, done=0),
            parse_mode="HTML"
        )

        from config.settings import BROADCAST_BATCH_SIZE, BROADCAST_DELAY
        import asyncio

        for i in range(0, total, BROADCAST_BATCH_SIZE):
            batch = all_ids[i:i + BROADCAST_BATCH_SIZE]
            for uid in batch:
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=text,
                        parse_mode="HTML"
                    )
                    success += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"Broadcast to {uid} failed: {e}")

            done = min(i + BROADCAST_BATCH_SIZE, total)
            try:
                await status_msg.edit_text(
                    t(lang, "admin_broadcast_progress", total=total, done=done),
                    parse_mode="HTML"
                )
            except Exception:
                pass

            await asyncio.sleep(BROADCAST_DELAY)

        # Log broadcast
        from database.db import db_cursor
        try:
            with db_cursor() as c:
                c.execute(
                    "INSERT INTO broadcast_log (admin_id, message, total, success, failed) VALUES (?, ?, ?, ?, ?)",
                    (user.id, text[:200], total, success, failed)
                )
        except Exception:
            pass

        await status_msg.edit_text(
            t(lang, "admin_broadcast_done", total=total, success=success, failed=failed),
            parse_mode="HTML"
        )

    elif state == "vip":
        if user.id in ADMIN_CONVERSATION_STATES:
            del ADMIN_CONVERSATION_STATES[user.id]
        # Format: user_id days
        parts = text.split()
        if len(parts) < 2 or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
            await update.message.reply_text(t(lang, "admin_vip_usage"))
            return
        target_id = int(parts[0])
        days = int(parts[1])
        target_user = get_user(target_id)
        if not target_user:
            await update.message.reply_text(t(lang, "admin_user_not_found"))
            return
        set_vip(target_id, days)
        await update.message.reply_text(t(lang, "admin_vip_set", user_id=target_id, days=days))

    elif state == "points":
        if user.id in ADMIN_CONVERSATION_STATES:
            del ADMIN_CONVERSATION_STATES[user.id]
        # Format: user_id amount
        parts = text.split()
        if len(parts) < 2 or not parts[0].lstrip("-").isdigit() or not parts[1].lstrip("-").isdigit():
            await update.message.reply_text(t(lang, "admin_points_usage"))
            return
        target_id = int(parts[0])
        amount = int(parts[1])
        new_total = adjust_points_admin(target_id, amount, user.id)
        if new_total == -1:
            await update.message.reply_text(t(lang, "admin_user_not_found"))
        elif new_total == -2:
            await update.message.reply_text(t(lang, "admin_points_negative_error"))
        else:
            await update.message.reply_text(t(lang, "admin_points_set", user_id=target_id, new_total=new_total))

    elif state == "ban":
        if user.id in ADMIN_CONVERSATION_STATES:
            del ADMIN_CONVERSATION_STATES[user.id]
        # Format: user_id or @username
        target = text.strip()
        target_id = None
        if target.lstrip("-").isdigit():
            target_id = int(target)
        else:
            # Try username
            term = target.lstrip("@").lower()
            results = search_users(term)
            if results:
                target_id = results[0]["user_id"]
        if target_id is None:
            await update.message.reply_text(t(lang, "admin_user_not_found"))
            return
        user_obj = get_user(target_id)
        if not user_obj:
            await update.message.reply_text(t(lang, "admin_user_not_found"))
            return
        is_banned = bool(user_obj.get("is_banned"))
        if is_banned:
            unban_user(target_id)
            await update.message.reply_text(t(lang, "admin_unbanned"))
        else:
            ban_user(target_id)
            await update.message.reply_text(t(lang, "admin_banned"))


# ─── Helpers ───

def _set_maintenance(on: bool):
    from utils.maintenance import set_maintenance
    set_maintenance(on)
