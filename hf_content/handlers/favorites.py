from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database.users import get_user
from database.favorites import get_favorites, remove_favorite
from middlewares.subscription_gate import require_subscription
from locales import t
from utils.helpers import truncate_title


@require_subscription
async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en")

    favs = get_favorites(user.id)

    if not favs:
        await update.message.reply_text(t(lang, "favorites_empty"))
        return

    text = t(lang, "favorites_title")
    keyboard = []
    for i, fav in enumerate(favs[:20]):
        title = truncate_title(fav.get("title", "Unknown"), 35)
        platform = fav.get("platform", "")
        date = str(fav.get("added_at", ""))[:10]
        text += f"{i+1}. <b>{title}</b> — {platform} ({date})\n"
        keyboard.append([
            InlineKeyboardButton(f"📥 {title}", url=fav["url"]),
            InlineKeyboardButton("🗑", callback_data=f"unfav_{i}"),
        ])
        context.user_data[f"fav_{i}_url"] = fav["url"]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def unfav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    # Re-verify subscription before any action
    from services.subscription import check_subscription
    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    idx = int(query.data.replace("unfav_", ""))
    url = context.user_data.get(f"fav_{idx}_url", "")

    if url:
        remove_favorite(user.id, url)
        # Single answer call — with alert confirming removal
        await query.answer(t(lang, "favorite_removed"), show_alert=True)
    else:
        await query.answer()

    favs = get_favorites(user.id)
    if not favs:
        await query.edit_message_text(t(lang, "favorites_empty"))
        return

    text = t(lang, "favorites_title")
    keyboard = []
    for i, fav in enumerate(favs[:20]):
        title = truncate_title(fav.get("title", "Unknown"), 35)
        platform = fav.get("platform", "")
        date = str(fav.get("added_at", ""))[:10]
        text += f"{i+1}. <b>{title}</b> — {platform} ({date})\n"
        keyboard.append([
            InlineKeyboardButton(f"📥 {title}", url=fav["url"]),
            InlineKeyboardButton("🗑", callback_data=f"unfav_{i}"),
        ])
        context.user_data[f"fav_{i}_url"] = fav["url"]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
