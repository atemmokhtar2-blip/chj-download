import asyncio
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from database.users import get_user, add_points, deduct_points, increment_downloads
from database.downloads import log_download
from database.cache import get_cached, set_cache
from database.achievements import check_and_award
from database.favorites import is_favorite, add_favorite, remove_favorite
from services.downloader import analyze_url, download_video, download_audio, download_image, FileTooLargeError
from services.subscription import check_subscription
from middlewares.rate_limiter import check_rate_limit, mark_download
from middlewares.auth import is_banned
from locales import t
from config.settings import (
    MAX_FILE_SIZE_MB, POINTS_DOWNLOAD, POINTS_REFERRAL, POINTS_FIRST_DOWNLOAD
)
from utils.helpers import is_valid_url, is_supported_url, truncate_title, make_progress_bar, format_size, get_platform_emoji

logger = logging.getLogger(__name__)

active_downloads: dict[int, bool] = {}


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    url = update.message.text.strip()

    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    from utils.maintenance import is_maintenance
    from middlewares.auth import is_admin
    if is_maintenance() and not is_admin(user.id):
        await update.message.reply_text(t(lang, "maintenance_text"), parse_mode="HTML")
        return

    if not db_user or is_banned(user.id):
        await update.message.reply_text(t(lang, "banned"))
        return

    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        from handlers.start import send_subscription_prompt
        await send_subscription_prompt(update, lang)
        return

    if not is_valid_url(url):
        await update.message.reply_text(t(lang, "invalid_url"))
        return

    if not is_supported_url(url):
        await update.message.reply_text(t(lang, "unsupported_url"))
        return

    # Check if user has enough points
    current_points = db_user.get("points", 0)
    if current_points < POINTS_DOWNLOAD:
        await update.message.reply_text(t(lang, "not_enough_points"), parse_mode="HTML")
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

    fav = is_favorite(user.id, url)
    fav_btn_text = t(lang, "remove_favorite") if fav else t(lang, "add_favorite")
    fav_cb = "fav_remove" if fav else "fav_add"

    keyboard = _build_action_keyboard(media_type, qualities, fav_btn_text, fav_cb, lang)

    caption = t(lang, "video_info",
                title=title, uploader=uploader,
                duration=duration,
                platform=f"{platform_emoji} {platform}",
                media_type=_media_type_label(media_type, lang))

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
        except Exception:
            pass

    await status_msg.edit_text(caption, parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup(keyboard))


def _media_type_label(media_type: str, lang: str) -> str:
    labels = {
        "video": ("🎬 Video", "🎬 فيديو"),
        "audio": ("🎵 Audio", "🎵 صوت"),
        "image": ("🖼 Image", "🖼 صورة"),
        "album": ("📁 Album", "📁 ألبوم"),
    }
    pair = labels.get(media_type, ("🎬 Video", "🎬 فيديو"))
    return pair[1] if lang == "ar" else pair[0]


def _build_action_keyboard(media_type: str, qualities: list, fav_btn_text: str, fav_cb: str, lang: str) -> list:
    """Build action buttons based on detected media type — no fake/invalid buttons."""
    keyboard = []

    if media_type == "video":
        if qualities:
            keyboard.append([
                InlineKeyboardButton(t(lang, "best_quality"), callback_data="dl_video_best")
            ])
            for q in qualities[-3:]:  # show max 3 quality options
                keyboard.append([
                    InlineKeyboardButton(f"📹 {q['label']}", callback_data=f"dl_video_{q['label']}")
                ])
        else:
            keyboard.append([
                InlineKeyboardButton(t(lang, "download_video"), callback_data="dl_video_best")
            ])
        keyboard.append([InlineKeyboardButton(t(lang, "download_audio"), callback_data="dl_audio")])

    elif media_type == "audio":
        keyboard.append([InlineKeyboardButton(t(lang, "download_audio"), callback_data="dl_audio")])

    elif media_type == "image":
        keyboard.append([InlineKeyboardButton(t(lang, "download_image"), callback_data="dl_image")])

    elif media_type == "album":
        keyboard.append([InlineKeyboardButton(t(lang, "download_album"), callback_data="dl_album")])
        keyboard.append([InlineKeyboardButton(t(lang, "download_audio"), callback_data="dl_audio")])

    keyboard.append([
        InlineKeyboardButton(fav_btn_text, callback_data=fav_cb),
        InlineKeyboardButton(t(lang, "cancel_button"), callback_data="dl_cancel"),
    ])

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

    if data in ("fav_add", "fav_remove"):
        info = context.user_data.get("current_info", {})
        url = info.get("url", "")
        if not url:
            await query.answer(t(lang, "session_no_url"), show_alert=True)
            return
        if data == "fav_add":
            add_favorite(user.id, url, info.get("title", ""), info.get("platform", ""),
                         info.get("thumbnail", ""))
            await query.answer(t(lang, "favorite_added"), show_alert=True)
        else:
            remove_favorite(user.id, url)
            await query.answer(t(lang, "favorite_removed"), show_alert=True)
        return

    info = context.user_data.get("current_info")
    if not info:
        await query.answer(t(lang, "session_expired"), show_alert=True)
        return

    subscribed = await check_subscription(context.bot, user.id)
    if not subscribed:
        await query.answer(t(lang, "not_subscribed"), show_alert=True)
        return

    # Check points before downloading
    live_user = get_user(user.id)
    current_points = live_user.get("points", 0) if live_user else 0
    if current_points < POINTS_DOWNLOAD:
        await query.answer(t(lang, "not_enough_points_short"), show_alert=True)
        return

    allowed, wait_secs = check_rate_limit(user.id)
    if not allowed:
        await query.answer(t(lang, "rate_limit", seconds=wait_secs), show_alert=True)
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
            if is_audio:
                await query.message.reply_audio(audio=cached_id)
            elif is_image:
                await query.message.reply_photo(photo=cached_id)
            else:
                await query.message.reply_video(video=cached_id)
            _post_download_sync(user.id)
            asyncio.ensure_future(_award_achievements(user.id))
            await _handle_first_download(query, context, user.id, lang)
            await send_invite_prompt(query.message.chat_id, context, lang, user.id)
            return
        except TelegramError:
            pass

    active_downloads[user.id] = True
    mark_download(user.id)

    progress_msg = query.message
    file_path = None
    edit_fn = query.edit_message_caption if progress_msg.caption else progress_msg.edit_text

    def _fmt_progress(pct, downloaded, total, speed, eta):
        bar = make_progress_bar(pct)
        dl_str    = format_size(downloaded) if downloaded else "..."
        total_str = format_size(total)      if total     else "..."
        speed_str = f"{format_size(int(speed))}/s" if speed else "..."
        if eta and eta > 0:
            eta_str = f"{eta // 60}m {eta % 60}s" if eta >= 60 else f"{int(eta)}s"
        else:
            eta_str = "..."
        return t(lang, "downloading", bar=bar, percent=pct,
                 downloaded=dl_str, total=total_str, speed=speed_str, eta=eta_str)

    async def update_progress(prog):
        try:
            if isinstance(prog, dict):
                pct  = prog.get("pct", 0)
                dl   = prog.get("downloaded", 0)
                tot  = prog.get("total", 0)
                spd  = prog.get("speed", 0)
                eta  = prog.get("eta", 0)
            else:
                pct, dl, tot, spd, eta = prog, 0, 0, 0, 0
            txt = _fmt_progress(pct, dl, tot, spd, eta)
            if progress_msg.caption:
                await progress_msg.edit_caption(txt, parse_mode="HTML")
            else:
                await progress_msg.edit_text(txt, parse_mode="HTML")
        except Exception:
            pass

    try:
        await edit_fn(_fmt_progress(0, 0, 0, 0, 0), parse_mode="HTML")

        title   = info.get("title", "Unknown")
        platform = info.get("platform", "")

        if is_image:
            image_url = info.get("image_url")
            if not image_url:
                await edit_fn(t(lang, "download_failed"))
                return
            file_path = await download_image(info["url"], image_url)
            if not file_path:
                await edit_fn(t(lang, "download_failed"))
                return
            await edit_fn(t(lang, "uploading"), parse_mode="HTML")
            with open(file_path, "rb") as f:
                sent = await query.message.reply_photo(
                    photo=InputFile(f, filename=f"{title[:50]}.jpg"),
                    caption=t(lang, "completed"),
                )
            file_id = sent.photo[-1].file_id
            set_cache(info["url"], quality_label, "image", file_id, title, platform)

        elif is_audio:
            file_path = await download_audio(info["url"], update_progress)
            if not file_path:
                await edit_fn(t(lang, "download_failed"))
                return
            await edit_fn(t(lang, "uploading"), parse_mode="HTML")
            with open(file_path, "rb") as f:
                sent = await query.message.reply_audio(
                    audio=InputFile(f, filename=f"{title[:50]}.mp3"),
                    title=title[:64],
                    performer=info.get("uploader", "")[:64],
                )
            file_id = sent.audio.file_id
            set_cache(info["url"], quality_label, "audio", file_id, title, platform)

        else:
            fmt_id = next(
                (q["format_id"] for q in info.get("qualities", []) if q["label"] == quality_label),
                "bestvideo"
            )
            file_path = await download_video(info["url"], fmt_id, quality_label, update_progress)
            if not file_path:
                await edit_fn(t(lang, "download_failed"))
                return
            await edit_fn(t(lang, "uploading"), parse_mode="HTML")
            with open(file_path, "rb") as f:
                sent = await query.message.reply_video(
                    video=InputFile(f, filename=f"{title[:50]}.mp4"),
                    caption=t(lang, "completed"),
                    supports_streaming=True,
                )
            file_id = sent.video.file_id
            set_cache(info["url"], quality_label, "video", file_id, title, platform)

        file_size = os.path.getsize(file_path) if file_path and os.path.exists(file_path) else 0
        download_id = log_download(
            user.id, info["url"], title, platform,
            quality_label, "audio" if is_audio else ("image" if is_image else "video"),
            file_size
        )

        # Deduct 1 point per download
        _post_download_sync(user.id)

        new_achievements = check_and_award(
            user.id,
            get_user(user.id).get("downloads", 0),
            get_user(user.id).get("referrals", 0)
        )
        for ach in new_achievements:
            await query.message.reply_text(t(lang, "achievement_unlocked", name=ach))

        await edit_fn(
            t(lang, "completed_detail",
              size=format_size(file_size),
              platform=platform,
              quality=quality_label),
            parse_mode="HTML"
        )

        await _handle_first_download(query, context, user.id, lang)

        bot_info = await context.bot.get_me()
        share_url = f"https://t.me/share/url?url=https://t.me/{bot_info.username}"
        feedback_keyboard = [[
            InlineKeyboardButton(t(lang, "feedback_like"),    callback_data=f"fb_like_{download_id}"),
            InlineKeyboardButton(t(lang, "feedback_dislike"), callback_data=f"fb_dislike_{download_id}"),
        ], [
            InlineKeyboardButton(t(lang, "feedback_report"),  callback_data=f"fb_report_{download_id}"),
            InlineKeyboardButton(t(lang, "feedback_share"),   url=share_url),
        ]]
        await query.message.reply_text(
            t(lang, "feedback_text"),
            reply_markup=InlineKeyboardMarkup(feedback_keyboard),
            parse_mode="HTML"
        )

        await send_invite_prompt(query.message.chat_id, context, lang, user.id)

    except FileTooLargeError:
        await edit_fn(t(lang, "file_too_large", max_mb=MAX_FILE_SIZE_MB))
    except Exception as e:
        logger.error(f"Download failed for user {user.id}: {e}", exc_info=True)
        await edit_fn(t(lang, "download_failed"))
    finally:
        active_downloads.pop(user.id, None)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


def _post_download_sync(user_id: int):
    """Increment download count and deduct 1 point."""
    increment_downloads(user_id)
    deduct_points(user_id, POINTS_DOWNLOAD)


async def _handle_first_download(query, context, user_id: int, lang: str):
    """Award bonus points on user's very first download."""
    fresh = get_user(user_id)
    if not fresh:
        return
    if fresh.get("downloads", 0) == 1:
        add_points(user_id, POINTS_FIRST_DOWNLOAD)
        await query.message.reply_text(
            t(lang, "first_download_reward"), parse_mode="HTML"
        )
        # Credit referral on first download
        from database.referrals import get_referral_by_referred, complete_referral, log_audit as log_ref
        from database.users import increment_referrals
        referral = get_referral_by_referred(user_id)
        if referral and referral["status"] == "pending" and not referral["reward_given"]:
            credited = complete_referral(user_id)
            if credited:
                add_points(referral["referrer_id"], POINTS_REFERRAL)
                increment_referrals(referral["referrer_id"])
                log_ref("reward_given", referral["referrer_id"], user_id,
                        f"+{POINTS_REFERRAL} pts after first download")


async def _award_achievements(user_id: int):
    try:
        fresh = get_user(user_id)
        if fresh:
            check_and_award(user_id, fresh.get("downloads", 0), fresh.get("referrals", 0))
    except Exception:
        pass


async def send_invite_prompt(chat_id: int, context, lang: str, user_id: int):
    db_user = get_user(user_id)
    if not db_user:
        return
    code = db_user.get("referral_code", "")
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=REF_{code}"

    keyboard = [[
        InlineKeyboardButton(t(lang, "share_button"),
                             url=f"https://t.me/share/url?url={ref_link}")
    ]]
    await context.bot.send_message(
        chat_id=chat_id,
        text=t(lang, "invite_after_download",
               link=ref_link,
               points_per_referral=POINTS_REFERRAL),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
