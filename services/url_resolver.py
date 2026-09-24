"""URL normalization and expansion helpers for resilient media downloads.

Many failures come from short links, mobile hosts, tracking query params, or
country/language redirects. This module keeps the rest of the downloader working
with canonical, clean URLs whenever possible.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from utils.logger import download_logger

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "igshid", "si", "feature", "share_app_id", "share_iid",
    "share_link_id", "timestamp", "sender_device", "sender_web_id",
}

SHORT_HOSTS = {
    "youtu.be", "vt.tiktok.com", "vm.tiktok.com", "t.co", "fb.watch",
    "pin.it", "redd.it", "dai.ly", "on.soundcloud.com",
}

MOBILE_HOST_REWRITES = {
    "m.youtube.com": "www.youtube.com",
    "music.youtube.com": "www.youtube.com",
    "m.facebook.com": "www.facebook.com",
    "mobile.facebook.com": "www.facebook.com",
    "m.tiktok.com": "www.tiktok.com",
    "l.instagram.com": "www.instagram.com",
}


def clean_url(url: str) -> str:
    """Remove tracking params and normalize common mobile hosts."""
    raw = (url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return raw

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        normalized_netloc = netloc
    else:
        normalized_netloc = MOBILE_HOST_REWRITES.get(netloc, netloc)

    # Instagram link shim: /?u=<real-url>
    if netloc == "l.instagram.com":
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        target = params.get("u")
        if target and target.startswith(("http://", "https://")):
            return clean_url(target)

    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() not in TRACKING_PARAMS:
            kept.append((key, value))

    # Drop fragments; media pages do not need them and they can break cache keys.
    return urlunparse((
        parsed.scheme,
        normalized_netloc,
        parsed.path.rstrip("/") if parsed.path != "/" else parsed.path,
        parsed.params,
        urlencode(kept, doseq=True),
        "",
    ))


def should_expand(url: str) -> bool:
    host = urlparse(url).netloc.lower().lstrip("www.")
    return host in SHORT_HOSTS or host.startswith("l.")


def expand_url(url: str, timeout: int = 12) -> str:
    """Follow redirects for short links using a browser-like HEAD/GET fallback."""
    cleaned = clean_url(url)
    if not should_expand(cleaned):
        return cleaned

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.head(cleaned, allow_redirects=True, timeout=timeout, headers=headers)
        if resp.url and resp.url != cleaned:
            expanded = clean_url(resp.url)
            download_logger.info("Expanded short URL via HEAD: %s -> %s", cleaned[:80], expanded[:100])
            return expanded
    except Exception:
        pass

    try:
        resp = requests.get(cleaned, allow_redirects=True, timeout=timeout, headers=headers, stream=True)
        if resp.url and resp.url != cleaned:
            expanded = clean_url(resp.url)
            download_logger.info("Expanded short URL via GET: %s -> %s", cleaned[:80], expanded[:100])
            return expanded
    except Exception as exc:
        download_logger.info("URL expansion failed for %s: %s", cleaned[:80], exc)

    return cleaned


def normalize_url(url: str) -> str:
    """Best-effort canonical URL used before analyze/download."""
    return expand_url(clean_url(url))
