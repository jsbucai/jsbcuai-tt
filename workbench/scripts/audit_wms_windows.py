"""Find a safe WMS list window without printing business records."""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.erp_api import ErpClient, ErpUnavailable
from core.erp_source import QIMEN, START, END, collection, text


def date_part(value):
    value = text(value)
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def main():
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    rows = []
    expected = None
    for page in range(1, 100):
        response = client.call(QIMEN, {
            "pageNo": page,
            "pageSize": 100,
            "extendProps": {
                "profile_id": int(profile),
                "query_time_type": 2,
                "start_time": START,
                "end_time": END,
                "bill_status": "1,2,3,4,5",
            },
        })
        items = collection(response.get("trade_orders"), "trade_orders")
        total = int(response.get("TotalResults", response.get("totalResults")))
        if expected is not None and expected != total:
            raise RuntimeError("Qimen count changed")
        expected = total
        rows.extend(items)
        if len(rows) == total:
            break
    heads = [row["trade_order"] for row in rows]
    candidate_fields = sorted({key for head in heads for key in head
                               if re.search(r"date|time|created|modified", key, re.I)})
    coverage = {}
    for field in candidate_fields:
        values = [text(head.get(field)) for head in heads if text(head.get(field))]
        coverage[field] = {"nonempty": len(values),
            "min": min(values)[:19] if values else "",
            "max": max(values)[:19] if values else ""}
    preferred = next((field for field in (
        "created", "create_time", "createtime", "trade_time", "pay_time", "modified", "modify_time"
    ) if coverage.get(field, {}).get("nonempty") == len(rows)), None)
    dates = [date_part(head.get(preferred)) for head in heads] if preferred else []
    dates = [value for value in dates if value]
    if not dates:
        print(json.dumps({"qimen_rows": len(rows), "qimen_total": expected,
            "date_field_coverage": coverage, "wms_year_windows": []},
            ensure_ascii=False, indent=2))
        return
    first_year = int(min(dates)[:4])
    current_year = datetime.now().year
    windows = []
    for year in range(first_year, current_year + 1):
        begin = max(min(dates), f"{year}-01-01") + " 00:00:00"
        end = min(datetime.now().date().isoformat(), f"{year}-12-31") + " 23:59:59"
        try:
            result = client.call("erp.ewms.stockoutbill.list", {
                "begintime": begin,
                "endtime": end,
                "timetype": 0,
                "pageno": 1,
                "pagesize": 10,
            })
            windows.append({"year": year, "ok": True,
                "rows": len(collection(result.get("orders"), "orders")),
                "total": result.get("totalresults")})
        except ErpUnavailable as error:
            windows.append({"year": year, "ok": False,
                "category": error.category, "code": error.code})
    print(json.dumps({
        "qimen_rows": len(rows),
        "qimen_total": expected,
        "date_min": min(dates),
        "date_max": max(dates),
        "orders_by_year": dict(sorted(Counter(value[:4] for value in dates).items())),
        "date_field_used": preferred,
        "date_field_coverage": coverage,
        "wms_year_windows": windows,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
