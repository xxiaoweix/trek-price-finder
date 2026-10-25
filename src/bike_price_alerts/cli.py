from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .brands.trek.config import load_config
from .brands.trek.framework import (
    ModelNotFound,
    config_for_watches,
    lowest_offer,
    resolve_watches,
)
from .brands.trek.geography import seed_cells
from .brands.trek.models import AppConfig
from .brands.trek.price_board import (
    build_dataset,
    load_upc_lows,
    merge_upc_lows,
    price_board_gate,
    write_dataset,
)
from .brands.trek.product_scan import load_top_seller_watches, scan_top_sellers
from .brands.trek.reporting import generate_daily_report
from .brands.trek.scanner import scan_widget_coverage
from .brands.trek.storage import Storage
from .brands.trek.store_finder import TREK_US_DIRECTORY_URL, audit_us_store_directory
from .core.http import HTTPClient

PROFILES = Path("examples/trek.yaml")


def _enabled() -> bool:
    return os.getenv("TRACKER_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _official_store_audit_enabled() -> bool:
    return os.getenv(
        "TRACKER_OFFICIAL_STORE_AUDIT_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}


def _config_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_official_store_audit(
    http: HTTPClient,
    storage: Storage,
    *,
    scan_id: int,
    force: bool = False,
) -> dict[str, object]:
    existing = storage.store_catalog_summary(scan_id)
    if (
        not force
        and existing["audit_status"] == "complete"
        and existing["audit_scan_id"] == scan_id
    ):
        return existing
    audit_started_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        audit = audit_us_store_directory(
            http,
            progress=lambda message: print(message, flush=True),
        )
    except Exception as exc:
        storage.save_store_catalog_audit(
            scan_id=scan_id,
            source_url=TREK_US_DIRECTORY_URL,
            started_at=audit_started_at,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    storage.save_store_catalog_audit(
        scan_id=scan_id,
        source_url=audit.source_url,
        started_at=audit_started_at,
        status="complete",
        page_count=audit.page_count,
        official_us_store_count=audit.official_us_store_count,
        official_conus_store_count=audit.official_conus_store_count,
    )
    return storage.store_catalog_summary(scan_id)


def scan_command(
    *,
    config: Path,
    db: Path,
    profile: str | None,
    watch_keys: set[str] | None,
    resume: bool,
    max_cells: int | None,
    report_dir: Path,
    emit_json: bool,
    fail_on_partial: bool,
    workers: int | None = None,
    settings: AppConfig | None = None,
    config_hash: str | None = None,
) -> int:
    if not _enabled():
        print("KILL SWITCH: TRACKER_ENABLED is false; no network requests were made.")
        return 0
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    settings = settings or load_config(config)
    selected_profile = settings.scan_profile(profile)
    digest = config_hash or _config_hash(config)

    with HTTPClient() as http, Storage(db) as storage:
        scan_id = (
            storage.resumable_scan(profile=selected_profile.name, config_hash=digest)
            if resume
            else None
        )
        if scan_id is None:
            scan_id = storage.start_scan(
                started_at=started_at,
                profile=selected_profile.name,
                config_hash=digest,
            )
            print(f"Started scan {scan_id} with profile {selected_profile.name}.")
        else:
            storage.recover_scan(scan_id)
            print(f"Resuming scan {scan_id} with profile {selected_profile.name}.")

        result = scan_widget_coverage(
            settings,
            http,
            storage,
            scan_id=scan_id,
            profile_name=selected_profile.name,
            watch_keys=watch_keys,
            max_cells=max_cells,
            workers=workers,
            progress=lambda message: print(message, flush=True),
        )
        if selected_profile.coverage == "conus" and _official_store_audit_enabled():
            try:
                _run_official_store_audit(http, storage, scan_id=scan_id)
            except Exception as exc:
                result.notes.append(
                    "official Trek store count unavailable; price scan remains valid: "
                    f"{type(exc).__name__}: {exc}"
                )
        storage.finish_scan(scan_id, errors=result.errors, notes=result.notes)
        report = generate_daily_report(
            storage,
            settings,
            scan_id=scan_id,
            output_dir=report_dir,
        )
        if emit_json:
            print(json.dumps(report.payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                f"Scan {scan_id}: {storage.scan_listing_count(scan_id)} verified offers; "
                f"status {report.status}; "
                f"{len(result.errors)} errors."
            )
            print(f"Markdown report: {report.markdown_path}")
            print(f"JSON report: {report.json_path}")
        for note in result.notes:
            print(f"NOTE: {note}")
        for error in result.errors:
            print(f"ERROR: {error}")
        return 2 if fail_on_partial and report.status != "COMPLETE" else 0


def model_scan_command(
    *,
    model: str,
    size: str | None,
    color: str | None,
    config: Path,
    db: Path,
    profile: str | None,
    resume: bool,
    max_cells: int | None,
    report_dir: Path,
    workers: int | None,
    fail_on_partial: bool,
) -> int:
    """Scan one Trek model across the selected US profile and print the low price."""

    if not _enabled():
        print("KILL SWITCH: TRACKER_ENABLED is false; no network requests were made.")
        return 0
    base = load_config(config)
    with HTTPClient() as http:
        try:
            watches = resolve_watches(base, http, model=model, size=size, color=color)
        except ModelNotFound as exc:
            print(f"ERROR: {exc}")
            return 2
    runtime = config_for_watches(base, watches)
    identity = "|".join(
        f"{watch.key}:{variant.upc}"
        for watch in watches
        for variant in watch.locally.variants
    )
    digest = hashlib.sha256(
        f"{profile or runtime.default_scan_profile}|{identity}".encode()
    ).hexdigest()
    exit_code = scan_command(
        config=config,
        db=db,
        profile=profile,
        watch_keys=None,
        resume=resume,
        max_cells=max_cells,
        report_dir=report_dir,
        emit_json=False,
        fail_on_partial=fail_on_partial,
        workers=workers,
        settings=runtime,
        config_hash=digest,
    )
    with Storage(db) as storage:
        status = storage.status()
        last_run = status["last_run"]
        listings = (
            storage.listings_for_scan(int(last_run["scan_id"]))
            if last_run
            else []
        )
        summary = (
            storage.coverage_summary(int(last_run["scan_id"]))
            if last_run
            else {}
        )
    offer = lowest_offer(listings)
    payload = {
        "query": {"model": model, "size": size, "color": color, "profile": profile},
        "watches": [watch.canonical_name for watch in watches],
        "variant_count": sum(len(watch.locally.variants) for watch in watches),
        "coverage_status": None if not last_run else last_run["coverage_status"],
        "coverage_complete": bool(summary.get("complete")) if summary else False,
        "lowest": None if offer is None else offer.as_dict(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def price_board_command(
    *,
    config: Path,
    db: Path,
    profile: str | None,
    watch_keys: set[str] | None,
    resume: bool,
    max_cells: int | None,
    report_dir: Path,
    out: Path,
    workers: int | None,
    fail_on_partial: bool,
) -> int:
    """Scan a product catalog and write the JSON file the static page reads."""

    if not _enabled():
        print("KILL SWITCH: TRACKER_ENABLED is false; no network requests were made.")
        return 0
    base = load_config(PROFILES)
    watches = load_top_seller_watches(config)
    if watch_keys:
        selected = {watch.key for watch in watches}
        missing = watch_keys - selected
        if missing:
            raise ValueError(f"Unknown top-seller keys: {', '.join(sorted(missing))}")
        watches = tuple(watch for watch in watches if watch.key in watch_keys)
    settings = config_for_watches(base, watches)
    selected_profile = settings.scan_profile(profile)
    identity = "|".join(
        f"{watch.key}:{variant.upc}" for watch in watches for variant in watch.locally.variants
    )
    digest = hashlib.sha256(f"{selected_profile.name}|{identity}".encode()).hexdigest()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    with HTTPClient() as http, Storage(db) as storage:
        scan_id = (
            storage.resumable_scan(profile=selected_profile.name, config_hash=digest)
            if resume
            else None
        )
        if scan_id is None:
            scan_id = storage.start_scan(
                started_at=started_at,
                profile=selected_profile.name,
                config_hash=digest,
            )
            print(f"Started scan {scan_id} with profile {selected_profile.name}.")
        else:
            storage.recover_scan(scan_id)
            print(f"Resuming scan {scan_id} with profile {selected_profile.name}.")
        stats = scan_top_sellers(
            settings,
            http,
            storage,
            scan_id=scan_id,
            profile_name=selected_profile.name,
            max_cells=max_cells,
            workers=workers,
            progress=lambda message: print(message, flush=True),
        )
        storage.finish_scan(scan_id, errors=stats.result.errors, notes=stats.result.notes)
        report = generate_daily_report(
            storage,
            settings,
            scan_id=scan_id,
            output_dir=report_dir,
        )
        listings = storage.listings_for_scan(scan_id)
        scan_row = report.payload.get("scan") or {}
        updated_at = str(scan_row.get("finished_at") or started_at)
        scanned_profile = selected_profile.name
        blockers, skipped = price_board_gate(storage, scan_id, profile=scanned_profile)
    previous = load_upc_lows(out)
    publishable = not blockers
    draft = build_dataset(
        listings,
        updated_at=updated_at,
        profile=scanned_profile,
        complete=publishable,
        skipped_offers=skipped,
    )
    lows = merge_upc_lows(previous, draft["offers"], updated_at)
    dataset = build_dataset(
        listings,
        updated_at=updated_at,
        profile=scanned_profile,
        complete=publishable,
        upc_lows=lows,
        requests={
            "discovery": stats.discovery_requests,
            "inventory": stats.inventory_requests,
            "price": stats.price_requests,
            "total": stats.request_count,
        },
        skipped_offers=skipped,
    )
    write_dataset(out, dataset)
    print(
        f"Wrote {dataset['offer_count']} offers to {out} "
        f"({stats.request_count} Locally requests: "
        f"{stats.discovery_requests} map, "
        f"{stats.inventory_requests} store, "
        f"{stats.price_requests} price). "
        f"status {report.status}; "
        f"publish {'yes' if publishable else 'no'}; "
        f"skipped {len(skipped)}."
    )
    for note in stats.result.notes:
        print(f"NOTE: {note}")
    for error in stats.result.errors:
        print(f"ERROR: {error}")
    for blocker in blockers:
        print(f"BLOCK: {blocker}")
    return 2 if fail_on_partial and blockers else 0


def audit_stores_command(
    *,
    config: Path,
    db: Path,
    scan_id: int | None,
    report_dir: Path,
) -> int:
    settings = load_config(config)
    with HTTPClient() as http, Storage(db) as storage:
        if scan_id is None:
            status = storage.status()
            last_run = status["last_run"]
            if not last_run:
                raise RuntimeError("No scan exists; run scan first")
            scan_id = int(last_run["scan_id"])
        try:
            summary = _run_official_store_audit(
                http,
                storage,
                scan_id=scan_id,
                force=True,
            )
        except Exception as exc:
            print(f"ERROR: official Trek store audit failed: {type(exc).__name__}: {exc}")
            return 2
        report = generate_daily_report(
            storage,
            settings,
            scan_id=scan_id,
            output_dir=report_dir,
        )
        print(
            f"Trek official CONUS stores: "
            f"{summary['official_conus_store_count']}; "
            f"Locally scannable stores: "
            f"{report.payload['store_counts']['locally_scannable_store_count']}"
        )
        print(f"Markdown report: {report.markdown_path}")
        print(f"JSON report: {report.json_path}")
        return 0


def report_command(
    *,
    config: Path,
    db: Path,
    scan_id: int | None,
    report_dir: Path,
) -> int:
    settings = load_config(config)
    with Storage(db) as storage:
        if scan_id is None:
            status = storage.status()
            last_run = status["last_run"]
            if not last_run:
                raise RuntimeError("No scan exists; run scan first")
            scan_id = int(last_run["scan_id"])
        report = generate_daily_report(
            storage,
            settings,
            scan_id=scan_id,
            output_dir=report_dir,
        )
        print(report.markdown_path)
        return 0


def validate_config_command(config: Path) -> int:
    settings = load_config(config)
    print(f"Config version: {settings.version}")
    for watch in settings.watches:
        print(
            f"{watch.key}: {len(watch.locally.variants)} exact variants; "
            f"sizes={','.join(watch.allowed_sizes)}"
        )
    for profile in settings.scan_profiles:
        print(
            f"profile {profile.name}: coverage={profile.coverage}; "
            f"seed_cells={len(seed_cells(profile))}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bike-price-alerts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "trek-scan",
        help="Scan every bike listed in a config file",
    )
    scan_parser.add_argument("--config", type=Path, default=PROFILES)
    scan_parser.add_argument("--db", type=Path, default=Path("data/tracker.sqlite3"))
    scan_parser.add_argument("--profile", help="canary, regional, or conus")
    scan_parser.add_argument(
        "--watch",
        action="append",
        dest="watch_keys",
        help="Restrict to one config key; repeat for more than one",
    )
    scan_parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume an interrupted scan with the same config and profile (default: true)",
    )
    scan_parser.add_argument(
        "--max-cells",
        type=int,
        help="Stop after this many map cells. Useful for a short test.",
    )
    scan_parser.add_argument("--report-dir", type=Path, default=Path("reports/trek"))
    scan_parser.add_argument("--emit-json", action="store_true")
    scan_parser.add_argument(
        "--fail-on-partial",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exit 2 when coverage is incomplete (default: true)",
    )
    scan_parser.add_argument(
        "--workers",
        type=int,
        help="Parallel requests (default: TRACKER_SCAN_WORKERS or 6)",
    )

    model_parser = subparsers.add_parser(
        "scan",
        help="Scan one Trek model and print the cheapest in-stock US price",
    )
    model_parser.add_argument("--model", required=True, help='Example: "Marlin 5 Gen 3"')
    model_parser.add_argument("--size", help="Optional size, such as 56, M, or ML")
    model_parser.add_argument("--color", help="Optional exact color name")
    model_parser.add_argument("--config", type=Path, default=PROFILES)
    model_parser.add_argument("--db", type=Path, default=Path("data/scan.sqlite3"))
    model_parser.add_argument(
        "--profile",
        default="conus",
        help="canary, regional, or conus (default: conus)",
    )
    model_parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    model_parser.add_argument("--max-cells", type=int)
    model_parser.add_argument("--report-dir", type=Path, default=Path("reports/scan"))
    model_parser.add_argument("--workers", type=int)
    model_parser.add_argument(
        "--fail-on-partial",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    board_parser = subparsers.add_parser(
        "price-board",
        help="Scan a catalog file and write site/data/prices.json",
    )
    board_parser.add_argument("--config", type=Path, default=Path("config/trek/top-sellers.yaml"))
    board_parser.add_argument("--db", type=Path, default=Path("data/price-board.sqlite3"))
    board_parser.add_argument("--profile", default="canary")
    board_parser.add_argument(
        "--watch",
        action="append",
        dest="watch_keys",
        help="Scan one catalog key; repeat to scan several",
    )
    board_parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=False)
    board_parser.add_argument("--max-cells", type=int)
    board_parser.add_argument("--report-dir", type=Path, default=Path("reports/price-board"))
    board_parser.add_argument("--out", type=Path, default=Path("site/data/prices.json"))
    board_parser.add_argument("--workers", type=int)
    board_parser.add_argument(
        "--fail-on-partial",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    report_parser = subparsers.add_parser(
        "trek-report",
        help="Regenerate the markdown and JSON report for a saved scan",
    )
    report_parser.add_argument("--config", type=Path, default=PROFILES)
    report_parser.add_argument("--db", type=Path, default=Path("data/tracker.sqlite3"))
    report_parser.add_argument("--scan-id", type=int)
    report_parser.add_argument("--report-dir", type=Path, default=Path("reports/trek"))

    audit_parser = subparsers.add_parser(
        "trek-audit-stores",
        help="Count official Trek US stores and rebuild a report",
    )
    audit_parser.add_argument("--config", type=Path, default=PROFILES)
    audit_parser.add_argument("--db", type=Path, default=Path("data/tracker.sqlite3"))
    audit_parser.add_argument("--scan-id", type=int)
    audit_parser.add_argument("--report-dir", type=Path, default=Path("reports/trek"))

    status_parser = subparsers.add_parser("trek-status", help="Show saved scan status")
    status_parser.add_argument("--db", type=Path, default=Path("data/tracker.sqlite3"))

    validate_parser = subparsers.add_parser(
        "validate-trek-config",
        help="Check a config file and print its scan profiles",
    )
    validate_parser.add_argument("--config", type=Path, default=PROFILES)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)
    if args.command == "trek-scan":
        return scan_command(
            config=args.config,
            db=args.db,
            profile=args.profile,
            watch_keys=set(args.watch_keys) if args.watch_keys else None,
            resume=args.resume,
            max_cells=args.max_cells,
            report_dir=args.report_dir,
            emit_json=args.emit_json,
            fail_on_partial=args.fail_on_partial,
            workers=args.workers,
        )
    if args.command == "scan":
        return model_scan_command(
            model=args.model,
            size=args.size,
            color=args.color,
            config=args.config,
            db=args.db,
            profile=args.profile,
            resume=args.resume,
            max_cells=args.max_cells,
            report_dir=args.report_dir,
            workers=args.workers,
            fail_on_partial=args.fail_on_partial,
        )
    if args.command == "price-board":
        return price_board_command(
            config=args.config,
            db=args.db,
            profile=args.profile,
            watch_keys=set(args.watch_keys) if args.watch_keys else None,
            resume=args.resume,
            max_cells=args.max_cells,
            report_dir=args.report_dir,
            out=args.out,
            workers=args.workers,
            fail_on_partial=args.fail_on_partial,
        )
    if args.command == "trek-report":
        return report_command(
            config=args.config,
            db=args.db,
            scan_id=args.scan_id,
            report_dir=args.report_dir,
        )
    if args.command == "trek-audit-stores":
        return audit_stores_command(
            config=args.config,
            db=args.db,
            scan_id=args.scan_id,
            report_dir=args.report_dir,
        )
    if args.command == "trek-status":
        with Storage(args.db) as storage:
            print(json.dumps(storage.status(), indent=2, sort_keys=True))
        return 0
    if args.command == "validate-trek-config":
        return validate_config_command(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
