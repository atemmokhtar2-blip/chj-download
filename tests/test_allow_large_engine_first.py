"""Regression: `allow_large` (auto-split) must NOT bypass platform engines.

Before this test, ``download_video`` short-circuited straight to the generic
yt-dlp path whenever ``allow_large=True`` (the default). That is exactly why
Pinterest video pins stopped downloading while Pinterest images — a different
code path — kept working: the dedicated Pinterest engine walks the CDN
renditions and merges the HLS playlist with audio, but it was never reached.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import services.downloader as dl


def test_allow_large_still_runs_platform_engine(monkeypatch):
    """A specialized engine must get first shot even when auto-split is on."""
    engine_calls: list[str] = []
    generic_calls: list[str] = []

    class PinterestLikeEngine:
        name = "PinterestLikeEngine"

        async def download_video(self, url, format_id, quality_label, progress, play_url):
            engine_calls.append(url)
            return "/tmp/verified.mp4"

    def fake_prepare(path, *, source="video", allow_large=False, cancel_check=None):
        # allow_large must be forwarded so an oversized engine result skips the
        # size-enforcing faststart remux and can be split later.
        assert allow_large is True
        return path

    async def fake_generic(*args, **kwargs):
        generic_calls.append("generic")
        raise AssertionError("generic must not run when the engine already produced a file")

    monkeypatch.setattr(dl, "_select_engine", lambda url: PinterestLikeEngine())
    monkeypatch.setattr(dl, "_prepare_video_for_delivery", fake_prepare)
    monkeypatch.setattr(dl, "_generic_download_video", fake_generic)

    path = asyncio.run(
        dl.download_video(
            "https://www.pinterest.com/pin/1234567890/",
            "best",
            "best",
            None,
            "https://v1.pinimg.com/videos/mc/hls/x.m3u8",
            allow_large=True,
        )
    )

    assert path == "/tmp/verified.mp4"
    assert engine_calls == ["https://www.pinterest.com/pin/1234567890/"]
    assert generic_calls == []


def test_allow_large_file_too_large_falls_back_to_generic_no_limit(monkeypatch):
    """If the engine's direct downloader hits the byte cap, auto-split retries
    through the no-limit generic path instead of failing the download."""
    generic_kwargs: list[dict] = []

    class SizeCapEngine:
        name = "SizeCapEngine"

        async def download_video(self, url, format_id, quality_label, progress, play_url):
            raise dl.FileTooLargeError(80 * 1024 * 1024, dl.MAX_FILE_SIZE_BYTES)

    async def fake_prepare(path, *, source="video", allow_large=False, cancel_check=None):
        return path

    async def fake_generic(*args, **kwargs):
        generic_kwargs.append(kwargs)
        return "/tmp/large-generic.mp4"

    monkeypatch.setattr(dl, "_select_engine", lambda url: SizeCapEngine())
    monkeypatch.setattr(dl, "_prepare_video_for_delivery", fake_prepare)
    monkeypatch.setattr(dl, "_generic_download_video", fake_generic)

    path = asyncio.run(
        dl.download_video(
            "https://www.pinterest.com/pin/1234567890/",
            "best",
            "best",
            None,
            None,
            allow_large=True,
        )
    )

    assert path == "/tmp/large-generic.mp4"
    assert generic_kwargs and generic_kwargs[0].get("allow_large") is True


if __name__ == "__main__":
    print("allow-large engine-first tests import OK")
