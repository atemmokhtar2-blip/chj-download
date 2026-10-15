import logging
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from utils.button_text import strip_button_emoji
from telegram.ext import ContextTypes

from config.settings import MAIN_BOT_USERNAME
from database.users import get_user, create_user, update_user
from middlewares.auth import is_admin
from locales import t
from services.instance_manager import is_clone_bot

logger = logging.getLogger(__name__)


def get_main_keyboard(lang: str = "ar", user_id: int = None, bot_username: str = None) -> ReplyKeyboardMarkup:
    """Permanent Arabic shortcuts: Download + Settings.

    Inside a clone instance (any bot whose username differs from
    MAIN_BOT_USERNAME) a third permanent button links back to the official
    main bot — required by the network design so every clone feeds users
    back to the source without nagging messages.
    """
    rows = [[t("ar", "menu_download"), t("ar", "menu_settings")]]
    try:
        if MAIN_BOT_USERNAME and is_clone_bot(bot_username):
            from telegram import KeyboardButton
            rows.append([KeyboardButton(
                "🏠 العودة إلى البوت الأساسي",
                url=f"https://t.me/{MAIN_BOT_USERNAME}",
            )])
    except Exception:
        pass
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    is_new = db_user is None

    if not db_user:
        create_user(
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            language="ar",
        )
        db_user = get_user(user.id)

    # Force Arabic for both new and existing users, including accounts that
    # previously selected English.
    update_user(user.id, language="ar")

    if db_user and db_user.get("is_banned"):
        await update.message.reply_text(t("ar", "banned"))
        return

    # ---- referral network registration ----------------------------------
    # Every start touches the referral registry (activity-day tracking can
    # qualify pending referrals via the 2-day rule). Brand-new users may
    # carry a ref_ deep-link code and get attributed ONCE, permanently.
    ref_note = ""
    try:
        from services import referral
        bot_username = context.bot.username or ""
        source = f"clone:{context.bot.id}" if is_clone_bot(bot_username) else "main"
        referral.register_user(user, is_new=is_new, source=source)
        if is_new:
            code = None
            if context.args:
                code = str(context.args[0]).strip()
            elif update.message and update.message.text and "ref_" in update.message.text:
                # Some clients deliver the payload inside the message text.
                for part in update.message.text.split():
                    if part.startswith("ref_"):
                        code = part.strip()
                        break
            bind = referral.bind_referral(code, user.id) if code else "no_code"
            if bind in ("bound", "pending", "suspicious"):
                ref_note = "\n\n🎁 تم تسجيلك عبر رابط دعوة — أهلًا بك!"
            elif bind == "self":
                logger.info("Self-referral blocked uid=%s", user.id)
    except Exception:
        logger.exception("Referral registration failed uid=%s", user.id)

    # Panels appear ONLY for privileged accounts — regular users never see
    # any panel button or screen, anywhere in the bot.
    #   * OWNER (you)      → the Owner Control Center opens immediately on /start.
    #   * regular admin    → the admin panel opens immediately on /start.
    if is_owner(user.id):
        from handlers.owner_panel import owner_command  # local import: avoids a circular dependency
        await owner_command(update, context)
        return

    if is_admin(user.id):
        from handlers.admin import admin_command  # local import: avoids a circular dependency
        await admin_command(update, context)
        return

    await update.message.reply_text(
        t("ar", "welcome_back") + ref_note,
        parse_mode="HTML",
        reply_markup=get_main_keyboard("ar", user.id),
    )

    # Inside a clone instance, new users always see the way back to the
    # main bot (sent once per process session so it never becomes spam).
    try:
        if is_clone_bot(context.bot.username or "") and MAIN_BOT_USERNAME and \
                not context.user_data.get("back_to_main_sent"):
            context.user_data["back_to_main_sent"] = True
            await update.message.reply_text(
                "🏠 هذا البوت نسخة تعمل ضمن شبكة البوت الأساسي.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "العودة إلى البوت الأساسي",
                        url=f"https://t.me/{MAIN_BOT_USERNAME}",
                    ),
                ]]),
            )
    except Exception:
        pass


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep old language buttons harmless; the bot now uses Arabic only."""
    query = update.callback_query
    update_user(query.from_user.id, language="ar")
    await query.answer("اللغة العربية ثابتة في البوت.")
    try:
        await query.edit_message_text("✅ تم ضبط لغة البوت على العربية.")
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=t("ar", "welcome"),
        parse_mode="HTML",
        reply_markup=get_main_keyboard("ar", query.from_user.id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    if db_user:
        update_user(user.id, language="ar")
    await update.message.reply_text(t("ar", "help_text"), parse_mode="HTML")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    if db_user:
        update_user(user.id, language="ar")
    await update.message.reply_text(
        t("ar", "settings_text", language="العربية"),
        parse_mode="HTML",
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "settings_lang":
        await query.answer("اللغة العربية ثابتة في البوت.", show_alert=True)
        try:
            await query.edit_message_text("⚙️ لغة البوت ثابتة على العربية.")
        except Exception:
            pass
        return
    await query.answer("هذا الزر لم يعد متاحًا.", show_alert=True)
