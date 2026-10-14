from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.download import _safe_upload_name, _cleanup_file
from services import downloader


def test_safe_upload_name_removes_path_separators() -> None:
    name = _safe_upload_name('../bad/name:*? test.mp4', limit=80)
    assert '/' not in name
    assert '\\' not in name
    assert ':' not in name
    assert name.endswith('test.mp4')


def test_cleanup_file_is_idempotent() -> None:
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    assert Path(path).exists()
    _cleanup_file(path)
    assert not Path(path).exists()
    _cleanup_file(path)


def test_require_audio_or_none_rejects_unknown_audio(monkeypatch, tmp_path) -> None:
    """Fail-closed: a file whose audio can't be verified is dropped, never delivered silent."""
    f = tmp_path / "video.mp4"
    f.write_bytes(b"\x00" * 4096)
    monkeypatch.setattr("services.downloader._has_audio_stream", lambda _path: None)
    assert downloader._require_audio_or_none(str(f)) is None
    assert not f.exists()


def test_require_audio_or_none_keeps_verified_audio(monkeypatch, tmp_path) -> None:
    f = tmp_path / "video.mp4"
    f.write_bytes(b"\x00" * 4096)
    monkeypatch.setattr("services.downloader._has_audio_stream", lambda _path: True)
    assert downloader._require_audio_or_none(str(f)) == str(f)
    assert f.exists()


if __name__ == '__main__':
    test_safe_upload_name_removes_path_separators()
    test_cleanup_file_is_idempotent()
    print('download helper tests passed')
