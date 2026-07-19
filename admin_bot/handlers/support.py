from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import (
    get_open_tickets, get_closed_tickets, get_ticket,
    get_ticket_messages, count_tickets, get_admin_lang,
)
from config.settings import SUPPORT_BOT_USERNAME
from locales import t

PAGE_SIZE = 8


def support_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    open_lbl   = "🟢 Open Tickets"   if lang == "en" else "🟢 تذاكر مفتوحة"
    closed_lbl = "✅ Closed Tickets" if lang == "en" else "✅ تذاكر مغلقة"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(open_lbl,           callback_data="adm_sup_open_0"),
            InlineKeyboardButton(closed_lbl,         callback_data="adm_sup_closed_0"),
        ],
        [InlineKeyboardButton(t(lang, "btn_back"),   callback_data="adm_menu")],
    ])


async def show_support_menu(query, context):
    lang     = get_admin_lang(query.from_user.id)
    open_c   = count_tickets("open")
    closed_c = count_tickets("closed")
    title    = t(lang, "stats_support")
    open_lbl = "🟢 Open" if lang == "en" else "🟢 مفتوحة"
    cls_lbl  = "✅ Closed" if lang == "en" else "✅ مغلقة"
    sup_link = (f"\n\n💬 {t(lang, 'support_redirect')} @{SUPPORT_BOT_USERNAME}"
                if SUPPORT_BOT_USERNAME else "")
    await query.edit_message_text(
        f"{title}\n\n{open_lbl}:   <b>{open_c}</b>\n{cls_lbl}: <b>{closed_c}</b>{sup_link}",
        parse_mode="HTML",
        reply_markup=support_menu_keyboard(lang),
    )


def _ticket_list_keyboard(lang: str, mode: str, offset: int, total: int, tickets: list) -> InlineKeyboardMarkup:
    buttons = []
    for tick in tickets:
        uname = f"@{tick.get('username')}" if tick.get("username") else tick.get("first_name", f"#{tick['id']}")
        buttons.append([InlineKeyboardButton(
            f"🎫 #{tick['id']} — {uname}",
            callback_data=f"adm_sup_ticket_{tick['id']}",
        )])
    nav = []
    if offset > 0:
        prev_lbl = "◀ Prev" if lang == "en" else "◀ السابق"
        nav.append(InlineKeyboardButton(prev_lbl, callback_data=f"adm_sup_{mode}_{offset - PAGE_SIZE}"))
    if offset + PAGE_SIZE < total:
        next_lbl = "Next ▶" if lang == "en" else "التالي ▶"
        nav.append(InlineKeyboardButton(next_lbl, callback_data=f"adm_sup_{mode}_{offset + PAGE_SIZE}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_sup")])
    return InlineKeyboardMarkup(buttons)


async def show_open_tickets(query, context, offset=0):
    lang    = get_admin_lang(query.from_user.id)
    tickets = get_open_tickets(limit=PAGE_SIZE, offset=offset)
    total   = count_tickets("open")
    title   = "🟢 Open Tickets" if lang == "en" else "🟢 التذاكر المفتوحة"
    empty   = "📭 No open tickets." if lang == "en" else "📭 لا توجد تذاكر مفتوحة."
    header  = f"{title} ({total})\n" if tickets else empty
    await query.edit_message_text(
        header, parse_mode="HTML",
        reply_markup=_ticket_list_keyboard(lang, "open", offset, total, tickets),
    )


async def show_closed_tickets(query, context, offset=0):
    lang    = get_admin_lang(query.from_user.id)
    tickets = get_closed_tickets(limit=PAGE_SIZE, offset=offset)
    total   = count_tickets("closed")
    title   = "✅ Closed Tickets" if lang == "en" else "✅ التذاكر المغلقة"
    empty   = "📭 No closed tickets." if lang == "en" else "📭 لا توجد تذاكر مغلقة."
    header  = f"{title} ({total})\n" if tickets else empty
    await query.edit_message_text(
        header, parse_mode="HTML",
        reply_markup=_ticket_list_keyboard(lang, "closed", offset, total, tickets),
    )


async def show_ticket(query, context, ticket_id: int):
    lang   = get_admin_lang(query.from_user.id)
    ticket = get_ticket(ticket_id)
    if not ticket:
        not_found = f"❌ Ticket #{ticket_id} not found." if lang == "en" else f"❌ التذكرة #{ticket_id} غير موجودة."
        await query.edit_message_text(
            not_found,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_sup")
            ]]),
        )
        return

    messages  = get_ticket_messages(ticket_id)
    status_e  = "🟢" if ticket["status"] == "open" else "✅"
    uname     = f"@{ticket.get('username')}" if ticket.get("username") else ticket.get("first_name", "?")
    user_role = "👤 User" if lang == "en" else "👤 مستخدم"
    adm_role  = "👑 Admin" if lang == "en" else "👑 مدير"

    lines = [
        f"{status_e} <b>{'Ticket' if lang == 'en' else 'تذكرة'} #{ticket_id}</b> — {ticket['status'].upper()}\n"
        f"👤 {ticket.get('first_name', '')} ({uname})\n"
        f"🆔 <code>{ticket['user_id']}</code>\n"
        f"📅 {(ticket.get('created_at') or '')[:16]}\n"
        f"━━━━━━━━━━━━━━━━\n"
    ]
    for msg in messages:
        role  = user_role if msg["sender_role"] == "user" else adm_role
        time_ = (msg.get("created_at") or "")[11:16]
        lines.append(f"<b>{role}</b> [{time_}]:\n{msg['message']}\n")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]

    kb_rows = []
    if SUPPORT_BOT_USERNAME and ticket["status"] == "open":
        reply_lbl = "💬 Reply via Support Bot" if lang == "en" else "💬 الرد عبر بوت الدعم"
        kb_rows.append([InlineKeyboardButton(reply_lbl, url=f"https://t.me/{SUPPORT_BOT_USERNAME}")])
    back_mode = "open" if ticket["status"] == "open" else "closed"
    kb_rows.append([InlineKeyboardButton(t(lang, "btn_back"), callback_data=f"adm_sup_{back_mode}_0")])

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )
