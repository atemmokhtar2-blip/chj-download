import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from database.db import get_all_active_user_ids, log_broadcast, get_broadcast_history, get_admin_lang
from locales import t
from utils.logger import action_logger, error_logger


def broadcast_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_bcast_text"),    callback_data="adm_bcast_text")],
        [InlineKeyboardButton(t(lang, "btn_bcast_photo"),   callback_data="adm_bcast_photo")],
        [InlineKeyboardButton(t(lang, "btn_bcast_video"),   callback_data="adm_bcast_video")],
        [InlineKeyboardButton(t(lang, "btn_bcast_history"), callback_data="adm_bcast_hist")],
        [InlineKeyboardButton(t(lang, "btn_back"),          callback_data="adm_menu")],
    ])


async def show_broadcast_menu(query, context):
    context.user_data.pop("adm_state", None)
    lang = get_admin_lang(query.from_user.id)
    title    = t(lang, "broadcast_title")
    subtitle = t(lang, "broadcast_subtitle")
    await query.edit_message_text(
        f"{title}\n\n{subtitle}",
        parse_mode="HTML",
        reply_markup=broadcast_menu_keyboard(lang)
    )


async def prompt_text_broadcast(query, context):
    lang = get_admin_lang(query.from_user.id)
    context.user_data["adm_state"] = "bcast_text"
    prompt = ("📝 <b>Text Broadcast</b>\n\nSend the message to broadcast to all users.\nHTML supported."
              if lang == "en" else
              "📝 <b>البث النصي</b>\n\nأرسل الرسالة لبثها لجميع المستخدمين.\nيدعم HTML.")
    await query.edit_message_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="adm_bcast")
        ]]),
    )


async def prompt_photo_broadcast(query, context):
    lang = get_admin_lang(query.from_user.id)
    context.user_data["adm_state"] = "bcast_photo"
    prompt = ("🖼 <b>Photo Broadcast</b>\n\nSend a photo (with optional caption)."
              if lang == "en" else
              "🖼 <b>بث صورة</b>\n\nأرسل الصورة (مع تعليق اختياري).")
    await query.edit_message_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="adm_bcast")
        ]]),
    )


async def prompt_video_broadcast(query, context):
    lang = get_admin_lang(query.from_user.id)
    context.user_data["adm_state"] = "bcast_video"
    prompt = ("🎬 <b>Video Broadcast</b>\n\nSend a video (with optional caption)."
              if lang == "en" else
              "🎬 <b>بث فيديو</b>\n\nأرسل الفيديو (مع تعليق اختياري).")
    await query.edit_message_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_cancel"), callback_data="adm_bcast")
        ]]),
    )


async def _do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        send_fn, label: str, preview: str):
    lang     = get_admin_lang(update.effective_user.id)
    admin_id = update.effective_user.id
    user_ids = get_all_active_user_ids()
    total    = len(user_ids)
    success  = 0
    failed   = 0

    waiting = ("📢 Broadcasting to <b>{n}</b> users… ⏳" if lang == "en"
               else "📢 جاري البث لـ <b>{n}</b> مستخدم… ⏳")
    status_msg = await update.message.reply_text(
        waiting.format(n=f"{total:,}"), parse_mode="HTML",
    )

    for i, uid in enumerate(user_ids, 1):
        try:
            await send_fn(uid)
            success += 1
        except Exception as exc:
            failed += 1
            error_logger.debug("Broadcast skip uid=%s: %s", uid, exc)
        if i % 50 == 0:
            try:
                progress = (f"📢 Broadcasting… {i}/{total}\n✅ {success} sent  ❌ {failed} failed"
                            if lang == "en" else
                            f"📢 جاري البث… {i}/{total}\n✅ {success} تم  ❌ {failed} فشل")
                await status_msg.edit_text(progress, parse_mode="HTML")
            except Exception:
                pass
        await asyncio.sleep(0.04)

    log_broadcast(admin_id, preview, total, success, failed)
    action_logger.info("Admin %s broadcast [%s] total=%s ok=%s fail=%s",
                       admin_id, label, total, success, failed)

    done = (f"✅ <b>Broadcast Complete</b>\n\n📤 Total: <b>{total:,}</b>\n✅ Sent: <b>{success:,}</b>\n❌ Failed: <b>{failed:,}</b>"
            if lang == "en" else
            f"✅ <b>اكتمل البث</b>\n\n📤 الإجمالي: <b>{total:,}</b>\n✅ تم: <b>{success:,}</b>\n❌ فشل: <b>{failed:,}</b>")
    await status_msg.edit_text(
        done, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_bcast")
        ]]),
    )


async def handle_text_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    context.user_data.pop("adm_state", None)

    async def send_fn(uid):
        await context.bot.send_message(uid, text, parse_mode="HTML")

    await _do_broadcast(update, context, send_fn, "text", text[:200])


async def handle_photo_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo   = update.message.photo[-1]
    caption = update.message.caption or ""
    context.user_data.pop("adm_state", None)

    async def send_fn(uid):
        await context.bot.send_photo(uid, photo.file_id, caption=caption, parse_mode="HTML")

    await _do_broadcast(update, context, send_fn, "photo", f"[photo] {caption[:100]}")


async def handle_video_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video   = update.message.video
    caption = update.message.caption or ""
    context.user_data.pop("adm_state", None)

    async def send_fn(uid):
        await context.bot.send_video(uid, video.file_id, caption=caption, parse_mode="HTML")

    await _do_broadcast(update, context, send_fn, "video", f"[video] {caption[:100]}")


async def show_broadcast_history(query, context):
    lang    = get_admin_lang(query.from_user.id)
    history = get_broadcast_history(limit=10)
    title   = "📋 <b>Broadcast History</b>" if lang == "en" else "📋 <b>سجل البث</b>"
    empty   = "📭 No broadcasts yet." if lang == "en" else "📭 لا يوجد بث سابق."
    if not history:
        text = f"{title}\n\n{empty}"
    else:
        lines = [f"{title}\n"]
        for b in history:
            date = (b.get("created_at") or "")[:16]
            lines.append(
                f"📤 <b>{date}</b>  ✅ {b['success']:,}/{b['total']:,}  ❌ {b['failed']:,}\n"
                f"   <i>{(b.get('message') or '')[:60]}</i>\n"
            )
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(lang, "btn_back"), callback_data="adm_bcast")
        ]]),
    )
