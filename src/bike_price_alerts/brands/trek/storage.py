from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from ...core.models import Alert, Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER,
    fingerprint TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    model_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    size TEXT,
    size_verified INTEGER NOT NULL,
    in_stock INTEGER,
    stock_verified INTEGER NOT NULL,
    sale_price_usd TEXT,
    msrp_usd TEXT,
    retailer TEXT,
    city TEXT,
    state TEXT,
    product_id INTEGER,
    store_id INTEGER,
    sku TEXT,
    listing_id TEXT,
    upc TEXT,
    color TEXT,
    confidence TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_lookup
ON observations(model_key, size, sale_price_usd, observed_at);

CREATE INDEX IF NOT EXISTS idx_observations_offer_history
ON observations(model_key, upc, store_id, scan_id, observed_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    alert_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    kind TEXT NOT NULL,
    model_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    last_error TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'legacy',
    config_hash TEXT NOT NULL DEFAULT '',
    listing_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    note_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    coverage_status TEXT NOT NULL DEFAULT 'unknown',
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_cells (
    scan_id INTEGER NOT NULL,
    model_key TEXT NOT NULL,
    upc TEXT NOT NULL,
    cell_id TEXT NOT NULL,
    parent_cell_id TEXT,
    label TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    distance_diag INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    saturated INTEGER NOT NULL DEFAULT 0,
    store_count INTEGER NOT NULL DEFAULT 0,
    marker_count INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY (scan_id, model_key, upc, cell_id),
    FOREIGN KEY (scan_id) REFERENCES scan_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_coverage_cells_status
ON coverage_cells(scan_id, status, model_key, upc);

CREATE TABLE IF NOT EXISTS offer_tasks (
    scan_id INTEGER NOT NULL,
    model_key TEXT NOT NULL,
    upc TEXT NOT NULL,
    store_id INTEGER NOT NULL,
    cell_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scan_id, model_key, upc, store_id),
    FOREIGN KEY (scan_id) REFERENCES scan_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_offer_tasks_status
ON offer_tasks(scan_id, status);

CREATE TABLE IF NOT EXISTS stores (
    store_id INTEGER PRIMARY KEY,
    name TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    zip TEXT,
    lat REAL,
    lng REAL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_discoveries (
    scan_id INTEGER NOT NULL,
    model_key TEXT NOT NULL,
    upc TEXT NOT NULL,
    store_id INTEGER NOT NULL,
    cell_id TEXT NOT NULL,
    in_stock INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (scan_id, model_key, upc, store_id),
    FOREIGN KEY (scan_id) REFERENCES scan_runs(id),
    FOREIGN KEY (store_id) REFERENCES stores(store_id)
);

CREATE INDEX IF NOT EXISTS idx_store_discoveries_scan
ON store_discoveries(scan_id, store_id, in_stock);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY,
    scan_id INTEGER NOT NULL UNIQUE,
    report_date TEXT NOT NULL,
    status TEXT NOT NULL,
    markdown_path TEXT NOT NULL,
    json_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_message TEXT,
    sent_at TEXT,
    last_error TEXT,
    FOREIGN KEY (scan_id) REFERENCES scan_runs(id)
);

CREATE TABLE IF NOT EXISTS store_catalog_audits (
    scan_id INTEGER PRIMARY KEY,
    source_url TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    page_count INTEGER,
    official_us_store_count INTEGER,
    official_conus_store_count INTEGER,
    error TEXT,
    FOREIGN KEY (scan_id) REFERENCES scan_runs(id)
);
"""


class Storage:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        additions = {
            "alerts": {
                "status": "TEXT NOT NULL DEFAULT 'pending'",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "sent_at": "TEXT",
                "last_error": "TEXT",
                "message": "TEXT",
            },
            "observations": {
                "scan_id": "INTEGER",
                "product_id": "INTEGER",
                "store_id": "INTEGER",
            },
            "stores": {
                "country": "TEXT",
            },
            "scan_runs": {
                "profile": "TEXT NOT NULL DEFAULT 'legacy'",
                "config_hash": "TEXT NOT NULL DEFAULT ''",
                "coverage_status": "TEXT NOT NULL DEFAULT 'unknown'",
            },
            "daily_reports": {
                "delivery_status": "TEXT NOT NULL DEFAULT 'pending'",
                "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
                "delivery_message": "TEXT",
                "sent_at": "TEXT",
                "last_error": "TEXT",
            },
        }
        for table, table_additions in additions.items():
            columns = {
                row[1]
                for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in table_additions.items():
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_observations_scan_offer
            ON observations(scan_id, model_key, source, store_id, upc)
            WHERE scan_id IS NOT NULL AND store_id IS NOT NULL AND upc IS NOT NULL
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save_listing(self, listing: Listing, *, scan_id: int | None = None) -> bool:
        payload = listing.as_json_dict()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO observations (
                scan_id, fingerprint, observed_at, model_key, canonical_name, source, url,
                size, size_verified, in_stock, stock_verified, sale_price_usd,
                msrp_usd, retailer, city, state, product_id, store_id, sku,
                listing_id, upc, color, confidence, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                listing.fingerprint(),
                listing.observed_at,
                listing.model_key,
                listing.canonical_name,
                listing.source,
                listing.url,
                listing.size,
                int(listing.size_verified),
                None if listing.in_stock is None else int(listing.in_stock),
                int(listing.stock_verified),
                str(listing.sale_price_usd) if listing.sale_price_usd is not None else None,
                str(listing.msrp_usd) if listing.msrp_usd is not None else None,
                listing.retailer,
                listing.city,
                listing.state,
                listing.product_id,
                listing.store_id,
                listing.sku,
                listing.listing_id,
                listing.upc,
                listing.color,
                listing.confidence.value,
                json.dumps(payload, sort_keys=True),
            ),
        )
        if listing.store_id is not None:
            now = listing.observed_at
            evidence = listing.evidence
            self.conn.execute(
                """
                INSERT INTO stores (
                    store_id, name, address, city, state, zip, lat, lng,
                    first_seen_at, last_seen_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id) DO UPDATE SET
                    name = COALESCE(excluded.name, stores.name),
                    address = COALESCE(excluded.address, stores.address),
                    city = COALESCE(excluded.city, stores.city),
                    state = COALESCE(excluded.state, stores.state),
                    zip = COALESCE(excluded.zip, stores.zip),
                    lat = COALESCE(excluded.lat, stores.lat),
                    lng = COALESCE(excluded.lng, stores.lng),
                    last_seen_at = excluded.last_seen_at,
                    source = excluded.source
                """,
                (
                    listing.store_id,
                    listing.retailer,
                    evidence.get("address"),
                    listing.city,
                    listing.state,
                    evidence.get("zip"),
                    evidence.get("store_lat"),
                    evidence.get("store_lng"),
                    now,
                    now,
                    listing.source,
                ),
            )
        self.conn.commit()
        return cursor.rowcount == 1

    def save_store_discoveries(
        self,
        *,
        scan_id: int,
        model_key: str,
        upc: str,
        cell_id: str,
        markers: list[dict[str, Any]],
        carrying_store_ids: set[int],
    ) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        saved = 0
        for marker in markers:
            try:
                store_id = int(marker["id"])
            except (KeyError, TypeError, ValueError):
                continue
            self.conn.execute(
                """
                INSERT INTO stores (
                    store_id, name, address, city, state, country, zip, lat, lng,
                    first_seen_at, last_seen_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'locally_widget_marker')
                ON CONFLICT(store_id) DO UPDATE SET
                    name = COALESCE(excluded.name, stores.name),
                    address = COALESCE(excluded.address, stores.address),
                    city = COALESCE(excluded.city, stores.city),
                    state = COALESCE(excluded.state, stores.state),
                    country = COALESCE(excluded.country, stores.country),
                    zip = COALESCE(excluded.zip, stores.zip),
                    lat = COALESCE(excluded.lat, stores.lat),
                    lng = COALESCE(excluded.lng, stores.lng),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    store_id,
                    marker.get("name"),
                    marker.get("address"),
                    marker.get("city"),
                    str(marker.get("state") or "").strip().upper() or None,
                    str(marker.get("country") or "").strip().upper() or None,
                    marker.get("zip"),
                    marker.get("lat"),
                    marker.get("lng"),
                    now,
                    now,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO store_discoveries (
                    scan_id, model_key, upc, store_id, cell_id, in_stock, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_id, model_key, upc, store_id) DO UPDATE SET
                    in_stock = MAX(store_discoveries.in_stock, excluded.in_stock),
                    discovered_at = excluded.discovered_at
                """,
                (
                    scan_id,
                    model_key,
                    upc,
                    store_id,
                    cell_id,
                    int(store_id in carrying_store_ids),
                    now,
                ),
            )
            saved += 1
        self.conn.commit()
        return saved

    def start_scan(self, *, started_at: str, profile: str, config_hash: str) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO scan_runs (
                started_at, finished_at, profile, config_hash, listing_count,
                error_count, note_count, status, coverage_status, details_json
            ) VALUES (?, ?, ?, ?, 0, 0, 0, 'running', 'pending', '{}')
            """,
            (started_at, started_at, profile, config_hash),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def resumable_scan(self, *, profile: str, config_hash: str) -> int | None:
        row = self.conn.execute(
            """
            SELECT id
            FROM scan_runs
            WHERE profile = ? AND config_hash = ? AND status = 'running'
            ORDER BY id DESC
            LIMIT 1
            """,
            (profile, config_hash),
        ).fetchone()
        return int(row[0]) if row else None

    def recover_scan(self, scan_id: int) -> None:
        self.conn.execute(
            """
            UPDATE coverage_cells
            SET status = 'planned'
            WHERE scan_id = ?
              AND (status = 'running' OR (status = 'failed' AND attempts < 3))
            """,
            (scan_id,),
        )
        self.conn.execute(
            """
            UPDATE offer_tasks
            SET status = 'planned'
            WHERE scan_id = ?
              AND (status = 'running' OR (status = 'failed' AND attempts < 3))
            """,
            (scan_id,),
        )
        self.conn.commit()

    def requeue_transient_failures(self, scan_id: int) -> list[str]:
        """Give transient HTTP failures one more planned attempt.

        Attempts are kept, so a cell or offer that has already been tried
        three times stays failed. Saturated coverage is not transient.
        """

        transient = """
            LOWER(COALESCE(error, '')) NOT LIKE '%saturated%'
            AND (
                LOWER(error) LIKE '%429%'
                OR LOWER(error) LIKE '%too many requests%'
                OR LOWER(error) LIKE '%timeout%'
                OR LOWER(error) LIKE '%urlerror%'
                OR LOWER(error) LIKE '%incompleteread%'
                OR LOWER(error) LIKE '%http error 500%'
                OR LOWER(error) LIKE '%http error 502%'
                OR LOWER(error) LIKE '%http error 503%'
                OR LOWER(error) LIKE '%http error 504%'
            )
        """
        cell_rows = self.conn.execute(
            f"""
            SELECT error
            FROM coverage_cells
            WHERE scan_id = ? AND status = 'failed' AND attempts < 3
              AND saturated = 0 AND {transient}
            """,
            (scan_id,),
        ).fetchall()
        offer_rows = self.conn.execute(
            f"""
            SELECT error
            FROM offer_tasks
            WHERE scan_id = ? AND status = 'failed' AND attempts < 3
              AND {transient}
            """,
            (scan_id,),
        ).fetchall()
        self.conn.execute(
            f"""
            UPDATE coverage_cells
            SET status = 'planned', error = NULL, finished_at = NULL
            WHERE scan_id = ? AND status = 'failed' AND attempts < 3
              AND saturated = 0 AND {transient}
            """,
            (scan_id,),
        )
        self.conn.execute(
            f"""
            UPDATE offer_tasks
            SET status = 'planned', error = NULL
            WHERE scan_id = ? AND status = 'failed' AND attempts < 3
              AND {transient}
            """,
            (scan_id,),
        )
        self.conn.commit()
        return [str(row[0]) for row in (*cell_rows, *offer_rows) if row[0]]

    def add_coverage_cells(
        self,
        scan_id: int,
        model_key: str,
        upc: str,
        cells: tuple[object, ...],
    ) -> None:
        rows = [
            (
                scan_id,
                model_key,
                upc,
                cell.cell_id,
                cell.parent_cell_id,
                cell.label,
                cell.lat,
                cell.lng,
                cell.distance_diag,
                cell.depth,
            )
            for cell in cells
        ]
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO coverage_cells (
                scan_id, model_key, upc, cell_id, parent_cell_id, label,
                lat, lng, distance_diag, depth
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()

    def pending_coverage_cells(self, scan_id: int) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT model_key, upc, cell_id, parent_cell_id, label, lat, lng,
                   distance_diag, depth, attempts
            FROM coverage_cells
            WHERE scan_id = ? AND status = 'planned'
            ORDER BY depth, cell_id, model_key, upc
            """,
            (scan_id,),
        ).fetchall()
        keys = (
            "model_key",
            "upc",
            "cell_id",
            "parent_cell_id",
            "label",
            "lat",
            "lng",
            "distance_diag",
            "depth",
            "attempts",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def mark_cell_running(self, scan_id: int, model_key: str, upc: str, cell_id: str) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """
            UPDATE coverage_cells
            SET status = 'running', attempts = attempts + 1,
                started_at = ?, error = NULL
            WHERE scan_id = ? AND model_key = ? AND upc = ? AND cell_id = ?
            """,
            (now, scan_id, model_key, upc, cell_id),
        )
        self.conn.commit()

    def finish_cell(
        self,
        scan_id: int,
        model_key: str,
        upc: str,
        cell_id: str,
        *,
        status: str,
        saturated: bool,
        store_count: int,
        marker_count: int,
        error: str | None = None,
    ) -> None:
        if status not in {"complete", "subdivided", "failed"}:
            raise ValueError(f"Unsupported coverage status: {status}")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """
            UPDATE coverage_cells
            SET status = ?, saturated = ?, store_count = ?, marker_count = ?,
                error = ?, finished_at = ?
            WHERE scan_id = ? AND model_key = ? AND upc = ? AND cell_id = ?
            """,
            (
                status,
                int(saturated),
                store_count,
                marker_count,
                error[:1000] if error else None,
                now,
                scan_id,
                model_key,
                upc,
                cell_id,
            ),
        )
        self.conn.commit()

    def claim_offer(
        self,
        scan_id: int,
        model_key: str,
        upc: str,
        store_id: int,
        cell_id: str,
        *,
        max_attempts: int = 3,
    ) -> bool:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT OR IGNORE INTO offer_tasks (
                scan_id, model_key, upc, store_id, cell_id, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'planned', ?)
            """,
            (scan_id, model_key, upc, store_id, cell_id, now),
        )
        cursor = self.conn.execute(
            """
            UPDATE offer_tasks
            SET status = 'running', attempts = attempts + 1, error = NULL,
                cell_id = ?, updated_at = ?
            WHERE scan_id = ? AND model_key = ? AND upc = ? AND store_id = ?
              AND status IN ('planned', 'failed') AND attempts < ?
            """,
            (
                cell_id,
                now,
                scan_id,
                model_key,
                upc,
                store_id,
                max_attempts,
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def pending_offer_tasks(self, scan_id: int) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT o.model_key, o.upc, o.store_id, o.cell_id, o.attempts,
                   c.label, c.lat, c.lng, c.distance_diag, c.depth
            FROM offer_tasks AS o
            JOIN coverage_cells AS c
              ON c.scan_id = o.scan_id
             AND c.model_key = o.model_key
             AND c.upc = o.upc
             AND c.cell_id = o.cell_id
            WHERE o.scan_id = ? AND o.status = 'planned'
            ORDER BY c.cell_id, o.model_key, o.upc, o.store_id
            """,
            (scan_id,),
        ).fetchall()
        keys = (
            "model_key",
            "upc",
            "store_id",
            "cell_id",
            "attempts",
            "label",
            "lat",
            "lng",
            "distance_diag",
            "depth",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def missing_stocked_discoveries(
        self,
        scan_id: int,
        *,
        max_attempts: int = 3,
    ) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT d.model_key, d.upc, d.store_id, d.cell_id,
                   COALESCE(o.attempts, 0),
                   c.label, c.lat, c.lng, c.distance_diag, c.depth
            FROM store_discoveries AS d
            JOIN coverage_cells AS c
              ON c.scan_id = d.scan_id
             AND c.model_key = d.model_key
             AND c.upc = d.upc
             AND c.cell_id = d.cell_id
            LEFT JOIN offer_tasks AS o
              ON o.scan_id = d.scan_id
             AND o.model_key = d.model_key
             AND o.upc = d.upc
             AND o.store_id = d.store_id
            WHERE d.scan_id = ? AND d.in_stock = 1
              AND (
                  o.status IS NULL
                  OR (o.status = 'failed' AND o.attempts < ?)
              )
            ORDER BY c.cell_id, d.model_key, d.upc, d.store_id
            """,
            (scan_id, max_attempts),
        ).fetchall()
        keys = (
            "model_key",
            "upc",
            "store_id",
            "cell_id",
            "attempts",
            "label",
            "lat",
            "lng",
            "distance_diag",
            "depth",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def finish_offer(
        self,
        scan_id: int,
        model_key: str,
        upc: str,
        store_id: int,
        *,
        error: str | None = None,
        ignored: bool = False,
    ) -> None:
        if error and ignored:
            raise ValueError("An offer cannot be both failed and ignored")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """
            UPDATE offer_tasks
            SET status = ?, error = ?, updated_at = ?
            WHERE scan_id = ? AND model_key = ? AND upc = ? AND store_id = ?
            """,
            (
                "ignored" if ignored else ("failed" if error else "complete"),
                error[:1000] if error else None,
                now,
                scan_id,
                model_key,
                upc,
                store_id,
            ),
        )
        self.conn.commit()

    def coverage_summary(self, scan_id: int) -> dict[str, object]:
        cell_rows = self.conn.execute(
            """
            SELECT status, COUNT(*), COALESCE(SUM(saturated), 0)
            FROM coverage_cells
            WHERE scan_id = ?
            GROUP BY status
            """,
            (scan_id,),
        ).fetchall()
        cell_status = {row[0]: row[1] for row in cell_rows}
        saturated = sum(int(row[2]) for row in cell_rows)
        offer_rows = self.conn.execute(
            """
            SELECT status, COUNT(*)
            FROM offer_tasks
            WHERE scan_id = ?
            GROUP BY status
            """,
            (scan_id,),
        ).fetchall()
        offer_status = {row[0]: row[1] for row in offer_rows}
        discovery_row = self.conn.execute(
            """
            SELECT COUNT(DISTINCT store_id), COALESCE(SUM(in_stock), 0)
            FROM store_discoveries
            WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchone()
        unpriced_stocked_pairs = int(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM store_discoveries AS d
                LEFT JOIN offer_tasks AS o
                  ON o.scan_id = d.scan_id
                 AND o.model_key = d.model_key
                 AND o.upc = d.upc
                 AND o.store_id = d.store_id
                WHERE d.scan_id = ? AND d.in_stock = 1
                  AND (o.status IS NULL OR o.status NOT IN ('complete', 'ignored'))
                """,
                (scan_id,),
            ).fetchone()[0]
        )
        state_rows = self.conn.execute(
            """
            SELECT COALESCE(NULLIF(UPPER(TRIM(s.state)), ''), 'UNKNOWN'),
                   COUNT(DISTINCT d.store_id)
            FROM store_discoveries AS d
            JOIN stores AS s ON s.store_id = d.store_id
            WHERE d.scan_id = ?
            GROUP BY COALESCE(NULLIF(UPPER(TRIM(s.state)), ''), 'UNKNOWN')
            ORDER BY 1
            """,
            (scan_id,),
        ).fetchall()
        incomplete = sum(
            cell_status.get(status, 0) for status in ("planned", "running", "failed")
        ) + sum(
            offer_status.get(status, 0) for status in ("planned", "running", "failed")
        ) + unpriced_stocked_pairs
        return {
            "complete": incomplete == 0 and bool(cell_rows),
            "cells": cell_status,
            "saturated_cells": saturated,
            "offers": offer_status,
            "discovered_stores": int(discovery_row[0]),
            "stocked_store_variant_pairs": int(discovery_row[1]),
            "unpriced_stocked_store_variant_pairs": unpriced_stocked_pairs,
            "discovered_store_states": {
                str(state): int(count) for state, count in state_rows
            },
        }

    def scan_listing_count(self, scan_id: int) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM observations WHERE scan_id = ?", (scan_id,)
            ).fetchone()[0]
        )

    def save_store_catalog_audit(
        self,
        *,
        scan_id: int,
        source_url: str,
        started_at: str,
        status: str,
        page_count: int | None = None,
        official_us_store_count: int | None = None,
        official_conus_store_count: int | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"complete", "failed"}:
            raise ValueError(f"Unsupported store catalog audit status: {status}")
        if status == "complete" and (
            page_count is None
            or official_us_store_count is None
            or official_conus_store_count is None
        ):
            raise ValueError("A complete store catalog audit requires all counts")
        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO store_catalog_audits (
                scan_id, source_url, started_at, finished_at, status, page_count,
                official_us_store_count, official_conus_store_count, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id) DO UPDATE SET
                source_url = excluded.source_url,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                status = excluded.status,
                page_count = excluded.page_count,
                official_us_store_count = excluded.official_us_store_count,
                official_conus_store_count = excluded.official_conus_store_count,
                error = excluded.error
            """,
            (
                scan_id,
                source_url,
                started_at,
                finished_at,
                status,
                page_count,
                official_us_store_count,
                official_conus_store_count,
                error[:1000] if error else None,
            ),
        )
        self.conn.commit()

    def store_catalog_summary(self, scan_id: int) -> dict[str, object]:
        current = self.conn.execute(
            """
            SELECT scan_id, source_url, finished_at, status, page_count,
                   official_us_store_count, official_conus_store_count, error
            FROM store_catalog_audits
            WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchone()
        latest_complete = self.conn.execute(
            """
            SELECT scan_id, source_url, finished_at, status, page_count,
                   official_us_store_count, official_conus_store_count, error
            FROM store_catalog_audits
            WHERE scan_id <= ? AND status = 'complete'
            ORDER BY scan_id DESC
            LIMIT 1
            """,
            (scan_id,),
        ).fetchone()
        source = current or latest_complete
        return {
            "audit_status": current[3] if current else "not_run",
            "official_us_store_count": (
                int(latest_complete[5]) if latest_complete is not None else None
            ),
            "official_conus_store_count": (
                int(latest_complete[6]) if latest_complete is not None else None
            ),
            "source_url": latest_complete[1] if latest_complete is not None else None,
            "audited_at": latest_complete[2] if latest_complete is not None else None,
            "audit_scan_id": (
                int(latest_complete[0]) if latest_complete is not None else None
            ),
            "page_count": (
                int(latest_complete[4]) if latest_complete is not None else None
            ),
            "stale": bool(
                latest_complete is not None and int(latest_complete[0]) != scan_id
            ),
            "error": current[7] if current is not None else None,
            "has_record": source is not None,
        }

    def listings_for_scan(self, scan_id: int) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT current.payload_json,
                   COALESCE(
                       (
                           SELECT MIN(history.observed_at)
                           FROM observations AS history
                           WHERE history.model_key = current.model_key
                             AND history.upc IS current.upc
                             AND history.store_id IS current.store_id
                             AND history.source = current.source
                             AND CAST(history.sale_price_usd AS NUMERIC)
                                 = CAST(current.sale_price_usd AS NUMERIC)
                             AND (
                                 history.scan_id IS NULL
                                 OR history.scan_id <= current.scan_id
                             )
                       ),
                       current.observed_at
                   ) AS first_seen_at
            FROM observations AS current
            WHERE current.scan_id = ?
            ORDER BY CAST(current.sale_price_usd AS REAL),
                     current.model_key,
                     current.retailer
            """,
            (scan_id,),
        ).fetchall()
        listings = []
        for payload_json, first_seen_at in rows:
            listing = json.loads(payload_json)
            listing["first_seen_at"] = first_seen_at
            listings.append(listing)
        return listings

    def scan_record(self, scan_id: int) -> dict[str, object]:
        row = self.conn.execute(
            """
            SELECT id, started_at, finished_at, profile, listing_count,
                   error_count, note_count, status, coverage_status, details_json
            FROM scan_runs
            WHERE id = ?
            """,
            (scan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown scan id: {scan_id}")
        return {
            "scan_id": row[0],
            "started_at": row[1],
            "finished_at": row[2],
            "profile": row[3],
            "listing_count": row[4],
            "error_count": row[5],
            "note_count": row[6],
            "status": row[7],
            "coverage_status": row[8],
            "details": json.loads(row[9]),
        }

    def previous_finished_scan(self, scan_id: int, *, profile: str) -> int | None:
        row = self.conn.execute(
            """
            SELECT id
            FROM scan_runs
            WHERE id < ? AND profile = ?
              AND status = 'ok' AND coverage_status = 'complete'
            ORDER BY id DESC
            LIMIT 1
            """,
            (scan_id, profile),
        ).fetchone()
        return int(row[0]) if row else None

    def previous_low(
        self,
        model_key: str,
        *,
        size: str | None = None,
        verified_only: bool = True,
        exclude_scan_id: int | None = None,
    ) -> Decimal | None:
        clauses = [
            "model_key = ?",
            "sale_price_usd IS NOT NULL",
            "in_stock = 1",
        ]
        args: list[object] = [model_key]
        if size:
            clauses.append("size = ?")
            args.append(size)
        if verified_only:
            clauses.extend(["size_verified = 1", "stock_verified = 1"])
        if exclude_scan_id is not None:
            clauses.append("(scan_id IS NULL OR scan_id != ?)")
            args.append(exclude_scan_id)
        rows = self.conn.execute(
            f"SELECT sale_price_usd FROM observations WHERE {' AND '.join(clauses)}",
            args,
        ).fetchall()
        values = [Decimal(row[0]) for row in rows if row[0] is not None]
        return min(values) if values else None

    def has_alert(self, alert_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM alerts WHERE alert_key = ? LIMIT 1", (alert_key,)
        ).fetchone()
        return row is not None

    def enqueue_alert(self, alert_key: str, alert: Alert, message: str) -> bool:
        payload = {
            "kind": alert.kind,
            "reason": alert.reason,
            "baseline_usd": str(alert.baseline_usd),
            "previous_low_usd": (
                str(alert.previous_low_usd) if alert.previous_low_usd is not None else None
            ),
            "listing": alert.listing.as_json_dict(),
        }
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO alerts
                (alert_key, kind, model_key, reason, payload_json, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                alert_key,
                alert.kind,
                alert.listing.model_key,
                alert.reason,
                json.dumps(payload, sort_keys=True),
                message,
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def mark_alert_sent(self, alert_key: str) -> None:
        self.conn.execute(
            """
            UPDATE alerts
            SET status = 'sent', attempts = attempts + 1,
                sent_at = ?, last_error = NULL
            WHERE alert_key = ?
            """,
            (datetime.now(UTC).isoformat(timespec="seconds"), alert_key),
        )
        self.conn.commit()

    def mark_alert_failed(self, alert_key: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE alerts
            SET status = 'pending', attempts = attempts + 1, last_error = ?
            WHERE alert_key = ?
            """,
            (error[:1000], alert_key),
        )
        self.conn.commit()

    def pending_alerts(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT alert_key, message
            FROM alerts
            WHERE status = 'pending'
            ORDER BY id
            """
        ).fetchall()
        return [(row[0], row[1]) for row in rows if row[1]]

    def save_scan_run(
        self,
        *,
        started_at: str,
        listing_count: int,
        errors: list[str],
        notes: list[str],
    ) -> None:
        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        status = "degraded" if errors else "ok"
        self.conn.execute(
            """
            INSERT INTO scan_runs (
                started_at, finished_at, profile, config_hash, listing_count,
                error_count, note_count, status, coverage_status, details_json
            ) VALUES (?, ?, 'legacy', '', ?, ?, ?, ?, 'unknown', ?)
            """,
            (
                started_at,
                finished_at,
                listing_count,
                len(errors),
                len(notes),
                status,
                json.dumps({"errors": errors, "notes": notes}, sort_keys=True),
            ),
        )
        self.conn.commit()

    def finish_scan(
        self,
        scan_id: int,
        *,
        errors: list[str],
        notes: list[str],
    ) -> None:
        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        coverage = self.coverage_summary(scan_id)
        coverage_status = "complete" if coverage["complete"] else "partial"
        status = "ok" if not errors and coverage["complete"] else "degraded"
        self.conn.execute(
            """
            UPDATE scan_runs
            SET finished_at = ?, listing_count = ?, error_count = ?,
                note_count = ?, status = ?, coverage_status = ?, details_json = ?
            WHERE id = ?
            """,
            (
                finished_at,
                self.scan_listing_count(scan_id),
                len(errors),
                len(notes),
                status,
                coverage_status,
                json.dumps(
                    {"errors": errors, "notes": notes, "coverage": coverage},
                    sort_keys=True,
                ),
                scan_id,
            ),
        )
        self.conn.commit()

    def save_report(
        self,
        *,
        scan_id: int,
        report_date: str,
        status: str,
        markdown_path: str,
        json_path: str,
        delivery_message: str,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO daily_reports (
                scan_id, report_date, status, markdown_path, json_path, created_at,
                delivery_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id) DO UPDATE SET
                report_date = excluded.report_date,
                status = excluded.status,
                markdown_path = excluded.markdown_path,
                json_path = excluded.json_path,
                created_at = excluded.created_at,
                delivery_message = excluded.delivery_message
            """,
            (
                scan_id,
                report_date,
                status,
                markdown_path,
                json_path,
                now,
                delivery_message,
            ),
        )
        self.conn.commit()

    def pending_report_deliveries(
        self,
        scan_id: int | None = None,
    ) -> list[tuple[int, str, str, str]]:
        query = """
            SELECT scan_id, delivery_message, markdown_path, json_path
            FROM daily_reports
            WHERE delivery_status = 'pending' AND delivery_message IS NOT NULL
        """
        parameters: tuple[object, ...] = ()
        if scan_id is not None:
            query += " AND scan_id = ?"
            parameters = (scan_id,)
        query += " ORDER BY id"
        rows = self.conn.execute(query, parameters).fetchall()
        return [
            (int(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in rows
        ]

    def mark_report_sent(self, scan_id: int) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.conn.execute(
            """
            UPDATE daily_reports
            SET delivery_status = 'sent',
                delivery_attempts = delivery_attempts + 1,
                sent_at = ?, last_error = NULL
            WHERE scan_id = ?
            """,
            (now, scan_id),
        )
        self.conn.commit()

    def mark_report_failed(self, scan_id: int, error: str) -> None:
        self.conn.execute(
            """
            UPDATE daily_reports
            SET delivery_status = 'pending',
                delivery_attempts = delivery_attempts + 1,
                last_error = ?
            WHERE scan_id = ?
            """,
            (error[:1000], scan_id),
        )
        self.conn.commit()

    def status(self) -> dict[str, object]:
        run = self.conn.execute(
            """
            SELECT id, started_at, finished_at, profile, listing_count, error_count,
                   note_count, status, coverage_status
            FROM scan_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status = 'pending'"
        ).fetchone()[0]
        observations = self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        return {
            "last_run": (
                {
                    "scan_id": run[0],
                    "started_at": run[1],
                    "finished_at": run[2],
                    "profile": run[3],
                    "listing_count": run[4],
                    "error_count": run[5],
                    "note_count": run[6],
                    "status": run[7],
                    "coverage_status": run[8],
                }
                if run
                else None
            ),
            "pending_alerts": pending,
            "observation_count": observations,
        }
