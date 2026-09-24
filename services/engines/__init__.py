from __future__ import annotations

from services.engines.base import first_match, PlatformEngine
from services.engines.platforms import (
    YouTubeEngine,
    TikTokEngine,
    InstagramEngine,
    PinterestEngine,
    FacebookEngine,
    TwitterEngine,
    RedditEngine,
    SoundCloudEngine,
    SpotifyEngine,
    GenericEngine,
)

GENERIC_ENGINE = GenericEngine()
ENGINES: tuple[PlatformEngine, ...] = (
    YouTubeEngine(),
    TikTokEngine(),
    InstagramEngine(),
    PinterestEngine(),
    FacebookEngine(),
    TwitterEngine(),
    RedditEngine(),
    SoundCloudEngine(),
    SpotifyEngine(),
)


def get_engine(url: str) -> PlatformEngine:
    return first_match(url, ENGINES, GENERIC_ENGINE)


def list_engines() -> list[dict]:
    return [
        {"name": engine.name, "domains": list(engine.domains)}
        for engine in (*ENGINES, GENERIC_ENGINE)
    ]
