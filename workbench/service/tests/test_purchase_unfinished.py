"""The Android API consumes only complete, validated purchase snapshots."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook

from test_staff_auth import admin_session, create_worker, public_app
from core.purchase_unfinished import ensure_schema


SNAPSHOT_ID = "20261006T000000Z-0123456789ab-01234567"
HEADERS = ["行号", "单位编号", "单位名称", "商品编号", "商品名称",
           "订单编号", "仓库", "订货日期", "到货日期", "订货数量",
           "完成数量", "终止完成数量", "未完成数量", "未完成金额", "客户名称"]


def publish(root: Path, *, duplicate=False, snapshot_id=SNAPSHOT_ID,
            account_scope="a" * 64):
    directory = root / "snapshots" / snapshot_id
    directory.mkdir(parents=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "进货订单统计列表"
    sheet.append(["进货订单统计列表"])
    sheet.append(["开始时间", "2026-10-01"])
    sheet.append(["结束时间", "2026-10-06"])
    sheet.append([])
    sheet.append(HEADERS)
    first = [1, "S01", "供应商甲", "LS0001", "螺栓", "PO1", "仓库甲",
             "2026-10-02", "", 5, 2, 0, 3, 9999, "客户机密"]
    second = [2, "S01", "供应商甲", "LS0001" if duplicate else "LS0002",
              "螺母", "PO1" if duplicate else "PO2", "仓库甲",
              "2026-10-02", "", 2, 1, 0, 1, 8888, "客户机密"]
    sheet.append(first)
    sheet.append(second)
    sheet.append(["合计", None, None, None, None, None, None, None, None,
                  None, None, None, 4])
    path = directory / "purchase-unfinished.xlsx"
    workbook.save(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {
        "status": "validated", "source": "beefun_browser_export",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "web_row_count": 2, "snapshot_id": snapshot_id,
        "sheet": "进货订单统计列表", "header_row": 5, "columns": HEADERS,
        "query_start": "2026-10-01", "query_end": "2026-10-06",
        "filter": "未完成数量 >= 0.1", "row_count": 2,
        "order_count": 1 if duplicate else 2, "supplier_count": 1,
        "duplicate_candidate_key_groups": 1 if duplicate else 0,
        "minimum_unfinished": "1", "unfinished_sum": "4",
        "sha256": digest, "bytes": len(raw),
        "units_verified": False, "coverage_complete": False,
    }
    if account_scope is not None:
        manifest["account_scope"] = account_scope
        manifest["account_scope_basis"] = "login_company_and_verified_erp_host_v1"
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    (root / "latest.json").write_text(json.dumps({
        "snapshot_id": snapshot_id,
        "manifest": f"snapshots/{snapshot_id}/manifest.json",
    }))
    return path


def test_missing_snapshot_is_visible_without_preventing_service_start(public_app):
    client = public_app.test_client()
    admin = admin_session(client)
    status = client.get("/api/v2/purchase-unfinished/status", headers=admin)
    rows = client.get("/api/v2/purchase-unfinished", headers=admin)
    assert status.status_code == 200
    assert status.get_json()["status"] == "unavailable"
    assert rows.status_code == 503
    assert rows.get_json()["error"] == "snapshot_unavailable"


def test_valid_collector_trial_is_readable_but_cannot_record_business_replies(public_app, tmp_path):
    formal, trial = tmp_path / "formal", tmp_path / "trial"
    formal.mkdir()
    publish(trial)
    engine = public_app.extensions["service"]["purchase_unfinished"]
    engine.root, engine.preview_root = formal, trial
    client = public_app.test_client()
    admin = admin_session(client)

    status = client.get("/api/v2/purchase-unfinished/status", headers=admin).get_json()
    assert status["status"] == "review"
    assert status["publication_stage"] == "internal_trial"
    assert status["account_scope_verified"] is True
    assert status["units_verified"] is False and status["coverage_complete"] is False
    suppliers = client.get("/api/v2/purchase-unfinished/suppliers", headers=admin).get_json()
    assert suppliers["publication_stage"] == "internal_trial" and len(suppliers["items"]) == 1
    create_worker(client, admin)
    buyer_login = client.post("/api/v2/auth/login", json={
        "username": "buyer.one", "password": "worker-password"}).get_json()
    buyer = {"Authorization": "Bearer " + buyer_login["token"]}
    create_worker(client, admin, username="sales.one", roles=["sales"])
    sales_login = client.post("/api/v2/auth/login", json={
        "username": "sales.one", "password": "worker-password"}).get_json()
    sales = {"Authorization": "Bearer " + sales_login["token"]}
    assert client.get("/api/v2/purchase-unfinished/suppliers", headers=buyer).status_code == 200
    assert client.get("/api/v2/purchase-unfinished/suppliers", headers=sales).status_code == 403
    rows = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    assert rows["publication_stage"] == "internal_trial" and rows["total"] == 2
    assert "客户机密" not in json.dumps(rows, ensure_ascii=False)
    preview = client.get("/api/v2/purchase-unfinished/suppliers/S01/reminder-preview",
                         headers=admin).get_json()
    assert preview["draft_only"] and preview["send_allowed"] is False

    line_key = rows["items"][0]["line_key"]
    response = client.put(f"/api/v2/purchase-unfinished/promises/{line_key}", headers=admin,
                          json={"snapshot_id": status["snapshot_id"], "version": 0,
                                "idempotency_key": "preview-must-not-write",
                                "promised_date": "2026-10-12", "note": "内部试用"})
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error"] == "purchase_preview_read_only"
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM purchase_supplier_promise_events").fetchone()[0] == 0

    (trial / "collector-status.json").write_text(json.dumps({"status": "failed",
        "reason": "network_interrupted", "updated_at_utc": datetime.now(timezone.utc).isoformat()}))
    retained = client.get("/api/v2/purchase-unfinished/status", headers=admin).get_json()
    assert retained["snapshot_id"] == status["snapshot_id"] and retained["row_count"] == 2
    assert retained["collector"]["status"] == "failed"

    # A formal pointer replaces the trial; a damaged formal pointer cannot
    # silently fall back to the trial's seemingly healthy numbers.
    published = publish(formal)
    assert client.get("/api/v2/purchase-unfinished/status", headers=admin).get_json()["publication_stage"] == "formal"
    published.write_bytes(published.read_bytes() + b"tamper")
    assert client.get("/api/v2/purchase-unfinished/status", headers=admin).get_json()["status"] == "invalid"
    assert client.get("/api/v2/purchase-unfinished", headers=admin).status_code == 422


def test_collector_needs_human_does_not_replace_last_success_or_expose_secrets(public_app,tmp_path):
    publish(tmp_path)
    engine=public_app.extensions['service']['purchase_unfinished']
    engine.root=tmp_path
    state={'status':'needs_human','reason':'captcha_required',
           'updated_at_utc':'2026-10-06T02:00:00+00:00',
           'password':'never-forward','url':'https://example.invalid/?token=secret'}
    (tmp_path/'collector-status.json').write_text(json.dumps(state))
    client=public_app.test_client();admin=admin_session(client)
    result=client.get('/api/v2/purchase-unfinished/status',headers=admin).get_json()
    assert result['status']=='ready' and result['row_count']==2
    assert result['collector']['status']=='needs_human'
    assert 'never-forward' not in json.dumps(result) and 'token=secret' not in json.dumps(result)
    state['reason']='https://example.invalid/?password=secret'
    (tmp_path/'collector-status.json').write_text(json.dumps(state))
    result=engine.status({'id':'admin','is_admin':True})
    assert 'reason' not in result['collector']


def test_valid_snapshot_is_role_scoped_and_reminder_hides_private_fields(public_app, tmp_path):
    publish(tmp_path)
    public_app.extensions["service"]["purchase_unfinished"].root = tmp_path
    client = public_app.test_client()
    admin = admin_session(client)
    create_worker(client, admin)
    buyer_login = client.post("/api/v2/auth/login", json={
        "username": "buyer.one", "password": "worker-password"}).get_json()
    buyer = {"Authorization": "Bearer " + buyer_login["token"]}
    create_worker(client, admin, username="sales.one", roles=["sales"])
    sales_login = client.post("/api/v2/auth/login", json={
        "username": "sales.one", "password": "worker-password"}).get_json()
    sales = {"Authorization": "Bearer " + sales_login["token"]}
    assert client.get("/api/v2/purchase-unfinished", headers=sales).status_code == 403
    response = client.get("/api/v2/purchase-unfinished", headers=buyer)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["row_count"] == 2 and payload["total"] == 2
    assert payload["units_verified"] is False and payload["coverage_complete"] is False
    assert payload["items"][0]["unit"] == ""
    preview = client.get("/api/v2/purchase-unfinished/suppliers/S01/reminder-preview",
                         headers=buyer).get_json()
    assert preview["draft_only"] and not preview["send_allowed"]
    assert "amount" not in json.dumps(preview, ensure_ascii=False).lower()
    assert "客户机密" not in json.dumps(preview, ensure_ascii=False)


def test_hash_or_pointer_tampering_never_returns_partial_rows(public_app, tmp_path):
    path = publish(tmp_path)
    public_app.extensions["service"]["purchase_unfinished"].root = tmp_path
    client = public_app.test_client()
    admin = admin_session(client)
    path.write_bytes(path.read_bytes() + b"x")
    assert client.get("/api/v2/purchase-unfinished/status", headers=admin).get_json()["status"] == "invalid"
    assert client.get("/api/v2/purchase-unfinished", headers=admin).status_code == 422
    pointer = tmp_path / "latest.json"
    pointer.write_text(json.dumps({"snapshot_id": SNAPSHOT_ID,
                                   "manifest": "../../secrets/erp.json"}))
    assert client.get("/api/v2/purchase-unfinished", headers=admin).get_json()["reason"] == "snapshot_pointer_invalid"


def test_supplier_reply_persists_across_snapshot_refresh_and_ambiguous_rows_block_write(public_app, tmp_path):
    publish(tmp_path)
    engine = public_app.extensions["service"]["purchase_unfinished"]
    engine.root = tmp_path
    client = public_app.test_client()
    admin = admin_session(client)
    first = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    line_key = first["items"][0]["line_key"]
    body = {"snapshot_id": SNAPSHOT_ID, "version": 0,
            "idempotency_key": "promise-one", "promised_date": "2026-10-12",
            "note": "供应商电话回复"}
    saved = client.put(f"/api/v2/purchase-unfinished/promises/{line_key}",
                       headers=admin, json=body)
    assert saved.status_code == 200
    assert saved.get_json()["item"]["version"] == 1
    replay = client.put(f"/api/v2/purchase-unfinished/promises/{line_key}",
                        headers=admin, json=body)
    assert replay.get_json()["replayed"] is True
    publish(tmp_path, duplicate=True,
            snapshot_id="20261007T000000Z-0123456789ab-01234567")
    promises = client.get("/api/v2/purchase-unfinished/promises", headers=admin).get_json()
    assert len(promises["items"]) == 1
    assert promises["items"][0]["promised_date"] == "2026-10-12"
    current = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    assert all(item["identity_ambiguous"] for item in current["items"])
    assert all(item["promised_date"] == "" and item["promise_version"] == 1
               and item["promise_requires_confirmation"]
               for item in current["items"])
    attempted = client.put(f"/api/v2/purchase-unfinished/promises/{line_key}",
                           headers=admin, json={**body, "snapshot_id": current["snapshot_id"],
                                                "version": 1, "idempotency_key": "promise-two"})
    assert attempted.status_code == 409
    assert attempted.get_json()["error"] == "purchase_line_conflict"


def test_promises_do_not_cross_login_account_scopes(public_app, tmp_path):
    publish(tmp_path, account_scope="a" * 64)
    public_app.extensions["service"]["purchase_unfinished"].root = tmp_path
    client = public_app.test_client()
    admin = admin_session(client)
    account_a = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    old_key = account_a["items"][0]["line_key"]
    body = {"snapshot_id": SNAPSHOT_ID, "version": 0,
            "idempotency_key": "scope-a", "promised_date": "2026-10-12",
            "note": "仅属于账套A"}
    assert client.put(f"/api/v2/purchase-unfinished/promises/{old_key}",
                      headers=admin, json=body).status_code == 200

    publish(tmp_path, account_scope="b" * 64,
            snapshot_id="20261007T000000Z-0123456789ab-01234567")
    account_b = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    assert account_b["items"][0]["line_key"] != old_key
    assert account_b["items"][0]["promised_date"] == ""
    assert client.get("/api/v2/purchase-unfinished/promises", headers=admin).get_json()["items"] == []
    assert client.get(f"/api/v2/purchase-unfinished/promises/{old_key}/events",
                      headers=admin).status_code == 404
    stale_write = client.put(f"/api/v2/purchase-unfinished/promises/{old_key}",
                             headers=admin, json=body)
    assert stale_write.status_code == 409

    publish(tmp_path, account_scope="a" * 64,
            snapshot_id="20261008T000000Z-0123456789ab-01234567")
    restored = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    assert restored["items"][0]["line_key"] == old_key
    assert restored["items"][0]["promised_date"] == ""
    assert restored["items"][0]["prior_promised_date"] == "2026-10-12"
    assert restored["items"][0]["promise_requires_confirmation"] is True


def test_legacy_unscoped_snapshot_remains_readable_but_cannot_attach_promises(public_app, tmp_path):
    publish(tmp_path, account_scope=None)
    public_app.extensions["service"]["purchase_unfinished"].root = tmp_path
    client = public_app.test_client()
    admin = admin_session(client)
    current = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    assert current["status"] == "review"
    assert current["account_scope_verified"] is False
    assert current["total"] == 2
    line_key = current["items"][0]["line_key"]
    write = client.put(f"/api/v2/purchase-unfinished/promises/{line_key}",
                       headers=admin, json={"snapshot_id": SNAPSHOT_ID,
                           "version": 0, "idempotency_key": "legacy",
                           "promised_date": "2026-10-12", "note": "旧快照"})
    assert write.status_code == 403
    assert write.get_json()["error"] == "purchase_account_scope_review"


def test_old_promise_table_migrates_without_assigning_legacy_notes_to_new_account(public_app, tmp_path):
    publish(tmp_path)
    engine = public_app.extensions["service"]["purchase_unfinished"]
    engine.root = tmp_path
    old_key = hashlib.sha256(json.dumps(
        ["S01", "PO1", "LS0001", "仓库甲", "2026-10-02"],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:32]
    with engine.store.connect(write=True) as db:
        db.execute("DROP TABLE purchase_supplier_promises")
        db.execute("""CREATE TABLE purchase_supplier_promises(
            line_key TEXT PRIMARY KEY, supplier_code TEXT NOT NULL, order_no TEXT NOT NULL,
            product_code TEXT NOT NULL, warehouse TEXT NOT NULL, order_date TEXT NOT NULL,
            promised_date TEXT NOT NULL, note TEXT NOT NULL, version INTEGER NOT NULL,
            updated_by TEXT NOT NULL, source_snapshot_id TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO purchase_supplier_promises VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            old_key, "S01", "PO1", "LS0001", "仓库甲", "2026-10-02",
            "2026-10-12", "旧账套来源未核实", 1, "admin", SNAPSHOT_ID,
            "2026-10-06T00:00:00", "2026-10-06T00:00:00"))
    ensure_schema(engine.store)
    with engine.store.connect() as db:
        saved = db.execute("SELECT account_scope,note FROM purchase_supplier_promises WHERE line_key=?",
                           (old_key,)).fetchone()
        assert saved["account_scope"] == ""
        assert saved["note"] == "旧账套来源未核实"
    client = public_app.test_client()
    admin = admin_session(client)
    current = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    assert current["items"][0]["line_key"] != old_key
    assert current["items"][0]["promised_date"] == ""
    assert client.get("/api/v2/purchase-unfinished/promises", headers=admin).get_json()["items"] == []


