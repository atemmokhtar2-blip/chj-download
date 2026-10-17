"""
Browser-grade TLS / HTTP2 fingerprint impersonation (curl_cffi).

Plain `requests` emits a Python-client JA3 handshake that modern CDNs and
bot-filters (Reddit, Pinterest, TikTok, Twitter/X, ...) fingerprint and 403.
curl_cffi replays the exact JA3/JA4 + HTTP/2 SETTINGS handshake of a real
browser, so every request looks like a genuine Chrome/Safari client at the
network layer.

Targets are newest-first (chrome140 → chrome131 → … → safari) with an
explicit fallback chain: builds that don't know a target SKIP it instead of
raising, and the first target that succeeds is cached for the rest of the
process so later requests don't re-walk the chain. Helpers return None when
curl_cffi isn't installed, letting callers fall back to plain requests exactly
as before.

Only `ImpersonateError` / `ValueError` (unknown-target errors) trigger the
fallback chain — genuine network errors still propagate so callers keep their
existing retry behavior.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 2026/2027 browser fingerprints — the newest Chrome the installed curl_cffi
# build can impersonate is tried first.
TLS_TARGETS = (
    "chrome140", "chrome139", "chrome138", "chrome137", "chrome136", "chrome135",
    "chrome134", "chrome133", "chrome132", "chrome131",
    "chrome124", "chrome120", "chrome",
    "safari_ios", "safari",
)

_state: dict = {}


def _ordered_targets():
    """Best-target-first, with a process-wide cache.

    Once a request succeeds with a fingerprint, later calls try that same
    target FIRST (zero extra handshake-probe overhead) before falling back to
    the full newest→oldest chain.
    """
    cached = _state.get("target")
    if cached and cached in TLS_TARGETS:
        return (cached,) + tuple(t for t in TLS_TARGETS if t != cached)
    return TLS_TARGETS


def _creq():
    if "creq" not in _state:
        try:
            from curl_cffi import requests as creq
            _state["creq"] = creq
        except Exception as exc:  # pragma: no cover - environment dependent
            log.debug("curl_cffi unavailable, using plain requests fallback: %s", exc)
            _state["creq"] = None
    return _state["creq"]


def _impersonate_error_types():
    try:
        from curl_cffi.requests.errors import ImpersonateError
        return (ImpersonateError, ValueError)
    except Exception:  # pragma: no cover
        return (ValueError,)


def available() -> bool:
    return _creq() is not None


def get(url: str, headers=None, timeout: float = 15, allow_redirects: bool = True, stream: bool = False, **kw):
    creq = _creq()
    if creq is None:
        return None
    last_error = None
    err_types = _impersonate_error_types()
    for target in _ordered_targets():
        try:
            resp = creq.get(
                url,
                headers=headers,
                impersonate=target,
                timeout=timeout,
                allow_redirects=allow_redirects,
                stream=stream,
                **kw,
            )
            _state["target"] = target
            return resp
        except err_types as exc:  # unknown target on this curl_cffi build
            last_error = exc
            continue
    log.debug("no TLS impersonation target accepted: %s", last_error)
    return None


def post(url: str, headers=None, timeout: float = 15, allow_redirects: bool = True, **kw):
    creq = _creq()
    if creq is None:
        return None
    last_error = None
    err_types = _impersonate_error_types()
    for target in _ordered_targets():
        try:
            resp = creq.post(
                url,
                headers=headers,
                impersonate=target,
                timeout=timeout,
                allow_redirects=allow_redirects,
                **kw,
            )
            _state["target"] = target
            return resp
        except err_types as exc:
            last_error = exc
            continue
    log.debug("no TLS impersonation target accepted: %s", last_error)
    return None


def session(headers=None, **kw):
    """curl_cffi Session with the newest available browser fingerprint.

    Returns a Session whose `.get`/`.post` already carry the impersonation
    target (unlike plain creq.Session), or None when curl_cffi is missing.
    """
    creq = _creq()
    if creq is None:
        return None
    last_error = None
    err_types = _impersonate_error_types()
    for target in _ordered_targets():
        try:
            s = creq.Session(impersonate=target, **kw)
            if headers:
                s.headers.update(headers)
            _state["target"] = target
            return s
        except err_types as exc:
            last_error = exc
            continue
    log.debug("no TLS impersonation target accepted: %s", last_error)
    return None


def ytdlp_target():
    """ImpersonateTarget for yt-dlp opts (newest bundled Chrome)."""
    try:
        import yt_dlp
        return yt_dlp.networking.impersonate.ImpersonateTarget("chrome")
    except Exception:  # pragma: no cover
        return None


def chrome_ua(mobile: bool = False) -> str:
    """User-Agent matching the newest Chrome target in TLS_TARGETS."""
    if mobile:
        return (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.3 Mobile/15E148 Safari/604.1"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    )


def browser_headers(referer: str | None = None, mobile: bool = False) -> dict[str, str]:
    """Modern-Chrome header suite — Client Hints + Fetch Metadata + Priority.

    `Sec-CH-UA*` client hints and `Sec-Fetch-*` fetch metadata are checked by
    DataDome / Cloudflare / PerimeterX class filters; `Priority: u=0, i` is the
    Chrome 136+ HTTP/2 priority signal. Missing them is a classic "python bot"
    tell — this makes both the curl_cffi AND plain-requests paths talk like a
    2026 browser at the HTTP level.
    """
    h = {
        "User-Agent": chrome_ua(mobile),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/json;q=0.8,*/*;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "empty" if mobile else "document",
        "Sec-Fetch-Mode": "cors" if mobile else "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Priority": "u=0, i",
    }
    if mobile:
        h["Sec-CH-UA"] = (
            '"Chromium";v="139", "Google Chrome";v="139", "Not_A Brand";v="24"'
        )
        h["Sec-CH-UA-Mobile"] = "?1"
        h["Sec-CH-UA-Platform"] = '"Android"'
    else:
        h["Sec-CH-UA"] = (
            '"Chromium";v="139", "Google Chrome";v="139", "Not_A Brand";v="24"'
        )
        h["Sec-CH-UA-Mobile"] = "?0"
        h["Sec-CH-UA-Platform"] = '"Windows"'
    if referer:
        h["Referer"] = referer
    return h
