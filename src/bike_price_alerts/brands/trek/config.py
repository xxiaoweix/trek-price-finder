from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .models import (
    AppConfig,
    LocallySpec,
    LocallyVariant,
    LocallyWidgetPoint,
    PublicURL,
    ScanProfile,
    WatchSpec,
)


class ConfigError(ValueError):
    pass


def _required(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing required config key: {key}")
    return mapping[key]


def _positive_decimal(value: Any, *, key: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ConfigError(f"{key} must be a decimal number") from exc
    if not result.is_finite() or result <= 0:
        raise ConfigError(f"{key} must be greater than zero")
    return result


def _public_url(item: Any, *, watch_key: str) -> PublicURL:
    if not isinstance(item, dict):
        raise ConfigError(f"{watch_key}.public_urls entries must be mappings")
    source = str(_required(item, "source"))
    if source not in {"bikeexchange", "locally_storefront"}:
        raise ConfigError(f"{watch_key}: unsupported source {source!r}")
    url = str(_required(item, "url"))
    parsed = urlparse(url)
    expected_host = {
        "bikeexchange": "www.bikeexchange.com",
        "locally_storefront": "trek.locally.com",
    }[source]
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        raise ConfigError(f"{watch_key}: {source} URL must use https://{expected_host}")
    return PublicURL(source=source, url=url)


def _widget_point(item: Any) -> LocallyWidgetPoint:
    if not isinstance(item, dict):
        raise ConfigError("locally.widget_points entries must be mappings")
    label = str(_required(item, "label")).strip()
    if not label:
        raise ConfigError("locally.widget_points label must not be empty")
    lat = float(_required(item, "lat"))
    lng = float(_required(item, "lng"))
    distance_diag = int(item.get("distance_diag", 100))
    if not -90 <= lat <= 90:
        raise ConfigError(f"{label}: widget latitude must be between -90 and 90")
    if not -180 <= lng <= 180:
        raise ConfigError(f"{label}: widget longitude must be between -180 and 180")
    if not 15 <= distance_diag <= 200:
        raise ConfigError(f"{label}: widget distance_diag must be between 15 and 200")
    return LocallyWidgetPoint(
        label=label,
        lat=lat,
        lng=lng,
        distance_diag=distance_diag,
    )


def _locally_variant(item: Any, *, watch_key: str) -> LocallyVariant:
    if not isinstance(item, dict):
        raise ConfigError(f"{watch_key}.locally.variants entries must be mappings")
    upc = str(_required(item, "upc")).strip()
    if not upc.isdigit() or len(upc) not in {12, 13, 14}:
        raise ConfigError(f"{watch_key}: variant UPC must contain 12-14 digits")
    product_id = int(_required(item, "product_id"))
    if product_id <= 0:
        raise ConfigError(f"{watch_key}: variant product_id must be positive")
    style = str(_required(item, "style")).strip()
    size = str(_required(item, "size")).strip()
    if not style or not size:
        raise ConfigError(f"{watch_key}: variant style and size must not be empty")
    color = str(item["color"]).strip(": ") if item.get("color") else None
    mpn = str(item["mpn"]).strip() if item.get("mpn") else None
    return LocallyVariant(
        upc=upc,
        product_id=product_id,
        style=style,
        size=size,
        color=color or None,
        mpn=mpn or None,
    )


def _scan_profile(name: str, item: Any) -> ScanProfile:
    if not isinstance(item, dict):
        raise ConfigError(f"scan.profiles.{name} must be a mapping")
    coverage = str(item.get("coverage", "points"))
    if coverage not in {"points", "conus"}:
        raise ConfigError(f"scan.profiles.{name}.coverage must be points or conus")
    points = tuple(_widget_point(point) for point in item.get("points", []))
    if coverage == "points" and not points:
        raise ConfigError(f"scan.profiles.{name} must configure at least one point")
    initial = int(item.get("initial_distance_diag", 160))
    minimum = int(item.get("minimum_distance_diag", 40))
    max_depth = int(item.get("max_depth", 2))
    overlap = float(item.get("overlap", 0.30))
    if not 40 <= initial <= 400:
        raise ConfigError(
            f"scan.profiles.{name}.initial_distance_diag must be between 40 and 400"
        )
    if not 15 <= minimum <= initial:
        raise ConfigError(
            f"scan.profiles.{name}.minimum_distance_diag must be between 15 and initial"
        )
    if not 0 <= max_depth <= 4:
        raise ConfigError(f"scan.profiles.{name}.max_depth must be between 0 and 4")
    if not 0 <= overlap <= 0.50:
        raise ConfigError(f"scan.profiles.{name}.overlap must be between 0 and 0.50")
    return ScanProfile(
        name=name,
        coverage=coverage,
        points=points,
        initial_distance_diag=initial,
        minimum_distance_diag=minimum,
        max_depth=max_depth,
        overlap=overlap,
    )


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Top-level configuration must be a mapping")
    if raw.get("version") != 2:
        raise ConfigError("Only config version 2 is supported")

    locally_raw = _required(raw, "locally")
    if not isinstance(locally_raw, dict):
        raise ConfigError("locally must be a mapping")
    raw_watches = _required(raw, "watches")
    if not isinstance(raw_watches, list) or not raw_watches:
        raise ConfigError("watches must be a non-empty list")
    watches: list[WatchSpec] = []
    seen: set[str] = set()

    for item in raw_watches:
        if not isinstance(item, dict):
            raise ConfigError("Each watch must be a mapping")
        key = str(_required(item, "key"))
        if key in seen:
            raise ConfigError(f"Duplicate watch key: {key}")
        seen.add(key)

        loc = item.get("locally") or {}
        aliases = tuple(str(x).strip() for x in _required(item, "aliases") if str(x).strip())
        allowed_sizes = tuple(
            str(x).strip() for x in _required(item, "allowed_sizes") if str(x).strip()
        )
        if not aliases:
            raise ConfigError(f"{key}: aliases must not be empty")
        if not allowed_sizes:
            raise ConfigError(f"{key}: allowed_sizes must not be empty")
        public = tuple(_public_url(x, watch_key=key) for x in item.get("public_urls", []))
        variants = tuple(
            _locally_variant(variant, watch_key=key) for variant in loc.get("variants", [])
        )
        if not public and not variants:
            raise ConfigError(f"{key}: configure at least one public URL or Locally identifier")
        duplicate_upcs = {
            variant.upc for variant in variants if sum(v.upc == variant.upc for v in variants) > 1
        }
        if duplicate_upcs:
            raise ConfigError(f"{key}: duplicate variant UPCs: {sorted(duplicate_upcs)}")
        allowed_normalized = {
            value.upper().replace(" ", "").replace("CM", "").replace("-", "/")
            for value in allowed_sizes
        }
        for variant in variants:
            normalized = variant.size.upper().replace(" ", "").replace("CM", "").replace("-", "/")
            if normalized == "ML":
                normalized = "M/L"
            if normalized not in allowed_normalized:
                raise ConfigError(
                    f"{key}: variant {variant.upc} size {variant.size!r} is not allowed"
                )
        watches.append(
            WatchSpec(
                key=key,
                canonical_name=str(_required(item, "canonical_name")),
                aliases=aliases,
                exclusions=tuple(str(x) for x in item.get("exclusions", [])),
                allowed_sizes=allowed_sizes,
                baseline_usd=_positive_decimal(
                    _required(item, "baseline_usd"), key=f"{key}.baseline_usd"
                ),
                msrp_usd=(
                    _positive_decimal(item["msrp_usd"], key=f"{key}.msrp_usd")
                    if item.get("msrp_usd") is not None
                    else None
                ),
                locally=LocallySpec(
                    variants=variants,
                    seed_store_ids=tuple(int(x) for x in loc.get("seed_store_ids", [])),
                ),
                public_urls=public,
            )
        )

    page_limit = int(locally_raw.get("page_limit", 100))
    if not 1 <= page_limit <= 100:
        raise ConfigError("locally.page_limit must be between 1 and 100")
    radius_miles = int(_required(locally_raw, "radius_miles"))
    if not 1 <= radius_miles <= 2500:
        raise ConfigError("locally.radius_miles must be between 1 and 2500")
    scan_raw = _required(raw, "scan")
    if not isinstance(scan_raw, dict):
        raise ConfigError("scan must be a mapping")
    profiles_raw = _required(scan_raw, "profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise ConfigError("scan.profiles must be a non-empty mapping")
    profiles = tuple(_scan_profile(str(name), value) for name, value in profiles_raw.items())
    default_profile = str(_required(scan_raw, "default_profile"))
    if default_profile not in {profile.name for profile in profiles}:
        raise ConfigError("scan.default_profile must reference a configured profile")
    return AppConfig(
        version=2,
        locally_company_id=int(_required(locally_raw, "company_id")),
        locally_center_lat=float(_required(locally_raw, "center_lat")),
        locally_center_lng=float(_required(locally_raw, "center_lng")),
        locally_radius_miles=radius_miles,
        locally_page_limit=page_limit,
        watches=tuple(watches),
        default_scan_profile=default_profile,
        scan_profiles=profiles,
    )
