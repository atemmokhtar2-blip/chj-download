from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import (
    get_admin_lang, set_admin_lang,
    get_setting, set_setting,
)
from config.settings import (
    REQUIRED_CHANNEL, SUPPORT_BOT_USERNAME, OWNER_ID, ADMIN_IDS
)
from locales import t
from utils.logger import error_logger


async def show_settings(query, context):
    uid  = query.from_user.id
    lang = get_admin_lang(uid)

    admin_list = ", ".join(str(x) for x in ADMIN_IDS) if ADMIN_IDS else "none"
    support_un = f"@{SUPPORT_BOT_USERNAME}" if SUPPORT_BOT_USERNAME else "not set"

    text = (
        f"{t(lang, 'settings_title')}\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"{t(lang, 'settings_access')}\n"
        f"  OWNER_ID:            <code>{OWNER_ID}</code>\n"
        f"  ADMIN_IDS:           <code>{admin_list}</code>\n\n"
        f"{t(lang, 'settings_integrations')}\n"
        f"  REQUIRED_CHANNEL:    <code>{REQUIRED_CHANNEL or 'not set'}</code>\n"
        f"  SUPPORT_BOT:         <code>{support_un}</code>\n\n"
        f"{t(lang, 'settings_note')}"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_lang_toggle"), callback_data="adm_lang_toggle")],
        [InlineKeyboardButton(t(lang, "btn_back"),        callback_data="adm_menu")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def toggle_language(query, context):
    uid      = query.from_user.id
    cur_lang = get_admin_lang(uid)
    new_lang = "ar" if cur_lang == "en" else "en"
    set_admin_lang(uid, new_lang)
    await query.answer(
        "تم التبديل للعربية ✅" if new_lang == "ar" else "Switched to English ✅",
        show_alert=False
    )
    # Redraw main menu in new language
    from handlers.dashboard import panel_text, main_menu_keyboard
    await query.edit_message_text(
        panel_text(new_lang), parse_mode="HTML",
        reply_markup=main_menu_keyboard(new_lang)
    )
