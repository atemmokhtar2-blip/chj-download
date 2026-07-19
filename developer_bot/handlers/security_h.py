from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import log_action, get_recent_actions


def security_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 سجل الإجراءات",      callback_data="dv_sec_log")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",  callback_data="dv_menu")],
    ])


async def show_security_menu(query, context):
    await query.edit_message_text(
        "🔐 <b>الأمان</b>\n\n"
        "جميع الإجراءات تُسجَّل تلقائياً.\n"
        "يُطلب تأكيد قبل أي إجراء حذف أو إعادة تشغيل.",
        parse_mode="HTML",
        reply_markup=security_kb(),
    )


async def show_action_log(query, context):
    uid = query.from_user.id
    actions = get_recent_actions(20)
    log_action(uid, "view_action_log", "", "ok")

    if not actions:
        text = "📋 <b>سجل الإجراءات</b>\n\nلا توجد إجراءات مسجَّلة."
    else:
        lines = []
        for a in actions:
            icon = "✅" if a["result"] == "ok" else "❌"
            lines.append(
                f"{icon} <b>{a['action']}</b>\n"
                f"   📝 {a['detail'] or '—'}  |  📅 {a['ts'][:16]}"
            )
        text = "📋 <b>سجل الإجراءات الأخيرة</b>\n\n" + "\n\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 تحديث",             callback_data="dv_sec_log")],
            [InlineKeyboardButton("🔙 الأمان",             callback_data="dv_security")],
        ]),
    )
