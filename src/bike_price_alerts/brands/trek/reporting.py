from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ...core.geography import CONUS_STATE_CODES
from .models import AppConfig
from .storage import Storage

PACIFIC_TIME = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class DailyReport:
    scan_id: int
    status: str
    markdown_path: Path
    json_path: Path
    payload: dict[str, Any]
    delivery_message: str


def _price(item: dict[str, Any]) -> Decimal:
    value = item.get("sale_price_usd")
    return Decimal(str(value)) if value is not None else Decimal("Infinity")


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result > 0 else None


def _discount_pct(msrp: Decimal | None, sale: Decimal | None) -> Decimal | None:
    if not msrp or not sale or msrp <= 0:
        return None
    return ((msrp - sale) / msrp * 100).quantize(Decimal("0.1"))


def _apply_reporting_msrps(
    listings: list[dict[str, Any]],
    config: AppConfig,
) -> None:
    msrps = {watch.key: watch.msrp_usd for watch in config.watches}
    for item in listings:
        sale = _decimal_or_none(item.get("sale_price_usd"))
        msrp = _decimal_or_none(item.get("msrp_usd"))
        configured_msrp = msrps.get(str(item.get("model_key") or ""))
        if configured_msrp is not None and (msrp is None or msrp < configured_msrp):
            msrp = configured_msrp
            item["msrp_usd"] = str(msrp)
        discount = _discount_pct(msrp, sale)
        item["discount_pct"] = str(discount) if discount is not None else None


MIN_SHORTLIST_DISCOUNT_PCT = Decimal(10)
SHORTLIST_PRICE_LEVELS = 5


