from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import FileTooLargeError, _download_direct_sync


class FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, chunk_size):
        yield from self.chunks


def test_direct_download_streams_to_unique_file_and_retries(tmp_path: Path) -> None:
    responses = iter([
        FakeResponse(status_code=503),
        FakeResponse(
            headers={"Content-Type": "video/mp4", "Content-Length": "3072"},
            chunks=[b"x" * 3072],
        ),
    ])
    with (
        patch("services.downloader.TEMP_DIR", str(tmp_path)),
        patch("requests.get", side_effect=lambda *_args, **_kwargs: next(responses)),
        patch("services.downloader.time.sleep"),
    ):
        path = _download_direct_sync(
            "https://cdn.example/video.mp4",
            {"Referer": "https://example.com/"},
            retries=1,
        )

    assert path is not None
    assert Path(path).read_bytes() == b"x" * 3072
    Path(path).unlink()


def test_direct_download_rejects_oversize_content_length(tmp_path: Path) -> None:
    with (
        patch("services.downloader.TEMP_DIR", str(tmp_path)),
        patch("services.downloader.MAX_FILE_SIZE_BYTES", 4096),
        patch(
            "requests.get",
            return_value=FakeResponse(
                headers={"Content-Type": "video/mp4", "Content-Length": "4097"},
            ),
        ),
    ):
        try:
            _download_direct_sync("https://cdn.example/large.mp4")
        except FileTooLargeError:
            pass
        else:
            raise AssertionError("oversized direct media should be rejected")

    assert list(tmp_path.iterdir()) == []


def test_direct_download_stops_when_stream_exceeds_limit(tmp_path: Path) -> None:
    with (
        patch("services.downloader.TEMP_DIR", str(tmp_path)),
        patch("services.downloader.MAX_FILE_SIZE_BYTES", 4096),
        patch(
            "requests.get",
            return_value=FakeResponse(
                headers={"Content-Type": "video/mp4"},
                chunks=[b"x" * 3072, b"y" * 2048],
            ),
        ),
    ):
        try:
            _download_direct_sync("https://cdn.example/chunked.mp4")
        except FileTooLargeError:
            pass
        else:
            raise AssertionError("oversized streamed media should be rejected")

    assert list(tmp_path.iterdir()) == []


def test_direct_download_discards_non_media_responses(tmp_path: Path) -> None:
    responses = [
        FakeResponse(headers={"Content-Type": "application/json"}, chunks=[b'{"error":true}']),
        FakeResponse(
            headers={"Content-Type": "video/mp4"},
            chunks=[b"<html>blocked</html>"],
        ),
    ]
    with (
        patch("services.downloader.TEMP_DIR", str(tmp_path)),
        patch("requests.get", side_effect=responses),
    ):
        assert _download_direct_sync("https://cdn.example/not-media") is None
        assert _download_direct_sync("https://cdn.example/blocked") is None

    assert list(tmp_path.iterdir()) == []