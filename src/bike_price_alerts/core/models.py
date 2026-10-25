from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Confidence(StrEnum):
    VERIFIED = "verified"
    STRONG = "strong"
    LEAD = "lead"
    REJECTED = "rejected"


@dataclass
class Listing:
    model_key: str
    canonical_name: str
    source: str
    url: str
    observed_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    title: str | None = None
    generation: str | None = None
    year: int | None = None
    size: str | None = None
    size_verified: bool = False
    in_stock: bool | None = None
    stock_verified: bool = False
    sale_price_usd: Decimal | None = None
    msrp_usd: Decimal | None = None
    retailer: str | None = None
    city: str | None = None
    state: str | None = None
    sku: str | None = None
    listing_id: str | None = None
    upc: str | None = None
    color: str | None = None
    product_id: int | None = None
    store_id: int | None = None
    confidence: Confidence = Confidence.LEAD
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def discount_pct(self) -> Decimal | None:
        if not self.msrp_usd or not self.sale_price_usd or self.msrp_usd <= 0:
            return None
        return ((self.msrp_usd - self.sale_price_usd) / self.msrp_usd * 100).quantize(
            Decimal("0.1")
        )

    @property
    def price_match_ready(self) -> bool:
        # A marketplace listing id is not a manufacturer SKU. Require a
        # manufacturer identifier before calling evidence price-match-ready.
        exact_item = bool(self.sku or self.upc)
        return bool(
            self.confidence == Confidence.VERIFIED
            and self.size_verified
            and self.stock_verified
            and self.in_stock
            and self.sale_price_usd is not None
            and self.retailer
            and exact_item
        )

    def fingerprint(self) -> str:
        payload = {
            "model_key": self.model_key,
            "source": self.source,
            "url": self.url,
            "size": self.size,
            "price": str(self.sale_price_usd),
            "retailer": self.retailer,
            "sku": self.sku,
            "listing_id": self.listing_id,
            "upc": self.upc,
            "in_stock": self.in_stock,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def as_json_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["confidence"] = self.confidence.value
        for key in ("sale_price_usd", "msrp_usd"):
            if result[key] is not None:
                result[key] = str(result[key])
        result["discount_pct"] = str(self.discount_pct) if self.discount_pct is not None else None
        result["price_match_ready"] = self.price_match_ready
        return result


@dataclass(frozen=True)
class Alert:
    kind: str
    reason: str
    listing: Listing
    previous_low_usd: Decimal | None
    baseline_usd: Decimal
