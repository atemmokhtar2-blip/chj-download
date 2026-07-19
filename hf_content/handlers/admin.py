import asyncio
import logging
import os
import time
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError, RetryAfter

from database.users import (
    get_user, get_total_users, get_new_users_today, get_all_user_ids,
    get_top_referrers, get_users_page, ban_user, unban_user,
    get_total_points_issued, update_user, get_active_today,
    search_users, adjust_points_admin
)
from database.downloads import (
    get_downloads_today, get_downloads_week, get_total_downloads,
    get_downloads_month, get_downloads_by_platform
)
from database.cache import get_cache_count, get_cache_hits
from database.reports import (
    get_reports, count_reports, get_report_by_id,
    reply_report, close_report, get_open_tickets
)
from database.db import db_cursor
from middlewares.auth import is_admin, is_owner
from locales import t
from config.settings import BROADCAST_BATCH_SIZE, BROADCAST_DELAY, DATABASE_PATH

logger = logging.getLogger(__name__)

REPORTS_PER_PAGE = 5


# ─── Decorator ─────────────────────────────────────────────────────────────────

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not is_admin(user.id):
            db_user = get_user(user.id)
            lang = db_user.get("language", "en") if db_user else "en"
            await update.message.reply_text(t(lang, "admin_no_perm"))
            return
        return await func(update, context)
    return wrapper


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _admin_lang(user_id: int) -> str:
    db_user = get_user(user_id)
    return db_user.get("language", "en") if db_user else "en"


def _progress_bar(pct: int, length: int = 12) -> str:
    filled = int(length * pct / 100)
    return "█" * filled + "░" * (length - filled)


def _admin_keyboard(lang: str, maint_on: bool) -> InlineKeyboardMarkup:
    maint_btn = t(lang, "admin_btn_maint_on") if maint_on else t(lang, "admin_btn_maint_off")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "admin_btn_dashboard"), callback_data="adm_dashboard"),
            InlineKeyboardButton(t(lang, "admin_btn_system"),    callback_data="adm_system"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_btn_search"),    callback_data="adm_search"),
            InlineKeyboardButton(t(lang, "admin_btn_points"),    callback_data="adm_points"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_btn_reports"),   callback_data="adm_reports"),
            InlineKeyboardButton(t(lang, "admin_btn_broadcast"), callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_btn_referrals"), callback_data="adm_referrals"),
            InlineKeyboardButton(maint_btn,                      callback_data="adm_maint"),
        ],
        [
            InlineKeyboardButton(t(lang, "admin_btn_activity"),  callback_data="adm_activity"),
        ],
    ])


def _gather_panel_stats() -> dict:
    total_users    = get_total_users()
    active_today   = get_active_today()
    new_today      = get_new_users_today()
    dl_today       = get_downloads_today()
    dl_week        = get_downloads_week()
    dl_month       = get_downloads_month()
    platforms      = get_downloads_by_platform()
    cache_hits     = get_cache_hits()
    cache_entries  = get_cache_count()
    points_issued  = get_total_points_issued()
    with db_cursor() as c:
        c.execute("SELECT COALESCE(SUM(referrals), 0) AS total FROM users")
        referrals = c.fetchone()["total"]
    return dict(
        total_users=total_users, active_today=active_today, new_today=new_today,
        downloads_today=dl_today, downloads_week=dl_week, downloads_month=dl_month,
        youtube=platforms["youtube"], facebook=platforms["facebook"],
        pinterest=platforms["pinterest"],
        cache_hits=cache_hits, cache_entries=cache_entries,
        queue=0, points_issued=points_issued, referrals=referrals
    )


def _get_maintenance_state() -> bool:
    try:
        from utils.maintenance import is_maintenance
        return is_maintenance()
    except Exception:
        return False


# ─── /panel ────────────────────────────────────────────────────────────────────

@admin_only
async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)
    stats = _gather_panel_stats()
    maint_on = _get_maintenance_state()
    await update.message.reply_text(
        t(lang, "admin_panel", **stats),
        reply_markup=_admin_keyboard(lang, maint_on),
        parse_mode="HTML"
    )


