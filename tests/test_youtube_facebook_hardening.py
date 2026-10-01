from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.youtube_scraper import _safe_selector
from services.facebook_scraper import _from_ytdlp
from services.downloader import _youtube_client_profiles
from services.tiktok_scraper import resolve_tiktok


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


def test_youtube_uses_multi_surface_client_profiles() -> None:
    profiles = _youtube_client_profiles()
    flat = {client for _name, clients in profiles for client in clients}
    assert "android" in flat
    assert "ios" in flat
    assert "tv_embedded" in flat
    assert "web_creator" in flat
    assert any(len(clients) > 1 for _name, clients in profiles)


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


def test_tiktok_resolver_keeps_ranked_download_candidates() -> None:
    def low(_url):
        return {"play_url": "https://cdn.example/low.mp4", "source": "low", "height": 360, "title": "t"}

    def high(_url):
        return {"play_url": "https://cdn.example/high.mp4", "source": "high", "height": 1080, "title": "t"}

    providers = [("low", low), ("high", high)]
    with patch("services.tiktok_scraper.expand_tiktok_url", return_value="https://www.tiktok.com/@u/video/1234567890123456789"), \
         patch("services.tiktok_scraper.PROVIDERS", providers), \
         patch("services.tiktok_scraper._probe_media_url", return_value=(True, 2_000_000, "video/mp4")):
        result = resolve_tiktok("https://vm.tiktok.com/x")
    assert result is not None
    urls = [c["play_url"] for c in result["download_candidates"]]
    assert "https://cdn.example/high.mp4" in urls
    assert "https://cdn.example/low.mp4" in urls
    assert len(urls) == 2


if __name__ == "__main__":
    test_youtube_selector_prefers_audio_merge_not_video_only()
    test_youtube_uses_multi_surface_client_profiles()
    test_facebook_ytdlp_prefers_progressive_with_audio()
    test_facebook_ytdlp_marks_video_only_for_page_fallback()
    test_tiktok_resolver_keeps_ranked_download_candidates()
    print("youtube/facebook/tiktok hardening tests passed")
