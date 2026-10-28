"""Run one real update job against an isolated runtime and verify query routes."""
import json
import os
import sys
import uuid
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app import create_app
from core.api_source import WmsSource
from core.erp_api import ErpClient
from core.store import Store
from core.update_jobs import UpdateJobs


def main():
    runtime = Path(os.environ.get("UL_PREFLIGHT_RUNTIME", "/app/service/runtime/data"))
    store = Store(runtime)
    erp = ErpClient(os.environ["UL_ERP_VAULT"], allow_logistics=False)
    source = WmsSource(erp, store)
    updates = UpdateJobs(store, source, sender=None)
    if os.environ.get("UL_PREFLIGHT_ROUTES_ONLY") == "1":
        job = updates.latest()
        if job is None:
            raise RuntimeError("no completed preflight job")
        identity = job["id"]
    else:
        identity = str(uuid.uuid4())
        updates.enqueue(identity, "sync")
        updates.process_one()
        job = updates.get(identity)

    app = create_app({
        "TESTING": True,
        "SOURCE_MODE": "api",
        "PUBLIC_MODE": False,
        "ERP_CLIENT": erp,
        "API_SOURCE": source,
        "LOGISTICS_WRITE_ENABLED": False,
    }, runtime_dir=runtime)
    web = app.test_client()
    health = web.get("/api/v2/health")
    sample_sku = next((row.get("sku") for row in store.rows("orders") if row.get("sku")), "")
    orders = web.get("/api/v2/orders", query_string={"q": sample_sku, "page": 1, "page_size": 1})
    inventory = web.get("/api/v2/inventory?page=1&page_size=1")
    health_json = health.get_json() or {}
    order_json = orders.get_json() or {}
    inventory_json = inventory.get_json() or {}
    print(json.dumps({
        "job": {
            "id": identity,
            "status": job["status"],
            "stage": job["stage"],
            "published": job["result"].get("published"),
            "query_status": job["result"].get("query_status"),
            "logistics_status": job["result"].get("logistics_status"),
            "data_version": job["result"].get("data_version"),
            "record_counts": job["result"].get("record_counts"),
            "blocked_orders": len(job["result"].get("blocked_orders", [])),
            "attempts": len(job["result"].get("attempts", [])),
        },
        "database_counts": store.counts(),
        "http": {
            "health": health.status_code,
            "orders": orders.status_code,
            "inventory": inventory.status_code,
            "health_ok": health_json.get("ok"),
            "orders_ok": order_json.get("ok"),
            "inventory_ok": inventory_json.get("ok"),
            "order_total": order_json.get("total"),
            "inventory_total": inventory_json.get("total"),
            "order_item_keys": sorted((order_json.get("items") or [{}])[0]),
            "inventory_item_keys": sorted((inventory_json.get("items") or [{}])[0]),
        },
        "logistics_write_enabled": updates.sender is not None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
