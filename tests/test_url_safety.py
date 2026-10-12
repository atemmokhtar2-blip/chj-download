from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.url_safety import is_private_ip, is_public_url
from utils.helpers import summarize_error


def test_private_ip_literals_detected() -> None:
    for private in ("127.0.0.1", "10.0.0.8", "172.16.0.1", "192.168.1.1",
                    "169.254.169.254", "::1", "fe80::1", "0.0.0.0"):
        assert is_private_ip(private), private
    assert not is_private_ip("8.8.8.8")
    assert not is_private_ip("1.1.1.1")


def test_public_url_blocks_private_literals() -> None:
    ok, reason = is_public_url("http://127.0.0.1:8080/admin")
    assert not ok and reason == "private-ip"
    ok, reason = is_public_url("http://169.254.169.254/latest/meta-data/")
    assert not ok and reason == "private-ip"
    ok, _ = is_public_url("http://[::1]/")
    assert not ok


def test_public_url_blocks_numeric_shorthand_hosts() -> None:
    # 2130706433 is the decimal shorthand for 127.0.0.1
    ok, reason = is_public_url("http://2130706433/")
    assert not ok and reason == "private-ip"


def test_public_url_blocks_hostname_resolving_to_private() -> None:
    ok, _ = is_public_url("http://localhost/")
    assert not ok


def test_public_url_allows_public_targets() -> None:
    ok, _ = is_public_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert ok
    ok, _ = is_public_url("https://1.1.1.1/")
    assert ok
    ok, reason = is_public_url("http://nonexistent.invalid/")
    assert ok and reason == "unresolved"


def test_userinfo_and_nonhttp_blocked() -> None:
    ok, reason = is_public_url("http://user:pass@example.com/x")
    assert not ok and reason == "userinfo"
    ok, reason = is_public_url("ftp://example.com/")
    assert not ok and reason == "scheme"
    ok, reason = is_public_url("not-a-url")
    assert not ok and reason in {"scheme", "invalid"}


def test_summarize_error_redacts_tokens_and_url_credentials() -> None:
    text = summarize_error(
        "Failed with token 1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuv "
        "and https://user:secret@example.com/x"
    )
    assert "1234567890" not in text
    assert "user:secret" not in text
    assert "<token-redacted>" in text
    assert "<user-redacted>" in text


if __name__ == "__main__":
    test_private_ip_literals_detected()
    test_public_url_blocks_private_literals()
    test_public_url_blocks_numeric_shorthand_hosts()
    test_public_url_blocks_hostname_resolving_to_private()
    test_public_url_allows_public_targets()
    test_userinfo_and_nonhttp_blocked()
    test_summarize_error_redacts_tokens_and_url_credentials()
    print("url safety tests passed")
