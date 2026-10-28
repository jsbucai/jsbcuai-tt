import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from flask import Flask, g

from core.daily_work import DailyWork, DailyWorkConflict, daily_work_routes, ensure_schema
from core.order_print_state import ensure_schema as ensure_print_schema, reset_order
from core.store import Store, dumps


NOW = datetime(2026, 10, 4, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr("core.daily_work.timestamp",lambda:NOW.isoformat())
    with tempfile.TemporaryDirectory(dir=SERVICE / "runtime") as runtime:
        value = Store(runtime)
        with value.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE staff_users(
                    id TEXT PRIMARY KEY,username TEXT,display_name TEXT,password_hash TEXT,
                    roles TEXT,is_admin INTEGER,active INTEGER,packing_limit INTEGER,
                    created_at TEXT,updated_at TEXT);
                CREATE TABLE logistics_calculations(
                    job_id TEXT PRIMARY KEY,profile TEXT,data_version TEXT,payload TEXT,created_at TEXT);
            """)
            users = [
                ("admin", "admin", "管理员", "", json.dumps(["sales", "purchase", "stock_packing", "arrival_packing"]), 1, 1, 3, "", ""),
                ("sales", "sales", "销售甲", "", json.dumps(["sales"]), 0, 1, 3, "", ""),
                ("buyer", "buyer", "采购甲", "", json.dumps(["purchase"]), 0, 1, 3, "", ""),
                ("packer", "packer", "包装甲", "", json.dumps(["stock_packing", "arrival_packing"]), 0, 1, 3, "", ""),
            ]
            db.executemany("INSERT INTO staff_users VALUES (?,?,?,?,?,?,?,?,?,?)", users)
        yield value


def user(identity, roles, admin=False):
    return {"id": identity, "roles": roles, "is_admin": admin, "active": True}


def order(number, sku, category, due="2026-10-05", customer="客户甲", process_state="", **extra):
    summary = "PO#PRJ#1#DRAW#品名#M8#8.8#锌#1#" + sku + ("#交期=" + due if due else "")
    return {"id": number + ":1", "profile_id": "P", "order_no": number, "line_id": "1",
            "sku": sku, "product_id": sku + "-p", "sku_id": sku + "-s", "warehouse_id": "08390",
            "base_unit": "个", "product_name": "测试商品", "shipping_id": "@10/05#" + number,
            "summary": summary, "system_note": summary, "quantity": "1",
            "buyer": customer, "customer_source": "wms_receiveraddress",
            "document_date": "2026-10-01", "logistics_company": category,
            "calculation_status": "ready", "process_state": process_state, **extra}


def enable_assignment(engine):
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_settings SET value='true' WHERE key='assignment_enabled'")
        db.execute("""INSERT OR IGNORE INTO sales_customer_owners
            (profile_id,customer_name,owner_id,status,source,updated_at)
            VALUES ('P','客户甲','sales','assigned','customer_confirmed',?)""", (NOW.isoformat(),))


def seed(store, rows, plans, allocations=None, source="2026-10-04T08:00:00+08:00", version="v1",
         blocked=None):
    calculation = {"plans": [{"tradeid": number, "freightname": category,
                               "freightnumber": "@10/05#" + number}
                              for number, category in plans.items()],
                   "allocations": allocations or [], "blocked_orders": blocked or []}
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps(version),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_source_updated_at',?)", (dumps(source),))
        db.execute("DELETE FROM records WHERE kind='orders'")
        for item in rows:
            db.execute("INSERT INTO records(kind,id,payload,sku) VALUES ('orders',?,?,?)",
                       (item["id"], dumps(item), item["sku"]))
        db.execute("DELETE FROM logistics_calculations")
        db.execute("INSERT INTO logistics_calculations VALUES (?,?,?,?,?)",
                   ("job-" + version, "P", version, dumps(calculation), source))


def test_refresh_derives_roles_groups_purchase_without_packing_cap(store):
    rows = [order(f"S{i}", f"LS000{i}", "LS库存件") for i in range(1, 5)]
    rows += [order("A1", "FB1000", "异常"), order("P1", "FB2000", "需采购"),
             order("C1", "FB3000", "催货"), order("O1", "FB4000", "待到货", due="2026-10-03")]
    plans = {item["order_no"]: item["logistics_company"] for item in rows}
    allocations = [
        {"demand": {"order": "A1", "sku": "FB1000"}, "shortage": 0, "pending": 0},
        {"demand": {"order": "P1", "sku": "FB2000"}, "shortage": 7, "pending": 0},
    ]
    seed(store, rows, plans, allocations)
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    result = engine.refresh(idempotency_key="first")
    assert result["created"] >= 9 and result["stale"] is False
    assert result["counts"] == {name: result[name] for name in
                                ("created", "updated", "condition_resolved", "business_completed")}

    admin = engine.list(user("admin", [], True), view="all")
    assert set(admin["roles"]) == {"sales", "purchase", "stock_packing", "arrival_packing"}
    assert any(item["task_type"] == "overdue_order" and item["order_no"] == "O1"
               for item in admin["items"])
    gap = next(item for item in admin["items"] if item["task_type"] == "procurement_gap")
    assert gap["sku"] == "FB2000" and gap["payload"]["order_nos"] == ["P1"]
    packing = [item for item in admin["items"] if item["role"] in {"stock_packing", "arrival_packing"}]
    assert sum(item["assignee_id"] == "packer" for item in packing) == 5
    assert sum(not item["assignee_id"] for item in packing) == 0
    assert next(item for item in packing if item["order_no"] == "A1")["role"] == "arrival_packing"
    assert admin["staff_load"]
    assert admin["summary"]["overdue"] >= 1

    replay = engine.refresh(idempotency_key="first")
    assert replay["replayed"] is True
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM daily_work_tasks").fetchone()[0] == result["created"]


def test_actions_are_optimistic_idempotent_and_packed_refills_slot(store):
    rows = [order(f"S{i}", f"LS000{i}", "LS库存件") for i in range(1, 5)]
    seed(store, rows, {item["order_no"]: "LS库存件" for item in rows})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    mine = engine.list(user("packer", ["stock_packing"]))["items"]
    assert len(mine) == 4
    target = mine[0]
    result = engine.action(target["id"], user("packer", ["stock_packing"]), "packed",
                           "pack-once", target["version"], "包装完成")
    assert result["task"]["status"] == "packed"
    admin_items = engine.list(user("admin", [], True), view="all")["items"]
    dispatch = [item for item in admin_items if item["task_type"] == "packed_waiting_dispatch"
                and item["order_no"] == target["order_no"]]
    assert len(dispatch) == 1 and dispatch[0]["role"] == "sales"
    replay = engine.action(target["id"], user("packer", ["stock_packing"]), "packed",
                           "pack-once", target["version"], "包装完成")
    assert replay["replayed"] is True
    assert len([item for item in engine.list(user("admin", [], True), view="all")["items"]
                if item["task_type"] == "packed_waiting_dispatch" and
                item["order_no"] == target["order_no"]]) == 1
    mine_after = engine.list(user("packer", ["stock_packing"]))["items"]
    assert sum(item["status"] == "todo" for item in mine_after) == 3
    assert sum(item["status"] == "packed" for item in mine_after) == 1
    with pytest.raises(DailyWorkConflict) as error:
        engine.action(target["id"], user("packer", ["stock_packing"]), "reopen",
                      "bad-reopen", result["task"]["version"], "")
    assert error.value.code == "admin_required"


def test_progress_defaults_follow_up_and_stale_package_is_unassigned(store):
    rows = [order("S1", "LS0001", "LS库存件"), order("C1", "FB3000", "催货")]
    seed(store, rows, {"S1": "LS库存件", "C1": "催货"},
         source="2026-10-03T20:00:00+08:00")
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    admin = engine.list(user("admin", [], True), view="all")
    pack = next(item for item in admin["items"] if item["role"] == "stock_packing")
    assert pack["assignee_id"] == "" and admin["stale"] is True
    sales = next(item for item in admin["items"] if item["role"] == "sales")
    progressed = engine.action(sales["id"], user("sales", ["sales"]), "progress",
                               "progress-1", sales["version"], "已联系采购")
    assert progressed["task"]["next_follow_up_date"] == "2026-10-05"


def test_erp_500_business_completes_prior_packing_task(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    engine.action(task["id"], user("packer", ["stock_packing"]), "packed",
                  "packed", task["version"], "")
    shipped = dict(item, process_state="500", shadow_demand=True,
                   lifecycle_state="shipped_unposted", confirmation_count=2,
                   logistics_company="已完成订单需结存", printable=False)
    seed(store, [shipped], {"S1": "已完成订单需结存"}, version="v2")
    result = engine.refresh()
    assert result["business_completed"] == 2
    assert result["business_completed_by_role"]["stock_packing"] == 1
    assert result["business_completed_by_role"]["sales"] == 1
    with store.connect() as db:
        row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task["id"],)).fetchone()
        assert row["status"] == "business_completed"
        assert row["condition_state"] == "resolved"
        event = db.execute("""SELECT payload FROM daily_work_events
            WHERE task_id=? AND event_type='erp_business_completed'""", (task["id"],)).fetchone()
        assert json.loads(event[0])["process_state"] == "500"
    completed = engine.list(user("admin", [], True), view="all")["completed_items"]
    assert {item["role"] for item in completed if item["order_no"] == "S1"} >= {
        "stock_packing", "sales"}


def test_single_500_leaves_app_without_fabricating_erp_completion(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    candidate = dict(item, process_state="500", lifecycle_state="candidate",
                     lifecycle_pending_review=True, shadow_demand=True, printable=False,
                     logistics_company="订单状态待核对")
    seed(store, [candidate], {"S1": "订单状态待核对"}, version="v2")
    result = engine.refresh()
    assert result["business_completed"] == 0
    assert engine.list(user("packer", ["stock_packing"]))["items"]==[]
    with pytest.raises(DailyWorkConflict) as error:
        engine.action(task["id"], user("packer", ["stock_packing"]), "packed",
                      "packed-during-lifecycle-review", task["version"], "包装完成")
    assert error.value.code == "task_condition_resolved"
    with store.connect() as db:
        row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task["id"],)).fetchone()
        assert row["status"] == "todo" and row["condition_state"] == "active"
        assert not db.execute("""SELECT 1 FROM daily_work_events
            WHERE task_id=? AND event_type='erp_business_completed'""", (task["id"],)).fetchone()
    confirmed = dict(candidate, lifecycle_state="shipped_unposted",
                     lifecycle_pending_review=False, lifecycle_confirmations=2)
    seed(store, [confirmed], {"S1": "已完成订单需结存"}, version="v3")
    completed = engine.refresh()
    assert completed["business_completed"] == 1
    with store.connect() as db:
        row = db.execute("SELECT status,condition_state FROM daily_work_tasks WHERE id=?",
                         (task["id"],)).fetchone()
        assert row["status"] == "business_completed" and row["condition_state"] == "resolved"


def test_source_change_preserves_staff_follow_up_and_status(store):
    item = order("C1", "FB3000", "催货")
    seed(store, [item], {"C1": "催货"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("sales", ["sales"]))["items"][0]
    handled = engine.action(task["id"], user("sales", ["sales"]), "wait", "wait-1",
                            task["version"], "客户明天回复", "2026-10-06")
    changed = dict(item, buyer="客户名称已更新")
    seed(store, [changed], {"C1": "催货"}, version="v2")
    engine.refresh()
    with store.connect() as db:
        row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task["id"],)).fetchone()
        assert row["status"] == "waiting"
        assert row["next_follow_up_date"] == "2026-10-06"
        assert row["version"] > handled["task"]["version"]


def test_attendance_excludes_staff_and_print_access_follows_owner(store):
    ensure_schema(store)
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    admin = user("admin", [], True)
    attendance = engine.set_attendance(admin, "packer", False)
    assert next(item for item in attendance["items"] if item["user_id"] == "packer")["on_duty"] is False
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine.refresh()
    task = next(item for item in engine.list(admin, view="all")["items"]
                if item["role"] == "stock_packing")
    assert task["assignee_id"] == ""
    with pytest.raises(DailyWorkConflict):
        engine.assert_print_access(user("packer", ["stock_packing"]), ["S1"], "P")
    engine.set_attendance(admin, "packer", True)
    updated = next(item for item in engine.list(admin, view="all")["items"] if item["id"] == task["id"])
    assert updated["assignee_id"] == "packer"
    assert engine.assert_print_access(user("packer", ["stock_packing"]), ["S1"], "P") is True


def test_assignment_rule_urgent_override_and_first_projection(store):
    with store.connect(write=True) as db:
        db.execute("INSERT INTO staff_users VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("sales2", "sales2", "销售乙", "", json.dumps(["sales"]), 0, 1, 3, "", ""))
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    admin = user("admin", [], True)
    # Existing phone rules remain migration input; legacy writes are rejected.
    with store.connect(write=True) as db:
        db.execute("INSERT INTO daily_work_assignment_rules VALUES ('legacy-sales','sales','customer','重点客户','sales2',100,1,'','')")
    with pytest.raises(DailyWorkConflict) as error:
        engine.set_assignment_rule(admin, "sales", "customer", "重点客户", "sales2")
    assert error.value.code == "client_upgrade_required"
    item = order("C1", "FB3000", "催货", customer="重点客户")
    seed(store, [item], {"C1": "催货"})
    engine.refresh()
    task = next(item for item in engine.list(admin, view="all")["items"]
                if item["role"] == "sales")
    assert task["assignee_id"] == "sales2"
    urgent = engine.set_urgent(task["id"], admin, True, "urgent-1", task["version"], "客户要求加急")
    assert urgent["task"]["urgent"] is True and urgent["task"]["priority"] == 0
    assert engine.set_urgent(task["id"], admin, True, "urgent-1", task["version"],
                             "客户要求加急")["replayed"] is True
    with store.connect() as db:
        assert db.execute("SELECT version FROM daily_work_schema WHERE singleton=1").fetchone()[0] == 3

    # A fresh engine on an existing published snapshot reconstructs only the
    # local projection; it does not need an ERP call or an update job.
    with store.connect(write=True) as db:
        db.execute("DELETE FROM daily_work_events")
        db.execute("DELETE FROM daily_work_tasks")
    rebuilt = DailyWork(store, now=lambda: NOW)
    assert rebuilt.list(admin, view="all")["items"]


@pytest.mark.parametrize("print_state", ["printed", "uncertain", "reserved"])
def test_historical_print_state_requires_review_and_cannot_be_claimed(store, print_state):
    ensure_print_schema(store)
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1',?,'device','job','hash','legacy','2026-10-03T08:00:00')""",
            (print_state,))
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = next(item for item in engine.list(user("admin", [], True), view="all")["items"]
                if item["role"] == "stock_packing")
    assert task["task_type"] == "print_history_review"
    assert task["status"] == "waiting" and task["assignee_id"] == ""
    assert task["print_state"] == print_state and task["order_nos"] == ["S1"]
    with pytest.raises(DailyWorkConflict) as error:
        engine.action(task["id"], user("packer", ["stock_packing"]), "start",
                      "claim-history", task["version"], "")
    assert error.value.code == "packing_assignment_required"


