"""Regression: yt-dlp's zero-byte fragment error must surface as a clear,
actionable message instead of the raw 'ERROR: The downloaded file is empty'.

The downloader detects the CDN-rejection condition three ways:
  1. yt-dlp's raw "The downloaded file is empty" text,
  2. our own ``EmptyFileError`` normalisation (raised at the ``_download_sync_impl``
     boundary, including the silent zero-byte-file case yt-dlp doesn't raise on),
  3. and confirms the video/audio generic paths stop re-trying a dead target.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import services.downloader as dl


def test_empty_file_error_is_detected():
    assert dl._is_empty_file_error(Exception("ERROR: The downloaded file is empty"))
    assert dl._is_empty_file_error(Exception("ERROR: the DOWNLOADED FILE IS EMPTY!"))
    assert dl._is_empty_file_error(dl.EmptyFileError("zero bytes"))
    assert not dl._is_empty_file_error(Exception("ERROR: Requested format is not available"))


def test_generic_video_empty_file_raises_friendly_error(monkeypatch):
    """When every fragment comes back empty, the final error must be a clear
    RuntimeError (not yt-dlp's cryptic text), so the bot can explain the cause."""

    def fake_sync(url, opts):
        raise Exception("ERROR: The downloaded file is empty")

    monkeypatch.setattr(dl, "_download_sync", fake_sync)
    monkeypatch.setattr(dl, "_download_sync_no_limit", fake_sync)

    async def run():
        with pytest.raises(RuntimeError) as exc_info:
            await dl._generic_download_video(
                "https://pin.it/abc", "best", "best", None, None
            )
        assert "zero data" in str(exc_info.value)
        assert "file is empty" not in str(exc_info.value)

    asyncio.run(run())


def test_generic_video_empty_file_does_not_retry_other_formats(monkeypatch):
    """A rejected target must not burn the remaining format selectors.

    Calling one dead URL should hit ``_download_sync`` exactly once, not once
    per format candidate (``best`` expands into three selector chains).
    """
    calls: list[str] = []

    def fake_sync(url, opts):
        calls.append(url)
        raise dl.EmptyFileError("zero bytes")

    monkeypatch.setattr(dl, "_download_sync", fake_sync)
    monkeypatch.setattr(dl, "_download_sync_no_limit", fake_sync)

    async def run():
        with pytest.raises(RuntimeError):
            await dl._generic_download_video(
                "https://pin.it/abc", "best", "best", None, None
            )
        assert len(calls) == 1, calls

    asyncio.run(run())


def test_generic_video_skips_dead_play_url_and_succeeds_on_page_url(monkeypatch):
    """An empty direct play_url must still let the canonical page URL be tried."""

    calls: list[str] = []

    def fake_sync(url, opts):
        calls.append(url)
        if "play.example" in url:
            raise Exception("ERROR: The downloaded file is empty")
        return "/tmp/ok.mp4"

    monkeypatch.setattr(dl, "_download_sync", fake_sync)
    monkeypatch.setattr(dl, "_download_sync_no_limit", fake_sync)
    monkeypatch.setattr(dl, "_prepare_video_for_delivery", lambda path, **kw: path)

    async def run():
        path = await dl._generic_download_video(
            "https://pin.it/abc", "best", "best", None,
            "https://play.example/v.mp4",
        )
        assert path == "/tmp/ok.mp4"
        assert calls == ["https://play.example/v.mp4", "https://pin.it/abc"]

    asyncio.run(run())


def test_download_sync_impl_rejects_zero_byte_file(tmp_path, monkeypatch):
    """yt-dlp can silently write a 0-byte file; that must be rejected as empty."""

    out = tmp_path / "t-x.mp4"
    out.write_bytes(b"")

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            return {"id": "x", "title": "t"}

        def prepare_filename(self, info):
            return str(out)

    monkeypatch.setattr(dl, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", lambda opts: FakeYDL(opts))

    with pytest.raises(dl.EmptyFileError):
        dl._download_sync_impl("https://example.com/video", {}, check_size=False)
    assert not out.exists()


def test_download_sync_impl_normalizes_ytdlp_empty_error(tmp_path, monkeypatch):
    """yt-dlp's raw empty-file text is normalised into EmptyFileError at the
    lowest layer, so every caller (video AND audio) gets one signal type."""

    class RaisingYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            raise Exception("ERROR: The downloaded file is empty")

    monkeypatch.setattr(dl, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", lambda opts: RaisingYDL(opts))

    with pytest.raises(dl.EmptyFileError):
        dl._download_sync_impl("https://example.com/video", {}, check_size=False)


def test_generic_audio_empty_file_raises_friendly_error(monkeypatch):
    """Audio downloads get the same clear CDN-rejection message as video."""

    calls: list[str] = []

    def fake_sync(url, opts):
        calls.append(url)
        raise Exception("ERROR: The downloaded file is empty")

    monkeypatch.setattr(dl, "_download_sync", fake_sync)

    async def run():
        with pytest.raises(RuntimeError) as exc_info:
            await dl._generic_download_audio("https://soundcloud.com/x/y", None)
        assert "zero data" in str(exc_info.value)
        assert len(calls) == 1, calls

    asyncio.run(run())
