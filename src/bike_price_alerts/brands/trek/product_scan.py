"""Nationwide Trek price scan: one map sweep per product, then carried UPCs only.

A Locally discovery call for any UPC of a product returns
``product_carrying_store_ids``. A follow-up call with ``only_store_id`` returns
``all_upcs_carried`` for that store. Price calls are limited to those pairs.
Checked against per-UPC discovery for one complete bike: the product store
set matched the union of that product's UPCs, and two store calls listed
exactly the UPCs those stores stocked.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ...core.http import HTTPClient
from ...core.models import Listing
from .geography import seed_cells, subdivide
from .models import AppConfig, LocallySpec, LocallyVariant, WatchSpec
from .scanner import (
    ScanResult,
    _accept_listing,
    _cell_from_row,
    _client_provider,
    _marker_in_scope,
    _worker_count,
)
from .sources.locally_widget import (
    LocallyWidgetCrawler,
    carried_upc_codes,
    product_carrying_store_ids,
)
from .storage import Storage

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_GEN_RE = re.compile(r"\bGen\s*\d+\b", re.IGNORECASE)


def _aliases(name: str) -> tuple[str, ...]:
    """Locally sometimes drops the generation the Trek catalog keeps in the name."""

    names = [name]
    shortened = re.sub(r"\s+", " ", _GEN_RE.sub("", name)).strip(" -")
    if shortened and shortened.casefold() not in {item.casefold() for item in names}:
        names.append(shortened)
    return tuple(names)


@dataclass
class ProductScanStats:
    discovery_requests: int = 0
    inventory_requests: int = 0
    price_requests: int = 0
    unusable_prices: int = 0
    result: ScanResult = field(default_factory=ScanResult)

    @property
    def request_count(self) -> int:
        return self.discovery_requests + self.inventory_requests + self.price_requests


def _unusable_price(message: str) -> bool:
    """A min/max range is not one store's price, so it is omitted rather than failed."""

    return "returned a price range " in message


def load_top_seller_watches(path: Path) -> tuple[WatchSpec, ...]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("products"), list):
        raise TypeError(f"{path} must contain a products list")
    watches: list[WatchSpec] = []
    seen_upcs: set[str] = set()
    for product in raw["products"]:
        if not isinstance(product, dict):
            raise TypeError(f"{path} has a product that is not a mapping")
        name = str(product.get("name") or "").strip()
        style = str(product.get("style") or "").strip()
        probe = str(product.get("probe_upc") or "").strip()
        key = str(product.get("key") or _SLUG_RE.sub("_", name.casefold()).strip("_"))
        rows = product.get("variants") or []
        if not name or not style or not probe or not rows:
            raise ValueError(f"{path} product {key or name!r} is missing name, style, or variants")
        variants: list[LocallyVariant] = []
        for row in rows:
            upc = str(row.get("upc") or "").strip()
            size = str(row.get("size") or "").strip()
            color = str(row.get("color") or "").strip() or None
            if not upc or not size:
                raise ValueError(f"{name} has a variant without a UPC and size")
            if upc in seen_upcs:
                raise ValueError(f"Duplicate UPC {upc} in {path}")
            seen_upcs.add(upc)
            variants.append(
                LocallyVariant(
                    upc=upc,
                    product_id=int(row.get("product_id") or 0),
                    style=style,
                    size=size,
                    color=color,
                )
            )
        if probe not in {variant.upc for variant in variants}:
            raise ValueError(f"{name} probe UPC {probe} is not one of its variants")
        ordered = tuple(sorted(variants, key=lambda variant: variant.upc != probe))
        watches.append(
            WatchSpec(
                key=key,
                canonical_name=name,
                aliases=_aliases(name),
                exclusions=(),
                allowed_sizes=tuple(dict.fromkeys(variant.size for variant in ordered)),
                baseline_usd=Decimal("1.00"),
                msrp_usd=None,
                locally=LocallySpec(variants=ordered),
                public_urls=(),
            )
        )
    if not watches:
        raise ValueError(f"{path} has no products")
    return tuple(watches)


def _with_product_id(variant: LocallyVariant, product_id: int) -> LocallyVariant:
    if variant.product_id and variant.product_id != product_id:
        raise ValueError(
            f"UPC {variant.upc} belongs to product {product_id}, "
            f"not configured product {variant.product_id}"
        )
    return replace(variant, product_id=product_id)


