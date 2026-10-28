"""The browser lane must survive repeats and crashes without replaying writes."""
import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from core.store import Store
from core.web_operations import WebOperationConflict, WebOperations
from tools.web_operation_worker import _run_one
from test_staff_auth import admin_session, public_app


SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def queue():
    with tempfile.TemporaryDirectory(dir=SERVICE / "runtime") as runtime:
        yield WebOperations(Store(runtime))


def add(queue, kind, key, actor="sales-1"):
    return queue.enqueue(kind=kind, actor_id=actor,
        role="sales" if kind == "sales_order_import" else "purchase",
        profile_id="08390", target_id=key, key=key,
        payload={"slot": key} if kind == "purchase_download" else {"confirmed_version": 4},
        total_steps=4)


def test_fifo_and_replay_do_not_duplicate_work(queue):
    first = add(queue, "purchase_order_import", "purchase-20261007-001", "buyer-1")
    assert add(queue, "purchase_order_import", "purchase-20261007-001", "buyer-1")["replayed"]
    with pytest.raises(WebOperationConflict) as conflict:
        queue.enqueue(kind="purchase_download", actor_id="buyer-1", role="purchase",
            profile_id="08390", target_id="other", key="purchase-20261007-001", payload={})
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(WebOperationConflict) as conflict:
        queue.enqueue(kind="purchase_order_import", actor_id="buyer-1", role="purchase",
            profile_id="08390", target_id="purchase-20261007-001", key="another-key-20261007",
            payload={"confirmed_version": 4})
    assert conflict.value.code == "target_already_enqueued"
    second = add(queue, "purchase_order_import", "purchase-20261007-002", "buyer-1")
    token = "worker-token-123456"
    assert queue.claim_next(token)["id"] == first["operation"]["id"]
    assert queue.claim_next("other-worker-token") is None
    queue.progress(first["operation"]["id"], token, step=2, stage="网页上传", detail="一次尝试")
    with pytest.raises(WebOperationConflict) as conflict:
        queue.progress(first["operation"]["id"], token, step=1, stage="倒退", detail="不允许")
    assert conflict.value.code == "progress_regressed"
    with pytest.raises(WebOperationConflict) as conflict:
        queue.finish(first["operation"]["id"], token, status="succeeded",
                     stage="完成", detail="仅点击完成")
    assert conflict.value.code == "readback_required"
    queue.finish(first["operation"]["id"], token, status="uncertain",
                 stage="ERP 待核对", detail="上传结果未知")
    assert queue.claim_next("other-worker-token") is None
    queue.resolve(first["operation"]["id"], status="succeeded", detail="精确单号回查 1 张",
                  result={"verified": True, "reference": "UL20261007TEST"}, actor_id="admin")
    assert queue.claim_next("other-worker-token")["id"] == second["operation"]["id"]
    events = queue.detail({"id": "buyer-1", "roles": ["purchase"]}, first["operation"]["id"])["events"]
    assert [event["status"] for event in events] == ["queued", "running", "running", "uncertain", "succeeded"]


def test_sales_generic_finish_and_resolve_cannot_claim_erp_success(queue):
    sale = add(queue, "sales_order_import", "sales-20261007-001")
    ident = sale["operation"]["id"]
    token = "worker-token-123456"
    assert queue.claim_next(token)["id"] == ident
    with pytest.raises(WebOperationConflict) as conflict:
        queue.finish(ident, token, status="succeeded", stage="完成",
            detail="人工声称已回查", result={"verified": True, "reference": "ULTEST"})
    assert conflict.value.code == "sales_reconciliation_required"
    with pytest.raises(WebOperationConflict) as conflict:
        queue.finish(ident, token, status="uncertain", stage="ERP 待核对",
                     detail="原上传结果未知")
    assert conflict.value.code == "sales_reconciliation_required"
    queue.hold_sales_operation(ident, token, status="uncertain",
                               code="web_result_unknown")
    for status, result in (("succeeded", {"verified": True, "reference": "ULTEST"}),
                           ("blocked", {})):
        with pytest.raises(WebOperationConflict) as conflict:
            queue.resolve(ident, status=status, detail="人工处理",
                          result=result, actor_id="admin")
        assert conflict.value.code == "sales_reconciliation_required"
    assert queue.detail({"id": "sales-1", "roles": ["sales"]}, ident)["operation"]["status"] == "uncertain"


def test_restart_holds_possible_write_and_privacy(queue):
    sale = add(queue, "sales_order_import", "sales-20261007-003")
    queue.claim_next("worker-token-123456")
    assert queue.recover_interrupted() == 1
    assert queue.detail({"id": "sales-1", "roles": ["sales"]}, sale["operation"]["id"])["operation"]["status"] == "uncertain"
    with pytest.raises(WebOperationConflict):
        queue.detail({"id": "sales-2", "roles": ["sales"]}, sale["operation"]["id"])
    with pytest.raises(WebOperationConflict):
        queue.list({"id": "packer", "roles": ["stock_packing"]})
    assert len(queue.list({"id": "admin", "is_admin": True})) == 1


