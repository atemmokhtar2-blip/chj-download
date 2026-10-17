from __future__ import annotations

from services.engines.platforms import (
    BandcampEngine,
    BilibiliEngine,
    BlueskyEngine,
    CoubEngine,
    DailymotionEngine,
    DouyinEngine,
    DzenEngine,
    FacebookEngine,
    ImgurEngine,
    InstagramEngine,
    KakaoTVEngine,
    KickEngine,
    KuaishouEngine,
    KwaiEngine,
    Lemon8Engine,
    LikeeEngine,
    MastodonEngine,
    MixcloudEngine,
    NaverTVEngine,
    NiconicoEngine,
    NineGagEngine,
    OdnoklassnikiEngine,
    OdyseeEngine,
    PinterestEngine,
    RedditEngine,
    RumbleEngine,
    RutubeEngine,
    SnapchatEngine,
    SoundCloudEngine,
    SpotifyEngine,
    StreamableEngine,
    TedEngine,
    TelegramPublicEngine,
    ThreadsEngine,
    TikTokEngine,
    TumblrEngine,
    TwitchEngine,
    TwitterEngine,
    VimeoEngine,
    VkEngine,
    WeiboEngine,
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
        KickEngine(),
        BilibiliEngine(),
        BlueskyEngine(),
        VkEngine(),
        TumblrEngine(),
        ImgurEngine(),
        StreamableEngine(),
        CoubEngine(),
        OdnoklassnikiEngine(),
        RumbleEngine(),
        RutubeEngine(),
        OdyseeEngine(),
        NiconicoEngine(),
        DouyinEngine(),
        XiaohongshuEngine(),
        KuaishouEngine(),
        NaverTVEngine(),
        KakaoTVEngine(),
        MastodonEngine(),
        WeiboEngine(),
        NineGagEngine(),
        Lemon8Engine(),
        DzenEngine(),
        TedEngine(),
        MixcloudEngine(),
        BandcampEngine(),
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
