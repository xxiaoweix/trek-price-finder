from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from bike_price_alerts.brands.trek.config import load_config
from bike_price_alerts.brands.trek.geography import seed_cells, subdivide
from bike_price_alerts.brands.trek.models import LocallySpec, ScanProfile
from bike_price_alerts.brands.trek.reporting import (
    _annotate_offer_history,
    _apply_reporting_msrps,
    _scan_reference,
    _shortlist_offers,
    generate_daily_report,
)
from bike_price_alerts.brands.trek.scanner import scan_widget_coverage
from bike_price_alerts.brands.trek.storage import Storage


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class ProductionWidgetHTTP:
    def __init__(self, variant, *, price="800.00", saturated_at=()):
        self.variant = variant
        self.price = price
        self.saturated_at = set(saturated_at)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        params = params or {}
        self.calls.append((url, params, headers))
        if url.endswith("/conversion"):
            return FakeResponse({})
        if "/geo/point/" in url:
            match = re.search(r"/geo/point/(-?\d+\.\d+)/(-?\d+\.\d+)", url)
            assert match
            return FakeResponse(
                {
                    "is_certain": True,
                    "lat": float(match.group(1)),
                    "lng": float(match.group(2)),
                }
            )
        if not url.endswith("/stores/conversion_data"):
            raise AssertionError(f"unexpected URL {url}")
        diag = int(params["map_distance_diag"])
        if diag in self.saturated_at:
            return FakeResponse(
                {
                    "stores_are_overpopulated": True,
                    "upc_carrying_store_ids": [],
                    "markers": [],
                }
            )
        store_id = params.get("only_store_id")
        if store_id is None:
            return FakeResponse(
                {
                    "stores_are_overpopulated": False,
                    "upc_carrying_store_ids": [243954],
                    "markers": [self._marker()],
                }
            )
        return FakeResponse(
            {
                "stores_are_overpopulated": False,
                "upc_carrying_store_ids": [243954],
                "markers": [self._marker()],
                "product": {
                    "id": self.variant.product_id,
                    "name": "Marlin 5 Gen 3",
                    "style_number": self.variant.style,
                    "unit_msrp": 1000.00,
                    "upc_data": {
                        "code": self.variant.upc,
                        "size_1": self.variant.size,
                        "size_2": "",
                        "color_name": self.variant.color,
                        "unit_msrp": 1000.00,
                    },
                },
                "variant_html": (
                    '<span class="pdp-price" '
                    f'data-price-min="{self.price}" data-price-max="{self.price}" '
                    'data-forced-currency="USD" data-n-store-prices="1" '
                    'data-n-custom-prices="1" data-is-sale-price="1"></span>'
                ),
            }
        )

    @staticmethod
    def _marker():
        return {
            "id": 243954,
            "name": "Example Bikes",
            "address": "1 Main",
            "city": "Denver",
            "state": "CO",
            "zip": "80202",
            "lat": 39.7525,
            "lng": -104.9995,
            "distance": 5.8,
            "inventory_refreshed_at": "2026-07-27 01:12:43",
        }


class FlakyDiscoveryHTTP(ProductionWidgetHTTP):
    def __init__(self, variant):
        super().__init__(variant)
        self.failures_left = 1

    def get(self, url, *, params=None, headers=None):
        params = params or {}
        if (
            str(url).endswith("/stores/conversion_data")
            and not params.get("only_store_id")
            and self.failures_left
        ):
            self.failures_left -= 1
            raise TimeoutError("429 too many requests")
        return super().get(url, params=params, headers=headers)


class ForeignWidgetHTTP(ProductionWidgetHTTP):
    @staticmethod
    def _marker():
        marker = ProductionWidgetHTTP._marker()
        marker.update({"country": "CA", "state": "BC"})
        return marker


