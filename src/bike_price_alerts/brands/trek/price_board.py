"""Compact JSON for the static Trek price lookup page."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from ...core.geography import CONUS_STATE_CODES
from ...core.parsing import infer_generation, infer_year

_GEN_RE = re.compile(r"\bGen\s*\d+\b", re.IGNORECASE)
# One bad store label must not fail the job. Above this share, the scan is broken.
OFFER_FAILURE_RATE_LIMIT = 0.01


def display_model(name: str) -> str:
    """Drop the Trek prefix and the generation so filters can group a family."""

    text = name.strip()
    if text.casefold().startswith("trek "):
        text = text[5:].strip()
    text = _GEN_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -")


def load_upc_lows(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    lows = payload.get("upc_lows") if isinstance(payload, dict) else None
    return lows if isinstance(lows, dict) else {}


def merge_upc_lows(
    previous: dict[str, Any],
    offers: list[dict[str, Any]],
    updated_at: str,
) -> dict[str, dict[str, str]]:
    """Keep the lowest in-stock price seen for each UPC across page refreshes."""

    lows: dict[str, dict[str, str]] = {}
    for upc, row in previous.items():
        if not isinstance(row, dict) or not row.get("lowest_usd") or not row.get("lowest_at"):
            continue
        lows[str(upc)] = {
            "lowest_usd": str(row["lowest_usd"]),
            "lowest_at": str(row["lowest_at"]),
        }
    today: dict[str, Decimal] = {}
    for offer in offers:
        upc = str(offer.get("upc") or "")
        if not upc:
            continue
        price = Decimal(str(offer["price_usd"]))
        if upc not in today or price < today[upc]:
            today[upc] = price
    for upc, price in today.items():
        prior = lows.get(upc)
        if prior is None or price < Decimal(prior["lowest_usd"]):
            lows[upc] = {"lowest_usd": f"{price:.2f}", "lowest_at": updated_at}
    return lows


def build_dataset(
    listings: list[dict[str, Any]],
    *,
    updated_at: str,
    profile: str,
    complete: bool,
    upc_lows: dict[str, Any] | None = None,
    requests: dict[str, int] | None = None,
    skipped_offers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    offers: list[dict[str, Any]] = []
    for listing in listings:
        if not listing.get("in_stock") or listing.get("sale_price_usd") is None:
            continue
        if not listing.get("size_verified") or not listing.get("stock_verified"):
            continue
        name = str(listing.get("canonical_name") or listing.get("model_key") or "")
        generation = listing.get("generation") or infer_generation(name)
        year = infer_year(name)
        offer: dict[str, Any] = {
            "model": display_model(name),
            "generation": generation,
            "size": listing.get("size"),
            "color": listing.get("color"),
            "price_usd": str(listing["sale_price_usd"]),
            "retailer": listing.get("retailer"),
            "city": listing.get("city"),
            "state": listing.get("state"),
            "url": listing.get("url"),
            "upc": listing.get("upc"),
        }
        if year is not None:
            offer["year"] = year
        offers.append(offer)
    offers.sort(
        key=lambda offer: (
            Decimal(str(offer["price_usd"])),
            str(offer.get("retailer") or ""),
            str(offer.get("url") or ""),
        )
    )
    for offer in offers:
        low = (upc_lows or {}).get(str(offer.get("upc") or ""))
        if isinstance(low, dict) and low.get("lowest_usd"):
            offer["lowest_seen_usd"] = str(low["lowest_usd"])
            offer["lowest_seen_at"] = str(low.get("lowest_at") or "")
    nationwide = bool(complete and profile == "conus")
    skipped = list(skipped_offers or [])
    if nationwide:
        note = "Cheapest verified in-stock prices from a complete contiguous-US scan."
        if skipped:
            noun = "offer was" if len(skipped) == 1 else "offers were"
            note += f" {len(skipped)} {noun} skipped because the store response did not verify."
    else:
        note = (
            "This file is a sample from the "
            f"{profile} profile, not a nationwide low."
        )
    return {
        "schema_version": 1,
        "updated_at": updated_at,
        "profile": profile,
        "nationwide": nationwide,
        "note": note,
        "skipped_offer_count": len(skipped),
        "skipped_offers": skipped,
        "offer_count": len(offers),
        "offers": offers,
        "upc_lows": upc_lows or {},
        "requests": requests or {},
    }


def _failed_outright(counts: dict[str, int]) -> bool:
    """Every attempt failed. Ignored price ranges are answers, not a dead model."""

    failed = int(counts.get("failed", 0))
    return (
        failed > 0
        and int(counts.get("complete", 0)) == 0
        and int(counts.get("ignored", 0)) == 0
    )


def publication_blockers(
    *,
    cells: dict[str, int],
    offers: dict[str, int],
    by_model: dict[str, dict[str, int]],
    by_state: dict[str, dict[str, int]],
    missing_states: list[str],
    unpriced_stocked_pairs: int,
    priced_offer_count: int,
    require_conus_states: bool,
) -> list[str]:
    """Reasons the scheduled price file must not be published.

    A single store can return a bad size label. That offer is skipped.
    The job fails when the map did not finish, a state or model produced
    only failures, offer failures exceed 1% of attempts, a stocked pair
    was never checked, or the file would be empty.
    """

    blockers: list[str] = []
    if not cells:
        blockers.append("scan recorded no coverage cells")
    finished_cells = int(cells.get("complete", 0)) + int(cells.get("subdivided", 0))
    unfinished = {
        status: int(cells.get(status, 0))
        for status in ("planned", "running", "failed")
        if int(cells.get(status, 0))
    }
    if unfinished:
        if finished_cells == 0 and set(unfinished) == {"failed"}:
            blockers.append(
                f"map coverage failed outright ({unfinished['failed']} failed cells)"
            )
        else:
            detail = ", ".join(f"{count} {status}" for status, count in unfinished.items())
            blockers.append(f"coverage did not finish: {detail}")
    hanging = {
        status: int(offers.get(status, 0))
        for status in ("planned", "running")
        if int(offers.get(status, 0))
    }
    if hanging:
        detail = ", ".join(f"{count} {status}" for status, count in hanging.items())
        blockers.append(f"offer checks did not finish: {detail}")
    failed = int(offers.get("failed", 0))
    complete = int(offers.get("complete", 0))
    ignored = int(offers.get("ignored", 0))
    attempted = failed + complete + ignored
    if attempted and failed * 100 > attempted:
        blockers.append(
            f"offer failures are {failed} of {attempted}, above 1% of attempted offers"
        )
    if attempted and complete == 0 and ignored == 0 and failed:
        blockers.append(f"price checks failed outright ({failed} failed offers)")
    for model, counts in sorted(by_model.items()):
        if _failed_outright(counts):
            blockers.append(
                f"model {model} failed outright ({int(counts.get('failed', 0))} failed offers)"
            )
    for state, counts in sorted(by_state.items()):
        if _failed_outright(counts):
            blockers.append(
                f"state {state} failed outright ({int(counts.get('failed', 0))} failed offers)"
            )
    if require_conus_states and missing_states:
        blockers.append("missing states: " + ", ".join(missing_states))
    if unpriced_stocked_pairs > failed:
        blockers.append(
            f"{unpriced_stocked_pairs - failed} in-stock store pairs were never priced"
        )
    if priced_offer_count <= 0:
        blockers.append("scan produced no verified prices")
    return blockers


def price_board_gate(storage: Any, scan_id: int, *, profile: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Decide whether this price-board scan may be published, and list skips."""

    coverage = storage.coverage_summary(scan_id)
    offers = {str(status): int(count) for status, count in coverage["offers"].items()}
    cells = {str(status): int(count) for status, count in coverage["cells"].items()}
    by_model = _grouped_counts(
        storage.conn.execute(
            """
            SELECT model_key, status, COUNT(*)
            FROM offer_tasks
            WHERE scan_id = ?
            GROUP BY model_key, status
            """,
            (scan_id,),
        ).fetchall()
    )
    by_state = _grouped_counts(
        storage.conn.execute(
            """
            SELECT COALESCE(NULLIF(UPPER(TRIM(s.state)), ''), 'UNKNOWN'),
                   o.status,
                   COUNT(*)
            FROM offer_tasks AS o
            LEFT JOIN stores AS s ON s.store_id = o.store_id
            WHERE o.scan_id = ?
            GROUP BY 1, o.status
            """,
            (scan_id,),
        ).fetchall()
    )
    discovered = set(coverage["discovered_store_states"]) - {"UNKNOWN"}
    missing = sorted(CONUS_STATE_CODES - discovered) if profile == "conus" else []
    skipped = [
        {
            "model_key": str(model_key),
            "upc": str(upc),
            "store_id": int(store_id),
            "retailer": retailer,
            "city": city,
            "state": state,
            "reason": str(reason or ""),
        }
        for model_key, upc, store_id, reason, retailer, city, state in storage.conn.execute(
            """
            SELECT o.model_key, o.upc, o.store_id, o.error, s.name, s.city, s.state
            FROM offer_tasks AS o
            LEFT JOIN stores AS s ON s.store_id = o.store_id
            WHERE o.scan_id = ? AND o.status = 'failed'
            ORDER BY o.model_key, o.upc, o.store_id
            """,
            (scan_id,),
        )
    ]
    blockers = publication_blockers(
        cells=cells,
        offers=offers,
        by_model=by_model,
        by_state=by_state,
        missing_states=missing,
        unpriced_stocked_pairs=int(coverage["unpriced_stocked_store_variant_pairs"]),
        priced_offer_count=storage.scan_listing_count(scan_id),
        require_conus_states=profile == "conus",
    )
    return blockers, skipped


def _grouped_counts(rows: list[tuple[Any, ...]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for key, status, count in rows:
        grouped.setdefault(str(key), {})[str(status)] = int(count)
    return grouped


def write_dataset(path: Path, dataset: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
