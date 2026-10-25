from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

from ....core.http import HTTPClient
from ....core.models import Confidence, Listing
from ....core.parsing import infer_generation, matching_size, normalize_size, title_matches
from ..models import AppConfig, LocallyVariant, LocallyWidgetPoint, WatchSpec


@dataclass(frozen=True)
class WidgetPrice:
    minimum: Decimal
    maximum: Decimal
    currency: str | None
    custom_price_count: int
    store_price_count: int
    is_sale: bool


@dataclass
class WidgetScanResult:
    listings: list[Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    saturated_upcs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WidgetDiscovery:
    store_ids: tuple[int, ...]
    saturated: bool
    marker_count: int
    payload: dict[str, Any]


class OutOfScopeStore(ValueError):
    pass


class _PriceAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.price_attrs: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "span" or self.price_attrs is not None:
            return
        values = {key: value or "" for key, value in attrs}
        if "pdp-price" in values.get("class", "").split():
            self.price_attrs = values


def parse_widget_price(fragment: str) -> WidgetPrice:
    parser = _PriceAttributeParser()
    parser.feed(fragment)
    attrs = parser.price_attrs
    if not attrs:
        raise ValueError("Locally widget response did not contain pdp-price attributes")
    try:
        minimum = Decimal(attrs["data-price-min"]).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        maximum = Decimal(attrs["data-price-max"]).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        custom_count = int(attrs.get("data-n-custom-prices", "0"))
        store_count = int(attrs.get("data-n-store-prices", "0"))
    except (InvalidOperation, KeyError, ValueError) as exc:
        raise ValueError("Locally widget returned invalid price attributes") from exc
    if minimum <= 0 or maximum <= 0 or maximum < minimum:
        raise ValueError("Locally widget returned an invalid price range")
    return WidgetPrice(
        minimum=minimum,
        maximum=maximum,
        currency=attrs.get("data-forced-currency") or attrs.get("data-range-currency"),
        custom_price_count=custom_count,
        store_price_count=store_count,
        is_sale=attrs.get("data-is-sale-price") == "1",
    )


def product_carrying_store_ids(payload: dict[str, Any]) -> tuple[int, ...]:
    """Stores stocking any UPC of the product, from one UPC's discovery call."""

    return tuple(
        dict.fromkeys(int(value) for value in payload.get("product_carrying_store_ids") or [])
    )


def carried_upc_codes(payload: dict[str, Any]) -> tuple[str, ...]:
    """UPCs a single store call says that store is carrying."""

    return tuple(dict.fromkeys(str(value) for value in payload.get("all_upcs_carried") or []))


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result > 0 else None


def _slug(product_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"trek-{product_name}".lower()).strip("-")


class LocallyWidgetCrawler:
    """Read the public Locally product-locator widget without loading the GUI.

    The crawler uses the same read-only session, geolocation and
    ``stores/conversion_data`` requests as Locally's public Trek widget. It
    does not solve or bypass Cloudflare challenges on hosted product pages.
    """

    BASE = "https://trek.locally.com"
    PARENT_DOMAIN = "www.trekbikes.com"

    def __init__(self, http: HTTPClient, config: AppConfig) -> None:
        self.http = http
        self.config = config

    @staticmethod
    def _map_center(point: LocallyWidgetPoint) -> dict[str, float]:
        # conversion_data accepts forced coordinates directly. Skipping the
        # session and geo-switch round trips keeps each cell to one request
        # and lets workers run without sharing cookies.
        return {"lat": point.lat, "lng": point.lng}

    def _conversion_params(
        self,
        variant: LocallyVariant,
        point: LocallyWidgetPoint,
        geo: dict[str, Any],
        *,
        only_store_id: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "company_id": self.config.locally_company_id,
            "style": variant.style,
            "upc": variant.upc,
            "inline": 1,
            "parent_domain": self.PARENT_DOMAIN,
            "map_center_lat": geo["lat"],
            "map_center_lng": geo["lng"],
            "map_distance_diag": point.distance_diag,
            "sort_by": "stock",
            "no_variants": 0,
            "dealers_company_id": self.config.locally_company_id,
            "forced_coords": 1,
            "q": point.label,
        }
        if only_store_id is not None:
            params["only_store_id"] = only_store_id
        return params

    def _conversion_data(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self.http.get(
            f"{self.BASE}/stores/conversion_data",
            params=params,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Locally widget conversion_data response was not an object")
        return payload

    @staticmethod
    def _marker(payload: dict[str, Any], store_id: int) -> dict[str, Any] | None:
        return next(
            (
                marker
                for marker in payload.get("markers", [])
                if int(marker.get("id", -1)) == store_id
            ),
            None,
        )

    def _listing(
        self,
        watch: WatchSpec,
        expected: LocallyVariant,
        point: LocallyWidgetPoint,
        store_id: int,
        base: dict[str, Any] | None,
        detail: dict[str, Any],
    ) -> Listing:
        marker = self._marker(detail, store_id) or self._marker(base or {}, store_id)
        if not marker:
            raise ValueError(f"Locally widget omitted marker metadata for store {store_id}")
        country = str(marker.get("country") or "").upper()
        if country and country != "US":
            raise OutOfScopeStore(
                f"store {store_id} is in country {country}, outside the US scan scope"
            )
        price = parse_widget_price(str(detail.get("variant_html") or ""))
        if price.minimum != price.maximum:
            raise ValueError(
                f"isolated store {store_id} returned a price range "
                f"{price.minimum}-{price.maximum}"
            )
        if price.currency != "USD":
            raise ValueError(
                f"isolated store {store_id} returned currency {price.currency!r}, expected USD"
            )
        stocking_store_ids = {
            int(value) for value in detail.get("upc_carrying_store_ids", [])
        }
        if store_id not in stocking_store_ids:
            raise ValueError(
                f"isolated store {store_id} was not confirmed in stock for the target UPC"
            )
        product = detail.get("product") or {}
        variant_data = product.get("upc_data") or {}
        actual_upc = str(variant_data.get("code") or "")
        if actual_upc != expected.upc:
            raise ValueError(
                f"isolated store {store_id} returned UPC {actual_upc!r}, "
                f"expected {expected.upc!r}"
            )
        title = str(product.get("name") or watch.canonical_name)
        if not title_matches(title, watch.aliases, watch.exclusions):
            raise ValueError(f"isolated store {store_id} returned wrong product {title!r}")
        size = matching_size(
            [
                str(variant_data.get("size_1") or ""),
                str(variant_data.get("size_2") or ""),
            ],
            (expected.size,),
        )
        if not size:
            raise ValueError(f"isolated store {store_id} returned a non-target size")
        product_id = int(product.get("id") or expected.product_id)
        if product_id != expected.product_id:
            raise ValueError(
                f"isolated store {store_id} returned product {product_id}, "
                f"expected {expected.product_id}"
            )
        actual_style = str(product.get("style_number") or "")
        if actual_style and actual_style != expected.style:
            raise ValueError(
                f"isolated store {store_id} returned style {actual_style!r}, "
                f"expected {expected.style!r}"
            )
        color = str(variant_data.get("color_name") or "").strip(": ") or None
        if expected.color and (
            not color
            or re.sub(r"\W+", "", color).casefold()
            != re.sub(r"\W+", "", expected.color).casefold()
        ):
            raise ValueError(
                f"isolated store {store_id} returned color {color!r}, "
                f"expected {expected.color!r}"
            )
        msrp = _positive_decimal(variant_data.get("unit_msrp"))
        if msrp is None:
            msrp = _positive_decimal(product.get("unit_msrp"))
        if watch.msrp_usd is not None and (msrp is None or msrp < watch.msrp_usd):
            msrp = watch.msrp_usd
        product_url = (
            f"{self.BASE}/product/{product_id}/{_slug(title)}"
            f"?store={store_id}"
        )
        inventory_refreshed_at = marker.get("inventory_refreshed_at")
        return Listing(
            model_key=watch.key,
            canonical_name=watch.canonical_name,
            source="locally_widget",
            url=product_url,
            title=title,
            generation=infer_generation(title),
            size=size,
            size_verified=True,
            in_stock=True,
            stock_verified=True,
            sale_price_usd=price.minimum,
            msrp_usd=msrp,
            retailer=str(marker.get("name") or "").strip() or None,
            city=marker.get("city"),
            state=str(marker.get("state") or "").strip().upper() or None,
            sku=expected.mpn,
            upc=expected.upc,
            color=color,
            product_id=product_id,
            store_id=store_id,
            confidence=Confidence.VERIFIED,
            evidence={
                "parser": "locally-widget-conversion-data-v1",
                "widget_point": point.label,
                "widget_lat": point.lat,
                "widget_lng": point.lng,
                "distance_diag": point.distance_diag,
                "distance_miles": marker.get("distance"),
                "address": marker.get("address"),
                "zip": marker.get("zip"),
                "store_lat": marker.get("lat"),
                "store_lng": marker.get("lng"),
                "inventory_refreshed_at": inventory_refreshed_at,
                "price_currency": price.currency,
                "custom_price_count": price.custom_price_count,
                "store_price_count": price.store_price_count,
                "is_sale_price": price.is_sale,
                "style": expected.style,
                "configured_product_id": expected.product_id,
                "configured_size": normalize_size(expected.size),
                "configured_color": expected.color,
            },
        )

    def store_payload(
        self,
        variant: LocallyVariant,
        point: LocallyWidgetPoint,
        store_id: int,
    ) -> dict[str, Any]:
        """One store call. ``all_upcs_carried`` is the UPCs that store stocks."""

        geo = self._map_center(point)
        return self._conversion_data(
            self._conversion_params(variant, point, geo, only_store_id=store_id)
        )

    def discover(
        self,
        watch: WatchSpec,
        variant: LocallyVariant,
        point: LocallyWidgetPoint,
    ) -> WidgetDiscovery:
        geo = self._map_center(point)
        params = self._conversion_params(variant, point, geo)
        payload = self._conversion_data(params)
        store_ids = tuple(
            dict.fromkeys(int(value) for value in payload.get("upc_carrying_store_ids", []))
        )
        return WidgetDiscovery(
            store_ids=store_ids,
            saturated=bool(payload.get("stores_are_overpopulated")),
            marker_count=len(payload.get("markers", [])),
            payload=payload,
        )

    def offer(
        self,
        watch: WatchSpec,
        variant: LocallyVariant,
        point: LocallyWidgetPoint,
        store_id: int,
        *,
        base: dict[str, Any] | None = None,
    ) -> Listing:
        geo = self._map_center(point)
        detail_params = self._conversion_params(
            variant,
            point,
            geo,
            only_store_id=store_id,
        )
        detail = self._conversion_data(detail_params)
        return self._listing(watch, variant, point, store_id, base, detail)

    def offers_at(self, watch: WatchSpec, point: LocallyWidgetPoint) -> WidgetScanResult:
        result = WidgetScanResult()
        seen: set[tuple[int, str]] = set()

        for variant in watch.locally.variants:
            try:
                discovery = self.discover(watch, variant, point)
            except Exception as exc:
                result.errors.append(
                    f"{watch.key} {point.label} UPC {variant.upc}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if discovery.saturated:
                result.saturated_upcs.append(variant.upc)
                continue
            if not discovery.store_ids:
                result.notes.append(
                    f"{watch.key} {point.label} UPC {variant.upc}: no stock"
                )
                continue
            for store_id in discovery.store_ids:
                key = (store_id, variant.upc)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    result.listings.append(
                        self.offer(
                            watch,
                            variant,
                            point,
                            store_id,
                            base=discovery.payload,
                        )
                    )
                except Exception as exc:
                    result.errors.append(
                        f"{watch.key} {point.label} store {store_id} UPC {variant.upc}: "
                        f"{type(exc).__name__}: {exc}"
                    )
        return result
