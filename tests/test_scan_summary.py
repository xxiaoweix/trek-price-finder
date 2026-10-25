from decimal import Decimal

from bike_price_alerts.brands.trek.scan_summary import (
    cheapest_offers,
    render_scan_summary,
)


def _offer(**overrides):
    item = {
        "sale_price_usd": "800.00",
        "in_stock": True,
        "size_verified": True,
        "stock_verified": True,
        "retailer": "Example Bikes",
        "city": "Austin",
        "state": "TX",
        "size": "M",
        "color": "Miami Green",
        "url": "https://trek.locally.com/product/57363?store=10001",
    }
    item.update(overrides)
    return item


def test_cheapest_offers_keep_verified_stock_in_price_order():
    offers = cheapest_offers(
        [
            _offer(sale_price_usd="3000.00", retailer="Higher"),
            _offer(sale_price_usd="1000.00", in_stock=False, retailer="Gone"),
            _offer(sale_price_usd="2000.00", size_verified=False, retailer="Unsized"),
            _offer(sale_price_usd="800.00", retailer="Example Bikes"),
        ],
        limit=10,
    )

    assert [item["retailer"] for item in offers] == ["Example Bikes", "Higher"]
    assert Decimal(offers[0]["sale_price_usd"]) < Decimal(offers[1]["sale_price_usd"])


def test_summary_table_names_the_cheapest_store_and_refuses_a_national_claim():
    text = render_scan_summary(
        model="Marlin 5",
        size="M",
        color=None,
        profile="canary",
        status="COMPLETE",
        national_claim_allowed=False,
        offers=[_offer(retailer="Shop | North")],
        shown=1,
        total_verified=1,
    )

    assert "# Trek scan: Marlin 5" in text
    assert "not a nationwide low" in text
    assert "| $800.00 | Shop / North | Austin, TX | M | Miami Green | [open](" in text
    assert "Color: any" in text
