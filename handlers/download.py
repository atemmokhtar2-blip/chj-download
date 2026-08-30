import asyncio
import os
import logging
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
    InputMediaPhoto, InputMediaVideo,
)
from telegram.ext import ContextTypes
from telegram.error import TelegramError, NetworkError

from database.users import get_user, increment_downloads
from database.downloads import log_download
from database.cache import get_cached, set_cache, invalidate_cache
from services.downloader import (
    analyze_url, download_video, download_audio, download_image,
    download_album, FileTooLargeError, ALBUM_MAX_ITEMS,
)
from middlewares.rate_limiter import check_rate_limit_detailed, mark_download
from middlewares.concurrency import download_slot, active_global_slots
from middlewares.auth import is_banned
from locales import t
from utils.helpers import is_valid_url, is_supported_url, truncate_title, make_progress_bar, format_size, get_platform_emoji
from config.settings import MAX_FILE_SIZE_MB, HOURLY_DOWNLOAD_LIMIT, DAILY_DOWNLOAD_LIMIT

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

    rl = check_rate_limit_detailed(user.id)
    if not rl.allowed:
        if rl.reason == "daily":
            await update.message.reply_text(
                t(lang, "rate_limit_daily", limit=DAILY_DOWNLOAD_LIMIT)
            )
        elif rl.reason == "hourly":
            await update.message.reply_text(
                t(lang, "rate_limit_hourly", limit=HOURLY_DOWNLOAD_LIMIT)
            )
        else:
            await update.message.reply_text(
                t(lang, "rate_limit", seconds=rl.wait_seconds)
            )
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

    album_count = len(info.get("album_items") or [])
    keyboard = _build_action_keyboard(media_type, qualities, lang, album_count=album_count)
    caption = t(lang, "video_info",
                title=title, uploader=uploader,
                duration=duration,
                platform=f"{platform_emoji} {platform}")
    if media_type == "album" and album_count:
        caption += f"\n📂 <b>Items:</b> {album_count}"

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

def _build_action_keyboard(
    media_type: str, qualities: list, lang: str, album_count: int = 0
) -> list:
    """Video: best + quality buttons only (audio is always included in the video file)."""
    keyboard = []
    if media_type == "video":
        keyboard.append([InlineKeyboardButton(t(lang, "best_quality"), callback_data="dl_video_best")])
        if qualities:
            for q in qualities[-3:]:
                keyboard.append([
                    InlineKeyboardButton(f"📹 {q['label']}", callback_data=f"dl_video_{q['label']}")
                ])
    elif media_type == "audio":
        keyboard.append([InlineKeyboardButton(t(lang, "download_audio"), callback_data="dl_audio")])
    elif media_type == "image":
        keyboard.append([InlineKeyboardButton(t(lang, "download_image"), callback_data="dl_image")])
    elif media_type == "album":
        label = t(lang, "download_album")
        if album_count:
            label = f"{label} ({album_count})"
        keyboard.append([InlineKeyboardButton(label, callback_data="dl_album")])
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

    rl = check_rate_limit_detailed(user.id)
    if not rl.allowed:
        if rl.reason == "daily":
            await query.answer(t(lang, "rate_limit_daily", limit=DAILY_DOWNLOAD_LIMIT), show_alert=True)
        elif rl.reason == "hourly":
            await query.answer(t(lang, "rate_limit_hourly", limit=HOURLY_DOWNLOAD_LIMIT), show_alert=True)
        else:
            await query.answer(t(lang, "rate_limit", seconds=rl.wait_seconds), show_alert=True)
        return

    await query.answer()
    is_audio = data == "dl_audio"
    is_image = data == "dl_image"
    is_album = data == "dl_album"
    quality_label = "audio" if is_audio else ("image" if is_image else data.replace("dl_video_", ""))

    media_kind = "audio" if is_audio else ("image" if is_image else "video")
    cached_id = get_cached(info["url"], quality_label, media_kind)
    if cached_id:
        try:
            edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
            await edit_fn(t(lang, "from_cache"), parse_mode="HTML")
            if is_audio:
                await query.message.reply_audio(audio=cached_id)
            elif is_image:
                await query.message.reply_photo(photo=cached_id)
            else:
                await query.message.reply_video(video=cached_id)
            increment_downloads(user.id)
            return
        except TelegramError as e:
            # Stale / rotated file_id — drop it and fall through to a real download.
            logger.warning("Stale file_id for %s (%s): %s", info["url"][:80], quality_label, e)
            invalidate_cache(info["url"], quality_label, media_kind)

    # Consume rate-limit windows atomically when a real download starts
    # (cache hits do not consume). Closes double-click races.
    if not mark_download(user.id):
        await query.answer(t(lang, "rate_limit", seconds=1), show_alert=True)
        return

    active_downloads[user.id] = True
    progress_msg = query.message
    file_path = None
    edit_fn = query.edit_message_caption if progress_msg.caption else progress_msg.edit_text

    # Global concurrency gate — wait for a free slot (do not overwhelm server)
    try:
        in_use, max_slots = active_global_slots()
        if in_use >= max_slots:
            await edit_fn(t(lang, "queue_wait", current=in_use, maximum=max_slots), parse_mode="HTML")
    except Exception:
        pass

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
        async with download_slot(timeout=180):
            await _run_download(query, context, info, user, lang, quality_label,
                                is_audio, is_image, is_album, edit_fn, update_progress)
    except asyncio.TimeoutError:
        await edit_fn(t(lang, "server_busy"))
    except FileTooLargeError:
        await edit_fn(t(lang, "file_too_large", max_size=f"{MAX_FILE_SIZE_MB}MB"))
    except Exception as e:
        logger.error(f"Download error: {e}")
        await edit_fn(t(lang, "download_failed"))
    finally:
        active_downloads.pop(user.id, None)
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass


async def _run_download(query, context, info, user, lang, quality_label,
                        is_audio, is_image, is_album, edit_fn, update_progress):
    file_path = None
    try:
        title = info.get("title", "Unknown")
        platform = info.get("platform", "")

        if is_album:
            items = info.get("album_items") or []
            if not items:
                raise Exception("Album has no items")

            async def album_progress(prog):
                try:
                    idx = prog.get("album_index", 0)
                    tot = prog.get("album_total", len(items))
                    txt = t(lang, "album_downloading", current=idx, total=tot)
                    if progress_msg.caption:
                        await progress_msg.edit_caption(txt, parse_mode="HTML")
                    else:
                        await progress_msg.edit_text(txt, parse_mode="HTML")
                except Exception:
                    pass

            downloaded = await download_album(items, album_progress)
            if not downloaded:
                raise Exception("Album download failed")

            await edit_fn(
                t(lang, "album_uploading", count=len(downloaded)),
                parse_mode="HTML",
            )

            # Telegram media groups require 2–10 items. Single item → normal send.
            open_handles = []
            try:
                if len(downloaded) == 1:
                    item = downloaded[0]
                    safe_title = (item.get("title") or title or "media")[:40]
                    with open(item["path"], "rb") as fh:
                        if item["type"] == "image":
                            sent = await _upload_with_retry(
                                query.message.reply_photo(
                                    photo=InputFile(fh, filename=f"{safe_title}.jpg"),
                                    caption=t(lang, "completed"),
                                )
                            )
                            set_cache(
                                info["url"], quality_label, "image",
                                sent.photo[-1].file_id, title, platform,
                            )
                        else:
                            sent = await _upload_with_retry(
                                query.message.reply_video(
                                    video=InputFile(fh, filename=f"{safe_title}.mp4"),
                                    caption=t(lang, "completed"),
                                    supports_streaming=True,
                                )
                            )
                            set_cache(
                                info["url"], quality_label, "video",
                                sent.video.file_id, title, platform,
                            )
                else:
                    media_group = []
                    for i, item in enumerate(downloaded[:ALBUM_MAX_ITEMS]):
                        fh = open(item["path"], "rb")
                        open_handles.append(fh)
                        caption = t(lang, "completed") if i == 0 else None
                        safe_title = (item.get("title") or title or "media")[:40]
                        if item["type"] == "image":
                            media_group.append(
                                InputMediaPhoto(
                                    media=InputFile(fh, filename=f"{safe_title}_{i + 1}.jpg"),
                                    caption=caption,
                                    parse_mode="HTML" if caption else None,
                                )
                            )
                        else:
                            media_group.append(
                                InputMediaVideo(
                                    media=InputFile(fh, filename=f"{safe_title}_{i + 1}.mp4"),
                                    caption=caption,
                                    parse_mode="HTML" if caption else None,
                                    supports_streaming=True,
                                )
                            )

                    sent_msgs = await _upload_with_retry(
                        query.message.reply_media_group(media=media_group)
                    )
                    if sent_msgs:
                        first = sent_msgs[0]
                        if first.photo:
                            set_cache(
                                info["url"], quality_label, "image",
                                first.photo[-1].file_id, title, platform,
                            )
                        elif first.video:
                            set_cache(
                                info["url"], quality_label, "video",
                                first.video.file_id, title, platform,
                            )
            finally:
                for fh in open_handles:
                    try:
                        fh.close()
                    except Exception:
                        pass
                for item in downloaded:
                    p = item.get("path")
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                file_path = None

        elif is_image:
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
            # Resolve real yt-dlp format_id from the analyzed qualities list.
            # quality_label is UI text like "best" / "1080p" — not a format id.
            format_id = "best"
            for q in info.get("qualities") or []:
                if str(q.get("label", "")).lower() == str(quality_label).lower():
                    format_id = q.get("format_id") or "best"
                    break
            if str(quality_label).lower() in ("best", "best_quality"):
                format_id = "best"

            file_path = await download_video(
                info["url"], format_id, quality_label, update_progress,
                play_url=info.get("play_url"),
            )
            if not file_path:
                raise Exception("Download failed")
            await edit_fn(t(lang, "uploading"), parse_mode="HTML")
            with open(file_path, "rb") as f:
                sent = await _upload_with_retry(
                    query.message.reply_video(
                        video=InputFile(f, filename=f"{title[:50]}.mp4"),
                        caption=t(lang, "completed"),
                        supports_streaming=True,
                    )
                )
            set_cache(
                info["url"], quality_label, "video",
                sent.video.file_id, title, platform,
            )

        increment_downloads(user.id)
        log_download(user.id, info["url"], title, platform, quality_label, "audio" if is_audio else ("image" if is_image else "video"))
    finally:
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass
