from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.tickets import (
    get_ticket, get_ticket_messages, get_open_tickets,
    get_closed_tickets, count_tickets, close_ticket, add_message
)
from config.settings import ADMIN_IDS, OWNER_ID
from utils.logger import tickets_logger, error_logger, system_logger


def _all_admins() -> list[int]:
    return list(set(ADMIN_IDS + ([OWNER_ID] if OWNER_ID else [])))


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMIN_IDS


def _require_admin(func):
    from functools import wraps

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not is_admin(user.id):
            await update.message.reply_text("❌ You don't have permission to use this command.")
            return
        return await func(update, context)

    return wrapper


def _ticket_admin_keyboard(ticket_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Reply",  callback_data=f"sup_reply_{ticket_id}_{user_id}"),
        InlineKeyboardButton("✅ Close",  callback_data=f"sup_close_{ticket_id}_{user_id}"),
    ]])


@_require_admin
async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_count   = count_tickets("open")
    closed_count = count_tickets("closed")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🟢 Open ({open_count})",   callback_data="sup_list_open_0"),
            InlineKeyboardButton(f"✅ Closed ({closed_count})", callback_data="sup_list_closed_0"),
        ]
    ])

    await update.message.reply_text(
        f"🎫 <b>Support Panel</b>\n\n"
        f"🟢 Open Tickets:   <b>{open_count}</b>\n"
        f"✅ Closed Tickets: <b>{closed_count}</b>\n\n"
        f"Use /tickets — open  |  /closed — closed",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@_require_admin
async def list_open_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tickets = get_open_tickets(limit=10)
    if not tickets:
        await update.message.reply_text("📭 No open tickets.")
        return
    await update.message.reply_text(
        _format_ticket_list(tickets, "🟢 Open Tickets"),
        parse_mode="HTML"
    )


@_require_admin
async def list_closed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tickets = get_closed_tickets(limit=10)
    if not tickets:
        await update.message.reply_text("📭 No closed tickets.")
        return
    await update.message.reply_text(
        _format_ticket_list(tickets, "✅ Closed Tickets"),
        parse_mode="HTML"
    )


@_require_admin
async def view_ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /ticket <id>")
        return

    ticket_id = int(args[0])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text(f"❌ Ticket #{ticket_id} not found.")
        return

    messages = get_ticket_messages(ticket_id)
    status_emoji = "🟢" if ticket["status"] == "open" else "✅"
    uname = f"@{ticket['username']}" if ticket.get("username") else "no username"

    lines = [
        f"{status_emoji} <b>Ticket #{ticket_id}</b> — {ticket['status'].upper()}\n"
        f"👤 {ticket['first_name']} ({uname})\n"
        f"🆔 <code>{ticket['user_id']}</code>\n"
        f"📅 {ticket['created_at'][:16]}\n"
        f"━━━━━━━━━━━━━━━━\n"
    ]

    for msg in messages:
        role_label = "👤 User" if msg["sender_role"] == "user" else "👑 Admin"
        lines.append(f"<b>{role_label}</b> [{msg['created_at'][11:16]}]:\n{msg['message']}\n")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]

    kb = _ticket_admin_keyboard(ticket_id, ticket["user_id"]) if ticket["status"] == "open" else None
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


@_require_admin
async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /reply <ticket_id> <your message>")
        return

    ticket_id = int(args[0])
    reply_text = " ".join(args[1:]).strip()

    if not reply_text:
        context.user_data["pending_sup_reply"] = {
            "ticket_id": ticket_id,
            "user_id": None
        }
        await update.message.reply_text(
            f"✏️ Type your reply for ticket #{ticket_id}:\n(Send /cancel to abort)"
        )
        return

    await _send_reply(update, context, ticket_id, reply_text)


