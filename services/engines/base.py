from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlparse


@dataclass(frozen=True)
class EngineMatch:
    name: str
    domains: tuple[str, ...]


class PlatformEngine:
    """Base class for platform-specific media engines.

    Engines keep platform quirks isolated while preserving the public downloader
    API used by Telegram handlers: analyze / download_video / download_audio.
    """

    name = "Generic"
    domains: tuple[str, ...] = ()

    def matches(self, url: str) -> bool:
        """Return True when the URL host belongs to this engine.

        The old implementation searched the entire URL string, which could
        accidentally route a generic URL whose query text mentioned a platform.
        This version focuses on the parsed hostname while still supporting the
        intentionally broad patterns used by engines such as ``pinterest.``.
        """
        raw = (url or "").strip().lower()
        if not raw:
            return False
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.netloc or parsed.path.split("/", 1)[0]).split("@")[-1].split(":", 1)[0]
        host = host.removeprefix("www.")
        if not host:
            return False

        for domain in self.domains:
            pattern = (domain or "").lower().removeprefix("www.")
            if not pattern:
                continue
            if pattern.endswith("."):
                if host.startswith(pattern) or pattern in host:
                    return True
            elif host == pattern or host.endswith(f".{pattern}"):
                return True
            elif pattern in {"fb.watch", "pin.it", "t.co"} and host == pattern:
                return True
        return False

    async def analyze(self, url: str) -> dict | None:
        from services import downloader
        info = await downloader._generic_analyze_url(url)
        return self._tag(info)

    async def download_video(
        self,
        url: str,
        format_id: str,
        quality_label: str,
        progress_callback: Callable | None = None,
        play_url: str | None = None,
    ) -> str | None:
        from services import downloader
        return await downloader._generic_download_video(
            url, format_id, quality_label, progress_callback, play_url
        )

    async def download_audio(self, url: str, progress_callback: Callable | None = None) -> str | None:
        from services import downloader
        return await downloader._generic_download_audio(url, progress_callback)

    def _tag(self, info: dict | None) -> dict | None:
        if info:
            info.setdefault("engine", self.name)
        return info


def first_match(url: str, engines: Iterable[PlatformEngine], fallback: PlatformEngine) -> PlatformEngine:
    for engine in engines:
        if engine.matches(url):
            return engine
    return fallback
