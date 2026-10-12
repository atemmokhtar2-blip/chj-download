from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import (
    YOUTUBE_PLAYER_CLIENTS,
    _youtube_client_profiles,
    _base_opts,
)


def test_youtube_client_set_has_music_and_embedded_surfaces():
    for client in ("web_music", "web_embedded", "mediaconnect", "android_music", "ios_music"):
        assert client in YOUTUBE_PLAYER_CLIENTS, f"{client} missing from YOUTUBE_PLAYER_CLIENTS"


def test_youtube_profiles_include_music_embedded_mediaconnect():
    names = {name for name, _ in _youtube_client_profiles()}
    for required in ("music", "embedded", "mediaconnect"):
        assert required in names, f"profile {required} missing"
    for _, clients in _youtube_client_profiles():
        assert all(c in YOUTUBE_PLAYER_CLIENTS for c in clients)


def test_base_opts_rotates_tiktok_api_hosts():
    opts = _base_opts()
    tiktok = (opts.get("extractor_args") or {}).get("tiktok") or {}
    hosts = tiktok.get("api_hostname") or []
    assert len(hosts) >= 4, f"expected >=4 TikTok API hosts, got {hosts}"


if __name__ == "__main__":
    test_youtube_client_set_has_music_and_embedded_surfaces()
    test_youtube_profiles_include_music_embedded_mediaconnect()
    test_base_opts_rotates_tiktok_api_hosts()
    print("engine hardening tests passed")
