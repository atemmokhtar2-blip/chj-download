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
    start_command, language_callback, 
    help_command, settings_command, settings_callback, CHOOSING_LANGUAGE
)
from handlers.download import handle_url, download_callback
from handlers.admin import (
    admin_command, admin_callback
)
from workers.cleanup import cleanup_temp_files, cleanup_old_cache

# All main-menu button texts (EN + AR) for routing
MENU_BUTTONS = {
    "📥 Download",    "📥 تحميل",
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
        return

    # --- Admin text handler ---
    from handlers.admin import admin_text_handler
    await admin_text_handler(update, context)

    # --- URL detection ---
    if text.startswith(("http://", "https://")):
        await handle_url(update, context)

async def post_init(application: Application):
    system_logger.info("Bot starting up...")
    asyncio.create_task(cleanup_temp_files())
    asyncio.create_task(cleanup_old_cache())
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

    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(language_callback,            pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(settings_callback,            pattern="^settings_"))
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
    app = build_application()
    system_logger.info("Bot polling started.")
    app.run_polling(stop_signals=False,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
