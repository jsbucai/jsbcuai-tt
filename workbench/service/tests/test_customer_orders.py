"""Whole-customer-order safety checks over the complete published snapshot."""
import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask, g

from core.customer_orders import CustomerOrders, WAREHOUSE, customer_order_routes
from core.daily_work import DailyWork, DailyWorkConflict, SHANGHAI
from core.store import Store, dumps

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def context():
    (ROOT / "runtime").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="customer-po-", dir=ROOT / "runtime") as runtime:
        store = Store(runtime)
        with store.connect(write=True) as db:
            db.execute("""CREATE TABLE staff_users(id TEXT PRIMARY KEY,username TEXT,display_name TEXT,
                password_hash TEXT,roles TEXT,is_admin INTEGER,active INTEGER,packing_limit INTEGER,
                created_at TEXT,updated_at TEXT)""")
            db.execute("""CREATE TABLE logistics_calculations(job_id TEXT PRIMARY KEY,profile TEXT,
                data_version TEXT,payload TEXT,created_at TEXT)""")
            for identity, role, admin in (("admin", "sales", 1), ("seller", "sales", 0)):
                db.execute("INSERT INTO staff_users VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (identity, identity, identity, "", dumps([role]), admin, 1, 3, "", ""))
        yield store, DailyWork(store, auto_project=False)


def row(number, product, customer="客户甲"):
    return {"id": number + ":1", "profile_id": "P", "warehouse_id": WAREHOUSE,
            "order_no": number, "line_id": "1", "sku": product, "product_id": product + "-p",
            "sku_id": product + "-s", "base_unit": "件", "quantity": "1",
            "summary": "PO#PRJ#1#DRAW#品名#M8#8.8#锌#1#" + product + "#交期=2026-10-10",
            "customer_name": customer, "customer_source": "wms_receiveraddress",
            "document_date": "2026-10-01", "calculation_status": "ready",
            "logistics_company": "LS库存件" if product.startswith("LS") else "需采购"}


def allocation(item, actual, pending=0, shortage=0):
    return {"demand": {"order": item["order_no"], "warehouse": dumps([
                "P", WAREHOUSE, item["sku_id"], item["base_unit"]]),
                "sku": item["product_id"], "quantity": 1,
                "product_code": item["sku"]},
            "actual": actual, "pending": pending, "shortage": shortage}


def seed(store, rows, allocations):
    source = datetime.now(SHANGHAI).isoformat()
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps("v1"),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_source_updated_at',?)", (dumps(source),))
        for item in rows:
            db.execute("INSERT INTO records(kind,id,payload,sku) VALUES ('orders',?,?,?)",
                       (item["id"], dumps(item), item["sku"]))
        calculation = {"plans": [{"tradeid": item["order_no"],
                                  "freightname": item["logistics_company"]} for item in rows],
                       "allocations": allocations, "blocked_orders": []}
        db.execute("INSERT INTO logistics_calculations VALUES (?,?,?,?,?)",
                   ("job1", "P", "v1", dumps(calculation), source))


def admin():
    return {"id": "admin", "roles": ["sales"], "is_admin": True}


def assign(engine, key, customer="客户甲", owner="seller"):
    value = next(row for row in engine.customer_owners(admin())["items"] if row["customer_name"] == customer)
    return engine.set_customer_owner(admin(), "P", customer, owner, "", "", value["version"], key, "确认客户固定销售")


def test_one_card_for_three_lines_and_only_missing_rows_count(context):
    store, daily = context
    rows = [row("PO#1", "LS001"), row("PO#2", "LS002"), row("PO#3", "FB003")]
    seed(store, rows, [allocation(rows[0], 1), allocation(rows[1], 1), allocation(rows[2], 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    result = engine.list(admin(), role="sales", view="all")
    assert result["total"] == 1
    card = result["items"][0]
    assert card["customer_order_no"] == "PO" and card["order_count"] == 3
    assert card["ready_count"] == 2 and card["procurement_count"] == 1
    assert card["missing_count"] == 1 and card["missing_product_count"] == 1
    assert not card["ready_to_ship"]
    detail = engine.detail(admin(), card["id"], role="sales", view="all")["group"]
    assert [line["order_no"] for line in detail["lines"]] == ["PO#1", "PO#2", "PO#3"]
    assert detail["missing_items"][0]["sku"] == "FB003"


def test_conflicting_customer_and_missing_allocation_are_review_not_ready(context):
    store, daily = context
    rows = [row("PO#1", "LS001"), row("PO#2", "LS002", "另一个客户")]
    seed(store, rows, [allocation(rows[0], 1)])
    daily.refresh()
    cards = CustomerOrders(store, daily).list(admin(), view="all")["items"]
    assert len(cards) == 2
    assert {card["customer"] for card in cards} == {"客户甲", "另一个客户"}
    assert len({card["id"] for card in cards}) == 2
    assert all(card["order_count"] == 1 for card in cards)
    assert any(card["review_count"] == 1 and not card["ready_to_ship"] for card in cards)


def test_followup_is_versioned_and_idempotent(context):
    store, daily = context
    rows = [row("PO#1", "FB003")]
    seed(store, rows, [allocation(rows[0], 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    card = engine.list(admin(), view="all")["items"][0]
    assert card["follow_up_version"] == 1
    owner = assign(engine, "transfer-one")
    assert owner["owner"]["version"] == 2
    person = {"id": "seller", "roles": ["sales"], "is_admin": False}
    recorded = engine.followup(person, card["id"], "first", 2, "已联系采购", "采购", "2026-10-09")
    replay = engine.followup(person, card["id"], "first", 2, "已联系采购", "采购", "2026-10-09")
    assert recorded["version"] == 3 and replay["replayed"] is True
    with pytest.raises(DailyWorkConflict):
        engine.followup(person, card["id"], "second", 2, "旧版本", "", "2026-10-09")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM customer_order_followup_events WHERE group_id=?",
                          (card["id"],)).fetchone()[0] == 2


def test_new_customer_stays_unassigned_until_fixed_owner_is_set(context):
    store, daily = context
    item = row("PO#1", "FB003")
    seed(store, [item], [allocation(item, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    preview = engine.list(admin(), view="all")["items"][0]
    assert preview["assignee_id"] == ""
    with store.connect(write=True) as db:
        db.execute("UPDATE daily_work_settings SET value='true' WHERE key='assignment_enabled'")
    engine.reconcile()
    assigned = engine.list(admin(), view="all")["items"][0]
    assert assigned["id"] == preview["id"] and assigned["assignee_id"] == ""
    owner = engine.customer_owners(admin())["items"][0]
    result = engine.set_customer_owner(admin(), "P", "客户甲", "seller", "", "",
                                       owner["version"], "owner-one", "确定固定销售")
    assert result["owner"]["owner_id"] == "seller"
    assert engine.list(admin(), view="all")["items"][0]["assignee_id"] == "seller"


def test_customer_collision_retains_legacy_followup_in_history(context):
    store, daily = context
    first = row("PO#1", "FB003")
    seed(store, [first], [allocation(first, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    original = engine.list(admin(), view="all")["items"][0]
    assign(engine, "assign-legacy")
    engine.followup({"id": "seller", "roles": ["sales"], "is_admin": False},
                    original["id"], "legacy-note", 2, "已约定交期", "供应商", "2026-10-09")
    second = row("PO#2", "LS002", "客户乙")
    with store.connect(write=True) as db:
        db.execute("INSERT INTO records(kind,id,payload,sku) VALUES ('orders',?,?,?)",
                   (second["id"], dumps(second), second["sku"]))
        calc = json.loads(db.execute("SELECT payload FROM logistics_calculations LIMIT 1").fetchone()[0])
        calc["allocations"].append(allocation(second, 1))
        calc["plans"].append({"tradeid": second["order_no"], "freightname": second["logistics_company"]})
        db.execute("UPDATE logistics_calculations SET payload=?", (dumps(calc),))
        db.execute("UPDATE settings SET value=? WHERE key='data_source_updated_at'",
                   (dumps(datetime.now(SHANGHAI).isoformat()),))
    daily.refresh()
    engine.reconcile()
    groups = engine.list(admin(), view="all")["items"]
    assert len(groups) == 2 and all(group["id"] != original["id"] for group in groups)
    assert all(group["migration_review"] for group in groups)
    history = engine.history(admin(), view="all", scope="archived")["items"]
    assert len(history) == 1 and history[0]["group_id"] == original["id"]
    assert engine.history_detail(admin(), original["id"], view="all")["events"][0]["note"] == "已约定交期"


def test_deputy_can_work_without_changing_fixed_owner(context):
    store, daily = context
    with store.connect(write=True) as db:
        db.execute("INSERT INTO staff_users VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("deputy", "deputy", "代办", "", dumps(["sales"]), 0, 1, 3, "", ""))
    first = row("PO#1", "FB003")
    first["summary"] = first["summary"].replace("2026-10-10", "2026-10-01")
    seed(store, [first], [allocation(first, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    before = engine.customer_owners(admin())["items"][0]
    fixed = assign(engine, "fixed-first")["owner"]
    engine.set_customer_owner(admin(), "P", "客户甲", None, "deputy", "2026-10-31",
                              fixed["version"], "deputy-one", "销售请假代办", _mode="deputy")
    deputy = {"id": "deputy", "roles": ["sales"], "is_admin": False}
    card = engine.list(deputy, role="sales", view="mine")["items"][0]
    assert card["assignee_id"] == "seller" and card["deputy_id"] == "deputy"
    engine.followup(deputy, card["id"], "deputy-note", card["follow_up_version"],
                    "代办已联系客户", "", "2026-10-09")
    after = engine.list(admin(), view="all")["items"][0]
    assert after["assignee_id"] == "seller"
    task = daily.list(deputy, role="sales")["items"][0]
    daily.action(task["id"], deputy, "progress", "deputy-task", task["version"], "代办跟进")
    with store.connect(write=True) as db:
        db.execute("UPDATE sales_customer_owners SET deputy_until='2026-01-01'")
    assert not daily.list(deputy, role="sales")["items"]
    with pytest.raises(DailyWorkConflict):
        daily.action(task["id"], deputy, "progress", "expired-deputy-task", task["version"] + 1, "过期不能处理")


def test_customer_name_change_keeps_old_followup_for_review(context):
    store, daily = context
    first = row("PO#1", "FB003")
    seed(store, [first], [allocation(first, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    original = engine.list(admin(), view="all")["items"][0]
    assign(engine, "owner-before-name")
    changed = {**first, "customer_name": "客户乙"}
    with store.connect(write=True) as db:
        db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id=?",
                   (dumps(changed), first["id"]))
        db.execute("UPDATE settings SET value=? WHERE key='data_source_updated_at'",
                   (dumps(datetime.now(SHANGHAI).isoformat()),))
    daily.refresh()
    engine.reconcile()
    card = engine.list(admin(), view="all")["items"][0]
    assert card["id"] == original["id"]
    assert card["customer"] == "客户乙" and card["identity_review"]
    assert card["assignee_id"] == ""
    history = engine.history(admin(), view="all", scope="archived")["items"]
    assert history[0]["customer_name"] == "客户甲" and not history[0]["source_present"]
    with store.connect() as db:
        original_followup = dict(db.execute("SELECT * FROM customer_order_followups WHERE group_id=?",
                                            (original["id"],)).fetchone())
        original_events = [dict(event) for event in db.execute(
            "SELECT * FROM customer_order_followup_events WHERE group_id=? ORDER BY rowid",
            (original["id"],))]
    with pytest.raises(DailyWorkConflict) as transfer_error:
        engine.transfer(admin(), card["id"], "seller", "wrong-customer-transfer",
                        card["follow_up_version"], "不能转给新客户")
    assert transfer_error.value.code == "customer_owner_scope_required"
    with pytest.raises(DailyWorkConflict) as followup_error:
        engine.followup(admin(), card["id"], "wrong-customer-note",
                        card["follow_up_version"], "不能记在旧客户名下")
    assert followup_error.value.code == "customer_identity_review"
    new_owner = next(item for item in engine.customer_owners(admin())["items"]
                     if item["customer_name"] == "客户乙")
    with pytest.raises(DailyWorkConflict) as owner_error:
        engine.set_customer_owner(admin(), "P", "客户乙", "seller", "", "",
                                  new_owner["version"], "new-customer-owner", "新客户指定销售")
    assert owner_error.value.code == "customer_identity_review"
    with store.connect() as db:
        after_followup = dict(db.execute("SELECT * FROM customer_order_followups WHERE group_id=?",
                                         (original["id"],)).fetchone())
        after_events = [dict(event) for event in db.execute(
            "SELECT * FROM customer_order_followup_events WHERE group_id=? ORDER BY rowid",
            (original["id"],))]
    assert after_followup == original_followup
    assert after_events == original_events
    assert engine.list(admin(), view="all")["items"][0]["identity_review"]


def test_owner_initialization_ignores_resolved_tasks_and_inactive_sales(context):
    store, daily = context
    item = row("PO#1", "FB003")
    seed(store, [item], [allocation(item, 0, shortage=1)])
    daily.refresh()
    with store.connect(write=True) as db:
        db.execute("INSERT INTO staff_users VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("former", "former", "停用销售", "", dumps(["sales"]), 0, 0, 3, "", ""))
        for identity, assignee, state in (("old-resolved", "seller", "resolved"),
                                          ("old-inactive", "former", "active")):
            db.execute("""INSERT INTO daily_work_tasks
                (id,business_key,role,task_type,status,condition_state,assignee_id,
                 profile_id,order_no,customer,title,first_seen_at,last_seen_at,created_at,updated_at)
                VALUES (?,?,'sales','legacy_followup','todo',?,?,?,?,?,'旧销售任务','','','','')""",
                (identity, identity, state, assignee, "P", item["order_no"], "客户甲"))
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    with store.connect() as db:
        owner = db.execute("""SELECT owner_id,status FROM sales_customer_owners
            WHERE profile_id='P' AND customer_name='客户甲'""").fetchone()
    assert owner["owner_id"] == "" and owner["status"] == "unassigned"
    assert engine.list(admin(), view="all")["items"][0]["assignee_id"] == ""


def test_owner_and_history_routes_keep_old_group_route_compatible(context):
    store, daily = context
    first = row("PO#1", "FB003")
    seed(store, [first], [allocation(first, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    group_id = engine.list(admin(), view="all")["items"][0]["id"]
    app = Flask(__name__)
    app.register_blueprint(customer_order_routes(engine))
    app.before_request(lambda: setattr(g, "current_user", admin()))
    with app.test_client() as client:
        owners = client.get("/api/v2/daily-work/customer-owners").get_json()
        history = client.get("/api/v2/daily-work/groups/history?view=all&scope=all").get_json()
        detail = client.get(f"/api/v2/daily-work/groups/{group_id}?view=all").get_json()
    assert owners["ok"] and owners["items"][0]["status"] == "unassigned"
    assert history["ok"] and history["total"] == 1
    assert detail["ok"] and detail["group"]["id"] == group_id


def test_added_order_line_is_flagged_without_duplicating_followup(context):
    store, daily = context
    first = row("PO#1", "FB003")
    seed(store, [first], [allocation(first, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    before = engine.list(admin(), view="all")["items"][0]
    second = row("PO#2", "LS002")
    with store.connect(write=True) as db:
        db.execute("INSERT INTO records(kind,id,payload,sku) VALUES ('orders',?,?,?)",
                   (second["id"], dumps(second), second["sku"]))
        source = datetime.now(SHANGHAI).isoformat()
        db.execute("UPDATE settings SET value=? WHERE key='data_source_updated_at'", (dumps(source),))
        calculation = db.execute("SELECT payload FROM logistics_calculations LIMIT 1").fetchone()
        payload = json.loads(calculation[0])
        payload["allocations"].append(allocation(second, 1))
        payload["plans"].append({"tradeid": second["order_no"], "freightname": second["logistics_company"]})
        db.execute("UPDATE logistics_calculations SET payload=?", (dumps(payload),))
    daily.refresh()
    engine.reconcile()
    after = engine.list(admin(), view="all")["items"][0]
    assert after["id"] == before["id"] and after["order_count"] == 2
    assert after["lines_changed"] is True
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM customer_order_followups WHERE group_id=?",
                          (before["id"],)).fetchone()[0] == 1


def test_phone_rule_wins_over_old_tasks_and_preserves_work_facts(context):
    store, daily = context
    item = row("PO#1", "FB003")
    item["summary"] = item["summary"].replace("2026-10-10", "2026-10-01")
    seed(store, [item], [allocation(item, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    card = engine.list(admin(), view="all")["items"][0]
    with store.connect(write=True) as db:
        db.execute("INSERT INTO daily_work_assignment_rules VALUES ('phone','sales','customer','客户甲','seller',100,1,'','')")
        db.execute("UPDATE sales_customer_owners SET owner_id='admin',status='assigned',source='existing_assignment'")
        db.execute("UPDATE customer_order_followups SET assignee_id='admin',status='waiting',next_follow_up_date='2026-11-01'")
        db.execute("UPDATE daily_work_tasks SET assignee_id='admin',status='waiting' WHERE role='sales'")
        db.execute("INSERT INTO customer_order_followup_events VALUES ('original',?,'original','admin','原备注','等回复','2026-11-01','original-time')", (card['id'],))
        db.execute("INSERT INTO daily_work_tasks(id,business_key,role,task_type,status,condition_state,assignee_id,profile_id,order_no,customer,title,first_seen_at,last_seen_at,created_at,updated_at) VALUES ('done','done','sales','old','resolved','resolved','admin','P','OLD','客户甲','旧事项','','','','')")
    engine.reconcile()
    with store.connect() as db:
        fixed = dict(db.execute("SELECT * FROM sales_customer_owners").fetchone())
        followup = dict(db.execute("SELECT * FROM customer_order_followups").fetchone())
        assert fixed['owner_id'] == followup['assignee_id'] == 'seller'
        assert fixed['source'] == 'phone_rule_migrated'
        assert followup['status'] == 'waiting' and followup['next_follow_up_date'] == '2026-11-01'
        assert db.execute("SELECT note FROM customer_order_followup_events WHERE id='original'").fetchone()[0] == '原备注'
        assert db.execute("SELECT assignee_id FROM daily_work_tasks WHERE id='done'").fetchone()[0] == 'admin'
        assert {r[0] for r in db.execute("SELECT assignee_id FROM daily_work_tasks WHERE role='sales' AND condition_state='active'")} == {'seller'}
    restarted_store = Store(store.runtime)
    restarted = CustomerOrders(restarted_store, DailyWork(restarted_store, auto_project=False))
    result = restarted.reconcile()
    assert result['changed'] == 0
    seller = {'id': 'seller', 'roles': ['sales']}
    group = restarted.list(seller, role='sales')['items'][0]
    assert group['assignee_id'] == 'seller'
    restarted.followup(seller, group['id'], 'new-note', group['follow_up_version'], '新销售可以处理')


def test_no_rule_does_not_recover_old_sales_after_refresh(context):
    store, daily = context
    item = row('PO#1', 'FB003')
    item['summary'] = item['summary'].replace('2026-10-10', '2026-10-01')
    seed(store, [item], [allocation(item, 0, shortage=1)])
    daily.refresh()
    with store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET assignee_id='seller' WHERE role='sales'")
        db.execute("UPDATE sales_customer_owners SET owner_id='seller',status='assigned',source='admin_transfer'")
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    daily.refresh()
    engine.reconcile()
    fixed = engine.customer_owners(admin())['items'][0]
    assert fixed['status'] == 'unassigned' and fixed['owner_id'] == ''
    assert not engine.list({'id':'seller','roles':['sales']}, role='sales')['items']
    with store.connect() as db:
        assert {r[0] for r in db.execute("SELECT assignee_id FROM daily_work_tasks WHERE role='sales' AND condition_state='active'")} == {''}


def test_old_unscoped_owner_route_and_sales_rule_writes_are_rejected(context):
    store, daily = context
    item = row('PO#1', 'FB003')
    item['summary'] = item['summary'].replace('2026-10-10', '2026-10-01')
    seed(store, [item], [allocation(item, 0, shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    app = Flask(__name__)
    app.register_blueprint(customer_order_routes(engine))
    app.before_request(lambda: setattr(g, 'current_user', admin()))
    @app.errorhandler(DailyWorkConflict)
    def conflict(error):
        return {'error':error.code}, 409
    body = dict(profile_id='P',customer='客户甲',owner_id='seller',version=1,idempotency_key='old',reason='旧版')
    with app.test_client() as client:
        old = client.put('/api/v2/daily-work/customer-owners', json=body)
        assert old.status_code == 409 and old.json['error'] == 'client_upgrade_required'
        new = client.put('/api/v2/daily-work/customer-owners', json={**body, 'scope':'customer'})
        assert new.status_code == 200 and new.json['owner']['owner_id'] == 'seller'
    with pytest.raises(DailyWorkConflict) as error:
        daily.set_assignment_rule(admin(), 'sales', 'customer', '客户甲', 'admin')
    assert error.value.code == 'client_upgrade_required'
    assert engine.customer_owners(admin())['items'][0]['owner_id'] == 'seller'


def test_same_name_across_profiles_does_not_share_unscoped_phone_rule(context):
    store, daily = context
    first = row('PO#1', 'FB003')
    second = {**row('OTHER#1', 'FB004'), 'profile_id':'OTHER'}
    seed(store, [first, second], [allocation(first, 0, shortage=1)])
    with store.connect(write=True) as db:
        db.execute("INSERT INTO daily_work_assignment_rules VALUES ('phone','sales','customer','客户甲','seller',100,1,'','')")
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    owners = engine.customer_owners(admin())['items']
    assert len(owners) == 2
    assert all(value['status'] == 'review' and not value['owner_id'] for value in owners)
    assert all(value['source'] == 'phone_rule_scope_review' for value in owners)
    assert all(value['can_set_owner'] for value in owners)


def test_owner_change_rolls_back_every_task_when_one_update_fails(context):
    import sqlite3
    store, daily = context
    rows = [row('PO#1','FB003'), row('OTHER#1','FB004')]
    for item in rows:
        item['summary'] = item['summary'].replace('2026-10-10', '2026-10-01')
    seed(store, rows, [allocation(item, 0, shortage=1) for item in rows])
    daily.refresh()
    engine = CustomerOrders(store, daily)
    engine.reconcile()
    with store.connect(write=True) as db:
        task = db.execute("SELECT id FROM daily_work_tasks WHERE role='sales' ORDER BY id DESC LIMIT 1").fetchone()[0]
        db.execute("CREATE TRIGGER fail_owner BEFORE UPDATE OF assignee_id ON daily_work_tasks WHEN NEW.id='" + task + "' BEGIN SELECT RAISE(ABORT, 'simulated concurrent failure'); END")
        before = [dict(row) for row in db.execute('SELECT * FROM customer_order_followups')]
    with pytest.raises(sqlite3.IntegrityError):
        assign(engine, 'atomic-failure')
    with store.connect() as db:
        assert db.execute('SELECT owner_id FROM sales_customer_owners').fetchone()[0] == ''
        assert not db.execute("SELECT 1 FROM daily_work_tasks WHERE role='sales' AND assignee_id<>''").fetchone()
        assert [dict(row) for row in db.execute('SELECT * FROM customer_order_followups')] == before
        assert not db.execute("SELECT 1 FROM sales_customer_owner_events WHERE idempotency_key='owner:atomic-failure'").fetchone()


def test_new_owner_controls_current_tasks_and_old_owner_keeps_history_only(context):
    store, daily = context
    with store.connect(write=True) as db:
        db.execute("INSERT INTO staff_users VALUES ('other','other','销售乙','','[\"sales\"]',0,1,3,'','')")
    item = row('PO#1', 'FB003')
    item['summary'] = item['summary'].replace('2026-10-10', '2026-10-01')
    seed(store, [item], [allocation(item,0,shortage=1)])
    daily.refresh()
    engine = CustomerOrders(store,daily)
    engine.reconcile()
    assign(engine, 'initial')
    with store.connect() as db:
        task = dict(db.execute("SELECT * FROM daily_work_tasks WHERE role='sales'").fetchone())
    person = {'id':'seller','roles':['sales']}
    daily.action(task['id'],person,'progress','old-progress',task['version'],'已联系')
    assign(engine,'replacement',owner='other')
    with store.connect() as db:
        updated = dict(db.execute('SELECT * FROM daily_work_tasks WHERE id=?',(task['id'],)).fetchone())
        original = db.execute("SELECT actor_id,note FROM daily_work_events WHERE idempotency_key='old-progress'").fetchone()
        assert original['actor_id']=='seller' and original['note']=='已联系'
    assert updated['status']=='in_progress' and updated['assignee_id']=='other'
    with pytest.raises(DailyWorkConflict):
        daily.action(task['id'],person,'progress','after-transfer',updated['version'],'不再有权限')
    assert not daily.list(person,role='sales')['items']
    daily.action(task['id'],{'id':'other','roles':['sales']},'progress','new-progress',updated['version'],'接手跟进')
