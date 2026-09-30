from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.download import _safe_upload_name, _cleanup_file


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


if __name__ == '__main__':
    test_safe_upload_name_removes_path_separators()
    test_cleanup_file_is_idempotent()
    print('download helper tests passed')
