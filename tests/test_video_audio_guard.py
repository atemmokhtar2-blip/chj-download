import asyncio
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import (
    _generic_download_video,
    _prepare_video_for_delivery,
    _remux_mp4_faststart,
    _video_format_candidates,
    _require_audio_or_none,
)


def test_video_format_candidates_prefer_merged_audio() -> None:
    candidates = _video_format_candidates("best")
    assert candidates
    assert "+bestaudio" in candidates[0]
    assert "acodec!=none" in "/".join(candidates)


def test_requested_format_keeps_audio_merge_first() -> None:
    candidates = _video_format_candidates("137")
    assert candidates[0].startswith("137+bestaudio")
    assert any("bestvideo" in item and "+bestaudio" in item for item in candidates)


def test_require_audio_removes_verified_silent_file(tmp_path: Path) -> None:
    path = tmp_path / "silent.mp4"
    path.write_bytes(b"not-real-video")
    with patch("services.downloader._has_audio_stream", return_value=False):
        assert _require_audio_or_none(str(path), source="test") is None
    assert not path.exists()


def test_require_audio_keeps_unverifiable_file(tmp_path: Path) -> None:
    path = tmp_path / "unknown.mp4"
    path.write_bytes(b"not-real-video")
    with patch("services.downloader._has_audio_stream", return_value=None):
        assert _require_audio_or_none(str(path), source="test") == str(path)
    assert path.exists()


def test_prepare_video_rejects_silent_before_delivery(tmp_path: Path) -> None:
    path = tmp_path / "silent.mp4"
    path.write_bytes(b"video-only")
    with patch("services.downloader._has_audio_stream", return_value=False):
        assert _prepare_video_for_delivery(str(path), source="test") is None
    assert not path.exists()


def test_faststart_keeps_original_when_ffmpeg_missing(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video+audio")
    with patch("services.downloader.shutil.which", return_value=None):
        assert _remux_mp4_faststart(str(path)) == str(path)
    assert path.exists()


def test_direct_play_url_falls_back_to_page_url_for_audio(tmp_path: Path) -> None:
    silent = tmp_path / "silent.mp4"
    silent.write_bytes(b"video-only")
    merged = tmp_path / "merged.mp4"
    merged.write_bytes(b"video+audio")
    calls: list[str] = []

    def fake_download_sync(target: str, _opts: dict) -> str:
        calls.append(target)
        return str(silent if "cdn.example" in target else merged)

    def fake_audio_probe(path: str):
        return path == str(merged)

    with (
        patch("services.downloader._download_sync", side_effect=fake_download_sync),
        patch("services.downloader._has_audio_stream", side_effect=fake_audio_probe),
    ):
        result = asyncio.run(_generic_download_video(
            "https://www.facebook.com/watch/?v=123",
            play_url="https://cdn.example/video-only.mp4",
        ))

    assert result == str(merged)
    assert calls[0] == "https://cdn.example/video-only.mp4"
    assert "https://www.facebook.com/watch/?v=123" in calls
    assert not silent.exists()


if __name__ == "__main__":
    test_video_format_candidates_prefer_merged_audio()
    test_requested_format_keeps_audio_merge_first()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_require_audio_removes_verified_silent_file(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_require_audio_keeps_unverifiable_file(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_prepare_video_rejects_silent_before_delivery(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_faststart_keeps_original_when_ffmpeg_missing(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_direct_play_url_falls_back_to_page_url_for_audio(Path(d))
    print("video audio guard tests passed")
