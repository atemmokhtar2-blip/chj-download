from __future__ import annotations

from services.engines.platforms import (
    BilibiliEngine,
    BlueskyEngine,
    CoubEngine,
    DailymotionEngine,
    DouyinEngine,
    FacebookEngine,
    ImgurEngine,
    InstagramEngine,
    KwaiEngine,
    LikeeEngine,
    NiconicoEngine,
    OdnoklassnikiEngine,
    OdyseeEngine,
    PinterestEngine,
    RedditEngine,
    RumbleEngine,
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
    XiaohongshuEngine,
    YouTubeEngine,
    UniversalEngine,
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
        BlueskyEngine(),
        VkEngine(),
        TumblrEngine(),
        ImgurEngine(),
        StreamableEngine(),
        CoubEngine(),
        OdnoklassnikiEngine(),
        RumbleEngine(),
        OdyseeEngine(),
        NiconicoEngine(),
        DouyinEngine(),
        XiaohongshuEngine(),
    ]


# Backward-compatible private alias for any older internal imports.
_registered_engines = registered_engines


def list_engines() -> list[dict]:
    rows = [
        {
            "name": engine.name,
            "status": "ready",
            "domains": len(getattr(engine, "domains", ()) or ()),
        }
        for engine in _registered_engines()
    ]
    # Show the universal catch-all explicitly so dashboards/status screens
    # communicate that ANY yt-dlp-supported site (1000+) is downloadable.
    rows.append({
        "name": "UniversalEngine",
        "status": "ready",
        "domains": "any",
        "note": "أي منصة يدعمها yt-dlp (1000+)",
    })
    return rows