def test_manual_owner_of_historical_print_review_survives_refresh(store):
    ensure_print_schema(store)
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1','printed','device','legacy','hash','legacy','2026-10-03T08:00:00')""")
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                if row["task_type"] == "print_history_review")
    assigned = engine.transfer(task["id"], user("admin", [], True), "packer",
                               "assign-review", task["version"], "安排人工核对")["task"]
    seed(store, [item], {"S1": "LS库存件"}, version="v2")
    engine.refresh()
    refreshed = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                     if row["id"] == task["id"])
    assert assigned["assignee_id"] == "packer" and refreshed["assignee_id"] == "packer"


def test_stale_refresh_never_fills_preexisting_unassigned_packaging(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"}, source="2026-10-03T08:00:00+08:00")
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    first = next(item for item in engine.list(user("admin", [], True), view="all")["items"]
                 if item["role"] == "stock_packing")
    assert first["assignee_id"] == ""
    seed(store, [dict(item, buyer="更新客户")], {"S1": "LS库存件"},
         source="2026-10-03T23:59:00+08:00", version="v2")
    engine.refresh()
    second = next(item for item in engine.list(user("admin", [], True), view="all")["items"]
                  if item["id"] == first["id"])
    assert second["assignee_id"] == ""


def test_blueprint_card_and_attendance_contract(store):
    item = order("S1", "LS0001", "LS库存件", customer="合同客户")
    item["quantity"] = "12"
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    app = Flask(__name__)
    app.register_blueprint(daily_work_routes(engine))

    @app.before_request
    def actor():
        g.current_user = user("admin", [], True)

    client = app.test_client()
    version = client.get("/api/v2/daily-work/version").get_json()
    assert version == {
        "ok": True,
        "print_state_version": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "business_date": "2026-10-04",
        "data_version": "v1",
        "source_updated_at": "2026-10-04T08:00:00+08:00",
        "task_version": 1,
        "task_data_version": "v1",
        "tasks_current": True,
    }
    assert "items" not in version
    body = client.get("/api/v2/daily-work?view=all").get_json()
    assert body["ok"] is True
    card = next(item for item in body["items"] if item["role"] == "stock_packing")
    assert card["order_nos"] == ["S1"]
    assert card["assignee"] == {"id": "packer", "display_name": "包装甲"}
    assert card["assignee_name"] == "包装甲"
    assert card["follow_up_date"] == card["next_follow_up_date"]
    assert card["logistics_company"] == "LS库存件"
    assert card["quantity"] == "12" and card["packing_state"] == "todo"
    attendance = client.put("/api/v2/daily-work/attendance",
                            json={"user_id": "packer", "present": False}).get_json()
    packer = next(item for item in attendance["items"] if item["user_id"] == "packer")
    assert packer["present"] is False and packer["on_duty"] is False


def test_print_access_rechecks_current_role(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    with pytest.raises(DailyWorkConflict) as error:
        engine.assert_print_access(user("packer", []), ["S1"], "P")
    assert error.value.code == "role_forbidden"


def test_label_change_moves_active_packaging_to_review(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    handled = engine.action(task["id"], user("packer", ["stock_packing"]), "packed",
                            "packed-before-change", task["version"], "包装完成")["task"]
    changed = dict(item, quantity="2", summary=item["summary"].replace("#1#LS0001", "#2#LS0001"),
                   system_note=item["system_note"].replace("#1#LS0001", "#2#LS0001"))
    seed(store, [changed], {"S1": "LS库存件"}, version="v2")
    engine.refresh()
    reviewed = next(item for item in engine.list(user("packer", ["stock_packing"]))["items"]
                    if item["id"] == task["id"])
    assert reviewed["status"] == "waiting"
    assert "标签内容已变化" in reviewed["blocked_by"]
    assert reviewed["version"] > handled["version"]
    with store.connect() as db:
        assert db.execute("""SELECT 1 FROM daily_work_events WHERE task_id=?
            AND event_type='source_changed_requires_review'""", (task["id"],)).fetchone()
        dispatch = db.execute("""SELECT condition_state FROM daily_work_tasks
            WHERE task_type='packed_waiting_dispatch' AND order_no='S1'""").fetchone()
        assert dispatch["condition_state"] == "resolved"


def test_label_change_after_printed_todo_invalidates_old_output_and_blocks_packed(store):
    ensure_print_schema(store)
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    old_fingerprint = task["payload"]["label_fingerprint"]
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1','printed','device','job',?,'submitted','2026-10-04T10:00:00')""",
                   (old_fingerprint,))
    changed = dict(item, quantity="2", summary=item["summary"].replace("#1#LS0001", "#2#LS0001"),
                   system_note=item["system_note"].replace("#1#LS0001", "#2#LS0001"))
    seed(store, [changed], {"S1": "LS库存件"}, version="v2")
    engine.refresh()
    reviewed = next(row for row in engine.list(user("packer", ["stock_packing"]))["items"]
                    if row["id"] == task["id"])
    assert reviewed["status"] == "waiting" and "标签内容已变化" in reviewed["blocked_by"]
    assert reviewed["payload"]["label_review_required"] is True
    with store.connect() as db:
        print_row = db.execute("""SELECT state,reason FROM order_print_states
            WHERE profile_id='P' AND order_no='S1'""").fetchone()
        audit = db.execute("""SELECT 1 FROM order_print_audit WHERE profile_id='P'
            AND order_no='S1' AND from_state='printed' AND to_state='unprinted'
            AND reason='content_changed'""").fetchone()
    assert print_row["state"] == "unprinted" and print_row["reason"] == "content_changed"
    assert audit
    started = engine.action(reviewed["id"], user("packer", ["stock_packing"]), "start",
                            "inspect-changed-label", reviewed["version"], "")["task"]
    with pytest.raises(DailyWorkConflict) as blocked:
        engine.action(started["id"], user("packer", ["stock_packing"]), "packed",
                      "pack-without-current-label", started["version"], "包装完成")
    assert blocked.value.code == "label_review_required"
    with store.connect(write=True) as db:
        db.execute("""UPDATE order_print_states SET state='printed',fingerprint=?,reason='submitted'
            WHERE profile_id='P' AND order_no='S1'""",
                   (started["payload"]["label_fingerprint"],))
    packed = engine.action(started["id"], user("packer", ["stock_packing"]), "packed",
                           "pack-with-current-label", started["version"], "包装完成")["task"]
    assert packed["status"] == "packed"
    assert packed["payload"].get("label_review_required") is None


def test_normal_print_state_keeps_existing_pack_task_identity(store):
    ensure_print_schema(store)
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1','printed','device','job','hash','submitted','2026-10-04T10:00:00')""")
    seed(store, [item], {"S1": "LS库存件"}, version="v2")
    engine.refresh()
    rows = [item for item in engine.list(user("admin", [], True), view="all")["items"]
            if item["order_no"] == "S1" and item["role"] == "stock_packing"]
    assert len(rows) == 1 and rows[0]["id"] == task["id"]
    assert rows[0]["task_type"] == "whole_order_pack" and rows[0]["assignee_id"] == "packer"


def test_future_wait_is_deferred_but_today_action_remains_visible(store):
    item = order("C1", "FB3000", "催货")
    seed(store, [item], {"C1": "催货"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("sales", ["sales"]))["items"][0]
    engine.action(task["id"], user("sales", ["sales"]), "wait", "wait-future",
                  task["version"], "等待客户答复", "2026-10-06")
    listed = engine.list(user("sales", ["sales"]))
    assert len(listed["items"]) == 1 and listed["items"][0]["status"] == "waiting"
    assert listed["summary"]["deferred"] == 1
    assert listed["summary"]["actions_completed_today"] == 1
    assert listed["today_events"][0]["event_type"] == "wait"


def test_projection_version_is_not_disguised_as_current(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    assert engine.list(user("admin", [], True), view="all")["tasks_current"] is True
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'", (dumps("v2"),))
    behind = engine.list(user("admin", [], True), view="all")
    assert behind["data_version"] == "v2" and behind["task_data_version"] == "v1"
    assert behind["tasks_current"] is False
    task = next(item for item in behind["items"] if item["role"] == "stock_packing")
    with pytest.raises(DailyWorkConflict) as error:
        engine.assert_print_access(user("packer", ["stock_packing"]), ["S1"], "P")
    assert error.value.code == "task_projection_conflict"
    with pytest.raises(DailyWorkConflict):
        engine.action(task["id"], user("packer", ["stock_packing"]), "start",
                      "stale-start", task["version"], "")


def test_same_data_version_with_new_source_generation_is_not_current(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_source_updated_at'",
                   (dumps("2026-10-04T09:00:00+08:00"),))
    current = engine.list(user("admin", [], True), view="all")
    assert current["data_version"] == current["task_data_version"] == "v1"
    assert current["source_updated_at"] != current["task_source_updated_at"]
    assert current["tasks_current"] is False
    assert engine.version()["tasks_current"] is False
    with pytest.raises(DailyWorkConflict) as error:
        engine.assert_print_access(user("packer", ["stock_packing"]), ["S1"], "P")
    assert error.value.code == "task_projection_conflict"

    engine.refresh()
    assert engine.version()["tasks_current"] is True


def test_resolved_issue_returns_to_todo_when_next_generation_still_has_it(store):
    item = order("C1", "FB3000", "催货")
    seed(store, [item], {"C1": "催货"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("sales", ["sales"]))["items"][0]
    resolved = engine.action(task["id"], user("sales", ["sales"]), "resolve", "resolve-1",
                             task["version"], "已反馈客户")["task"]
    seed(store, [item], {"C1": "催货"}, version="v2")
    engine.refresh()
    reopened = engine.list(user("sales", ["sales"]))["items"][0]
    assert reopened["status"] == "todo" and reopened["version"] > resolved["version"]
    with store.connect() as db:
        assert db.execute("""SELECT 1 FROM daily_work_events WHERE task_id=?
            AND event_type='resolution_not_confirmed'""", (task["id"],)).fetchone()


def test_resolved_issue_reopens_on_new_sync_with_same_content_hash(store):
    item = order("C1", "FB3000", "催货")
    seed(store, [item], {"C1": "催货"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("sales", ["sales"]))["items"][0]
    resolved = engine.action(task["id"], user("sales", ["sales"]), "resolve",
                             "resolve-same-hash", task["version"], "已完成今日跟进")["task"]
    seed(store, [item], {"C1": "催货"}, version="v1",
         source="2026-10-04T09:30:00+08:00")
    engine.refresh()
    reopened = engine.list(user("sales", ["sales"]))["items"][0]
    assert reopened["id"] == resolved["id"]
    assert reopened["status"] == "todo" and reopened["version"] > resolved["version"]
    assert "尚未被新一轮" in reopened["blocked_by"]


def test_assignment_is_disabled_by_default_and_customer_source_is_strict(store):
    untrusted = order("C1", "LS0001", "LS库存件", customer="旧客户字段")
    untrusted["customer_source"] = "legacy_buyer"
    seed(store, [untrusted], {"C1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    result = engine.refresh()
    assert result["assignment_enabled"] is False
    admin = engine.list(user("admin", [], True), view="all")
    packing = next(item for item in admin["items"] if item["role"] == "stock_packing")
    assert packing["assignee_id"] == ""
    missing = next(item for item in admin["items"] if item["task_type"] == "missing_customer")
    assert missing["customer"] == ""


def test_enabling_assignment_fills_existing_preview_without_source_change(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    before = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                  if row["role"] == "stock_packing")
    assert before["assignee_id"] == ""
    enabled = engine.set_settings(user("admin", [], True), True)
    after = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                 if row["id"] == before["id"])
    assert enabled["refresh"]["updated"] >= 1
    assert after["assignee_id"] == "packer" and after["version"] > before["version"]


def test_overdue_supersedes_urge_as_one_stable_sales_issue(store):
    item = order("C1", "FB3000", "催货", due="2026-10-05")
    seed(store, [item], {"C1": "催货"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    first = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                 if row["role"] == "sales" and row["task_type"] == "delivery_follow_up")
    overdue = order("C1", "FB3000", "催货", due="2026-10-03")
    seed(store, [overdue], {"C1": "催货"}, version="v2")
    engine.refresh()
    sales = [row for row in engine.list(user("admin", [], True), view="all")["items"]
             if row["role"] == "sales" and row["order_no"] == "C1"]
    assert len(sales) == 1
    assert sales[0]["id"] == first["id"] and sales[0]["task_type"] == "overdue_order"


def test_enabling_assignment_persists_owners_from_existing_preview(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    before = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                  if row["role"] == "stock_packing")
    assert before["assignee_id"] == ""
    enabled = engine.set_settings(user("admin", [], True), True)
    assert enabled["assignment_enabled"] is True
    after = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                 if row["id"] == before["id"])
    assert after["assignee_id"] == "packer"
    assert after["source_data_version"] == "v1"


def test_incomplete_current_calculation_preserves_previous_projection(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    before = engine.list(user("admin", [], True), view="all")
    task_id = before["items"][0]["id"]
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'", (dumps("v2"),))
        db.execute("DELETE FROM logistics_calculations")
    with pytest.raises(DailyWorkConflict) as error:
        engine.refresh()
    assert error.value.code == "task_projection_conflict"
    with store.connect() as db:
        row = db.execute("SELECT condition_state FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
        projected = db.execute("""SELECT value FROM daily_work_settings
            WHERE key='projection_data_version'""").fetchone()[0]
        assert row["condition_state"] == "active" and projected == "v1"


def test_structurally_valid_but_uncovered_calculation_is_rejected(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'", (dumps("v2"),))
        db.execute("INSERT INTO logistics_calculations VALUES (?,?,?,?,?)",
                   ("job-v2", "P", "v2", dumps({"plans": [], "allocations": [],
                                                   "blocked_orders": []}),
                    "2026-10-04T09:00:00+08:00"))
    with pytest.raises(DailyWorkConflict) as error:
        engine.refresh()
    assert error.value.code == "task_projection_conflict"
    current = engine.list(user("admin", [], True), view="all")
    assert current["task_data_version"] == "v1" and current["tasks_current"] is False


def test_blocked_calculation_never_creates_packaging_task(store):
    item = order("S1", "LS0001", "LS库存件")
    item["calculation_status"] = "blocked"
    seed(store, [item], {"S1": "LS库存件"},
         blocked=[{"order_no": "S1", "reason": "库存单位待核对"}])
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    items = engine.list(user("admin", [], True), view="all")["items"]
    assert not any(row["role"] in {"stock_packing", "arrival_packing"} for row in items)
    assert any(row["task_type"] == "data_review" for row in items)


def test_attendance_reconcile_releases_todo_but_preserves_started_for_handoff(store):
    rows = [order("S1", "LS0001", "LS库存件"), order("S2", "LS0002", "LS库存件")]
    seed(store, rows, {"S1": "LS库存件", "S2": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    packer = user("packer", ["stock_packing"])
    mine = engine.list(packer)["items"]
    started = engine.action(mine[0]["id"], packer, "start", "start-handoff",
                            mine[0]["version"], "")["task"]
    result = engine.set_attendance(user("admin", [], True), "packer", False)
    assert result["reconcile"]["released"] == 1
    assert result["reconcile"]["handoff_required"] == 1
    all_items = engine.list(user("admin", [], True), view="all")["items"]
    kept = next(row for row in all_items if row["id"] == started["id"])
    released = next(row for row in all_items if row["id"] != started["id"] and
                    row["role"] == "stock_packing")
    assert kept["assignee_id"] == "packer" and "需要管理员交接" in kept["blocked_by"]
    assert released["assignee_id"] == ""
    seed(store, rows, {"S1": "LS库存件", "S2": "LS库存件"}, version="v2")
    engine.refresh()
    kept = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                if row["id"] == started["id"])
    assert "需要管理员交接" in kept["blocked_by"]
    engine.set_attendance(user("admin", [], True), "packer", True)
    current = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                   if row["id"] == started["id"])
    transferred = engine.transfer(current["id"], user("admin", [], True), "packer",
                                  "confirm-handoff", current["version"], "确认继续由原员工处理")
    assert transferred["task"]["blocked_by"] == ""


def test_off_duty_or_released_worker_cannot_reclaim_task_from_cached_card(store):
    item = order("C1", "FB3000", "催货")
    seed(store, [item], {"C1": "催货"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    cached = engine.list(user("sales", ["sales"]))["items"][0]
    engine.set_attendance(user("admin", [], True), "sales", False)
    with pytest.raises(DailyWorkConflict) as released:
        engine.action(cached["id"], user("sales", ["sales"]), "start",
                      "stale-card-reclaim", cached["version"], "")
    assert released.value.code in {"role_forbidden", "task_version_changed"}

    engine.set_attendance(user("admin", [], True), "sales", True)
    current = next(row for row in engine.list(user("sales", ["sales"]))["items"]
                   if row["id"] == cached["id"])
    started = engine.action(current["id"], user("sales", ["sales"]), "start",
                            "start-before-off-duty", current["version"], "")["task"]
    engine.set_attendance(user("admin", [], True), "sales", False)
    blocked = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                   if row["id"] == started["id"])
    with pytest.raises(DailyWorkConflict) as off_duty:
        engine.action(blocked["id"], user("sales", ["sales"]), "progress",
                      "off-duty-progress", blocked["version"], "仍在处理")
    assert off_duty.value.code == "role_forbidden"


def test_sales_customer_owner_survives_a_day_off(store):
    item = order("C1", "FB3000", "催货")
    seed(store, [item], {"C1": "催货"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    before = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                  if row["role"] == "sales")
    assert before["assignee_id"] == "sales"
    engine.set_attendance(user("admin", [], True), "sales", False)
    after = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                 if row["id"] == before["id"])
    assert after["assignee_id"] == "sales"
    with store.connect() as db:
        assert db.execute("""SELECT owner_id FROM sales_customer_owners
            WHERE profile_id='P' AND customer_name='客户甲'""").fetchone()[0] == "sales"


def test_action_state_machine_rejects_unsafe_completion(store):
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    with pytest.raises(ValueError, match="已包装"):
        engine.action(task["id"], user("packer", ["stock_packing"]), "resolve",
                      "bad-resolve", task["version"], "想直接完成")
    with pytest.raises(ValueError, match="处理结果"):
        engine.action(task["id"], user("packer", ["stock_packing"]), "wait",
                      "blank-wait", task["version"], "")


def test_uncertain_print_cannot_be_confirmed_packed_without_review(store):
    ensure_print_schema(store)
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1','uncertain','device','job','hash','unknown','2026-10-04T10:00:00')""")
    live = next(row for row in engine.list(user("packer", ["stock_packing"]))["items"]
                if row["id"] == task["id"])
    assert live["status"] == "waiting" and "出纸" in live["blocked_by"]
    assert engine.assert_print_reset_access(
        user("packer", ["stock_packing"]), ["S1"], "P") is True
    assert engine.assert_print_reset_access(
        user("admin", [], True), ["S1"], "P") is True
    with pytest.raises(DailyWorkConflict):
        engine.assert_print_reset_access(user("sales", ["sales"]), ["S1"], "P")
    with pytest.raises(DailyWorkConflict) as error:
        engine.action(task["id"], user("packer", ["stock_packing"]), "packed",
                      "unsafe-packed", task["version"], "包装完成")
    assert error.value.code == "print_state_requires_review"
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'", (dumps("v2"),))
    with pytest.raises(DailyWorkConflict) as stale:
        engine.assert_print_reset_access(user("packer", ["stock_packing"]), ["S1"], "P")
    assert stale.value.code == "task_projection_conflict"
    assert engine.assert_print_reset_access(
        user("admin", [], True), ["S1"], "P") is True
    seed(store, [item], {"S1": "LS库存件"}, version="v2")
    engine.refresh()
    reviewed = next(row for row in engine.list(user("packer", ["stock_packing"]))["items"]
                    if row["id"] == task["id"])
    assert reviewed["status"] == "waiting"
    with pytest.raises(DailyWorkConflict) as ordinary:
        engine.assert_print_access(user("packer", ["stock_packing"]), ["S1"], "P")
    assert ordinary.value.code == "daily_work_owner_required"
    assert engine.assert_print_reset_access(
        user("packer", ["stock_packing"]), ["S1"], "P") is True


def test_print_reset_refresh_restores_normal_packaging_without_erasing_other_blocks(store):
    ensure_print_schema(store)
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    started = engine.action(task["id"], user("packer", ["stock_packing"]), "start",
                            "start-before-uncertain", task["version"], "")["task"]
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1','uncertain','device','job','hash','unknown','2026-10-04T10:00:00')""")
    engine.refresh()
    reviewed = next(row for row in engine.list(user("packer", ["stock_packing"]))["items"]
                    if row["id"] == started["id"])
    assert reviewed["status"] == "waiting" and "人工核对" in reviewed["blocked_by"]

    reset_order(store, "S1", "P", "device", "manual_reset")
    engine.refresh(actor_id="packer")
    restored = next(row for row in engine.list(user("packer", ["stock_packing"]))["items"]
                    if row["id"] == started["id"])
    assert restored["status"] == "in_progress" and restored["blocked_by"] == ""

    deferred = engine.action(restored["id"], user("packer", ["stock_packing"]), "wait",
                             "employee-wait-before-print-review", restored["version"],
                             "等待同事协同", "2026-10-06")["task"]
    with store.connect(write=True) as db:
        db.execute("""UPDATE order_print_states SET state='uncertain',reason='unknown'
            WHERE profile_id='P' AND order_no='S1'""")
    engine.refresh()
    reset_order(store, "S1", "P", "device", "manual_reset")
    engine.refresh(actor_id="packer")
    with store.connect() as db:
        kept_wait = db.execute("SELECT * FROM daily_work_tasks WHERE id=?",
                               (deferred["id"],)).fetchone()
    assert kept_wait["status"] == "waiting"
    assert kept_wait["next_follow_up_date"] == "2026-10-06"

    protected = "订单标签内容已变化，需重新核对包装结果"
    with store.connect(write=True) as db:
        payload = json.loads(db.execute("SELECT payload FROM daily_work_tasks WHERE id=?",
                                        (started["id"],)).fetchone()[0])
        payload["blocked_by"] = protected
        db.execute("""UPDATE daily_work_tasks SET status='waiting',next_follow_up_date='',payload=?
            WHERE id=?""",
                   (dumps(payload), started["id"]))
        db.execute("""UPDATE order_print_states SET state='uncertain',reason='unknown'
            WHERE profile_id='P' AND order_no='S1'""")
    engine.refresh()
    with store.connect(write=True) as db:
        db.execute("""UPDATE order_print_states SET state='unprinted',reason='manual_reset'
            WHERE profile_id='P' AND order_no='S1'""")
    engine.refresh()
    still_blocked = next(row for row in engine.list(user("packer", ["stock_packing"]))["items"]
                         if row["id"] == started["id"])
    assert still_blocked["status"] == "waiting" and still_blocked["blocked_by"] == protected


def test_print_reset_owner_guard_runs_inside_reset_transaction(store):
    ensure_print_schema(store)
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = engine.list(user("packer", ["stock_packing"]))["items"][0]
    with store.connect(write=True) as db:
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1','uncertain','device','job','hash','unknown','2026-10-04T10:00:00')""")

    def ownership_changes_before_guard(db, _items):
        assert db.in_transaction is True
        db.execute("UPDATE daily_work_tasks SET assignee_id='buyer' WHERE id=?", (task["id"],))
        engine.assert_print_reset_access(user("packer", ["stock_packing"]), ["S1"], "P", db=db)

    with pytest.raises(DailyWorkConflict) as error:
        reset_order(store, "S1", "P", "device", "manual_reset",
                    access_checker=ownership_changes_before_guard)
    assert error.value.code == "daily_work_owner_required"
    with store.connect() as db:
        state = db.execute("""SELECT state FROM order_print_states
            WHERE profile_id='P' AND order_no='S1'""").fetchone()[0]
        owner = db.execute("SELECT assignee_id FROM daily_work_tasks WHERE id=?",
                           (task["id"],)).fetchone()[0]
    assert state == "uncertain" and owner == "packer"


@pytest.mark.parametrize("workflow", ["in_progress", "printed", "packed"])
def test_ready_abnormal_becoming_short_preserves_physical_work_for_review(store, workflow):
    ensure_print_schema(store)
    item = order("A1", "FB1000", "异常")
    ready = {"demand": {"order": "A1", "sku": item["product_id"],
                         "product_code": "FB1000"},
             "shortage": "0", "pending": "0"}
    seed(store, [item], {"A1": "异常"}, [ready])
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = next(row for row in engine.list(user("packer", ["arrival_packing"]))["items"]
                if row["task_type"] == "whole_order_pack")
    if workflow == "in_progress":
        task = engine.action(task["id"], user("packer", ["arrival_packing"]), "start",
                             "start-before-shortage", task["version"], "")["task"]
    elif workflow == "packed":
        task = engine.action(task["id"], user("packer", ["arrival_packing"]), "packed",
                             "packed-before-shortage", task["version"], "包装完成")["task"]
    else:
        with store.connect(write=True) as db:
            db.execute("""INSERT INTO order_print_states
                (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
                VALUES ('P','A1','printed','device','job','hash','submitted',
                        '2026-10-04T10:00:00')""")

    short = {**ready, "shortage": "2", "pending": "1"}
    seed(store, [item], {"A1": "异常"}, [short], version="v2")
    engine.refresh()
    admin = engine.list(user("admin", [], True), view="all")
    held = next(row for row in admin["items"] if row["id"] == task["id"])
    assert held["status"] == "waiting" and held["assignee_id"] == "packer"
    assert "重新出现缺项" in held["blocked_by"]
    assert held["payload"]["shortage_review_required"] is True
    assert any(row["task_type"] == "arrival_shortage_waiting" for row in admin["items"])
    held_version = held["version"]
    engine.refresh()
    unchanged = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                     if row["id"] == task["id"])
    assert unchanged["version"] == held_version
    if workflow == "packed":
        with store.connect() as db:
            dispatch = db.execute("""SELECT condition_state FROM daily_work_tasks
                WHERE task_type='packed_waiting_dispatch' AND order_no='A1'""").fetchone()
        assert dispatch["condition_state"] == "resolved"

    seed(store, [item], {"A1": "异常"}, [ready], version="v3")
    engine.refresh()
    restored = next(row for row in engine.list(user("packer", ["arrival_packing"]))["items"]
                    if row["id"] == task["id"])
    assert restored["status"] == "waiting" and restored["assignee_id"] == "packer"
    assert "缺项已经补齐" in restored["blocked_by"]
    resumed = engine.action(restored["id"], user("packer", ["arrival_packing"]), "start",
                            "review-and-resume-" + workflow, restored["version"], "")["task"]
    assert resumed["status"] == "in_progress" and resumed["blocked_by"] == ""


def test_abnormal_shortage_is_read_only_arrival_broadcast_and_does_not_use_capacity(store):
    rows = [order(f"S{i}", f"LS000{i}", "LS库存件") for i in range(1, 4)]
    abnormal = order("A1", "FB1000", "异常")
    rows.append(abnormal)
    plans = {row["order_no"]: row["logistics_company"] for row in rows}
    allocation = {"demand": {"order": "A1", "sku": abnormal["product_id"],
                              "product_code": "FB1000"},
                  "shortage": "2", "pending": "1"}
    seed(store, rows, plans, [allocation])
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()

    admin = engine.list(user("admin", [], True), view="all")
    broadcast = next(row for row in admin["items"]
                     if row["task_type"] == "arrival_shortage_waiting")
    assert broadcast["read_only"] is True
    assert broadcast["status"] == "waiting" and broadcast["assignee_id"] == ""
    assert broadcast["payload"]["missing_items"] == [{
        "sku": "FB1000", "product_id": abnormal["product_id"],
        "shortage": "2", "pending": "1"}]
    assert broadcast["payload"]["linked_purchase_business_key"] == \
        "purchase:abnormal_shortage:A1"
    assert admin["summary"]["unassigned"] == 0
    mine = engine.list(user("packer", ["arrival_packing"]))["items"]
    assert broadcast["id"] in {row["id"] for row in mine}
    assert sum(row["task_type"] == "whole_order_pack" and row["assignee_id"] == "packer"
               for row in admin["items"]) == 3

    for action in ("start", "progress", "wait", "resolve", "packed", "reopen"):
        with pytest.raises(DailyWorkConflict) as error:
            engine.action(broadcast["id"], user("packer", ["arrival_packing"]), action,
                          "readonly-" + action, broadcast["version"], "不可操作")
        assert error.value.code == "task_read_only"
    with pytest.raises(DailyWorkConflict) as transfer_error:
        engine.transfer(broadcast["id"], user("admin", [], True), "packer",
                        "readonly-transfer", broadcast["version"], "不能分配只读事项")
    assert transfer_error.value.code == "task_read_only"
    with pytest.raises(DailyWorkConflict) as print_error:
        engine.assert_print_access(user("packer", ["arrival_packing"]), ["A1"], "P")
    assert print_error.value.code == "daily_work_owner_required"

    seed(store, rows, plans, [{**allocation, "shortage": "0", "pending": "0"}], version="v2")
    engine.refresh()
    current = engine.list(user("admin", [], True), view="all")["items"]
    assert not any(row["task_type"] == "arrival_shortage_waiting" for row in current)
    assert any(row["task_type"] == "whole_order_pack" and row["order_no"] == "A1"
               for row in current)


def test_legacy_zero_packing_limit_does_not_limit_dispatch(store):
    with store.connect(write=True) as db:
        db.execute("UPDATE staff_users SET packing_limit=0 WHERE id='packer'")
    item = order("S1", "LS0001", "LS库存件")
    seed(store, [item], {"S1": "LS库存件"})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine)
    engine.refresh()
    task = next(row for row in engine.list(user("admin", [], True), view="all")["items"]
                if row["role"] == "stock_packing")
    assert task["assignee_id"] == "packer"


def test_due_purchase_follow_up_sorts_before_future_procurement(store):
    rows = [order("P1", "FB2000", "需采购", due="2026-10-08"),
            order("C1", "FB3000", "催货", due="2026-10-04")]
    allocations = [
        {"demand": {"order": "P1", "sku": "FB2000"}, "shortage": 2, "pending": 0},
        {"demand": {"order": "C1", "sku": "FB3000"}, "shortage": 1,
         "pending": 1, "urging": 1},
    ]
    seed(store, rows, {"P1": "需采购", "C1": "催货"}, allocations)
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    purchase = [row for row in engine.list(user("admin", [], True),
                                            role="purchase", view="all")["items"]
                if row["task_type"] in {"procurement_gap", "supply_follow_up"}]
    assert [row["task_type"] for row in purchase] == ["supply_follow_up", "procurement_gap"]
    assert purchase[0]["priority"] < purchase[1]["priority"]


def test_procurement_matches_real_allocation_product_identity_once(store):
    first = order("P1", "FB2140", "需采购", due="2026-10-08")
    first["product_id"] = "7992819852148049972"
    second = dict(first, id="P1:2", line_id="2", quantity="2")
    allocation = {
        "demand": {
            "order": "P1", "sku": "7992819852148049972",
            "product_code": "FB2140", "quantity": "7", "warehouse": "08390",
            "source_rows": ["P1:1", "P1:2"], "date": "2026-10-08",
        },
        "shortage": "7", "pending": "0", "actual": "0",
    }
    seed(store, [first, second], {"P1": "需采购"}, [allocation])
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    gaps = [row for row in engine.list(user("admin", [], True),
                                        role="purchase", view="all")["items"]
            if row["task_type"] == "procurement_gap"]
    assert len(gaps) == 1
    assert gaps[0]["sku"] == "FB2140"
    assert gaps[0]["product_id"] == "7992819852148049972"
    assert gaps[0]["payload"]["shortage"] == "7"
    assert gaps[0]["payload"]["order_nos"] == ["P1"]


def test_mixed_order_purchase_follow_up_only_includes_urging_line(store):
    urging = order("C1", "FB3000", "催货", due="2026-10-05")
    urging["product_id"] = "product-urge"
    normal = dict(urging, id="C1:2", line_id="2", sku="FB4000",
                  product_id="product-normal", sku_id="FB4000-s")
    allocations = [
        {"demand": {"order": "C1", "sku": "product-urge", "product_code": "FB3000",
                    "warehouse": "08390"},
         "shortage": "1", "pending": "1", "urging": "1", "order_category": "催货"},
        {"demand": {"order": "C1", "sku": "product-normal", "product_code": "FB4000",
                    "warehouse": "08390"},
         "shortage": "0", "pending": "0", "urging": "0", "order_category": "待到货"},
    ]
    seed(store, [urging, normal], {"C1": "催货"}, allocations)
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    follow_up = [row for row in engine.list(user("admin", [], True),
                                             role="purchase", view="all")["items"]
                 if row["task_type"] == "supply_follow_up"]
    assert len(follow_up) == 1
    assert follow_up[0]["sku"] == "FB3000"
    assert follow_up[0]["product_id"] == "product-urge"


def test_admin_unprinted_correction_clears_only_matching_label_review(store):
    from core.daily_work import LABEL_REVIEW_BLOCK
    from core.order_print_state import order_fingerprint
    ensure_print_schema(store)
    item = order('S1', 'LS0001', 'LS库存件')
    seed(store, [item], {'S1': 'LS库存件'})
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    enable_assignment(engine); engine.refresh()
    with store.connect(write=True) as db:
        task = db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing'").fetchone()
        payload=json.loads(task['payload']); payload.update(blocked_by=LABEL_REVIEW_BLOCK,label_review_required=True,status_before_label_review='in_progress',promised_date='2026-10-05')
        db.execute("UPDATE daily_work_tasks SET status='waiting',payload=? WHERE id=?",(dumps(payload),task['id']))
        db.execute("INSERT INTO order_print_states VALUES ('P','S1','unprinted','','',?,'correction:test:用户确认实际未打印','now')",(order_fingerprint([item]),))
    engine.refresh()
    with store.connect() as db:
        corrected=db.execute('SELECT * FROM daily_work_tasks WHERE id=?',(task['id'],)).fetchone();p=json.loads(corrected['payload'])
    assert corrected['status']=='todo' and not p.get('label_review_required')
    assert p.get('blocked_by') != LABEL_REVIEW_BLOCK and p['promised_date']=='2026-10-05'
    # The prior correction cannot waive a later real label change.
    with store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET status='in_progress' WHERE id=?",(task['id'],))
    changed=dict(item, quantity='5')
    seed(store,[changed],{'S1':'LS库存件'},version='v2');engine.refresh()
    with store.connect() as db:
        changed_task=db.execute('SELECT * FROM daily_work_tasks WHERE id=?',(task['id'],)).fetchone()
    assert changed_task['status']=='waiting' and json.loads(changed_task['payload'])['label_review_required']


def test_new_source_generation_preserves_version_guards_without_noop_events(store):
    engine=DailyWork(store,now=lambda:NOW);enable_assignment(engine)
    rows=[order('S1','LS0001','LS库存件')]
    seed(store,rows,{'S1':'LS库存件'})
    engine.refresh()
    # Second pass normalizes any initial assignment-dependent payload fields.
    engine.refresh()
    with store.connect() as db:
        prior={r['id']:r['version'] for r in db.execute('SELECT id,version FROM daily_work_tasks')}
        events=db.execute('SELECT count(*) FROM daily_work_events').fetchone()[0]
    seed(store,rows,{'S1':'LS库存件'},source='2026-10-04T08:15:00+08:00',version='v2')
    engine.refresh()
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM daily_work_events').fetchone()[0]==events
        for row in db.execute('SELECT id,version,source_data_version FROM daily_work_tasks'):
            assert row['version']>prior[row['id']]
            assert row['source_data_version']=='v2'
    changed=order('S1','LS0001','LS库存件',quantity='5')
    seed(store,[changed],{'S1':'LS库存件'},source='2026-10-04T08:30:00+08:00',version='v3')
    engine.refresh()
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM daily_work_events').fetchone()[0]>events
