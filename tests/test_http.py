from __future__ import annotations

from email.message import Message
from email.utils import formatdate
from http.client import IncompleteRead
from urllib.error import HTTPError

from bike_price_alerts.core import http as http_module
from bike_price_alerts.core.http import HTTPClient


class _Headers:
    def get_content_charset(self) -> str:
        return "utf-8"


class _RawResponse:
    status = 200
    headers = _Headers()

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self.body

    def geturl(self) -> str:
        return "https://example.test/data"


class _RetryingOpener:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, _request, *, timeout):
        self.calls += 1
        if self.calls == 1:
            raise IncompleteRead(b"")
        return _RawResponse(b'{"ok": true}')


class _StatusOpener:
    def __init__(
        self,
        code: int,
        *,
        retry_after: str | None = None,
        succeed_on: int | None = None,
    ):
        self.calls = 0
        self.code = code
        self.retry_after = retry_after
        self.succeed_on = succeed_on

    def open(self, request, *, timeout):
        self.calls += 1
        if self.succeed_on is not None and self.calls >= self.succeed_on:
            return _RawResponse(b'{"ok": true}')
        headers = Message()
        if self.retry_after is not None:
            headers["Retry-After"] = self.retry_after
        raise HTTPError(request.full_url, self.code, "Too Many Requests", headers, None)


def _client(opener) -> HTTPClient:
    http_module.reset_rate_limiter()
    client = HTTPClient()
    client._opener = opener
    client.min_host_interval = 0
    return client


def test_http_client_retries_incomplete_read(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda _seconds: None)
    client = _client(_RetryingOpener())
    opener = client._opener

    response = client.get("https://example.test/data")

    assert opener.calls == 2
    assert response.json() == {"ok": True}


def test_retry_after_seconds_accepts_delay_and_http_date():
    delay = HTTPError(
        "https://example.test/data",
        429,
        "Too Many Requests",
        Message(),
        None,
    )
    delay.headers["Retry-After"] = "12.5"
    assert http_module._retry_after_seconds(delay) == 12.5

    dated = HTTPError(
        "https://example.test/data",
        429,
        "Too Many Requests",
        Message(),
        None,
    )
    dated.headers["Retry-After"] = formatdate(http_module.time.time() + 20, usegmt=True)
    assert 18 <= http_module._retry_after_seconds(dated) <= 22

    garbage = HTTPError(
        "https://example.test/data",
        429,
        "Too Many Requests",
        Message(),
        None,
    )
    garbage.headers["Retry-After"] = "not-a-date"
    assert http_module._retry_after_seconds(garbage) == 0.0


def test_backoff_honors_retry_after_and_caps_at_sixty_seconds(monkeypatch):
    monkeypatch.setattr(http_module.random, "uniform", lambda _start, _end: 0.25)

    assert http_module._backoff_seconds(0, 12) == 12.25
    assert http_module._backoff_seconds(10, 0) == 60.0


def test_too_many_requests_retries_more_than_other_server_errors(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(http_module.random, "uniform", lambda _start, _end: 0.0)
    limited = _StatusOpener(429, retry_after="12")
    client = _client(limited)

    try:
        client.get("https://example.test/data")
    except HTTPError as exc:
        assert exc.code == 429
    else:
        raise AssertionError("expected the 429 to exhaust its retries")

    assert limited.calls == 6
    assert http_module.rate_limit_penalty_seconds() == 2.0

    server_error = _StatusOpener(503)
    client = _client(server_error)
    try:
        client.get("https://example.test/data")
    except HTTPError as exc:
        assert exc.code == 503
    else:
        raise AssertionError("expected the 503 to exhaust its retries")
    assert server_error.calls == 3
    assert http_module.rate_limit_penalty_seconds() == 0.0


def test_client_error_is_not_retried_or_throttled(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda _seconds: None)
    missing = _StatusOpener(404)
    client = _client(missing)

    try:
        client.get("https://example.test/data")
    except HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("expected a 404")

    assert missing.calls == 1
    assert http_module.rate_limit_penalty_seconds() == 0.0


def test_rate_limiter_backs_off_globally_then_recovers():
    http_module.reset_rate_limiter()

    http_module.note_throttle(0)
    assert http_module.rate_limit_penalty_seconds() == 0.35
    http_module.note_throttle(0)
    assert http_module.rate_limit_penalty_seconds() == 0.7
    http_module.note_throttle(5)
    assert http_module.rate_limit_penalty_seconds() == 2.0

    for _ in range(200):
        http_module.note_success()
    assert http_module.rate_limit_penalty_seconds() == 0.0

    http_module.reset_rate_limiter()
    assert http_module.rate_limit_penalty_seconds() == 0.0


def test_accepted_request_after_429_keeps_a_shared_penalty(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(http_module.random, "uniform", lambda _start, _end: 0.0)
    opener = _StatusOpener(429, retry_after="12", succeed_on=2)
    client = _client(opener)

    response = client.get("https://example.test/data")

    assert opener.calls == 2
    assert response.json() == {"ok": True}
    assert 12 in sleeps
    assert http_module.rate_limit_penalty_seconds() == 2.0 * 0.97
