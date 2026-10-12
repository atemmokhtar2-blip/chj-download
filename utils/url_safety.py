"""Server-side request forgery (SSRF) guard for user-supplied media URLs.

The bot downloads media from arbitrary http(s) URLs. Without a guard, an
attacker could point the server at internal addresses (``127.0.0.1``, cloud
metadata ``169.254.169.254``, internal dashboards, etc.) and make the bot
fetch/reflect data that should never leave the host.

Every entry point must call :func:`is_public_url` before analysis or download.
The function is synchronous (it may resolve DNS) — callers should run it in a
worker thread, e.g. ``asyncio.wait_for(asyncio.to_thread(is_public_url, url), 6)``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

# Numeric shorthand hosts ("2130706433", "0x7f000001") are always suspicious for
# media URLs — no legitimate platform link uses them.
_NUMERIC_HOST_RE = re.compile(r"^\d+$")


def is_private_ip(host_or_ip: str) -> bool:
    """True when the value is an IP literal inside a non-public range.

    Covers IPv4 + IPv6 loopback, private, link-local (incl. cloud metadata
    ``169.254.169.254``), multicast, unspecified and reserved ranges.
    Returns False for hostnames and public IPs.
    """
    try:
        ip = ipaddress.ip_address(str(host_or_ip).strip().strip("[]"))
    except ValueError:
        return False  # hostname, not an IP literal
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def resolve_host_ips(host: str) -> list[str]:
    """Return the unique IPs a hostname resolves to (empty when unresolvable)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return list({info[4][0] for info in infos})
    except OSError:
        return []


def is_public_url(url: str) -> tuple[bool, str]:
    """Validate that a URL points exclusively at public internet infrastructure.

    Blocks, in order:
      * non http(s) schemes and URLs with embedded ``user:pass@`` credentials
      * numeric-shorthand hosts and IP literals in any non-public range
      * hostnames whose DNS resolution yields ANY non-public address
        (covers localhost and also DNS-rebinding style attacks)

    Returns ``(is_safe, reason)``. A hostname that simply does not resolve is
    allowed (yt-dlp will fail it later) — the guard's job is to stop internal
    access, not to replicate the downloader's own error handling.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False, "invalid"

    if parsed.scheme not in {"http", "https"}:
        return False, "scheme"
    if parsed.username or parsed.password:
        return False, "userinfo"

    host = parsed.hostname
    if not host:
        return False, "invalid"
    if _NUMERIC_HOST_RE.match(host):
        return False, "private-ip"
    if is_private_ip(host):
        return False, "private-ip"

    ips = resolve_host_ips(host)
    if not ips:
        return True, "unresolved"
    for ip in ips:
        if is_private_ip(ip):
            return False, f"private-ip:{ip}"
    return True, "ok"
