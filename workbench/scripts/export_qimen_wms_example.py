"""Export one matched Qimen/WMS order response without authentication secrets.

This script is intentionally read-only. It chooses a recent order that exists in
both APIs and writes the complete application-level responses returned by
``ErpClient.call``. Gateway authentication fields are generated internally and
are never written to the export.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app/service")

from core.erp_api import ErpClient
from core.erp_source import END, START, collection, text


QIMEN_METHOD = "qimen.taobao.erp.order.sync"
WMS_METHOD = "erp.ewms.stockoutbill.query"
DATABASE = Path("/app/service/runtime/data/workbench.sqlite3")


def cached_order_ids():
    with sqlite3.connect(DATABASE) as database:
        rows = database.execute(
            "SELECT payload FROM records WHERE kind='orders'"
        ).fetchall()
    result = set()
    for (payload,) in rows:
        row = json.loads(payload)
        order_id = text(row.get("order_no"))
        freight_number = text(row.get("source_shipping_id"))
        if order_id and freight_number.startswith("@"):
            result.add(order_id)
    if not result:
        raise RuntimeError("缓存中没有带既有物流单号的订单实例")
    return sorted(result)


def wms_candidates(client, order_ids):
    candidates = {}
    for offset in range(0, len(order_ids), 100):
        requested = order_ids[offset:offset + 100]
        response = client.call(WMS_METHOD, {"numbers": requested, "status": 2})
        for row in collection(response.get("orders"), "orders"):
            order_id = text(row.get("tradeid"))
            if order_id in requested:
                candidates[order_id] = row
    return sorted(
        candidates.values(),
        key=lambda row: (text(row.get("createtime")), text(row.get("modifytime")), text(row.get("tradeid"))),
        reverse=True,
    )


def qimen_exact(client, profile, order_id):
    params = {
        "pageNo": 1,
        "pageSize": 5,
        "extendProps": {
            "profile_id": int(profile),
            "query_time_type": 2,
            "start_time": START,
            "end_time": END,
            "trade_id": order_id,
        },
    }
    response = client.call(QIMEN_METHOD, params)
    rows = collection(response.get("trade_orders"), "trade_orders")
    matching = [row for row in rows if text((row.get("trade_order") or {}).get("trade_id")) == order_id]
    return params, response, matching


def wms_exact(client, order_id):
    params = {"numbers": [order_id], "status": 2}
    response = client.call(WMS_METHOD, params)
    rows = collection(response.get("orders"), "orders")
    matching = [row for row in rows if text(row.get("tradeid")) == order_id]
    return params, response, matching


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    candidates = wms_candidates(client, cached_order_ids())
    if not candidates:
        raise RuntimeError("WMS 没有返回可用于对照的订单")

    selected = None
    for candidate in candidates[:50]:
        order_id = text(candidate.get("tradeid"))
        qimen_params, qimen_response, qimen_rows = qimen_exact(client, profile, order_id)
        if len(qimen_rows) != 1:
            continue
        wms_params, wms_response, wms_rows = wms_exact(client, order_id)
        if len(wms_rows) != 1:
            continue
        selected = {
            "order_id": order_id,
            "qimen_params": qimen_params,
            "qimen_response": qimen_response,
            "wms_params": wms_params,
            "wms_response": wms_response,
        }
        break
    if selected is None:
        raise RuntimeError("最近 50 个候选中没有同时被奇门和 WMS 唯一命中的订单")

    exported = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "selected_order_id": selected["order_id"],
        "说明": [
            "同一张真实订单的只读查询实例。",
            "response 是 ErpClient 解包后的完整业务响应，业务字段未删减。",
            "未保存 appkey、token、sign、signkey、refresh_token、管理员密码等认证信息。",
            "request_business_params 只包含调用该接口时提交的业务参数。",
        ],
        "奇门": {
            "method": QIMEN_METHOD,
            "request_business_params": selected["qimen_params"],
            "response": selected["qimen_response"],
        },
        "WMS": {
            "method": WMS_METHOD,
            "request_business_params": selected["wms_params"],
            "response": selected["wms_response"],
        },
    }
    destination = Path(args.output)
    destination.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(destination),
        "selected_order_id": selected["order_id"],
        "qimen_matches": 1,
        "wms_matches": 1,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
