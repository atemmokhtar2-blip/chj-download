from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import (
    _format_height_candidates,
    _heights_from_urls,
    _collect_quality_candidates,
    _finalize_analysis,
    normalize_qualities,
)
from services.content_intelligence import render_intelligence_html


def test_format_height_candidates_keep_only_real_video_rows() -> None:
    formats = [
        {"height": 720, "vcodec": "avc1.4d401f", "acodec": "mp4a", "format_id": "v720"},
        {"height": 480, "vcodec": "avc1.4d401e", "acodec": "none", "format_id": "v480"},
        {"height": 0, "vcodec": "none", "acodec": "mp4a", "format_id": "a_only"},
        {"vcodec": "avc1", "format_id": "no_height_row"},
    ]
    assert _format_height_candidates(formats) == [
        {"label": "720p", "format_id": "v720", "has_audio": True},
        {"label": "480p", "format_id": "v480", "has_audio": False},
    ]


def test_heights_from_urls_parses_concrete_resolutions() -> None:
    assert _heights_from_urls("https://v.redd.it/abc/DASH_720.mp4?source=fallback") == [720]
    assert _heights_from_urls("https://cdn.example/1920x1080/video.mp4") == [1080]
    assert _heights_from_urls("https://cdn.example/video.mp4") == []
    assert _heights_from_urls(None) == []
    assert _heights_from_urls("not-a-url") == []


def test_facebook_direct_height_becomes_single_real_quality() -> None:
    info = {
        "qualities": [{"label": "HD", "format_id": "hd"}, {"label": "Best", "format_id": "best"}],
        "direct_height": 720,
        "intelligence": {"best_quality": "best", "details": ["Best quality: best"]},
    }
    out = _finalize_analysis(info)
    assert out["qualities"] == [{"label": "720p", "format_id": "best"}]
    assert out["intelligence"]["best_quality"] == "720p"
    assert out["intelligence"]["available_qualities"] == ["720p"]


def test_reddit_dash_url_height_becomes_single_real_quality() -> None:
    info = {
        "qualities": [],
        "play_url": "https://v.redd.it/xyz/DASH_1080.mp4?source=fallback",
        "intelligence": {"best_quality": "best", "details": ["Best quality: best"]},
    }
    out = _finalize_analysis(info)
    assert out["qualities"] == [{"label": "1080p", "format_id": "best"}]
    assert out["intelligence"]["best_quality"] == "1080p"


def test_youtube_real_selector_rows_are_preserved() -> None:
    selector_720 = "bestvideo[height<=720]+bestaudio/best[height<=720]"
    selector_1080 = "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    info = {
        "qualities": [
            {"label": "720p", "format_id": selector_720, "has_audio": True},
            {"label": "1080p", "format_id": selector_1080, "has_audio": True},
        ],
        "intelligence": {"best_quality": "best", "details": ["Best quality: best"]},
    }
    out = _finalize_analysis(info)
    assert [q["label"] for q in out["qualities"]] == ["720p", "1080p"]
    assert out["qualities"][-1]["format_id"] == selector_1080
    assert out["intelligence"]["available_qualities"] == ["720p", "1080p"]


def test_finalize_never_fabricates_quality() -> None:
    info = {
        "qualities": [{"label": "Best", "format_id": "best"}, {"label": "HD", "format_id": "hd"}],
        "intelligence": {"best_quality": "best", "details": ["Best quality: best"]},
    }
    out = _finalize_analysis(info)
    assert out["qualities"] == []
    assert out["intelligence"]["best_quality"] == "best"
    assert out["intelligence"]["available_qualities"] == []


def test_tiktok_height_plus_fake_best_keeps_only_real() -> None:
    info = {
        "qualities": [
            {"label": "Best", "format_id": "best", "has_audio": True},
            {"label": "768p", "format_id": "best", "has_audio": True},
        ],
        "intelligence": {"best_quality": "best", "details": ["Best quality: best"]},
    }
    out = _finalize_analysis(info)
    assert out["qualities"] == [{"label": "720p", "format_id": "best", "has_audio": True}]


def test_intelligence_card_lists_available_qualities() -> None:
    intel = {
        "score": 82,
        "best_quality": "1080p",
        "estimated_size": "14.2 MB",
        "risk": "low",
        "badges": ["HD/Full HD"],
        "available_qualities": ["720p", "1080p"],
    }
    ar = render_intelligence_html(intel, "ar")
    en = render_intelligence_html(intel, "en")
    assert "الجودات المتاحة" in ar
    assert "720p • 1080p" in ar
    assert "Available qualities" in en
    assert "720p • 1080p" in en


if __name__ == "__main__":
    test_format_height_candidates_keep_only_real_video_rows()
    test_heights_from_urls_parses_concrete_resolutions()
    test_facebook_direct_height_becomes_single_real_quality()
    test_reddit_dash_url_height_becomes_single_real_quality()
    test_youtube_real_selector_rows_are_preserved()
    test_finalize_never_fabricates_quality()
    test_tiktok_height_plus_fake_best_keeps_only_real()
    test_intelligence_card_lists_available_qualities()
    print("quality probe tests passed")
