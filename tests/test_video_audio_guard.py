from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import _video_format_candidates, _require_audio_or_none


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


if __name__ == "__main__":
    test_video_format_candidates_prefer_merged_audio()
    test_requested_format_keeps_audio_merge_first()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_require_audio_removes_verified_silent_file(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_require_audio_keeps_unverifiable_file(Path(d))
    print("video audio guard tests passed")
