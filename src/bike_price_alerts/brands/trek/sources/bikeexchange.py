from __future__ import annotations

import re
from decimal import Decimal

from ....core.models import Confidence, Listing
from ....core.parsing import (
    city_state_from_text,
    infer_generation,
    infer_year,
    matching_size,
    money_values,
    section,
    soup_text,
    title_matches,
)
from ..models import WatchSpec


def _embedded_offer(html: str, listing_id: str | None) -> dict[str, str | Decimal | None]:
    """Parse the narrow, server-rendered Next.js actionBlock contract."""
    if not listing_id:
        return {}
    match = re.search(rf'\\"productSku\\":\\"{re.escape(listing_id)}\\"', html)
    if not match:
        return {}
    payload = html[match.start() : match.start() + 3000]
    barcode = re.search(r'\\"barcode\\":\\"(\d{12,14})\\"', payload)
    regular = re.search(r'\\"price\\":\{\\"amount\\":(\d+),\\"currency\\":\\"USD\\"\}', payload)
    reduced = re.search(
        r'\\"reducedPrice\\":\{\\"amount\\":(\d+),\\"currency\\":\\"USD\\"\}',
        payload,
    )
    if not barcode or not regular:
        return {}
    regular_price = Decimal(regular.group(1)) / Decimal(100)
    reduced_price = Decimal(reduced.group(1)) / Decimal(100) if reduced else None
    return {
        "upc": barcode.group(1),
        "sale_price": reduced_price or regular_price,
        "msrp": regular_price if reduced_price else None,
    }


def parse_bikeexchange(html: str, url: str, watch: WatchSpec) -> Listing | None:
    document, text = soup_text(html)
    title = document.h1 or watch.canonical_name
    if not title_matches(title, watch.aliases, watch.exclusions):
        return None

    offer_area = section(text, title, ("About the Seller", "Description"))
    prices = money_values(offer_area)

    size_area = section(text, "Sizes", ("Loading", "Add to Cart", "About the Seller"))
    available_sizes = [
        x.strip()
        for x in re.findall(r"(?:\b\d{2}\s*cm\b|\bM[/ -]?L\b|\bML\b)", size_area, re.IGNORECASE)
    ]
    selected_size = matching_size(available_sizes, watch.allowed_sizes)
    size_verified = selected_size is not None

    seller_area = section(text, "About the Seller", ("Questions and Answers", "Description"))
    seller_lines = [line.strip() for line in seller_area.splitlines() if line.strip()]
    retailer = seller_lines[0] if seller_lines else None
    city, state = city_state_from_text(seller_area)

    color_area = section(text, "Colors", ("Sizes",))
    colors = [x.strip() for x in color_area.splitlines() if x.strip()]
    color = colors[0] if colors else None

    sku_match = re.search(r"[?&]sku=([^&#]+)", url, re.IGNORECASE)
    listing_id = sku_match.group(1) if sku_match else None
    structured = _embedded_offer(html, listing_id)
    if structured:
        msrp = structured["msrp"]
        sale = structured["sale_price"]
    elif len(prices) >= 2:
        msrp, sale = prices[0], prices[1]
    elif len(prices) == 1:
        msrp, sale = None, prices[0]
    else:
        msrp, sale = None, None
    has_add_to_cart = "add to cart" in text.lower()
    in_stock = bool(size_verified and has_add_to_cart)
    stock_verified = in_stock
    confidence = (
        Confidence.VERIFIED
        if sale is not None and size_verified and stock_verified and retailer
        else Confidence.LEAD
    )

    listing = Listing(
        model_key=watch.key,
        canonical_name=watch.canonical_name,
        source="bikeexchange",
        url=url,
        title=title,
        generation=infer_generation(title),
        year=infer_year(text[:1000]),
        size=selected_size,
        size_verified=size_verified,
        in_stock=in_stock,
        stock_verified=stock_verified,
        sale_price_usd=sale,
        msrp_usd=msrp,
        retailer=retailer,
        city=city,
        state=state,
        listing_id=listing_id,
        upc=str(structured["upc"]) if structured else None,
        color=color,
        confidence=confidence,
        evidence={
            "available_sizes": available_sizes,
            "price_text": [str(x) for x in prices],
            "add_to_cart": has_add_to_cart,
            "structured_offer": bool(structured),
            "parser": "bikeexchange-v2",
        },
    )
    return listing
