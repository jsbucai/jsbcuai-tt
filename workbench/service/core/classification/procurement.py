"""Due-date-only urging rule confirmed by the business owner on 2026-09-29."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import allocation


PACKING_DAYS = 3


def as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def rules():
    return {
        "basis": "sales_delivery_date_only",
        "packing_days": PACKING_DAYS,
        "calendar": "natural_days",
        "boundary_inclusive": True,
    }


def follow_up_date(due):
    due = as_date(due)
    if due is None:
        raise ValueError("销售交期无效")
    return due - dt.timedelta(days=PACKING_DAYS)


def classify(rows, sales, today):
    """Promote waiting supply to 催货 from delivery date minus three days.

    Physical stock consumes earlier due-date segments first. Only the part still
    waiting for incoming supply can trigger urging. Purchase order date and
    supplier are intentionally absent from this contract.
    """
    issues = set()
    sales_groups = defaultdict(list)
    for record in sales:
        qty = allocation.number(record.get("未完成数量"), "销售未完成数量")
        if qty < 0:
            raise ValueError("销售未完成数量为负，停止催货计算")
        if qty <= allocation.EPSILON:
            continue
        key = tuple(allocation.text(record.get(k)) for k in ("订单编号", "仓库", "商品编号"))
        sales_groups[key].append({
            "quantity": qty,
            "due": as_date(record.get("到货日期")),
            "source_row": record["_row"],
            "shadow_demand": record.get("_shadow_demand") is True,
        })

    for row in rows:
        demand = row["demand"]
        segments = sorted(
            sales_groups[(demand.order, *demand.key)],
            key=lambda item: (item["due"] or dt.date.max, item["source_row"]),
        )
        if abs(sum(item["quantity"] for item in segments) - demand.quantity) > allocation.EPSILON:
            raise ValueError(f"销售交期明细与原订单需求不一致，停止发布: {demand.order}")
        if any(item["due"] is None and not item["shadow_demand"] for item in segments):
            issues.add(f"销售单{demand.order}商品{demand.sku}缺少交期，保留原分类")

        row["purchase_allocations"] = []
        row["urging"] = []
        physical = row["actual"]
        pending = row["pending"]
        for segment in segments:
            ready = min(physical, segment["quantity"])
            physical -= ready
            waiting = min(pending, segment["quantity"] - ready)
            pending -= waiting
            if waiting <= allocation.EPSILON or segment["due"] is None:
                continue
            start = follow_up_date(segment["due"])
            evidence = {
                "sales_due": segment["due"],
                "urge_date": start,
                "source_row": segment["source_row"],
                "quantity": waiting,
                "rule": "交期前3个自然日（含当天）",
            }
            if today >= start:
                row["urging"].append(evidence)
        if pending > allocation.EPSILON:
            raise ValueError(f"订单待到货数量未分配完整: {demand.order}")
        if row["category"] == "待到货" and row["urging"]:
            row["category"] = "催货"

    allocation.assign_order_categories(rows)
    return {
        "urging_date": today.isoformat(),
        "urging_rules": rules(),
        "urging_orders": sorted({
            row["demand"].order for row in rows if row["order_category"] == "催货"
        }),
        "urging_data_issues": sorted(issues),
    }
