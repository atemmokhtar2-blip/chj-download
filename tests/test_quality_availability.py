from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import normalize_qualities, _finalize_analysis
from handlers.download import _build_action_keyboard


def test_drops_fake_best_and_hd_labels() -> None:
    assert normalize_qualities([{"label": "Best", "format_id": "best"}]) == []
    assert normalize_qualities([{"label": "HD", "format_id": "hd"}, {"label": "Best", "format_id": "best"}]) == []


def test_buckets_nonstandard_heights() -> None:
    assert normalize_qualities([{"label": "1916p", "format_id": "f"}]) == [
        {"label": "1440p", "format_id": "f"}
    ]
    assert normalize_qualities([{"label": "768p", "format_id": "best"}]) == [
        {"label": "720p", "format_id": "best"}
    ]


def test_dedupes_sorts_and_keeps_real_only() -> None:
    result = normalize_qualities([
        {"label": "1080p", "format_id": "a"},
        {"label": "720p", "format_id": "b"},
        {"label": "1080p", "format_id": "a"},
        {"label": "best", "format_id": "best"},
    ])
    assert [q["label"] for q in result] == ["720p", "1080p"]


def test_tiktok_mixed_result_keeps_single_real_quality() -> None:
    result = normalize_qualities([
        {"label": "Best", "format_id": "best", "has_audio": True},
        {"label": "768p", "format_id": "best", "has_audio": True},
    ])
    assert result == [{"label": "720p", "format_id": "best", "has_audio": True}]


def test_finalize_updates_intelligence_best_quality() -> None:
    info = {
        "qualities": [{"label": "Best", "format_id": "best"}, {"label": "1080p", "format_id": "s"}],
        "intelligence": {
            "best_quality": "best",
            "details": ["Platform: TikTok", "Best quality: best"],
        },
    }
    out = _finalize_analysis(info)
    assert out["qualities"] == [{"label": "1080p", "format_id": "s"}]
    assert out["intelligence"]["best_quality"] == "1080p"
    assert "Best quality: 1080p" in out["intelligence"]["details"]


def test_keyboard_never_renders_fake_quality_buttons() -> None:
    keyboard = _build_action_keyboard(
        "video",
        [{"label": "Best", "format_id": "best"}, {"label": "HD", "format_id": "hd"}],
        "ar",
        info={},
    )
    callback_data = {btn.callback_data for row in keyboard for btn in row}
    button_texts = {btn.text for row in keyboard for btn in row}
    forbidden_callbacks = {"dl_video_Best", "dl_video_HD"}
    assert forbidden_callbacks.isdisjoint(callback_data), callback_data
    assert not any(text.startswith("📹 Best") or text.startswith("📹 HD") for text in button_texts), button_texts


if __name__ == "__main__":
    test_drops_fake_best_and_hd_labels()
    test_buckets_nonstandard_heights()
    test_dedupes_sorts_and_keeps_real_only()
    test_tiktok_mixed_result_keeps_single_real_quality()
    test_finalize_updates_intelligence_best_quality()
    test_keyboard_never_renders_fake_quality_buttons()
    print("quality availability tests passed")
