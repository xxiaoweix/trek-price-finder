import json
from decimal import Decimal

import pytest

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.framework import (
    ModelNotFound,
    lowest_offer,
    resolve_watches,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self.payload, str):
            raise TypeError("not json")
        return self.payload


class CatalogHTTP:
    def __init__(self):
        self.urls = []

    def get(self, url, *, params=None, headers=None):
        self.urls.append(url)
        if url.startswith("https://www.trekbikes.com/"):
            return _Response(
                '<div :product="{ "code" : "57361", "name" : "Marlin 4 Gen 3", '
                '"url" : "/p/57361/" }'
                '{ "code" : "99901", "name" : "Marlin 4 Gen 3 - 2025, M" }'
                '{ "code" : "79586", "name" : "Fuel EX 5 Gen 7" }'
            )
        if url.endswith("/products/57361/full"):
            return _Response(
                {
                    "code": "57361",
                    "name": "Marlin 4 Gen 3",
                    "variantAttributesDirectory": [
                        {
                            "code": "bike_sizeFrame",
                            "options": [
                                {"value": "bike_sizeframe_m", "displayName": 'M (29" wheel)'},
                                {"value": "bike_sizeframe_l", "displayName": 'L (29" wheel)'},
                            ],
                        },
                        {
                            "code": "bike_colorSwatch",
                            "options": [
                                {"value": "bike_colorswatch_web", "displayName": "Matte Dark Web"}
                            ],
                        },
                    ],
                    "variants": [
                        {
                            "upc": "197216252054",
                            "variantAttributes": [
                                {"key": "bike_sizeFrame", "value": "bike_sizeframe_m"},
                                {"key": "bike_colorSwatch", "value": "bike_colorswatch_web"},
                            ],
                            "prices": {
                                "consumerPrice": {
                                    "advertised": {"low": {"value": 899.99}},
                                    "price": {"low": {"value": 899.99}},
                                }
                            },
                        },
                        {
                            "upc": "197216252061",
                            "variantAttributes": [
                                {"key": "bike_sizeFrame", "value": "bike_sizeframe_l"},
                                {"key": "bike_colorSwatch", "value": "bike_colorswatch_web"},
                            ],
                            "prices": {"consumerPrice": {"price": {"low": {"value": 899.99}}}},
                        },
                    ],
                }
            )
        if "conversion_data" in url:
            assert params["upc"] == "197216252054"
            return _Response({"product": {"id": 57361, "style_number": "57361", "name": "Marlin 4 Gen 3"}})
        raise AssertionError(url)


def test_configured_model_uses_the_catalog_without_network():
    config = load_config("tests/fixtures/parser-config.yaml")

    class Boom:
        def get(self, url, *, params=None, headers=None):
            raise AssertionError(url)

    watches = resolve_watches(config, Boom(), model="Marlin 5", size='M (29" wheel)')

    assert [watch.key for watch in watches] == ["marlin_5_gen_3_57363"]
    assert watches[0].locally.variants
    assert {variant.size for variant in watches[0].locally.variants} == {'M (29" wheel)'}


def test_configured_model_rejects_unknown_size():
    config = load_config("tests/fixtures/parser-config.yaml")
    with pytest.raises(ModelNotFound):
        resolve_watches(config, None, model="Marlin 5", size="10")


def test_catalog_model_uses_trek_json_and_locally_identity():
    config = load_config("tests/fixtures/parser-config.yaml")
    http = CatalogHTTP()

    watches = resolve_watches(config, http, model="Marlin 4 Gen 3", size='M (29" wheel)')

    assert len(watches) == 1
    watch = watches[0]
    assert watch.canonical_name == "Marlin 4 Gen 3"
    assert len(watch.locally.variants) == 1
    variant = watch.locally.variants[0]
    assert variant.upc == "197216252054"
    assert variant.product_id == 57361
    assert variant.style == "57361"
    assert variant.size == 'M (29" wheel)'
    assert variant.color == "Matte Dark Web"
    assert watch.msrp_usd == Decimal("899.99")
    assert any(url.endswith("/products/57361/full") for url in http.urls)
    assert not any("99901" in url for url in http.urls)


def test_lowest_offer_picks_the_in_stock_minimum():
    offer = lowest_offer(
        [
            {
                "canonical_name": "Marlin 5 Gen 3",
                "sale_price_usd": "900.00",
                "msrp_usd": "1000.00",
                "in_stock": True,
                "retailer": "Higher",
                "city": "A",
                "state": "IL",
                "url": "https://trek.locally.com/higher",
                "size": 'M (29" wheel)',
                "color": "Miami Green",
                "upc": "1",
                "store_id": 2,
            },
            {
                "canonical_name": "Marlin 5 Gen 3",
                "sale_price_usd": "700.00",
                "msrp_usd": "1000.00",
                "in_stock": False,
                "retailer": "Out",
                "city": "B",
                "state": "IL",
                "url": "https://trek.locally.com/out",
                "size": 'M (29" wheel)',
                "color": "Miami Green",
                "upc": "1",
                "store_id": 3,
            },
            {
                "canonical_name": "Marlin 5 Gen 3",
                "sale_price_usd": "800.00",
                "msrp_usd": "1000.00",
                "in_stock": True,
                "retailer": "Trek Bicycle Rockford",
                "city": "Loves Park",
                "state": "IL",
                "url": "https://trek.locally.com/low",
                "size": 'M (29" wheel)',
                "color": "Miami Green",
                "upc": "1",
                "store_id": 9,
            },
        ]
    )

    assert offer is not None
    assert offer.price_usd == "800.00"
    assert offer.retailer == "Trek Bicycle Rockford"
    assert offer.url == "https://trek.locally.com/low"
