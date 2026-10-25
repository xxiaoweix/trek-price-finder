from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*\.[0-9]{2})")
YEAR_RE = re.compile(r"\b(20[2-3][0-9])\b")
GEN_RE = re.compile(r"\bGen\s*(\d+)\b", re.IGNORECASE)
STATE_RE = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")


@dataclass(frozen=True)
class HTMLDocument:
    text: str
    h1: str | None
    headings: tuple[tuple[str, str], ...]


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and not self._skip_depth:
            self._heading_tag = tag
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == self._heading_tag:
            value = re.sub(r"\s+", " ", " ".join(self._heading_parts)).strip()
            if value:
                self.headings.append((tag, value))
            self._heading_tag = None
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        self.tokens.append(value)
        if self._heading_tag:
            self._heading_parts.append(value)


def soup_text(html: str) -> tuple[HTMLDocument, str]:
    parser = _VisibleTextParser()
    parser.feed(html)
    text = "\n".join(parser.tokens)
    h1 = next((value for tag, value in parser.headings if tag == "h1"), None)
    document = HTMLDocument(text=text, h1=h1, headings=tuple(parser.headings))
    return document, text


def money_values(text: str) -> list[Decimal]:
    return [Decimal(x.replace(",", "")) for x in PRICE_RE.findall(text)]


def section(text: str, start: str, end_candidates: tuple[str, ...]) -> str:
    start_pos = text.lower().find(start.lower())
    if start_pos < 0:
        return ""
    body = text[start_pos + len(start) :]
    positions = [pos for end in end_candidates if (pos := body.lower().find(end.lower())) >= 0]
    return body[: min(positions)] if positions else body


# Some Trek colorways are stored in Locally with Spanish or Italian size codes.
_SIZE_ALIASES = {
    "P": "S",
    "G": "L",
    "MG": "M/L",
    "XG": "XL",
}


def normalize_size(value: str) -> str:
    normalized = value.upper().replace(" ", "")
    normalized = normalized.replace("CM", "")
    normalized = normalized.replace("-", "/")
    if normalized == "ML":
        normalized = "M/L"
    return _SIZE_ALIASES.get(normalized, normalized)


def matching_size(available: list[str], allowed: tuple[str, ...]) -> str | None:
    allowed_norm = {normalize_size(x) for x in allowed}
    for value in available:
        if normalize_size(value) in allowed_norm:
            return value
    return None


def title_matches(title: str, aliases: tuple[str, ...], exclusions: tuple[str, ...]) -> bool:
    lowered = title.lower()
    if any(x.lower() in lowered for x in exclusions):
        return False
    return any(x.lower() in lowered for x in aliases)


def infer_generation(title: str) -> str | None:
    match = GEN_RE.search(title)
    return f"Gen {match.group(1)}" if match else None


def infer_year(text: str) -> int | None:
    match = YEAR_RE.search(text)
    return int(match.group(1)) if match else None


def parse_store_id(url: str) -> int | None:
    raw = parse_qs(urlparse(url).query).get("store")
    if not raw:
        return None
    try:
        return int(raw[0])
    except ValueError:
        return None


def parse_product_id(url: str) -> int | None:
    match = re.search(r"/product/(\d+)", url)
    return int(match.group(1)) if match else None


def city_state_from_text(text: str) -> tuple[str | None, str | None]:
    lines = text.splitlines()
    for line in lines:
        match = STATE_RE.search(line)
        if match:
            state = match.group(1)
            city = line[: match.start()].strip(" ,")
            return city or None, state
    return None, None
