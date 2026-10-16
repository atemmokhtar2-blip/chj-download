"""Regression: every platform engine must walk past a dead/silent CDN candidate
instead of letting one empty HLS/mp4 abort the whole platform download.

Direct candidates are the first line of defence for TikTok, Instagram,
Pinterest, Reddit and the EmbedFallback family (Threads, Vimeo, Dailymotion,
Snapchat, Telegram, Likee, Kwai, Twitch, Bilibili, VK, Tumblr, Odysee,
Niconico, Douyin, Xiaohongshu, Rumble, Odnoklassniki). These tests assert the
shared pattern: a candidate that downloads zero usable bytes is skipped and the
next candidate (or the yt-dlp fallback) takes over.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import downloader
from services.engines.platforms import (
    EmbedFallbackEngine,
    InstagramEngine,
    PinterestEngine,
    RedditEngine,
    TikTokEngine,
)


def _identity(path, **kwargs):
    return path


def test_pinterest_engine_walks_past_dead_mp4(monkeypatch):
    """A stale progressive mp4 must not stop the next Pinterest rendition."""
    hits: list[str] = []

    def fake_direct(url, *args, **kwargs):
        hits.append(url)
        return None if "dead" in url else "/tmp/live.mp4"

    monkeypatch.setattr(downloader, "_download_direct_sync", fake_direct)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        "services.pinterest_scraper.scrape_pinterest",
        lambda url: {
            "source": "pin-resource",
            "provider_score": 100,
            "play_url": "https://cdn.example/dead.mp4",
            "download_candidates": [
                {"url": "https://cdn.example/dead.mp4", "kind": "mp4", "score": 100},
                {"url": "https://cdn.example/live.mp4", "kind": "mp4", "score": 90},
            ],
        },
    )

    path = asyncio.run(
        PinterestEngine().download_video(
            "https://www.pinterest.com/pin/123/", "best", "best"
        )
    )
    assert path == "/tmp/live.mp4"
    assert hits == ["https://cdn.example/dead.mp4", "https://cdn.example/live.mp4"]


def test_pinterest_engine_walks_past_dead_hls(monkeypatch):
    """An HLS pin whose first rendition is rejected must fall through to the
    next HLS rendition (HLS-only video pins exist on Pinterest)."""
    hits: list[str] = []

    def fake_hls(play_url, out_path):
        hits.append(play_url)
        return None if "dead" in play_url else "/tmp/live.mp4"

    monkeypatch.setattr(
        "services.pinterest_scraper.download_hls_video", fake_hls
    )
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        "services.pinterest_scraper.scrape_pinterest",
        lambda url: {
            "source": "pin-resource",
            "provider_score": 100,
            "play_url": "https://cdn.example/dead.m3u8",
            "download_candidates": [
                {"url": "https://cdn.example/dead.m3u8", "kind": "hls", "score": 100},
                {"url": "https://cdn.example/live.m3u8", "kind": "hls", "score": 90},
            ],
        },
    )

    path = asyncio.run(
        PinterestEngine().download_video(
            "https://www.pinterest.com/pin/123/", "best", "best"
        )
    )
    assert path == "/tmp/live.mp4"
    assert hits == ["https://cdn.example/dead.m3u8", "https://cdn.example/live.m3u8"]


def test_tiktok_engine_walks_past_dead_direct(monkeypatch):
    """TikTok direct no-watermark CDN candidates are walked until one is usable."""
    hits: list[str] = []

    def fake_tk_direct(play_url, out_path):
        hits.append(play_url)
        return None if "dead" in play_url else "/tmp/live.mp4"

    monkeypatch.setattr(
        "services.tiktok_scraper.download_tiktok_direct", fake_tk_direct
    )
    monkeypatch.setattr(
        "services.tiktok_scraper.scrape_tiktok",
        lambda url: {
            "source": "test",
            "provider_score": 10,
            "play_url": "https://cdn.example/live.mp4",
            "download_candidates": [
                {"provider": "p1", "play_url": "https://cdn.example/dead.mp4", "score": 100},
                {"provider": "p2", "play_url": "https://cdn.example/live.mp4", "score": 90},
            ],
        },
    )
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(downloader, "_enforce_max_file_size", lambda path: path)

    path = asyncio.run(
        TikTokEngine().download_video(
            "https://www.tiktok.com/@u/video/123", "best", "best"
        )
    )
    assert path == "/tmp/live.mp4"
    assert hits[:2] == [
        "https://cdn.example/dead.mp4",
        "https://cdn.example/live.mp4",
    ]


def test_instagram_engine_walks_past_dead_direct(monkeypatch):
    """Instagram direct CDN + album-slide candidates are walked before fallback."""
    hits: list[str] = []

    def fake_direct(url, *args, **kwargs):
        hits.append(url)
        return None if "dead" in url else "/tmp/live.mp4"

    monkeypatch.setattr(downloader, "_download_direct_sync", fake_direct)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        "services.instagram_scraper.scrape_instagram",
        lambda url: {
            "webpage_url": url,
            "source": "test",
            "play_url": "https://cdn.example/dead.mp4",
            "album_items": [
                {"type": "video", "play_url": "https://cdn.example/live.mp4"},
            ],
        },
    )

    path = asyncio.run(
        InstagramEngine().download_video(
            "https://www.instagram.com/reel/ABC/", "best", "best"
        )
    )
    assert path == "/tmp/live.mp4"
    assert hits == ["https://cdn.example/dead.mp4", "https://cdn.example/live.mp4"]


def test_reddit_engine_walks_past_dead_direct(monkeypatch):
    """Reddit's scored DASH/fallback candidates are walked until one verifies."""
    hits: list[str] = []

    def fake_direct(url, *args, **kwargs):
        hits.append(url)
        return None if "dead" in url else "/tmp/live.mp4"

    monkeypatch.setattr(downloader, "_download_direct_sync", fake_direct)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        "services.reddit_scraper.scrape_reddit",
        lambda url: {
            "source": "test",
            "play_url": "https://v.redd.it/dead_fallback.mp4",
            "formats": [
                {"url": "https://v.redd.it/dead_fallback.mp4"},
                {"url": "https://v.redd.it/live.mp4"},
            ],
        },
    )

    path = asyncio.run(
        RedditEngine().download_video(
            "https://www.reddit.com/r/videos/comments/abc/x/", "best", "best"
        )
    )
    assert path == "/tmp/live.mp4"
    # Reddit scores "fallback" + .mp4 highest, so the dead URL is attempted first.
    assert hits[0] == "https://v.redd.it/dead_fallback.mp4"
    assert hits == [
        "https://v.redd.it/dead_fallback.mp4",
        "https://v.redd.it/live.mp4",
    ]


def test_embed_engine_walks_past_dead_candidate(monkeypatch):
    """EmbedFallbackEngine (Threads/Dailymotion/Likee/VK/Twitch/... base) walks
    its resolver candidates past a dead direct URL."""

    class FakeEmbed(EmbedFallbackEngine):
        name = "FakeEmbedEngine"
        platform_label = "Fake"

        def _resolve(self, url):
            return {
                "url": url,
                "play_url": "https://cdn.example/dead.mp4",
                "video_candidates": [
                    "https://cdn.example/dead.mp4",
                    "https://cdn.example/live.mp4",
                ],
            }

    hits: list[str] = []

    def fake_direct(url, *args, **kwargs):
        hits.append(url)
        return None if "dead" in url else "/tmp/live.mp4"

    monkeypatch.setattr(downloader, "_download_direct_sync", fake_direct)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)

    path = asyncio.run(FakeEmbed().download_video("https://example.com/video", "best", "best"))
    assert path == "/tmp/live.mp4"
    assert hits == ["https://cdn.example/dead.mp4", "https://cdn.example/live.mp4"]


if __name__ == "__main__":
    print("engine empty-file walk smoke tests import OK")
