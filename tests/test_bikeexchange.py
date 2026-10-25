from decimal import Decimal

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.sources.bikeexchange import parse_bikeexchange
from bike_price_alerts.core.models import Confidence


def test_bikeexchange_exact_size_is_verified(fixture_dir):
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(x for x in config.watches if x.key == "domane_al_2_gen_4_80164")
    html = (fixture_dir / "bikeexchange_domane_al_2.html").read_text()
    url = (
        "https://www.bikeexchange.com/en-US/products/"
        "trek-domane-al-2-gen-4?sku=EXAMPLE52"
    )
    listing = parse_bikeexchange(html, url, watch)

    assert listing is not None
    assert listing.sale_price_usd == Decimal("960.00")
    assert listing.msrp_usd == Decimal("1200.00")
    assert listing.size == "52 cm"
    assert listing.retailer == "Example Bikes"
    assert listing.city == "Austin"
    assert listing.state == "TX"
    assert listing.confidence == Confidence.VERIFIED
    assert not listing.price_match_ready


def test_embedded_offer_supplies_usd_price_and_upc(fixture_dir):
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(x for x in config.watches if x.key == "domane_al_2_gen_4_80164")
    html = (fixture_dir / "bikeexchange_domane_al_2.html").read_text()
    html += (
        r"\"actionBlock\":{\"productSku\":\"EXAMPLE52\","
        r"\"barcode\":\"197216436744\",\"price\":{\"amount\":120000,"
        r"\"currency\":\"USD\"},\"reducedPrice\":{\"amount\":96000,"
        r"\"currency\":\"USD\"}}"
    )
    listing = parse_bikeexchange(
        html,
        "https://www.bikeexchange.com/en-US/products/item?sku=EXAMPLE52",
        watch,
    )
    assert listing is not None
    assert listing.sale_price_usd == Decimal("960.00")
    assert listing.msrp_usd == Decimal(1200)
    assert listing.upc == "197216436744"
    assert listing.price_match_ready
