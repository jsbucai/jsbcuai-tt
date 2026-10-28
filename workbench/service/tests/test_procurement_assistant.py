"""A purchase click must reserve once and never skip missing evidence."""
import sys
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.procurement_whitelist import read_whitelist, stage_whitelist
from core.store import dumps
from test_staff_auth import public_app, admin_session


def _task(app, *, code="LS0192", version=1):
    engine = app.extensions["service"]["procurement_assistant"]
    with engine.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps("v1"),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_source_updated_at',?)", (dumps("2026-10-06T07:30:00+08:00"),))
        db.execute("UPDATE daily_work_settings SET value='v1' WHERE key='projection_data_version'")
        db.execute("UPDATE daily_work_settings SET value='2026-10-06T07:30:00+08:00' WHERE key='projection_source_updated_at'")
        db.execute("""INSERT INTO daily_work_tasks
            (id,business_key,role,task_type,status,condition_state,profile_id,sku,
             title,payload,version,first_seen_at,last_seen_at,created_at,updated_at,source_data_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("purchase:1", "purchase:1", "purchase", "procurement_gap", "todo", "active",
             "P", code, "安排采购 · " + code,
             dumps({"shortage": "40", "order_nos": ["S1"], "product_identity": code}),
             version, "2026-10-06", "2026-10-06", "2026-10-06", "2026-10-06", "v1"))
        db.execute("INSERT INTO purchase_whitelist_sources VALUES (?,?,?,?,?,?)",
                   ("test-source", "a" * 64, "2026-10-06", 1, 1, 0))
        db.execute("""INSERT INTO purchase_whitelist_entries
            (id,profile_id,erp_code,platform,platform_sku,product_name,
             spec_check,model_check,enabled,review_reason,source_id,source_row,imported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("mapping-1", "P", code, "淘钉钉", "1145028", "螺栓", "M20*90",
             "8.8级发黑", 1, "", "test-source", 2, "2026-10-06"))


def test_enroll_is_durable_and_idempotent_without_fresh_price(public_app):
    _task(public_app)
    client = public_app.test_client()
    headers = admin_session(client)
    listed = client.get("/api/v2/assistant/purchase/plan", headers=headers)
    assert listed.status_code == 200
    payload = listed.get_json()
    assert len(payload["items"]) == 1
    assert payload['whitelist_counts'] == {'candidate_total': 1, 'verified_total': 1,
                                         'review_total': 0, 'matched_demand_total': 1}
    assert payload["items"][0]["reason"] == "销售价格资料待更新"
    body = {"version": 1, "source_version": "v1", "idempotency_key": "purchase-click-1"}
    accepted = client.post("/api/v2/assistant/purchase/plan/purchase:1/enroll",
                           json=body, headers=headers)
    assert accepted.status_code == 200, accepted.get_json()
    result = accepted.get_json()
    assert result["job"]["status"] == "awaiting_price_source"
    replay = client.post("/api/v2/assistant/purchase/plan/purchase:1/enroll",
                         json=body, headers=headers)
    assert replay.status_code == 200 and replay.get_json()["replayed"] is True
    with public_app.extensions["service"]["store"].connect() as db:
        assert db.execute("SELECT count(*) FROM purchase_assistant_plans").fetchone()[0] == 1
        assert db.execute("SELECT status FROM daily_work_tasks WHERE id='purchase:1'").fetchone()[0] == "in_progress"
        assert db.execute("SELECT count(*) FROM purchase_assistant_jobs").fetchone()[0] == 1


def test_enroll_rejects_changed_version_without_partial_start(public_app):
    _task(public_app)
    client = public_app.test_client()
    headers = admin_session(client)
    response = client.post("/api/v2/assistant/purchase/plan/purchase:1/enroll",
        json={"version": 0, "source_version": "v1", "idempotency_key": "purchase-click-2"},
        headers=headers)
    assert response.status_code == 409
    with public_app.extensions["service"]["store"].connect() as db:
        assert db.execute("SELECT status FROM daily_work_tasks WHERE id='purchase:1'").fetchone()[0] == "todo"
        assert db.execute("SELECT count(*) FROM purchase_assistant_plans").fetchone()[0] == 0


def test_enroll_is_blocked_during_restore_hold_or_stale_projection(public_app):
    _task(public_app)
    client=public_app.test_client();headers=admin_session(client)
    store=public_app.extensions['service']['store']
    body={"version":1,"source_version":"v1","idempotency_key":"purchase-click-restore"}
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('restoration_hold','true')")
    response=client.post('/api/v2/assistant/purchase/plan/purchase:1/enroll',json=body,headers=headers)
    assert response.status_code==409 and response.get_json()['error']=='restoration_hold'
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('restoration_hold','false')")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)",(dumps('v2'),))
    response=client.post('/api/v2/assistant/purchase/plan/purchase:1/enroll',json=body,headers=headers)
    assert response.status_code==409 and response.get_json()['error']=='task_projection_conflict'
    listed=client.get('/api/v2/assistant/purchase/plan',headers=headers).get_json()['items'][0]
    assert not listed['can_enroll'] and listed['reason']=='任务尚未跟上最新订单数据'
    with store.connect() as db:
        assert db.execute("SELECT status FROM daily_work_tasks WHERE id='purchase:1'").fetchone()[0]=='todo'
        assert db.execute('SELECT count(*) FROM purchase_assistant_plans').fetchone()[0]==0


