from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import get_setting, set_setting

SETTING_KEY = "vodafone_cash_number"


def get_vodafone_number() -> str:
    return get_setting(SETTING_KEY, "")


def save_vodafone_number(number: str) -> None:
    set_setting(SETTING_KEY, number.strip())


def _keyboard(has_number: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Change Number", callback_data="adm_vcash_change")],
        [InlineKeyboardButton("⬅️ Back", callback_data="adm_menu")],
    ]
    return InlineKeyboardMarkup(rows)


def _menu_text(number: str) -> str:
    if number:
        display = f"<code>{number}</code>"
    else:
        display = "<i>Not configured</i>"
    return (
        "💳 <b>Vodafone Cash Manager</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"📱 <b>Current Number:</b> {display}\n\n"
        "This number is shown to users in payment instructions.\n"
        "Use <b>Change Number</b> to update it."
    )


async def show_vcash_menu(query, context):
    number = get_vodafone_number()
    await query.edit_message_text(
        _menu_text(number),
        parse_mode="HTML",
        reply_markup=_keyboard(bool(number))
    )


async def prompt_change_number(query, context):
    context.user_data["adm_state"] = "vcash_number"
    await query.edit_message_text(
        "💳 <b>Change Vodafone Cash Number</b>\n\n"
        "Send the new Vodafone Cash number.\n"
        "Example: <code>01012345678</code>\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="adm_vcash")]
        ])
    )


async def handle_vcash_number_input(update, context):
    number = (update.message.text or "").strip()
    context.user_data.pop("adm_state", None)

    if not number.lstrip("+").isdigit() or len(number.lstrip("+")) < 7:
        await update.message.reply_text(
            "❌ Invalid number. Please send digits only (e.g. <code>01012345678</code>).\n"
            "Use /panel to return to the menu.",
            parse_mode="HTML"
        )
        return

    save_vodafone_number(number)
    await update.message.reply_text(
        f"✅ <b>Vodafone Cash number updated.</b>\n\n"
        f"📱 New number: <code>{number}</code>\n\n"
        "Use /panel to return to the menu.",
        parse_mode="HTML"
    )
