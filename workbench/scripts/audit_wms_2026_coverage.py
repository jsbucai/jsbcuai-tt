"""Read-only coverage audit for current-year WMS rows versus Qimen demand."""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.erp_api import ErpClient
from core.erp_source import QIMEN, START, END, collection, text
from core.rules import note_delivery_date


def quantity(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def due(value):
    try:
        return bool(note_delivery_date(text(value)))
    except ValueError:
        return False


def main():
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    qimen = []
    expected = None
    for page in range(1, 100):
        response = client.call(QIMEN, {"pageNo": page, "pageSize": 100,
            "extendProps": {"profile_id": int(profile), "query_time_type": 2,
                "start_time": START, "end_time": END, "bill_status": "1,2,3,4,5"}})
        items = collection(response.get("trade_orders"), "trade_orders")
        total = int(response.get("TotalResults", response.get("totalResults")))
        if expected is not None and total != expected:
            raise RuntimeError("Qimen total changed while paging")
        expected = total
        qimen.extend(items)
        if len(qimen) == total:
            break
    qimen_by_id = {}
    duplicate_qimen_rows = 0
    for row in qimen:
        trade_id = text(row["trade_order"].get("trade_id"))
        if trade_id in qimen_by_id:
            duplicate_qimen_rows += 1
            if json.dumps(qimen_by_id[trade_id], ensure_ascii=False, sort_keys=True) != \
                    json.dumps(row, ensure_ascii=False, sort_keys=True):
                raise RuntimeError("Qimen duplicate IDs have conflicting content")
        else:
            qimen_by_id[trade_id] = row

    wms = []
    expected_wms = None
    now = datetime.now()
    for page in range(1, 1000):
        response = client.call("erp.ewms.stockoutbill.list", {
            "begintime": f"{now.year}-01-01 00:00:00",
            "endtime": now.strftime("%Y-%m-%d 23:59:59"),
            "timetype": 0, "pageno": page, "pagesize": 200})
        items = collection(response.get("orders"), "orders")
        total = int(response.get("totalresults"))
        if expected_wms is not None and total != expected_wms:
            raise RuntimeError("WMS total changed while paging")
        expected_wms = total
        wms.extend(items)
        if len(wms) == total:
            break
        if len(items) != 200:
            raise RuntimeError("WMS pagination did not close")

    matches = defaultdict(list)
    for row in wms:
        trade_id = text(row.get("tradeid"))
        if trade_id in qimen_by_id:
            matches[trade_id].append(row)
    unique = {key: rows[0] for key, rows in matches.items() if len(rows) == 1}
    quantity_equal = 0
    quantity_compared = 0
    for trade_id, row in unique.items():
        q_sums, w_sums = defaultdict(Decimal), defaultdict(Decimal)
        q_lines = collection(qimen_by_id[trade_id].get("order_info"), "order_info")
        valid = True
        for item in q_lines:
            value = quantity(item.get("sell_count"))
            if value is None:
                valid = False
                break
            q_sums[text(item.get("spec_code") or item.get("goods_code")).upper()] += value
        goods = row.get("goodslist")
        if not isinstance(goods, list):
            valid = False
        else:
            for item in goods:
                value = quantity(item.get("assqty", item.get("qty")))
                if value is None:
                    valid = False
                    break
                w_sums[text(item.get("usercode")).upper()] += value
        if valid:
            quantity_compared += 1
            quantity_equal += q_sums == w_sums

    report = {
        "qimen_total": expected,
        "qimen_unique_orders": len(qimen_by_id),
        "qimen_duplicate_rows": duplicate_qimen_rows,
        "wms_2026_total": expected_wms,
        "wms_pages": (len(wms) + 199) // 200,
        "matched_qimen_orders": len(matches),
        "unique_matches": len(unique),
        "duplicate_match_orders": sum(len(rows) > 1 for rows in matches.values()),
        "unmatched_qimen_orders": len(qimen_by_id) - len(matches),
        "unique_summary_nonempty": sum(bool(text(row.get("summary"))) for row in unique.values()),
        "unique_comment_nonempty": sum(bool(text(row.get("comment"))) for row in unique.values()),
        "summary_due_suffix": sum(due(row.get("summary")) for row in unique.values()),
        "comment_due_suffix": sum(due(row.get("comment")) for row in unique.values()),
        "warehouse_08390": sum(text(row.get("ktypeid")) == "29871293290508390" for row in unique.values()),
        "goods_nonempty": sum(isinstance(row.get("goodslist"), list) and bool(row.get("goodslist"))
                              for row in unique.values()),
        "quantity_compared": quantity_compared,
        "quantity_equal": quantity_equal,
        "unmatched_pay_years": dict(sorted(Counter(
            text(qimen_by_id[key]["trade_order"].get("pay_time"))[:4]
            for key in set(qimen_by_id) - set(matches)
        ).items())),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
