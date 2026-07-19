import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database.users import (
    get_user, create_user, get_user_by_referral,
    update_user
)
from database.referrals import (
    create_pending_referral, get_referral_by_referred, log_audit
)
from services.subscription import check_subscription, build_subscription_keyboard
from middlewares.auth import is_admin
from locales import t
from config.settings import REQUIRED_CHANNEL
from utils.helpers import get_display_name

logger = logging.getLogger(__name__)

CHOOSING_LANGUAGE = 1


def get_main_keyboard(lang: str, user_id: int = None) -> ReplyKeyboardMarkup:
    if lang == "ar":
        buttons = [
            [t("ar", "menu_download"),    t("ar", "menu_profile")],
            [t("ar", "menu_referrals"),   t("ar", "menu_achievements")],
            [t("ar", "menu_wheel"),       t("ar", "menu_favorites")],
            [t("ar", "menu_support")],
        ]
        if user_id and is_admin(user_id):
            buttons.append([t("ar", "menu_admin")])
    else:
        buttons = [
            [t("en", "menu_download"),    t("en", "menu_profile")],
            [t("en", "menu_referrals"),   t("en", "menu_achievements")],
            [t("en", "menu_wheel"),       t("en", "menu_favorites")],
            [t("en", "menu_support")],
        ]
        if user_id and is_admin(user_id):
            buttons.append([t("en", "menu_admin")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []

    ref_code = args[0].replace("REF_", "") if args and args[0].startswith("REF_") else None

    db_user = get_user(user.id)

    if not db_user:
        # Resolve referrer — reject self-referral at lookup time
        referred_by = None
        if ref_code:
            referrer = get_user_by_referral(ref_code)
            if referrer and referrer["user_id"] != user.id:
                referred_by = referrer["user_id"]
            elif referrer and referrer["user_id"] == user.id:
                log_audit("rejected_self", user.id, user.id, "Self-referral attempt")

        create_user(
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            referred_by=referred_by
        )

        # Create the pending referral row immediately — UNIQUE(referred_id)
        # guarantees this can only ever succeed once per new user.
        if referred_by:
            created = create_pending_referral(referred_by, user.id)
            if created:
                log_audit("registered", referred_by, user.id,
                          f"New user registered via referral code {ref_code}")
            else:
                log_audit("rejected_duplicate", referred_by, user.id,
                          "Referral row already existed")

        keyboard = [[
            InlineKeyboardButton("🇺🇸 English", callback_data="lang_en"),
            InlineKeyboardButton("🇸🇦 العربية", callback_data="lang_ar"),
        ]]
        await update.message.reply_text(
            t("en", "choose_language"),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return CHOOSING_LANGUAGE

    lang = db_user.get("language", "en")

    if db_user.get("is_banned"):
        await update.message.reply_text(t(lang, "banned"))
        return ConversationHandler.END

    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await send_subscription_prompt(update, lang)
        return ConversationHandler.END

    await update.message.reply_text(
        t(lang, "welcome_back"),
        parse_mode="HTML",
        reply_markup=get_main_keyboard(lang, user.id)
    )
    return ConversationHandler.END


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    lang = "ar" if query.data == "lang_ar" else "en"

    db_user = get_user(user.id)
    if not db_user:
        db_user = create_user(
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or ""
        )

    update_user(user.id, language=lang)
    await query.answer()
    await query.edit_message_text(t(lang, "language_set"))

    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await send_subscription_prompt_message(query.message.chat_id, context, lang)
        return ConversationHandler.END

    # User is already subscribed at language-pick time — log the event
    # but do NOT credit the referral yet: that only happens after first download.
    referral = get_referral_by_referred(user.id)
    if referral and referral["status"] == "pending":
        log_audit("subscribed", referral["referrer_id"], user.id,
                  "Subscribed before first download (reward pending)")

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=t(lang, "welcome"),
        parse_mode="HTML",
        reply_markup=get_main_keyboard(lang, user.id)
    )
    return ConversationHandler.END


async def send_subscription_prompt(update: Update, lang: str):
    keyboard = await build_subscription_keyboard(lang)
    await update.message.reply_text(
        t(lang, "join_channel"),
        reply_markup=keyboard,
        parse_mode="HTML"
    )


async def send_subscription_prompt_message(chat_id: int, context, lang: str):
    keyboard = await build_subscription_keyboard(lang)
    await context.bot.send_message(
        chat_id=chat_id,
        text=t(lang, "join_channel"),
        reply_markup=keyboard,
        parse_mode="HTML"
    )


async def verify_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    subscribed = await check_subscription(context.bot, user.id)

    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    await query.answer()

    # Log subscription event — referral reward still not given yet (first download required)
    referral = get_referral_by_referred(user.id)
    if referral and referral["status"] == "pending":
        log_audit("subscribed", referral["referrer_id"], user.id,
                  "Verified channel subscription (reward pending first download)")

    await query.edit_message_text(t(lang, "subscribed_ok"))
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=t(lang, "welcome"),
        parse_mode="HTML",
        reply_markup=get_main_keyboard(lang, user.id)
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    await update.message.reply_text(t(lang, "help_text"), parse_mode="HTML")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    lang_display = "العربية" if lang == "ar" else "English"

    keyboard = [[
        InlineKeyboardButton(t(lang, "settings_lang_btn"), callback_data="settings_lang")
    ]]

    await update.message.reply_text(
        t(lang, "settings_text", language=lang_display),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    await query.answer()

    if query.data == "settings_lang":
        keyboard = [[
            InlineKeyboardButton("🇺🇸 English", callback_data="lang_en"),
            InlineKeyboardButton("🇸🇦 العربية", callback_data="lang_ar"),
        ]]
        await query.edit_message_text(
            t("en", "choose_language"),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
