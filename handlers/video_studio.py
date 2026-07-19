"""
🎬 استوديو الفيديو — Full Video Studio for Main Bot
Extended video processing with FFmpeg: trim, audio, thumbnail, compress,
resize, text overlay, format convert, optimize, and user logo overlay.
"""
import asyncio
import os
import subprocess
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes

from database.users import get_user
from utils.ffmpeg_check import FFMPEG_AVAILABLE, FFMPEG_PATH
from utils.logger import error_logger

# ── Text overlays ──────────────────────────────────────────────────────────────

STUDIO_MENU_AR = "🎬 <b>استوديو الفيديو</b>\n\nأرسل فيديو أولاً ثم اختر الأداة:"
STUDIO_MENU_EN = "🎬 <b>Video Studio</b>\n\nSend a video first, then pick a tool:"
NO_FFMPEG_AR   = "⚠️ استوديو الفيديو غير متاح (FFmpeg مفقود)."
NO_FFMPEG_EN   = "⚠️ Video Studio unavailable (FFmpeg missing)."
NO_VIDEO_AR    = "⚠️ لم يتم إرسال فيديو. أرسل فيديو أولاً ثم اختر الأداة."
NO_VIDEO_EN    = "⚠️ No video found. Send a video first, then pick a tool."
PROCESSING_AR  = "⏳ <b>جارٍ المعالجة…</b>"
PROCESSING_EN  = "⏳ <b>Processing…</b>"

STATE_VS_TEXT   = "vs_add_text"
STATE_VS_FORMAT = "vs_convert_fmt"


def _is_premium(db_user: dict) -> bool:
    import sqlite3
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


def studio_keyboard(lang: str, is_premium: bool = False) -> InlineKeyboardMarkup:
    ar = lang == "ar"
    rows = [
        [
            InlineKeyboardButton("✂️ " + ("قص الفيديو"     if ar else "Trim"),       callback_data="vs_trim"),
            InlineKeyboardButton("🎵 " + ("استخراج الصوت"  if ar else "Extract Audio"), callback_data="vs_audio"),
        ],
        [
            InlineKeyboardButton("🖼 " + ("صورة مصغرة"    if ar else "Thumbnail"),    callback_data="vs_thumb"),
            InlineKeyboardButton("📐 " + ("تغيير الحجم"   if ar else "Resize"),       callback_data="vs_resize"),
        ],
        [
            InlineKeyboardButton("⚡ " + ("ضغط الفيديو"   if ar else "Compress"),     callback_data="vs_compress"),
            InlineKeyboardButton("🔄 " + ("تحويل الصيغة"  if ar else "Convert"),      callback_data="vs_convert"),
        ],
        [
            InlineKeyboardButton("📝 " + ("إضافة نص"      if ar else "Add Text"),     callback_data="vs_text"),
            InlineKeyboardButton("🚀 " + ("تحسين الفيديو" if ar else "Optimize"),     callback_data="vs_optimize"),
        ],
    ]
    if is_premium:
        rows.append([
            InlineKeyboardButton("🎨 " + ("إضافة شعار"    if ar else "Add Logo"),     callback_data="vs_logo"),
        ])
    rows.append([InlineKeyboardButton("❌ " + ("إلغاء" if ar else "Cancel"),           callback_data="vs_cancel")])
    return InlineKeyboardMarkup(rows)


def _run_ffmpeg(args: list, timeout: int = 180) -> tuple[bool, str]:
    cmd = [FFMPEG_PATH] + args
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return result.returncode == 0, result.stderr.decode(errors="replace")[-300:]


