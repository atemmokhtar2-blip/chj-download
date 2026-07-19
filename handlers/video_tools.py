import asyncio
import os
import subprocess
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes

from database.users import get_user
from locales import t
from utils.ffmpeg_check import FFMPEG_AVAILABLE, FFMPEG_PATH
from utils.logger import error_logger

TOOLS_MENU_TEXT_AR = (
    "🎬 <b>أدوات الفيديو</b>\n\n"
    "أرسل ملف فيديو أولاً، ثم اختر الأداة التي تريد تطبيقها:"
)
TOOLS_MENU_TEXT_EN = (
    "🎬 <b>Video Tools</b>\n\n"
    "Send a video file first, then choose a tool to apply:"
)

FFMPEG_UNAVAILABLE_AR = "⚠️ أدوات الفيديو غير متاحة حالياً (FFmpeg مفقود)."
FFMPEG_UNAVAILABLE_EN = "⚠️ Video tools are not available (FFmpeg missing)."


def tools_keyboard(lang: str) -> InlineKeyboardMarkup:
    if lang == "ar":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✂️ قص الفيديو",       callback_data="vt_trim"),
                InlineKeyboardButton("🎵 استخراج الصوت",    callback_data="vt_audio"),
            ],
            [
                InlineKeyboardButton("🖼 استخراج الصورة",   callback_data="vt_thumb"),
                InlineKeyboardButton("📐 تغيير الحجم",      callback_data="vt_resize"),
            ],
            [
                InlineKeyboardButton("⚡ ضغط الفيديو",      callback_data="vt_compress"),
            ],
            [InlineKeyboardButton("❌ إلغاء", callback_data="vt_cancel")],
        ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✂️ Trim",             callback_data="vt_trim"),
            InlineKeyboardButton("🎵 Extract Audio",    callback_data="vt_audio"),
        ],
        [
            InlineKeyboardButton("🖼 Thumbnail",        callback_data="vt_thumb"),
            InlineKeyboardButton("📐 Resize",           callback_data="vt_resize"),
        ],
        [
            InlineKeyboardButton("⚡ Compress",         callback_data="vt_compress"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="vt_cancel")],
    ])


async def video_tools_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user = get_user(update.effective_user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if not FFMPEG_AVAILABLE:
        await update.message.reply_text(
            FFMPEG_UNAVAILABLE_AR if lang == "ar" else FFMPEG_UNAVAILABLE_EN
        )
        return

    text = TOOLS_MENU_TEXT_AR if lang == "ar" else TOOLS_MENU_TEXT_EN
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=tools_keyboard(lang))


