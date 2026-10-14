"""Audit tests: every settings button must have a REAL, precise effect.

Focus: the per-user size limit now applies to every media type (not just video).
"""
from __future__ import annotations

import pytest

from services.downloader import FileTooLargeError


def test_user_size_limit_applies_to_audio_and_image(tmp_path):
    from handlers.download import _enforce_user_size

    small = tmp_path / "small.mp3"
    small.write_bytes(b"x" * (1024 * 1024))          # 1 MB
    big = tmp_path / "big.mp3"
    big.write_bytes(b"x" * (2 * 1024 * 1024))        # 2 MB

    # 1 MB file under a 2 MB cap is fine
    _enforce_user_size(str(small), {"max_file_size_mb": 2})

    # 2 MB file over a 1 MB cap must raise (audio/image/album paths share this)
    with pytest.raises(FileTooLargeError):
        _enforce_user_size(str(big), {"max_file_size_mb": 1})

    # cap of 0 == "بدون حد إضافي" -> never raises
    _enforce_user_size(str(big), {"max_file_size_mb": 0})

    # missing settings / missing path -> never raises
    _enforce_user_size(str(big), {})
    _enforce_user_size(None, {"max_file_size_mb": 1})
    _enforce_user_size(str(tmp_path / "absent.mp3"), {"max_file_size_mb": 1})
