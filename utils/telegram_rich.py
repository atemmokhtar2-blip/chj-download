"""Native Telegram Rich Message table helper."""
from html import escape
import logging

logger = logging.getLogger(__name__)


def plan_table_html(policy, ctx):
    b = policy.PLAN_LIMITS[policy.PLAN_BASIC]
    p = policy.PLAN_LIMITS[policy.PLAN_PRO]
    # Keep every cell short so Telegram can fit all three columns on a phone.
    rows = [
        ("الميزة", "🆓 Basic", "👑 Pro"),
        ("📥 يوميًّا", f"{b['daily_downloads']}/يوم", f"{p['daily_downloads']}/يوم"),
        ("🔗 الدفعة", f"{b['batch_urls']} روابط", f"{p['batch_urls']} روابط"),
        ("🎬 الفيديو", f"حتى {b['max_video_height']}p", "الأعلى"),
        ("🎵 MP3", f"{b['audio_cap_kbps']} kbps", f"{p['audio_cap_kbps']} kbps"),
        ("📚 السجل", f"{b['history_days']} أيام", f"{p['history_days']} يوم"),
        ("🛠️ لوحة الإدارة", "❌", "✅"),
        ("👥 المستخدمون", "❌", "✅"),
        ("📣 الإذاعة", "❌", "✅"),
        ("🔐 صلاحيات الإداريين", "❌", "✅"),
        ("🧩 إدارة النسخ", "للمالك", "للمالك"),
    ]
    trs = []
    for i, row in enumerate(rows):
        tag = "th" if i == 0 else "td"
        trs.append("<tr>" + "".join(f"<{tag}>{escape(str(cell))}</{tag}>" for cell in row) + "</tr>")
    html = "<h3>📦 مقارنة الخطط</h3><table bordered striped compact>" + "".join(trs) + "</table>"
    if ctx.get("is_clone"):
        plan = policy.effective_plan(ctx.get("instance")) if ctx.get("instance") else policy.normalize_plan(ctx.get("plan"))
        html += "<p><b>خطتك:</b> " + ("👑 Pro" if plan == policy.PLAN_PRO else "🆓 Basic") + "</p>"
        try:
            from services.referral import instance_days_left
            days = instance_days_left(ctx.get("instance") or {})
            if days is not None:
                html += f"<p>⏳ المتبقي: {int(days)} يوم</p>"
        except Exception:
            logger.debug("Could not resolve plan expiry", exc_info=True)
    else:
        html += "<p>البوت الأساسي: لا تنطبق عليه حدود النسخ.</p>"
    return html

async def edit_plan_table(query, context, policy, reply_markup, fallback_text):
    message = getattr(query, "message", None)
    if message is None:
        await query.edit_message_text(fallback_text, parse_mode="HTML", reply_markup=reply_markup)
        return
    payload = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "rich_message": {"html": plan_table_html(policy, policy.plan_context(context.bot))},
        "reply_markup": reply_markup,
    }
    try:
        await context.bot.do_api_request(endpoint="editMessageText", api_kwargs=payload)
    except Exception:
        logger.exception("Rich table edit failed; falling back to normal text")
        await query.edit_message_text(fallback_text, parse_mode="HTML", reply_markup=reply_markup)
