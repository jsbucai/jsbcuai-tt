"""Compare Qimen pay_time with the frozen legacy order date, without raw IDs."""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.erp_api import ErpClient
from core.erp_source import QIMEN, START, END, collection, text


def day(value):
    value = text(value)
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def main():
    baseline = json.loads(Path(os.environ["UL_BASELINE_INPUTS"]).read_text())
    legacy = {}
    for row in baseline["sales"]:
        order = text(row.get("order"))
        value = day(row.get("order_date"))
        if order in legacy and legacy[order] != value:
            raise RuntimeError("legacy order has conflicting dates")
        legacy[order] = value

    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    current = {}
    raw_count = expected = 0
    for page in range(1, 100):
        response = client.call(QIMEN, {"pageNo": page, "pageSize": 100,
            "extendProps": {"profile_id": int(profile), "query_time_type": 2,
                "start_time": START, "end_time": END, "bill_status": "1,2,3,4,5"}})
        items = collection(response.get("trade_orders"), "trade_orders")
        total = int(response.get("TotalResults", response.get("totalResults")))
        if expected and total != expected:
            raise RuntimeError("Qimen total changed while paging")
        expected = total
        for row in items:
            head = row["trade_order"]
            order = text(head.get("trade_id"))
            value = day(head.get("pay_time"))
            if order in current and current[order] != value:
                raise RuntimeError("Qimen duplicate has conflicting pay_time")
            current[order] = value
        raw_count += len(items)
        if raw_count == total:
            break
    overlap = sorted(set(legacy) & set(current))
    differences = Counter()
    for order in overlap:
        differences[(legacy[order], current[order])] += 1
    mismatch = sum(count for (old, new), count in differences.items() if old != new)
    mismatch_orders = {order for order in overlap if legacy[order] != current[order]}
    dates_to_read = sorted({value for order in mismatch_orders
                            for value in (legacy[order], current[order]) if value})
    wms_matches = {}
    wms_query_days = defaultdict(set)
    for value in dates_to_read:
        received = 0
        expected_wms = None
        for page in range(1, 100):
            response = client.call("erp.ewms.stockoutbill.list", {
                "begintime": value + " 00:00:00", "endtime": value + " 23:59:59",
                "timetype": 0, "pageno": page, "pagesize": 200})
            items = collection(response.get("orders"), "orders")
            total = int(response.get("totalresults"))
            if expected_wms is not None and total != expected_wms:
                raise RuntimeError("WMS total changed while paging")
            expected_wms = total
            for row in items:
                tradeid = text(row.get("tradeid"))
                if tradeid in mismatch_orders:
                    wms_matches[tradeid] = row
                    wms_query_days[tradeid].add(value)
            received += len(items)
            if received == total:
                break
    wms_creation = {order: day(row.get("createtime")) for order, row in wms_matches.items()}
    print(json.dumps({
        "legacy_orders": len(legacy),
        "qimen_raw_rows": raw_count,
        "qimen_unique_orders": len(current),
        "overlap": len(overlap),
        "date_equal": len(overlap) - mismatch,
        "date_mismatch": mismatch,
        "missing_qimen_pay_time": sum(not current[order] for order in overlap),
        "mismatch_wms_matched": len(wms_matches),
        "mismatch_wms_createtime_nonempty": sum(bool(value) for value in wms_creation.values()),
        "mismatch_wms_createtime_equals_legacy": sum(
            value == legacy[order] for order, value in wms_creation.items() if value),
        "mismatch_wms_createtime_equals_pay": sum(
            value == current[order] for order, value in wms_creation.items() if value),
        "mismatch_wms_query_day_unique": sum(len(values) == 1 for values in wms_query_days.values()),
        "mismatch_wms_query_day_equals_legacy": sum(
            len(values) == 1 and next(iter(values)) == legacy[order]
            for order, values in wms_query_days.items()),
        "mismatch_wms_query_day_equals_pay": sum(
            len(values) == 1 and next(iter(values)) == current[order]
            for order, values in wms_query_days.items()),
        "mismatch_date_pairs": [
            {"legacy_date": old, "qimen_pay_date": new, "count": count}
            for (old, new), count in differences.most_common(10) if old != new
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
