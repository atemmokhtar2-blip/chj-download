from __future__ import annotations

ENGINES = [
    "YouTubeEngine", "TikTokEngine", "InstagramEngine", "FacebookEngine",
    "TwitterXEngine", "ThreadsEngine", "RedditEngine", "PinterestEngine",
    "SnapchatEngine", "VimeoEngine", "DailymotionEngine", "SoundCloudEngine",
    "TelegramPublicEngine", "GenericEngine",
]


def list_engines() -> list[dict]:
    return [{"name": name, "status": "ready"} for name in ENGINES]
