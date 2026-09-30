from __future__ import annotations

from services.engines.platforms import (
    FacebookEngine,
    InstagramEngine,
    PinterestEngine,
    RedditEngine,
    SoundCloudEngine,
    SpotifyEngine,
    TikTokEngine,
    TwitterEngine,
    YouTubeEngine,
)


def registered_engines():
    """Return the real platform engines enabled by the downloader router.

    Keep this as the single source of truth for platform routing, dashboard
    engine status, and future smoke tests.
    """
    return [
        YouTubeEngine(),
        TikTokEngine(),
        InstagramEngine(),
        FacebookEngine(),
        TwitterEngine(),
        RedditEngine(),
        PinterestEngine(),
        SoundCloudEngine(),
        SpotifyEngine(),
    ]


# Backward-compatible private alias for any older internal imports.
_registered_engines = registered_engines


def list_engines() -> list[dict]:
    return [
        {
            "name": engine.name,
            "status": "ready",
            "domains": len(getattr(engine, "domains", ()) or ()),
        }
        for engine in _registered_engines()
    ]
