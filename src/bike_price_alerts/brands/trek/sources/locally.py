from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from ....core.http import HTTPClient
from ....core.models import Confidence, Listing
from ....core.parsing import (
    city_state_from_text,
    infer_generation,
    matching_size,
    money_values,
    parse_product_id,
    parse_store_id,
    section,
    soup_text,
    title_matches,
)
from ..models import AppConfig, WatchSpec


def parse_locally_storefront(html: str, url: str, watch: WatchSpec) -> Listing | None:
    document, text = soup_text(html)
    title = document.h1 or watch.canonical_name
    if not title_matches(title, watch.aliases, watch.exclusions):
        return None

    header_area = section(text, title, ("Product Information", "Change Store"))
    prices = money_values(header_area)
    if len(prices) >= 2:
        msrp, sale = prices[0], prices[-1]
    elif len(prices) == 1:
        msrp, sale = None, prices[0]
    else:
        msrp, sale = None, None

    size_candidates = re.findall(
        r"(?:\b\d{2}\s*cm\b|\b\d{2}\b|\bM[/ -]?L\b|\bML\b)",
        header_area,
        re.IGNORECASE,
    )
    selected_size = matching_size(size_candidates, watch.allowed_sizes)
    size_verified = selected_size is not None

    in_stock = "in stock" in text.lower()
    store_heading = None
    for tag, value in document.headings:
        if tag not in {"h2", "h3"}:
            continue
        if (
            value
            and value.lower()
            not in {
                "product information",
                "product reviews",
                "purchase options",
                "select a store",
                "selected store",
            }
            and "review" not in value.lower()
        ):
            store_heading = value
            break
    city, state = city_state_from_text(text)

    # Locally often renders sizes as JS-powered <select> controls whose values
    # are absent from server HTML. Treat those pages as leads, not verified.
    stock_verified = bool(in_stock and size_verified)
    confidence = (
        Confidence.VERIFIED
        if sale is not None and size_verified and stock_verified and store_heading
        else Confidence.LEAD
    )
    return Listing(
        model_key=watch.key,
        canonical_name=watch.canonical_name,
        source="locally_storefront",
        url=url,
        title=title,
        generation=infer_generation(title),
        size=selected_size,
        size_verified=size_verified,
        in_stock=in_stock,
        stock_verified=stock_verified,
        sale_price_usd=sale,
        msrp_usd=msrp,
        retailer=store_heading,
        city=city,
        state=state,
        product_id=parse_product_id(url),
        store_id=parse_store_id(url),
        confidence=confidence,
        evidence={
            "size_candidates": size_candidates,
            "price_text": [str(x) for x in prices],
            "visible_in_stock": in_stock,
            "parser": "locally-storefront-v1",
        },
    )


@dataclass(frozen=True)
class AuthorizedStore:
    store_id: int
    name: str
    city: str | None
    state: str | None
    product_id: int | None
    product_url: str | None
    matching_upcs: tuple[str, ...]


class LocallyAPI:
    """Official Locally API client.

    This path is intentionally disabled without an explicit token. It does not
    attempt to extract or reuse Trek's browser credentials.
    """

    BASE = "https://www.locally.com"

    def __init__(self, http: HTTPClient, config: AppConfig) -> None:
        self.http = http
        self.config = config
        self.token = os.getenv("LOCALLY_API_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise RuntimeError("LOCALLY_API_TOKEN is required for official API calls")
        return {"Locally-Api-Token": self.token}

    def product_variants(
        self,
        watch: WatchSpec,
        *,
        style: str | None = None,
        upcs: tuple[str, ...] = (),
    ) -> dict[str, dict[str, Any]]:
        upcs = upcs or watch.locally.upcs
        if not style and not upcs:
            return {}
        params: dict[str, Any] = {
            "company_id": self.config.locally_company_id,
            "map_center_lat": self.config.locally_center_lat,
            "map_center_lng": self.config.locally_center_lng,
            "radius_in_miles": self.config.locally_radius_miles,
            "only_show_country": "US",
            "n_results": self.config.locally_page_limit,
        }
        if style:
            params["style"] = style
        else:
            params["upc"] = ",".join(upcs)
        response = self.http.get(
            f"{self.BASE}/headless/api/1.0/product/locate",
            params=params,
            headers=self._headers(),
        )
        payload = response.json()
        product = payload.get("data", {}).get("product", {})
        return {str(item["code"]): item for item in product.get("upcs", []) if item.get("code")}

    def nationwide_stores(self, watch: WatchSpec) -> list[AuthorizedStore]:
        results: list[AuthorizedStore] = []
        for style in watch.locally.styles:
            configured_upcs = tuple(
                variant.upc for variant in watch.locally.variants if variant.style == style
            )
            variants = self.product_variants(watch, style=style, upcs=configured_upcs)
            target_upcs = {
                code
                for code, item in variants.items()
                if code in configured_upcs
                and matching_size(
                    [str(item.get("size_1", "")), str(item.get("size_2", ""))],
                    watch.allowed_sizes,
                )
            }
            if not target_upcs:
                continue

            offset = 0
            while True:
                url = (
                    f"{self.BASE}/api/v2/brand/{self.config.locally_company_id}"
                    f"/style/{style}/stores"
                )
                response = self.http.get(
                    url,
                    params={"limit": self.config.locally_page_limit, "offset": offset},
                    headers=self._headers(),
                )
                payload = response.json()
                rows = payload.get("results", [])
                for row in rows:
                    stock = row.get("in_stock_upcs") or []
                    matched = tuple(
                        str(item["upc"])
                        for item in stock
                        if str(item.get("upc")) in target_upcs
                    )
                    if not matched:
                        continue
                    first = next(
                        (item for item in stock if str(item.get("upc")) in target_upcs),
                        {},
                    )
                    product_url = first.get("product_url") or row.get("store_url")
                    if product_url:
                        product_url = urljoin(self.BASE, str(product_url))
                    results.append(
                        AuthorizedStore(
                            store_id=int(row["store_id"]),
                            name=str(row.get("company_name") or row.get("name") or ""),
                            city=row.get("city"),
                            state=row.get("state"),
                            product_id=(
                                int(first["product_id"]) if first.get("product_id") else None
                            ),
                            product_url=product_url,
                            matching_upcs=matched,
                        )
                    )
                offset += len(rows)
                total = int(payload.get("total", len(rows)))
                if not rows or offset >= total:
                    break
        return results
