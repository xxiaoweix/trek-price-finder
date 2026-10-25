from __future__ import annotations

import json
import os
import random
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from http.cookiejar import CookieJar
from typing import Any, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_PENALTY_SECONDS = 0.0
_PENALTY_CEILING_SECONDS = 2.0
_OTHER_ATTEMPTS = 3
_TOO_MANY_REQUESTS_ATTEMPTS = 6


def reset_rate_limiter() -> None:
    """Clear process-wide throttle state. Tests use this between cases."""

    global _NEXT_REQUEST_AT, _PENALTY_SECONDS
    with _RATE_LOCK:
        _NEXT_REQUEST_AT = 0.0
        _PENALTY_SECONDS = 0.0


def rate_limit_penalty_seconds() -> float:
    with _RATE_LOCK:
        return _PENALTY_SECONDS


def note_throttle(retry_after: float = 0.0) -> None:
    """Slow every worker after a 429, and hold the next start for Retry-After."""

    global _NEXT_REQUEST_AT, _PENALTY_SECONDS
    pause = min(60.0, max(0.0, retry_after))
    with _RATE_LOCK:
        grown = _PENALTY_SECONDS * 2 if _PENALTY_SECONDS else 0.35
        _PENALTY_SECONDS = min(_PENALTY_CEILING_SECONDS, max(grown, 0.35, pause))
        _NEXT_REQUEST_AT = max(
            _NEXT_REQUEST_AT,
            time.monotonic() + max(_PENALTY_SECONDS, pause),
        )


def note_success() -> None:
    """Ease the global penalty a little after a request is accepted."""

    global _PENALTY_SECONDS
    with _RATE_LOCK:
        _PENALTY_SECONDS *= 0.97
        if _PENALTY_SECONDS < 0.02:
            _PENALTY_SECONDS = 0.0


def _retry_after_seconds(exc: HTTPError) -> float:
    raw = ""
    if exc.headers is not None:
        raw = str(exc.headers.get("Retry-After") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff_seconds(attempt: int, retry_after: float) -> float:
    delay = max(2**attempt, retry_after)
    jitter = random.uniform(0, min(delay, 1.0))
    return min(delay + jitter, 60.0)


@dataclass(frozen=True)
class Response:
    text: str
    url: str
    status_code: int

    def json(self) -> Any:
        return json.loads(self.text)


class HTTPClient:
    def __init__(self) -> None:
        timeout = float(os.getenv("TRACKER_TIMEOUT_SECONDS", "20"))
        user_agent = os.getenv(
            "TRACKER_USER_AGENT",
            "bike-price-alerts/0.3",
        )
        self.timeout = timeout
        self.max_response_bytes = int(os.getenv("TRACKER_MAX_RESPONSE_BYTES", "5242880"))
        self.min_host_interval = float(os.getenv("TRACKER_MIN_HOST_INTERVAL_SECONDS", "0.5"))
        self._last_request_at: dict[str, float] = {}
        self._cookie_jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookie_jar))
        self.default_headers = {
            "User-Agent": user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        }

    def close(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params, doseq=True)}"
        request_headers = dict(self.default_headers)
        if headers:
            request_headers.update(headers)
        request = Request(url, headers=request_headers)
        last_error: Exception | None = None
        for attempt in range(_TOO_MANY_REQUESTS_ATTEMPTS):
            host = request.host
            self._await_rate_limit(host)
            try:
                with self._opener.open(request, timeout=self.timeout) as raw:
                    body_bytes = raw.read(self.max_response_bytes + 1)
                    if len(body_bytes) > self.max_response_bytes:
                        raise ValueError(
                            f"Response exceeded {self.max_response_bytes} byte safety limit"
                        )
                    body = body_bytes.decode(
                        raw.headers.get_content_charset() or "utf-8", "replace"
                    )
                    note_success()
                    return Response(
                        text=body,
                        url=raw.geturl(),
                        status_code=raw.status,
                    )
            except HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise
                last_error = exc
                limit = _TOO_MANY_REQUESTS_ATTEMPTS if exc.code == 429 else _OTHER_ATTEMPTS
                retry_after = _retry_after_seconds(exc) if exc.code == 429 else 0.0
                if exc.code == 429:
                    note_throttle(retry_after)
                if attempt + 1 >= limit:
                    raise
                time.sleep(_backoff_seconds(attempt, retry_after))
                continue
            except (IncompleteRead, URLError, TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= _OTHER_ATTEMPTS:
                    raise
                time.sleep(_backoff_seconds(attempt, 0.0))
                continue
        assert last_error is not None
        raise last_error

    def _await_rate_limit(self, host: str) -> None:
        """Space request starts across every client in this process.

        Each client still honors ``TRACKER_MIN_HOST_INTERVAL_SECONDS``.
        ``TRACKER_GLOBAL_MIN_INTERVAL_SECONDS`` caps the combined rate when
        several workers share one host.
        """

        global _NEXT_REQUEST_AT
        floor = max(0.0, float(os.getenv("TRACKER_GLOBAL_MIN_INTERVAL_SECONDS", "0")))
        with _RATE_LOCK:
            now = time.monotonic()
            host_wait = self.min_host_interval - (now - self._last_request_at.get(host, 0.0))
            global_wait = _NEXT_REQUEST_AT - now
            wait = max(0.0, host_wait, global_wait)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_request_at[host] = now
            interval = floor + _PENALTY_SECONDS
            if interval > 0:
                _NEXT_REQUEST_AT = now + interval
