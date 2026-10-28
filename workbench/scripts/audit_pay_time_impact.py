"""Measure classification impact if Qimen pay_time replaces legacy order date."""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.classification_pipeline import calculate_query_snapshot
from core.erp_api import ErpClient
from core.erp_source import QIMEN, START, END, collection, text
from core.receipt_bootstrap import convert_receipt_state

WAREHOUSE_ID = "29871293290508390"


def day(value):
    value = text(value)
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def main():
    package = Path(os.environ["UL_BOOTSTRAP_PACKAGE"])
    identities = json.loads(Path(os.environ["UL_INVENTORY_IDENTITIES"]).read_text())
    inputs = json.loads((package / "source-snapshot/sanitized-inputs.json").read_text())
    raw = json.loads((package / "source-snapshot/原始-入库批次.json").read_text())
    mapping = json.loads((package / "账本与API身份映射.json").read_text())
    profile = next(iter({str(row["profile_id"]) for row in identities if row.get("profile_id")}))
    state = convert_receipt_state(raw, mapping, identities, profile, WAREHOUSE_ID)

    by_code = defaultdict(set)
    for row in identities:
        if str(row.get("warehouse_id")) == WAREHOUSE_ID and row.get("sku") and row.get("sku_id"):
            by_code[str(row["sku"]).upper()].add((str(row["product_id"]),
                str(row["sku_id"]), str(row["base_unit"])))
    def identity(sku):
        candidates = by_code[str(sku).upper()]
        if len(candidates) != 1:
            raise RuntimeError("SKU identity is not unique")
        product, sku_id, unit = next(iter(candidates))
        return {"profile_id": profile, "warehouse_id": WAREHOUSE_ID,
                "product_id": product, "sku_id": sku_id, "base_unit": unit}

    client = ErpClient(os.environ["UL_ERP_VAULT"])
    pay_dates = {}
    received = expected = 0
    for page in range(1, 100):
        response = client.call(QIMEN, {"pageNo": page, "pageSize": 100,
            "extendProps": {"profile_id": int(profile), "query_time_type": 2,
                "start_time": START, "end_time": END, "bill_status": "1,2,3,4,5"}})
        items = collection(response.get("trade_orders"), "trade_orders")
        total = int(response.get("TotalResults", response.get("totalResults")))
        if expected and total != expected:
            raise RuntimeError("Qimen total changed")
        expected = total
        for row in items:
            head = row["trade_order"]
            order, value = text(head.get("trade_id")), day(head.get("pay_time"))
            if order in pay_dates and pay_dates[order] != value:
                raise RuntimeError("Qimen pay date conflict")
            pay_dates[order] = value
        received += len(items)
        if received == total:
            break

    inventory = [{**identity(row["sku"]), "physical_qty": str(row["inventory_quantity"]),
                  "saleable_qty": str(row["saleable_quantity"])} for row in inputs["inventory"]]
    receipt_events = []
    for event in state["events"].values():
        event_profile, event_warehouse, sku_id, unit = json.loads(event["warehouse"])
        receipt_events.append({"event_id": event["id"], "document_no": event["document"],
            "posted_date": event["date"], "type": event["type"], "profile_id": event_profile,
            "warehouse_id": event_warehouse, "product_id": event["sku"], "sku_id": sku_id,
            "base_unit": unit, "quantity": event["quantity"]})

    def calculate(use_pay):
        grouped = {}
        replacements = 0
        for index, row in enumerate(inputs["sales"], 1):
            number = str(row["order"])
            original = str(row["order_date"])
            selected = pay_dates.get(number) if use_pay else None
            order_date = selected or original
            replacements += bool(use_pay and selected and selected != original)
            order = grouped.setdefault(number, {"order_no": number, "order_date": order_date,
                "delivery_date": str(row["export_due_date"]), "lines": []})
            if order["order_date"] != order_date:
                raise RuntimeError("one order has multiple dates")
            order["lines"].append({**identity(row["sku"]), "line_id": str(index),
                "product_code": str(row["sku"]).upper(),
                "open_qty": str(row["unfinished_quantity"]),
                "delivery_date": str(row["export_due_date"])})
        snapshot = {"rules_contract":"v2", "query_complete":True, "profile_id":profile,
            "orders":list(grouped.values()), "inventory":inventory,
            "receipt_events_complete":True, "receipt_period":["2026-08-07","2026-09-24"],
            "receipt_events":receipt_events}
        return calculate_query_snapshot(snapshot, state, today=date(2026, 9, 24)), replacements

    legacy, _ = calculate(False)
    pay, replacements = calculate(True)
    old = {row["tradeid"]: row["freightname"] for row in legacy["plans"]}
    new = {row["tradeid"]: row["freightname"] for row in pay["plans"]}
    transitions = Counter((old.get(order,"blocked"), new.get(order,"blocked"))
                          for order in set(old) | set(new))
    print(json.dumps({
        "pay_dates_available": len(pay_dates),
        "changed_order_date_rows": replacements,
        "legacy_plans": len(old), "pay_date_plans": len(new),
        "category_changes": sum(count for (a,b),count in transitions.items() if a != b),
        "transitions": {f"{a} -> {b}": count for (a,b),count in sorted(transitions.items()) if a != b},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