def _single_variant_config():
    config = load_config("tests/fixtures/parser-config.yaml")
    watch = next(item for item in config.watches if item.key == "marlin_5_gen_3_57363")
    variant = next(item for item in watch.locally.variants if item.upc == "197216263494")
    watch = replace(
        watch,
        locally=LocallySpec(variants=(variant,), seed_store_ids=()),
        public_urls=(),
    )
    return replace(config, watches=(watch,)), watch, variant


def _run(tmp_path, *, price, scan_number):
    config, _, variant = _single_variant_config()
    db = tmp_path / "tracker.sqlite3"
    with Storage(db) as storage:
        scan_id = storage.start_scan(
            started_at=f"2026-07-{scan_number:02d}T12:00:00+00:00",
            profile="canary",
            config_hash="test-config",
        )
        result = scan_widget_coverage(
            config,
            ProductionWidgetHTTP(variant, price=price),
            storage,
            scan_id=scan_id,
            profile_name="canary",
        )
        storage.finish_scan(scan_id, errors=result.errors, notes=result.notes)
        report = generate_daily_report(
            storage,
            config,
            scan_id=scan_id,
            output_dir=tmp_path / "reports",
        )
        return (
            scan_id,
            result,
            report,
            storage.coverage_summary(scan_id),
            storage.listings_for_scan(scan_id),
        )


def test_scan_reference_uses_pacific_date_and_hides_internal_id():
    reference = _scan_reference(
        {
            "scan_id": 123,
            "started_at": "2026-07-27T15:19:00+00:00",
            "finished_at": "2026-07-27T16:01:00+00:00",
        }
    )

    assert reference["scan_id"] == 123
    assert reference["label"] == "2026-07-27 08:19–09:01 PDT"
    assert "123" not in reference["label"]
    assert "Scan" not in reference["label"]


def test_report_date_uses_scan_completion_date(tmp_path):
    config, _, _ = _single_variant_config()
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-07-31T06:50:00+00:00",
            profile="canary",
            config_hash="test-config",
        )
        storage.finish_scan(scan_id, errors=[], notes=[])
        storage.conn.execute(
            "UPDATE scan_runs SET finished_at = ? WHERE id = ?",
            ("2026-07-31T06:55:00+00:00", scan_id),
        )
        storage.conn.commit()
        report = generate_daily_report(
            storage,
            config,
            scan_id=scan_id,
            output_dir=tmp_path / "reports",
        )

    assert report.payload["report_date"] == "2026-07-30"


def test_offer_history_annotation_marks_current_new_offer():
    listing = {
        "model_key": "example",
        "upc": "123",
        "store_id": 456,
        "observed_at": "2026-07-28T02:15:00+00:00",
        "first_seen_at": "2026-07-28T02:15:00+00:00",
    }
    changes = [{"kind": "new_offer", "listing": listing}]

    _annotate_offer_history([listing], changes, "compared")

    assert listing["first_seen_date"] == "2026-07-27"
    assert listing["is_new_offer"]


def test_shortlist_keeps_all_offers_at_five_lowest_overall_price_levels():
    rows = [
        {"sale_price_usd": str(price), "discount_pct": str(discount), "retailer": retailer}
        for price, discount, retailer in [
            (1000, 10, "A"),
            (1000, 20, "B"),
            (1100, 10, "C"),
            (1200, 10, "D"),
            (1300, 10, "E"),
            (1400, 10, "F"),
            (1500, 10, "G"),
            (900, 9.9, "excluded"),
        ]
    ]

    shortlist = _shortlist_offers(rows)

    assert [item["retailer"] for item in shortlist] == ["A", "B", "C", "D", "E"]


def test_reporting_msrp_uses_the_configured_price_when_locally_is_lower():
    config = load_config("tests/fixtures/parser-config.yaml")
    rows = [
        {
            "model_key": "fuel_ex_5_gen_7_79586",
            "sale_price_usd": "1400.00",
            "msrp_usd": "1500.00",
            "discount_pct": "0.0",
            "retailer": "Example Bikes",
        }
    ]

    _apply_reporting_msrps(rows, config)
    shortlist = _shortlist_offers(rows)

    assert rows[0]["msrp_usd"] == "2000.0"
    assert rows[0]["discount_pct"] == "30.0"
    assert [item["retailer"] for item in shortlist] == ["Example Bikes"]


