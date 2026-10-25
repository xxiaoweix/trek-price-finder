from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser

from ...core.geography import CONUS_STATE_CODES
from ...core.http import HTTPClient

TREK_US_DIRECTORY_URL = (
    "https://www.trekbikes.com/us/en_US/store-finder/retailer/US/"
)

_STATE_CODES = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}


@dataclass(frozen=True)
class OfficialStore:
    store_id: str
    name: str
    city: str
    state: str


@dataclass(frozen=True)
class OfficialStoreAudit:
    source_url: str
    page_count: int
    official_us_store_count: int
    official_conus_store_count: int


class _DirectoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._href:
            self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            label = " ".join("".join(self._link_text).split())
            self.links.append((self._href, label))
            self._href = None
            self._link_text = []


def _parse_directory_page(html: str) -> tuple[list[OfficialStore], tuple[int, int]]:
    parser = _DirectoryParser()
    parser.feed(html)
    page_matches = [
        (int(current), int(total))
        for current, total in re.findall(
            r"\b(\d+)\s+of\s+(\d+)\b",
            " ".join(parser.text),
            flags=re.IGNORECASE,
        )
    ]
    if not page_matches:
        raise ValueError("Trek directory response did not include pagination")
    current_page, page_count = max(page_matches, key=lambda item: item[1])
    if page_count < 1 or page_count > 100 or not 1 <= current_page <= page_count:
        raise ValueError(
            f"Trek directory returned invalid pagination {current_page} of {page_count}"
        )

    grouped: dict[str, list[str]] = {}
    for href, label in parser.links:
        match = re.fullmatch(r"/store/([^/]+)/", href)
        if match:
            grouped.setdefault(match.group(1), []).append(label)

    stores: list[OfficialStore] = []
    for store_id, labels in grouped.items():
        if len(labels) < 2 or "," not in labels[0]:
            continue
        city, state_name = (part.strip() for part in labels[0].rsplit(",", 1))
        stores.append(
            OfficialStore(
                store_id=store_id,
                name=labels[1],
                city=city,
                state=_STATE_CODES.get(state_name, state_name),
            )
        )
    if not stores:
        raise ValueError("Trek directory response did not include store rows")
    return stores, (current_page, page_count)


def audit_us_store_directory(
    http: HTTPClient,
    *,
    progress: Callable[[str], None] | None = None,
) -> OfficialStoreAudit:
    first_response = http.get(TREK_US_DIRECTORY_URL)
    first_stores, (current_page, page_count) = _parse_directory_page(
        first_response.text
    )
    if current_page != 1:
        raise ValueError(f"Trek directory first page identified itself as {current_page}")

    stores = {store.store_id: store for store in first_stores}
    if progress:
        progress(
            f"STORE_AUDIT page=1/{page_count} unique_official_stores={len(stores)}"
        )
    for page_index in range(1, page_count):
        response = http.get(f"{TREK_US_DIRECTORY_URL}{page_index}/")
        page_stores, (reported_page, reported_count) = _parse_directory_page(
            response.text
        )
        expected_page = page_index + 1
        if reported_page != expected_page or reported_count != page_count:
            raise ValueError(
                "Trek directory pagination changed during audit: "
                f"expected {expected_page} of {page_count}, "
                f"received {reported_page} of {reported_count}"
            )
        stores.update((store.store_id, store) for store in page_stores)
        if progress and (
            expected_page == page_count or expected_page % 5 == 0
        ):
            progress(
                f"STORE_AUDIT page={expected_page}/{page_count} "
                f"unique_official_stores={len(stores)}"
            )

    conus_count = sum(store.state in CONUS_STATE_CODES for store in stores.values())
    if conus_count == 0:
        raise ValueError("Trek directory returned no contiguous-US stores")
    return OfficialStoreAudit(
        source_url=TREK_US_DIRECTORY_URL,
        page_count=page_count,
        official_us_store_count=len(stores),
        official_conus_store_count=conus_count,
    )
