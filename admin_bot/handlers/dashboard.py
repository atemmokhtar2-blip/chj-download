from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from handlers.auth import require_admin
from database.db import get_admin_lang
from locales import t
from utils.logger import system_logger


def main_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t(lang, "btn_stats"),     callback_data="adm_stats"),
            InlineKeyboardButton(t(lang, "btn_users"),     callback_data="adm_users"),
        ],
        [
            InlineKeyboardButton(t(lang, "btn_broadcast"), callback_data="adm_bcast"),
            InlineKeyboardButton(t(lang, "btn_support"),   callback_data="adm_sup"),
        ],
        [
            InlineKeyboardButton(t(lang, "btn_downloads"), callback_data="adm_dl"),
            InlineKeyboardButton(t(lang, "btn_security"),  callback_data="adm_sec"),
        ],
        [
            InlineKeyboardButton(t(lang, "btn_settings"),  callback_data="adm_settings"),
            InlineKeyboardButton(t(lang, "btn_lang_toggle"), callback_data="adm_lang_toggle"),
        ],
    ])


def back_button(lang: str = "en", callback: str = "adm_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "btn_back"), callback_data=callback)]])


def back_row(lang: str = "en", callback: str = "adm_menu") -> list:
    return [InlineKeyboardButton(t(lang, "btn_back"), callback_data=callback)]


def panel_text(lang: str) -> str:
    return f"{t(lang, 'panel_title')}\n\n{t(lang, 'panel_subtitle')}"


@require_admin
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid  = update.effective_user.id
    lang = get_admin_lang(uid)
    system_logger.info("Admin panel opened by user_id=%s lang=%s", uid, lang)
    await update.message.reply_text(
        panel_text(lang), parse_mode="HTML",
        reply_markup=main_menu_keyboard(lang)
    )


@require_admin
async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    uid  = update.effective_user.id
    lang = get_admin_lang(uid)
    await update.message.reply_text(
        panel_text(lang), parse_mode="HTML",
        reply_markup=main_menu_keyboard(lang)
    )


async def show_main_menu(query, context):
    context.user_data.clear()
    uid  = query.from_user.id
    lang = get_admin_lang(uid)
    await query.edit_message_text(
        panel_text(lang), parse_mode="HTML",
        reply_markup=main_menu_keyboard(lang)
    )
