import asyncio
import logging
import sys
import os

# Make support_bot/ the primary import root so its packages shadow the main bot's
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

from config.settings import SUPPORT_BOT_TOKEN
from database.db import init_db
from utils.logger import system_logger

from database.db import is_main_bot_user
from handlers.user import (
    start_cmd, new_ticket_start, my_tickets_cmd, cancel_cmd,
    create_ticket_from_message, handle_user_message,
    _BTN_NEW, _BTN_STATUS
)
from handlers.admin import (
    panel_cmd, list_open_cmd, list_closed_cmd,
    view_ticket_cmd, reply_cmd, close_cmd,
    admin_callback, handle_pending_admin_reply, is_admin
)


async def message_router(update: Update, context):
    """Single text-message router — ordered by priority."""
    user = update.effective_user
    if not user or not update.message or not update.message.text:
        return

    # Top-level security gate: block anyone not in the main bot DB
    # (defence in depth — individual handlers also check, but this
    #  stops unregistered users before any routing logic runs)
    if not is_admin(user.id) and not is_main_bot_user(user.id):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\n\n"
            "You must use the main bot before accessing support.\n"
            "Please start the main bot and try again.",
            parse_mode="HTML"
        )
        return

    text = update.message.text.strip()

    # 1. Admin pending reply intercept (highest priority)
    if context.user_data.get("pending_sup_reply") and is_admin(user.id):
        handled = await handle_pending_admin_reply(update, context)
        if handled:
            return

    # 2. User is mid-ticket-creation flow
    if context.user_data.get("state") == "creating_ticket":
        await create_ticket_from_message(update, context)
        return

    # 3. Main-menu button routing (bilingual)
    if text in (_BTN_NEW["en"], _BTN_NEW["ar"]):
        await new_ticket_start(update, context)
        return

    if text in (_BTN_STATUS["en"], _BTN_STATUS["ar"]):
        await my_tickets_cmd(update, context)
        return

    # 4. Any other text from a user → add to their open ticket (if any)
    await handle_user_message(update, context)


async def post_init(application: Application):
    system_logger.info("Support Bot starting up…")


def build_application() -> Application:
    if not SUPPORT_BOT_TOKEN:
        raise ValueError("SUPPORT_BOT_TOKEN is not set.")

    app = Application.builder().token(SUPPORT_BOT_TOKEN).post_init(post_init).build()

    # Admin commands (registered first so they always take priority)
    app.add_handler(CommandHandler("panel",   panel_cmd))
    app.add_handler(CommandHandler("tickets", list_open_cmd))
    app.add_handler(CommandHandler("open",    list_open_cmd))
    app.add_handler(CommandHandler("closed",  list_closed_cmd))
    app.add_handler(CommandHandler("ticket",  view_ticket_cmd))
    app.add_handler(CommandHandler("reply",   reply_cmd))
    app.add_handler(CommandHandler("close",   close_cmd))

    # User commands
    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # Inline callbacks for admin
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^sup_"))

    # All text messages → router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    return app


def main():
    init_db()
    system_logger.info("Support DB initialized.")
    system_logger.info("=== SUPPORT BOT STARTUP === PID:%s", os.getpid())

    app = build_application()
    system_logger.info("Support Bot polling started.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
