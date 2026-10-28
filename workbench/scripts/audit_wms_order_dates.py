"""Compare exact-query WMS date fields with the frozen legacy order date."""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.erp_api import ErpClient
from core.erp_source import collection, text


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
    inputs = json.loads(Path(os.environ["UL_BASELINE_INPUTS"]).read_text())
    legacy = {}
    for row in inputs["sales"]:
        order, value = text(row.get("order")), day(row.get("order_date"))
        if order in legacy and legacy[order] != value:
            raise RuntimeError("legacy order date conflict")
        legacy[order] = value
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    matched = {}
    response_rows = 0
    for offset in range(0, len(legacy), 100):
        selected = set(sorted(legacy)[offset:offset + 100])
        response = client.call("erp.ewms.stockoutbill.query", {
            "numbers": sorted(selected), "status": 2})
        rows = collection(response.get("orders"), "orders")
        response_rows += len(rows)
        for row in rows:
            tradeid = text(row.get("tradeid"))
            if tradeid in selected and tradeid not in matched:
                matched[tradeid] = row
    fields = sorted({key for row in matched.values() for key in row
                     if re.search(r"date|time|created|modified", key, re.I)})
    report = {}
    for field in fields:
        values = [(legacy[order], day(row.get(field))) for order, row in matched.items()]
        nonempty = [(old, new) for old, new in values if new]
        report[field] = {
            "nonempty": len(nonempty),
            "equal_legacy_order_date": sum(old == new for old, new in nonempty),
            "different": sum(old != new for old, new in nonempty),
            "top_mismatch_date_pairs": [
                {"legacy_date": old, "wms_date": new, "count": count}
                for (old, new), count in Counter(
                    (old, new) for old, new in nonempty if old != new
                ).most_common(5)
            ],
        }
    print(json.dumps({
        "requested_orders": len(legacy),
        "response_rows": response_rows,
        "matched_tradeids": len(matched),
        "date_fields": report,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