async def run_in_executor(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


# ── Entry points ───────────────────────────────────────────────────────────────

async def studio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user = get_user(update.effective_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    if not FFMPEG_AVAILABLE:
        await update.message.reply_text(NO_FFMPEG_AR if lang == "ar" else NO_FFMPEG_EN)
        return
    premium = _is_premium(db_user)
    text = STUDIO_MENU_AR if lang == "ar" else STUDIO_MENU_EN
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=studio_keyboard(lang, premium))


async def handle_video_for_studio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user = get_user(update.effective_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    if not FFMPEG_AVAILABLE:
        return
    video = update.message.video or update.message.document
    if not video:
        return
    context.user_data["vs_file_id"]   = video.file_id
    context.user_data["vs_file_name"] = getattr(video, "file_name", "video.mp4") or "video.mp4"
    premium = _is_premium(db_user)
    text = STUDIO_MENU_AR if lang == "ar" else STUDIO_MENU_EN
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=studio_keyboard(lang, premium))


async def studio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db_user = get_user(query.from_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    ar = lang == "ar"
    data = query.data
    await query.answer()

    if data == "vs_cancel":
        await query.edit_message_text("❌ " + ("تم الإلغاء." if ar else "Cancelled."))
        return

    # Prompt for text input
    if data == "vs_text":
        context.user_data["vs_state"] = STATE_VS_TEXT
        await query.edit_message_text(
            "📝 " + ("أرسل النص الذي تريد إضافته للفيديو:" if ar else "Send the text to overlay on the video:"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ " + ("إلغاء" if ar else "Cancel"), callback_data="vs_cancel")
            ]]),
        )
        return

    # Prompt for format choice
    if data == "vs_convert":
        await query.edit_message_text(
            "🔄 " + ("اختر صيغة التحويل:" if ar else "Choose output format:"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📹 MP4",  callback_data="vs_fmt_mp4"),
                 InlineKeyboardButton("🎵 MP3",  callback_data="vs_fmt_mp3")],
                [InlineKeyboardButton("🎬 WebM", callback_data="vs_fmt_webm"),
                 InlineKeyboardButton("📼 AVI",  callback_data="vs_fmt_avi")],
                [InlineKeyboardButton("❌ " + ("إلغاء" if ar else "Cancel"), callback_data="vs_cancel")],
            ]),
        )
        return

    file_id = context.user_data.get("vs_file_id")
    if not file_id:
        msg = NO_VIDEO_AR if ar else NO_VIDEO_EN
        await query.edit_message_text(msg)
        return

    await query.edit_message_text(PROCESSING_AR if ar else PROCESSING_EN, parse_mode="HTML")

    try:
        tg_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = os.path.join(tmpdir, "input.mp4")
            await tg_file.download_to_drive(in_path)

            if data == "vs_audio":
                await _do_extract_audio(query, in_path, tmpdir, ar)
            elif data == "vs_thumb":
                await _do_thumbnail(query, in_path, tmpdir, ar)
            elif data == "vs_trim":
                await _do_trim(query, in_path, tmpdir, ar)
            elif data == "vs_resize":
                await _do_resize(query, in_path, tmpdir, ar)
            elif data == "vs_compress":
                await _do_compress(query, in_path, tmpdir, ar)
            elif data == "vs_optimize":
                await _do_optimize(query, in_path, tmpdir, ar)
            elif data == "vs_logo":
                await _do_logo(query, context, in_path, tmpdir, ar, db_user)
            elif data.startswith("vs_fmt_"):
                fmt = data[len("vs_fmt_"):]
                await _do_convert(query, in_path, tmpdir, ar, fmt)

    except Exception as e:
        error_logger.error("Video studio error [%s]: %s", data, e, exc_info=True)
        err = ("❌ حدث خطأ أثناء المعالجة." if ar else "❌ Processing error occurred.")
        try:
            await query.edit_message_text(err)
        except Exception:
            pass