def test_basic_unit_price_candidate_never_queues_payment_without_pack_conversion(public_app):
    _task(public_app)
    store=public_app.extensions['service']['store']
    with store.connect(write=True) as db:
        db.execute('INSERT INTO purchase_price_bounds VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('purchase:1','P','LS0192','LS0192','v1','base_unit:1','5','4',
             'tax_inclusive','b'*64,'2026-10-06T07:30:00+08:00'))
    client=public_app.test_client();headers=admin_session(client)
    item=client.get('/api/v2/assistant/purchase/plan',headers=headers).get_json()['items'][0]
    assert item['price_limit']=='4' and item['unit']=='基本单位（包装待核对）'
    assert not item['unit_verified'] and item['reason']=='基本单位与平台包装待核对'
    response=client.post('/api/v2/assistant/purchase/plan/purchase:1/enroll',
        json={'version':1,'source_version':'v1','idempotency_key':'purchase-click-unit'},headers=headers)
    assert response.status_code==200
    assert response.get_json()['job']['status']=='awaiting_unit_review'


def test_whitelist_import_is_staged_not_enabled(public_app, tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "采购正式白名单"
    sheet.append(["启用", "ERP商品编号", "平台", "平台商品链接/SKU", "商品名校验",
                  "标准校验", "规格校验", "型号/材质/表面处理校验", "是否自营",
                  "最小包装", "起订量", "成交单价", "单价上限"])
    sheet.append(["是", "LS0192", "淘钉钉", "1145028", "螺栓", "GB5783", "M20*90",
                  "8.8级发黑", "是", "0.001千支/支", "1支", 1, 2])
    path = tmp_path / "采购正式白名单.xlsx"
    workbook.save(path)
    digest, rows = read_whitelist(path)
    assert len(digest) == 64 and rows[0]["erp_code"] == "LS0192"
    engine = public_app.extensions["service"]["procurement_assistant"]
    first = stage_whitelist(engine.store, path)
    assert first["row_count"] == 1 and first["valid_count"] == 1
    assert stage_whitelist(engine.store, path)["replayed"] is True
    with engine.store.connect() as db:
        entry = db.execute("SELECT enabled,review_reason FROM purchase_whitelist_entries").fetchone()
        assert entry["enabled"] == 0 and entry["review_reason"] == "待平台实时复核"
    client = public_app.test_client()
    listed = client.get('/api/v2/assistant/purchase/plan', headers=admin_session(client)).get_json()
    assert listed['whitelist_counts'] == {'candidate_total': 1, 'verified_total': 0,
                                         'review_total': 1, 'matched_demand_total': 0}
    assert listed['items'] == []
