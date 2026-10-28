import json
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.inventory_ledger import (
    BUSINESS_WAREHOUSE_ID, InventoryLedger, InventorySubsetRefresher,
)
from core.store import Store, dumps
from core.update_jobs import UpdateJobs
from core.api_source import SourceIncomplete
from core.print_jobs import JobConflict
from app import create_app


def inventory_row(product="30", sku="31", physical="70", saleable="60", record="stock-1"):
    return {
        "id": record, "profile_id": "10", "warehouse_id": BUSINESS_WAREHOUSE_ID,
        "product_id": product, "sku_id": sku, "base_unit": "1", "sku": "DQ0517",
        "product_name": "测试商品", "unit_name": "件",
        "stock_quantity": physical, "available_stock": saleable,
    }


class BillClient:
    def __init__(self, bills):
        self.bills = bills
        self.calls = []

    def call(self, method, params):
        assert method == "erp.bill.billcenterlist"
        self.calls.append(params)
        begin = (params["pageindex"] - 1) * params["pagesize"]
        return {"details": self.bills[begin:begin + params["pagesize"]]}


def bill(identity, code, when, quantity, inout, line=None):
    return {
        "vchcode": identity, "number": f"B-{identity}", "vchtype": code,
        "vchtypename": {34: "进货入库单", 11: "销售出库单", 143: "进货换货单"}[code],
        "date": when[:10], "overtime": when, "bfullname": "往来单位",
        "details": {"details": [{
            "dlyorder": line or f"L-{identity}", "ktypeid": BUSINESS_WAREHOUSE_ID,
            "ptypeid": "30", "skuid": "31", "qty": quantity, "inout": inout,
        }]},
    }


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory(dir=ROOT / "runtime", prefix="ledger-test-") as path:
        yield Store(path)


def persist_records(store, rows, version="v1"):
    with store.connect(write=True) as database:
        for row in rows:
            database.execute(
                "INSERT INTO records VALUES ('inventory',?,?,?,?,?,?,?,?,?,?)",
                (row["id"], dumps(row), row["sku"], "", "", row["sku"], "", None, None, ""),
            )
        database.execute("INSERT INTO settings VALUES ('data_version',?)", (dumps(version),))
        database.execute("INSERT INTO sources VALUES ('inventory',?)", (dumps({"kind": "inventory"}),))