def test_checkpointed_scan_is_idempotent_and_builds_complete_report(tmp_path):
    scan_id, result, report, coverage, listings = _run(
        tmp_path,
        price="800.00",
        scan_number=27,
    )

    assert scan_id == 1
    assert not result.errors
    assert coverage["complete"]
    assert coverage["cells"] == {"complete": 1}
    assert coverage["offers"] == {"complete": 1}
    assert coverage["discovered_stores"] == 1
    assert coverage["stocked_store_variant_pairs"] == 1
    assert coverage["unpriced_stocked_store_variant_pairs"] == 0
    assert coverage["discovered_store_states"] == {"CO": 1}
    assert len(listings) == 1
    assert listings[0]["sale_price_usd"] == "800.00"
    assert listings[0]["sku"] is None
    assert report.status == "COMPLETE"
    assert not report.payload["national_claim_allowed"]
    assert report.markdown_path.exists()
    report_text = report.markdown_path.read_text(encoding="utf-8")
    assert "Example Bikes" in report_text
    assert "它不是美国本土全国扫描" in report_text
    assert "- 扫描时间：" in report_text
    assert "- Scan ID：" not in report_text

    config, _, variant = _single_variant_config()
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        rerun = scan_widget_coverage(
            config,
            ProductionWidgetHTTP(variant, price="2500.00"),
            storage,
            scan_id=scan_id,
            profile_name="canary",
        )
        assert not rerun.listings
        assert storage.scan_listing_count(scan_id) == 1


def test_second_report_detects_price_drop(tmp_path):
    first_id, *_ = _run(tmp_path, price="800.00", scan_number=27)
    second_id, _, report, _, _ = _run(tmp_path, price="700.00", scan_number=28)

    assert first_id == 1
    assert second_id == 2
    assert report.payload["schema_version"] == 4
    assert report.payload["comparison"]["status"] == "compared"
    assert report.payload["comparison"]["previous_scan"]["scan_id"] == first_id
    assert report.payload["comparison"]["current_scan"]["scan_id"] == second_id
    drops = [change for change in report.payload["changes"] if change["kind"] == "price_drop"]
    assert len(drops) == 1
    assert drops[0]["old_price_usd"] == "800.00"
    assert drops[0]["new_price_usd"] == "700.00"
    assert "$800.00 → $700.00" in report.markdown_path.read_text(encoding="utf-8")


def test_partial_report_skips_changes_against_previous_complete_scan(tmp_path):
    first_id, *_ = _run(tmp_path, price="800.00", scan_number=27)
    config, _, _ = _single_variant_config()
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        second_id = storage.start_scan(
            started_at="2026-07-28T12:00:00+00:00",
            profile="canary",
            config_hash="test-config",
        )
        storage.finish_scan(second_id, errors=["temporary network failure"], notes=[])
        report = generate_daily_report(
            storage,
            config,
            scan_id=second_id,
            output_dir=tmp_path / "reports",
        )

    assert report.status == "PARTIAL"
    assert report.payload["previous_scan_id"] == first_id
    assert report.payload["comparison"]["status"] == "skipped_current_partial"
    assert report.payload["changes"] == []
    report_text = report.markdown_path.read_text(encoding="utf-8")
    assert "为避免把未覆盖区域误判为库存消失" in report_text
    assert "库存消失：" not in report_text


