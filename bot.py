import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters
)

from config.settings import BOT_TOKEN
from database.db import init_db
from utils.logger import system_logger

from handlers.start import (
    start_command, language_callback, verify_subscription_callback,
    help_command, settings_command, settings_callback, CHOOSING_LANGUAGE
)
from handlers.download import handle_url, download_callback
from handlers.profile import (
    profile_command, profile_callback, history_command, referral_command,
    points_command, redeem_callback, daily_command, wheel_command, wheel_callback,
    achievements_command, leaderboard_command, leaderboard_callback,
    goals_command, top_command
)
from handlers.feedback import (
    feedback_callback, handle_report_message,
    support_command, handle_support_message
)
from handlers.favorites import favorites_command, unfav_callback
from handlers.video_tools import video_tools_command, handle_video_file, video_tools_callback
from handlers.video_studio import (
    studio_command, handle_video_for_studio, studio_callback,
    handle_studio_text_input, STATE_VS_TEXT,
)
from handlers.logo import (
    logo_command, logo_callback, handle_logo_upload, init_logo_table,
    STATE_LOGO_UPLOAD,
)
from handlers.admin import (
    panel_command, admin_panel_callback, stats_command, users_command, topusers_command,
    broadcast_command, cancelbroadcast_command, ban_command, unban_command,
    reports_command, referrals_command, status_command,
    reply_command, report_reply_command, closereport_command,
    maintenance_command, rpt_inline_callback,
    addpoints_command, removepoints_command, search_command,
    admin_search_handler, activity_feed_command,
)
from workers.cleanup import cleanup_temp_files, cleanup_old_cache
from workers.heartbeat import run_heartbeat
from workers.crash_monitor import record_startup, record_crash

# All main-menu button texts (EN + AR) for routing
MENU_BUTTONS = {
    "📥 Download",    "📥 تحميل",
    "👤 Profile",     "👤 حسابي",
    "🎁 Referrals",   "🎁 الدعوات",
    "🏆 Achievements","🏆 الإنجازات",
    "⭐ Favorites",   "⭐ المفضلة",
    "📞 Support",     "📞 الدعم",
    "👑 Admin Panel", "👑 لوحة الإدارة",
    "🎰 Lucky Wheel", "🎰 عجلة الحظ",
}


async def _handle_pending_rpt_reply(update: Update, context) -> bool:
    """Send admin's inline-reply text to the report author and close the report."""
    pending = context.user_data.get("pending_rpt_reply")
    if not pending:
        return False
    from middlewares.auth import is_admin
    if not is_admin(update.effective_user.id):
        return False
    context.user_data.pop("pending_rpt_reply", None)
    report_id      = pending["report_id"]
    target_user_id = pending["target_user_id"]
    reply_text     = (update.message.text or "").strip()
    if not reply_text:
        return False
    from database.reports import reply_report, get_report_by_id
    from database.users import get_user
    from locales import t
    db_user = get_user(target_user_id)
    lang    = db_user.get("language", "en") if db_user else "en"
    try:
        await context.bot.send_message(
            target_user_id,
            t(lang, "report_reply_header") + reply_text,
            parse_mode="HTML"
        )
    except Exception:
        pass
    reply_report(report_id, update.effective_user.id, reply_text)
    admin_db  = get_user(update.effective_user.id)
    admin_lang = admin_db.get("language", "en") if admin_db else "en"
    await update.message.reply_text(t(admin_lang, "admin_report_replied_ok"))
    return True


async def message_router(update: Update, context):
    """Route text messages — handle admin pending replies, support/report replies, menu buttons, URLs."""
    if await _handle_pending_rpt_reply(update, context):
        return

    if context.user_data.get("waiting_for_admin_search"):
        handled = await admin_search_handler(update, context)
        if handled:
            return

    if context.user_data.get("waiting_for") == "report":
        handled = await handle_report_message(update, context)
        if handled:
            return

    if context.user_data.get("waiting_for") == "support":
        handled = await handle_support_message(update, context)
        if handled:
            return

    # Video studio text overlay
    if context.user_data.get("vs_state") == STATE_VS_TEXT:
        handled = await handle_studio_text_input(update, context)
        if handled:
            return

    text = (update.message.text or "").strip()

    # --- Main menu button routing ---
    if text in MENU_BUTTONS:
        from database.users import get_user
        from locales import t
        db_user = get_user(update.effective_user.id)
        lang = db_user.get("language", "en") if db_user else "en"

        if text in (t("en", "menu_download"), t("ar", "menu_download")):
            await update.message.reply_text(t(lang, "send_url_prompt"), parse_mode="HTML")

        elif text in (t("en", "menu_profile"), t("ar", "menu_profile")):
            await profile_command(update, context)

        elif text in (t("en", "menu_referrals"), t("ar", "menu_referrals")):
            await referral_command(update, context)

        elif text in (t("en", "menu_achievements"), t("ar", "menu_achievements")):
            await achievements_command(update, context)

        elif text in (t("en", "menu_favorites"), t("ar", "menu_favorites")):
            await favorites_command(update, context)

        elif text in (t("en", "menu_wheel"), t("ar", "menu_wheel")):
            await wheel_command(update, context)

        elif text in (t("en", "menu_support"), t("ar", "menu_support")):
            await support_command(update, context)

        elif text in (t("en", "menu_admin"), t("ar", "menu_admin")):
            from middlewares.auth import is_admin
            from config.settings import ADMIN_BOT_USERNAME
            if is_admin(update.effective_user.id):
                if ADMIN_BOT_USERNAME:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    lang = update.effective_user.language_code or "en"
                    await update.message.reply_text(
                        "👑 <b>Admin Panel</b>\n\nOpen the dedicated Admin Bot:",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "🚀 Open Admin Bot",
                                url=f"https://t.me/{ADMIN_BOT_USERNAME}"
                            )
                        ]])
                    )
                else:
                    await panel_command(update, context)
        return

    # --- URL detection ---
    if text.startswith(("http://", "https://")):
        await handle_url(update, context)


