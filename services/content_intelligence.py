from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from config.settings import MAX_FILE_SIZE_BYTES


@dataclass
class ContentIntelligence:
    score: int
    recommendation: str
    best_quality: str
    estimated_size: str
    risk: str
    badges: list[str]
    details: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _human_size(size: int | float | None) -> str:
    if not size or size <= 0:
        return "Unknown"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _quality_height(label: str) -> int:
    try:
        return int(str(label).lower().replace("p", "").strip())
    except Exception:
        return 0


def _estimate_size_from_formats(formats: list[dict[str, Any]]) -> int:
    candidates: list[int] = []
    for fmt in formats or []:
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if isinstance(size, (int, float)) and size > 0:
            candidates.append(int(size))
    return max(candidates) if candidates else 0


def build_content_intelligence(info: dict[str, Any], raw_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a compact, user-facing intelligence card for a media item.

    This never blocks downloads. It only uses metadata already returned by yt-dlp
    and gives the bot a premium feel: recommendations, file-size risk and badges.
    """
    raw_info = raw_info or {}
    formats = raw_info.get("formats") or []
    qualities = info.get("qualities") or []
    media_type = info.get("media_type", "video")
    platform = info.get("platform") or "Generic"
    duration = raw_info.get("duration") or 0
    view_count = raw_info.get("view_count") or 0
    like_count = raw_info.get("like_count") or 0
    estimated_size = _estimate_size_from_formats(formats)

    best_quality = "best"
    if qualities:
        best_quality = max(qualities, key=lambda q: _quality_height(q.get("label", "0p"))).get("label", "best")

    badges: list[str] = []
    details: list[str] = []
    score = 72

    if media_type == "album":
        badges.append("Album-ready")
        score += 5
    elif media_type == "audio":
        badges.append("Audio optimized")
        score += 6
    elif media_type == "image":
        badges.append("Fast image delivery")
        score += 4
    else:
        badges.append("Video + MP3")

    if _quality_height(best_quality) >= 1080:
        badges.append("HD/Full HD")
        score += 8
    elif _quality_height(best_quality) >= 720:
        badges.append("HD")
        score += 5

    if duration:
        if duration <= 60:
            badges.append("Short-form")
            score += 5
        elif duration >= 1800:
            badges.append("Long video")
            score -= 8
        details.append(f"Duration: {int(duration // 60)}m {int(duration % 60)}s")

    if view_count:
        details.append(f"Views: {view_count:,}")
        if view_count >= 1_000_000:
            badges.append("Viral")
            score += 5
    if like_count:
        details.append(f"Likes: {like_count:,}")

    if estimated_size:
        ratio = estimated_size / max(MAX_FILE_SIZE_BYTES, 1)
        if ratio >= 1:
            risk = "high"
            recommendation = "Use MP3 or a lower video quality because the best file may exceed Telegram limits."
            score -= 22
        elif ratio >= 0.72:
            risk = "medium"
            recommendation = "Best quality should work, but 720p/MP3 is safer on slow networks."
            score -= 7
        else:
            risk = "low"
            recommendation = "Best quality is recommended for this link."
            score += 4
    else:
        risk = "unknown"
        recommendation = "Best quality is recommended; MP3 is available when you only need audio."

    details.append(f"Platform: {platform}")
    details.append(f"Best quality: {best_quality}")
    details.append(f"Estimated max size: {_human_size(estimated_size)}")

    score = max(1, min(99, score))
    return ContentIntelligence(
        score=score,
        recommendation=recommendation,
        best_quality=best_quality,
        estimated_size=_human_size(estimated_size),
        risk=risk,
        badges=badges[:5],
        details=details[:8],
    ).to_dict()


def render_intelligence_html(intel: dict[str, Any] | None, lang: str = "en") -> str:
    if not intel:
        return ""
    badges = " • ".join(intel.get("badges") or [])
    risk = str(intel.get("risk") or "unknown").upper()
    if lang == "ar":
        return (
            "\n\n🧠 <b>التحليل الذكي</b>"
            f"\n• النتيجة: <b>{intel.get('score', '-')}/99</b>"
            f"\n• أفضل جودة: <b>{intel.get('best_quality', 'best')}</b>"
            f"\n• الحجم المتوقع: <b>{intel.get('estimated_size', 'Unknown')}</b>"
            f"\n• مخاطرة الحجم: <b>{risk}</b>"
            f"\n• {badges}"
        )
    return (
        "\n\n🧠 <b>Smart intelligence</b>"
        f"\n• Score: <b>{intel.get('score', '-')}/99</b>"
        f"\n• Best quality: <b>{intel.get('best_quality', 'best')}</b>"
        f"\n• Estimated size: <b>{intel.get('estimated_size', 'Unknown')}</b>"
        f"\n• Size risk: <b>{risk}</b>"
        f"\n• {badges}"
    )
