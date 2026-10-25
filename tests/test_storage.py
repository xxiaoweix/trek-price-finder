from dataclasses import replace
from decimal import Decimal

from bike_price_alerts.brands.trek.geography import GridCell
from bike_price_alerts.brands.trek.storage import Storage
from bike_price_alerts.core.models import Alert, Confidence, Listing


def _alert() -> Alert:
    listing = Listing(
        model_key="example",
        canonical_name="Trek Test",
        source="test",
        url="https://example.test/item",
        size="M",
        size_verified=True,
        in_stock=True,
        stock_verified=True,
        sale_price_usd=Decimal(1000),
        retailer="Example",
        sku="SKU-1",
        confidence=Confidence.VERIFIED,
    )
    return Alert(
        kind="verified_new_low",
        reason="test",
        listing=listing,
        previous_low_usd=None,
        baseline_usd=Decimal(1200),
    )


def test_alert_outbox_is_idempotent_and_retryable(tmp_path):
    with Storage(tmp_path / "state.sqlite3") as storage:
        assert storage.enqueue_alert("key", _alert(), "message")
        assert not storage.enqueue_alert("key", _alert(), "message")
        assert storage.pending_alerts() == [("key", "message")]

        storage.mark_alert_failed("key", "temporary")
        assert storage.pending_alerts() == [("key", "message")]

        storage.mark_alert_sent("key")
        assert storage.pending_alerts() == []


def test_scan_health_is_persisted(tmp_path):
    with Storage(tmp_path / "state.sqlite3") as storage:
        storage.save_scan_run(
            started_at="2026-01-01T00:00:00+00:00",
            listing_count=3,
            errors=["parse failed"],
            notes=["canary"],
        )
        row = storage.conn.execute(
            "SELECT status, listing_count, error_count FROM scan_runs"
        ).fetchone()
        status = storage.status()
    assert row == ("degraded", 3, 1)
    assert status["last_run"]["status"] == "degraded"
    assert status["pending_alerts"] == 0


def test_listings_for_scan_includes_price_first_seen_at(tmp_path):
    first = Listing(
        model_key="example",
        canonical_name="Trek Test",
        source="locally",
        url="https://example.test/item",
        observed_at="2026-07-27T15:00:00+00:00",
        size="M",
        size_verified=True,
        in_stock=True,
        stock_verified=True,
        sale_price_usd=Decimal(1000),
        retailer="Example",
        store_id=42,
        upc="123",
        confidence=Confidence.VERIFIED,
    )
    second = replace(
        first,
        observed_at="2026-07-28T15:00:00+00:00",
        sale_price_usd=Decimal(900),
    )
    with Storage(tmp_path / "state.sqlite3") as storage:
        storage.save_listing(first, scan_id=1)
        storage.save_listing(second, scan_id=2)

        current = storage.listings_for_scan(2)

    assert current[0]["first_seen_at"] == "2026-07-28T15:00:00+00:00"


def test_previous_finished_scan_uses_only_complete_baseline(tmp_path):
    cell = GridCell(
        cell_id="cell",
        label="test",
        lat=42.0,
        lng=-89.0,
        distance_diag=100,
    )
    with Storage(tmp_path / "state.sqlite3") as storage:
        partial_id = storage.start_scan(
            started_at="2026-01-01T00:00:00+00:00",
            profile="conus",
            config_hash="same",
        )
        storage.finish_scan(partial_id, errors=["temporary"], notes=[])

        complete_id = storage.start_scan(
            started_at="2026-01-02T00:00:00+00:00",
            profile="conus",
            config_hash="same",
        )
        storage.add_coverage_cells(complete_id, "example", "upc", (cell,))
        storage.mark_cell_running(complete_id, "example", "upc", cell.cell_id)
        storage.finish_cell(
            complete_id,
            "example",
            "upc",
            cell.cell_id,
            status="complete",
            saturated=False,
            store_count=0,
            marker_count=0,
        )
        storage.finish_scan(complete_id, errors=[], notes=[])

        current_id = storage.start_scan(
            started_at="2026-01-03T00:00:00+00:00",
            profile="conus",
            config_hash="same",
        )

        assert storage.previous_finished_scan(current_id, profile="conus") == complete_id


