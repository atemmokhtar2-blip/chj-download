from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import get_stats, get_admin_lang
from locales import t
from utils.logger import error_logger


def build_stats_text(lang: str) -> str:
    try:
        s = get_stats()
    except Exception as exc:
        error_logger.error("Failed to load stats: %s", exc)
        return "❌ Failed to load statistics. Please try again."

    return (
        f"{t(lang, 'stats_title')}\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"{t(lang, 'stats_users')}\n"
        f"{t(lang, 'stats_total_users')}   <b>{s['total_users']:,}</b>\n"
        f"{t(lang, 'stats_new_today')}      <b>{s['new_users_today']:,}</b>\n"
        f"{t(lang, 'stats_banned')}         <b>{s['banned_users']:,}</b>\n\n"
        f"{t(lang, 'stats_downloads')}\n"
        f"{t(lang, 'stats_total_dl')}   <b>{s['total_downloads']:,}</b>\n"
        f"{t(lang, 'stats_dl_today')}   <b>{s['dl_today']:,}</b>\n\n"
        f"{t(lang, 'stats_referrals')}\n"
        f"{t(lang, 'stats_total_ref')}   <b>{s['total_referrals']:,}</b>\n\n"
        f"{t(lang, 'stats_support')}\n"
        f"{t(lang, 'stats_total_tix')}   <b>{s['total_tickets']:,}</b>\n"
        f"{t(lang, 'stats_open_tix')}   <b>{s['open_tickets']:,}</b>\n"
        "━━━━━━━━━━━━━━━━"
    )


async def show_stats(query, context):
    uid  = query.from_user.id
    lang = get_admin_lang(uid)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang, "btn_refresh"), callback_data="adm_stats"),
        InlineKeyboardButton(t(lang, "btn_back"),    callback_data="adm_menu"),
    ]])
    await query.edit_message_text(build_stats_text(lang), parse_mode="HTML", reply_markup=kb)
