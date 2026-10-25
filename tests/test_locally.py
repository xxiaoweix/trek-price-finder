from decimal import Decimal

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.sources.locally import LocallyAPI, parse_locally_storefront
from bike_price_alerts.core.models import Confidence


def _watch():
    config = load_config("tests/fixtures/parser-config.yaml")
    return config, next(x for x in config.watches if x.key == "domane_al_2_gen_4_80164")


def test_locally_exact_size_can_be_verified(fixture_dir):
    _, watch = _watch()
    html = (fixture_dir / "locally_domane_al_2_exact.html").read_text()
    url = "https://trek.locally.com/product/80164/x?store=10001"
    listing = parse_locally_storefront(html, url, watch)

    assert listing is not None
    assert listing.sale_price_usd == Decimal("980.00")
    assert listing.msrp_usd == Decimal("1200.00")
    assert listing.size == "52"
    assert listing.store_id == 10001
    assert listing.product_id == 80164
    assert listing.confidence == Confidence.VERIFIED


def test_locally_hidden_size_is_only_a_lead(fixture_dir):
    _, watch = _watch()
    html = (fixture_dir / "locally_domane_al_2_ambiguous.html").read_text()
    listing = parse_locally_storefront(
        html,
        "https://trek.locally.com/product/80164/x?store=10001",
        watch,
    )

    assert listing is not None
    assert listing.sale_price_usd == Decimal("980.00")
    assert not listing.size_verified
    assert not listing.stock_verified
    assert listing.confidence == Confidence.LEAD
    assert not listing.price_match_ready


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params, headers))
        if url.endswith("/conversion/start"):
            return FakeResponse({"data": {"style": "79586", "upc": "197216460381"}})
        if url.endswith("/product/locate"):
            return FakeResponse(
                {
                    "data": {
                        "product": {
                            "upcs": [
                                {"code": "197216460381", "size_1": "M", "size_2": ""},
                                {"code": "197216460398", "size_1": "L", "size_2": ""},
                            ]
                        }
                    }
                }
            )
        return FakeResponse(
            {
                "total": 1,
                "results": [
                    {
                        "store_id": "10001",
                        "company_name": "Example Bikes",
                        "city": "Austin",
                        "state": "TX",
                        "in_stock_upcs": [
                            {
                                "upc": "197216460381",
                                "product_id": 79586,
                                "product_url": "/sapi/p/example",
                            }
                        ],
                    }
                ],
            }
        )


def test_official_api_resolves_product_id_and_filters_target_size(monkeypatch):
    monkeypatch.setenv("LOCALLY_API_TOKEN", "test-token")
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(x for x in config.watches if x.key == "fuel_ex_5_gen_7_79586")
    api = LocallyAPI(FakeHTTP(), config)

    stores = api.nationwide_stores(watch)

    assert len(stores) == 1
    assert stores[0].store_id == 10001
    assert stores[0].matching_upcs == ("197216460381",)
    assert stores[0].product_id == 79586
