"""Read-only WMS range coverage audit against the current published order IDs."""
import json
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "/app/service")

from core.erp_api import ErpClient
from core.erp_api import ErpUnavailable
from core.erp_source import collection, text


def main():
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    with sqlite3.connect("/app/service/runtime/data/workbench.sqlite3") as database:
        current = {
            text(json.loads(payload).get("order_no"))
            for (payload,) in database.execute("SELECT payload FROM records WHERE kind='orders'")
        }
    current.discard("")

    rows, expected, page_counts = [], None, []
    for page in range(1, 101):
        try:
            response = client.call("erp.ewms.stockoutbill.list", {
                "begintime": "2026-09-24 00:00:00",
                "endtime": "2026-09-25 00:00:00",
                "timetype": 0,
                "pageno": page,
                "pagesize": 100,
            })
        except ErpUnavailable as error:
            print(json.dumps({"error_category":error.category,"error_code":error.code},ensure_ascii=False))
            return
        items = collection(response.get("orders"), "orders")
        total = response.get("totalresults")
        if not str(total).isdigit():
            raise RuntimeError("WMS range response has no total")
        total = int(total)
        if expected is not None and expected != total:
            raise RuntimeError("WMS total changed during paging")
        expected = total
        rows.extend(items)
        page_counts.append(len(items))
        if len(rows) == expected:
            break
        if len(rows) > expected or len(items) != 100:
            raise RuntimeError("WMS paging did not close")
    if len(rows) != expected:
        raise RuntimeError("WMS page limit reached")

    ids = [text(row.get("tradeid") or row.get("number") or row.get("sourceid")) for row in rows]
    counts = Counter(ids)
    duplicates = {key for key, count in counts.items() if key and count > 1}
    matched = [row for row, order_id in zip(rows, ids) if order_id in current]
    summary = {
        "wms_total": expected,
        "pages": len(page_counts),
        "page_counts": page_counts,
        "unique_order_ids": len({value for value in ids if value}),
        "duplicate_order_ids": len(duplicates),
        "published_order_ids": len(current),
        "matched_published_orders": len({value for value in ids if value in current}),
        "matched_rows": len(matched),
        "matched_with_summary": sum(bool(text(row.get("summary") or row.get("comment"))) for row in matched),
        "matched_with_goods": sum(isinstance(row.get("goodslist"), list) and bool(row.get("goodslist")) for row in matched),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
