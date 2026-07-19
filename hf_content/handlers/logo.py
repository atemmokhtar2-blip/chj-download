"""
🎨 نظام الشعار — User Logo System
Free users: 1 logo, Premium users: 2 logos.
"""
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes
from database.users import get_user
from database.db import db_cursor
from utils.logger import error_logger

MAX_LOGO_FREE    = 1
MAX_LOGO_PREMIUM = 2

STATE_LOGO_UPLOAD = "logo_upload"


def _is_premium(db_user: dict) -> bool:
    from datetime import datetime
    if not db_user:
        return False
    if db_user.get("is_premium"):
        return True
    vip = db_user.get("vip_until")
    if vip:
        try:
            return datetime.strptime(vip, "%Y-%m-%d %H:%M:%S") > datetime.now()
        except Exception:
            pass
    return False


def init_logo_table():
    with db_cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_logos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                file_id    TEXT NOT NULL,
                name       TEXT DEFAULT 'شعاري',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_user_logos(user_id: int) -> list[dict]:
    with db_cursor() as c:
        rows = c.execute(
            "SELECT * FROM user_logos WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_user_logo(user_id: int) -> dict | None:
    logos = get_user_logos(user_id)
    return logos[0] if logos else None


def save_logo(user_id: int, file_id: str, name: str = "شعاري"):
    with db_cursor() as c:
        c.execute(
            "INSERT INTO user_logos (user_id, file_id, name) VALUES (?, ?, ?)",
            (user_id, file_id, name),
        )


def delete_logo(logo_id: int, user_id: int):
    with db_cursor() as c:
        c.execute("DELETE FROM user_logos WHERE id = ? AND user_id = ?", (logo_id, user_id))


# ── Handlers ──────────────────────────────────────────────────────────────────

async def logo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user = get_user(update.effective_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    ar = lang == "ar"
    uid = update.effective_user.id
    init_logo_table()

    logos = get_user_logos(uid)
    premium = _is_premium(db_user)
    max_logos = MAX_LOGO_PREMIUM if premium else MAX_LOGO_FREE
    plan_label = ("مميز ⭐" if ar else "Premium ⭐") if premium else ("مجاني" if ar else "Free")

    if ar:
        text = (
            f"🎨 <b>إدارة الشعار</b>\n\n"
            f"الخطة: <b>{plan_label}</b>\n"
            f"الشعارات المحفوظة: <b>{len(logos)}/{max_logos}</b>\n\n"
        )
        if logos:
            text += "شعاراتك الحالية:\n"
            for i, lg in enumerate(logos, 1):
                text += f"  {i}. {lg['name']} — <i>{lg['created_at'][:10]}</i>\n"
        else:
            text += "لا توجد شعارات محفوظة.\n"
    else:
        text = (
            f"🎨 <b>Logo Management</b>\n\n"
            f"Plan: <b>{plan_label}</b>\n"
            f"Saved logos: <b>{len(logos)}/{max_logos}</b>\n\n"
        )
        if logos:
            text += "Your logos:\n"
            for i, lg in enumerate(logos, 1):
                text += f"  {i}. {lg['name']} — <i>{lg['created_at'][:10]}</i>\n"
        else:
            text += "No logos saved.\n"

    rows = []
    if len(logos) < max_logos:
        rows.append([InlineKeyboardButton(
            "📤 " + ("رفع شعار" if ar else "Upload Logo"),
            callback_data="logo_upload"
        )])
    for lg in logos:
        rows.append([
            InlineKeyboardButton(
                f"👁 {lg['name'][:20]}",
                callback_data=f"logo_view_{lg['id']}"
            ),
            InlineKeyboardButton(
                "🗑 " + ("حذف" if ar else "Delete"),
                callback_data=f"logo_del_{lg['id']}"
            ),
        ])
    if not premium and len(logos) >= MAX_LOGO_FREE:
        rows.append([InlineKeyboardButton(
            "⭐ " + ("ترقية للحصول على شعارين" if ar else "Upgrade for 2 logos"),
            callback_data="logo_upgrade_info"
        )])

    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None
    )


async def logo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db_user = get_user(query.from_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    ar = lang == "ar"
    uid = query.from_user.id
    data = query.data
    await query.answer()
    init_logo_table()

    if data == "logo_upload":
        premium = _is_premium(db_user)
        max_logos = MAX_LOGO_PREMIUM if premium else MAX_LOGO_FREE
        logos = get_user_logos(uid)
        if len(logos) >= max_logos:
            msg = (f"⚠️ وصلت للحد الأقصى ({max_logos} شعار)."
                   if ar else f"⚠️ Max logos reached ({max_logos}).")
            await query.answer(msg, show_alert=True)
            return
        context.user_data["logo_state"] = STATE_LOGO_UPLOAD
        await query.edit_message_text(
            "📤 " + ("أرسل صورة الشعار (PNG/JPG):" if ar else "Send your logo image (PNG/JPG):"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ " + ("إلغاء" if ar else "Cancel"), callback_data="logo_cancel")
            ]]),
        )

    elif data.startswith("logo_del_"):
        lid = int(data.split("_")[-1])
        delete_logo(lid, uid)
        msg = "✅ " + ("تم حذف الشعار." if ar else "Logo deleted.")
        await query.edit_message_text(msg)

    elif data.startswith("logo_view_"):
        lid = int(data.split("_")[-1])
        logos = get_user_logos(uid)
        lg = next((l for l in logos if l["id"] == lid), None)
        if lg:
            try:
                await query.message.reply_photo(
                    photo=lg["file_id"],
                    caption="🎨 " + (f"شعار: {lg['name']}" if ar else f"Logo: {lg['name']}"),
                )
            except Exception as e:
                await query.answer("فشل عرض الشعار.", show_alert=True)
        else:
            await query.answer("الشعار غير موجود.", show_alert=True)

    elif data == "logo_upgrade_info":
        msg = ("⭐ ترقِّ للعضوية المميزة للحصول على 2 شعار وأولوية في المعالجة!"
               if ar else "⭐ Upgrade to Premium for 2 logos and processing priority!")
        await query.answer(msg, show_alert=True)

    elif data == "logo_cancel":
        context.user_data.pop("logo_state", None)
        await query.edit_message_text("❌ " + ("تم الإلغاء." if ar else "Cancelled."))


async def handle_logo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle photo/document upload for logo. Returns True if handled."""
    state = context.user_data.get("logo_state")
    if state != STATE_LOGO_UPLOAD:
        return False

    db_user = get_user(update.effective_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    ar = lang == "ar"
    uid = update.effective_user.id

    photo = update.message.photo
    doc = update.message.document

    file_id = None
    if photo:
        file_id = photo[-1].file_id  # highest resolution
    elif doc and doc.mime_type and doc.mime_type.startswith("image/"):
        file_id = doc.file_id

    if not file_id:
        await update.message.reply_text(
            "⚠️ " + ("أرسل صورة PNG أو JPG فقط." if ar else "Send a PNG or JPG image.")
        )
        return True

    try:
        init_logo_table()
        save_logo(uid, file_id, "شعاري" if ar else "My Logo")
        context.user_data.pop("logo_state", None)
        await update.message.reply_text(
            "✅ " + ("تم حفظ الشعار! يمكنك الآن استخدامه في استوديو الفيديو 🎬"
                     if ar else "Logo saved! You can now use it in Video Studio 🎬")
        )
    except Exception as e:
        error_logger.error("Logo save error: %s", e)
        await update.message.reply_text("❌ " + ("فشل حفظ الشعار." if ar else "Logo save failed."))
    return True