def test_closed_unknown_sale_never_requeues_but_other_browser_work_can_continue(queue):
    sale = add(queue, 'sales_order_import', 'sales-unknown-closed')
    ident = sale['operation']['id']
    queue.claim_next('worker-token-123456')
    queue.hold_sales_operation(ident, 'worker-token-123456', status='uncertain',
                               code='web_import_unconfirmed')
    download = add(queue, 'purchase_download', 'download-after-unknown', 'buyer-1')
    # A regular unknown is not selected automatically for receipt recovery.
    assert queue.claim_sales_reconciliation('worker-token-234567') is None
    assert queue.claim_next('worker-token-234567')['id']==download['operation']['id']
    assert queue.detail({'id':'sales-1','roles':['sales']},ident)['operation']['status']=='uncertain'
    with pytest.raises(WebOperationConflict) as error:
        queue.enqueue(kind='sales_order_import', actor_id='sales-1', role='sales',
            profile_id='08390', target_id='sales-unknown-closed', key='other-unique-sale-key',
            payload={})
    assert error.value.code=='target_already_enqueued'


def test_interrupt_receipt_recovery_remains_possible_beside_older_unknown_sale(queue):
    first=add(queue,'sales_order_import','sales-old-unknown')
    queue.claim_next('worker-token-123456')
    queue.hold_sales_operation(first['operation']['id'],'worker-token-123456',
                               status='uncertain',code='web_import_unconfirmed')
    second=add(queue,'sales_order_import','sales-interrupted-later')
    assert queue.claim_next('worker-token-234567')['id']==second['operation']['id']
    queue.recover_interrupted()
    recovered=queue.claim_sales_reconciliation('worker-token-345678')
    assert recovered['id']==second['operation']['id'] and recovered['readback_only'] is True


def test_purchase_download_requires_new_valid_file(queue, tmp_path):
    root = tmp_path / "trial"
    root.mkdir()
    stale = "old-snapshot"
    (root / "latest.json").write_text(json.dumps({"snapshot_id": stale}), encoding="utf-8")
    fresh = "new-snapshot"
    directory = root / "snapshots" / fresh
    workbook = b"fake-xlsx-bytes"
    script = tmp_path / "collector.sh"
    script.write_text("#!/bin/sh\nmkdir -p '" + str(directory) + "'\n" +
        "printf 'fake-xlsx-bytes' > '" + str(directory / "purchase-unfinished.xlsx") + "'\n" +
        "printf '%s' '" + json.dumps({"snapshot_id": fresh,
            "manifest": f"snapshots/{fresh}/manifest.json"}) + "' > '" + str(root / "latest.json") + "'\n" +
        "printf '%s' '{\"status\":\"succeeded\"}' > '" + str(root / "collector-status.json") + "'\n",
        encoding="utf-8")
    script.chmod(0o700)
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "status": "validated", "snapshot_id": fresh, "row_count": 2,
        "web_row_count": 2, "bytes": len(workbook),
        "sha256": hashlib.sha256(workbook).hexdigest(),
        "units_verified": False, "coverage_complete": False,
    }), encoding="utf-8")
    item = add(queue, "purchase_download", "download-20261007-1130", "system:purchase-export")
    assert _run_one(queue, script, root) == item["operation"]["id"]
    result = queue.detail({"id": "buyer-1", "roles": ["purchase"]}, item["operation"]["id"])["operation"]
    assert result["status"] == "succeeded"
    assert result["result"]["reference"] == fresh
    assert result["result"]["formal_business_publish"] is False
    assert result["result"]["units_verified"] is False


def test_unvalidated_import_handler_never_calls_erp(queue, tmp_path):
    item = add(queue, "purchase_order_import", "purchase-20261007-004", "buyer-1")
    assert _run_one(queue, tmp_path / "does-not-exist", tmp_path) == item["operation"]["id"]
    result = queue.detail({"id": "buyer-1", "roles": ["purchase"]}, item["operation"]["id"])["operation"]
    assert result["status"] == "blocked"
    assert "尚未完成" in result["detail"]


def test_authenticated_progress_api_is_read_only_and_returns_events(public_app):
    queue = public_app.extensions["service"]["web_operations"]
    item = add(queue, "purchase_download", "download-20261007-1610", "system:purchase-export")
    client = public_app.test_client()
    assert client.get("/api/v2/assistant/web-operations").status_code == 401
    headers = admin_session(client)
    listing = client.get("/api/v2/assistant/web-operations", headers=headers)
    assert listing.status_code == 200
    assert listing.get_json()["items"][0]["id"] == item["operation"]["id"]
    detail = client.get("/api/v2/assistant/web-operations/" + item["operation"]["id"], headers=headers)
    assert detail.status_code == 200
    assert detail.get_json()["events"][0]["stage"] == "排队"
    assert client.post("/api/v2/assistant/web-operations", json={}, headers=headers).status_code == 405
