from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import init_db
from database.cache import (
    get_cached,
    set_cache,
    invalidate_cache,
    normalize_url,
    set_cache_with_meta,
    resolve_cached_delivery,
    set_cache_album,
    get_cached_album,
    url_hash,
)


def test_url_normalization_strips_tracking() -> None:
    a = "https://www.youtube.com/watch?v=abc123&utm_source=x&feature=share"
    b = "https://youtube.com/watch?v=abc123"
    assert normalize_url(a) == normalize_url(b)
    assert url_hash(a) == url_hash(b)


def test_cache_roundtrip_and_invalidate() -> None:
    init_db()
    url = "https://example.com/cache-contract/video?id=roundtrip"
    invalidate_cache(url)
    set_cache(url, "720p", "video", "file_test_roundtrip", title="Contract", platform="Test")
    assert get_cached(url, "720p", "video") == "file_test_roundtrip"
    assert invalidate_cache(url, "720p", "video") >= 1
    assert get_cached(url, "720p", "video") is None


def test_fingerprint_promotes_across_urls() -> None:
    init_db()
    url_a = "https://youtube.com/watch?v=contract_fp&utm_source=share"
    url_b = "https://m.youtube.com/watch?v=contract_fp&fbclid=remove"
    invalidate_cache(url_a)
    invalidate_cache(url_b)
    set_cache_with_meta(
        url_a,
        "best",
        "video",
        "file_test_fingerprint",
        title="Fingerprint Contract",
        platform="YouTube",
        media_id="contract_fp",
        vault_chat_id=-100123,
        vault_message_id=456,
    )
    hit = resolve_cached_delivery(url_b, "best", "video", platform="YouTube", media_id="contract_fp")
    assert hit and hit["file_id"] == "file_test_fingerprint"
    assert hit["vault_chat_id"] == -100123
    assert hit["vault_message_id"] == 456
    invalidate_cache(url_a)
    invalidate_cache(url_b)


def test_album_cache_contract() -> None:
    init_db()
    url = "https://instagram.com/p/cache_contract_album"
    invalidate_cache(url, "album", "album")
    set_cache_album(
        url,
        [
            {"file_id": "photo_contract_1", "type": "image"},
            {"file_id": "video_contract_2", "type": "video"},
        ],
        title="Album Contract",
        platform="Instagram",
    )
    album = get_cached_album(url)
    assert album == [
        {"file_id": "photo_contract_1", "type": "image"},
        {"file_id": "video_contract_2", "type": "video"},
    ]
    invalidate_cache(url, "album", "album")


if __name__ == "__main__":
    test_url_normalization_strips_tracking()
    test_cache_roundtrip_and_invalidate()
    test_fingerprint_promotes_across_urls()
    test_album_cache_contract()
    print("cache contract tests passed")
