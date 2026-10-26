from decimal import Decimal

import pytest

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.sources.locally_widget import (
    LocallyWidgetCrawler,
    parse_widget_price,
)
from bike_price_alerts.core.models import Confidence


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def _marker(store_id, name, city, distance):
    return {
        "id": store_id,
        "name": name,
        "address": "123 Bike Lane",
        "city": city,
        "state": "CO",
        "zip": "80202",
        "distance": distance,
        "inventory_refreshed_at": "2026-07-27 01:12:43",
    }


def _product(upc):
    return {
        "id": 57363,
        "name": "Marlin 5 Gen 3",
        "style_number": "57363",
        "unit_msrp": 1000.00,
        "upc_data": {
            "code": upc,
            "color_name": "Miami Green/Dark Aquatic Fade",
            "size_1": 'M (29" wheel)',
            "size_2": "",
            "unit_msrp": 1000.00,
        },
    }


def _price_html(price):
    return (
        '<span class="pdp-price" '
        f'data-price-min="{price}" data-price-max="{price}" '
        'data-forced-currency="USD" data-n-store-prices="1" '
        'data-n-custom-prices="1" data-is-sale-price="1">'
        f"${price}</span>"
    )


class FakeWidgetHTTP:
    target_upc = "197216263494"

    def __init__(self):
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        params = params or {}
        self.calls.append((url, params, headers))
        if url.endswith("/conversion"):
            return FakeResponse({})
        if "/geo/point/" in url:
            return FakeResponse(
                {
                    "is_certain": True,
                    "city_label": "Denver, CO",
                    "lat": 39.7525,
                    "lng": -104.9995,
                }
            )
        if url.endswith("/stores/conversion_data"):
            upc = str(params["upc"])
            if upc != self.target_upc:
                return FakeResponse(
                    {
                        "stores_are_overpopulated": False,
                        "upc_carrying_store_ids": [],
                        "markers": [],
                        "product": _product(upc),
                        "variant_html": _price_html("1000.00"),
                    }
                )
            markers = [
                _marker(243954, "Example Bikes", "Denver", 5.8),
                _marker(207114, "George Garner Cyclery", "Lake Zurich", 50.2),
            ]
            store_id = params.get("only_store_id")
            if store_id is None:
                return FakeResponse(
                    {
                        "stores_are_overpopulated": False,
                        "upc_carrying_store_ids": [243954, 207114],
                        "markers": markers,
                        "product": _product(upc),
                        "variant_html": _price_html("1000.00"),
                    }
                )
            price = "800.00" if store_id == 243954 else "900.00"
            marker = next(item for item in markers if item["id"] == store_id)
            return FakeResponse(
                {
                    "stores_are_overpopulated": False,
                    "n_markers_stocking_upc": 1,
                    "upc_carrying_store_ids": [store_id],
                    "markers": [marker],
                    "product": _product(upc),
                    "variant_html": _price_html(price),
                }
            )
        raise AssertionError(f"unexpected URL {url}")


def test_widget_price_parser_reads_structured_attributes():
    price = parse_widget_price(_price_html("800.00"))

    assert price.minimum == Decimal("800.00")
    assert price.maximum == Decimal("800.00")
    assert price.currency == "USD"
    assert price.custom_price_count == 1
    assert price.is_sale


def test_public_widget_crawler_isolates_each_store_price():
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(item for item in config.watches if item.key == "marlin_5_gen_3_57363")
    point = config.scan_profile("canary").points[0]
    http = FakeWidgetHTTP()

    result = LocallyWidgetCrawler(http, config).offers_at(watch, point)

    assert not result.errors
    assert len(result.listings) == 2
    lowest = min(result.listings, key=lambda item: item.sale_price_usd)
    assert lowest.sale_price_usd == Decimal("800.00")
    assert lowest.msrp_usd == Decimal("1000.00")
    assert lowest.retailer == "Example Bikes"
    assert lowest.size == 'M (29" wheel)'
    assert lowest.upc == "197216263494"
    assert lowest.color == "Miami Green/Dark Aquatic Fade"
    assert lowest.confidence == Confidence.VERIFIED
    assert lowest.price_match_ready
    isolated = [
        params
        for url, params, _ in http.calls
        if url.endswith("/stores/conversion_data") and params.get("only_store_id")
    ]
    assert {params["only_store_id"] for params in isolated} == {243954, 207114}


def test_public_widget_uses_configured_msrp_when_locally_msrp_is_lower():
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(item for item in config.watches if item.key == "fuel_ex_5_gen_7_79586")
    variant = watch.locally.variants[0]
    marker = _marker(109794, "Example Bikes", "Austin", 12.0)
    detail = {
        "upc_carrying_store_ids": [109794],
        "markers": [marker],
        "product": {
            "id": variant.product_id,
            "name": "Fuel EX 5 Gen 7",
            "style_number": variant.style,
            "unit_msrp": 1500.00,
            "upc_data": {
                "code": variant.upc,
                "color_name": variant.color,
                "size_1": variant.size,
                "size_2": "",
                "unit_msrp": 1500.00,
            },
        },
        "variant_html": _price_html("1400.00"),
    }

    listing = LocallyWidgetCrawler(FakeWidgetHTTP(), config)._listing(
        watch,
        variant,
        config.scan_profile("canary").points[0],
        109794,
        None,
        detail,
    )

    assert listing.msrp_usd == Decimal("2000.00")
    assert listing.discount_pct == Decimal("30.0")


def test_widget_price_parser_fails_closed_without_contract():
    with pytest.raises(ValueError, match="pdp-price"):
        parse_widget_price("<span>$2,699.99</span>")


def test_widget_price_parser_normalizes_float_noise_to_usd_cents():
    fragment = (
        '<span class="pdp-price" data-price-min="5099.990234" '
        'data-price-max="5099.99" data-forced-currency="USD"></span>'
    )

    price = parse_widget_price(fragment)

    assert price.minimum == Decimal("5099.99")
    assert price.maximum == Decimal("5099.99")