def test_unique_key_after_duplicate_disappears_never_inherits_reply_without_confirmation(public_app, tmp_path):
    publish(tmp_path)
    engine = public_app.extensions["service"]["purchase_unfinished"]
    engine.root = tmp_path
    client = public_app.test_client()
    admin = admin_session(client)
    initial = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()
    line_key = initial["items"][0]["line_key"]
    body = {"snapshot_id": SNAPSHOT_ID, "version": 0, "idempotency_key": "reply-original-line",
            "promised_date": "2026-10-12", "note": "原明细供应商回复"}
    endpoint = f"/api/v2/purchase-unfinished/promises/{line_key}"
    assert client.put(endpoint, headers=admin, json=body).status_code == 200
    same = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()["items"][0]
    assert same["promised_date"] == "2026-10-12" and not same["promise_requires_confirmation"]

    publish(tmp_path, duplicate=True, snapshot_id="20261007T000000Z-0123456789ab-01234567")
    # A second same-product detail becomes visible, then the original may close.
    latest_id = "20261008T000000Z-0123456789ab-01234567"
    publish(tmp_path, snapshot_id=latest_id)
    new = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()["items"][0]
    assert new["line_key"] == line_key and not new["identity_ambiguous"]
    assert new["promised_date"] == new["promise_note"] == ""
    assert new["promise_requires_confirmation"] and new["promise_version"] == 1
    assert new["prior_promised_date"] == "2026-10-12"
    assert new["prior_promise_note"] == "原明细供应商回复"
    assert new["source_snapshot_id"] == SNAPSHOT_ID
    # Reusing an old key cannot silently turn a historical reply into current fact.
    replay_on_new = client.put(endpoint, headers=admin,
        json={**body, "snapshot_id": latest_id, "version": 1})
    assert replay_on_new.status_code == 409
    assert replay_on_new.json["error"] == "idempotency_conflict"
    stale = client.put(endpoint, headers=admin,
        json={**body, "snapshot_id": latest_id, "version": 0, "idempotency_key":"stale-reconfirmation"})
    assert stale.status_code == 409
    confirmation = {**body, "snapshot_id": latest_id, "version": 1,
                    "idempotency_key":"new-confirmation", "note":"已核对当前明细并重新确认交期"}
    assert client.put(endpoint, headers=admin, json=confirmation).status_code == 200
    assert client.put(endpoint, headers=admin, json=confirmation).json["replayed"]
    current = client.get("/api/v2/purchase-unfinished", headers=admin).get_json()["items"][0]
    assert current["promised_date"] == "2026-10-12" and not current["promise_requires_confirmation"]
    assert current["promise_version"] == 2 and current["source_snapshot_id"] == latest_id
    history = client.get(endpoint+"/events", headers=admin).get_json()["items"]
    assert len(history) == 2
    assert {event["source_snapshot_id"] for event in history} == {SNAPSHOT_ID, latest_id}
