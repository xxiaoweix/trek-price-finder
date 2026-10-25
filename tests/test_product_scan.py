from decimal import Decimal
from pathlib import Path

import yaml

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.framework import config_for_watches
from bike_price_alerts.brands.trek.price_board import merge_upc_lows
from bike_price_alerts.brands.trek.product_scan import load_top_seller_watches, scan_top_sellers
from bike_price_alerts.brands.trek.storage import Storage


def _price(amount, maximum=None):
    high = amount if maximum is None else maximum
    return (
        '<span class="pdp-price" '
        f'data-price-min="{amount}" data-price-max="{high}" '
        'data-forced-currency="USD" data-n-store-prices="1" '
        'data-n-custom-prices="0" data-is-sale-price="0"></span>'
    )


def _marker(store_id, name, *, country="US", state="IL"):
    return {
        "id": store_id,
        "name": name,
        "city": "Rockford",
        "state": state,
        "country": country,
        "zip": "61101",
        "address": "1 Main",
        "lat": 42.27,
        "lng": -89.09,
    }


def _product(upc, *, size, color):
    return {
        "id": 99,
        "name": "Sample SL 5",
        "style_number": "111",
        "upc_data": {
            "code": upc,
            "color_name": color,
            "size_1": size,
            "size_2": "",
            "unit_msrp": 2000,
        },
    }


class FakeCatalogHTTP:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        params = params or {}
        self.calls.append(params)
        upc = str(params["upc"])
        store_id = params.get("only_store_id")
        if store_id is None:
            assert upc == "111111111111"
            return _Response(
                {
                    "stores_are_overpopulated": False,
                    "product_carrying_store_ids": [1, 2, 3],
                    "upc_carrying_store_ids": [1],
                    "all_upcs_carried": ["111111111111", "222222222222"],
                    "markers": [
                        _marker(1, "Probe Shop"),
                        _marker(2, "Sibling Shop"),
                        _marker(3, "Canada Shop", country="CA", state="ON"),
                    ],
                    "product": _product(upc, size="M", color="Black"),
                    "variant_html": _price("100.00"),
                }
            )
        carried = {
            1: ["111111111111", "222222222222"],
            2: ["222222222222"],
        }[store_id]
        if upc == "111111111111":
            size, color, amount = "M", "Black", "100.00"
        else:
            size, color, amount = "L", "Red", "90.00"
        return _Response(
            {
                "stores_are_overpopulated": False,
                "product_carrying_store_ids": [store_id],
                "upc_carrying_store_ids": [store_id] if upc in carried else [],
                "all_upcs_carried": carried,
                "markers": [_marker(store_id, "Shop")],
                "product": _product(upc, size=size, color=color),
                "variant_html": _price(amount),
            }
        )


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_locally_romance_language_sizes_match_the_catalog_label():
    from bike_price_alerts.core.parsing import matching_size

    assert matching_size(["P"], ("S",)) == "P"
    assert matching_size(["G"], ("L",)) == "G"
    assert matching_size(["MG"], ("ML",)) == "MG"
    assert matching_size(["XG"], ("XL",)) == "XG"
    assert matching_size(["M"], ("M",)) == "M"
    assert matching_size(["S"], ("S",)) == "S"
    assert matching_size(["P"], ("M",)) is None
    assert matching_size(['XS (27.5" wheel)'], ('XS (27.5" wheel)',)) == 'XS (27.5" wheel)'


def test_published_top_seller_list_locks_ten_families():
    raw = yaml.safe_load(Path("config/trek/top-sellers.yaml").read_text(encoding="utf-8"))
    assert [item["family"] for item in raw["families"]] == [
        "Checkpoint",
        "Domane",
        "Madone",
        "Marlin",
        "Fuel EX",
        "Supercaliber",
        "Roscoe",
        "Slash",
        "Procaliber",
        "Checkmate",
    ]
    for family in raw["families"]:
        assert str(family["source_url"]).startswith("https://")
    watches = load_top_seller_watches(Path("config/trek/top-sellers.yaml"))
    names = [watch.canonical_name for watch in watches]
    assert len(names) == 57
    assert sum(len(watch.locally.variants) for watch in watches) == 836
    assert "Top Fuel 8 Gen 4" not in names
    assert all("frameset" not in name.casefold() and "+" not in name for name in names)
    assert "Checkpoint ALR 3 Gen 3" in names
    assert "Domane AL 2 Gen 4" in names
    assert "Marlin 4 Gen 3" in names
    fuel = next(watch for watch in watches if watch.canonical_name == "Fuel EX 8 Gen 7")
    assert "Fuel EX 8" in fuel.aliases


