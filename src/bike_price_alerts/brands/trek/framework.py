"""Resolve a Trek model and report the lowest verified US price.

A bike listed in the config is used when the model name matches it. Any other
current Trek model is resolved from Trek's public product catalog and priced
through the same Locally coverage scanner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from ...core.http import HTTPClient
from ...core.parsing import matching_size, normalize_size
from .models import AppConfig, LocallySpec, LocallyVariant, WatchSpec

_CARD_RE = re.compile(r'"code"\s*:\s*"(\d+)"\s*,\s*"name"\s*:\s*"([^"]+)"')
_YEAR_RE = re.compile(r"\b20\d{2}\b")
_SEARCH_URL = "https://www.trekbikes.com/us/en_US/search?text={query}"
_PRODUCT_URL = "https://api.trekbikes.com/occ/v2/us/products/{code}/full"
_MAX_CATALOG_PRODUCTS = 4


class ModelNotFound(LookupError):
    pass


@dataclass(frozen=True)
class CatalogProduct:
    code: str
    name: str


@dataclass(frozen=True)
class LowestOffer:
    model: str
    price_usd: str
    msrp_usd: str | None
    in_stock: bool
    retailer: str | None
    city: str | None
    state: str | None
    url: str
    size: str | None
    color: str | None
    upc: str | None
    store_id: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "price_usd": self.price_usd,
            "msrp_usd": self.msrp_usd,
            "in_stock": self.in_stock,
            "retailer": self.retailer,
            "city": self.city,
            "state": self.state,
            "url": self.url,
            "size": self.size,
            "color": self.color,
            "upc": self.upc,
            "store_id": self.store_id,
        }


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _watch_score(watch: WatchSpec, query: str) -> int:
    needle = _compact(query)
    if len(needle) < 4:
        return 0
    names = (watch.key, watch.canonical_name, *watch.aliases)
    compacts = [_compact(name) for name in names]
    if needle in compacts:
        return 2
    if any(name.startswith(needle) for name in compacts):
        return 1
    return 0


def _filter_variants(
    watch: WatchSpec,
    *,
    size: str | None,
    color: str | None,
) -> WatchSpec:
    variants = list(watch.locally.variants)
    if size:
        variants = [
            variant
            for variant in variants
            if matching_size([variant.size], (size,)) is not None
        ]
    if color:
        wanted = _compact(color)
        variants = [
            variant
            for variant in variants
            if variant.color and _compact(variant.color) == wanted
        ]
    if not variants:
        available = ", ".join(
            f"{variant.size}" + (f" {variant.color}" if variant.color else "")
            for variant in watch.locally.variants
        )
        raise ModelNotFound(
            f"{watch.canonical_name} has no variant matching "
            f"size={size or 'any'} color={color or 'any'}. Configured: {available}"
        )
    allowed = tuple(dict.fromkeys(normalize_size(variant.size) for variant in variants))
    return replace(
        watch,
        allowed_sizes=allowed or watch.allowed_sizes,
        locally=LocallySpec(
            variants=tuple(variants),
            seed_store_ids=watch.locally.seed_store_ids,
        ),
    )


def _configured_watches(
    config: AppConfig,
    model: str,
    *,
    size: str | None,
    color: str | None,
) -> tuple[WatchSpec, ...]:
    scored = [(_watch_score(watch, model), watch) for watch in config.watches]
    best = max((score for score, _ in scored), default=0)
    if best == 0:
        return ()
    selected = [watch for score, watch in scored if score == best]
    return tuple(_filter_variants(watch, size=size, color=color) for watch in selected)


def _catalog_cards(html: str) -> list[CatalogProduct]:
    text = html.replace("&#034;", '"').replace("&quot;", '"')
    seen: set[str] = set()
    products: list[CatalogProduct] = []
    for code, name in _CARD_RE.findall(text):
        if code in seen:
            continue
        seen.add(code)
        products.append(CatalogProduct(code=code, name=name))
    return products


def _select_catalog_products(products: list[CatalogProduct], model: str) -> list[CatalogProduct]:
    needle = _compact(model)
    matches = [
        product
        for product in products
        if _compact(product.name) == needle or _compact(product.name).startswith(needle)
    ]
    base = [product for product in matches if not _YEAR_RE.search(product.name)]
    chosen = base or matches
    chosen.sort(key=lambda product: (len(_compact(product.name)), product.name))
    if len(chosen) > _MAX_CATALOG_PRODUCTS:
        examples = ", ".join(product.name for product in chosen[:6])
        raise ModelNotFound(
            f"{model!r} matched {len(chosen)} Trek products. "
            f"Pass a more specific model name. Examples: {examples}"
        )
    return chosen


def _amount(value: Any) -> Decimal | None:
    if not isinstance(value, dict):
        return None
    low = value.get("low")
    if not isinstance(low, dict) or low.get("value") is None:
        return None
    amount = Decimal(str(low["value"]))
    return amount if amount > 0 else None


def _attribute_labels(product: dict[str, Any]) -> dict[str, dict[str, str]]:
    labels: dict[str, dict[str, str]] = {}
    for attribute in product.get("variantAttributesDirectory") or []:
        if not isinstance(attribute, dict):
            continue
        key = str(attribute.get("code") or "")
        if not key:
            continue
        labels[key] = {
            str(option.get("value")): str(option.get("displayName") or option.get("value"))
            for option in attribute.get("options") or []
            if isinstance(option, dict) and option.get("value")
        }
    return labels


def _variant_size_color(
    variant: dict[str, Any],
    labels: dict[str, dict[str, str]],
) -> tuple[str | None, str | None]:
    size = None
    color = None
    for item in variant.get("variantAttributes") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        raw = str(item.get("value") or "")
        label = labels.get(key, {}).get(raw, raw)
        if "size" in key.casefold():
            size = label
        elif "color" in key.casefold():
            color = label
    return size, color


def _locally_identity(
    http: HTTPClient,
    config: AppConfig,
    upc: str,
) -> tuple[int, str]:
    response = http.get(
        "https://trek.locally.com/stores/conversion_data",
        params={
            "company_id": config.locally_company_id,
            "upc": upc,
            "inline": 1,
            "parent_domain": "www.trekbikes.com",
            "map_center_lat": config.locally_center_lat,
            "map_center_lng": config.locally_center_lng,
            "map_distance_diag": 50,
            "forced_coords": 1,
            "dealers_company_id": config.locally_company_id,
        },
    )
    product = response.json().get("product") or {}
    product_id = int(product.get("id") or 0)
    style = str(product.get("style_number") or "").strip()
    if product_id <= 0 or not style:
        raise ModelNotFound(f"Locally did not identify UPC {upc}")
    return product_id, style


def _watch_from_catalog(
    http: HTTPClient,
    config: AppConfig,
    product: CatalogProduct,
    *,
    size: str | None,
    color: str | None,
) -> WatchSpec:
    payload = http.get(
        _PRODUCT_URL.format(code=product.code),
        headers={"Accept": "application/json"},
    ).json()
    body = payload.get("data", payload)
    if not isinstance(body, dict):
        raise ModelNotFound(f"Trek product {product.code} returned an unexpected payload")
    labels = _attribute_labels(body)
    drafts: list[tuple[str, str | None, str | None, Decimal | None]] = []
    for variant in body.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        upc = str(variant.get("upc") or "").strip()
        variant_size, variant_color = _variant_size_color(variant, labels)
        if not upc or not variant_size:
            continue
        if size and matching_size([variant_size], (size,)) is None:
            continue
        if color and (not variant_color or _compact(variant_color) != _compact(color)):
            continue
        prices = (variant.get("prices") or {}).get("consumerPrice") or {}
        msrp = _amount(prices.get("advertised")) or _amount(prices.get("price"))
        drafts.append((upc, variant_size, variant_color, msrp))
    if not drafts:
        raise ModelNotFound(
            f"{product.name} has no Trek variant matching "
            f"size={size or 'any'} color={color or 'any'}"
        )
    product_id, style = _locally_identity(http, config, drafts[0][0])
    locally_variants = tuple(
        LocallyVariant(
            upc=upc,
            product_id=product_id,
            style=style,
            size=variant_size or "",
            color=variant_color,
        )
        for upc, variant_size, variant_color, _msrp in drafts
    )
    msrp_values = [msrp for *_rest, msrp in drafts if msrp is not None]
    msrp = min(msrp_values) if msrp_values else None
    baseline = msrp or Decimal("1.00")
    slug = re.sub(r"[^a-z0-9]+", "_", product.name.casefold()).strip("_")
    return WatchSpec(
        key=f"{slug}_{product.code}"[:80],
        canonical_name=product.name,
        aliases=(product.name,),
        exclusions=(),
        allowed_sizes=tuple(dict.fromkeys(normalize_size(item.size) for item in locally_variants)),
        baseline_usd=baseline,
        msrp_usd=msrp,
        locally=LocallySpec(variants=locally_variants),
        public_urls=(),
    )


def resolve_watches(
    config: AppConfig,
    http: HTTPClient,
    *,
    model: str,
    size: str | None = None,
    color: str | None = None,
) -> tuple[WatchSpec, ...]:
    """Return the watches to scan for a model name."""

    query = model.strip()
    if not query:
        raise ModelNotFound("model is required")
    configured = _configured_watches(config, query, size=size, color=color)
    if configured:
        return configured
    search = http.get(
        _SEARCH_URL.format(query=quote(query)),
        headers={"Accept": "text/html"},
    )
    products = _select_catalog_products(_catalog_cards(search.text), query)
    if not products:
        raise ModelNotFound(f"No Trek product matched {query!r}")
    return tuple(
        _watch_from_catalog(http, config, product, size=size, color=color)
        for product in products
    )


def config_for_watches(config: AppConfig, watches: tuple[WatchSpec, ...]) -> AppConfig:
    return replace(config, watches=watches)


def lowest_offer(listings: list[dict[str, Any]]) -> LowestOffer | None:
    priced = [
        listing
        for listing in listings
        if listing.get("in_stock") and listing.get("sale_price_usd")
    ]
    if not priced:
        return None
    listing = min(priced, key=lambda item: Decimal(str(item["sale_price_usd"])))
    return LowestOffer(
        model=str(listing.get("canonical_name") or listing.get("model_key") or ""),
        price_usd=str(listing["sale_price_usd"]),
        msrp_usd=str(listing["msrp_usd"]) if listing.get("msrp_usd") else None,
        in_stock=True,
        retailer=listing.get("retailer"),
        city=listing.get("city"),
        state=listing.get("state"),
        url=str(listing.get("url") or ""),
        size=listing.get("size"),
        color=listing.get("color"),
        upc=listing.get("upc"),
        store_id=int(listing["store_id"]) if listing.get("store_id") is not None else None,
    )
