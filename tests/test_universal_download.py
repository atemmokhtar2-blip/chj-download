from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.downloader import _select_engine
from services.engines.platforms import UniversalEngine
from services.engines import list_engines
from utils.helpers import is_valid_url


def test_unknown_site_routes_to_universal_engine():
    """Any yt-dlp-capable site must route through the universal fallback."""
    engine = _select_engine("https://some-random-site.example/watch?v=123")
    assert isinstance(engine, UniversalEngine)


def test_known_platforms_do_not_use_universal_engine():
    engine = _select_engine("https://www.youtube.com/watch?v=abc123")
    assert not isinstance(engine, UniversalEngine)


def test_dashboard_lists_universal_engine():
    names = [row["name"] for row in list_engines()]
    assert "UniversalEngine" in names
    universal = next(row for row in list_engines() if row["name"] == "UniversalEngine")
    assert universal["status"] == "ready"


def test_gate_still_rejects_non_urls():
    assert is_valid_url("not a url") is False
    assert is_valid_url("ftp://example.com/file") is False  # only http(s)


if __name__ == "__main__":
    test_unknown_site_routes_to_universal_engine()
    test_known_platforms_do_not_use_universal_engine()
    test_dashboard_lists_universal_engine()
    test_gate_still_rejects_non_urls()
    print("universal download tests passed")
