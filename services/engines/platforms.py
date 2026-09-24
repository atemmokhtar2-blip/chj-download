from __future__ import annotations

import os
from typing import Callable

from config.settings import TEMP_DIR
from utils.helpers import sanitize_filename

from services.engines.base import PlatformEngine
from utils.logger import download_logger, error_logger


class YouTubeEngine(PlatformEngine):
    name = "YouTubeEngine"
    domains = ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com")


class TikTokEngine(PlatformEngine):
    name = "TikTokEnginePro"
    domains = ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com", "m.tiktok.com")

    async def analyze(self, url: str) -> dict | None:
        """TikTok Pro analyzer: providers race + CDN probing + photo-mode support."""
        from services import downloader
        from services.tiktok_scraper import scrape_tiktok
        from middlewares.concurrency import get_executor
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            tk = await loop.run_in_executor(get_executor(), scrape_tiktok, url)
            if tk and (tk.get("play_url") or tk.get("images") or tk.get("album_items") or tk.get("image_url")):
                normalized = downloader._normalize_tiktok_info(tk, url)
                normalized["engine_profile"] = "providers-race+cdn-probe+photo-mode"
                normalized["source"] = tk.get("source") or normalized.get("source")
                normalized["provider_score"] = tk.get("provider_score")
                normalized["provider_candidates"] = tk.get("provider_candidates") or []
                return self._tag(normalized)
        except Exception as exc:
            error_logger.error("TikTokEnginePro scraper analyze failed: %s", exc)

        fallback = await downloader._generic_analyze_url(url)
        if fallback:
            fallback["engine_profile"] = "yt-dlp-generic-fallback"
        return self._tag(fallback)

    async def download_video(
        self,
        url: str,
        format_id: str,
        quality_label: str,
        progress_callback: Callable | None = None,
        play_url: str | None = None,
    ) -> str | None:
        """Try verified no-watermark/direct CDN first, then fall back to generic yt-dlp."""
        from services import downloader
        from services.tiktok_scraper import download_tiktok_direct, scrape_tiktok
        from middlewares.concurrency import get_executor
        import asyncio

        loop = asyncio.get_running_loop()
        safe_name = sanitize_filename(f"tiktok_{hash(url) % 100000}_{quality_label}")
        direct_out = os.path.join(TEMP_DIR, safe_name)

        candidate_play = play_url
        if not candidate_play:
            try:
                meta = await loop.run_in_executor(get_executor(), scrape_tiktok, url)
                candidate_play = meta.get("play_url") if meta else None
                if meta:
                    download_logger.info(
                        "TikTokEnginePro selected source=%s score=%s",
                        meta.get("source"), meta.get("provider_score"),
                    )
            except Exception as exc:
                error_logger.error("TikTokEnginePro resolve before download failed: %s", exc)

        if candidate_play:
            try:
                path = await loop.run_in_executor(
                    get_executor(), download_tiktok_direct, candidate_play, direct_out
                )
                if path:
                    if progress_callback:
                        try:
                            await progress_callback({"pct": 100, "downloaded": 1, "total": 1, "speed": 0, "eta": 0})
                        except Exception:
                            pass
                    return downloader._enforce_max_file_size(path)
            except downloader.FileTooLargeError:
                raise
            except Exception as exc:
                error_logger.error("TikTokEnginePro direct download failed: %s", exc)

        return await downloader._generic_download_video(
            url, format_id, quality_label, progress_callback, play_url
        )

    async def download_audio(self, url: str, progress_callback: Callable | None = None) -> str | None:
        # Keep audio on yt-dlp/ffmpeg path for reliable mp3 conversion.
        from services import downloader
        return await downloader._generic_download_audio(url, progress_callback)


class InstagramEngine(PlatformEngine):
    name = "InstagramEnginePro"
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
                ig["engine_profile"] = "graphql+embed+gallery-dl+instaloader+cdn-probe"
                return self._tag(ig)
            if ig and ig.get("requires_login"):
                return self._tag(ig)
        except Exception as exc:
            error_logger.error("InstagramEnginePro scraper analyze failed: %s", exc)

        fallback = await downloader._generic_analyze_url(url)
        if fallback:
            fallback["engine_profile"] = "yt-dlp-generic-fallback"
        return self._tag(fallback)


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
    name = "FacebookEnginePro"
    domains = ("facebook.com", "fb.watch", "fb.com", "facebookreel.com")

    async def analyze(self, url: str) -> dict | None:
        """Facebook Pro analyzer: fb.watch expansion + HTML/OG + yt-dlp + CDN probe."""
        from services import downloader
        from services.facebook_scraper import scrape_facebook
        from middlewares.concurrency import get_executor
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            fb = await loop.run_in_executor(get_executor(), scrape_facebook, url)
            if fb and fb.get("play_url"):
                fb.setdefault("platform", "Facebook")
                fb["engine_profile"] = fb.get("engine_profile") or "yt-dlp+html+mobile+cdn-probe"
                return self._tag(fb)
            if fb and fb.get("requires_login"):
                return self._tag(fb)
        except Exception as exc:
            error_logger.error("FacebookEnginePro scraper analyze failed: %s", exc)

        fallback = await downloader._generic_analyze_url(url)
        if fallback:
            fallback["engine_profile"] = "yt-dlp-generic-fallback"
        return self._tag(fallback)

    async def download_video(
        self,
        url: str,
        format_id: str,
        quality_label: str,
        progress_callback: Callable | None = None,
        play_url: str | None = None,
    ) -> str | None:
        from services import downloader
        from services.facebook_scraper import scrape_facebook
        from middlewares.concurrency import get_executor
        import asyncio
        import requests

        loop = asyncio.get_running_loop()
        candidate = play_url
        if not candidate:
            try:
                meta = await loop.run_in_executor(get_executor(), scrape_facebook, url)
                if meta:
                    candidate = meta.get("hd_url") if format_id == "hd" else meta.get("play_url")
                    candidate = candidate or meta.get("play_url")
                    download_logger.info(
                        "FacebookEnginePro selected source=%s score=%s",
                        meta.get("source"), meta.get("provider_score"),
                    )
            except Exception as exc:
                error_logger.error("FacebookEnginePro resolve before download failed: %s", exc)

        if candidate:
            safe_name = sanitize_filename(f"facebook_{hash(url) % 100000}_{quality_label}")
            out_path = os.path.join(TEMP_DIR, safe_name + ".mp4")

            def _download_direct() -> str | None:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
                    "Referer": "https://www.facebook.com/",
                    "Range": "bytes=0-",
                }
                with requests.get(candidate, headers=headers, stream=True, timeout=60) as r:
                    if r.status_code not in (200, 206):
                        return None
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype or "application/json" in ctype:
                        return None
                    total = int(r.headers.get("Content-Length") or 0)
                    done = 0
                    with open(out_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            if progress_callback and total:
                                try:
                                    progress_callback(done, total)
                                except Exception:
                                    pass
                    return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 2048 else None

            try:
                direct_path = await loop.run_in_executor(get_executor(), _download_direct)
                if direct_path:
                    return direct_path
            except Exception as exc:
                error_logger.error("FacebookEnginePro direct download failed: %s", exc)

        return await downloader._generic_download_video(url, format_id, quality_label, progress_callback)


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
