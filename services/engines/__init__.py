from __future__ import annotations

from services.engines.platforms import (
    BilibiliEngine,
    CoubEngine,
    DailymotionEngine,
    FacebookEngine,
    ImgurEngine,
    InstagramEngine,
    KwaiEngine,
    LikeeEngine,
    OdnoklassnikiEngine,
    PinterestEngine,
    RedditEngine,
    SnapchatEngine,
    SoundCloudEngine,
    SpotifyEngine,
    StreamableEngine,
    TelegramPublicEngine,
    ThreadsEngine,
    TikTokEngine,
    TumblrEngine,
    TwitchEngine,
    TwitterEngine,
    VimeoEngine,
    VkEngine,
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
        ThreadsEngine(),
        SnapchatEngine(),
        VimeoEngine(),
        DailymotionEngine(),
        TelegramPublicEngine(),
        SoundCloudEngine(),
        SpotifyEngine(),
        LikeeEngine(),
        KwaiEngine(),
        TwitchEngine(),
        BilibiliEngine(),
        VkEngine(),
        TumblrEngine(),
        ImgurEngine(),
        StreamableEngine(),
        CoubEngine(),
        OdnoklassnikiEngine(),
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