async def handle_studio_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for text-overlay tool."""
    db_user = get_user(update.effective_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    ar = lang == "ar"
    state = context.user_data.get("vs_state")
    if state != STATE_VS_TEXT:
        return False  # not our message

    overlay_text = update.message.text.strip()
    context.user_data.pop("vs_state", None)
    file_id = context.user_data.get("vs_file_id")
    if not file_id:
        await update.message.reply_text(NO_VIDEO_AR if ar else NO_VIDEO_EN)
        return True

    msg = await update.message.reply_text(PROCESSING_AR if ar else PROCESSING_EN, parse_mode="HTML")
    try:
        tg_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = os.path.join(tmpdir, "input.mp4")
            await tg_file.download_to_drive(in_path)
            await _do_add_text(update, in_path, tmpdir, ar, overlay_text)
    except Exception as e:
        error_logger.error("Text overlay error: %s", e)
        await update.message.reply_text("❌ " + ("فشل إضافة النص." if ar else "Text overlay failed."))
    return True


# ── Processing functions ───────────────────────────────────────────────────────

async def _do_extract_audio(query, in_path, tmpdir, ar):
    out = os.path.join(tmpdir, "audio.mp3")
    ok, _ = await run_in_executor(
        _run_ffmpeg, ["-i", in_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", "-y", out]
    )
    if ok and os.path.exists(out):
        cap = "🎵 الصوت المستخرج" if ar else "🎵 Extracted Audio"
        with open(out, "rb") as f:
            await query.message.reply_audio(audio=InputFile(f, filename="audio.mp3"), caption=cap)
        await query.edit_message_text("✅ " + ("تم استخراج الصوت!" if ar else "Audio extracted!"))
    else:
        await query.edit_message_text("❌ " + ("فشل استخراج الصوت." if ar else "Audio extraction failed."))


async def _do_thumbnail(query, in_path, tmpdir, ar):
    out = os.path.join(tmpdir, "thumb.jpg")
    ok, _ = await run_in_executor(
        _run_ffmpeg, ["-i", in_path, "-ss", "00:00:01", "-vframes", "1", "-q:v", "2", "-y", out]
    )
    if ok and os.path.exists(out):
        cap = "🖼 الصورة المصغرة" if ar else "🖼 Thumbnail"
        with open(out, "rb") as f:
            await query.message.reply_photo(photo=InputFile(f, filename="thumb.jpg"), caption=cap)
        await query.edit_message_text("✅ " + ("تم استخراج الصورة المصغرة!" if ar else "Thumbnail extracted!"))
    else:
        await query.edit_message_text("❌ " + ("فشل استخراج الصورة." if ar else "Thumbnail failed."))


async def _do_trim(query, in_path, tmpdir, ar):
    out = os.path.join(tmpdir, "trimmed.mp4")
    ok, _ = await run_in_executor(
        _run_ffmpeg, ["-i", in_path, "-ss", "0", "-t", "60",
                      "-c:v", "libx264", "-c:a", "aac", "-y", out]
    )
    if ok and os.path.exists(out):
        cap = "✂️ أول 60 ثانية" if ar else "✂️ First 60 seconds"
        with open(out, "rb") as f:
            await query.message.reply_video(video=InputFile(f, filename="trimmed.mp4"),
                                            caption=cap, supports_streaming=True)
        await query.edit_message_text("✅ " + ("تم قص الفيديو!" if ar else "Video trimmed!"))
    else:
        await query.edit_message_text("❌ " + ("فشل قص الفيديو." if ar else "Trim failed."))


async def _do_resize(query, in_path, tmpdir, ar):
    out = os.path.join(tmpdir, "resized.mp4")
    ok, _ = await run_in_executor(
        _run_ffmpeg, ["-i", in_path, "-vf", "scale=720:-2",
                      "-c:v", "libx264", "-c:a", "aac", "-y", out]
    )
    if ok and os.path.exists(out):
        cap = "📐 720p" if ar else "📐 Resized to 720p"
        with open(out, "rb") as f:
            await query.message.reply_video(video=InputFile(f, filename="resized.mp4"),
                                            caption=cap, supports_streaming=True)
        await query.edit_message_text("✅ " + ("تم تغيير الحجم إلى 720p!" if ar else "Resized to 720p!"))
    else:
        await query.edit_message_text("❌ " + ("فشل تغيير الحجم." if ar else "Resize failed."))


async def _do_compress(query, in_path, tmpdir, ar):
    out = os.path.join(tmpdir, "compressed.mp4")
    ok, _ = await run_in_executor(
        _run_ffmpeg, ["-i", in_path, "-vcodec", "libx264", "-crf", "28",
                      "-acodec", "aac", "-b:a", "96k", "-y", out]
    )
    if ok and os.path.exists(out):
        orig = os.path.getsize(in_path)
        new  = os.path.getsize(out)
        pct  = max(0, int((1 - new/orig)*100)) if orig else 0
        cap  = f"⚡ وُفِّر {pct}%" if ar else f"⚡ Saved {pct}%"
        with open(out, "rb") as f:
            await query.message.reply_video(video=InputFile(f, filename="compressed.mp4"),
                                            caption=cap, supports_streaming=True)
        await query.edit_message_text("✅ " + (f"تم الضغط! وُفِّر {pct}%" if ar else f"Compressed! Saved {pct}%"))
    else:
        await query.edit_message_text("❌ " + ("فشل الضغط." if ar else "Compression failed."))


async def _do_optimize(query, in_path, tmpdir, ar):
    """Fast-start optimized MP4 for web/streaming."""
    out = os.path.join(tmpdir, "optimized.mp4")
    ok, _ = await run_in_executor(
        _run_ffmpeg, ["-i", in_path, "-c:v", "libx264", "-crf", "23",
                      "-c:a", "aac", "-b:a", "128k",
                      "-movflags", "+faststart", "-y", out]
    )
    if ok and os.path.exists(out):
        orig = os.path.getsize(in_path)
        new  = os.path.getsize(out)
        pct  = max(0, int((1 - new/orig)*100)) if orig else 0
        cap  = f"🚀 فيديو محسَّن للبث (وُفِّر {pct}%)" if ar else f"🚀 Optimized for streaming (saved {pct}%)"
        with open(out, "rb") as f:
            await query.message.reply_video(video=InputFile(f, filename="optimized.mp4"),
                                            caption=cap, supports_streaming=True)
        await query.edit_message_text("✅ " + ("تم التحسين!" if ar else "Optimized!"))
    else:
        await query.edit_message_text("❌ " + ("فشل التحسين." if ar else "Optimization failed."))


async def _do_convert(query, in_path, tmpdir, ar, fmt: str):
    fmt_map = {
        "mp4":  (".mp4",  ["-c:v", "libx264", "-c:a", "aac"]),
        "mp3":  (".mp3",  ["-vn", "-acodec", "libmp3lame", "-q:a", "2"]),
        "webm": (".webm", ["-c:v", "libvpx-vp9", "-c:a", "libopus"]),
        "avi":  (".avi",  ["-c:v", "libxvid", "-c:a", "mp3"]),
    }
    ext, extra_args = fmt_map.get(fmt, (".mp4", ["-c:v", "libx264", "-c:a", "aac"]))
    out = os.path.join(tmpdir, f"converted{ext}")
    ok, err = await run_in_executor(
        _run_ffmpeg, ["-i", in_path] + extra_args + ["-y", out]
    )
    if ok and os.path.exists(out):
        cap = f"🔄 {'تم التحويل إلى' if ar else 'Converted to'} {fmt.upper()}"
        with open(out, "rb") as f:
            await query.message.reply_document(
                document=InputFile(f, filename=f"converted{ext}"), caption=cap
            )
        await query.edit_message_text("✅ " + (f"تم التحويل إلى {fmt.upper()}!" if ar else f"Converted to {fmt.upper()}!"))
    else:
        await query.edit_message_text("❌ " + (f"فشل التحويل إلى {fmt.upper()}." if ar else f"Conversion to {fmt.upper()} failed."))


async def _do_add_text(update, in_path, tmpdir, ar, text: str):
    out = os.path.join(tmpdir, "text_overlay.mp4")
    safe_text = text.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")
    drawtext = (
        f"drawtext=text='{safe_text}':"
        "fontsize=48:fontcolor=white:x=(w-text_w)/2:y=h-th-30:"
        "box=1:boxcolor=black@0.5:boxborderw=5"
    )
    ok, _ = await run_in_executor(
        _run_ffmpeg, ["-i", in_path, "-vf", drawtext,
                      "-c:v", "libx264", "-c:a", "aac", "-y", out]
    )
    if ok and os.path.exists(out):
        cap = f"📝 {'النص المضاف' if ar else 'Text Overlay'}: {text[:30]}"
        with open(out, "rb") as f:
            await update.message.reply_video(video=InputFile(f, filename="text_overlay.mp4"),
                                             caption=cap, supports_streaming=True)
    else:
        await update.message.reply_text("❌ " + ("فشل إضافة النص." if ar else "Text overlay failed."))


async def _do_logo(query, context, in_path, tmpdir, ar, db_user):
    """Overlay user's saved logo onto the video."""
    from handlers.logo import get_user_logo
    uid = query.from_user.id
    logo_info = get_user_logo(uid)
    if not logo_info:
        msg = ("لا يوجد شعار محفوظ. استخدم /logo لرفع شعار."
               if ar else "No logo saved. Use /logo to upload one.")
        await query.edit_message_text("⚠️ " + msg)
        return

    try:
        logo_file = await context.bot.get_file(logo_info["file_id"])
        logo_path = os.path.join(tmpdir, "logo.png")
        await logo_file.download_to_drive(logo_path)
        out = os.path.join(tmpdir, "logo_overlay.mp4")
        ok, _ = await run_in_executor(
            _run_ffmpeg,
            ["-i", in_path, "-i", logo_path,
             "-filter_complex", "overlay=W-w-10:H-h-10",
             "-c:a", "copy", "-y", out]
        )
        if ok and os.path.exists(out):
            cap = "🎨 " + ("تم إضافة الشعار" if ar else "Logo added")
            with open(out, "rb") as f:
                await query.message.reply_video(video=InputFile(f, filename="logo_overlay.mp4"),
                                                caption=cap, supports_streaming=True)
            await query.edit_message_text("✅ " + ("تم إضافة الشعار!" if ar else "Logo added!"))
        else:
            await query.edit_message_text("❌ " + ("فشل إضافة الشعار." if ar else "Logo overlay failed."))
    except Exception as e:
        error_logger.error("Logo overlay error: %s", e)
        await query.edit_message_text("❌ " + ("خطأ في الشعار." if ar else "Logo error."))
