from concurrent.futures import ThreadPoolExecutor

import middlewares.rate_limiter as rate_limiter


def _clear_user(user_id: int) -> None:
    with rate_limiter._lock:
        rate_limiter._recent.pop(user_id, None)
        rate_limiter._downloads_hour.pop(user_id, None)
        rate_limiter._downloads_day.pop(user_id, None)


def test_mark_download_enforces_burst_limit_atomically(monkeypatch) -> None:
    user_id = 918273645
    _clear_user(user_id)
    monkeypatch.setattr(rate_limiter, "RATE_LIMIT_SECONDS", 60)
    monkeypatch.setattr(rate_limiter, "RATE_LIMIT_BURST", 1)
    monkeypatch.setattr(rate_limiter, "HOURLY_DOWNLOAD_LIMIT", 100)
    monkeypatch.setattr(rate_limiter, "DAILY_DOWNLOAD_LIMIT", 100)
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(rate_limiter.mark_download, [user_id] * 40))
        assert sum(results) == 1
        result = rate_limiter.check_rate_limit_detailed(user_id)
        assert not result.allowed
        assert result.reason == "burst"
        assert result.wait_seconds > 0
    finally:
        _clear_user(user_id)


def test_expired_burst_window_allows_next_download(monkeypatch) -> None:
    user_id = 918273646
    _clear_user(user_id)
    monkeypatch.setattr(rate_limiter, "RATE_LIMIT_SECONDS", 10)
    monkeypatch.setattr(rate_limiter, "RATE_LIMIT_BURST", 1)
    monkeypatch.setattr(rate_limiter, "HOURLY_DOWNLOAD_LIMIT", 100)
    monkeypatch.setattr(rate_limiter, "DAILY_DOWNLOAD_LIMIT", 100)
    now = [100.0]
    monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: now[0])
    try:
        assert rate_limiter.mark_download(user_id)
        assert not rate_limiter.mark_download(user_id)
        now[0] = 111.0
        assert rate_limiter.mark_download(user_id)
    finally:
        _clear_user(user_id)