def test_product_discovery_prices_only_carried_pairs(tmp_path):
    catalog = tmp_path / "top.yaml"
    catalog.write_text(
        """
products:
  - key: sample
    name: Sample SL 5
    style: "111"
    probe_upc: "111111111111"
    variants:
      - {upc: "111111111111", size: "M", color: "Black"}
      - {upc: "222222222222", size: "L", color: "Red"}
""",
        encoding="utf-8",
    )
    watches = load_top_seller_watches(catalog)
    settings = config_for_watches(load_config("tests/fixtures/parser-config.yaml"), watches)
    http = FakeCatalogHTTP()
    db = tmp_path / "board.sqlite3"
    with Storage(db) as storage:
        scan_id = storage.start_scan(
            started_at="2026-10-24T00:00:00+00:00",
            profile="canary",
            config_hash="test",
        )
        stats = scan_top_sellers(
            settings,
            http,
            storage,
            scan_id=scan_id,
            profile_name="canary",
            workers=1,
        )
        summary = storage.coverage_summary(scan_id)
        listings = storage.listings_for_scan(scan_id)

    assert summary["complete"] is True
    assert stats.result.errors == []
    assert stats.discovery_requests == 1
    assert stats.inventory_requests == 2
    assert stats.price_requests == 2
    discovery_upcs = [call["upc"] for call in http.calls if "only_store_id" not in call]
    assert discovery_upcs == ["111111111111"]
    assert sorted((item["store_id"], item["upc"], item["sale_price_usd"]) for item in listings) == [
        (1, "111111111111", "100.00"),
        (1, "222222222222", "90.00"),
        (2, "222222222222", "90.00"),
    ]
    assert all(item["retailer"] != "Canada Shop" for item in listings)


def test_a_price_range_is_omitted_and_does_not_fail_the_scan(tmp_path):
    catalog = tmp_path / "top.yaml"
    catalog.write_text(
        """
products:
  - key: sample
    name: Sample SL 5
    style: "111"
    probe_upc: "111111111111"
    variants:
      - {upc: "111111111111", size: "M", color: "Black"}
      - {upc: "222222222222", size: "L", color: "Red"}
""",
        encoding="utf-8",
    )
    watches = load_top_seller_watches(catalog)
    settings = config_for_watches(load_config("tests/fixtures/parser-config.yaml"), watches)

    class RangeHTTP(FakeCatalogHTTP):
        def get(self, url, *, params=None, headers=None):
            response = super().get(url, params=params, headers=headers)
            params = params or {}
            store_id = params.get("only_store_id")
            upc = str(params.get("upc"))
            if store_id == 1 and upc == "111111111111":
                response.payload["variant_html"] = _price("100.00", "120.00")
            if store_id == 2 and upc == "222222222222":
                response.payload["variant_html"] = _price("90.00", "110.00")
            return response

    http = RangeHTTP()
    with Storage(tmp_path / "board.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-10-24T00:00:00+00:00",
            profile="canary",
            config_hash="range",
        )
        stats = scan_top_sellers(
            settings,
            http,
            storage,
            scan_id=scan_id,
            profile_name="canary",
            workers=1,
        )
        summary = storage.coverage_summary(scan_id)
        listings = storage.listings_for_scan(scan_id)

    assert summary["complete"] is True
    assert stats.result.errors == []
    assert stats.unusable_prices == 2
    assert any("returned a range" in note for note in stats.result.notes)
    assert [(item["store_id"], item["upc"]) for item in listings] == [(1, "222222222222")]


def test_upc_low_moves_only_when_today_is_cheaper():
    previous = {"111": {"lowest_usd": "100.00", "lowest_at": "2026-10-21T00:00:00+00:00"}}
    offers = [
        {"upc": "111", "price_usd": "110.00"},
        {"upc": "222", "price_usd": "80.00"},
    ]

    kept = merge_upc_lows(previous, offers, "2026-10-24T00:00:00+00:00")

    assert kept["111"] == {"lowest_usd": "100.00", "lowest_at": "2026-10-21T00:00:00+00:00"}
    assert kept["222"]["lowest_usd"] == "80.00"
    cheaper = merge_upc_lows(kept, [{"upc": "111", "price_usd": "95.50"}], "2026-10-27T00:00:00+00:00")
    assert cheaper["111"]["lowest_usd"] == "95.50"
    assert Decimal(cheaper["111"]["lowest_usd"]) == Decimal("95.50")
