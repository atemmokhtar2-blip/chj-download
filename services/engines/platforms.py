from __future__ import annotations

from typing import Callable

from services.engines.base import PlatformEngine
from utils.logger import download_logger, error_logger


class YouTubeEngine(PlatformEngine):
    name = "YouTubeEngine"
    domains = ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com")


class TikTokEngine(PlatformEngine):
    name = "TikTokEngine"
    domains = ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com", "m.tiktok.com")

    async def analyze(self, url: str) -> dict | None:
        # Dedicated scraper first; generic yt-dlp still runs as backup inside downloader.
        from services import downloader
        from services.tiktok_scraper import scrape_tiktok
        from middlewares.concurrency import get_executor
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            tk = await loop.run_in_executor(get_executor(), scrape_tiktok, url)
            if tk and (tk.get("play_url") or tk.get("images")):
                return self._tag(downloader._normalize_tiktok_info(tk, url))
        except Exception as exc:
            error_logger.error("TikTokEngine scraper analyze failed: %s", exc)
        return self._tag(await downloader._generic_analyze_url(url))


class InstagramEngine(PlatformEngine):
    name = "InstagramEngine"
    domains = ("instagram.com", "instagr.am", "cdninstagram.com")

    async def analyze(self, url: str) -> dict | None:
        from services import downloader
        from services.instagram_scraper import scrape_instagram
        from middlewares.concurrency import get_executor
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            ig = await loop.run_in_executor(get_executor(), scrape_instagram, url)
            if ig and (ig.get("image_url") or ig.get("play_url") or ig.get("album_items")):
                ig.setdefault("platform", "Instagram")
                return self._tag(ig)
        except Exception as exc:
            error_logger.error("InstagramEngine scraper analyze failed: %s", exc)
        return self._tag(await downloader._generic_analyze_url(url))


class PinterestEngine(PlatformEngine):
    name = "PinterestEngine"
    domains = ("pinterest.", "pin.it", "pinimg.com")

    async def analyze(self, url: str) -> dict | None:
        from services import downloader
        from services.pinterest_scraper import scrape_pinterest
        from middlewares.concurrency import get_executor
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            pin = await loop.run_in_executor(get_executor(), scrape_pinterest, url)
            if pin and (pin.get("image_url") or pin.get("play_url") or pin.get("album_items")):
                pin.setdefault("platform", "Pinterest")
                return self._tag(pin)
        except Exception as exc:
            error_logger.error("PinterestEngine scraper analyze failed: %s", exc)
        return self._tag(await downloader._generic_analyze_url(url))


class FacebookEngine(PlatformEngine):
    name = "FacebookEngine"
    domains = ("facebook.com", "fb.watch", "fb.com", "facebookreel.com")


class TwitterEngine(PlatformEngine):
    name = "TwitterEngine"
    domains = ("twitter.com", "x.com", "t.co", "fxtwitter.com", "fixupx.com", "nitter.net")


class RedditEngine(PlatformEngine):
    name = "RedditEngine"
    domains = ("reddit.com", "redd.it", "v.redd.it", "old.reddit.com")


class SoundCloudEngine(PlatformEngine):
    name = "SoundCloudEngine"
    domains = ("soundcloud.com", "m.soundcloud.com")


class GenericEngine(PlatformEngine):
    name = "GenericEngine"
    domains = ()