# ─── Main Panel Callback ────────────────────────────────────────────────────────

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user

    if not is_admin(user.id):
        await query.answer("❌ No permission.", show_alert=True)
        return

    await query.answer()
    lang   = _admin_lang(user.id)
    action = query.data  # full string, e.g. "adm_dashboard"

    # ── Dashboard ──
    if action == "adm_dashboard":
        stats    = _gather_panel_stats()
        maint_on = _get_maintenance_state()
        try:
            await query.edit_message_text(
                t(lang, "admin_panel", **stats),
                reply_markup=_admin_keyboard(lang, maint_on),
                parse_mode="HTML"
            )
        except Exception:
            await query.message.reply_text(
                t(lang, "admin_panel", **stats),
                reply_markup=_admin_keyboard(lang, maint_on),
                parse_mode="HTML"
            )

    # ── System Monitor ──
    elif action == "adm_system":
        await _send_system_monitor(query.message, lang)

    # ── User Search ──
    elif action == "adm_search":
        context.user_data["waiting_for_admin_search"] = True
        await query.message.reply_text(t(lang, "admin_search_prompt"))

    # ── Points Management ──
    elif action == "adm_points":
        await query.message.reply_text(
            t(lang, "admin_pts_usage"), parse_mode="HTML"
        )

    # ── Reports Center (open) ──
    elif action == "adm_reports":
        await _send_reports_page(query.message, lang, "open", 0)

    # ── Reports Center (closed) ──
    elif action == "adm_reports_c":
        await _send_reports_page(query.message, lang, "closed", 0)

    # ── Broadcast ──
    elif action == "adm_broadcast":
        await query.message.reply_text(
            "📢 <b>Broadcast</b>\n\n"
            "Usage: /broadcast &lt;your message&gt;\n"
            "Cancel: /cancelbroadcast\n\n"
            "The broadcast shows live progress and can be cancelled at any time.",
            parse_mode="HTML"
        )

    # ── Referral Analytics ──
    elif action == "adm_referrals":
        await _send_referral_analytics(query.message, lang)

    # ── Ban/Unban from User Card ──
    elif action.startswith("adm_usr_ban_"):
        target_id = int(action.split("_")[-1])
        target    = get_user(target_id)
        if not target:
            await query.answer("User not found.", show_alert=True)
            return
        if target.get("is_banned"):
            unban_user(target_id)
            await query.answer(f"✅ User {target_id} unbanned.", show_alert=True)
        else:
            ban_user(target_id)
            await query.answer(f"🚫 User {target_id} banned.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    # ── Broadcast Cancel Button ──
    elif action.startswith("adm_bc_cancel_"):
        admin_id   = int(action.split("_")[-1])
        cancel_key = f"broadcast_cancel_{admin_id}"
        context.application.bot_data[cancel_key] = True
        await query.answer("🛑 Cancellation requested.", show_alert=True)

    # ── Maintenance Toggle ──
    elif action == "adm_maint":
        from utils.maintenance import is_maintenance, set_maintenance
        current = is_maintenance()
        set_maintenance(not current)
        new_state = not current
        from utils.logger import admin_logger
        admin_logger.info(f"Admin {user.id} toggled maintenance → {'ON' if new_state else 'OFF'}")
        key = "maintenance_on" if new_state else "maintenance_off"
        await query.message.reply_text(t(lang, key), parse_mode="HTML")
        maint_on = new_state
        stats    = _gather_panel_stats()
        try:
            await query.edit_message_reply_markup(
                reply_markup=_admin_keyboard(lang, maint_on)
            )
        except Exception:
            pass

    # ── Live Activity Feed ──
    elif action == "adm_activity":
        await _send_activity_feed(query.message, lang)

    # ── Report: Close ──
    elif action.startswith("adm_rpt_close_"):
        report_id = int(action.split("_")[-1])
        report = get_report_by_id(report_id)
        if not report:
            await query.answer(f"Report #{report_id} not found.", show_alert=True)
            return
        close_report(report_id, closed_by=user.id)
        await query.answer(f"✅ Report #{report_id} closed.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    # ── Report: Initiate Reply ──
    elif action.startswith("adm_rpt_reply_"):
        parts = action.split("_")
        try:
            report_id      = int(parts[-2])
            target_user_id = int(parts[-1])
        except (IndexError, ValueError):
            await query.answer("❌ Invalid data", show_alert=True)
            return
        context.user_data["pending_rpt_reply"] = {
            "report_id": report_id,
            "target_user_id": target_user_id,
        }
        await query.message.reply_text(
            t(lang, "admin_reply_prompt", report_id=report_id, user_id=target_user_id)
        )

    # ── Reports Pagination ──
    elif action.startswith("adm_rpt_pg_"):
        parts = action.split("_")
        status = parts[3]   # "open" or "closed"
        page   = int(parts[4])
        await _send_reports_page(query.message, lang, status, page)


# ─── System Monitor ────────────────────────────────────────────────────────────

async def _send_system_monitor(message, lang: str):
    try:
        import psutil
        cpu      = psutil.cpu_percent(interval=0.3)
        vm       = psutil.virtual_memory()
        disk     = psutil.disk_usage("/")
        ram_used  = round(vm.used  / 1024 / 1024, 1)
        ram_total = round(vm.total / 1024 / 1024, 1)
        ram_pct   = vm.percent
        disk_used  = round(disk.used  / 1024 / 1024 / 1024, 2)
        disk_total = round(disk.total / 1024 / 1024 / 1024, 2)
        disk_pct   = round(disk.used / disk.total * 100, 1)
    except Exception:
        cpu = ram_used = ram_total = ram_pct = 0
        disk_used = disk_total = disk_pct = 0

    try:
        db_size = round(os.path.getsize(DATABASE_PATH) / 1024, 1)
    except Exception:
        db_size = 0

    try:
        from workers.heartbeat import get_health_data
        hdata = get_health_data()
        uptime = hdata.get("uptime_human", "N/A")
        ts     = hdata.get("timestamp", 0)
        age    = int(time.time() - ts) if ts else -1
        heartbeat_age = f"{age}s ago" if age >= 0 else "N/A"
    except Exception:
        uptime = "N/A"
        heartbeat_age = "N/A"

    await message.reply_text(
        t(lang, "system_monitor",
          cpu=cpu,
          ram_used=ram_used, ram_total=ram_total, ram_pct=ram_pct,
          disk_used=disk_used, disk_total=disk_total, disk_pct=disk_pct,
          db_size=db_size, uptime=uptime, heartbeat_age=heartbeat_age),
        parse_mode="HTML"
    )


# ─── Reports Center ────────────────────────────────────────────────────────────

async def _send_reports_page(message, lang: str, status: str, page: int):
    limit  = REPORTS_PER_PAGE
    offset = page * limit
    reports = get_reports(status=status, limit=limit, offset=offset)
    open_count   = count_reports("open")
    closed_count = count_reports("closed")
    total        = open_count if status == "open" else closed_count

    open_label   = t(lang, "admin_reports_open_tab",   count=open_count)
    closed_label = t(lang, "admin_reports_closed_tab", count=closed_count)

    tab_row = [
        InlineKeyboardButton(
            f"▶ {open_label}"   if status == "open"   else open_label,
            callback_data="adm_reports"
        ),
        InlineKeyboardButton(
            f"▶ {closed_label}" if status == "closed" else closed_label,
            callback_data="adm_reports_c"
        ),
    ]

    if not reports:
        await message.reply_text(
            t(lang, "admin_no_reports"),
            reply_markup=InlineKeyboardMarkup([tab_row]),
            parse_mode="HTML"
        )
        return

    text = f"🐞 <b>Reports Center</b>\n\n"
    action_rows = []

    for r in reports:
        uname     = f"@{r['username']}" if r.get("username") else f"ID:{r['user_id']}"
        date_str  = str(r.get("created_at", ""))[:16]
        msg_short = (r.get("message") or "—")[:80]
        text += (
            f"<b>#{r['id']}</b> | {uname} | 📺 {r.get('platform','?')} | 🕐 {date_str}\n"
            f"📝 {msg_short}\n\n"
        )
        if status == "open":
            action_rows.append([
                InlineKeyboardButton(
                    f"📩 Reply #{r['id']}",
                    callback_data=f"adm_rpt_reply_{r['id']}_{r['user_id']}"
                ),
                InlineKeyboardButton(
                    f"✅ Close #{r['id']}",
                    callback_data=f"adm_rpt_close_{r['id']}"
                ),
            ])
        else:
            reply_text = (r.get("reply") or "—")[:60]
            text += f"💬 Reply: {reply_text}\n\n"

    total_pages = max(1, (total + limit - 1) // limit)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            "◀", callback_data=f"adm_rpt_pg_{status}_{page - 1}"
        ))
    nav_row.append(InlineKeyboardButton(
        f"📄 {page + 1}/{total_pages}", callback_data="adm_dashboard"
    ))
    if (page + 1) * limit < total:
        nav_row.append(InlineKeyboardButton(
            "▶", callback_data=f"adm_rpt_pg_{status}_{page + 1}"
        ))

    keyboard = [tab_row] + action_rows + [nav_row]
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# ─── Referral Analytics ─────────────────────────────────────────────────────────

async def _send_referral_analytics(message, lang: str):
    with db_cursor() as c:
        c.execute("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'completed'")
        completed = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) AS cnt FROM referrals WHERE status = 'pending'")
        pending = c.fetchone()["cnt"]

    from config.settings import POINTS_REFERRAL
    pts_awarded = completed * POINTS_REFERRAL

    top = get_top_referrers(period="all", limit=10)
    top_list = ""
    for i, u in enumerate(top, 1):
        name = u.get("first_name") or u.get("username") or f"ID:{u['user_id']}"
        top_list += t(lang, "admin_referral_item",
                      rank=i, name=name, count=u["referrals"])

    if not top_list:
        top_list = "— No referrals yet —\n"

    await message.reply_text(
        t(lang, "admin_referral_analytics",
          completed=completed, pending=pending,
          pts_awarded=pts_awarded, top_list=top_list),
        parse_mode="HTML"
    )


