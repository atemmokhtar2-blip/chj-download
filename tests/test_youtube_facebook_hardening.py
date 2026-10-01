from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.youtube_scraper import _safe_selector
from services.facebook_scraper import _from_ytdlp


class FakeYDL:
    def __init__(self, _opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        return {
            "id": "fb123",
            "title": "FB test",
            "webpage_url": "https://www.facebook.com/watch/?v=123",
            "formats": [
                {
                    "format_id": "dash-video",
                    "url": "https://video.xx.fbcdn.net/video-only.mp4",
                    "height": 1080,
                    "tbr": 2500,
                    "vcodec": "avc1",
                    "acodec": "none",
                },
                {
                    "format_id": "progressive",
                    "url": "https://video.xx.fbcdn.net/progressive.mp4",
                    "height": 720,
                    "tbr": 1500,
                    "vcodec": "avc1",
                    "acodec": "mp4a.40.2",
                },
            ],
        }


def test_youtube_selector_prefers_audio_merge_not_video_only() -> None:
    selector = _safe_selector(720)
    assert "+bestaudio" in selector
    assert "acodec!=none" in selector
    assert "best[ext=mp4]/best" not in selector


def test_facebook_ytdlp_prefers_progressive_with_audio() -> None:
    with patch("yt_dlp.YoutubeDL", FakeYDL):
        result = _from_ytdlp("https://www.facebook.com/watch/?v=123")
    assert result is not None
    assert result["play_url"].endswith("progressive.mp4")
    assert result["direct_has_audio"] is True
    assert result.get("prefer_page_download") is not True


class FakeVideoOnlyYDL(FakeYDL):
    def extract_info(self, _url, download=False):
        return {
            "id": "fb124",
            "title": "FB silent",
            "webpage_url": "https://www.facebook.com/watch/?v=124",
            "formats": [
                {
                    "format_id": "dash-video",
                    "url": "https://video.xx.fbcdn.net/video-only.mp4",
                    "height": 1080,
                    "tbr": 2500,
                    "vcodec": "avc1",
                    "acodec": "none",
                },
            ],
        }


def test_facebook_ytdlp_marks_video_only_for_page_fallback() -> None:
    with patch("yt_dlp.YoutubeDL", FakeVideoOnlyYDL):
        result = _from_ytdlp("https://www.facebook.com/watch/?v=124")
    assert result is not None
    assert result["direct_has_audio"] is False
    assert result["prefer_page_download"] is True


if __name__ == "__main__":
    test_youtube_selector_prefers_audio_merge_not_video_only()
    test_facebook_ytdlp_prefers_progressive_with_audio()
    test_facebook_ytdlp_marks_video_only_for_page_fallback()
    print("youtube/facebook hardening tests passed")
