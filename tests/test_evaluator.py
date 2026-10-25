from decimal import Decimal

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.evaluator import evaluate
from bike_price_alerts.brands.trek.storage import Storage
from bike_price_alerts.core.models import Confidence, Listing


def test_verified_new_low_alert(tmp_path):
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(x for x in config.watches if x.key == "fuel_ex_5_gen_7_79586")
    listing = Listing(
        model_key=watch.key,
        canonical_name=watch.canonical_name,
        source="test",
        url="https://example.test/item",
        title=watch.canonical_name,
        generation="Gen 7",
        year=2026,
        size="M",
        size_verified=True,
        in_stock=True,
        stock_verified=True,
        sale_price_usd=Decimal("800.00"),
        msrp_usd=Decimal("2000.00"),
        retailer="Example Bikes",
        sku="SKU-1",
        confidence=Confidence.VERIFIED,
    )
    with Storage(tmp_path / "state.sqlite3") as storage:
        alert = evaluate(listing, watch, storage)
    assert alert is not None
    assert alert.kind == "verified_new_low"


def test_ambiguous_price_requires_material_discount(tmp_path):
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(x for x in config.watches if x.key == "domane_al_2_gen_4_80164")
    listing = Listing(
        model_key=watch.key,
        canonical_name=watch.canonical_name,
        source="test",
        url="https://example.test/item",
        title=watch.canonical_name,
        in_stock=True,
        sale_price_usd=Decimal("980.00"),
        retailer="Example Bikes",
        confidence=Confidence.LEAD,
    )
    with Storage(tmp_path / "state.sqlite3") as storage:
        alert = evaluate(listing, watch, storage)
    assert alert is None