@dataclass
class _StoreInventory:
    store_id: int
    carried: tuple[str, ...] = ()
    probe_listing: Listing | None = None
    error: str | None = None


@dataclass
class _DiscoveryWork:
    task: dict[str, object]
    error: str | None = None
    payload: dict[str, Any] | None = None
    product_id: int = 0
    saturated: bool = False
    marker_count: int = 0
    store_ids: tuple[int, ...] = ()


def scan_top_sellers(
    config: AppConfig,
    http: HTTPClient,
    storage: Storage,
    *,
    scan_id: int,
    profile_name: str,
    max_cells: int | None = None,
    workers: int | None = None,
    progress: Any = None,
) -> ProductScanStats:
    """Sweep the grid once per configured product and price carried UPCs."""

    stats = ProductScanStats(result=ScanResult(scan_id=scan_id))
    result = stats.result
    profile = config.scan_profile(profile_name)
    watches = {watch.key: watch for watch in config.watches}
    probes = {watch.key: watch.locally.variants[0] for watch in config.watches}
    variants = {
        (watch.key, variant.upc): variant
        for watch in config.watches
        for variant in watch.locally.variants
    }
    catalog = {
        watch.key: {variant.upc for variant in watch.locally.variants}
        for watch in config.watches
    }
    initial_cells = seed_cells(profile)
    for watch in config.watches:
        if not watch.locally.variants:
            result.errors.append(f"{watch.key}: no variants configured")
            continue
        storage.add_coverage_cells(scan_id, watch.key, probes[watch.key].upc, initial_cells)

    worker_count = _worker_count(workers)
    client_for = _client_provider(http)
    crawlers = threading.local()
    claimed_stores: set[tuple[str, int]] = set()

    def crawler_for() -> LocallyWidgetCrawler:
        crawler = getattr(crawlers, "crawler", None)
        client = client_for()
        if crawler is None or crawler.http is not client:
            crawler = LocallyWidgetCrawler(client, config)
            crawlers.crawler = crawler
        return crawler

    def discover_task(task: dict[str, object]) -> _DiscoveryWork:
        key = str(task["model_key"])
        watch = watches.get(key)
        probe = probes.get(key)
        if watch is None or probe is None:
            return _DiscoveryWork(task=task, error="product is no longer configured")
        cell = _cell_from_row(task)
        try:
            discovery = crawler_for().discover(watch, probe, cell.as_widget_point())
        except Exception as exc:
            return _DiscoveryWork(task=task, error=f"{type(exc).__name__}: {exc}")
        product = discovery.payload.get("product") or {}
        try:
            product_id = int(product.get("id") or 0)
        except (TypeError, ValueError):
            product_id = 0
        style = str(product.get("style_number") or "")
        title = str(product.get("name") or "")
        if product_id <= 0 or (style and style != probe.style):
            return _DiscoveryWork(
                task=task,
                error=f"Locally identified style {style!r} product {product_id}, expected {probe.style}",
            )
        if title and not any(alias.casefold() in title.casefold() for alias in watch.aliases):
            return _DiscoveryWork(task=task, error=f"Locally returned {title!r}")
        return _DiscoveryWork(
            task=task,
            payload=discovery.payload,
            product_id=product_id,
            saturated=discovery.saturated,
            marker_count=discovery.marker_count,
            store_ids=product_carrying_store_ids(discovery.payload),
        )

    def inventory_task(job: tuple[str, int, dict[str, object], dict[str, Any], int]) -> _StoreInventory:
        model_key, store_id, task, _payload, product_id = job
        watch = watches[model_key]
        probe = _with_product_id(probes[model_key], product_id)
        cell = _cell_from_row(task)
        try:
            detail = crawler_for().store_payload(probe, cell.as_widget_point(), store_id)
        except Exception as exc:
            return _StoreInventory(store_id=store_id, error=f"{type(exc).__name__}: {exc}")
        carried = tuple(upc for upc in carried_upc_codes(detail) if upc in catalog[model_key])
        listing = None
        if probe.upc in carried:
            try:
                listing = crawler_for()._listing(
                    watch,
                    probe,
                    cell.as_widget_point(),
                    store_id,
                    None,
                    detail,
                )
            except Exception as exc:
                return _StoreInventory(
                    store_id=store_id,
                    carried=carried,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return _StoreInventory(store_id=store_id, carried=carried, probe_listing=listing)

    def run_price(jobs: list[tuple[WatchSpec, LocallyVariant, Any, int]]) -> None:
        claimed = [
            job
            for job in jobs
            if storage.claim_offer(
                scan_id, job[0].key, job[1].upc, job[3], job[2].cell_id
            )
        ]
        if not claimed:
            return
        stats.price_requests += len(claimed)

        def once(job: tuple[WatchSpec, LocallyVariant, Any, int]) -> tuple[Any, Listing | None, str | None]:
            watch, variant, cell, store_id = job
            try:
                listing = crawler_for().offer(watch, variant, cell.as_widget_point(), store_id)
            except Exception as exc:
                return job, None, f"{type(exc).__name__}: {exc}"
            return job, listing, None

        if worker_count == 1:
            outcomes = [once(job) for job in claimed]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                outcomes = list(pool.map(once, claimed))
        for job, listing, error in outcomes:
            watch, variant, _cell, store_id = job
            if error or listing is None:
                message = error or "empty offer"
                if _unusable_price(message):
                    storage.finish_offer(
                        scan_id, watch.key, variant.upc, store_id, ignored=True
                    )
                    stats.unusable_prices += 1
                    continue
                storage.finish_offer(
                    scan_id,
                    watch.key,
                    variant.upc,
                    store_id,
                    error=f"{watch.key} store {store_id} UPC {variant.upc}: {message}",
                )
                result.errors.append(
                    f"{watch.key} store {store_id} UPC {variant.upc}: {message}"
                )
                continue
            if not _accept_listing(profile, listing):
                storage.finish_offer(
                    scan_id, watch.key, variant.upc, store_id, ignored=True
                )
                result.ignored_out_of_scope += 1
                continue
            storage.save_listing(listing, scan_id=scan_id)
            result.listings.append(listing)
            storage.finish_offer(scan_id, watch.key, variant.upc, store_id)

    def pending_price_jobs() -> list[tuple[WatchSpec, LocallyVariant, Any, int]]:
        # Sibling UPCs do not have their own coverage rows, so the join is on
        # the product's probe cell rather than offer.upc.
        rows = storage.conn.execute(
            """
            SELECT o.model_key, o.upc, o.store_id, o.cell_id,
                   c.label, c.lat, c.lng, c.distance_diag, c.depth
            FROM offer_tasks AS o
            JOIN coverage_cells AS c
              ON c.scan_id = o.scan_id
             AND c.model_key = o.model_key
             AND c.cell_id = o.cell_id
            WHERE o.scan_id = ? AND o.status = 'planned'
            """,
            (scan_id,),
        ).fetchall()
        jobs = []
        for model_key, upc, store_id, cell_id, label, lat, lng, distance_diag, depth in rows:
            variant = variants.get((str(model_key), str(upc)))
            watch = watches.get(str(model_key))
            if watch is None or variant is None:
                result.errors.append(f"orphaned offer {model_key} UPC {upc}")
                continue
            jobs.append(
                (
                    watch,
                    variant,
                    _cell_from_row(
                        {
                            "cell_id": cell_id,
                            "label": label,
                            "lat": lat,
                            "lng": lng,
                            "distance_diag": distance_diag,
                            "depth": depth,
                        }
                    ),
                    int(store_id),
                )
            )
        return jobs

    processed = 0
    reported = 0
    batch_size = max(worker_count * 4, 8)
    retry_pass = False
    while True:
        pending = storage.pending_coverage_cells(scan_id)
        if not pending:
            if retry_pass:
                break
            retry_pass = True
            requeued = storage.requeue_transient_failures(scan_id)
            if not requeued:
                break
            worker_count = min(2, worker_count)
            batch_size = max(worker_count * 4, 8)
            dropped = {message[:1000] for message in requeued}
            result.errors = [message for message in result.errors if message[:1000] not in dropped]
            result.notes.append("retried transient Locally failures at lower concurrency")
            run_price(pending_price_jobs())
            continue
        if max_cells is not None and processed >= max_cells:
            result.notes.append(f"bounded scan stopped after {max_cells} coverage cells")
            break
        room = len(pending) if max_cells is None else min(len(pending), max_cells - processed)
        batch = pending[: min(room, batch_size)]
        runnable: list[dict[str, object]] = []
        for task in batch:
            watch = watches.get(str(task["model_key"]))
            probe = probes.get(str(task["model_key"]))
            if watch is None or probe is None:
                storage.finish_cell(
                    scan_id,
                    str(task["model_key"]),
                    str(task["upc"]),
                    str(task["cell_id"]),
                    status="failed",
                    saturated=False,
                    store_count=0,
                    marker_count=0,
                    error="product is no longer configured",
                )
                continue
            storage.mark_cell_running(scan_id, watch.key, probe.upc, str(task["cell_id"]))
            runnable.append(task)
        if not runnable:
            continue
        processed += len(runnable)
        stats.discovery_requests += len(runnable)
        if worker_count == 1:
            discovered = [discover_task(task) for task in runnable]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                discovered = list(pool.map(discover_task, runnable))

        inventory_jobs: list[tuple[str, int, dict[str, object], dict[str, Any], int]] = []
        owners: dict[tuple[str, str], list[int]] = {}
        for work in discovered:
            task = work.task
            model_key = str(task["model_key"])
            probe = probes[model_key]
            cell = _cell_from_row(task)
            if work.error or work.payload is None:
                message = f"{model_key} {cell.label}: {work.error or 'empty discovery'}"
                storage.finish_cell(
                    scan_id,
                    model_key,
                    probe.upc,
                    cell.cell_id,
                    status="failed",
                    saturated=False,
                    store_count=0,
                    marker_count=0,
                    error=message,
                )
                result.errors.append(message)
                continue
            for upc in catalog[model_key]:
                variants[(model_key, upc)] = _with_product_id(
                    variants[(model_key, upc)], work.product_id
                )
            probes[model_key] = variants[(model_key, probes[model_key].upc)]
            if work.saturated:
                children = subdivide(cell, profile)
                if not children:
                    message = (
                        f"{model_key} {cell.label}: saturated at "
                        f"minimum distance_diag={cell.distance_diag}"
                    )
                    storage.finish_cell(
                        scan_id,
                        model_key,
                        probe.upc,
                        cell.cell_id,
                        status="failed",
                        saturated=True,
                        store_count=len(work.store_ids),
                        marker_count=work.marker_count,
                        error=message,
                    )
                    result.errors.append(message)
                    continue
                storage.add_coverage_cells(scan_id, model_key, probe.upc, children)
                storage.finish_cell(
                    scan_id,
                    model_key,
                    probe.upc,
                    cell.cell_id,
                    status="subdivided",
                    saturated=True,
                    store_count=len(work.store_ids),
                    marker_count=work.marker_count,
                )
                continue
            markers = [
                marker
                for marker in work.payload.get("markers", [])
                if isinstance(marker, dict) and _marker_in_scope(marker)
            ]
            markers_by_id = {int(marker["id"]): marker for marker in markers if "id" in marker}
            owned: list[int] = []
            for store_id in work.store_ids:
                if store_id not in markers_by_id:
                    continue
                token = (model_key, store_id)
                if token in claimed_stores:
                    continue
                claimed_stores.add(token)
                owned.append(store_id)
                inventory_jobs.append((model_key, store_id, task, work.payload, work.product_id))
            owners[(model_key, cell.cell_id)] = owned
            storage.save_store_discoveries(
                scan_id=scan_id,
                model_key=model_key,
                upc=probe.upc,
                cell_id=cell.cell_id,
                markers=markers,
                carrying_store_ids=set(),
            )
        stats.inventory_requests += len(inventory_jobs)
        if worker_count == 1:
            inventories = [inventory_task(job) for job in inventory_jobs]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                inventories = list(pool.map(inventory_task, inventory_jobs))
        inventory_by_store = {
            (job[0], job[1]): inventory
            for job, inventory in zip(inventory_jobs, inventories, strict=True)
        }
        price_jobs: list[tuple[WatchSpec, LocallyVariant, Any, int]] = []
        finished: set[tuple[str, str]] = set()
        for work in discovered:
            task = work.task
            model_key = str(task["model_key"])
            cell = _cell_from_row(task)
            token = (model_key, cell.cell_id)
            if token not in owners or token in finished:
                continue
            finished.add(token)
            probe = probes[model_key]
            watch = watches[model_key]
            # A store call that failed before listing UPCs must be retried with the cell.
            http_failures = [
                inventory_by_store[(model_key, store_id)]
                for store_id in owners[token]
                if inventory_by_store[(model_key, store_id)].error
                and not inventory_by_store[(model_key, store_id)].carried
                and inventory_by_store[(model_key, store_id)].probe_listing is None
            ]
            if http_failures:
                for store_id in owners[token]:
                    claimed_stores.discard((model_key, store_id))
                message = (
                    f"{model_key} {cell.label} store {http_failures[0].store_id}: "
                    f"{http_failures[0].error}"
                )
                storage.finish_cell(
                    scan_id,
                    model_key,
                    probe.upc,
                    cell.cell_id,
                    status="failed",
                    saturated=False,
                    store_count=len(owners[token]),
                    marker_count=work.marker_count,
                    error=message,
                )
                result.errors.append(message)
                continue
            carrying: set[int] = set()
            for store_id in owners[token]:
                inventory = inventory_by_store[(model_key, store_id)]
                if probe.upc in inventory.carried and inventory.probe_listing is not None:
                    carrying.add(store_id)
                    if storage.claim_offer(scan_id, model_key, probe.upc, store_id, cell.cell_id):
                        listing = inventory.probe_listing
                        if _accept_listing(profile, listing):
                            storage.save_listing(listing, scan_id=scan_id)
                            result.listings.append(listing)
                            storage.finish_offer(scan_id, model_key, probe.upc, store_id)
                        else:
                            storage.finish_offer(
                                scan_id, model_key, probe.upc, store_id, ignored=True
                            )
                            result.ignored_out_of_scope += 1
                elif probe.upc in inventory.carried and inventory.error:
                    if storage.claim_offer(scan_id, model_key, probe.upc, store_id, cell.cell_id):
                        if _unusable_price(inventory.error):
                            storage.finish_offer(
                                scan_id, model_key, probe.upc, store_id, ignored=True
                            )
                            stats.unusable_prices += 1
                        else:
                            message = (
                                f"{model_key} store {store_id} UPC {probe.upc}: {inventory.error}"
                            )
                            storage.finish_offer(
                                scan_id, model_key, probe.upc, store_id, error=message
                            )
                            result.errors.append(message)
                        carrying.add(store_id)
                for upc in inventory.carried:
                    if upc == probe.upc:
                        continue
                    variant = _with_product_id(variants[(model_key, upc)], work.product_id)
                    price_jobs.append((watch, variant, cell, store_id))
            if carrying:
                storage.save_store_discoveries(
                    scan_id=scan_id,
                    model_key=model_key,
                    upc=probe.upc,
                    cell_id=cell.cell_id,
                    markers=[
                        marker
                        for marker in (work.payload or {}).get("markers", [])
                        if isinstance(marker, dict)
                        and _marker_in_scope(marker)
                        and int(marker.get("id", -1)) in carrying
                    ],
                    carrying_store_ids=carrying,
                )
            storage.finish_cell(
                scan_id,
                model_key,
                probe.upc,
                cell.cell_id,
                status="complete",
                saturated=False,
                store_count=len(work.store_ids),
                marker_count=work.marker_count,
            )
        run_price(price_jobs)
        if progress and processed // 25 > reported // 25:
            reported = processed
            summary = storage.coverage_summary(scan_id)
            progress(
                f"PROGRESS scan={scan_id} processed_cells={processed} "
                f"requests={stats.request_count} cells={summary['cells']}"
            )
    run_price(pending_price_jobs())
    if result.ignored_out_of_scope:
        result.notes.append(
            f"ignored {result.ignored_out_of_scope} offers outside the selected geography"
        )
    if stats.unusable_prices:
        result.notes.append(
            f"ignored {stats.unusable_prices} prices because the store returned a range, "
            "not one price"
        )
    result.notes.append(
        "product discovery: "
        f"{stats.discovery_requests} map calls, "
        f"{stats.inventory_requests} store calls, "
        f"{stats.price_requests} price calls"
    )
    return stats
