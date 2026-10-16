"""Surface guard: the CDN-rejection message must actually reach the user.

The downloader raises ``DownloadImpossibleError(empty_file=True)`` when the
host CDN rejects every segment. Two wiring mistakes would silently break that
promise at runtime:

  * ``handlers.download`` referencing ``DownloadImpossibleError`` without
    importing it (NameError inside the ``except Exception`` branch),
  * the Arabic ``cdn_rejected`` locale key being missing (the bot is
    Arabic-only, so a missing key would fall back to English).

This file locks both in place.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers import download as hd
from locales import t
from services.downloader import DownloadImpossibleError


def test_handler_resolves_download_impossible_error():
    """The except branch uses the bare name — it must resolve as a module global."""
    assert hd.DownloadImpossibleError is DownloadImpossibleError


def test_arabic_cdn_rejected_message_exists_and_is_not_english_fallback():
    msg = t("ar", "cdn_rejected")

    # Real Arabic copy, not the English fallback and not a Title-Cased key.
    assert "خادم المصدر" in msg
    assert "CDN" in msg
    assert "Technical reason" not in msg
    assert msg.strip() != "Cdn Rejected"


def test_empty_cdn_error_carries_flag():
    exc = DownloadImpossibleError("zero data", empty_file=True)
    assert exc.empty_file is True
    non_empty = DownloadImpossibleError("no stream")
    assert non_empty.empty_file is False


if __name__ == "__main__":
    test_handler_resolves_download_impossible_error()
    test_arabic_cdn_rejected_message_exists_and_is_not_english_fallback()
    test_empty_cdn_error_carries_flag()
    print("cdn-rejection surface tests passed")
