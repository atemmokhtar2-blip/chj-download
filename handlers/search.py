from __future__ import annotations

import asyncio
import logging

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import ContextTypes

from middlewares.auth import is_banned
from middlewares.concurrency import get_executor
from locales import t
from utils.telegram_safe import safe_edit

logger = logging.getLogger(__name__)

SEARCH_RESULT_LIMIT = 10


def _fmt_duration(seconds) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _search_ytdlp(query: str, limit: int = 10) -> list[dict]:
    """Run a YouTube search through yt-dlp flat extraction."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    results: list[dict] = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        thumb = entry.get("thumbnail")
        if isinstance(thumb, list) and thumb:
            thumb = thumb[-1].get("url") if isinstance(thumb[-1], dict) else None
        results.append(
            {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "id": str(video_id),
                "title": entry.get("title") or "Untitled",
                "uploader": entry.get("uploader") or entry.get("channel") or "YouTube",
                "duration": _fmt_duration(entry.get("duration")),
                "thumbnail": thumb,
            }
        )
    return results[:limit]


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text(t("ar", "banned"), parse_mode="HTML")
        return

    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text(t("ar", "search_usage"), parse_mode="HTML")
        return

    status = await update.message.reply_text("🔎 جاري البحث...")
    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            get_executor(), _search_ytdlp, query, SEARCH_RESULT_LIMIT
        )
    except Exception as exc:
        logger.warning("Bot search failed for %r: %s", query, exc)
        results = []

    if not results:
        await status.edit_text(t("ar", "search_no_results"), parse_mode="HTML")
        return

    context.user_data["search_results"] = [item["url"] for item in results]
    rows = [
        [InlineKeyboardButton(f"{idx}. {item['title'][:50]}", callback_data=f"search_{idx - 1}")]
        for idx, item in enumerate(results[:SEARCH_RESULT_LIMIT], start=1)
    ]
    text = "🔎 <b>نتائج البحث:</b>\n\n" + "\n".join(
        f"<b>{idx}.</b> {item['title'][:70]} • {item['duration'] or '?'}"
        for idx, item in enumerate(results[:SEARCH_RESULT_LIMIT], start=1)
    )
    await status.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if is_banned(user.id):
        await query.answer(t("ar", "banned"), show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("search_"):
        return

    results = context.user_data.get("search_results") or []
    try:
        idx = int(data.split("_")[-1])
    except ValueError:
        await query.answer("⚠️ انتهت صلاحية النتائج. ابحث مرة أخرى.")
        return

    if idx < 0 or idx >= len(results):
        await query.answer("⚠️ انتهت صلاحية النتائج. ابحث مرة أخرى.")
        return

    await query.answer()
    try:
        await query.edit_message_text("🔎 جاري تحليل النتيجة المختارة...")
    except Exception:
        pass

    from handlers.download import process_url

    await process_url(update, context, user, results[idx], query.message)


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = (update.inline_query.query or "").strip()
    if not query_text:
        await update.inline_query.answer(
            [],
            switch_pm_text="اكتب اسم الفيديو للبحث",
            switch_pm_parameter="search",
            cache_time=0,
        )
        return

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            get_executor(), _search_ytdlp, query_text, 10
        )
    except Exception as exc:
        logger.warning("Inline search failed for %r: %s", query_text, exc)
        results = []

    inline_results = []
    for item in results:
        kwargs = {
            "id": f"yt_{item['id']}",
            "title": (item["title"] or "YouTube")[:128],
            "input_message_content": InputTextMessageContent(message_text=item["url"]),
        }
        description = " • ".join(
            part for part in (item.get("uploader"), item.get("duration")) if part
        )
        if description:
            kwargs["description"] = description[:256]
        if item.get("thumbnail"):
            kwargs["thumbnail_url"] = item["thumbnail"]
        inline_results.append(InlineQueryResultArticle(**kwargs))

    await update.inline_query.answer(inline_results, cache_time=60, is_personal=True)