def _shortlist_offers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep qualifying offers from the five lowest overall price levels."""
    ordered = sorted(rows, key=_price)
    price_levels: list[Decimal] = []
    for row in ordered:
        price = _price(row)
        if price not in price_levels:
            price_levels.append(price)
        if len(price_levels) == SHORTLIST_PRICE_LEVELS:
            break
    included = set(price_levels)
    return [
        row
        for row in ordered
        if _price(row) in included
        and Decimal(str(row.get("discount_pct") or 0))
        >= MIN_SHORTLIST_DISCOUNT_PCT
    ]


def _offer_key(item: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(item.get("model_key") or ""),
        str(item.get("upc") or ""),
        int(item.get("store_id") or 0),
    )


def _location(item: dict[str, Any]) -> str:
    parts = [
        str(value).strip()
        for value in (item.get("city"), item.get("state"))
        if value and str(value).strip()
    ]
    return ", ".join(parts)


def _money(value: Any) -> str:
    return f"${Decimal(str(value)):,.2f}"


def _discount(item: dict[str, Any]) -> str:
    value = item.get("discount_pct")
    return f"（较 MSRP 低 {value}%）" if value is not None else ""


def _report_link() -> str | None:
    server = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if server and repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return None


def _scan_reference(scan: dict[str, Any]) -> dict[str, Any]:
    started = datetime.fromisoformat(str(scan["started_at"])).astimezone(PACIFIC_TIME)
    finished = datetime.fromisoformat(str(scan["finished_at"])).astimezone(PACIFIC_TIME)
    if started.date() == finished.date():
        time_label = f"{started:%Y-%m-%d %H:%M}–{finished:%H:%M} {finished:%Z}"
    else:
        time_label = (
            f"{started:%Y-%m-%d %H:%M %Z}–"
            f"{finished:%Y-%m-%d %H:%M %Z}"
        )
    return {
        "scan_id": int(scan["scan_id"]),
        "started_at": str(scan["started_at"]),
        "finished_at": str(scan["finished_at"]),
        "label": time_label,
    }


def _pacific_date(value: Any) -> str | None:
    if not value:
        return None
    return (
        datetime.fromisoformat(str(value))
        .astimezone(PACIFIC_TIME)
        .date()
        .isoformat()
    )


def _annotate_offer_history(
    listings: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    comparison_status: str,
) -> None:
    new_offer_keys = (
        {
            _offer_key(change["listing"])
            for change in changes
            if change["kind"] == "new_offer"
        }
        if comparison_status == "compared"
        else set()
    )
    for listing in listings:
        listing["first_seen_date"] = _pacific_date(
            listing.get("first_seen_at") or listing.get("observed_at")
        )
        listing["is_new_offer"] = _offer_key(listing) in new_offer_keys


def _changes(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_by_key = {_offer_key(item): item for item in current}
    previous_by_key = {_offer_key(item): item for item in previous}
    changes: list[dict[str, Any]] = []
    for key, item in current_by_key.items():
        old = previous_by_key.get(key)
        if old is None:
            changes.append({"kind": "new_offer", "listing": item})
            continue
        old_price = _price(old)
        new_price = _price(item)
        if new_price < old_price:
            changes.append(
                {
                    "kind": "price_drop",
                    "old_price_usd": str(old_price),
                    "new_price_usd": str(new_price),
                    "listing": item,
                }
            )
        elif new_price > old_price:
            changes.append(
                {
                    "kind": "price_increase",
                    "old_price_usd": str(old_price),
                    "new_price_usd": str(new_price),
                    "listing": item,
                }
            )
    for key, item in previous_by_key.items():
        if key not in current_by_key:
            changes.append({"kind": "offer_disappeared", "listing": item})
    order = {"price_drop": 0, "new_offer": 1, "price_increase": 2, "offer_disappeared": 3}
    return sorted(changes, key=lambda item: (order[item["kind"]], _price(item["listing"])))


def generate_daily_report(
    storage: Storage,
    config: AppConfig,
    *,
    scan_id: int,
    output_dir: Path,
) -> DailyReport:
    scan = storage.scan_record(scan_id)
    coverage = storage.coverage_summary(scan_id)
    listings = [
        item
        for item in storage.listings_for_scan(scan_id)
        if item.get("sale_price_usd") is not None
        and item.get("in_stock")
        and item.get("size_verified")
        and item.get("stock_verified")
    ]
    for item in listings:
        if item.get("city"):
            item["city"] = str(item["city"]).strip()
        if item.get("state"):
            item["state"] = str(item["state"]).strip().upper()
    _apply_reporting_msrps(listings, config)
    discovered_states = set(coverage["discovered_store_states"]) - {"UNKNOWN"}
    missing_store_states = sorted(
        CONUS_STATE_CODES - discovered_states
        if str(scan["profile"]) == "conus"
        else set()
    )
    coverage["missing_store_states"] = missing_store_states
    store_catalog = storage.store_catalog_summary(scan_id)
    store_counts = {
        "official_conus_store_count": store_catalog[
            "official_conus_store_count"
        ],
        "locally_scannable_store_count": coverage["discovered_stores"],
        "official_audit": store_catalog,
    }
    report_status = (
        "COMPLETE"
        if coverage["complete"] and not missing_store_states
        else "PARTIAL"
    )
    national_claim_allowed = bool(
        report_status == "COMPLETE" and str(scan["profile"]) == "conus"
    )
    previous_id = storage.previous_finished_scan(scan_id, profile=str(scan["profile"]))
    previous_scan = storage.scan_record(previous_id) if previous_id is not None else None
    if report_status != "COMPLETE":
        comparison_status = "skipped_current_partial"
        changes: list[dict[str, Any]] = []
    elif previous_id is None:
        comparison_status = "no_previous_complete_scan"
        changes = []
    else:
        comparison_status = "compared"
        changes = _changes(listings, storage.listings_for_scan(previous_id))
    comparison = {
        "status": comparison_status,
        "profile": str(scan["profile"]),
        "current_scan": _scan_reference(scan),
        "previous_scan": (
            _scan_reference(previous_scan) if previous_scan is not None else None
        ),
    }
    _annotate_offer_history(listings, changes, comparison_status)
    known_state_count = len(discovered_states)
    generated_at = datetime.now(UTC)
    report_date = (
        datetime.fromisoformat(str(scan["finished_at"]))
        .astimezone(PACIFIC_TIME)
        .date()
        .isoformat()
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for listing in listings:
        grouped.setdefault(str(listing["model_key"]), []).append(listing)
    for rows in grouped.values():
        rows.sort(key=_price)

    models: list[dict[str, Any]] = []
    for watch in config.watches:
        rows = grouped.get(watch.key, [])
        shortlist = _shortlist_offers(rows)
        models.append(
            {
                "model_key": watch.key,
                "canonical_name": watch.canonical_name,
                "target_sizes": list(watch.allowed_sizes),
                "store_count": len(
                    {
                        int(row["store_id"])
                        for row in rows
                        if row.get("store_id") is not None
                    }
                ),
                "upc_count": len(
                    {str(row["upc"]) for row in rows if row.get("upc")}
                ),
                # One persisted offer is unique at scan/model/source/store/UPC
                # grain. A store with multiple matching UPCs contributes more
                # than one offer but only one store above.
                "offer_count": len(rows),
                "lowest": rows[0] if rows else None,
                "shortlist_lowest": shortlist[0] if shortlist else None,
                "top_offers": shortlist,
            }
        )

    payload: dict[str, Any] = {
        "schema_version": 4,
        "report_date": report_date,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "status": report_status,
        "national_claim_allowed": national_claim_allowed,
        "scan": scan,
        "coverage": coverage,
        "store_counts": store_counts,
        "previous_scan_id": previous_id,
        "comparison": comparison,
        "models": models,
        "changes": changes,
    }

    title = f"Trek 每日价格扫描 — {report_date}"
    lines = [
        f"# {title}",
        "",
        f"状态：**{report_status}**",
        "",
    ]
    if national_claim_allowed:
        lines.append("本报告中的最低价可表述为本次美国本土完整扫描的最低已验证价格。")
    elif report_status == "COMPLETE":
        lines.append(
            f"`{scan['profile']}` profile 已完整执行，但它不是美国本土全国扫描；"
            "以下最低价仅适用于本次 profile 的覆盖范围。"
        )
    else:
        lines.append(
            "覆盖不完整：以下结果仅是已成功扫描区域中的最低已验证价格，"
            "不能表述为美国本土最低价。"
        )
    lines.extend(
        [
            "",
            "## 覆盖与健康",
            "",
            f"- 扫描时间：{comparison['current_scan']['label']}",
            f"- Profile：`{scan['profile']}`",
            f"- 成功报价：{len(listings)}",
            f"- Coverage cells：`{json.dumps(coverage['cells'], sort_keys=True)}`",
            f"- Offer tasks：`{json.dumps(coverage['offers'], sort_keys=True)}`",
            (
                "- Trek 官方美国本土店铺："
                + (
                    str(store_counts["official_conus_store_count"])
                    if store_counts["official_conus_store_count"] is not None
                    else "暂不可用"
                )
            ),
            f"- Locally 可扫描店铺：{store_counts['locally_scannable_store_count']}",
            f"- 有门店标记的州/DC：{known_state_count}",
            (
                "- 缺失门店标记的州/DC："
                + (", ".join(missing_store_states) if missing_store_states else "无")
            ),
            f"- 有库存的门店-变体对：{coverage['stocked_store_variant_pairs']}",
            (
                "- 已发现但尚未完成逐店取价："
                f"{coverage['unpriced_stocked_store_variant_pairs']}"
            ),
            f"- 饱和网格（含已细分父节点）：{coverage['saturated_cells']}",
            f"- Source errors：{scan['error_count']}",
            "",
            "## 各车型最低报价",
            "",
        ]
    )
    for model in models:
        lines.extend([f"### {model['canonical_name']}", ""])
        offers = model["top_offers"]
        if not offers:
            lines.extend(["本次扫描未发现目标尺码的已验证库存报价。", ""])
            continue
        lines.extend(
            [
                "| 价格 | MSRP | 门店 | 地点 | 颜色 | 尺码 | UPC |",
                "| ---: | ---: | --- | --- | --- | --- | --- |",
            ]
        )
        for item in offers:
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"[{_money(item['sale_price_usd'])}]({item['url']})",
                        _money(item["msrp_usd"]) if item.get("msrp_usd") else "—",
                        str(item.get("retailer") or "—").replace("|", "/"),
                        _location(item) or "—",
                        str(item.get("color") or "—").replace("|", "/"),
                        str(item.get("size") or "—"),
                        str(item.get("upc") or "—"),
                    ]
                )
                + " |"
            )
        lines.append("")

    lines.extend(
        [
            f"## 较上次完整 {scan['profile']} 扫描的变化",
            "",
        ]
    )
    if comparison_status == "skipped_current_partial":
        lines.extend(
            [
                (
                    "本次扫描为 `PARTIAL`；为避免把未覆盖区域误判为库存消失，"
                    "本报告未计算变化。"
                ),
                "",
            ]
        )
    elif comparison_status == "no_previous_complete_scan":
        lines.extend(["没有可比较的历史完整扫描。", ""])
    else:
        assert previous_scan is not None
        lines.extend(
            [
                (
                    f"对比基准：{comparison['previous_scan']['label']} → "
                    f"{comparison['current_scan']['label']}"
                ),
                "",
            ]
        )
    if comparison_status == "compared" and not changes:
        lines.extend(["没有检测到报价变化。", ""])
    elif comparison_status == "compared":
        labels = {
            "new_offer": "新增库存",
            "price_drop": "降价",
            "price_increase": "涨价",
            "offer_disappeared": "库存消失",
        }
        for change in changes[:50]:
            item = change["listing"]
            detail = ""
            if change["kind"] in {"price_drop", "price_increase"}:
                detail = (
                    f" {_money(change['old_price_usd'])} → "
                    f"{_money(change['new_price_usd'])}"
                )
            lines.append(
                f"- {labels[change['kind']]}：{item.get('canonical_name')} / "
                f"{item.get('color') or '未知颜色'} / {item.get('size') or '未知尺码'} / "
                f"{item.get('retailer') or '未知门店'}{detail}"
            )
        lines.append("")

    lines.extend(
        [
            "## 数据质量规则",
            "",
            "- 仅纳入 exact UPC、目标尺码、当前库存和逐店价格均验证成功的 Locally 报价。",
            (
                "- 任何失败、未完成、未取价库存关系、州级门店标记缺失，"
                "或达到最小网格仍饱和的任务都会使报告成为 `PARTIAL`。"
            ),
            "- 价格和库存随时可能变化，购买或 price match 前请重新打开店铺链接确认。",
            "",
        ]
    )
    markdown = "\n".join(lines)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{report_date}-scan-{scan_id}"
    markdown_path = output_dir / f"{stem}.md"
    json_path = output_dir / f"{stem}.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary_lines = [
        f"🚲 Trek 每日价格扫描 {report_date}",
        (
            f"状态：{report_status} | {comparison['current_scan']['label']} | "
            f"已验证报价 {len(listings)}"
        ),
    ]
    if store_counts["official_conus_store_count"] is not None:
        summary_lines.append(
            f"门店：Locally 可扫描 {store_counts['locally_scannable_store_count']} / "
            f"Trek 官方美国本土 {store_counts['official_conus_store_count']}"
        )
    for model in models:
        lowest = model["lowest"]
        if lowest:
            summary_lines.append(
                f"{model['canonical_name']}：{_money(lowest['sale_price_usd'])}"
                f"{_discount(lowest)}，{lowest.get('retailer') or '未知门店'}"
                f"（{lowest.get('color') or '未知颜色'} / {lowest.get('size') or '未知尺码'}）"
            )
        else:
            summary_lines.append(f"{model['canonical_name']}：未发现已验证目标库存")
    link = _report_link()
    if link:
        summary_lines.append(f"完整报告：{link}")
    if report_status == "PARTIAL":
        summary_lines.append("注意：覆盖不完整，以上不能称为美国本土最低价。")
    elif not national_claim_allowed:
        summary_lines.append(
            f"注意：{scan['profile']} profile 不是全国扫描，以上不能称为美国本土最低价。"
        )
    delivery_message = "\n".join(summary_lines)
    storage.save_report(
        scan_id=scan_id,
        report_date=report_date,
        status=report_status,
        markdown_path=str(markdown_path),
        json_path=str(json_path),
        delivery_message=delivery_message,
    )

    return DailyReport(
        scan_id=scan_id,
        status=report_status,
        markdown_path=markdown_path,
        json_path=json_path,
        payload=payload,
        delivery_message=delivery_message,
    )
