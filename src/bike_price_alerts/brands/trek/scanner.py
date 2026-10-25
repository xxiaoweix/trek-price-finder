from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ...core.geography import CONUS_STATE_CODES
from ...core.http import HTTPClient
from ...core.models import Listing
from .geography import GridCell, seed_cells, subdivide
from .models import AppConfig, LocallyVariant, ScanProfile, WatchSpec
from .sources.bikeexchange import parse_bikeexchange
from .sources.locally import LocallyAPI, parse_locally_storefront
from .sources.locally_widget import LocallyWidgetCrawler, OutOfScopeStore
from .storage import Storage


@dataclass
class ScanResult:
    scan_id: int | None = None
    listings: list[Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ignored_out_of_scope: int = 0


def _parse(source: str, html: str, url: str, watch: WatchSpec) -> Listing | None:
    if source == "bikeexchange":
        return parse_bikeexchange(html, url, watch)
    if source == "locally_storefront":
        return parse_locally_storefront(html, url, watch)
    raise ValueError(f"Unknown source: {source}")


def scan(config: AppConfig, http: HTTPClient) -> ScanResult:
    result = ScanResult()
    seen_urls: set[tuple[str, str]] = set()
    seen_widget_offers: set[tuple[str, int, str]] = set()
    locally_api = LocallyAPI(http, config)
    locally_widget = LocallyWidgetCrawler(http, config)
    profile = config.scan_profile()

    for watch in config.watches:
        for public in watch.public_urls:
            key = (watch.key, public.url)
            if key in seen_urls:
                continue
            seen_urls.add(key)
            try:
                response = http.get(public.url)
                listing = _parse(public.source, response.text, str(response.url), watch)
                if listing:
                    result.listings.append(listing)
            except Exception as exc:  # keep other sources running
                result.errors.append(f"{watch.key} {public.source}: {type(exc).__name__}: {exc}")

        if (
            profile.points
            and watch.locally.variants
        ):
            for point in profile.points:
                try:
                    widget_result = locally_widget.offers_at(watch, point)
                    result.errors.extend(widget_result.errors)
                    result.notes.extend(widget_result.notes)
                    for upc in widget_result.saturated_upcs:
                        result.errors.append(
                            f"{watch.key} locally_widget {point.label} UPC {upc}: "
                            "coverage saturated"
                        )
                    for listing in widget_result.listings:
                        assert listing.store_id is not None
                        assert listing.upc is not None
                        offer_key = (watch.key, listing.store_id, listing.upc)
                        if offer_key in seen_widget_offers:
                            continue
                        seen_widget_offers.add(offer_key)
                        result.listings.append(listing)
                except Exception as exc:
                    result.errors.append(
                        f"{watch.key} locally_widget {point.label}: "
                        f"{type(exc).__name__}: {exc}"
                    )

        if not locally_api.enabled:
            result.notes.append(
                f"{watch.key}: nationwide Locally API enumeration skipped; "
                "LOCALLY_API_TOKEN not set"
            )
            continue
        if not watch.locally.variants:
            result.notes.append(
                f"{watch.key}: Locally API enabled but no exact variants are configured"
            )
            continue
        try:
            stores = locally_api.nationwide_stores(watch)
            max_stores = int(os.getenv("TRACKER_MAX_STORES_PER_WATCH", "0"))
            if max_stores > 0 and len(stores) > max_stores:
                result.notes.append(
                    f"{watch.key}: canary limit retained {max_stores} of {len(stores)} stores"
                )
                stores = stores[:max_stores]
            for store in stores:
                if not store.product_url:
                    continue
                key = (watch.key, store.product_url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                try:
                    response = http.get(store.product_url)
                    listing = parse_locally_storefront(response.text, str(response.url), watch)
                    if not listing:
                        continue
                    listing.store_id = store.store_id
                    listing.retailer = listing.retailer or store.name
                    listing.city = listing.city or store.city
                    listing.state = listing.state or store.state
                    listing.product_id = listing.product_id or store.product_id
                    listing.upc = store.matching_upcs[0] if len(store.matching_upcs) == 1 else None
                    listing.evidence["matching_upcs"] = list(store.matching_upcs)
                    result.listings.append(listing)
                except Exception as exc:
                    result.errors.append(
                        f"{watch.key} locally_store {store.store_id}: {type(exc).__name__}: {exc}"
                    )
        except Exception as exc:
            result.errors.append(f"{watch.key} locally_api: {type(exc).__name__}: {exc}")
    return result


def _variant_maps(
    config: AppConfig,
    watch_keys: set[str] | None,
) -> tuple[dict[str, WatchSpec], dict[tuple[str, str], LocallyVariant]]:
    watches = {
        watch.key: watch
        for watch in config.watches
        if watch_keys is None or watch.key in watch_keys
    }
    if watch_keys is not None:
        missing = watch_keys - set(watches)
        if missing:
            raise ValueError(f"Unknown watch keys: {', '.join(sorted(missing))}")
    variants = {
        (watch.key, variant.upc): variant
        for watch in watches.values()
        for variant in watch.locally.variants
    }
    return watches, variants


def _cell_from_row(row: dict[str, object]) -> GridCell:
    return GridCell(
        cell_id=str(row["cell_id"]),
        parent_cell_id=(
            str(row["parent_cell_id"]) if row.get("parent_cell_id") is not None else None
        ),
        label=str(row["label"]),
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        distance_diag=int(row["distance_diag"]),
        depth=int(row["depth"]),
    )


def _offer_point_from_row(row: dict[str, object]) -> GridCell:
    return GridCell(
        cell_id=str(row["cell_id"]),
        label=str(row["label"]),
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        distance_diag=int(row["distance_diag"]),
        depth=int(row["depth"]),
    )


def _accept_listing(profile: ScanProfile, listing: Listing) -> bool:
    if listing.state:
        return listing.state.upper() in CONUS_STATE_CODES
    return profile.coverage != "conus"


def _marker_in_scope(marker: dict[str, object]) -> bool:
    country = str(marker.get("country") or "").upper()
    state = str(marker.get("state") or "").upper()
    if country and country != "US":
        return False
    return not state or state in CONUS_STATE_CODES


def _worker_count(workers: int | None) -> int:
    count = workers if workers is not None else int(os.getenv("TRACKER_SCAN_WORKERS", "6"))
    if count < 1:
        raise ValueError("scan workers must be at least 1")
    return count


class _LockedHTTP:
    """Serialize a test double. Real clients are one per worker instead."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def get(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._inner.get(*args, **kwargs)


def _client_provider(template: HTTPClient) -> Callable[[], Any]:
    if not isinstance(template, HTTPClient):
        shared = _LockedHTTP(template)
        return lambda: shared
    local = threading.local()

    def provide() -> HTTPClient:
        client = getattr(local, "client", None)
        if client is None:
            client = HTTPClient()
            local.client = client
        return client

    return provide


@dataclass
class _OfferJob:
    watch: WatchSpec
    variant: LocallyVariant
    cell: GridCell
    store_id: int
    base: dict[str, Any] | None


@dataclass
class _OfferOutcome:
    job: _OfferJob
    listing: Listing | None = None
    ignored: bool = False
    error: str | None = None


@dataclass
class _DiscoveryOutcome:
    error: str | None = None
    discovery: Any = None


def scan_widget_coverage(
    config: AppConfig,
    http: HTTPClient,
    storage: Storage,
    *,
    scan_id: int,
    profile_name: str,
    watch_keys: set[str] | None = None,
    max_cells: int | None = None,
    progress: Callable[[str], None] | None = None,
    workers: int | None = None,
) -> ScanResult:
    result = ScanResult(scan_id=scan_id)
    profile = config.scan_profile(profile_name)
    watches, variants = _variant_maps(config, watch_keys)
    initial_cells = seed_cells(profile)
    worker_count = _worker_count(workers)
    client_for = _client_provider(http)
    crawlers = threading.local()

    def crawler_for() -> LocallyWidgetCrawler:
        crawler = getattr(crawlers, "crawler", None)
        client = client_for()
        if crawler is None or crawler.http is not client:
            crawler = LocallyWidgetCrawler(client, config)
            crawlers.crawler = crawler
        return crawler

    for watch in watches.values():
        if not watch.locally.variants:
            result.errors.append(f"{watch.key}: no exact Locally variants configured")
            continue
        for variant in watch.locally.variants:
            storage.add_coverage_cells(
                scan_id,
                watch.key,
                variant.upc,
                initial_cells,
            )

    def claim_offer_job(
        watch: WatchSpec,
        variant: LocallyVariant,
        cell: GridCell,
        store_id: int,
        *,
        base: dict[str, Any] | None = None,
    ) -> _OfferJob | None:
        if not storage.claim_offer(
            scan_id,
            watch.key,
            variant.upc,
            store_id,
            cell.cell_id,
        ):
            return None
        marker = next(
            (
                item
                for item in (base or {}).get("markers", [])
                if isinstance(item, dict) and int(item.get("id", -1)) == store_id
            ),
            None,
        )
        if marker:
            country = str(marker.get("country") or "").upper()
            state = str(marker.get("state") or "").upper()
            outside_us = bool(country and country != "US")
            outside_conus = bool(
                profile.coverage == "conus" and state and state not in CONUS_STATE_CODES
            )
            if outside_us or outside_conus:
                storage.finish_offer(
                    scan_id,
                    watch.key,
                    variant.upc,
                    store_id,
                    ignored=True,
                )
                result.ignored_out_of_scope += 1
                return None
        return _OfferJob(
            watch=watch,
            variant=variant,
            cell=cell,
            store_id=store_id,
            base=base,
        )

    def execute_offer(job: _OfferJob) -> _OfferOutcome:
        try:
            listing = crawler_for().offer(
                job.watch,
                job.variant,
                job.cell.as_widget_point(),
                job.store_id,
                base=job.base,
            )
        except OutOfScopeStore:
            return _OfferOutcome(job=job, ignored=True)
        except Exception as exc:
            message = (
                f"{job.watch.key} store {job.store_id} UPC {job.variant.upc}: "
                f"{type(exc).__name__}: {exc}"
            )
            return _OfferOutcome(job=job, error=message)
        return _OfferOutcome(job=job, listing=listing)

    def apply_offer(outcome: _OfferOutcome) -> None:
        job = outcome.job
        if outcome.ignored:
            storage.finish_offer(
                scan_id,
                job.watch.key,
                job.variant.upc,
                job.store_id,
                ignored=True,
            )
            result.ignored_out_of_scope += 1
            return
        if outcome.error or outcome.listing is None:
            message = outcome.error or (
                f"{job.watch.key} store {job.store_id} UPC {job.variant.upc}: empty offer"
            )
            storage.finish_offer(
                scan_id,
                job.watch.key,
                job.variant.upc,
                job.store_id,
                error=message,
            )
            result.errors.append(message)
            return
        listing = outcome.listing
        if not _accept_listing(profile, listing):
            storage.finish_offer(
                scan_id,
                job.watch.key,
                job.variant.upc,
                job.store_id,
                ignored=True,
            )
            result.ignored_out_of_scope += 1
            return
        if storage.save_listing(listing, scan_id=scan_id):
            result.listings.append(listing)
        storage.finish_offer(scan_id, job.watch.key, job.variant.upc, job.store_id)

    def run_offers(jobs: list[_OfferJob]) -> None:
        if not jobs:
            return
        if worker_count == 1:
            for job in jobs:
                apply_offer(execute_offer(job))
            return
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            for outcome in pool.map(execute_offer, jobs):
                apply_offer(outcome)

    def jobs_for_tasks(tasks: list[dict[str, object]]) -> list[_OfferJob]:
        jobs: list[_OfferJob] = []
        for task in tasks:
            key = (str(task["model_key"]), str(task["upc"]))
            watch = watches.get(key[0])
            variant = variants.get(key)
            if watch is None or variant is None:
                result.errors.append(f"orphaned offer task {key[0]} UPC {key[1]}")
                continue
            job = claim_offer_job(
                watch,
                variant,
                _offer_point_from_row(task),
                int(task["store_id"]),
            )
            if job is not None:
                jobs.append(job)
        return jobs

    def reconcile_missing_offers() -> None:
        run_offers(jobs_for_tasks(storage.missing_stocked_discoveries(scan_id)))

    run_offers(jobs_for_tasks(storage.pending_offer_tasks(scan_id)))
    reconcile_missing_offers()

    def discover_cell(task: dict[str, object]) -> _DiscoveryOutcome:
        key = (str(task["model_key"]), str(task["upc"]))
        watch = watches.get(key[0])
        variant = variants.get(key)
        if watch is None or variant is None:
            return _DiscoveryOutcome(error="watch or variant is no longer configured")
        cell = _cell_from_row(task)
        try:
            discovery = crawler_for().discover(watch, variant, cell.as_widget_point())
        except Exception as exc:
            return _DiscoveryOutcome(error=f"{type(exc).__name__}: {exc}")
        return _DiscoveryOutcome(discovery=discovery)

    processed_cells = 0
    reported_cells = 0
    batch_size = max(worker_count * 4, 8)
    retry_pass = False
    while True:
        pending = storage.pending_coverage_cells(scan_id)
        if not pending:
            if retry_pass:
                break
            retry_pass = True
            requeued_errors = storage.requeue_transient_failures(scan_id)
            if not requeued_errors:
                break
            worker_count = min(2, worker_count)
            batch_size = max(worker_count * 4, 8)
            dropped = {message[:1000] for message in requeued_errors}
            result.errors = [
                message for message in result.errors if message[:1000] not in dropped
            ]
            result.notes.append(
                "retried transient Locally failures at lower concurrency"
            )
            run_offers(jobs_for_tasks(storage.pending_offer_tasks(scan_id)))
            continue
        if max_cells is not None and processed_cells >= max_cells:
            result.notes.append(f"bounded scan stopped after {max_cells} coverage cells")
            return result
        room = len(pending)
        if max_cells is not None:
            room = min(room, max_cells - processed_cells)
        batch = pending[: min(room, batch_size)]
        runnable: list[dict[str, object]] = []
        for task in batch:
            key = (str(task["model_key"]), str(task["upc"]))
            watch = watches.get(key[0])
            variant = variants.get(key)
            if watch is None or variant is None:
                result.errors.append(f"orphaned coverage task {key[0]} UPC {key[1]}")
                storage.finish_cell(
                    scan_id,
                    key[0],
                    key[1],
                    str(task["cell_id"]),
                    status="failed",
                    saturated=False,
                    store_count=0,
                    marker_count=0,
                    error="watch or variant is no longer configured",
                )
                continue
            cell = _cell_from_row(task)
            storage.mark_cell_running(scan_id, watch.key, variant.upc, cell.cell_id)
            runnable.append(task)
        if not runnable:
            continue
        processed_cells += len(runnable)
        if worker_count == 1:
            discovered = [discover_cell(task) for task in runnable]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                discovered = list(pool.map(discover_cell, runnable))
        offer_jobs: list[_OfferJob] = []
        for task, outcome in zip(runnable, discovered, strict=True):
            key = (str(task["model_key"]), str(task["upc"]))
            watch = watches[key[0]]
            variant = variants[key]
            cell = _cell_from_row(task)
            if outcome.error or outcome.discovery is None:
                message = (
                    f"{watch.key} {cell.label} UPC {variant.upc}: "
                    f"{outcome.error or 'empty discovery'}"
                )
                storage.finish_cell(
                    scan_id,
                    watch.key,
                    variant.upc,
                    cell.cell_id,
                    status="failed",
                    saturated=False,
                    store_count=0,
                    marker_count=0,
                    error=message,
                )
                result.errors.append(message)
                continue
            discovery = outcome.discovery
            carrying_ids = set(discovery.store_ids)
            markers = [
                marker
                for marker in discovery.payload.get("markers", [])
                if isinstance(marker, dict) and _marker_in_scope(marker)
            ]
            storage.save_store_discoveries(
                scan_id=scan_id,
                model_key=watch.key,
                upc=variant.upc,
                cell_id=cell.cell_id,
                markers=markers,
                carrying_store_ids=carrying_ids,
            )
            for store_id in discovery.store_ids:
                job = claim_offer_job(
                    watch,
                    variant,
                    cell,
                    store_id,
                    base=discovery.payload,
                )
                if job is not None:
                    offer_jobs.append(job)
            if discovery.saturated:
                children = subdivide(cell, profile)
                if not children:
                    message = (
                        f"{watch.key} {cell.label} UPC {variant.upc}: saturated "
                        f"at minimum distance_diag={cell.distance_diag}"
                    )
                    storage.finish_cell(
                        scan_id,
                        watch.key,
                        variant.upc,
                        cell.cell_id,
                        status="failed",
                        saturated=True,
                        store_count=len(discovery.store_ids),
                        marker_count=discovery.marker_count,
                        error=message,
                    )
                    result.errors.append(message)
                    continue
                storage.add_coverage_cells(scan_id, watch.key, variant.upc, children)
                storage.finish_cell(
                    scan_id,
                    watch.key,
                    variant.upc,
                    cell.cell_id,
                    status="subdivided",
                    saturated=True,
                    store_count=len(discovery.store_ids),
                    marker_count=discovery.marker_count,
                )
                continue
            storage.finish_cell(
                scan_id,
                watch.key,
                variant.upc,
                cell.cell_id,
                status="complete",
                saturated=False,
                store_count=len(discovery.store_ids),
                marker_count=discovery.marker_count,
            )
        run_offers(offer_jobs)
        if progress and processed_cells // 25 > reported_cells // 25:
            reported_cells = processed_cells
            summary = storage.coverage_summary(scan_id)
            progress(
                f"PROGRESS scan={scan_id} processed_cells={processed_cells} "
                f"cells={summary['cells']} offers={summary['offers']}"
            )
    reconcile_missing_offers()
    if result.ignored_out_of_scope:
        result.notes.append(
            f"ignored {result.ignored_out_of_scope} offers outside the selected geography"
        )
    return result
