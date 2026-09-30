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


def _registered_engines():
    """Return the real platform engines enabled by the downloader router."""
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


def list_engines() -> list[dict]:
    return [
        {
            "name": engine.name,
            "status": "ready",
            "domains": len(getattr(engine, "domains", ()) or ()),
        }
        for engine in _registered_engines()
    ]
