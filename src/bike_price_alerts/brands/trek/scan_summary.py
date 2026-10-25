"""Render a manual Trek scan as a short markdown table."""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from .storage import Storage

_SCAN_JSON = re.compile(r"-scan-(\d+)\.json$")
_SUMMARY_LIMIT = 20


def latest_report(report_dir: Path) -> tuple[Path, dict[str, Any]]:
    candidates: list[tuple[int, Path]] = []
    for path in report_dir.glob("*-scan-*.json"):
        match = _SCAN_JSON.search(path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        raise FileNotFoundError(f"No scan JSON found in {report_dir}")
    _, path = max(candidates)
    return path, json.loads(path.read_text(encoding="utf-8"))


def cheapest_offers(
    listings: list[dict[str, Any]],
    *,
    limit: int = _SUMMARY_LIMIT,
) -> list[dict[str, Any]]:
    verified = [
        item
        for item in listings
        if item.get("sale_price_usd") is not None
        and item.get("in_stock")
        and item.get("size_verified")
        and item.get("stock_verified")
    ]
    verified.sort(
        key=lambda item: (
            Decimal(str(item["sale_price_usd"])),
            str(item.get("retailer") or ""),
            str(item.get("url") or ""),
        )
    )
    return verified[:limit]


def _cell(value: object) -> str:
    text = "—" if value is None or str(value).strip() == "" else str(value).strip()
    return text.replace("|", "/").replace("\n", " ")


def _money(value: object) -> str:
    return f"${Decimal(str(value)):,.2f}"


def _location(item: dict[str, Any]) -> str:
    parts = [
        str(value).strip()
        for value in (item.get("city"), item.get("state"))
        if value and str(value).strip()
    ]
    return ", ".join(parts)


def render_scan_summary(
    *,
    model: str,
    size: str | None,
    color: str | None,
    profile: str,
    status: str,
    national_claim_allowed: bool,
    offers: list[dict[str, Any]],
    shown: int,
    total_verified: int,
) -> str:
    size_label = size.strip() if size and size.strip() else "any"
    color_label = color.strip() if color and color.strip() else "any"
    lines = [
        f"# Trek scan: {_cell(model)}",
        "",
        f"- Profile: `{_cell(profile)}`",
        f"- Status: **{_cell(status)}**",
        f"- Size: {_cell(size_label)}",
        f"- Color: {_cell(color_label)}",
        f"- Verified in-stock offers: {total_verified}",
        "",
    ]
    if national_claim_allowed:
        lines.append(
            "This table is the cheapest verified stock from a complete contiguous-US scan."
        )
    else:
        lines.append(
            "These are the cheapest verified offers inside this profile. "
            "A canary or regional run, or a partial scan, is not a nationwide low."
        )
    lines.extend(
        [
            "",
            "| Price | Store | City / State | Size | Color | Link |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    if not offers:
        lines.append("| — | — | — | — | — | No verified in-stock offers |")
    else:
        for item in offers:
            url = str(item.get("url") or "").strip()
            link = f"[open]({url})" if url else "—"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _money(item["sale_price_usd"]),
                        _cell(item.get("retailer")),
                        _cell(_location(item)),
                        _cell(item.get("size")),
                        _cell(item.get("color")),
                        link,
                    ]
                )
                + " |"
            )
    if total_verified > shown:
        lines.extend(["", f"Showing {shown} of {total_verified} verified offers, cheapest first."])
    lines.append("")
    return "\n".join(lines)


def build_summary(
    *,
    db: Path,
    report_dir: Path,
    model: str,
    size: str | None,
    color: str | None,
    profile: str,
) -> str:
    _json_path, report = latest_report(report_dir)
    scan_id = int(report["scan"]["scan_id"])
    with Storage(db) as storage:
        listings = storage.listings_for_scan(scan_id)
    verified = cheapest_offers(listings, limit=10**9)
    shown = cheapest_offers(verified, limit=_SUMMARY_LIMIT)
    return render_scan_summary(
        model=model,
        size=size,
        color=color,
        profile=str(report.get("scan", {}).get("profile") or profile),
        status=str(report.get("status") or ""),
        national_claim_allowed=bool(report.get("national_claim_allowed")),
        offers=shown,
        shown=len(shown),
        total_verified=len(verified),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the manual Trek scan summary")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--size", default="")
    parser.add_argument("--color", default="")
    parser.add_argument("--profile", default="conus")
    args = parser.parse_args(argv)
    summary = build_summary(
        db=args.db,
        report_dir=args.report_dir,
        model=args.model,
        size=args.size or None,
        color=args.color or None,
        profile=args.profile,
    )
    print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
