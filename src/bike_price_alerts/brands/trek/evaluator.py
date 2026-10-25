from __future__ import annotations

import hashlib
from decimal import Decimal

from ...core.models import Alert, Confidence, Listing
from .models import WatchSpec
from .storage import Storage


def alert_key(alert: Alert) -> str:
    listing = alert.listing
    raw = "|".join(
        [
            alert.kind,
            listing.model_key,
            listing.size or "",
            str(listing.sale_price_usd),
            listing.retailer or "",
            listing.sku or "",
            listing.upc or "",
            listing.url,
        ]
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def evaluate(
    listing: Listing,
    watch: WatchSpec,
    storage: Storage,
    *,
    current_scan_id: int | None = None,
) -> Alert | None:
    price = listing.sale_price_usd
    if price is None or listing.in_stock is False:
        return None

    previous_verified = storage.previous_low(
        listing.model_key,
        size=listing.size if listing.size_verified else None,
        verified_only=True,
        exclude_scan_id=current_scan_id,
    )
    comparison = min(
        [x for x in (watch.baseline_usd, previous_verified) if x is not None],
        default=watch.baseline_usd,
    )

    if listing.confidence == Confidence.VERIFIED and price < comparison:
        return Alert(
            kind="verified_new_low",
            reason=f"Verified price ${price} is below comparison low ${comparison}",
            listing=listing,
            previous_low_usd=previous_verified,
            baseline_usd=watch.baseline_usd,
        )

    # A retailer-specific price can be valuable even when the server-rendered
    # page hides the selected size. Require a material improvement before
    # bothering the user with a phone-verification lead.
    material_lead_threshold = comparison * Decimal("0.95")
    if listing.confidence == Confidence.LEAD and price < material_lead_threshold:
        return Alert(
            kind="unverified_low_lead",
            reason=(
                f"Store-specific price ${price} is at least 5% below "
                f"${comparison}, but target size/stock needs confirmation"
            ),
            listing=listing,
            previous_low_usd=previous_verified,
            baseline_usd=watch.baseline_usd,
        )
    return None
