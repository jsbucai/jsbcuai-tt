"""Read-only exact Qimen/WMS logistics evidence for reviewed ERP orders.

Run inside the backend container.  Authentication stays in ``ErpClient`` and
the output contains only the business fields needed for before/after checks.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from core.erp_api import ErpClient
from core.erp_source import collection, text


QIMEN = "qimen.taobao.erp.order.sync"
WMS = "erp.ewms.stockoutbill.query"
START = "1900-01-01 00:00:00"
END = "2100-01-01 00:00:00"


def parsed(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def qimen_rows(client, profile, order_id, bill_status=None):
    extend = {
        "profile_id": int(profile),
        "query_time_type": 2,
        "start_time": START,
        "end_time": END,
        "trade_id": order_id,
    }
    if bill_status is not None:
        extend["bill_status"] = str(bill_status)
    response = client.call(QIMEN, {
        "pageNo": 1,
        "pageSize": 20,
        "extendProps": extend,
    })
    rows = collection(response.get("trade_orders"), "trade_orders")
    return [row for row in rows
            if text((row.get("trade_order") or {}).get("trade_id")) == order_id]


def inspect_order(client, profile, order_id):
    qrows = qimen_rows(client, profile, order_id)
    statuses = [value for value in range(1, 8)
                if qimen_rows(client, profile, order_id, value)]
    wresponse = client.call(WMS, {"numbers": [order_id], "status": 2})
    wrows = [row for row in collection(wresponse.get("orders"), "orders")
             if text(row.get("tradeid")) == order_id]

    q = (qrows[0].get("trade_order") or {}) if len(qrows) == 1 else {}
    qextend = parsed(q.get("extendProps"))
    w = wrows[0] if len(wrows) == 1 else {}
    goods = []
    for item in collection(w.get("goodslist"), "goodslist"):
        goods.append({key: item.get(key) for key in
                      ("id", "sourcedetailid", "ptypeid", "skuid", "usercode", "qty")})
    return {
        "order_id": order_id,
        "qimen_exact_matches": len(qrows),
        "qimen_bill_statuses": statuses,
        "qimen": {
            "trade_status": q.get("trade_status"),
            "process_state": qextend.get("process_state"),
            "vchcode": qextend.get("vchcode"),
            "logistic_id": q.get("logistic_id"),
            "logistic_no": q.get("logistic_no"),
            "logistname": q.get("logistname"),
        },
        "wms_exact_matches": len(wrows),
        "wms": {
            "deliverstatus": w.get("deliverstatus"),
            "freightbillno": w.get("freightbillno"),
            "freightcompany": w.get("freightcompany"),
            "vchcode": w.get("vchcode"),
            "sourceid": w.get("sourceid"),
            "goods": goods,
        },
    }


def main():
    order_ids = [value.strip() for value in
                 os.environ.get("UL_AUDIT_ORDER_IDS", "").split(",") if value.strip()]
    if not order_ids:
        raise SystemExit("UL_AUDIT_ORDER_IDS is required")
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    output = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "read_only": True,
        "orders": [inspect_order(client, profile, value) for value in order_ids],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
