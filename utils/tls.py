"""
Browser-grade TLS / HTTP2 fingerprint impersonation (curl_cffi).

Plain `requests` emits a Python-client JA3 handshake that modern CDNs and
bot-filters (Reddit, Pinterest, TikTok, Twitter/X, ...) fingerprint and 403.
curl_cffi replays the exact JA3/JA4 + HTTP/2 SETTINGS handshake of a real
browser, so every request looks like a genuine Chrome/Safari client at the
network layer.

We try the newest bundled Chrome target first (chrome131 on curl_cffi 0.13)
and walk an explicit fallback chain so an older curl_cffi build that lacks a
target SKIPS it instead of raising. Helpers return None when curl_cffi isn't
installed, letting callers fall back to plain requests exactly as before.

Only `ImpersonateError` / `ValueError` (unknown-target errors) trigger the
fallback chain — genuine network errors still propagate so callers keep their
existing retry behavior.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

TLS_TARGETS = ("chrome131", "chrome124", "chrome120", "chrome", "safari_ios", "safari")

_state: dict = {}


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
    for target in TLS_TARGETS:
        try:
            return creq.get(
                url,
                headers=headers,
                impersonate=target,
                timeout=timeout,
                allow_redirects=allow_redirects,
                stream=stream,
                **kw,
            )
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
    for target in TLS_TARGETS:
        try:
            return creq.post(
                url,
                headers=headers,
                impersonate=target,
                timeout=timeout,
                allow_redirects=allow_redirects,
                **kw,
            )
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
    for target in TLS_TARGETS:
        try:
            s = creq.Session(impersonate=target, **kw)
            if headers:
                s.headers.update(headers)
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