def test_saturated_cell_is_subdivided_before_completion(tmp_path):
    config, _, variant = _single_variant_config()
    adaptive = ScanProfile(
        name="adaptive",
        coverage="points",
        points=config.scan_profile("canary").points,
        initial_distance_diag=100,
        minimum_distance_diag=50,
        max_depth=1,
    )
    point = replace(adaptive.points[0], distance_diag=100)
    adaptive = replace(adaptive, points=(point,))
    config = replace(
        config,
        default_scan_profile="adaptive",
        scan_profiles=config.scan_profiles + (adaptive,),
    )
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-07-27T12:00:00+00:00",
            profile="adaptive",
            config_hash="test-config",
        )
        result = scan_widget_coverage(
            config,
            ProductionWidgetHTTP(variant, saturated_at=(100,)),
            storage,
            scan_id=scan_id,
            profile_name="adaptive",
        )
        storage.finish_scan(scan_id, errors=result.errors, notes=result.notes)
        coverage = storage.coverage_summary(scan_id)

    assert not result.errors
    assert coverage["complete"]
    assert coverage["cells"] == {"complete": 4, "subdivided": 1}
    assert coverage["saturated_cells"] == 1
    assert coverage["discovered_stores"] == 1
    assert coverage["stocked_store_variant_pairs"] == 1
    assert coverage["unpriced_stocked_store_variant_pairs"] == 0
    assert coverage["discovered_store_states"] == {"CO": 1}


def test_conus_grid_is_deterministic_and_adaptive():
    config = load_config("tests/fixtures/parser-config.yaml")
    profile = config.scan_profile("conus")
    cells = seed_cells(profile)

    assert len(cells) == 81
    assert len({cell.cell_id for cell in cells}) == len(cells)
    assert min(cell.lat for cell in cells) >= 24.4
    assert max(cell.lat for cell in cells) <= 49.0
    children = subdivide(cells[0], profile)
    assert len(children) == 4
    assert {cell.distance_diag for cell in children} == {150}
    assert all(cell.parent_cell_id == cells[0].cell_id for cell in children)


def test_report_delivery_outbox_is_retryable(tmp_path):
    scan_id, _, report, _, _ = _run(tmp_path, price="800.00", scan_number=27)
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        assert storage.pending_report_deliveries() == [
            (
                scan_id,
                report.delivery_message,
                str(report.markdown_path),
                str(report.json_path),
            )
        ]
        assert storage.pending_report_deliveries(scan_id) == (
            storage.pending_report_deliveries()
        )
        assert storage.pending_report_deliveries(scan_id + 1) == []
        storage.mark_report_failed(scan_id, "temporary")
        assert storage.pending_report_deliveries()
        storage.mark_report_sent(scan_id)
        assert storage.pending_report_deliveries() == []
        assert storage.previous_low("marlin_5_gen_3_57363") == Decimal("800.00")


def test_foreign_store_is_ignored_without_degrading_coverage(tmp_path):
    config, _, variant = _single_variant_config()
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-07-27T12:00:00+00:00",
            profile="canary",
            config_hash="test-config",
        )
        result = scan_widget_coverage(
            config,
            ForeignWidgetHTTP(variant),
            storage,
            scan_id=scan_id,
            profile_name="canary",
        )
        storage.finish_scan(scan_id, errors=result.errors, notes=result.notes)
        coverage = storage.coverage_summary(scan_id)

    assert not result.errors
    assert result.ignored_out_of_scope == 1
    assert coverage["complete"]
    assert coverage["offers"] == {"ignored": 1}
    assert coverage["discovered_stores"] == 0
    assert coverage["stocked_store_variant_pairs"] == 0
    assert coverage["unpriced_stocked_store_variant_pairs"] == 0
    assert coverage["discovered_store_states"] == {}


