"""Recompute the verified 2026-09-24 baseline under the new due-only rule."""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.classification_pipeline import calculate_query_snapshot
from core.receipt_bootstrap import convert_receipt_state


WAREHOUSE_ID = "29871293290508390"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--inventory-identities", required=True, type=Path)
    args = parser.parse_args()

    package = args.package
    inputs = json.loads((package / "source-snapshot/sanitized-inputs.json").read_text())
    expected = json.loads((package / "source-snapshot/sanitized-output.json").read_text())
    raw_state = json.loads((package / "source-snapshot/原始-入库批次.json").read_text())
    mapping = json.loads((package / "账本与API身份映射.json").read_text())
    identities = json.loads(args.inventory_identities.read_text())
    profiles = {str(row["profile_id"]) for row in identities if row.get("profile_id")}
    if len(profiles) != 1:
        raise RuntimeError("库存身份包含多个账套")
    profile = next(iter(profiles))
    state = convert_receipt_state(raw_state, mapping, identities, profile, WAREHOUSE_ID)

    by_code = defaultdict(set)
    for row in identities:
        if str(row.get("warehouse_id")) == WAREHOUSE_ID and row.get("sku") and row.get("sku_id"):
            by_code[str(row["sku"]).upper()].add((
                str(row["product_id"]), str(row["sku_id"]), str(row["base_unit"])))

    def identity(sku):
        candidates = by_code[str(sku).upper()]
        if len(candidates) != 1:
            raise RuntimeError("商品编码无法唯一关联 API 身份")
        product_id, sku_id, base_unit = next(iter(candidates))
        return dict(profile_id=profile, warehouse_id=WAREHOUSE_ID,
                    product_id=product_id, sku_id=sku_id, base_unit=base_unit)

    inventory = []
    for row in inputs["inventory"]:
        inventory.append({**identity(row["sku"]),
            "physical_qty": str(row["inventory_quantity"]),
            "saleable_qty": str(row["saleable_quantity"])})

    grouped = {}
    for index, row in enumerate(inputs["sales"], 1):
        number = str(row["order"])
        due = str(row["export_due_date"])
        order = grouped.setdefault(number, {
            "order_no": number,
            "order_date": str(row["order_date"]),
            "delivery_date": due,
            "lines": [],
        })
        if order["delivery_date"] != due:
            raise RuntimeError("同一订单存在多个交期")
        order["lines"].append({**identity(row["sku"]),
            "line_id": str(index),
            "product_code": str(row["sku"]).upper(),
            "open_qty": str(row["unfinished_quantity"]),
            "delivery_date": due})

    receipt_events = []
    for event in state["events"].values():
        event_profile, event_warehouse, sku_id, base_unit = json.loads(event["warehouse"])
        receipt_events.append({
            "event_id": event["id"],
            "document_no": event["document"],
            "posted_date": event["date"],
            "type": event["type"],
            "profile_id": event_profile,
            "warehouse_id": event_warehouse,
            "product_id": event["sku"],
            "sku_id": sku_id,
            "base_unit": base_unit,
            "quantity": event["quantity"],
        })

    snapshot = {
        "rules_contract": "v2",
        "query_complete": True,
        "profile_id": profile,
        "orders": list(grouped.values()),
        "inventory": inventory,
        "receipt_events_complete": True,
        "receipt_period": ["2026-08-07", "2026-09-24"],
        "receipt_events": receipt_events,
    }
    result = calculate_query_snapshot(snapshot, state, today=date(2026, 9, 24))

    new_by_order = {row["tradeid"]: row["freightname"] for row in result["plans"]}
    old_by_order = {}
    for row in expected["rows"]:
        old_by_order.setdefault(str(row["order"]), str(row["order_category"]))
        if old_by_order[str(row["order"])] != str(row["order_category"]):
            raise RuntimeError("旧输出同一订单分类不一致")
    transitions = Counter((old_by_order[number], new_by_order.get(number, "missing"))
                          for number in sorted(old_by_order))
    unrelated = sum(count for (old, new), count in transitions.items()
                    if old != new and "催货" not in {old, new})
    print(json.dumps({
        "orders": len(grouped),
        "lines": sum(len(order["lines"]) for order in grouped.values()),
        "receipt_events": len(receipt_events),
        "plans": len(result["plans"]),
        "blocked_orders": len(result["blocked_orders"]),
        "blocked_reasons": dict(sorted(Counter(
            item["reason"] for item in result["blocked_orders"]).items())),
        "blocked_codes": dict(sorted(Counter(
            item["code"] for item in result["issues"]).items())),
        "old_counts": dict(sorted(Counter(old_by_order.values()).items())),
        "new_counts": dict(sorted(Counter(new_by_order.values()).items())),
        "changed_orders": sum(count for (old, new), count in transitions.items() if old != new),
        "non_urging_rule_changes": unrelated,
        "transitions": {f"{old} -> {new}": count for (old, new), count in sorted(transitions.items()) if old != new},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
