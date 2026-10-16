"""Regression tests for the 🛑 cancel button's responsiveness.

The downloader now propagates cancellation into every long-running phase:
yt-dlp download chunks AND yt-dlp postprocessors (FFmpeg merge / audio extract)
raise DownloadCancelledError from their hooks, and local ffmpeg subprocesses
(FastStart remux / MP3 extraction) are polled every 50ms and killed on cancel.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import (
    _base_opts,
    _run_process_cancellable,
    DownloadCancelledError,
)


class FakeProgress:
    def __init__(self, raise_on_check: bool = True):
        self.raise_on_check = raise_on_check

    def cancel_check(self):
        if self.raise_on_check:
            raise DownloadCancelledError("user pressed stop")


def test_postprocessor_hook_raises_on_cancel() -> None:
    """The yt-dlp postprocessor hook (merge / MP3 extraction) aborts instantly."""
    progress = FakeProgress(raise_on_check=True)

    async def cb(_payload):
        pass

    loop = asyncio.new_event_loop()
    try:
        opts = _base_opts(progress, loop)
    finally:
        loop.close()

    hooks = opts.get("postprocessor_hooks")
    assert hooks, "postprocessor_hooks must be registered when progress is provided"

    did_raise = False
    try:
        hooks[0]({"status": "started", "postprocessor": "Merger"})
    except DownloadCancelledError:
        did_raise = True
    assert did_raise, "postprocessor hook must raise DownloadCancelledError on cancel"


def test_run_process_killed_immediately_on_cancel() -> None:
    """Local ffmpeg runs are killed as soon as cancel_check raises."""
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # pretend the process is still running

    def cancel_now():
        raise DownloadCancelledError("user pressed stop")

    with patch("services.downloader.subprocess.Popen", return_value=fake_proc) as popen:
        try:
            _run_process_cancellable(["ffmpeg", "-y", "in.mp4", "out.mp4"], cancel_now)
            raised = False
        except DownloadCancelledError:
            raised = True

    assert raised, "cancellation must propagate out of _run_process_cancellable"
    popen.assert_called_once()
    fake_proc.kill.assert_called_once()  # process killed, not left running


if __name__ == "__main__":
    test_postprocessor_hook_raises_on_cancel()
    test_run_process_killed_immediately_on_cancel()
    print("cancel responsiveness tests passed")
