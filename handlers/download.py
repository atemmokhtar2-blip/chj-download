import asyncio
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes
from telegram.error import TelegramError, NetworkError

from database.users import get_user, increment_downloads
from database.downloads import log_download
from database.cache import get_cached, set_cache
from services.downloader import analyze_url, download_video, download_audio, download_image, FileTooLargeError
from middlewares.rate_limiter import check_rate_limit, mark_download
from middlewares.auth import is_banned
from locales import t
from utils.helpers import is_valid_url, is_supported_url, truncate_title, make_progress_bar, format_size, get_platform_emoji

logger = logging.getLogger(__name__)
active_downloads: dict[int, bool] = {}

UPLOAD_RETRIES = 3
UPLOAD_RETRY_DELAY = 2

async def _upload_with_retry(coro):
    for attempt in range(UPLOAD_RETRIES):
        try:
            return await coro
        except (NetworkError, asyncio.TimeoutError) as e:
            if attempt < UPLOAD_RETRIES - 1:
                await asyncio.sleep(UPLOAD_RETRY_DELAY * (attempt + 1))
                continue
            raise e
        except TelegramError:
            raise

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = update.message.text.strip()
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if is_banned(user.id):
        await update.message.reply_text(t(lang, "banned"))
        return

    if not is_valid_url(url) or not is_supported_url(url):
        await update.message.reply_text(t(lang, "invalid_url"))
        return

    allowed, wait_secs = check_rate_limit(user.id)
    if not allowed:
        await update.message.reply_text(t(lang, "rate_limit", seconds=wait_secs))
        return

    if active_downloads.get(user.id):
        await update.message.reply_text(t(lang, "queue_full"))
        return

    status_msg = await update.message.reply_text(t(lang, "analyzing"), parse_mode="HTML")
    info = await analyze_url(url)
    if not info:
        await status_msg.edit_text(t(lang, "analysis_failed"))
        return

    context.user_data["current_info"] = info
    media_type = info.get("media_type", "video")
    qualities = info.get("qualities", [])
    title = truncate_title(info.get("title", "Unknown"))
    uploader = info.get("uploader", "Unknown")
    duration = info.get("duration", "Unknown")
    platform = info.get("platform", "")
    platform_emoji = get_platform_emoji(platform)

    keyboard = _build_action_keyboard(media_type, qualities, lang)
    caption = t(lang, "video_info",
                title=title, uploader=uploader,
                duration=duration,
                platform=f"{platform_emoji} {platform}")

    if info.get("thumbnail"):
        try:
            await status_msg.delete()
            await update.message.reply_photo(
                photo=info["thumbnail"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        except Exception: pass

    await status_msg.edit_text(caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def _build_action_keyboard(media_type: str, qualities: list, lang: str) -> list:
    keyboard = []
    if media_type == "video":
        keyboard.append([InlineKeyboardButton(t(lang, "best_quality"), callback_data="dl_video_best")])
        if qualities:
            for q in qualities[-3:]:
                keyboard.append([InlineKeyboardButton(f"📹 {q['label']}", callback_data=f"dl_video_{q['label']}")])
        keyboard.append([InlineKeyboardButton(t(lang, "download_audio"), callback_data="dl_audio")])
    elif media_type == "audio":
        keyboard.append([InlineKeyboardButton(t(lang, "download_audio"), callback_data="dl_audio")])
    elif media_type == "image":
        keyboard.append([InlineKeyboardButton(t(lang, "download_image"), callback_data="dl_image")])
    elif media_type == "album":
        keyboard.append([InlineKeyboardButton(t(lang, "download_album"), callback_data="dl_album")])
    
    keyboard.append([InlineKeyboardButton(t(lang, "cancel_button"), callback_data="dl_cancel")])
    return keyboard

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    data = query.data

    if data == "dl_cancel":
        await query.answer()
        edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
        await edit_fn(t(lang, "cancelled"))
        return

    info = context.user_data.get("current_info")
    if not info:
        await query.answer(t(lang, "session_expired"), show_alert=True)
        return

    if active_downloads.get(user.id):
        await query.answer(t(lang, "queue_full"), show_alert=True)
        return

    await query.answer()
    is_audio = data == "dl_audio"
    is_image = data == "dl_image"
    is_album = data == "dl_album"
    quality_label = "audio" if is_audio else ("image" if is_image else data.replace("dl_video_", ""))

    cached_id = get_cached(info["url"], quality_label, "audio" if is_audio else ("image" if is_image else "video"))
    if cached_id:
        try:
            edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
            await edit_fn(t(lang, "from_cache"), parse_mode="HTML")
            increment_downloads(user.id)
            if is_audio: await query.message.reply_audio(audio=cached_id)
            elif is_image: await query.message.reply_photo(photo=cached_id)
            else: await query.message.reply_video(video=cached_id)
            return
        except TelegramError: pass

    active_downloads[user.id] = True
    progress_msg = query.message
    file_path = None
    edit_fn = query.edit_message_caption if progress_msg.caption else progress_msg.edit_text

    async def update_progress(prog):
        try:
            if isinstance(prog, dict):
                pct, dl, tot, spd, eta = prog.get("pct", 0), prog.get("downloaded", 0), prog.get("total", 0), prog.get("speed", 0), prog.get("eta", 0)
            else: pct, dl, tot, spd, eta = prog, 0, 0, 0, 0
            bar = make_progress_bar(pct)
            txt = t(lang, "downloading", bar=bar, percent=pct, downloaded=format_size(dl), total=format_size(tot), speed=f"{format_size(int(spd))}/s", eta=f"{int(eta)}s")
            if progress_msg.caption: await progress_msg.edit_caption(txt, parse_mode="HTML")
            else: await progress_msg.edit_text(txt, parse_mode="HTML")
        except Exception: pass

    try:
        title = info.get("title", "Unknown")
        platform = info.get("platform", "")

        if is_image:
            file_path = await download_image(info["url"], info.get("image_url"))
            if not file_path: raise Exception("Download failed")
            await edit_fn(t(lang, "uploading"), parse_mode="HTML")
            with open(file_path, "rb") as f:
                sent = await _upload_with_retry(query.message.reply_photo(photo=InputFile(f, filename=f"{title[:50]}.jpg"), caption=t(lang, "completed")))
            set_cache(info["url"], quality_label, "image", sent.photo[-1].file_id, title, platform)

        elif is_audio:
            file_path = await download_audio(info["url"], update_progress)
            if not file_path: raise Exception("Download failed")
            await edit_fn(t(lang, "uploading"), parse_mode="HTML")
            with open(file_path, "rb") as f:
                sent = await _upload_with_retry(query.message.reply_audio(audio=InputFile(f, filename=f"{title[:50]}.mp3"), caption=t(lang, "completed")))
            set_cache(info["url"], quality_label, "audio", sent.audio.file_id, title, platform)

        else:
            file_path = await download_video(info["url"], quality_label, update_progress)
            if not file_path: raise Exception("Download failed")
            await edit_fn(t(lang, "uploading"), parse_mode="HTML")
            with open(file_path, "rb") as f:
                sent = await _upload_with_retry(query.message.reply_video(video=InputFile(f, filename=f"{title[:50]}.mp4"), caption=t(lang, "completed"), supports_streaming=True))
            set_cache(info["url"], quality_label, "video", sent.video.file_id, title, platform)

        increment_downloads(user.id)
        log_download(user.id, info["url"], title, platform, quality_label, "audio" if is_audio else ("image" if is_image else "video"))
        mark_download(user.id)

    except FileTooLargeError:
        await edit_fn(t(lang, "file_too_large", max_size="50MB"))
    except Exception as e:
        logger.error(f"Download error: {e}")
        await edit_fn(t(lang, "download_failed"))
    finally:
        active_downloads.pop(user.id, None)
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass
