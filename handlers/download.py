from __future__ import annotations

import asyncio
import os
import re
import uuid
import logging
import time
from html import escape
from telegram import (
    Update, InlineKeyboardMarkup, InputFile,
    InputMediaPhoto, InputMediaVideo,
)
from utils.button_text import inline_button
from telegram.ext import ContextTypes
from telegram.error import TelegramError, NetworkError

from database.users import get_user, increment_downloads
from database.downloads import log_download
from services import plan_policy
from database.user_settings import get_user_settings, AUDIO_QUALITY_BITRATE
from database.cache import (
    get_cached, set_cache, invalidate_cache, get_cached_album, set_cache_album,
    resolve_cached_delivery, set_cache_with_meta, make_fingerprint,
)
from services.media_vault import archive_to_vault, deliver_from_vault, get_storage_channel_id
from services.downloader import (
    analyze_url, download_video, download_audio, download_image,
    download_album, FileTooLargeError, DownloadImpossibleError, ALBUM_MAX_ITEMS,
)
from services.content_intelligence import render_intelligence_html
from middlewares.rate_limiter import check_rate_limit_detailed, mark_download
from middlewares.concurrency import download_slot, active_global_slots
from middlewares.user_locks import is_user_busy, try_acquire_user_download, release_user_download
from middlewares.cancellation import (
    DownloadCancelledError, request_cancel, clear_cancel,
    is_cancel_requested, check_cancelled,
)
from middlewares.auth import is_banned
from locales import t
from utils.helpers import is_valid_url, is_supported_url, truncate_title, make_progress_bar, format_size, get_platform_emoji, summarize_error
from utils.url_safety import is_public_url
from config.settings import (
    MAX_FILE_SIZE_MB,
    HOURLY_DOWNLOAD_LIMIT,
    DAILY_DOWNLOAD_LIMIT,
    MAX_FILE_SIZE_BYTES,
    AUTO_SPLIT_LARGE_VIDEOS,
    AUDIO_BITRATE,
    AUDIO_BITRATE_HIGH,
    BATCH_MAX_URLS,
    BOT_DISPLAY_NAME,
)
from utils.media_split import split_video_parts
from utils.telegram_safe import safe_edit

logger = logging.getLogger(__name__)

UPLOAD_RETRIES = 3
UPLOAD_RETRY_DELAY = 2
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


async def _quiet_edit(edit_fn, *args, **kwargs):
    """Best-effort status edit: never raise on benign/transient Telegram errors."""
    try:
        return await safe_edit(edit_fn, *args, **kwargs)
    except TelegramError:
        return None


def _safe_store(fn, *args, **kwargs):
    """Run post-send bookkeeping (cache/DB stats) without failing the download.

    Once the media has been delivered to Telegram, a follow-up failure (cache
    write, stats log, fingerprint index) must NEVER surface as "download
    failed" to the user — the file already arrived.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("Post-send bookkeeping failed (%s): %s", getattr(fn, "__name__", "fn"), exc)
        return None


async def _safe_vault(context, sent_message):
    """Archive a sent file to the storage channel best-effort.

    A vault failure is cosmetic — it must never mark a delivered file as failed.
    """
    try:
        return await archive_to_vault(context.bot, sent_message)
    except Exception as exc:
        logger.warning("Vault archive failed: %s", exc)
        return None


def _completion_caption(lang: str, settings: dict | None) -> str | None:
    """Completion caption shown on delivered media, honoring ``notify_complete``.

    When the user turns "اكتمال التحميل" off, media is sent with no caption
    instead of the usual brand/completed line.
    """
    if settings and not bool(settings.get("notify_complete", 1)):
        return None
    return t(lang, "completed", bot_name=BOT_DISPLAY_NAME)


def _enforce_user_size(path: str | None, settings: dict | None) -> None:
    """Apply the user's personal max_file_size_mb preference to any media file.

    The global MAX_FILE_SIZE_BYTES cap is enforced inside the downloader; this
    is the per-user limit from Settings -> "الحد الأقصى للحجم", applied uniformly
    to video, audio, image, and every album item before upload.
    """
    if not path or not os.path.exists(path):
        return
    user_max_mb = int((settings or {}).get("max_file_size_mb") or 0)
    if user_max_mb > 0:
        limit_bytes = user_max_mb * 1024 * 1024
        if os.path.getsize(path) > limit_bytes:
            raise FileTooLargeError(os.path.getsize(path), limit_bytes)


def _extract_urls(text: str) -> list[str]:
    """Pull every URL from a message, stripping trailing punctuation."""
    urls = []
    for raw in _URL_RE.findall(text or ""):
        url = raw.strip(".,;:!?)]}>»'\"")
        if is_valid_url(url):
            urls.append(url)
    return urls

async def _upload_with_retry(operation):
    """Run a Telegram upload with retries.

    ``operation`` must be a zero-argument callable returning a fresh awaitable.
    Reusing the same coroutine after a failed await raises runtime errors, so we
    recreate the Telegram request on every retry.
    """
    for attempt in range(UPLOAD_RETRIES):
        try:
            return await operation()
        except (NetworkError, asyncio.TimeoutError) as e:
            if attempt < UPLOAD_RETRIES - 1:
                await asyncio.sleep(UPLOAD_RETRY_DELAY * (attempt + 1))
                continue
            raise e
        except TelegramError:
            raise

async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, user, url: str, msg) -> None:
    """Analyze one URL and present the action card.

    Shared by the normal URL path, /search results, and multi-link messages so
    every entry point behaves identically.
    """
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if is_banned(user.id):
        await msg.reply_text(t(lang, "banned"))
        return

    if not is_valid_url(url):
        await msg.reply_text(t(lang, "invalid_url"))
        return

    # SSRF guard: never let the downloader fetch localhost, cloud metadata or
    # any other internal address. DNS resolution runs in a worker thread, and a
    # slow resolver fails closed (the URL is rejected) rather than blocking.
    try:
        safe_url, _block_reason = await asyncio.wait_for(
            asyncio.to_thread(is_public_url, url), timeout=6
        )
    except asyncio.TimeoutError:
        safe_url = False
    if not safe_url:
        await msg.reply_text(t(lang, "blocked_url"))
        return

    rl = check_rate_limit_detailed(user.id)
    if not rl.allowed:
        if rl.reason == "daily":
            await msg.reply_text(
                t(lang, "rate_limit_daily", limit=DAILY_DOWNLOAD_LIMIT)
            )
        elif rl.reason == "hourly":
            await msg.reply_text(
                t(lang, "rate_limit_hourly", limit=HOURLY_DOWNLOAD_LIMIT)
            )
        else:
            await msg.reply_text(
                t(lang, "rate_limit", seconds=rl.wait_seconds)
            )
        return

    if is_user_busy(user.id):
        await msg.reply_text(t(lang, "queue_full"))
        return

    # 📦 Plan enforcement (Basic vs Pro) — resolved from the RUNNING bot's
    # database row, NEVER from user input. Clone bans and the DB-backed
    # daily counter apply on EVERY entry point (single URL, batch, search).
    pctx = plan_policy.plan_context(context.bot)
    if pctx.get("is_clone"):
        if plan_policy.is_user_banned(pctx["instance_id"], user.id):
            await msg.reply_text(
                "\U0001F6AB تم تعطيل حسابك في هذه النسخة. تواصل مع صاحب النسخة."
            )
            return
        cap = plan_policy.daily_cap(pctx)
        if cap is not None and plan_policy.count_downloads_today(pctx["instance_id"], user.id) >= cap:
            await msg.reply_text(
                f"⏳ وصلت للحد اليومي لخطتك ({cap} تحميلًا). حاول مرة أخرى غدًا."
            )
            return

    status_msg = await msg.reply_text(t(lang, "analyzing"), parse_mode="HTML")
    try:
        info = await analyze_url(url)
    except Exception as exc:
        logger.exception("URL analysis crashed for %s: %s", url[:120], exc)
        _log_clone_failure(context, user.id, url, summarize_error(exc))
        await _quiet_edit(status_msg.edit_text, 
            t(lang, "analysis_failed_reason", reason=escape(summarize_error(exc))),
            parse_mode="HTML",
        )
        return
    if not info:
        _log_clone_failure(context, user.id, url, "analysis_failed")
        await _quiet_edit(status_msg.edit_text, t(lang, "analysis_failed"), parse_mode="HTML")
        return

    if info.get("error_reason") and not (info.get("downloadable", True) or info.get("album_items")):
        _log_clone_failure(context, user.id, url,
                           str(info.get("user_message_key") or info.get("error_reason")),
                           platform=str(info.get("platform") or ""))
        await _quiet_edit(status_msg.edit_text, _friendly_error(lang, info), parse_mode="HTML")
        return

    context.user_data["current_info"] = info
    token = uuid.uuid4().hex[:10]
    context.user_data[f"info:{token}"] = info
    media_type = info.get("media_type", "video")
    qualities = info.get("qualities", [])
    title = truncate_title(info.get("title", "Unknown"))
    uploader = info.get("uploader", "Unknown")
    duration = info.get("duration", "Unknown")
    platform = info.get("platform", "")
    platform_emoji = get_platform_emoji(platform)

    album_count = len(info.get("album_items") or [])
    keyboard = _build_action_keyboard(media_type, qualities, lang, album_count=album_count, info=info, token=token,
                                      plan=(pctx.get("plan") if pctx.get("is_clone") else None))
    caption = t(lang, "video_info",
                title=escape(title), uploader=escape(uploader),
                duration=escape(duration),
                platform=escape(f"{platform_emoji} {platform}"))
    caption += render_intelligence_html(info.get("intelligence"), lang)
    if media_type == "album" and album_count:
        caption += f"\n📂 <b>Items:</b> {album_count}"

    if info.get("thumbnail"):
        try:
            await msg.reply_photo(
                photo=info["thumbnail"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            await status_msg.delete()
            return
        except Exception:
            pass

    await _quiet_edit(status_msg.edit_text, caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    urls = _extract_urls(text)
    url = urls[0] if urls else text
    await process_url(update, context, user, url, update.message)


async def handle_batch_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process multiple URLs from a single message, one card per link."""
    user = update.effective_user
    text = (update.message.text or "").strip()
    # Plan batch cap — server-side, from the RUNNING bot's instance row:
    # Basic 3 links / Pro 10 / main the global setting. Never the raw global.
    pctx = plan_policy.plan_context(context.bot)
    batch_limit = plan_policy.batch_cap(pctx)
    all_urls = _extract_urls(text)
    urls = all_urls[:batch_limit]

    if not urls:
        await process_url(update, context, user, text, update.message)
        return

    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"

    if is_banned(user.id):
        await update.message.reply_text(t(lang, "banned"))
        return

    if pctx.get("is_clone") and plan_policy.is_user_banned(pctx["instance_id"], user.id):
        await update.message.reply_text("\U0001F6AB تم تعطيل حسابك في هذه النسخة.")
        return

    await update.message.reply_text(
        t(lang, "batch_start", count=len(urls)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if len(all_urls) > batch_limit and pctx.get("is_clone"):
        plan_label = plan_policy.PLAN_LABELS.get(pctx["plan"], "")
        await update.message.reply_text(
            f"✂️ خطتك ({plan_label}) تسمح بـ {batch_limit} روابط في الدفعة الواحدة — "
            f"تم تجاهل {len(all_urls) - batch_limit} رابطًا."
        )

    for url in urls:
        if not is_valid_url(url):
            await update.message.reply_text(
                t(lang, "batch_invalid", url=escape(url[:80])),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            continue
        await process_url(update, context, user, url, update.message)

def _log_clone_failure(context, user_id: int, url: str, reason: str,
                       platform: str = "") -> None:
    """Record a failed attempt for the Pro panel's failure stats.

    Clone instances ONLY (instance_id-scoped); the main bot is skipped so the
    table can't grow unbounded there. Stores the platform + a short safe
    reason — NEVER the full URL, tokens, or user content.
    """
    try:
        pctx = plan_policy.plan_context(context.bot)
        if not pctx.get("is_clone"):
            return
        plat = (platform or "").strip()
        if not plat:
            try:
                plat = url.split("/")[2][:40] if "//" in url else ""
            except Exception:
                plat = ""
        plan_policy.log_download_failure(
            int(user_id), int(pctx.get("instance_id") or 0), plat,
            str(reason or "unknown")[:160])
    except Exception:
        logger.debug("clone failure logging skipped", exc_info=True)


def _friendly_error(lang: str, info: dict) -> str:
    reason = info.get("user_message_key") or info.get("error_reason") or "analysis_failed"
    if reason == "spotify_metadata_only":
        return t(lang, "spotify_metadata_only")
    if reason in {"spotify_drm_protected", "private_or_login_required"}:
        return t(lang, "spotify_drm_protected")
    if reason == "region_or_availability_blocked":
        return t(lang, "region_blocked")
    if reason == "removed_or_not_found":
        return t(lang, "removed_or_not_found")
    if reason and reason != "analysis_failed":
        return t(lang, "analysis_failed_reason", reason=escape(summarize_error(reason)))
    return t(lang, "analysis_failed")


def _safe_upload_name(value: str, limit: int = 50) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in str(value or "media"))
    cleaned = "_".join(cleaned.split()).strip("._-")
    return (cleaned or "media")[:limit]


def _cleanup_file(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.debug("Temp cleanup failed for %s: %s", path, exc)


def _quality_height(label: str) -> int:
    try:
        return int(str(label).lower().replace("p", "").strip())
    except Exception:
        return 0


def _pick_smart_download(info: dict, settings: dict | None = None,
                         max_height: int | None = None) -> tuple[bool, bool, bool, str]:
    """Return (is_audio, is_image, is_album, quality_label) for one-tap smart mode.

    Honors the user's saved preferences: delivery_mode=audio forces MP3
    extraction, video_quality selects a specific default, audio_quality is
    applied by the caller (bitrate mapping).

    ``max_height`` is the PLAN ceiling (Basic: 720). Open-ended choices
    ('best'/'highest') and any specific pick above the ceiling collapse to
    the ceiling label; the engine then takes the best format at-or-below it.
    """
    settings = settings or {}
    media_type = info.get("media_type", "video")
    if media_type == "audio":
        return True, False, False, "audio"
    if media_type == "image":
        return False, True, False, "image"
    if media_type == "album":
        return False, False, True, "album"

    # User chose "استخراج الصوت" as the default delivery for videos.
    if str(settings.get("delivery_mode") or "video") == "audio":
        return True, False, False, "audio"

    vq = str(settings.get("video_quality") or "auto").lower()
    qualities = info.get("qualities") or []
    _cap_label = f"{max_height}p" if max_height else "best"

    if vq in ("highest", "best", "max"):
        return False, False, False, _cap_label

    if vq == "auto":
        intel = info.get("intelligence") or {}
        risk = str(intel.get("risk") or "unknown").lower()
        if risk == "high" or "Long video" in " • ".join(intel.get("badges") or []):
            return True, False, False, "audio"
        if risk == "medium" and qualities:
            safe_quality = None
            for q in sorted(qualities, key=lambda item: _quality_height(item.get("label", "0p")), reverse=True):
                label = q.get("label", "")
                if _quality_height(label) <= (max_height or 720):
                    safe_quality = label
                    break
            if safe_quality:
                return False, False, False, safe_quality
        best_quality = intel.get("best_quality") or "best"
        if not best_quality or best_quality == "best":
            return False, False, False, _cap_label
        bq = str(best_quality)
        if max_height and _quality_height(bq) > max_height:
            bq = f"{max_height}p"
        return False, False, False, bq

    # Specific choices: 360p / 480p / 720p / 1080p — exact match, else the
    # nearest available height under it, else the lowest available. A pick
    # above the plan ceiling is treated as the ceiling itself.
    wanted = _quality_height(vq)
    if max_height and wanted and wanted > max_height:
        wanted = max_height
    exact = next((q.get("label") for q in qualities if str(q.get("label", "")).lower() == vq), None)
    if exact and not (max_height and _quality_height(exact) > max_height):
        return False, False, False, exact
    lower = [q for q in qualities
             if 0 < _quality_height(q.get("label", "")) <= (wanted or 10 ** 9)
             and not (max_height and _quality_height(q.get("label", "")) > max_height)]
    if lower:
        chosen = max(lower, key=lambda q: _quality_height(q.get("label", "0p")))
        return False, False, False, chosen.get("label", _cap_label)
    if qualities:
        allowed = [q for q in qualities
                   if not (max_height and _quality_height(q.get("label", "")) > max_height)] or qualities
        chosen = min(allowed, key=lambda q: (_quality_height(q.get("label", "0p")) or 10 ** 9))
        label = chosen.get("label", _cap_label)
        if max_height and _quality_height(label) > max_height:
            label = f"{max_height}p"
        return False, False, False, label
    return False, False, False, _cap_label


def _resolve_download_choice(data: str, info: dict, settings: dict | None, plan=None) -> tuple[bool, bool, bool, str, str, str]:
    """Decide (is_audio, is_image, is_album, quality_label, audio_bitrate, delivery_mode).

    Pure of the Telegram query so every button mapping is unit-testable:

      * Smart Download honors ``video_quality`` + ``delivery_mode=audio`` +
        the auto-intelligence downgrade logic.
      * Explicit video/image/album buttons are their own quality and are never
        silently converted into MP3 by the delivery-mode default.
      * Audio ``quality_label`` is bitrate-specific (``audio_128``/``audio_192``/
        ``audio_320``) — the cache, history, and content-fingerprint keys all use
        this label, so a 320kbps cached file is never served back to a user who
        has since lowered ``audio_quality``. Without this, the audio-quality
        button would be saved but have NO real effect on cache hits.
    """
    settings = settings or {}
    user_audio_br = str(AUDIO_QUALITY_BITRATE.get(settings.get("audio_quality", "normal"), 192))
    # 🔊 Plan audio ceiling (Basic = 192kbps): a preference saved anywhere —
    # even "high" (320) set on the main bot — is clamped inside a Basic clone.
    user_audio_br = str(plan_policy.clamp_audio_bitrate(user_audio_br, plan))
    preferred_delivery = str(settings.get("delivery_mode") or "video")

    if data == "dl_smart":
        is_audio, is_image, is_album, quality_label = _pick_smart_download(info, settings)
        audio_bitrate = user_audio_br
        delivery_mode = preferred_delivery
        if is_audio:
            quality_label = f"audio_{audio_bitrate}"
        return is_audio, is_image, is_album, quality_label, audio_bitrate, delivery_mode

    is_audio = data in {"dl_audio", "dl_audio_320"}
    is_image = data == "dl_image"
    is_album = data == "dl_album"
    # "إرسال كملف" honored for explicit video choices too; only "استخراج
    # الصوت" stays Smart-Download-only (an explicit video button must never
    # silently turn into an MP3).
    delivery_mode = "video" if preferred_delivery == "audio" else preferred_delivery
    if is_audio:
        audio_bitrate = AUDIO_BITRATE_HIGH if data == "dl_audio_320" else user_audio_br
        # Plan ceiling applies to the EXPLICIT 320 button as well — a stale
        # (pre-downgrade) or forged dl_audio_320 callback can't stream above
        # the plan's cap; the visible button label is clamped to match.
        audio_bitrate = str(plan_policy.clamp_audio_bitrate(audio_bitrate, plan))
        quality_label = f"audio_{audio_bitrate}"
    else:
        quality_label = "image" if is_image else data.replace("dl_video_", "")
        audio_bitrate = user_audio_br
    return is_audio, is_image, is_album, quality_label, audio_bitrate, delivery_mode


def _callback_matches_media(data: str, info: dict) -> bool:
    media_type = info.get("media_type", "video")
    if data in {"dl_unavailable", "dl_cancel"}:
        return True
    if data == "dl_smart":
        return media_type in {"video", "audio", "image", "album"}
    if media_type == "video":
        return data in {"dl_audio", "dl_audio_320"} or data.startswith("dl_video_")
    if media_type == "audio":
        return data == "dl_audio"
    if media_type == "image":
        return data == "dl_image"
    if media_type == "album":
        return data == "dl_album"
    return False


def _build_action_keyboard(
    media_type: str, qualities: list, lang: str, album_count: int = 0, info: dict | None = None,
    token: str | None = None, plan=None,
) -> list:
    """Build clear download actions, including MP3/audio-only for videos."""
    suffix = f"|{token}" if token else ""
    keyboard = []
    if info and info.get("downloadable") is False:
        keyboard.append([inline_button(t(lang, "why_no_download"), callback_data=f"dl_unavailable{suffix}")])
        return keyboard
    if media_type == "video":
        keyboard.append([
            inline_button("⚡ Smart Download", callback_data=f"dl_smart{suffix}"),
        ])
        keyboard.append([
            inline_button(t(lang, "best_quality"), callback_data=f"dl_video_best{suffix}"),
            inline_button("🎧 MP3 / صوت فقط", callback_data=f"dl_audio{suffix}"),
        ])
        _acap = plan_policy.audio_cap(plan)
        _hi_label = (f"🎵 MP3 {_acap}kbps — أعلى نقاء لخطتك"
                     if _acap else "🎵 MP3 320kbps — أعلى نقاء")
        keyboard.append([
            inline_button(_hi_label, callback_data=f"dl_audio_320{suffix}"),
        ])
        real_qualities = [
            q for q in (qualities or [])
            if _quality_height(q.get("label", "")) > 0
        ]
        # Plan ceiling in the OFFERED list too: a Basic clone never sees a
        # 1080p button it can't actually use (the final server-side clamp
        # still guards stale/forged callbacks — this is honest UI on top).
        _vcap = (plan_policy.limits(plan).get("max_video_height") or 0) if plan is not None else 0
        if _vcap:
            real_qualities = [
                q for q in real_qualities
                if _quality_height(q.get("label", "")) <= _vcap
            ]
        for q in real_qualities:
            keyboard.append([
                inline_button(f"📹 {q['label']}", callback_data=f"dl_video_{q['label']}{suffix}"),
            ])
    elif media_type == "audio":
        keyboard.append([inline_button(t(lang, "download_audio"), callback_data=f"dl_audio{suffix}")])
    elif media_type == "image":
        keyboard.append([inline_button(t(lang, "download_image"), callback_data=f"dl_image{suffix}")])
    elif media_type == "album":
        label = t(lang, "download_album")
        if album_count:
            label = f"{label} ({album_count})"
        keyboard.append([inline_button(label, callback_data=f"dl_album{suffix}")])
    return keyboard

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    db_user = get_user(user.id)
    lang = db_user.get("language", "en") if db_user else "en"
    settings = get_user_settings(user.id)
    data = query.data

    # Batch/search messages embed a per-message token so each inline keyboard
    # binds to its own analysis even when several cards are present.
    token = None
    if "|" in data:
        data, token = data.split("|", 1)

    if data == "dl_cancel":
        await query.answer()
        edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
        await _quiet_edit(edit_fn, t(lang, "cancelled"))
        return

    if data.split("|", 1)[0] == "dl_stop":
        # 🛑 pressed while a download is running: flag it so every download
        # loop (yt-dlp hook, CDN chunk loop, fallback matrix, album items)
        # aborts at its next checkpoint. The running task then edits the card
        # to the final "download cancelled" message and cleans temp files.
        request_cancel(user.id)
        await query.answer(t(lang, "cancelling"))
        edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
        await _quiet_edit(edit_fn, t(lang, "cancelling"), parse_mode="HTML")
        return

    if token:
        info = context.user_data.get(f"info:{token}")
    else:
        info = context.user_data.get("current_info")
    if not info:
        await query.answer(t(lang, "session_expired"), show_alert=True)
        return

    if data == "dl_unavailable" or info.get("downloadable") is False:
        await query.answer(t(lang, "spotify_download_unavailable_alert"), show_alert=True)
        edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
        await _quiet_edit(edit_fn, _friendly_error(lang, info), parse_mode="HTML")
        return

    if not try_acquire_user_download(user.id):
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
        release_user_download(user.id)
        return

    # 📦 Plan enforcement on EVERY button press — ban + DB-backed daily
    # counter, resolved from the running bot's instance row.
    pctx = plan_policy.plan_context(context.bot)
    if pctx.get("is_clone"):
        if plan_policy.is_user_banned(pctx["instance_id"], user.id):
            await query.answer("\U0001F6AB تم تعطيل حسابك في هذه النسخة.", show_alert=True)
            release_user_download(user.id)
            return
        cap = plan_policy.daily_cap(pctx)
        if cap is not None and plan_policy.count_downloads_today(pctx["instance_id"], user.id) >= cap:
            await query.answer(f"⏳ وصلت للحد اليومي لخطتك ({cap} تحميلًا).", show_alert=True)
            release_user_download(user.id)
            return
        # Clone-level default quality (set by the Pro owner) applies only
        # when the user hasn't chosen their own preference.
        cdq = plan_policy.get_clone_setting(pctx["instance_id"], "default_quality", "")
        if cdq and cdq != "auto" and str(settings.get("video_quality") or "auto") == "auto":
            settings = dict(settings)
            settings["video_quality"] = cdq
        # Plan ceiling at DOWNLOAD time: a preference saved elsewhere (e.g.
        # 1080p on the main bot) can never lift a Basic clone above 720p —
        # server-side, on every press, regardless of where it was stored.
        _vq = str(settings.get("video_quality") or "auto")
        _clamped = plan_policy.clamp_quality(_vq, pctx["plan"])
        if _clamped != _vq:
            settings = dict(settings)
            settings["video_quality"] = _clamped

    allowed_prefixes = ("dl_video_",)
    allowed_exact = {"dl_smart", "dl_audio", "dl_audio_320", "dl_image", "dl_album", "dl_unavailable"}
    if data not in allowed_exact and not data.startswith(allowed_prefixes):
        await query.answer(t(lang, "session_expired"), show_alert=True)
        release_user_download(user.id)
        return
    if not _callback_matches_media(data, info):
        await query.answer(t(lang, "session_expired"), show_alert=True)
        release_user_download(user.id)
        return

    await query.answer()
    smart_mode = data == "dl_smart"

    # Single source of truth for button -> behavior mapping. The same pure,
    # unit-tested resolver the settings docs describe is used live, so every
    # saved preference is guaranteed to apply:
    #   * audio_quality -> bitrate-specific quality_label (audio_128/192/320),
    #     which keys the cache — changing audio quality can never serve a
    #     stale file rendered at the old bitrate.
    #   * delivery_mode -> "file" sends documents, "audio" drives Smart MP3.
    #   * video_quality -> Smart Download's default quality.
    is_audio, is_image, is_album, quality_label, audio_bitrate, delivery_mode = (
        _resolve_download_choice(data, info, settings,
                                 plan=(pctx.get("plan") if pctx.get("is_clone") else None))
    )

    # Plan quality ceiling — ONE final server-side clamp covering every
    # button path (smart / explicit dl_video_*): Basic never exceeds 720p,
    # even if the card offered a higher format or the callback was forged.
    if pctx.get("is_clone") and not is_audio and not is_image and not is_album:
        _mh = (pctx.get("limits") or {}).get("max_video_height")
        if _mh:
            _qh = _quality_height(quality_label)
            if str(quality_label).lower() in ("best", "highest", "max") or (_qh and _qh > _mh):
                quality_label = f"{_mh}p"

    if smart_mode and not is_audio:
        try:
            edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
            chosen = "image" if is_image else ("album" if is_album else f"video {quality_label}")
            await _quiet_edit(edit_fn, f"⚡ <b>Smart Download selected:</b> {chosen}", parse_mode="HTML")
        except Exception:
            pass

    if is_album:
        cached_album = get_cached_album(info["url"])
        if cached_album and len(cached_album) >= 2:
            try:
                edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
                await _quiet_edit(edit_fn, t(lang, "from_cache"), parse_mode="HTML")
                media_group = []
                for i, it in enumerate(cached_album[:ALBUM_MAX_ITEMS]):
                    caption = _completion_caption(lang, settings) if i == 0 else None
                    if it.get("type") == "video":
                        media_group.append(InputMediaVideo(media=it["file_id"], caption=caption, parse_mode="HTML" if caption else None, supports_streaming=True))
                    else:
                        media_group.append(InputMediaPhoto(media=it["file_id"], caption=caption, parse_mode="HTML" if caption else None))
                await _upload_with_retry(lambda: query.message.reply_media_group(media=media_group))
                _safe_store(increment_downloads, user.id)
                release_user_download(user.id)
                return
            except TelegramError as e:
                logger.warning("Stale album cache for %s: %s", info["url"][:80], e)
                invalidate_cache(info["url"], "album", "album")

    media_kind = "audio" if is_audio else ("image" if is_image else "video")
    cached = resolve_cached_delivery(
        info["url"], quality_label, media_kind,
        platform=info.get("platform") or "",
        media_id=str(info.get("media_id") or ""),
    )
    if cached and cached.get("file_id"):
        try:
            edit_fn = query.edit_message_caption if query.message.caption else query.edit_message_text
            await _quiet_edit(edit_fn, t(lang, "from_cache"), parse_mode="HTML")
            delivered = False
            # Prefer vault copy_message (durable CDN path)
            if cached.get("vault_chat_id") and cached.get("vault_message_id"):
                try:
                    await deliver_from_vault(
                        context.bot, query.message.chat_id,
                        int(cached["vault_chat_id"]), int(cached["vault_message_id"]),
                    )
                    delivered = True
                except TelegramError as ve:
                    logger.warning("Vault deliver failed, trying file_id: %s", ve)
            if not delivered:
                fid = cached["file_id"]
                brand_caption = _completion_caption(lang, settings)
                if is_audio:
                    await query.message.reply_audio(audio=fid, caption=brand_caption)
                elif is_image:
                    await query.message.reply_photo(photo=fid, caption=brand_caption)
                elif delivery_mode == "file":
                    await query.message.reply_document(document=fid, caption=brand_caption)
                else:
                    await query.message.reply_video(video=fid, caption=brand_caption)
            _safe_store(increment_downloads, user.id)
            release_user_download(user.id)
            return
        except TelegramError as e:
            logger.warning("Stale cache for %s (%s): %s", info["url"][:80], quality_label, e)
            invalidate_cache(info["url"], quality_label, media_kind)

    # Consume rate-limit windows atomically when a real download starts
    # (cache hits do not consume). Closes double-click races.
    if not mark_download(user.id):
        await query.answer(t(lang, "rate_limit", seconds=1), show_alert=True)
        release_user_download(user.id)
        return

    progress_msg = query.message
    file_path = None
    edit_fn = query.edit_message_caption if progress_msg.caption else progress_msg.edit_text

    quiet = bool(settings.get("quiet_mode")) or not bool(settings.get("notify_progress", 1))

    # 🛑 Cancel support: a cancel button rides along on every status edit while
    # the download runs; pressing it sets the per-user cancel flag that every
    # download loop checks (yt-dlp progress hook, CDN chunk loop, fallback
    # matrix, album items, pre-upload checkpoints).
    cancel_markup = InlineKeyboardMarkup([
        [inline_button(t(lang, "cancel_download_button"), callback_data="dl_stop")]
    ])
    clear_cancel(user.id)
    if not quiet:
        await _quiet_edit(edit_fn, t(lang, "preparing_download"), parse_mode="HTML", reply_markup=cancel_markup)

    # Global concurrency gate — wait for a free slot (do not overwhelm server)
    if bool(settings.get("notify_queue", 1)) and not quiet:
        try:
            in_use, max_slots = active_global_slots()
            if in_use >= max_slots:
                await _quiet_edit(edit_fn, t(lang, "queue_wait", current=in_use, maximum=max_slots), parse_mode="HTML", reply_markup=cancel_markup)
        except Exception:
            pass

    progress_state = {"last_ts": 0.0, "last_pct": -1}

    async def update_progress(prog):
        try:
            if is_cancel_requested(user.id):
                return
            if isinstance(prog, dict):
                pct, dl, tot, spd, eta = prog.get("pct", 0), prog.get("downloaded", 0), prog.get("total", 0), prog.get("speed", 0), prog.get("eta", 0)
            else:
                pct, dl, tot, spd, eta = prog, 0, 0, 0, 0
            now = time.monotonic()
            if pct < 100 and (now - progress_state["last_ts"] < 2) and abs(int(pct) - int(progress_state["last_pct"])) < 8:
                return
            progress_state["last_ts"] = now
            progress_state["last_pct"] = int(pct)
            bar = make_progress_bar(pct)
            txt = t(lang, "downloading", bar=bar, percent=pct, downloaded=format_size(dl), total=format_size(tot), speed=f"{format_size(int(spd))}/s", eta=f"{int(eta)}s")
            if progress_msg.caption:
                await progress_msg.edit_caption(txt, parse_mode="HTML", reply_markup=cancel_markup)
            else:
                await progress_msg.edit_text(txt, parse_mode="HTML", reply_markup=cancel_markup)
        except Exception:
            pass

    async def _noop_progress(prog):
        pass
    if quiet:
        update_progress = _noop_progress

    # The downloader reads this attribute synchronously inside worker threads
    # (yt-dlp hooks, requests chunk loops) and raises DownloadCancelledError,
    # which aborts the in-thread download immediately.
    update_progress.cancel_check = lambda: check_cancelled(user.id)

    try:
        async with download_slot(timeout=180):
            await _run_download(query, context, info, user, lang, quality_label,
                                is_audio, is_image, is_album, edit_fn, update_progress,
                                audio_bitrate=audio_bitrate, delivery_mode=delivery_mode,
                                settings=settings)
    except DownloadCancelledError:
        logger.info("Download cancelled by user=%s url=%s", user.id, str(info.get("url", ""))[:120])
        await _quiet_edit(edit_fn, t(lang, "download_cancelled"), parse_mode="HTML")
    except asyncio.TimeoutError:
        await _quiet_edit(edit_fn, t(lang, "server_busy"))
    except FileTooLargeError as exc:
        # Report the limit that ACTUALLY rejected the file: the user's
        # personal max_file_size_mb (from Settings) or Telegram's global cap.
        # Previously the user's limit was shown whenever it was set, even when
        # the global cap was the one that fired — a misleading message.
        user_max = int(settings.get("max_file_size_mb") or 0)
        user_limit_bytes = user_max * 1024 * 1024 if user_max > 0 else 0
        if exc.limit_bytes and user_limit_bytes and exc.limit_bytes == user_limit_bytes:
            limit_text = format_size(exc.limit_bytes)
            await _quiet_edit(
                edit_fn,
                t(lang, "file_too_large_user", max_size=limit_text),
                parse_mode="HTML",
            )
        else:
            limit_bytes = exc.limit_bytes or MAX_FILE_SIZE_MB * 1024 * 1024
            await _quiet_edit(
                edit_fn,
                t(lang, "file_too_large", max_size=format_size(limit_bytes)),
            )
    except Exception as e:
        logger.exception("Download error for user=%s url=%s: %s", user.id, str(info.get("url", ""))[:120], e)
        # CDN segment rejection (expired token / geo-block / rate-limit) gets a
        # short, human message instead of yt-dlp's cryptic technical text.
        empty_cdn = isinstance(e, DownloadImpossibleError) and bool(getattr(e, "empty_file", False))
        reason = "cdn_rejected" if empty_cdn else summarize_error(e)
        _log_clone_failure(context, user.id, str(info.get("url") or ""), reason,
                           platform=str(info.get("platform") or ""))
        show_detail = bool(settings.get("notify_failure", 1))
        try:
            if show_detail:
                if empty_cdn:
                    await _quiet_edit(edit_fn, t(lang, "cdn_rejected"), parse_mode="HTML")
                else:
                    await _quiet_edit(edit_fn,
                        t(lang, "download_failed_reason", reason=escape(reason)),
                        parse_mode="HTML",
                    )
            else:
                await _quiet_edit(edit_fn, t(lang, "download_failed"))
        except Exception:
            # Never let a failed status edit mask the original download error.
            try:
                await _quiet_edit(edit_fn, t(lang, "download_failed"))
            except Exception:
                pass
    finally:
        release_user_download(user.id)
        clear_cancel(user.id)
        if file_path and os.path.exists(file_path):
            _cleanup_file(file_path)


async def _run_download(query, context, info, user, lang, quality_label,
                        is_audio, is_image, is_album, edit_fn, update_progress,
                        audio_bitrate: str | None = None, delivery_mode: str = "video",
                        settings: dict | None = None):
    file_path = None
    complete_caption = _completion_caption(lang, settings)
    quiet = bool((settings or {}).get("quiet_mode"))

    async def _status(text, **kwargs):
        """Edit the action card with a status line, unless quiet mode is on."""
        if quiet:
            return
        await _quiet_edit(edit_fn, text, **kwargs)

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
                    await _status(txt, parse_mode="HTML")
                except Exception:
                    pass

            await _status(t(lang, "preparing_download"), parse_mode="HTML")
            downloaded = await download_album(items, album_progress)
            if not downloaded:
                raise Exception("Album download failed")

            for _item in downloaded:
                _enforce_user_size(_item.get("path"), settings)

            await _status(
                t(lang, "album_uploading", count=len(downloaded)),
                parse_mode="HTML",
            )

            # Telegram media groups require 2–10 items. Single item → normal send.
            try:
                if len(downloaded) == 1:
                    item = downloaded[0]
                    safe_title = _safe_upload_name(item.get("title") or title or "media", 40)

                    if item["type"] == "image":
                        async def _send_single_photo():
                            with open(item["path"], "rb") as fh:
                                return await query.message.reply_photo(
                                    photo=InputFile(fh, filename=f"{safe_title}.jpg"),
                                    caption=complete_caption,
                                )

                        sent = await _upload_with_retry(_send_single_photo)
                        _safe_store(
                            set_cache,
                            info["url"], quality_label, "image",
                            sent.photo[-1].file_id, title, platform,
                        )
                    else:
                        async def _send_single_video():
                            with open(item["path"], "rb") as fh:
                                return await query.message.reply_video(
                                    video=InputFile(fh, filename=f"{safe_title}.mp4"),
                                    caption=complete_caption,
                                    supports_streaming=True,
                                )

                        sent = await _upload_with_retry(_send_single_video)
                        _safe_store(
                            set_cache,
                            info["url"], quality_label, "video",
                            sent.video.file_id, title, platform,
                        )
                else:
                    async def _send_album_group():
                        open_handles = []
                        try:
                            media_group = []
                            for i, item in enumerate(downloaded[:ALBUM_MAX_ITEMS]):
                                fh = open(item["path"], "rb")
                                open_handles.append(fh)
                                caption = complete_caption if i == 0 else None
                                safe_title = _safe_upload_name(item.get("title") or title or "media", 40)
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
                            return await query.message.reply_media_group(media=media_group)
                        finally:
                            for fh in open_handles:
                                try:
                                    fh.close()
                                except Exception:
                                    pass

                    sent_msgs = await _upload_with_retry(_send_album_group)
                    if sent_msgs:
                        album_entries = []
                        for msg in sent_msgs:
                            if msg.photo:
                                album_entries.append({"file_id": msg.photo[-1].file_id, "type": "image"})
                            elif msg.video:
                                album_entries.append({"file_id": msg.video.file_id, "type": "video"})
                        if len(album_entries) >= 2:
                            _safe_store(set_cache_album, info["url"], album_entries, title, platform)
                        elif album_entries:
                            _safe_store(
                                set_cache,
                                info["url"], quality_label, album_entries[0]["type"],
                                album_entries[0]["file_id"], title, platform,
                            )
            finally:
                for item in downloaded:
                    p = item.get("path")
                    if p and os.path.exists(p):
                        _cleanup_file(p)
                file_path = None

        elif is_image:
            await _status(t(lang, "preparing_download"), parse_mode="HTML")
            file_path = await download_image(info["url"], info.get("image_url"))
            if not file_path: raise Exception("Download failed")
            _enforce_user_size(file_path, settings)
            await _status(t(lang, "uploading"), parse_mode="HTML")
            safe_title = _safe_upload_name(title)

            async def _send_photo():
                with open(file_path, "rb") as f:
                    return await query.message.reply_photo(
                        photo=InputFile(f, filename=f"{safe_title}.jpg"),
                        caption=complete_caption,
                    )

            sent = await _upload_with_retry(_send_photo)
            _fid = sent.photo[-1].file_id
            _vault = await _safe_vault(context, sent)
            _safe_store(
                set_cache_with_meta,
                info["url"], quality_label, "image", _fid,
                title=title, platform=platform,
                media_id=str(info.get("media_id") or ""),
                vault_chat_id=(_vault or {}).get("chat_id"),
                vault_message_id=(_vault or {}).get("message_id"),
            )

        elif is_audio:
            await _status(t(lang, "preparing_audio"), parse_mode="HTML")
            file_path = await download_audio(info["url"], update_progress, bitrate=audio_bitrate)
            if not file_path: raise Exception("Download failed")
            check_cancelled(user.id)
            _enforce_user_size(file_path, settings)
            await _status(t(lang, "uploading"), parse_mode="HTML")
            safe_title = _safe_upload_name(title)

            async def _send_audio():
                with open(file_path, "rb") as f:
                    return await query.message.reply_audio(
                        audio=InputFile(f, filename=f"{safe_title}.mp3"),
                        caption=complete_caption,
                    )

            sent = await _upload_with_retry(_send_audio)
            _fid = sent.audio.file_id
            _vault = await _safe_vault(context, sent)
            _safe_store(
                set_cache_with_meta,
                info["url"], quality_label, "audio", _fid,
                title=title, platform=platform,
                media_id=str(info.get("media_id") or ""),
                vault_chat_id=(_vault or {}).get("chat_id"),
                vault_message_id=(_vault or {}).get("message_id"),
            )

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

            await _status(t(lang, "preparing_download"), parse_mode="HTML")

            # Allow oversized files so they can be split into Telegram-friendly
            # parts instead of being deleted mid-download.
            allow_large = AUTO_SPLIT_LARGE_VIDEOS
            file_path = await download_video(
                info["url"], format_id, quality_label, update_progress,
                play_url=info.get("play_url"),
                allow_large=allow_large,
            )
            if not file_path:
                raise Exception("Download failed")

            _enforce_user_size(file_path, settings)

            if AUTO_SPLIT_LARGE_VIDEOS and os.path.getsize(file_path) > MAX_FILE_SIZE_BYTES:
                await _status(t(lang, "splitting_video"), parse_mode="HTML")
                parts = split_video_parts(file_path, MAX_FILE_SIZE_BYTES)
                if not parts:
                    raise FileTooLargeError(os.path.getsize(file_path), MAX_FILE_SIZE_BYTES)

                total = len(parts)
                safe_title = _safe_upload_name(title)
                # Honor notify_complete: when completion captions are off, split
                # parts are sent silently instead of stamping the brand on every
                # chunk.
                show_brand = complete_caption is not None
                try:
                    for idx, part_path in enumerate(parts, start=1):
                        async def _send_part(p=part_path, i=idx):
                            with open(p, "rb") as f:
                                part_label = (
                                    t(lang, "video_part", idx=i, total=total, bot_name=BOT_DISPLAY_NAME)
                                    if show_brand else None
                                )
                                # Honor the user's delivery_mode=file even for
                                # split parts — the preference must apply to
                                # every video surface, not just single files.
                                if delivery_mode == "file":
                                    return await query.message.reply_document(
                                        document=InputFile(f, filename=f"{safe_title}_part{i}.mp4"),
                                        caption=part_label,
                                    )
                                return await query.message.reply_video(
                                    video=InputFile(f, filename=f"{safe_title}_part{i}.mp4"),
                                    caption=part_label,
                                    supports_streaming=True,
                                )

                        await _upload_with_retry(_send_part)
                finally:
                    for part in parts:
                        if part and part != file_path and os.path.exists(part):
                            _cleanup_file(part)
            else:
                await _status(t(lang, "uploading"), parse_mode="HTML")
                safe_title = _safe_upload_name(title)

                async def _send_video():
                    with open(file_path, "rb") as f:
                        if delivery_mode == "file":
                            return await query.message.reply_document(
                                document=InputFile(f, filename=f"{safe_title}.mp4"),
                                caption=complete_caption,
                            )
                        return await query.message.reply_video(
                            video=InputFile(f, filename=f"{safe_title}.mp4"),
                            caption=complete_caption,
                            supports_streaming=True,
                        )

                sent = await _upload_with_retry(_send_video)
                _fid = sent.video.file_id if sent.video else sent.document.file_id
                _vault = await _safe_vault(context, sent)
                _safe_store(
                    set_cache_with_meta,
                    info["url"], quality_label, "video", _fid,
                    title=title, platform=platform,
                    media_id=str(info.get("media_id") or ""),
                    vault_chat_id=(_vault or {}).get("chat_id"),
                    vault_message_id=(_vault or {}).get("message_id"),
                )

        _safe_store(increment_downloads, user.id)
        _safe_store(
            log_download,
            user.id, info["url"], title, platform, quality_label,
            "audio" if is_audio else ("image" if is_image else "video"),
            instance_id=plan_policy.plan_context(context.bot).get("instance_id", 0),
        )
    finally:
        if file_path and os.path.exists(file_path):
            _cleanup_file(file_path)
