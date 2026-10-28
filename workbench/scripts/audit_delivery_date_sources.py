"""Compare delivery-date candidates from Qimen and WMS without business text."""
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, "/app/service")

from core.erp_api import ErpClient
from core.erp_source import expanded, text


DATE_VALUE = re.compile(r"(?<!\d)(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)(?!\d)")
FREIGHT_DATE = re.compile(r"^@([01]?\d)/([0-3]?\d)#")
CANDIDATE_NAME = re.compile(
    r"date|time|delivery|deliver|send|ship|plan|promise|require|expect|deadline|arrival|交货|发货|送货|到货|要求|预计",
    re.I,
)


def flatten(value, prefix=""):
    if isinstance(value, str) and value[:1] in "[{":
        decoded = expanded(value)
        if decoded:
            yield from flatten(decoded, prefix)
            return
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from flatten(child, path)
    elif isinstance(value, list):
        for child in value:
            path = f"{prefix}[]"
            yield from flatten(child, path)
    elif value not in (None, ""):
        yield prefix, value


def normalized_date(value):
    match = DATE_VALUE.search(text(value))
    if not match:
        return None
    try:
        return date(*map(int, match.groups()))
    except ValueError:
        return None


def freight_month_day(value):
    match = FREIGHT_DATE.match(text(value))
    return (int(match.group(1)), int(match.group(2))) if match else None


def main():
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    with sqlite3.connect("/app/service/runtime/data/workbench.sqlite3") as db:
        cached_rows = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM records WHERE kind='orders'")]
    truth = {}
    for row in cached_rows:
        order_id = text(row.get("order_no"))
        month_day = freight_month_day(row.get("source_shipping_id"))
        if order_id and month_day:
            truth[order_id] = month_day

    start = "1900-01-01 00:00:00"
    end = "2100-01-01 00:00:00"
    qimen_rows = []
    qimen_total = None
    for page in range(1, 2001):
        response = client.call("qimen.taobao.erp.order.sync", {
            "pageNo": page,
            "pageSize": 100,
            "extendProps": {
                "profile_id": int(profile),
                "query_time_type": 2,
                "start_time": start,
                "end_time": end,
                "bill_status": "1,2,3,4,5",
            },
        })
        raw = response.get("trade_orders")
        rows = raw.get("trade_orders") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise RuntimeError("Qimen response has no order list")
        total = response.get("TotalResults", response.get("totalResults"))
        if not str(total).isdigit():
            raise RuntimeError("Qimen response has no total")
        qimen_total = int(total)
        qimen_rows.extend(rows)
        if len(qimen_rows) >= qimen_total:
            break
    qimen_by_id = {}
    for row in qimen_rows:
        head = row.get("trade_order") or {}
        order_id = text(head.get("trade_id"))
        if order_id in truth:
            qimen_by_id[order_id] = row

    order_ids = sorted(truth)
    wms_rows = []
    for offset in range(0, len(order_ids), 100):
        result = client.call("erp.ewms.stockoutbill.query", {
            "numbers": order_ids[offset:offset + 100],
            "status": 2,
        })
        rows = result.get("orders")
        if not isinstance(rows, list):
            raise RuntimeError("WMS response has no order list")
        wms_rows.extend(rows)
    wms_by_id = {text(row.get("tradeid")): row for row in wms_rows if text(row.get("tradeid"))}

    stats = defaultdict(lambda: {"present": 0, "parseable": 0, "matched_freight_date": 0,
                                 "comparable": 0, "examples": []})

    def add(source, order_id, value):
        for path, raw_value in flatten(value):
            parsed = normalized_date(raw_value)
            if not parsed and not CANDIDATE_NAME.search(path):
                continue
            key = f"{source}:{path}"
            item = stats[key]
            item["present"] += 1
            if parsed:
                item["parseable"] += 1
                shown = parsed.isoformat()
                if shown not in item["examples"] and len(item["examples"]) < 5:
                    item["examples"].append(shown)
                if order_id in truth:
                    item["comparable"] += 1
                    if (parsed.month, parsed.day) == truth[order_id]:
                        item["matched_freight_date"] += 1

    for order_id, row in qimen_by_id.items():
        add("qimen", order_id, row)
    for order_id, row in wms_by_id.items():
        add("wms", order_id, row)

    candidates = []
    for path, item in sorted(stats.items()):
        candidates.append({"path": path, **item,
            "match_rate": (item["matched_freight_date"] / item["comparable"]
                           if item["comparable"] else None)})
    candidates.sort(key=lambda item: (
        item["matched_freight_date"], item["parseable"], item["present"]), reverse=True)

    print(json.dumps({
        "window": {"start": start, "end": end},
        "qimen_total": qimen_total,
        "qimen_sampled": len(qimen_by_id),
        "wms_matched": len(wms_by_id),
        "existing_freight_dates": len(truth),
        "candidates": candidates,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
