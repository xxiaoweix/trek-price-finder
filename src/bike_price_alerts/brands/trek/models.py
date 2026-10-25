"""Trek-only configuration and Locally identity models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PublicURL:
    source: str
    url: str


@dataclass(frozen=True)
class LocallyVariant:
    upc: str
    product_id: int
    style: str
    size: str
    color: str | None = None
    mpn: str | None = None


@dataclass(frozen=True)
class LocallySpec:
    variants: tuple[LocallyVariant, ...] = ()
    seed_store_ids: tuple[int, ...] = ()

    @property
    def product_ids(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(variant.product_id for variant in self.variants))

    @property
    def styles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(variant.style for variant in self.variants))

    @property
    def upcs(self) -> tuple[str, ...]:
        return tuple(variant.upc for variant in self.variants)


@dataclass(frozen=True)
class LocallyWidgetPoint:
    label: str
    lat: float
    lng: float
    distance_diag: int = 100


@dataclass(frozen=True)
class ScanProfile:
    name: str
    coverage: str
    points: tuple[LocallyWidgetPoint, ...] = ()
    initial_distance_diag: int = 160
    minimum_distance_diag: int = 40
    max_depth: int = 2
    overlap: float = 0.30


@dataclass(frozen=True)
class WatchSpec:
    key: str
    canonical_name: str
    aliases: tuple[str, ...]
    exclusions: tuple[str, ...]
    allowed_sizes: tuple[str, ...]
    baseline_usd: Decimal
    msrp_usd: Decimal | None
    locally: LocallySpec
    public_urls: tuple[PublicURL, ...]


@dataclass(frozen=True)
class AppConfig:
    version: int
    locally_company_id: int
    locally_center_lat: float
    locally_center_lng: float
    locally_radius_miles: int
    locally_page_limit: int
    watches: tuple[WatchSpec, ...]
    default_scan_profile: str
    scan_profiles: tuple[ScanProfile, ...]

    def scan_profile(self, name: str | None = None) -> ScanProfile:
        selected = name or self.default_scan_profile
        try:
            return next(profile for profile in self.scan_profiles if profile.name == selected)
        except StopIteration as exc:
            raise KeyError(f"Unknown scan profile: {selected}") from exc