def test_resumed_offer_tasks_are_grouped_by_cell(tmp_path):
    cells = (
        GridCell(
            cell_id="z-cell",
            label="z",
            lat=42.0,
            lng=-89.0,
            distance_diag=100,
        ),
        GridCell(
            cell_id="a-cell",
            label="a",
            lat=41.0,
            lng=-88.0,
            distance_diag=100,
        ),
    )
    with Storage(tmp_path / "state.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-01-01T00:00:00+00:00",
            profile="conus",
            config_hash="same",
        )
        storage.add_coverage_cells(scan_id, "example", "upc", cells)
        for store_id, cell in zip((2, 1), cells, strict=True):
            assert storage.claim_offer(
                scan_id,
                "example",
                "upc",
                store_id,
                cell.cell_id,
            )
            storage.finish_offer(
                scan_id,
                "example",
                "upc",
                store_id,
                error="temporary",
            )
        storage.recover_scan(scan_id)

        tasks = storage.pending_offer_tasks(scan_id)

    assert [task["cell_id"] for task in tasks] == ["a-cell", "z-cell"]


def test_requeue_transient_failures_skips_saturated_and_exhausted(tmp_path):
    cell = GridCell(
        cell_id="cell",
        label="cell",
        lat=42.0,
        lng=-89.0,
        distance_diag=100,
    )
    with Storage(tmp_path / "state.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-01-01T00:00:00+00:00",
            profile="conus",
            config_hash="same",
        )
        storage.add_coverage_cells(scan_id, "example", "upc-429", (cell,))
        storage.add_coverage_cells(scan_id, "example", "upc-sat", (cell,))
        storage.add_coverage_cells(scan_id, "example", "upc-old", (cell,))
        storage.mark_cell_running(scan_id, "example", "upc-429", cell.cell_id)
        storage.finish_cell(
            scan_id,
            "example",
            "upc-429",
            cell.cell_id,
            status="failed",
            saturated=False,
            store_count=0,
            marker_count=0,
            error="HTTPError: HTTP Error 429: Too Many Requests",
        )
        storage.mark_cell_running(scan_id, "example", "upc-sat", cell.cell_id)
        storage.finish_cell(
            scan_id,
            "example",
            "upc-sat",
            cell.cell_id,
            status="failed",
            saturated=True,
            store_count=0,
            marker_count=0,
            error="watch cell UPC upc-sat: saturated at minimum distance_diag=45",
        )
        for _ in range(3):
            storage.mark_cell_running(scan_id, "example", "upc-old", cell.cell_id)
        storage.finish_cell(
            scan_id,
            "example",
            "upc-old",
            cell.cell_id,
            status="failed",
            saturated=False,
            store_count=0,
            marker_count=0,
            error="TimeoutError: timed out",
        )
        assert storage.claim_offer(scan_id, "example", "upc-429", 7, cell.cell_id)
        storage.finish_offer(
            scan_id,
            "example",
            "upc-429",
            7,
            error="HTTPError: HTTP Error 429: Too Many Requests",
        )
        assert storage.claim_offer(scan_id, "example", "upc-429", 8, cell.cell_id)
        storage.finish_offer(
            scan_id,
            "example",
            "upc-429",
            8,
            error="ValueError: price missing",
        )

        requeued = storage.requeue_transient_failures(scan_id)
        pending_cells = {
            (row["upc"], row["attempts"])
            for row in storage.pending_coverage_cells(scan_id)
        }
        pending_offers = {
            (row["store_id"], row["attempts"])
            for row in storage.pending_offer_tasks(scan_id)
        }

    assert "HTTPError: HTTP Error 429: Too Many Requests" in requeued
    assert pending_cells == {("upc-429", 1)}
    assert pending_offers == {(7, 1)}


def test_store_catalog_audit_is_persisted_and_can_fall_back(tmp_path):
    with Storage(tmp_path / "state.sqlite3") as storage:
        first_id = storage.start_scan(
            started_at="2026-01-01T00:00:00+00:00",
            profile="conus",
            config_hash="same",
        )
        storage.save_store_catalog_audit(
            scan_id=first_id,
            source_url="https://example.test/stores",
            started_at="2026-01-01T00:01:00+00:00",
            status="complete",
            page_count=39,
            official_us_store_count=1923,
            official_conus_store_count=1872,
        )
        first = storage.store_catalog_summary(first_id)

        second_id = storage.start_scan(
            started_at="2026-01-02T00:00:00+00:00",
            profile="conus",
            config_hash="same",
        )
        storage.save_store_catalog_audit(
            scan_id=second_id,
            source_url="https://example.test/stores",
            started_at="2026-01-02T00:01:00+00:00",
            status="failed",
            error="temporary",
        )
        fallback = storage.store_catalog_summary(second_id)

    assert first["audit_status"] == "complete"
    assert first["official_conus_store_count"] == 1872
    assert not first["stale"]
    assert fallback["audit_status"] == "failed"
    assert fallback["official_conus_store_count"] == 1872
    assert fallback["audit_scan_id"] == first_id
    assert fallback["stale"]
    assert fallback["error"] == "temporary"
