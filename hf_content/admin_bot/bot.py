import asyncio
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

from config.settings import ADMIN_BOT_TOKEN
from handlers.auth import is_admin, require_admin
from handlers.dashboard import start_cmd, panel_cmd, show_main_menu
from handlers.stats import show_stats
from handlers.users import (
    show_users_menu, prompt_search_id, prompt_search_username,
    show_user_profile, do_ban, do_unban,
    prompt_add_points, prompt_remove_points,
    handle_search_id, handle_search_username,
    handle_add_points, handle_remove_points,
    show_all_users,
)
from handlers.broadcast import (
    show_broadcast_menu, prompt_text_broadcast,
    prompt_photo_broadcast, prompt_video_broadcast,
    show_broadcast_history,
    handle_text_broadcast, handle_photo_broadcast, handle_video_broadcast,
)
from handlers.support import (
    show_support_menu, show_open_tickets,
    show_closed_tickets, show_ticket,
)
from handlers.downloads import (
    show_downloads_menu, show_recent_downloads,
    show_incomplete_downloads, show_platform_stats, show_top_content,
)
from handlers.security import (
    show_security_menu, show_banned_users,
    show_abuse_reports, show_suspicious_activity, show_spam_reporters,
)
from handlers.settings_h import show_settings, toggle_language
from utils.logger import system_logger, error_logger


# ── Callback dispatcher ───────────────────────────────────────────────────────

async def callback_handler(update: Update, context):
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    if not is_admin(user.id):
        await query.answer("🚫 Access Denied", show_alert=True)
        return

    await query.answer()
    data = query.data

    try:
        # ── Main menu ──────────────────────────────────────────────────
        if data == "adm_menu":
            await show_main_menu(query, context)

        # ── Language toggle ────────────────────────────────────────────
        elif data == "adm_lang_toggle":
            await toggle_language(query, context)

        # ── Statistics ─────────────────────────────────────────────────
        elif data == "adm_stats":
            await show_stats(query, context)

        # ── Users ──────────────────────────────────────────────────────
        elif data == "adm_users":
            await show_users_menu(query, context)
        elif data == "adm_users_sid":
            await prompt_search_id(query, context)
        elif data == "adm_users_sun":
            await prompt_search_username(query, context)
        elif data.startswith("adm_users_list_"):
            offset = int(data.split("_")[-1])
            await show_all_users(query, context, offset)
        elif data.startswith("adm_users_view_"):
            uid = int(data.split("_")[-1])
            await show_user_profile(query, context, uid)
        elif data.startswith("adm_users_ban_"):
            uid = int(data.split("_")[-1])
            await do_ban(query, context, uid)
        elif data.startswith("adm_users_unban_"):
            uid = int(data.split("_")[-1])
            await do_unban(query, context, uid)
        elif data.startswith("adm_users_ap_"):
            uid = int(data.split("_")[-1])
            await prompt_add_points(query, context, uid)
        elif data.startswith("adm_users_rp_"):
            uid = int(data.split("_")[-1])
            await prompt_remove_points(query, context, uid)

        # ── Broadcast ──────────────────────────────────────────────────
        elif data == "adm_bcast":
            await show_broadcast_menu(query, context)
        elif data == "adm_bcast_text":
            await prompt_text_broadcast(query, context)
        elif data == "adm_bcast_photo":
            await prompt_photo_broadcast(query, context)
        elif data == "adm_bcast_video":
            await prompt_video_broadcast(query, context)
        elif data == "adm_bcast_hist":
            await show_broadcast_history(query, context)

        # ── Support ────────────────────────────────────────────────────
        elif data == "adm_sup":
            await show_support_menu(query, context)
        elif data.startswith("adm_sup_open_"):
            offset = int(data.split("_")[-1])
            await show_open_tickets(query, context, offset)
        elif data.startswith("adm_sup_closed_"):
            offset = int(data.split("_")[-1])
            await show_closed_tickets(query, context, offset)
        elif data.startswith("adm_sup_ticket_"):
            tid = int(data.split("_")[-1])
            await show_ticket(query, context, tid)

        # ── Downloads ──────────────────────────────────────────────────
        elif data == "adm_dl":
            await show_downloads_menu(query, context)
        elif data == "adm_dl_last":
            await show_recent_downloads(query, context)
        elif data == "adm_dl_fail":
            await show_incomplete_downloads(query, context)
        elif data == "adm_dl_plat":
            await show_platform_stats(query, context)
        elif data == "adm_dl_top":
            await show_top_content(query, context)

        # ── Security ───────────────────────────────────────────────────
        elif data == "adm_sec":
            await show_security_menu(query, context)
        elif data == "adm_sec_banned":
            await show_banned_users(query, context)
        elif data == "adm_sec_reports":
            await show_abuse_reports(query, context)
        elif data == "adm_sec_susp":
            await show_suspicious_activity(query, context)
        elif data == "adm_sec_spam":
            await show_spam_reporters(query, context)

        # ── Settings ───────────────────────────────────────────────────
        elif data == "adm_settings":
            await show_settings(query, context)

    except Exception as exc:
        error_logger.error("Callback error [%s] for admin %s: %s", data, user.id, exc)
        try:
            await query.edit_message_text(
                f"❌ An error occurred: <code>{exc}</code>\n\nUse /panel to restart.",
                parse_mode="HTML"
            )
        except Exception:
            pass


# ── Text message router ───────────────────────────────────────────────────────

@require_admin
async def message_router(update: Update, context):
    state = context.user_data.get("adm_state", "")

    if not state:
        await update.message.reply_text(
            "Use /panel to open the Admin Panel."
        )
        return

    # ── User search states ─────────────────────────────────────────────
    if state == "search_id":
        await handle_search_id(update, context)
    elif state == "search_un":
        await handle_search_username(update, context)

    # ── Points states ──────────────────────────────────────────────────
    elif state.startswith("addpts_"):
        uid = int(state.split("_")[1])
        await handle_add_points(update, context, uid)
    elif state.startswith("rmpts_"):
        uid = int(state.split("_")[1])
        await handle_remove_points(update, context, uid)

    # ── Broadcast states ───────────────────────────────────────────────
    elif state == "bcast_text":
        await handle_text_broadcast(update, context)

    else:
        context.user_data.pop("adm_state", None)
        await update.message.reply_text("❌ State reset. Use /panel to continue.")


# ── Media router (for photo/video broadcasts) ─────────────────────────────────

@require_admin
async def media_router(update: Update, context):
    state = context.user_data.get("adm_state", "")
    if state == "bcast_photo" and update.message.photo:
        await handle_photo_broadcast(update, context)
    elif state == "bcast_video" and update.message.video:
        await handle_video_broadcast(update, context)
    else:
        await update.message.reply_text("Use /panel to open the Admin Panel.")


# ── Cancel ────────────────────────────────────────────────────────────────────

@require_admin
async def cancel_cmd(update: Update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled. Use /panel to open the Admin Panel."
    )


# ── Post-init ─────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    system_logger.info("Admin Bot starting up…")


def build_application() -> Application:
    if not ADMIN_BOT_TOKEN:
        raise ValueError("ADMIN_BOT_TOKEN is not set.")

    app = Application.builder().token(ADMIN_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("panel",  panel_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(callback_handler, pattern="^adm_"))

    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, media_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    return app


def main():
    system_logger.info("=== ADMIN BOT STARTUP === PID:%s", os.getpid())
    app = build_application()
    system_logger.info("Admin Bot polling started.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
