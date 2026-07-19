import logging
from datetime import datetime
from telegram import Update, ForceReply
from telegram.ext import ContextTypes

from database.users import get_user
from database.reports import log_feedback, create_report, create_support_ticket
from middlewares.subscription_gate import require_subscription
from locales import t
from config.settings import ADMIN_IDS, OWNER_ID

logger = logging.getLogger(__name__)


async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    from services.subscription import check_subscription
    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("fb_like_") or data.startswith("fb_dislike_"):
        download_id = int(data.split("_")[-1])
        rating = "like" if "like" in data else "dislike"
        log_feedback(user.id, download_id, rating)
        await query.edit_message_text(t(lang, "feedback_thanks"))
        return

    if data.startswith("fb_report_"):
        download_id = int(data.split("_")[-1])
        context.user_data["reporting_download_id"] = download_id
        # Edit the button message to a neutral state — report is NOT yet submitted
        await query.edit_message_text(t(lang, "report_started"))
        await query.message.reply_text(
            t(lang, "report_prompt"),
            reply_markup=ForceReply(selective=True)
        )
        context.user_data["waiting_for"] = "report"
        return


async def handle_report_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if context.user_data.get("waiting_for") != "report":
        return False

    context.user_data.pop("waiting_for", None)
    message_text = update.message.text

    info = context.user_data.get("current_info", {})
    platform = info.get("platform", "Unknown")
    url = info.get("url", "Unknown")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    report_id = create_report(
        user_id=user.id,
        username=user.username or "",
        platform=platform,
        url=url,
        message=message_text
    )

    username_display = f"@{user.username}" if user.username else "no username"
    admin_text = (
        f"🚨 <b>New Report #{report_id}</b>\n\n"
        f"👤 User: {user.first_name} ({username_display})\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"📺 Platform: {platform}\n"
        f"🔗 URL: {url}\n"
        f"🕐 Time: {now}\n"
        f"📝 Message:\n{message_text}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💬 Reply: <code>/report_reply_{report_id}_{user.id} your reply here</code>\n"
        f"✅ Close: <code>/closereport {report_id}</code>"
    )

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from locales import t as _t
    admin_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(_t("en", "admin_report_reply_btn"),
                             callback_data=f"rpt_reply_{report_id}_{user.id}"),
        InlineKeyboardButton(_t("en", "admin_report_close_btn"),
                             callback_data=f"rpt_close_{report_id}"),
    ]])

    all_admins = list(set(ADMIN_IDS + ([OWNER_ID] if OWNER_ID else [])))
    for admin_id in all_admins:
        try:
            await context.bot.send_message(
                admin_id, admin_text,
                parse_mode="HTML",
                reply_markup=admin_keyboard
            )
        except Exception:
            pass

    from handlers.start import get_main_keyboard
    await update.message.reply_text(
        t(lang, "report_sent"),
        reply_markup=get_main_keyboard(lang, user.id)
    )
    return True


@require_subscription
async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    from config.settings import SUPPORT_BOT_USERNAME
    if SUPPORT_BOT_USERNAME:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        btn_label = "💬 Open Support Bot" if lang == "en" else "💬 فتح بوت الدعم"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(btn_label,
                                 url=f"https://t.me/{SUPPORT_BOT_USERNAME}?start=from_{user.id}")
        ]])
        await update.message.reply_text(
            t(lang, "support_redirect"),
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        return

    context.user_data["waiting_for"] = "support"
    await update.message.reply_text(
        t(lang, "support_prompt"),
        reply_markup=ForceReply(selective=True)
    )


async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if context.user_data.get("waiting_for") != "support":
        return False

    context.user_data.pop("waiting_for", None)
    message_text = update.message.text

    ticket_id = create_support_ticket(user.id, message_text)
    username_display = f"@{user.username}" if user.username else "no username"

    admin_text = (
        f"💬 <b>Support Ticket #{ticket_id}</b>\n\n"
        f"👤 User: {user.first_name} ({username_display})\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"📝 Message:\n{message_text}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💬 Reply: <code>/reply_{ticket_id}_{user.id} your reply here</code>"
    )

    all_admins = list(set(ADMIN_IDS + ([OWNER_ID] if OWNER_ID else [])))
    for admin_id in all_admins:
        try:
            await context.bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception:
            pass

    from handlers.start import get_main_keyboard
    await update.message.reply_text(
        t(lang, "support_sent"),
        reply_markup=get_main_keyboard(lang, user.id)
    )
    return True