def test_missing_stocked_discovery_is_reconciled_without_rescanning_cell(tmp_path):
    config, watch, variant = _single_variant_config()
    cell = seed_cells(config.scan_profile("canary"))[0]
    http = ProductionWidgetHTTP(variant)
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-07-27T12:00:00+00:00",
            profile="canary",
            config_hash="test-config",
        )
        storage.add_coverage_cells(scan_id, watch.key, variant.upc, (cell,))
        storage.mark_cell_running(scan_id, watch.key, variant.upc, cell.cell_id)
        storage.save_store_discoveries(
            scan_id=scan_id,
            model_key=watch.key,
            upc=variant.upc,
            cell_id=cell.cell_id,
            markers=[ProductionWidgetHTTP._marker()],
            carrying_store_ids={243954},
        )
        storage.finish_cell(
            scan_id,
            watch.key,
            variant.upc,
            cell.cell_id,
            status="complete",
            saturated=False,
            store_count=1,
            marker_count=1,
        )
        before = storage.coverage_summary(scan_id)

        result = scan_widget_coverage(
            config,
            http,
            storage,
            scan_id=scan_id,
            profile_name="canary",
        )
        after = storage.coverage_summary(scan_id)

    assert not before["complete"]
    assert before["unpriced_stocked_store_variant_pairs"] == 1
    assert not result.errors
    assert after["complete"]
    assert after["unpriced_stocked_store_variant_pairs"] == 0
    discovery_calls = [
        params
        for url, params, _ in http.calls
        if url.endswith("/stores/conversion_data") and not params.get("only_store_id")
    ]
    assert discovery_calls == []


def test_conus_report_fails_closed_when_a_state_has_no_store_markers(tmp_path):
    config, _, variant = _single_variant_config()
    canary = config.scan_profile("canary")
    test_conus = ScanProfile(
        name="conus",
        coverage="points",
        points=canary.points,
        initial_distance_diag=100,
        minimum_distance_diag=50,
        max_depth=1,
    )
    config = replace(
        config,
        default_scan_profile="conus",
        scan_profiles=tuple(
            profile for profile in config.scan_profiles if profile.name != "conus"
        )
        + (test_conus,),
    )
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-07-27T12:00:00+00:00",
            profile="conus",
            config_hash="test-config",
        )
        result = scan_widget_coverage(
            config,
            ProductionWidgetHTTP(variant),
            storage,
            scan_id=scan_id,
            profile_name="conus",
        )
        storage.finish_scan(scan_id, errors=result.errors, notes=result.notes)
        report = generate_daily_report(
            storage,
            config,
            scan_id=scan_id,
            output_dir=tmp_path / "reports",
        )

    assert report.status == "PARTIAL"
    assert not report.payload["national_claim_allowed"]
    assert "CO" not in report.payload["coverage"]["missing_store_states"]
    assert "CA" in report.payload["coverage"]["missing_store_states"]


def test_report_keeps_store_counts_concise(tmp_path):
    scan_id, *_ = _run(tmp_path, price="800.00", scan_number=27)
    config, _, _ = _single_variant_config()
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        storage.save_store_catalog_audit(
            scan_id=scan_id,
            source_url="https://www.trekbikes.com/us/en_US/store-finder/retailer/US/",
            started_at="2026-07-27T13:00:00+00:00",
            status="complete",
            page_count=39,
            official_us_store_count=1923,
            official_conus_store_count=1872,
        )
        report = generate_daily_report(
            storage,
            config,
            scan_id=scan_id,
            output_dir=tmp_path / "reports",
        )

    assert report.payload["store_counts"]["official_conus_store_count"] == 1872
    assert report.payload["store_counts"]["locally_scannable_store_count"] == 1
    report_text = report.markdown_path.read_text(encoding="utf-8")
    assert "Trek 官方美国本土店铺：1872" in report_text
    assert "Locally 可扫描店铺：1" in report_text


def test_transient_discovery_failure_is_retried_before_partial(tmp_path):
    config, _, variant = _single_variant_config()
    http = FlakyDiscoveryHTTP(variant)
    with Storage(tmp_path / "tracker.sqlite3") as storage:
        scan_id = storage.start_scan(
            started_at="2026-07-27T12:00:00+00:00",
            profile="canary",
            config_hash="test-config",
        )
        result = scan_widget_coverage(
            config,
            http,
            storage,
            scan_id=scan_id,
            profile_name="canary",
            workers=4,
        )
        summary = storage.coverage_summary(scan_id)

    assert http.failures_left == 0
    assert result.errors == []
    assert result.listings
    assert "retried transient Locally failures at lower concurrency" in result.notes
    assert summary["complete"] is True
