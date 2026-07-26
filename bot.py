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
print(f"DEBUG: Token starts with: {BOT_TOKEN[:10]}...")
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
from handlers.favorites import favorites_command, unfav_callback
from handlers.admin import (
    admin_command, admin_callback, admin_text_handler
)
from workers.cleanup import cleanup_temp_files, cleanup_old_cache
from workers.heartbeat import run_heartbeat
from workers.crash_monitor import record_startup, record_crash

# All main-menu button texts (EN + AR) for routing
MENU_BUTTONS = {
    "📥 Download",    "📥 تحميل",
    "👤 Profile",     "👤 حسابي",

    "🏆 Achievements","🏆 الإنجازات",
    "⭐ Favorites",   "⭐ المفضلة",
    "🎰 Lucky Wheel", "🎰 عجلة الحظ",
}


async def message_router(update: Update, context):
    """Route text messages — handle menu buttons and URLs."""
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



        elif text in (t("en", "menu_achievements"), t("ar", "menu_achievements")):
            await achievements_command(update, context)

        elif text in (t("en", "menu_favorites"), t("ar", "menu_favorites")):
            await favorites_command(update, context)

        elif text in (t("en", "menu_wheel"), t("ar", "menu_wheel")):
            await wheel_command(update, context)

        return

    # --- Admin text handler (for conversations like search, broadcast, etc.) ---
    from handlers.admin import admin_text_handler
    await admin_text_handler(update, context)

    # --- URL detection ---
    if text.startswith(("http://", "https://")):
        await handle_url(update, context)


async def post_init(application: Application):
    system_logger.info("Bot starting up...")
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

    app.add_handler(CommandHandler("points",       points_command))
    app.add_handler(CommandHandler("daily",        daily_command))
    app.add_handler(CommandHandler("wheel",        wheel_command))
    app.add_handler(CommandHandler("achievements", achievements_command))
    app.add_handler(CommandHandler("leaderboard",  leaderboard_command))
    app.add_handler(CommandHandler("goals",        goals_command))
    app.add_handler(CommandHandler("favorites",    favorites_command))
    app.add_handler(CommandHandler("top",          top_command))

    # Admin handlers
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", admin_command))
    app.add_handler(CommandHandler("broadcast", admin_command))
    app.add_handler(CommandHandler("ban", admin_command))
    app.add_handler(CommandHandler("unban", admin_command))
    app.add_handler(CommandHandler("users", admin_command))
    app.add_handler(CommandHandler("search", admin_command))
    app.add_handler(CommandHandler("system", admin_command))
    app.add_handler(CommandHandler("maintenance", admin_command))
    app.add_handler(CommandHandler("points", admin_command))
    app.add_handler(CommandHandler("vip", admin_command))

    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

    app.add_handler(CallbackQueryHandler(verify_subscription_callback, pattern="^verify_sub$"))
    app.add_handler(CallbackQueryHandler(language_callback,            pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(settings_callback,            pattern="^settings_"))
    app.add_handler(CallbackQueryHandler(profile_callback,             pattern="^prof_"))
    app.add_handler(CallbackQueryHandler(redeem_callback,              pattern="^redeem_"))
    app.add_handler(CallbackQueryHandler(leaderboard_callback,         pattern="^lb_"))
    app.add_handler(CallbackQueryHandler(unfav_callback,               pattern="^unfav_"))
    app.add_handler(CallbackQueryHandler(wheel_callback,               pattern="^wheel_"))
    app.add_handler(CallbackQueryHandler(download_callback,            pattern="^dl_"))
    app.add_handler(CallbackQueryHandler(download_callback,            pattern="^fav_"))

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
        app.run_polling(stop_signals=False,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except Exception as exc:
        record_crash(exc)
        raise


if __name__ == "__main__":
    main()
