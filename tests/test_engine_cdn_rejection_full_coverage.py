"""Full-coverage regression: CDN empty-file / zero-byte rejection across EVERY
downloader engine, plus cancellation & size propagation through the candidate
walk loops.

The previous regression files proved the downloader layer (`EmptyFileError`,
friendly "zero data" message, dead-target skip) and the walk behaviour for
TikTok / Instagram / Pinterest / Reddit / EmbedFallback. This file closes the
remaining surface:

  * direct CDN download returns None on a zero-byte body,
  * Pinterest's HLS ffmpeg path returns None on an empty remux,
  * Facebook walks its page variants before admitting defeat,
  * Twitter / Vimeo / Imgur / Streamable / Coub / EmbedFallback all walk past
    a dead candidate and surface the friendly error when every path is dead,
  * ``DownloadCancelledError`` and ``FileTooLargeError`` are NEVER swallowed by
    a candidate loop (🛑 must abort immediately),
  * the outer ``downloader.download_video`` still raises the friendly error
    even when an engine swallows everything internally.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from services import downloader
from services.engines.platforms import (
    CoubEngine,
    EmbedFallbackEngine,
    FacebookEngine,
    ImgurEngine,
    InstagramEngine,
    PinterestEngine,
    RedditEngine,
    StreamableEngine,
    TwitterEngine,
    VimeoEngine,
    YouTubeEngine,
    UniversalEngine,
)
from middlewares.cancellation import DownloadCancelledError
from utils import url_safety as usafe

ZERO_DATA = "The media stream returned zero data — the host CDN rejected or expired the segment URLs."


def _identity(path, **kwargs):
    return path


# --------------------------------------------------------------------------- #
# Downloader internals: zero-byte direct/HLS files must read as "no usable media".
# --------------------------------------------------------------------------- #

class _EmptyBodyResponse:
    status_code = 200
    headers = {"Content-Type": "video/mp4", "Content-Length": "0"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, chunk_size=0):
        return iter(())


def test_direct_download_zero_byte_body_returns_none(monkeypatch, tmp_path):
    """A CDN that answers 200 with zero bytes yields None, never an empty file."""
    monkeypatch.setattr(usafe, "is_public_url", lambda u: (True, ""))
    monkeypatch.setattr("requests.get", lambda *a, **kw: _EmptyBodyResponse())
    monkeypatch.setattr(downloader, "TEMP_DIR", str(tmp_path))

    assert downloader._download_direct_sync("https://cdn.example/dead.mp4") is None


def test_pinterest_hls_empty_output_returns_none(monkeypatch, tmp_path):
    """Pinterest's dedicated HLS ffmpeg path must return None when the remux is
    empty, so the engine walks to the next rendition instead of delivering junk."""
    import services.pinterest_scraper as ps
    from services import downloader as dl

    monkeypatch.setattr(usafe, "is_public_url", lambda u: (True, ""))
    monkeypatch.setattr(dl, "_ffmpeg_path", lambda: "/bin/true")

    class FakeCompleted:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeCompleted())

    out = tmp_path / "empty.mp4"
    out.write_bytes(b"")
    result = ps.download_hls_video(
        "https://v1.pinimg.com/videos/mc/hls/stale.m3u8", str(out)
    )
    assert result is None
    assert not out.exists()


# --------------------------------------------------------------------------- #
# Routing: every platform lands on the right engine (dedicated vs universal).
# --------------------------------------------------------------------------- #

def test_router_selects_dedicated_and_universal_engines():
    assert isinstance(
        downloader._select_engine("https://www.pinterest.com/pin/123/"), PinterestEngine
    )
    assert isinstance(downloader._select_engine("https://youtu.be/abc123"), YouTubeEngine)
    assert isinstance(
        downloader._select_engine("https://some-obscure-site.example/watch?v=1"), UniversalEngine
    )


# --------------------------------------------------------------------------- #
# Engines that walk candidates then fall back to generic yt-dlp.
# --------------------------------------------------------------------------- #

def test_vimeo_engine_walks_past_dead_candidate(monkeypatch):
    hits: list[str] = []

    def fake_direct(url, *args, **kwargs):
        hits.append(url)
        return None if "dead" in url else "/tmp/live.mp4"

    monkeypatch.setattr(downloader, "_download_direct_sync", fake_direct)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        "services.light_scrapers.resolve_vimeo",
        lambda url: {
            "url": url,
            "play_url": "https://cdn.example/dead.mp4",
            "video_candidates": [
                "https://cdn.example/dead.mp4",
                "https://cdn.example/live.mp4",
            ],
        },
    )

    path = asyncio.run(VimeoEngine().download_video("https://vimeo.com/123", "best", "best"))
    assert path == "/tmp/live.mp4"
    assert hits == ["https://cdn.example/dead.mp4", "https://cdn.example/live.mp4"]


@pytest.mark.parametrize(
    "engine_cls,resolver_attr",
    [
        (ImgurEngine, "resolve_imgur"),
        (StreamableEngine, "resolve_streamable"),
        (CoubEngine, "resolve_coub"),
    ],
)
def test_single_media_engines_walk_past_dead_candidate(monkeypatch, engine_cls, resolver_attr):
    hits: list[str] = []

    def fake_direct(url, *args, **kwargs):
        hits.append(url)
        return None if "dead" in url else "/tmp/live.mp4"

    monkeypatch.setattr(downloader, "_download_direct_sync", fake_direct)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        f"services.light_scrapers.{resolver_attr}",
        lambda url: {
            "url": url,
            "play_url": "https://cdn.example/dead.mp4",
            "video_candidates": [
                "https://cdn.example/dead.mp4",
                "https://cdn.example/live.mp4",
            ],
        },
    )

    path = asyncio.run(engine_cls().download_video("https://example.com/watch/1", "best", "best"))
    assert path == "/tmp/live.mp4"
    assert hits == ["https://cdn.example/dead.mp4", "https://cdn.example/live.mp4"]


class _FakeEmbed(EmbedFallbackEngine):
    name = "FakeEmbedEngine"
    platform_label = "Fake"

    def _resolve(self, url):
        return {
            "url": url,
            "play_url": "https://cdn.example/dead1.mp4",
            "video_candidates": [
                "https://cdn.example/dead2.mp4",
            ],
        }


def test_embed_engine_all_dead_raises_friendly_error(monkeypatch):
    """EmbedFallback family (Threads/Dailymotion/Snapchat/Likee/Kwai/Twitch/
    Bilibili/VK/Tumblr/Rumble/Odysee/Niconico/Douyin/Xiaohongshu/Odnoklassniki)
    surfaces the clear zero-data message when every direct candidate is dead."""
    monkeypatch.setattr(downloader, "_download_direct_sync", lambda *a, **kw: None)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)

    async def fake_generic(*args, **kwargs):
        raise RuntimeError(ZERO_DATA)

    monkeypatch.setattr(downloader, "_generic_download_video", fake_generic)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(_FakeEmbed().download_video("https://example.com/video", "best", "best"))
    assert "zero data" in str(exc_info.value)


def test_pinterest_engine_forwards_dead_play_as_generic_fallback(monkeypatch):
    """When every Pinterest rendition dies, the engine must still pass the
    strongest dead CDN URL to generic yt-dlp as its first target (which the
    downloader then skips in favour of the canonical page URL)."""
    calls: list[tuple[str, str | None]] = []

    async def fake_generic(url, format_id, quality_label, progress, play_url=None, **kw):
        calls.append((url, play_url))
        return "/tmp/generic.mp4"

    monkeypatch.setattr(downloader, "_download_direct_sync", lambda *a, **kw: None)
    monkeypatch.setattr("services.pinterest_scraper.download_hls_video", lambda url, out: None)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(downloader, "_generic_download_video", fake_generic)
    monkeypatch.setattr(
        "services.pinterest_scraper.scrape_pinterest",
        lambda url: {
            "source": "pin-resource",
            "provider_score": 10,
            "play_url": "https://cdn.example/dead.m3u8",
            "download_candidates": [
                {"url": "https://cdn.example/dead.m3u8", "kind": "hls", "score": 10},
            ],
        },
    )

    path = asyncio.run(
        PinterestEngine().download_video("https://www.pinterest.com/pin/123/", "best", "best")
    )
    assert path == "/tmp/generic.mp4"
    assert calls == [
        ("https://www.pinterest.com/pin/123/", "https://cdn.example/dead.m3u8")
    ]


def test_facebook_engine_walks_page_variants_then_admits_defeat(monkeypatch):
    """Facebook tries m./mbasic page variants. Even when all are dead it does not
    crash; the outer downloader then surfaces the generic friendly error."""
    page_calls: list[str] = []

    async def fake_generic(page_url, *args, **kwargs):
        page_calls.append(page_url)
        raise RuntimeError(ZERO_DATA)

    monkeypatch.setattr(downloader, "_download_direct_sync", lambda *a, **kw: None)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        "services.facebook_scraper.scrape_facebook",
        lambda url: {
            "webpage_url": "https://www.facebook.com/reel/456",
            "play_url": "https://cdn.example/dead.mp4",
            "direct_has_audio": True,
            "prefer_page_download": False,
        },
    )
    monkeypatch.setattr(downloader, "_generic_download_video", fake_generic)

    path = asyncio.run(
        FacebookEngine().download_video("https://www.facebook.com/watch/?v=123", "best", "best")
    )
    assert path is None
    assert page_calls == [
        "https://www.facebook.com/reel/456",
        "https://www.facebook.com/watch/?v=123",
        "https://m.facebook.com/watch/?v=123",
        "https://mbasic.facebook.com/watch/?v=123",
    ]


def test_twitter_engine_propagates_friendly_empty_error(monkeypatch):
    """Twitter's single direct candidate is skipped, then generic yt-dlp's
    friendly zero-data error surfaces to the user unchanged."""
    monkeypatch.setattr(downloader, "_download_direct_sync", lambda *a, **kw: None)
    monkeypatch.setattr(downloader, "_prepare_video_for_delivery", _identity)
    monkeypatch.setattr(
        "services.twitter_scraper.scrape_twitter",
        lambda url: {"play_url": "https://cdn.example/dead.mp4", "source": "test"},
    )

    async def fake_generic(*args, **kwargs):
        raise RuntimeError(ZERO_DATA)

    monkeypatch.setattr(downloader, "_generic_download_video", fake_generic)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            TwitterEngine().download_video("https://x.com/user/status/123", "best", "best")
        )
    assert "zero data" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Control-flow guards: cancellation and size limits must NEVER be swallowed.
# --------------------------------------------------------------------------- #

def test_pinterest_engine_propagates_cancel(monkeypatch):
    def raise_cancel(*args, **kwargs):
        raise DownloadCancelledError()

    monkeypatch.setattr(downloader, "_download_direct_sync", raise_cancel)
    monkeypatch.setattr(
        "services.pinterest_scraper.scrape_pinterest",
        lambda url: {
            "source": "pin-resource",
            "provider_score": 10,
            "play_url": "https://cdn.example/dead.mp4",
            "download_candidates": [
                {"url": "https://cdn.example/dead.mp4", "kind": "mp4", "score": 10},
            ],
        },
    )

    with pytest.raises(DownloadCancelledError):
        asyncio.run(
            PinterestEngine().download_video("https://www.pinterest.com/pin/123/", "best", "best")
        )


def test_instagram_engine_propagates_cancel(monkeypatch):
    def raise_cancel(*args, **kwargs):
        raise DownloadCancelledError()

    monkeypatch.setattr(downloader, "_download_direct_sync", raise_cancel)
    monkeypatch.setattr(
        "services.instagram_scraper.scrape_instagram",
        lambda url: {
            "webpage_url": url,
            "source": "test",
            "play_url": "https://cdn.example/dead.mp4",
        },
    )

    with pytest.raises(DownloadCancelledError):
        asyncio.run(
            InstagramEngine().download_video("https://www.instagram.com/reel/ABC/", "best", "best")
        )


def test_reddit_engine_propagates_cancel(monkeypatch):
    def raise_cancel(*args, **kwargs):
        raise DownloadCancelledError()

    monkeypatch.setattr(downloader, "_download_direct_sync", raise_cancel)
    monkeypatch.setattr(
        "services.reddit_scraper.scrape_reddit",
        lambda url: {
            "source": "test",
            "play_url": "https://v.redd.it/dead_fallback.mp4",
            "formats": [{"url": "https://v.redd.it/dead_fallback.mp4"}],
        },
    )

    with pytest.raises(DownloadCancelledError):
        asyncio.run(
            RedditEngine().download_video(
                "https://www.reddit.com/r/videos/comments/abc/x/", "best", "best"
            )
        )


def test_embed_engine_propagates_cancel(monkeypatch):
    def raise_cancel(*args, **kwargs):
        raise DownloadCancelledError()

    monkeypatch.setattr(downloader, "_download_direct_sync", raise_cancel)

    with pytest.raises(DownloadCancelledError):
        asyncio.run(_FakeEmbed().download_video("https://example.com/video", "best", "best"))


def test_embed_engine_propagates_file_too_large(monkeypatch):
    def raise_large(*args, **kwargs):
        raise downloader.FileTooLargeError(123, 100)

    monkeypatch.setattr(downloader, "_download_direct_sync", raise_large)

    with pytest.raises(downloader.FileTooLargeError):
        asyncio.run(_FakeEmbed().download_video("https://example.com/video", "best", "best"))


def test_outer_download_video_surfaces_friendly_error_when_engine_swallows_all(monkeypatch):
    """Integration: even when a dedicated engine internally swallows every
    candidate/page error and returns None, the public downloader API still
    raises the clear zero-data message instead of 'Download failed'."""
    async def fake_engine_download(*args, **kwargs):
        return None

    async def fake_generic(*args, **kwargs):
        raise RuntimeError(ZERO_DATA)

    monkeypatch.setattr(FacebookEngine, "download_video", fake_engine_download)
    monkeypatch.setattr(downloader, "_select_engine", lambda url: FacebookEngine())
    monkeypatch.setattr(downloader, "_generic_download_video", fake_generic)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            downloader.download_video("https://www.facebook.com/watch/?v=1", "best", "best")
        )
    assert "zero data" in str(exc_info.value)


if __name__ == "__main__":
    print("engine cdn-rejection full-coverage tests import OK")
