import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    InlineQueryHandler, filters
)

from config.settings import BOT_TOKEN
from database.db import init_db
from utils.logger import system_logger
from utils.helpers import summarize_error

from handlers.start import (
    start_command, language_callback,
    help_command,
)
from handlers.settings_menu import settings_command, settings_callback, try_handle_settings_text
from handlers.download import (
    handle_url, download_callback, handle_batch_urls, _extract_urls
)
from handlers.search import search_command, search_callback, inline_search
from handlers.admin import (
    admin_command, admin_callback, send_update_announcement,
    engine_status_command, update_ytdlp_command, health_command,
    callbacks_health_command, commands_command,
)
from workers.cleanup import cleanup_temp_files, cleanup_old_cache, cleanup_old_history

# Arabic-only main menu.
MENU_BUTTONS = {"📥 تحميل", "📥 Download", "⚙️ الإعدادات", "⚙️ Settings"}


async def ping_command(update: Update, context):
    """Lightweight liveness check for every user."""
    await update.effective_message.reply_text("🏓 البوت يعمل حاليًا.")


async def noop_callback(update: Update, context):
    """Acknowledge decorative/disabled inline buttons without side effects."""
    if update.callback_query:
        await update.callback_query.answer()


async def unknown_callback(update: Update, context):
    """Final safety net: no inline button should ever spin forever."""
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    user_id = query.from_user.id if query.from_user else "unknown"
    system_logger.warning("Unknown callback received: user=%s data=%r", user_id, data)
    await query.answer("انتهت صلاحية الجلسة. استخدم /start مرة أخرى.", show_alert=True)


async def message_router(update: Update, context):
    """Route text messages — handle menu buttons and URLs."""
    text = (update.message.text or "").strip()

    if await try_handle_settings_text(update, context):
        return

    if text in MENU_BUTTONS:
        from database.users import get_user, update_user
        from locales import t
        user_id = update.effective_user.id
        db_user = get_user(user_id)
        if db_user:
            update_user(user_id, language="ar")
        download_btn = {t("ar", "menu_download"), t("en", "menu_download")}
        settings_btn = {t("ar", "menu_settings"), t("en", "menu_settings")}
        if text in download_btn:
            await update.message.reply_text(t("ar", "send_url_prompt"), parse_mode="HTML")
        elif text in settings_btn:
            # Open the real settings menu (same screen as /settings).
            await settings_command(update, context)
        return

    from handlers.admin import admin_text_handler
    if await admin_text_handler(update, context):
        return

    urls = _extract_urls(text)
    if len(urls) > 1:
        await handle_batch_urls(update, context)
    elif urls:
        await handle_url(update, context)


async def post_init(application: Application):
    system_logger.info("Bot starting up...")
    asyncio.create_task(cleanup_temp_files())
    asyncio.create_task(cleanup_old_cache())
    asyncio.create_task(cleanup_old_history())
    system_logger.info("Background workers started.")


async def global_error_handler(update: object, context) -> None:
    """Catch unhandled exceptions so the bot never dies silently.

    The real root cause is always written to the server logs via
    ``system_logger.exception``, and now a short, safe version is also shown to
    the user so they can report/understand exactly what went wrong.
    """
    err = context.error
    system_logger.exception("Unhandled error: %s", err)
    try:
        from utils.helpers import summarize_error  # defensive: the handler must never crash itself
        reason = summarize_error(err)
    except Exception:
        reason = str(err or "Unknown error")[:220]
    try:
        if update and getattr(update, "effective_message", None):
            from locales import t
            await update.effective_message.reply_text(
                t("ar", "unexpected_error", reason=reason)
            )
    except Exception:
        pass


def build_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # No language-selection conversation: /start immediately opens the Arabic bot.
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("ping", ping_command))

    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", admin_command))
    app.add_handler(CommandHandler("broadcast", admin_command))
    app.add_handler(CommandHandler("ban", admin_command))
    app.add_handler(CommandHandler("unban", admin_command))
    app.add_handler(CommandHandler("users", admin_command))
    app.add_handler(CommandHandler("search", admin_command))
    app.add_handler(CommandHandler("system", admin_command))
    app.add_handler(CommandHandler("maintenance", admin_command))
    app.add_handler(CommandHandler("announce_update", send_update_announcement))
    app.add_handler(CommandHandler("engine_status", engine_status_command))
    app.add_handler(CommandHandler("update_ytdlp", update_ytdlp_command))
    app.add_handler(CommandHandler("health", health_command))
    app.add_handler(CommandHandler("callbacks_health", callbacks_health_command))
    app.add_handler(CommandHandler("commands", commands_command))
    app.add_handler(CommandHandler("find", search_command))

    app.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    # Legacy buttons are handled without allowing a language switch.
    app.add_handler(CallbackQueryHandler(language_callback, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^settings"))
    app.add_handler(CallbackQueryHandler(search_callback, pattern="^search_"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern="^dl_"))
    app.add_handler(CallbackQueryHandler(unknown_callback))
    app.add_handler(InlineQueryHandler(inline_search))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))
    app.add_error_handler(global_error_handler)
    return app


def main():
    init_db()
    system_logger.info("Database initialized.")
    app = build_application()
    system_logger.info("Bot polling started.")
    app.run_polling(
        stop_signals=False,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
