from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


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
        u = (url or "").lower()
        return any(domain in u for domain in self.domains)

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