# ─── Admin Search Handler ───────────────────────────────────────────────────────

async def admin_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called from message_router when waiting_for_admin_search is set."""
    if not context.user_data.get("waiting_for_admin_search"):
        return False
    if not is_admin(update.effective_user.id):
        return False

    context.user_data.pop("waiting_for_admin_search", None)
    query_str = (update.message.text or "").strip()
    lang = _admin_lang(update.effective_user.id)

    results = search_users(query_str)
    if not results:
        await update.message.reply_text(t(lang, "admin_user_not_found"))
        return True

    for u in results[:3]:
        uid       = u["user_id"]
        name      = u.get("first_name") or "—"
        username  = f"@{u['username']}" if u.get("username") else "—"
        banned    = "✅ Yes" if u.get("is_banned") else "❌ No"
        join_date = str(u.get("join_date", "—"))[:10]
        last_seen = str(u.get("last_seen", "—"))[:16]

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🚫 Ban" if not u.get("is_banned") else "✅ Unban",
                    callback_data=f"adm_usr_ban_{uid}"
                ),
            ]
        ])

        await update.message.reply_text(
            t(lang, "admin_user_card",
              user_id=uid, name=name, username=username,
              language=u.get("language", "?"),
              downloads=u.get("downloads", 0),
              referrals=u.get("referrals", 0),
              points=u.get("points", 0),
              join_date=join_date,
              last_seen=last_seen,
              banned=banned),
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    return True


# ─── /addpoints & /removepoints ────────────────────────────────────────────────

@admin_only
async def addpoints_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)
    args = context.args or []

    if len(args) < 2 or not args[0].lstrip("-").isdigit() or not args[1].isdigit():
        await update.message.reply_text(t(lang, "admin_pts_usage"))
        return

    target_id = int(args[0])
    amount    = int(args[1])
    note      = " ".join(args[2:]) if len(args) > 2 else ""

    new_total = adjust_points_admin(target_id, amount, admin_id=user.id, note=note)
    if new_total == -1:
        await update.message.reply_text(t(lang, "admin_user_not_found"))
        return

    from utils.logger import admin_logger
    admin_logger.info(f"Admin {user.id} added {amount} pts to {target_id}. Total: {new_total}")
    await update.message.reply_text(
        t(lang, "admin_pts_added", amount=amount, user_id=target_id, total=new_total),
        parse_mode="HTML"
    )


@admin_only
async def removepoints_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)
    args = context.args or []

    if len(args) < 2 or not args[0].lstrip("-").isdigit() or not args[1].isdigit():
        await update.message.reply_text(t(lang, "admin_pts_usage"))
        return

    target_id = int(args[0])
    amount    = int(args[1])
    note      = " ".join(args[2:]) if len(args) > 2 else ""

    target = get_user(target_id)
    if not target:
        await update.message.reply_text(t(lang, "admin_user_not_found"))
        return

    have = target.get("points", 0)
    if have < amount:
        await update.message.reply_text(
            t(lang, "admin_pts_not_enough", have=have, amount=amount),
            parse_mode="HTML"
        )
        return

    new_total = adjust_points_admin(target_id, -amount, admin_id=user.id, note=note)
    if new_total < 0:
        await update.message.reply_text(t(lang, "admin_user_not_found"))
        return

    from utils.logger import admin_logger
    admin_logger.info(f"Admin {user.id} removed {amount} pts from {target_id}. Total: {new_total}")
    await update.message.reply_text(
        t(lang, "admin_pts_removed", amount=amount, user_id=target_id, total=new_total),
        parse_mode="HTML"
    )


# ─── /search ───────────────────────────────────────────────────────────────────

@admin_only
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)

    if not context.args:
        await update.message.reply_text("Usage: /search <user_id or @username>")
        return

    query_str = context.args[0].strip()
    results   = search_users(query_str)

    if not results:
        await update.message.reply_text(t(lang, "admin_user_not_found"))
        return

    for u in results[:3]:
        uid      = u["user_id"]
        name     = u.get("first_name") or "—"
        username = f"@{u['username']}" if u.get("username") else "—"
        banned   = "✅ Yes" if u.get("is_banned") else "❌ No"
        join_date = str(u.get("join_date", "—"))[:10]
        last_seen = str(u.get("last_seen", "—"))[:16]

        await update.message.reply_text(
            t(lang, "admin_user_card",
              user_id=uid, name=name, username=username,
              language=u.get("language", "?"),
              downloads=u.get("downloads", 0),
              referrals=u.get("referrals", 0),
              points=u.get("points", 0),
              join_date=join_date,
              last_seen=last_seen,
              banned=banned),
            parse_mode="HTML"
        )


# ─── /broadcast (enhanced) ─────────────────────────────────────────────────────

@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)

    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast <message>\nCancel: /cancelbroadcast"
        )
        return

    message_text = " ".join(context.args)
    all_ids      = get_all_user_ids()
    total        = len(all_ids)

    cancel_key = f"broadcast_cancel_{user.id}"
    context.application.bot_data[cancel_key] = False

    cancel_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang, "broadcast_cancel_btn"), callback_data=f"adm_bc_cancel_{user.id}")
    ]])

    progress_msg = await update.message.reply_text(
        t(lang, "broadcast_start", total=total),
        reply_markup=cancel_btn,
        parse_mode="HTML"
    )

    success = 0
    failed  = 0
    sent    = 0
    last_edit = 0

    for i in range(0, total, BROADCAST_BATCH_SIZE):
        if context.application.bot_data.get(cancel_key):
            break

        batch   = all_ids[i:i + BROADCAST_BATCH_SIZE]
        tasks   = [_send_broadcast(context.bot, uid, message_text) for uid in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception) or r is False:
                failed += 1
            else:
                success += 1
            sent += 1

        pct = int(sent / total * 100) if total else 100
        now = time.time()
        if now - last_edit >= 2:
            last_edit = now
            bar = _progress_bar(pct)
            try:
                await progress_msg.edit_text(
                    t(lang, "broadcast_progress",
                      bar=bar, pct=pct, success=success,
                      failed=failed, sent=sent, total=total),
                    reply_markup=cancel_btn,
                    parse_mode="HTML"
                )
            except Exception:
                pass

        await asyncio.sleep(BROADCAST_DELAY)

    cancelled = context.application.bot_data.pop(cancel_key, False)

    with db_cursor() as c:
        c.execute("""
            INSERT INTO broadcast_log (admin_id, message, total, success, failed)
            VALUES (?, ?, ?, ?, ?)
        """, (user.id, message_text, total, success, failed))

    if cancelled:
        final_text = t(lang, "broadcast_cancelled", sent=sent, total=total)
    else:
        final_text = t(lang, "broadcast_done", success=success, failed=failed, total=total)

    try:
        await progress_msg.edit_text(final_text, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(final_text, parse_mode="HTML")


@admin_only
async def cancelbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    lang      = _admin_lang(user.id)
    cancel_key = f"broadcast_cancel_{user.id}"
    if not context.application.bot_data.get(cancel_key, None) is not None:
        context.application.bot_data[cancel_key] = True
        await update.message.reply_text("🛑 Cancel signal sent. Broadcast will stop after current batch.")
    else:
        await update.message.reply_text(t(lang, "broadcast_no_active"))


async def _send_broadcast(bot, user_id: int, message: str) -> bool:
    try:
        await bot.send_message(user_id, message)
        return True
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await bot.send_message(user_id, message)
            return True
        except TelegramError:
            return False
    except TelegramError:
        return False


# ─── /stats (enhanced) ─────────────────────────────────────────────────────────

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)

    total          = get_total_users()
    new_today      = get_new_users_today()
    active_today   = get_active_today()
    downloads_total = get_total_downloads()
    downloads_today = get_downloads_today()
    downloads_week  = get_downloads_week()
    downloads_month = get_downloads_month()
    platforms       = get_downloads_by_platform()
    points          = get_total_points_issued()
    cache_count     = get_cache_count()
    cache_hits      = get_cache_hits()
    cache_rate      = round(cache_hits / max(downloads_total, 1) * 100, 1)

    await update.message.reply_text(
        t(lang, "stats_text",
          total=total, new_today=new_today, active_today=active_today,
          downloads_total=downloads_total, downloads_today=downloads_today,
          downloads_week=downloads_week, downloads_month=downloads_month,
          youtube=platforms["youtube"], facebook=platforms["facebook"],
          pinterest=platforms["pinterest"],
          points=points, cache_rate=cache_rate),
        parse_mode="HTML"
    )


# ─── /users ────────────────────────────────────────────────────────────────────

@admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    page   = int(args[0]) if args and args[0].isdigit() else 0
    offset = page * 20

    users = get_users_page(offset=offset, limit=20)
    total = get_total_users()

    text = f"👥 <b>Users (Page {page+1})</b> — Total: {total}\n\n"
    for u in users:
        banned = "🚫" if u.get("is_banned") else "✅"
        name   = u.get("first_name") or u.get("username") or f"ID:{u['user_id']}"
        text  += (f"{banned} <b>{name}</b> | "
                  f"📥 {u.get('downloads', 0)} | "
                  f"⭐ {u.get('points', 0)} | "
                  f"ID: <code>{u['user_id']}</code>\n")

    if len(users) == 20:
        text += f"\n/users {page+1} — Next page"

    await update.message.reply_text(text, parse_mode="HTML")


# ─── /topusers ─────────────────────────────────────────────────────────────────

@admin_only
async def topusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db_cursor() as c:
        c.execute("""
            SELECT user_id, first_name, username, downloads, referrals, points
            FROM users ORDER BY downloads DESC LIMIT 15
        """)
        users = [dict(row) for row in c.fetchall()]

    text = "🏆 <b>Top Users by Downloads</b>\n\n"
    for i, u in enumerate(users, 1):
        name = u.get("first_name") or u.get("username") or f"ID:{u['user_id']}"
        text += f"{i}. {name} — 📥 {u['downloads']} | 👥 {u['referrals']} | ⭐ {u['points']}\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ─── /ban & /unban ─────────────────────────────────────────────────────────────

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /ban <user_id>")
        return

    target_id = int(context.args[0])
    target    = get_user(target_id)
    if not target:
        await update.message.reply_text(t(lang, "user_not_found"))
        return

    ban_user(target_id)
    from utils.logger import admin_logger
    admin_logger.info(f"Admin {user.id} banned user {target_id}")
    await update.message.reply_text(t(lang, "ban_success", user_id=target_id))


@admin_only
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return

    target_id = int(context.args[0])
    target    = get_user(target_id)
    if not target:
        await update.message.reply_text(t(lang, "user_not_found"))
        return

    unban_user(target_id)
    from utils.logger import admin_logger
    admin_logger.info(f"Admin {user.id} unbanned user {target_id}")
    await update.message.reply_text(t(lang, "unban_success", user_id=target_id))


# ─── /reports ──────────────────────────────────────────────────────────────────

@admin_only
async def reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args   = context.args or []
    status = "closed" if args and args[0].lower() == "closed" else "open"
    page   = 0
    for a in args:
        if a.isdigit():
            page = int(a)
            break

    lang = _admin_lang(update.effective_user.id)
    await _send_reports_page(update.message, lang, status, page)


# ─── /referrals ────────────────────────────────────────────────────────────────

@admin_only
async def referrals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _admin_lang(update.effective_user.id)
    await _send_referral_analytics(update.message, lang)


# ─── /status ───────────────────────────────────────────────────────────────────

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _admin_lang(update.effective_user.id)
    await _send_system_monitor(update.message, lang)


# ─── /maintenance ──────────────────────────────────────────────────────────────

@admin_only
async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = _admin_lang(user.id)

    if not context.args:
        await update.message.reply_text("Usage: /maintenance on|off")
        return

    arg = context.args[0].lower()
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /maintenance on|off")
        return

    from utils.maintenance import set_maintenance
    is_on = arg == "on"
    set_maintenance(is_on)

    from utils.logger import admin_logger
    admin_logger.info(f"Admin {user.id} set maintenance mode to {arg.upper()}")

    key = "maintenance_on" if is_on else "maintenance_off"
    await update.message.reply_text(t(lang, key), parse_mode="HTML")


# ─── rpt_inline_callback (^rpt_  — from report notifications) ────────────────

async def rpt_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user
    if not is_admin(user.id):
        await query.answer("⛔ Admins only", show_alert=True)
        return

    lang = _admin_lang(user.id)
    data = query.data

    if data.startswith("rpt_close_"):
        report_id = int(data.split("_")[2])
        report = get_report_by_id(report_id)
        if not report:
            await query.answer(t(lang, "report_not_found", report_id=report_id), show_alert=True)
            return
        close_report(report_id, closed_by=user.id)
        await query.answer(t(lang, "admin_report_closed_ok", report_id=report_id), show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif data.startswith("rpt_reply_"):
        parts = data.split("_")
        try:
            report_id      = int(parts[2])
            target_user_id = int(parts[3])
        except (IndexError, ValueError):
            await query.answer("❌ Invalid data", show_alert=True)
            return
        await query.answer()
        context.user_data["pending_rpt_reply"] = {
            "report_id": report_id,
            "target_user_id": target_user_id,
        }
        await query.message.reply_text(
            t(lang, "admin_reply_prompt", report_id=report_id, user_id=target_user_id)
        )



# ─── /reply_{id}_{uid} ─────────────────────────────────────────────────────────

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    cmd = update.message.text.split(" ", 1)
    if len(cmd) < 2:
        await update.message.reply_text("Usage: /reply_{ticket_id}_{user_id} <message>")
        return

    parts = cmd[0].split("_")
    if len(parts) < 3:
        return
    try:
        ticket_id      = int(parts[1])
        target_user_id = int(parts[2])
    except (ValueError, IndexError):
        return

    reply_text = cmd[1].strip()
    if not reply_text:
        await update.message.reply_text("❌ Reply message cannot be empty.")
        return

    db_user = get_user(target_user_id)
    lang    = db_user.get("language", "en") if db_user else "en"

    try:
        await context.bot.send_message(
            target_user_id,
            t(lang, "support_reply_header") + reply_text,
            parse_mode="HTML"
        )
        from database.reports import reply_support
        reply_support(ticket_id, reply_text)
        await update.message.reply_text("✅ Reply sent to user.")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")


# ─── /report_reply_{id}_{uid} ──────────────────────────────────────────────────

async def report_reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    cmd = update.message.text.split(" ", 1)
    if len(cmd) < 2:
        await update.message.reply_text(
            "Usage: /report_reply_{report_id}_{user_id} <message>"
        )
        return

    parts = cmd[0].split("_")
    try:
        report_id      = int(parts[-2])
        target_user_id = int(parts[-1])
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid command format.")
        return

    reply_text_str = cmd[1].strip()
    if not reply_text_str:
        await update.message.reply_text("❌ Reply message cannot be empty.")
        return

    report = get_report_by_id(report_id)
    if not report:
        lang = _admin_lang(user.id)
        await update.message.reply_text(t(lang, "report_not_found", report_id=report_id))
        return

    db_user = get_user(target_user_id)
    lang    = db_user.get("language", "en") if db_user else "en"

    try:
        await context.bot.send_message(
            target_user_id,
            t(lang, "report_reply_header") + reply_text_str,
            parse_mode="HTML"
        )
        reply_report(report_id, user.id, reply_text_str)
        from utils.logger import admin_logger
        admin_logger.info(
            f"Admin {user.id} replied to report #{report_id} for user {target_user_id}"
        )
        admin_lang = _admin_lang(user.id)
        await update.message.reply_text(t(admin_lang, "report_reply_sent"))
    except TelegramError as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")


# ─── /closereport ──────────────────────────────────────────────────────────────

async def closereport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    lang = _admin_lang(user.id)

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /closereport <report_id>")
        return

    report_id = int(context.args[0])
    report    = get_report_by_id(report_id)
    if not report:
        await update.message.reply_text(t(lang, "report_not_found", report_id=report_id))
        return

    close_report(report_id, closed_by=user.id)
    from utils.logger import admin_logger
    admin_logger.info(f"Admin {user.id} closed report #{report_id}")
    await update.message.reply_text(t(lang, "report_closed", report_id=report_id))


# ─── Live Activity Feed ────────────────────────────────────────────────────────

_ACTIVITY_EVENTS: dict[str, tuple[str, str]] = {
    "new_user":  ("🆕", "activity_event_new_user"),
    "download":  ("📥", "activity_event_download"),
    "referral":  ("🎁", "activity_event_referral"),
    "report":    ("🐞", "activity_event_report"),
    "points":    ("⭐", "activity_event_points"),
    "broadcast": ("📢", "activity_event_broadcast"),
}


def _format_activity_item(row: dict, lang: str) -> str:
    event_type      = row.get("event_type", "")
    icon, label_key = _ACTIVITY_EVENTS.get(event_type, ("•", ""))
    label           = t(lang, label_key) if label_key else event_type

    uid        = row.get("user_id") or ""
    username   = row.get("username") or ""
    first_name = row.get("first_name") or ""
    detail     = row.get("detail") or ""
    ts         = str(row.get("created_at") or "")[:16]
    time_part  = ts[11:16] if len(ts) >= 16 else ts

    user_str = f"@{username}" if username else (first_name or f"#{uid}")

    lines = [
        f"{icon} <b>{label}</b>",
        f"👤 User: {user_str}",
        f"🆔 ID: <code>{uid}</code>",
        f"🕒 Time: {time_part}",
    ]
    if detail:
        lines.append(f"📌 {detail}")
    return "\n".join(lines)


async def _send_activity_feed(message, lang: str) -> None:
    from database.activity import get_activity_feed

    items = get_activity_feed(50)

    refresh_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang, "activity_feed_refresh"), callback_data="adm_activity")
    ]])

    if not items:
        await message.reply_text(
            t(lang, "activity_feed_empty"),
            reply_markup=refresh_keyboard,
            parse_mode="HTML"
        )
        return

    header    = t(lang, "activity_feed_title")
    separator = "\n\n" + "─" * 18 + "\n\n"
    entries   = [_format_activity_item(row, lang) for row in items]

    body  = ""
    shown = 0
    for entry in entries:
        candidate = body + (separator if body else "") + entry
        if len(header) + len(candidate) > 3800:
            break
        body  = candidate
        shown += 1

    if shown < len(items):
        body += f"\n\n…{len(items) - shown} more not shown"

    await message.reply_text(
        header + body,
        reply_markup=refresh_keyboard,
        parse_mode="HTML"
    )


@admin_only
async def activity_feed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _admin_lang(update.effective_user.id)
    await _send_activity_feed(update.message, lang)
