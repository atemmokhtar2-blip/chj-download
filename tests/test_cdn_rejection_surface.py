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


def test_friendly_error_never_leaks_raw_technical_text():
    """An unmapped error_reason must collapse to the generic message — the
    raw yt-dlp/technical text is for the logs, never for the user's chat."""
    msg = hd._friendly_error(
        "ar", {"error_reason": "ERROR: The downloaded file is empty"}
    )

    assert "ERROR" not in msg
    assert "downloaded file is empty" not in msg
    assert "empty" not in msg.lower()
    # It's the plain, friendly analysis-failed copy, not a technical reason.
    assert t("ar", "analysis_failed") == msg


def test_friendly_error_known_reasons_map_to_friendly_messages():
    assert hd._friendly_error("ar", {"user_message_key": "spotify_metadata_only"}) == t("ar", "spotify_metadata_only")
    assert hd._friendly_error("ar", {"error_reason": "region_or_availability_blocked"}) == t("ar", "region_blocked")
    assert hd._friendly_error("ar", {"error_reason": "removed_or_not_found"}) == t("ar", "removed_or_not_found")


if __name__ == "__main__":
    test_handler_resolves_download_impossible_error()
    test_arabic_cdn_rejected_message_exists_and_is_not_english_fallback()
    test_empty_cdn_error_carries_flag()
    test_friendly_error_never_leaks_raw_technical_text()
    test_friendly_error_known_reasons_map_to_friendly_messages()
    print("cdn-rejection surface tests passed")
