"""Small read-only probes for ERP page-size and required-filter limits."""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.erp_api import ErpClient, ErpUnavailable
from core.erp_cache import ReferenceCache
from core.erp_source import BUSINESS_WAREHOUSE_ID, END, QIMEN, RECEIPT_START, START, collection, text
from core.store import Store


def probe(name, function, collection_name=None):
    try:
        value = function()
        rows = collection(value.get(collection_name), collection_name) if collection_name else []
        return {"name": name, "ok": True, "rows": len(rows),
                "total": value.get("totalresults", value.get("TotalResults"))}
    except ErpUnavailable as error:
        return {"name": name, "ok": False, "category": error.category, "code": error.code}
    except Exception as error:
        return {"name": name, "ok": False, "error": type(error).__name__, "message": str(error)}


def main():
    runtime = Path(os.environ.get("UL_PREFLIGHT_RUNTIME", "/app/service/runtime/data"))
    client = ErpClient(os.environ["UL_ERP_VAULT"]); profile = client.account()
    catalog = ReferenceCache(Store(runtime), max_age=timedelta(days=365)).get("catalog-v1", profile)
    products = collection((catalog or {}).get("products"), "products")
    ids = [int(text(row["ptypeid"])) for row in products if not row.get("isclass")]
    common_stock = {"ktypeids":[int(BUSINESS_WAREHOUSE_ID)], "pageno":1}
    qimen_common = {"pageNo":1, "extendProps":{"profile_id":int(profile),
        "query_time_type":2,"start_time":START,"end_time":END,"bill_status":"1,2,3,4,5"}}
    receipt_common = {"overbegin":RECEIPT_START,
        "overend":datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "vchtype":[34,6,143],"showredbill":True,"hascustom":True,"pageindex":1}
    tests = []
    for size in (100, 200, 500):
        tests.append(probe(f"stock_warehouse_only_{size}", lambda size=size:client.call(
            "erp.goodsstocks.list", dict(common_stock,pagesize=size)), "goodsstocks"))
    for size in (100, 200, 500):
        tests.append(probe(f"stock_100_products_{size}", lambda size=size:client.call(
            "erp.goodsstocks.list", dict(common_stock,ptypeids=ids[:100],pagesize=size)), "goodsstocks"))
    for product_count in (200, 500):
        tests.append(probe(f"stock_{product_count}_products_500", lambda product_count=product_count:client.call(
            "erp.goodsstocks.list", dict(common_stock,ptypeids=ids[:product_count],pagesize=500)), "goodsstocks"))
    for size in (100, 200, 500):
        tests.append(probe(f"qimen_{size}", lambda size=size:client.call(
            QIMEN, dict(qimen_common,pageSize=size)), "trade_orders"))
    for size in (100, 200, 500):
        tests.append(probe(f"receipts_{size}", lambda size=size:client.call(
            "erp.bill.billcenterlist", dict(receipt_common,pagesize=size)), "details"))
    print(json.dumps({"profile_id":profile,"probes":tests},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
