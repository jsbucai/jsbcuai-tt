"""Read-only proof for faster ERP pagination and warehouse-scoped inventory."""
import hashlib
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.erp_api import ErpClient
from core.erp_cache import ReferenceCache
from core.erp_source import BUSINESS_WAREHOUSE_ID, END, QIMEN, RECEIPT_START, START, canonical, collection, text
from core.store import Store


def digest(rows):
    return hashlib.sha256(canonical(rows).encode()).hexdigest()


def standard_pages(client, method, params, name, identity, size, page_name="pageno"):
    rows, seen, expected, calls = [], {}, None, 0
    for page in range(1, 2001):
        payload = dict(params, pagesize=size, **{page_name: page})
        result = client.call(method, payload); calls += 1
        items = collection(result.get(name), name)
        total = result.get("totalresults")
        if isinstance(total, bool) or not str(total).isdigit():
            raise RuntimeError(f"{method} missing totalresults")
        if expected is not None and expected != int(total):
            raise RuntimeError(f"{method} total changed")
        expected = int(total)
        for row in items:
            key = identity(row)
            value = canonical(row)
            if not key or (key in seen and seen[key] != value):
                raise RuntimeError(f"{method} duplicate/conflicting identity")
            if key not in seen:
                seen[key] = value; rows.append(row)
        if len(rows) == expected:
            return rows, calls
        if len(rows) > expected or len(items) != size:
            raise RuntimeError(f"{method} pagination did not close")
    raise RuntimeError(f"{method} too many pages")


def qimen(client, profile, size):
    rows, seen, raw, expected, calls = [], {}, 0, None, 0
    for page in range(1, 2001):
        result = client.call(QIMEN, {"pageNo": page, "pageSize": size,
            "extendProps": {"profile_id": int(profile), "query_time_type": 2,
                "start_time": START, "end_time": END, "bill_status": "1,2,3,4,5"}})
        calls += 1
        items = collection(result.get("trade_orders"), "trade_orders")
        total = int(result.get("TotalResults", result.get("totalResults")))
        if expected is not None and expected != total:
            raise RuntimeError("Qimen total changed")
        expected = total; raw += len(items)
        for row in items:
            key = text(row.get("trade_order", {}).get("trade_id")); value = canonical(row)
            if not key or (key in seen and seen[key] != value):
                raise RuntimeError("Qimen duplicate conflict")
            if key not in seen:
                seen[key] = value; rows.append(row)
        if raw == expected:
            return sorted(rows, key=lambda r:text(r["trade_order"]["trade_id"])), calls, raw
        if raw > expected or len(items) != size:
            raise RuntimeError("Qimen pagination did not close")
    raise RuntimeError("Qimen too many pages")


def short_pages(client, method, params, name, identity, size, page_name):
    rows, seen, calls, first = [], {}, 0, None
    for page in range(1, 2001):
        result = client.call(method, dict(params, pagesize=size, **{page_name:page})); calls += 1
        items = collection(result.get(name), name)
        if page == 1:
            first = canonical(items)
        for row in items:
            key = identity(row); value = canonical(row)
            if not key or (key in seen and seen[key] != value):
                raise RuntimeError(f"{method} duplicate/conflicting identity")
            if key not in seen:
                seen[key] = value; rows.append(row)
        if len(items) < size:
            if page > 1:
                check = client.call(method, dict(params, pagesize=size, **{page_name:1})); calls += 1
                if canonical(collection(check.get(name), name)) != first:
                    raise RuntimeError(f"{method} first page changed")
            return rows, calls
    raise RuntimeError(f"{method} too many pages")


def timed(function):
    started = time.monotonic(); value = function()
    return value, round(time.monotonic() - started, 3)


def main():
    runtime = Path(os.environ.get("UL_PREFLIGHT_RUNTIME", "/app/service/runtime/data"))
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    profile = client.account()
    # The audit validates transport shapes, so it may use the last integrity-
    # checked catalog even when the normal six-hour refresh window has elapsed.
    catalog = ReferenceCache(Store(runtime), max_age=timedelta(days=365)).get("catalog-v1", profile)
    products = collection((catalog or {}).get("products"), "products")
    product_ids = sorted(text(row["ptypeid"]) for row in products if not row.get("isclass"))
    stock_identity = lambda r: tuple(text(r.get(k)) for k in ("profileid", "ktypeid", "ptypeid", "skuid"))

    def batched_stock(size):
        output, calls = [], 0
        for offset in range(0, len(product_ids), size):
            rows, used = standard_pages(client, "erp.goodsstocks.list", {
                "ptypeids": [int(value) for value in product_ids[offset:offset + size]],
                "ktypeids": [int(BUSINESS_WAREHOUSE_ID)],
            }, "goodsstocks", stock_identity, size)
            output.extend(rows); calls += used
        return sorted(output, key=stock_identity), calls

    (stock_old, stock_old_calls), stock_old_seconds = timed(lambda:batched_stock(100))
    (stock_fast, stock_fast_calls), stock_fast_seconds = timed(lambda:batched_stock(500))
    stock_old_map = {stock_identity(row):canonical(row) for row in stock_old}
    stock_fast_map = {stock_identity(row):canonical(row) for row in stock_fast}

    (orders_100, order_calls_100, raw_100), order_seconds_100 = timed(lambda:qimen(client, profile, 100))

    receipt_params = {"overbegin": RECEIPT_START,
        "overend": time.strftime("%Y-%m-%d %H:%M:%S"), "vchtype": [34, 6, 143],
        "showredbill": True, "hascustom": True}
    receipt_identity = lambda r:text(r.get("vchcode"))
    (receipts_100, receipt_calls_100), receipt_seconds_100 = timed(lambda:short_pages(
        client, "erp.bill.billcenterlist", receipt_params, "details", receipt_identity, 100, "pageindex"))
    (receipts_500, receipt_calls_500), receipt_seconds_500 = timed(lambda:short_pages(
        client, "erp.bill.billcenterlist", receipt_params, "details", receipt_identity, 500, "pageindex"))

    print(json.dumps({
        "profile_id": profile,
        "inventory": {
            "catalog_products": len(product_ids),
            "batched_rows": len(stock_old), "batched_calls": stock_old_calls,
            "batched_seconds": stock_old_seconds,
            "batch500_rows": len(stock_fast), "batch500_calls": stock_fast_calls,
            "batch500_seconds": stock_fast_seconds,
            "identity_equal": set(stock_old_map) == set(stock_fast_map),
            "payload_equal": stock_old_map == stock_fast_map,
            "only_batched": len(set(stock_old_map) - set(stock_fast_map)),
            "only_batch500": len(set(stock_fast_map) - set(stock_old_map)),
            "different_payloads": sum(stock_old_map[k] != stock_fast_map[k]
                for k in set(stock_old_map) & set(stock_fast_map)),
        },
        "orders": {
            "page100_unique": len(orders_100), "page100_raw": raw_100,
            "page100_calls": order_calls_100, "page100_seconds": order_seconds_100,
            "supported_page_size": 100,
        },
        "receipts": {
            "page100_rows": len(receipts_100), "page100_calls": receipt_calls_100,
            "page100_seconds": receipt_seconds_100,
            "page500_rows": len(receipts_500), "page500_calls": receipt_calls_500,
            "page500_seconds": receipt_seconds_500,
            "equal": digest(sorted(receipts_100, key=receipt_identity)) ==
                     digest(sorted(receipts_500, key=receipt_identity)),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