@_require_admin
async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /close <ticket_id>")
        return

    ticket_id = int(args[0])
    await _do_close(update.effective_user.id, ticket_id, context)
    await update.message.reply_text(f"✅ Ticket #{ticket_id} closed.")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = query.from_user

    if not is_admin(user.id):
        await query.answer("❌ No permission.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("sup_reply_"):
        parts     = data.split("_")
        ticket_id = int(parts[2])
        user_id   = int(parts[3])
        ticket    = get_ticket(ticket_id)
        if not ticket:
            await query.edit_message_text(f"❌ Ticket #{ticket_id} not found.")
            return
        if ticket["status"] != "open":
            await query.edit_message_text(f"⚠️ Ticket #{ticket_id} is already closed.")
            return
        context.user_data["pending_sup_reply"] = {
            "ticket_id": ticket_id,
            "user_id":   user_id
        }
        await context.bot.send_message(
            user.id,
            f"✏️ Type your reply for Ticket #{ticket_id} (user <code>{user_id}</code>):\n"
            f"Send /cancel to abort.",
            parse_mode="HTML"
        )

    elif data.startswith("sup_close_"):
        parts     = data.split("_")
        ticket_id = int(parts[2])
        user_id   = int(parts[3])
        success   = await _do_close(user.id, ticket_id, context)
        if success:
            await query.edit_message_text(
                query.message.text + f"\n\n✅ <b>Closed by admin</b>",
                parse_mode="HTML"
            )
        else:
            await context.bot.send_message(user.id, f"⚠️ Ticket #{ticket_id} is already closed.")

    elif data.startswith("sup_list_open_"):
        offset  = int(data.split("_")[-1])
        tickets = get_open_tickets(limit=10, offset=offset)
        count   = count_tickets("open")
        text    = _format_ticket_list(tickets, "🟢 Open Tickets") if tickets else "📭 No open tickets."
        kb      = _pagination_keyboard("open", offset, count)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    elif data.startswith("sup_list_closed_"):
        offset  = int(data.split("_")[-1])
        tickets = get_closed_tickets(limit=10, offset=offset)
        count   = count_tickets("closed")
        text    = _format_ticket_list(tickets, "✅ Closed Tickets") if tickets else "📭 No closed tickets."
        kb      = _pagination_keyboard("closed", offset, count)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def handle_pending_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user    = update.effective_user
    pending = context.user_data.get("pending_sup_reply")
    if not pending or not is_admin(user.id):
        return False

    text = (update.message.text or "").strip()
    if text == "/cancel":
        context.user_data.pop("pending_sup_reply", None)
        await update.message.reply_text("❌ Reply cancelled.")
        return True

    ticket_id = pending["ticket_id"]
    context.user_data.pop("pending_sup_reply", None)

    await _send_reply(update, context, ticket_id, text)
    return True


async def _send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       ticket_id: int, reply_text: str):
    admin_id = update.effective_user.id
    ticket   = get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text(f"❌ Ticket #{ticket_id} not found.")
        return
    if ticket["status"] != "open":
        await update.message.reply_text(f"⚠️ Ticket #{ticket_id} is already closed.")
        return

    add_message(ticket_id, admin_id, "admin", reply_text)
    tickets_logger.info("Admin %s replied to ticket #%s", admin_id, ticket_id)

    try:
        await context.bot.send_message(
            ticket["user_id"],
            f"📩 <b>Support Reply — Ticket #{ticket_id}</b>\n\n"
            f"{reply_text}\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"You can reply by sending a message here.",
            parse_mode="HTML"
        )
        await update.message.reply_text(f"✅ Reply sent to user for Ticket #{ticket_id}.")
    except Exception as exc:
        error_logger.error("Failed to deliver reply for ticket #%s: %s", ticket_id, exc)
        await update.message.reply_text(
            f"⚠️ Reply saved but could not deliver to user (they may have blocked the bot)."
        )


async def _do_close(admin_id: int, ticket_id: int,
                     context: ContextTypes.DEFAULT_TYPE) -> bool:
    ticket  = get_ticket(ticket_id)
    if not ticket or ticket["status"] != "open":
        return False

    success = close_ticket(ticket_id, admin_id)
    if success:
        tickets_logger.info("Ticket #%s closed by admin %s", ticket_id, admin_id)
        try:
            await context.bot.send_message(
                ticket["user_id"],
                f"✅ <b>Ticket #{ticket_id} Closed</b>\n\n"
                f"Your support ticket has been closed by our team.\n"
                f"If you need further help, open a new ticket anytime.",
                parse_mode="HTML"
            )
        except Exception as exc:
            error_logger.error("Failed to notify user on close ticket #%s: %s", ticket_id, exc)
    return success


def _format_ticket_list(tickets: list[dict], title: str) -> str:
    lines = [f"<b>{title}</b>\n"]
    for t in tickets:
        uname = f"@{t['username']}" if t.get("username") else t.get("first_name", "Unknown")
        date  = t["created_at"][:10]
        lines.append(f"🎫 <b>#{t['id']}</b> | {uname} | {date}")
        lines.append(f"   /ticket {t['id']}\n")
    return "\n".join(lines)


def _pagination_keyboard(mode: str, offset: int, total: int) -> InlineKeyboardMarkup:
    page_size = 10
    buttons   = []
    if offset > 0:
        buttons.append(
            InlineKeyboardButton("◀ Prev", callback_data=f"sup_list_{mode}_{offset - page_size}")
        )
    if offset + page_size < total:
        buttons.append(
            InlineKeyboardButton("Next ▶", callback_data=f"sup_list_{mode}_{offset + page_size}")
        )
    return InlineKeyboardMarkup([buttons]) if buttons else None
