"""Regression: a Pinterest video pin must never be served as its poster image."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.pinterest_scraper as ps


def test_video_pin_wins_over_poster_image(monkeypatch):
    """Even when an image candidate scores higher, a playable video wins."""
    monkeypatch.setattr(
        ps, "expand_pin_url",
        lambda u, session=None: "https://www.pinterest.com/pin/1234567890/",
    )
    monkeypatch.setattr(ps, "extract_pin_id", lambda u: "1234567890")
    monkeypatch.setattr(
        ps, "_from_pinterest_downloader", lambda u: None,
    )
    monkeypatch.setattr(
        ps, "_pin_from_api",
        lambda pin_id, session: {
            "id": pin_id,
            "title": "Poster image",
            "images": {"originals": {"url": "https://i.pinimg.com/originals/aa/bb/cc/poster.jpg"}},
        },
    )
    monkeypatch.setattr(ps, "_from_gallery_dl", lambda u: None)
    monkeypatch.setattr(
        ps, "_from_html",
        lambda u, session: {
            "title": "Video pin",
            "uploader": "Pinterest User",
            "duration": "Unknown",
            "duration_secs": 0,
            "thumbnail": "https://i.pinimg.com/originals/aa/bb/cc/poster.jpg",
            "platform": "Pinterest",
            "media_type": "video",
            "qualities": [{"label": "Best", "format_id": "best", "has_audio": True}],
            "audio_formats": [],
            "image_url": None,
            "album_items": [],
            "url": u,
            "webpage_url": u,
            "ext": "mp4",
            "media_id": "1234567890",
            "play_url": "https://v1.pinimg.com/videos/mc/hls/stream.m3u8",
            "source": "html_json",
        },
    )
    # Avoid real network probes inside _score_result.
    monkeypatch.setattr(
        ps, "_probe_media_url",
        lambda u, session=None, timeout=10: {
            "ok": True,
            "status": 200,
            "content_type": "application/vnd.apple.mpegurl" if "m3u8" in (u or "") else "image/jpeg",
            "content_length": 0,
        },
    )

    result = ps.scrape_pinterest("https://pin.it/abc")
    assert result is not None
    assert result["media_type"] == "video"
    assert result["play_url"] and ".m3u8" in result["play_url"]
    assert not result.get("image_url")


def test_raw_videos_shape_is_recognised(monkeypatch):
    """pinterest-downloader pin with raw videos.video_list must map to video."""
    pin = {
        "id": "1234567890",
        "title": "HLS only pin",
        "media_type": "video",
        "author": {"username": "u"},
        "images": {"originals": {"url": "https://i.pinimg.com/originals/aa/bb/cc/poster.jpg"}},
        "videos": {
            "video_list": {
                "V_HLSV4": {"url": "https://v1.pinimg.com/videos/mc/hls/v4.m3u8", "height": 720},
                "V_720P": {"url": "https://v1.pinimg.com/videos/mc/720p.mp4", "height": 720},
            }
        },
    }

    class FakeClient:
        def get_pin(self, url):
            return {"ok": True, "pin": pin}

    monkeypatch.setattr(ps, "extract_pin_id", lambda u: "1234567890")
    # Patch the import used inside the function (it re-imports at call time).
    import types
    fake_module = types.ModuleType("pinterest_downloader")
    fake_module.Pinterest = FakeClient
    monkeypatch.setitem(sys.modules, "pinterest_downloader", fake_module)

    result = ps._from_pinterest_downloader("https://www.pinterest.com/pin/1234567890/")
    assert result is not None
    assert result["media_type"] == "video"
    assert result["play_url"].endswith(".mp4")
    assert result.get("image_url") is None


def test_video_candidates_mp4_first_and_deduped():
    """Progressive mp4s come before HLS, all unique, all preserved."""
    videos = {
        "video_list": {
            "V_HLSV4": {"url": "https://v1.pinimg.com/videos/mc/hls/v4.m3u8", "height": 720},
            "V_720P": {"url": "https://v1.pinimg.com/videos/mc/720p.mp4", "height": 720},
            "V_480P": {"url": "https://v1.pinimg.com/videos/mc/480p.mp4", "height": 480},
        }
    }
    cands = ps._video_candidates(videos)
    urls = [c["url"] for c in cands]
    assert urls[0].endswith("720p.mp4"), urls
    assert urls[1].endswith("480p.mp4"), urls
    assert urls[2].endswith("v4.m3u8"), urls
    assert len(urls) == 3 and len(set(urls)) == 3


def test_extract_pin_id_supports_numeric_and_alphanumeric():
    """Both classic numeric and modern alphanumeric pin URLs resolve their id."""
    assert ps.extract_pin_id(
        "https://www.pinterest.com/pin/155303887461186624/"
    ) == "155303887461186624"
    assert ps.extract_pin_id(
        "https://www.pinterest.com/pin/some-title--155303887461186624/"
    ) == "155303887461186624"
    assert ps.extract_pin_id(
        "https://www.pinterest.com/pin/e1_AfqPShznDOOeWDz0mnIdqC4NgqcSYvi5ZIuyisa5aZU99xtgJ2Yi/"
    ) == "e1_AfqPShznDOOeWDz0mnIdqC4NgqcSYvi5ZIuyisa5aZU99xtgJ2Yi"


def test_html_harvest_extracts_embedded_video_urls():
    """Pin pages embed CDN renditions in the HTML; the HTML provider must
    return a video (mp4 preferred) instead of bailing on a missing image."""

    class FakeResp:
        url = "https://www.pinterest.com/pin/664281013778109217/"
        text = (
            "<html><head><title>Origami | Pinterest</title></head><body>"
            '<script>const a="https://v1.pinimg.com/videos/mc/hls/12/83/f0/'
            '1283f06c1c8fa040011cd7231f33f069_360w.m3u8";</script>'
            '<script>const b="https://v1.pinimg.com/videos/mc/720p/12/83/f0/'
            '1283f06c1c8fa040011cd7231f33f069.mp4";</script>'
            "</body></html>"
        )

    class FakeSession:
        def get(self, url, timeout=20, allow_redirects=True):
            return FakeResp()

    res = ps._from_html(
        "https://www.pinterest.com/pin/664281013778109217/", FakeSession()
    )
    assert res is not None
    assert res["media_type"] == "video"
    assert res["play_url"].endswith(".mp4")
    assert res["title"] == "Origami"
    cand_urls = [c["url"] for c in res.get("download_candidates") or []]
    assert any(".m3u8" in u for u in cand_urls)
    assert not res.get("image_url")  # video, never downgraded to its poster


def test_pin_data_exposes_download_candidates():
    """The PinResource result carries every rendition so the engine can walk
    past a stale/silent mp4 instead of failing into the yt-dlp format error."""
    pin = {
        "id": "123",
        "title": "t",
        "images": {"originals": {"url": "https://i.pinimg.com/originals/a/b/c.jpg"}},
        "videos": {"video_list": {
            "V_HLSV4": {"url": "https://v1.pinimg.com/videos/mc/hls/v.m3u8"},
            "V_720P": {"url": "https://v1.pinimg.com/videos/mc/v.mp4", "height": 720},
        }},
    }
    res = ps._result_from_pin_data(pin, "https://www.pinterest.com/pin/123/")
    assert res["media_type"] == "video"
    assert res["play_url"].endswith(".mp4")
    cand_urls = [c["url"] for c in res.get("download_candidates") or []]
    assert res["play_url"] in cand_urls
    assert any(".m3u8" in u for u in cand_urls), cand_urls
