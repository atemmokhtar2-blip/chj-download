from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import _select_engine
from services.engines import list_engines, registered_engines


def assert_engine(url: str, expected_name: str) -> None:
    engine = _select_engine(url)
    assert engine.name == expected_name, f"{url} routed to {engine.name}, expected {expected_name}"


def test_platform_routing() -> None:
    cases = {
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "YouTubeEnginePro",
        "https://youtu.be/dQw4w9WgXcQ": "YouTubeEnginePro",
        "https://www.tiktok.com/@user/video/123": "TikTokEnginePro",
        "https://vt.tiktok.com/abc/": "TikTokEnginePro",
        "https://www.instagram.com/reel/ABC/": "InstagramEnginePro",
        "https://www.facebook.com/watch/?v=123": "FacebookEnginePro",
        "https://fb.watch/abc/": "FacebookEnginePro",
        "https://x.com/user/status/123": "TwitterEnginePro",
        "https://twitter.com/user/status/123": "TwitterEnginePro",
        "https://www.reddit.com/r/videos/comments/abc/title/": "RedditEnginePro",
        "https://v.redd.it/abc123": "RedditEnginePro",
        "https://www.pinterest.com/pin/123/": "PinterestEnginePro",
        "https://pin.it/abc123": "PinterestEnginePro",
        "https://www.threads.net/@user/post/123": "ThreadsEnginePro",
        "https://www.snapchat.com/spotlight/123": "SnapchatEnginePro",
        "https://vimeo.com/123456": "VimeoEnginePro",
        "https://www.dailymotion.com/video/x123": "DailymotionEnginePro",
        "https://dai.ly/x123": "DailymotionEnginePro",
        "https://t.me/channel/123": "TelegramPublicEnginePro",
        "https://soundcloud.com/artist/track": "SoundCloudEnginePro",
        "https://open.spotify.com/track/123": "SpotifyEnginePro",
        "https://likee.video/@user/video/123": "LikeeEnginePro",
        "https://www.kwai.com/video/123": "KwaiEnginePro",
        "https://clips.twitch.tv/AbcDef123": "TwitchEnginePro",
        "https://www.bilibili.com/video/BV123": "BilibiliEnginePro",
        "https://vk.com/video-123_456": "VkEnginePro",
        "https://www.tumblr.com/user/123": "TumblrEnginePro",
        "https://imgur.com/gallery/abc": "ImgurEnginePro",
        "https://streamable.com/abc123": "StreamableEnginePro",
        "https://coub.com/view/abc123": "CoubEnginePro",
        "https://ok.ru/video/123": "OdnoklassnikiEnginePro",
    }
    for url, expected in cases.items():
        assert_engine(url, expected)


def test_engine_registry_matches_dashboard() -> None:
    engines = registered_engines()
    dashboard_names = [item["name"] for item in list_engines()]
    assert len(engines) == 24
    assert dashboard_names == [engine.name for engine in engines]
    assert all(item["status"] == "ready" for item in list_engines())
    assert all(item["domains"] > 0 for item in list_engines())


def test_unknown_url_uses_generic_engine() -> None:
    assert _select_engine("https://example.com/video/123").name == "GenericEngine"


if __name__ == "__main__":
    test_platform_routing()
    test_engine_registry_matches_dashboard()
    test_unknown_url_uses_generic_engine()
    print("engine routing smoke tests passed")
