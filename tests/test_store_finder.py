from __future__ import annotations

import pytest

from bike_price_alerts.brands.trek.store_finder import (
    TREK_US_DIRECTORY_URL,
    audit_us_store_directory,
)


class FakeResponse:
    def __init__(self, text: str):
        self.text = text


class FakeHTTP:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        return FakeResponse(self.pages[url])


def _page(current: int, total: int, stores: list[tuple[str, str, str]]) -> str:
    rows = "".join(
        (
            f'<a href="/store/{store_id}/">{location}</a>'
            f'<a href="/store/{store_id}/">{name}</a>'
        )
        for store_id, location, name in stores
    )
    return f"<html><body>{rows}<p>{current} of {total}</p></body></html>"


def test_official_store_directory_is_paginated_and_counts_conus():
    pages = {
        TREK_US_DIRECTORY_URL: _page(
            1,
            2,
            [
                ("1", "Birmingham, Alabama", "Example Bikes"),
                ("2", "Anchorage, Alaska", "Northern Bikes"),
            ],
        ),
        f"{TREK_US_DIRECTORY_URL}1/": _page(
            2,
            2,
            [
                ("3", "Washington, District of Columbia", "Capital Bikes"),
            ],
        ),
    }
    http = FakeHTTP(pages)
    progress: list[str] = []

    audit = audit_us_store_directory(http, progress=progress.append)

    assert audit.page_count == 2
    assert audit.official_us_store_count == 3
    assert audit.official_conus_store_count == 2
    assert http.calls == [TREK_US_DIRECTORY_URL, f"{TREK_US_DIRECTORY_URL}1/"]
    assert progress[-1] == "STORE_AUDIT page=2/2 unique_official_stores=3"


def test_official_store_directory_fails_closed_without_pagination():
    http = FakeHTTP(
        {
            TREK_US_DIRECTORY_URL: (
                '<a href="/store/1/">Birmingham, Alabama</a>'
                '<a href="/store/1/">Example Bikes</a>'
            )
        }
    )

    with pytest.raises(ValueError, match="pagination"):
        audit_us_store_directory(http)