async def handle_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store received video file for tool processing."""
    db_user = get_user(update.effective_user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if not FFMPEG_AVAILABLE:
        return

    video = update.message.video or update.message.document
    if not video:
        return

    context.user_data["vt_file_id"] = video.file_id
    context.user_data["vt_file_name"] = getattr(video, "file_name", "video.mp4") or "video.mp4"

    text = TOOLS_MENU_TEXT_AR if lang == "ar" else TOOLS_MENU_TEXT_EN
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=tools_keyboard(lang))


async def video_tools_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db_user = get_user(query.from_user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    data = query.data

    await query.answer()

    if data == "vt_cancel":
        msg = "❌ تم الإلغاء." if lang == "ar" else "❌ Cancelled."
        await query.edit_message_text(msg)
        return

    file_id = context.user_data.get("vt_file_id")
    if not file_id:
        msg = ("⚠️ لم يتم العثور على ملف. أرسل فيديو أولاً ثم اختر أداة."
               if lang == "ar" else
               "⚠️ No video found. Send a video file first, then pick a tool.")
        await query.edit_message_text(msg)
        return

    processing_msg = "⏳ جارٍ المعالجة..." if lang == "ar" else "⏳ Processing..."
    await query.edit_message_text(processing_msg)

    try:
        tg_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = os.path.join(tmpdir, "input.mp4")
            await tg_file.download_to_drive(in_path)

            if data == "vt_audio":
                await _extract_audio(query, context, in_path, tmpdir, lang)
            elif data == "vt_thumb":
                await _extract_thumbnail(query, context, in_path, tmpdir, lang)
            elif data == "vt_trim":
                await _trim_video(query, context, in_path, tmpdir, lang)
            elif data == "vt_resize":
                await _resize_video(query, context, in_path, tmpdir, lang)
            elif data == "vt_compress":
                await _compress_video(query, context, in_path, tmpdir, lang)

    except Exception as e:
        error_logger.error(f"Video tool error: {e}", exc_info=True)
        err_msg = "❌ حدث خطأ أثناء المعالجة." if lang == "ar" else "❌ An error occurred during processing."
        try:
            await query.edit_message_text(err_msg)
        except Exception:
            pass


def _run_ffmpeg(args: list, timeout: int = 120) -> bool:
    cmd = [FFMPEG_PATH] + args
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return result.returncode == 0


async def _extract_audio(query, context, in_path: str, tmpdir: str, lang: str):
    out_path = os.path.join(tmpdir, "audio.mp3")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _run_ffmpeg,
        ["-i", in_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", "-y", out_path]
    )
    if ok and os.path.exists(out_path):
        caption = "🎵 الصوت المستخرج" if lang == "ar" else "🎵 Extracted Audio"
        with open(out_path, "rb") as f:
            await query.message.reply_audio(audio=InputFile(f, filename="audio.mp3"), caption=caption)
        done_msg = "✅ تم استخراج الصوت بنجاح!" if lang == "ar" else "✅ Audio extracted successfully!"
        await query.edit_message_text(done_msg)
    else:
        err = "❌ فشل استخراج الصوت." if lang == "ar" else "❌ Audio extraction failed."
        await query.edit_message_text(err)


async def _extract_thumbnail(query, context, in_path: str, tmpdir: str, lang: str):
    out_path = os.path.join(tmpdir, "thumb.jpg")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _run_ffmpeg,
        ["-i", in_path, "-ss", "00:00:01", "-vframes", "1", "-q:v", "2", "-y", out_path]
    )
    if ok and os.path.exists(out_path):
        caption = "🖼 الصورة المصغرة" if lang == "ar" else "🖼 Thumbnail"
        with open(out_path, "rb") as f:
            await query.message.reply_photo(photo=InputFile(f, filename="thumb.jpg"), caption=caption)
        done_msg = "✅ تم استخراج الصورة المصغرة!" if lang == "ar" else "✅ Thumbnail extracted!"
        await query.edit_message_text(done_msg)
    else:
        err = "❌ فشل استخراج الصورة المصغرة." if lang == "ar" else "❌ Thumbnail extraction failed."
        await query.edit_message_text(err)


async def _trim_video(query, context, in_path: str, tmpdir: str, lang: str):
    out_path = os.path.join(tmpdir, "trimmed.mp4")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _run_ffmpeg,
        ["-i", in_path, "-ss", "0", "-t", "60",
         "-c:v", "libx264", "-c:a", "aac", "-y", out_path]
    )
    if ok and os.path.exists(out_path):
        caption = "✂️ الفيديو المقطوع (أول 60 ثانية)" if lang == "ar" else "✂️ Trimmed Video (first 60s)"
        with open(out_path, "rb") as f:
            await query.message.reply_video(video=InputFile(f, filename="trimmed.mp4"),
                                            caption=caption, supports_streaming=True)
        done_msg = "✅ تم قص الفيديو (أول 60 ثانية)!" if lang == "ar" else "✅ Video trimmed (first 60s)!"
        await query.edit_message_text(done_msg)
    else:
        err = "❌ فشل قص الفيديو." if lang == "ar" else "❌ Video trim failed."
        await query.edit_message_text(err)


async def _resize_video(query, context, in_path: str, tmpdir: str, lang: str):
    out_path = os.path.join(tmpdir, "resized.mp4")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _run_ffmpeg,
        ["-i", in_path, "-vf", "scale=720:-2",
         "-c:v", "libx264", "-c:a", "aac", "-y", out_path]
    )
    if ok and os.path.exists(out_path):
        caption = "📐 الفيديو المُعاد تحجيمه (720p)" if lang == "ar" else "📐 Resized Video (720p)"
        with open(out_path, "rb") as f:
            await query.message.reply_video(video=InputFile(f, filename="resized.mp4"),
                                            caption=caption, supports_streaming=True)
        done_msg = "✅ تم تغيير حجم الفيديو إلى 720p!" if lang == "ar" else "✅ Video resized to 720p!"
        await query.edit_message_text(done_msg)
    else:
        err = "❌ فشل تغيير حجم الفيديو." if lang == "ar" else "❌ Video resize failed."
        await query.edit_message_text(err)


async def _compress_video(query, context, in_path: str, tmpdir: str, lang: str):
    out_path = os.path.join(tmpdir, "compressed.mp4")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _run_ffmpeg,
        ["-i", in_path, "-vcodec", "libx264", "-crf", "28",
         "-acodec", "aac", "-b:a", "96k", "-y", out_path]
    )
    if ok and os.path.exists(out_path):
        orig_size = os.path.getsize(in_path)
        new_size  = os.path.getsize(out_path)
        saved_pct = max(0, int((1 - new_size / orig_size) * 100)) if orig_size else 0
        if lang == "ar":
            caption = f"⚡ الفيديو المضغوط (وفّر {saved_pct}%)"
        else:
            caption = f"⚡ Compressed Video (saved {saved_pct}%)"
        with open(out_path, "rb") as f:
            await query.message.reply_video(video=InputFile(f, filename="compressed.mp4"),
                                            caption=caption, supports_streaming=True)
        done_msg = (f"✅ تم ضغط الفيديو! تم توفير {saved_pct}% من الحجم."
                    if lang == "ar" else
                    f"✅ Video compressed! Saved {saved_pct}%.")
        await query.edit_message_text(done_msg)
    else:
        err = "❌ فشل ضغط الفيديو." if lang == "ar" else "❌ Video compression failed."
        await query.edit_message_text(err)