async def post_init(application: Application):
    system_logger.info("Bot starting up...")
    init_logo_table()
    asyncio.create_task(cleanup_temp_files())
    asyncio.create_task(cleanup_old_cache())
    asyncio.create_task(run_heartbeat())
    system_logger.info("Background workers started.")


def build_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            CHOOSING_LANGUAGE: [
                CallbackQueryHandler(language_callback, pattern="^lang_"),
            ]
        },
        fallbacks=[CommandHandler("start", start_command)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )

    app.add_handler(start_conv)
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("settings", settings_command))

    app.add_handler(CommandHandler("profile",      profile_command))
    app.add_handler(CommandHandler("history",      history_command))
    app.add_handler(CommandHandler("referral",     referral_command))
    app.add_handler(CommandHandler("points",       points_command))
    app.add_handler(CommandHandler("daily",        daily_command))
    app.add_handler(CommandHandler("wheel",        wheel_command))
    app.add_handler(CommandHandler("achievements", achievements_command))
    app.add_handler(CommandHandler("leaderboard",  leaderboard_command))
    app.add_handler(CommandHandler("goals",        goals_command))
    app.add_handler(CommandHandler("favorites",    favorites_command))
    app.add_handler(CommandHandler("support",      support_command))
    app.add_handler(CommandHandler("videotools",   video_tools_command))
    app.add_handler(CommandHandler("studio",       studio_command))
    app.add_handler(CommandHandler("logo",         logo_command))

    app.add_handler(CommandHandler("panel",           panel_command))
    app.add_handler(CommandHandler("stats",           stats_command))
    app.add_handler(CommandHandler("users",           users_command))
    app.add_handler(CommandHandler("topusers",        topusers_command))
    app.add_handler(CommandHandler("broadcast",       broadcast_command))
    app.add_handler(CommandHandler("cancelbroadcast", cancelbroadcast_command))
    app.add_handler(CommandHandler("ban",             ban_command))
    app.add_handler(CommandHandler("unban",           unban_command))
    app.add_handler(CommandHandler("reports",         reports_command))
    app.add_handler(CommandHandler("referrals",       referrals_command))
    app.add_handler(CommandHandler("closereport",     closereport_command))
    app.add_handler(CommandHandler("status",          status_command))
    app.add_handler(CommandHandler("maintenance",     maintenance_command))
    app.add_handler(CommandHandler("top",             top_command))
    app.add_handler(CommandHandler("addpoints",       addpoints_command))
    app.add_handler(CommandHandler("removepoints",    removepoints_command))
    app.add_handler(CommandHandler("search",          search_command))
    app.add_handler(CommandHandler("activity",        activity_feed_command))

    app.add_handler(MessageHandler(
        filters.Regex(r"^/reply_\d+_\d+"),
        reply_command
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/report_reply_\d+_\d+"),
        report_reply_command
    ))

    app.add_handler(CallbackQueryHandler(verify_subscription_callback, pattern="^verify_sub$"))
    app.add_handler(CallbackQueryHandler(language_callback,            pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(settings_callback,            pattern="^settings_"))
    app.add_handler(CallbackQueryHandler(profile_callback,             pattern="^prof_"))
    app.add_handler(CallbackQueryHandler(admin_panel_callback,         pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(rpt_inline_callback,          pattern="^rpt_"))
    app.add_handler(CallbackQueryHandler(video_tools_callback,         pattern="^vt_"))
    app.add_handler(CallbackQueryHandler(studio_callback,              pattern="^vs_"))
    app.add_handler(CallbackQueryHandler(logo_callback,                pattern="^logo_"))
    app.add_handler(CallbackQueryHandler(download_callback,            pattern="^dl_"))
    app.add_handler(CallbackQueryHandler(download_callback,            pattern="^fav_"))
    app.add_handler(CallbackQueryHandler(feedback_callback,            pattern="^fb_"))
    app.add_handler(CallbackQueryHandler(redeem_callback,              pattern="^redeem_"))
    app.add_handler(CallbackQueryHandler(leaderboard_callback,         pattern="^lb_"))
    app.add_handler(CallbackQueryHandler(unfav_callback,               pattern="^unfav_"))
    app.add_handler(CallbackQueryHandler(wheel_callback,               pattern="^wheel_"))

    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO,
        handle_video_for_studio
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO | (filters.Document.IMAGE),
        handle_logo_upload
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        message_router
    ))

    return app


def main():
    init_db()
    system_logger.info("Database initialized.")
    record_startup()

    app = build_application()
    system_logger.info("Bot polling started.")
    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except Exception as exc:
        record_crash(exc)
        raise


if __name__ == "__main__":
    main()