def test_verified_signed_quantity_persists_and_history_stops_at_four_inbounds(store):
    ledger = InventoryLedger(store)
    started = datetime(2026, 9, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    bills = [
        bill("1", 34, "2026-09-20 08:00:00", "10", 0),
        bill("2", 11, "2026-09-21 08:00:00", "-5", 1),
        bill("3", 34, "2026-09-22 08:00:00", "20", 0),
        bill("4", 34, "2026-09-23 08:00:00", "30", 0),
        bill("5", 11, "2026-09-24 08:00:00", "-10", 1),
        bill("6", 34, "2026-09-25 08:00:00", "40", 0),
        bill("7", 11, "2026-09-26 08:00:00", "-15", 1),
    ]
    stage = ledger.prepare(BillClient(bills), "10", [inventory_row()], started)
    assert stage["commit"] is True and stage["issues"] == []
    assert {event["signed_quantity"] for event in stage["events"]} >= {"10", "-5"}
    persist_records(store, [inventory_row()])
    with store.connect(write=True) as database:
        InventoryLedger.persist(database, stage, "v1")
    result = ledger.history("stock-1")
    assert result["status"] == "reconciled"
    assert result["coverage"]["positive_inbounds"] == 4
    assert len(result["movements"]) == 7
    assert result["movements"][0]["quantity"] == "-15"
    assert result["movements"][0]["balance_after"] == "70"
    assert result["movements"][1]["balance_after"] == "85"
    app = create_app({"TESTING": True, "AUTO_IMPORT": False}, runtime_dir=store.runtime)
    response = app.test_client().get("/api/v2/inventory/stock-1/history")
    assert response.status_code == 200
    assert response.get_json()["movements"][0]["balance_after"] == "70"
    first_page = app.test_client().get(
        "/api/v2/inventory/stock-1/history?days=90&limit=3").get_json()
    assert len(first_page['movements']) == 3
    assert first_page['coverage']['total'] == 7
    assert first_page['coverage']['next_offset'] == 3
    second_page = app.test_client().get(
        "/api/v2/inventory/stock-1/history?days=90&limit=3&offset=3").get_json()
    assert [item['id'] for item in first_page['movements'] + second_page['movements']] == [
        item['id'] for item in result['movements'][:6]]
    assert second_page['movements'][0]['balance_after'] == result['movements'][3]['balance_after']
    beyond = app.test_client().get(
        "/api/v2/inventory/stock-1/history?days=90&limit=3&offset=10").get_json()
    assert beyond['movements'] == [] and beyond['coverage']['next_offset'] is None


def test_empty_sample_type_is_saved_but_never_claims_balance_reconciliation(store):
    ledger = InventoryLedger(store)
    started = datetime(2026, 9, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    stage = ledger.prepare(BillClient([
        bill("143", 143, "2026-09-28 08:00:00", "2", 0),
    ]), "10", [inventory_row()], started)
    assert stage["events"][0]["semantics_verified"] is False
    assert stage["issues"][0]["code"] == "movement_semantics_unverified"
    persist_records(store, [inventory_row()])
    with store.connect(write=True) as database:
        InventoryLedger.persist(database, stage, "v1")
    result = ledger.history("stock-1")
    assert result["status"] == "incomplete"
    assert result["movements"][0]["balance_after"] is None


def test_watermark_backfills_180_days_then_uses_72_hour_overlap(store):
    ledger = InventoryLedger(store)
    first = datetime(2026, 9, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    initial = ledger.prepare(BillClient([]), "10", [inventory_row()], first)
    assert initial['window']['begin'] == (first-timedelta(days=180)).strftime('%Y-%m-%d 00:00:00')
    with store.connect(write=True) as database:
        InventoryLedger.persist(database, initial, "one")
    second_client = BillClient([])
    second = first + timedelta(days=1)
    incremental = ledger.prepare(second_client, "10", [inventory_row()], second)
    assert incremental["window"]["full_reconcile"] is False
    assert second_client.calls[0]["overbegin"] == "2026-09-17 12:00:00"
    weekly = ledger.prepare(BillClient([]), "10", [inventory_row()], first + timedelta(days=8))
    assert weekly["window"]["full_reconcile"] is False
    forced = ledger.prepare(BillClient([]), "10", [inventory_row()],
                            first + timedelta(days=8), force_full=True)
    assert forced["window"]["full_reconcile"] is True
    assert forced["window"]["begin"] == (first+timedelta(days=8)-timedelta(days=180))\
        .strftime('%Y-%m-%d 00:00:00')


class StockClient:
    def __init__(self, outside=False, qty="70", saleable="55"):
        self.outside = outside
        self.qty, self.saleable = qty, saleable
    def account(self):
        return "10"
    def call(self, method, params):
        assert method == "erp.goodsstocks.list"
        product = "999" if self.outside else "30"
        return {"goodsstocks": [{
            "profileid": "10", "ktypeid": BUSINESS_WAREHOUSE_ID,
            "ptypeid": product, "skuid": "31", "qty": self.qty, "saleqty": self.saleable,
        }], "totalresults": 1}


def test_inventory_subset_refresh_is_atomic_and_update_job_is_idempotent(store):
    persist_records(store, [inventory_row()])
    demand = {**inventory_row(), "id":"order-1", "order_no":"A", "line_id":"1",
              "quantity":"20"}
    with store.connect(write=True) as database:
        database.execute(
            "INSERT INTO records VALUES ('orders',?,?,?,?,?,?,?,?,?,?)",
            (demand["id"], dumps(demand), demand["sku"], "", "", demand["sku"],
             "", None, None, ""),
        )
    source = type("Source", (), {})()
    source.refresh_inventory_subset = InventorySubsetRefresher(store, StockClient()).refresh
    jobs = UpdateJobs(store, source)
    identity = str(uuid.uuid4())
    jobs.enqueue(identity, "inventory_subset", {"product_ids": ["30"]})
    assert jobs.process_one() is True
    assert jobs.get(identity)["status"] == "succeeded"
    assert jobs.enqueue(identity, "inventory_subset", {"product_ids": ["30"]})["id"] == identity
    with store.connect() as database:
        payload = json.loads(database.execute(
            "SELECT payload FROM records WHERE kind='inventory'"
        ).fetchone()[0])
    assert payload["available_stock"] == "55"
    assert payload["purchase_pending"] == "5"
    assert payload["inferred_other_reserved"] == "0"
    assert payload["effective_physical"] == "70"


def test_inventory_subset_records_other_draft_reservation_instead_of_negative_pending(store):
    persist_records(store, [inventory_row()])
    demand = {**inventory_row(), "id":"order-1", "order_no":"A", "line_id":"1",
              "quantity":"20"}
    with store.connect(write=True) as database:
        database.execute(
            "INSERT INTO records VALUES ('orders',?,?,?,?,?,?,?,?,?,?)",
            (demand["id"], dumps(demand), demand["sku"], "", "", demand["sku"],
             "", None, None, ""),
        )
    refresher = InventorySubsetRefresher(store, StockClient(qty="70", saleable="45"))
    refresher.refresh(product_ids=["30"])
    with store.connect() as database:
        payload = json.loads(database.execute(
            "SELECT payload FROM records WHERE kind='inventory'"
        ).fetchone()[0])
    assert payload["purchase_pending"] == "0"
    assert payload["inferred_other_reserved"] == "5"
    assert payload["effective_physical"] == "65"


def test_inventory_subset_keeps_negative_physical_queryable_without_demand(store):
    persist_records(store, [inventory_row()])
    refresher = InventorySubsetRefresher(
        store, StockClient(qty="-443", saleable="-443"))
    refresher.refresh(product_ids=["30"])
    with store.connect() as database:
        payload = json.loads(database.execute(
            "SELECT payload FROM records WHERE kind='inventory'"
        ).fetchone()[0])
    assert payload["stock_quantity"] == "-443"
    assert payload["available_stock"] == "-443"
    assert payload["purchase_pending"] == "0"
    assert payload["inferred_other_reserved"] == "0"
    assert payload["effective_physical"] == "0"


def test_inventory_subset_does_not_hide_request_behind_unrelated_active_job(store):
    jobs = UpdateJobs(store, object())
    first = str(uuid.uuid4())
    jobs.enqueue(first, "refresh")
    with pytest.raises(JobConflict) as caught:
        jobs.enqueue(str(uuid.uuid4()), "inventory_subset", {"product_ids": ["30"]})
    assert caught.value.code == "update_busy"


def test_inventory_subset_out_of_scope_response_does_not_publish(store):
    persist_records(store, [inventory_row()])
    refresher = InventorySubsetRefresher(store, StockClient(outside=True))
    with pytest.raises(SourceIncomplete):
        refresher.refresh(product_ids=["30"])
    assert store.rows("inventory")[0]["available_stock"] == "60"
