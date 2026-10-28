"""No network: irreversible clicks are fake and every crash is replayed safely."""
import json
import multiprocessing
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.purchase_execution import (AdapterCapabilities, ExecutionPolicy,
    OrderLookup, PlatformOrder, PreparedPurchase, PurchaseExecution)
from test_staff_auth import public_app, admin_session


NOW = 1791271800.0
WH = "a" * 64
PRICE = "b" * 64


class TestStore:
    __test__ = False

    def __init__(self, directory):
        self.runtime = directory
        self.path = directory / "execution.sqlite3"

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


class FakeAdapter:
    def __init__(self, store):
        self.store = store
        self.capabilities = AdapterCapabilities("淘钉钉", "account-A", True, True, True, True, True, True)
        self.inspections = self.submissions = self.payments = self.lookups = 0
        self.order = None
        self.quote_changes = {}
        self.lookup_complete = True
        self.order_changes = {}
        self.duplicate = False
        self.submit_exception = self.pay_exception = None
        self.submit_lands = self.pay_lands = True
        self.on_submit = self.on_pay = self.on_inspect = None

    def inspect(self, request):
        self.inspections += 1
        if self.on_inspect:
            self.on_inspect(request)
        return replace(PreparedPurchase("淘钉钉", "account-A", "checkout-stable-1", "platform-1", "LS1000",
            "P|08390|ptype|sku|1", "支", "40", "10", "4", "盒", "2", "80", "0", "0", "80",
            "tax_inclusive", PRICE, NOW, request.checks, "支", "40", "1", "f" * 64, "CNY"), **self.quote_changes)

    def lookup(self, intent):
        self.lookups += 1
        orders = () if self.order is None else (replace(self.order, **self.order_changes),)
        if self.duplicate:
            orders = orders + orders
        return OrderLookup(self.lookup_complete, "account-A", intent.prepared.checkout_reference, orders)

    def _assert_intent_is_committed(self, intent):
        # A separate connection observes the commit before the fake click.
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM purchase_execution_intents WHERE id=?", (intent.id,)).fetchone()
            assert row and json.loads(row["payload"])["phase"] == intent.phase
            state = db.execute("SELECT status FROM purchase_execution_attempts WHERE id=?", (intent.attempt_id,)).fetchone()[0]
            assert state == intent.phase + "_result_unknown"

    def submit_order(self, intent):
        self._assert_intent_is_committed(intent)
        self.submissions += 1
        if self.submit_lands:
            quote = intent.prepared
            self.order = PlatformOrder("platform-order-1", quote.platform, quote.account_id, quote.checkout_reference,
                quote.platform_sku, quote.erp_code, quote.product_identity, quote.unit, quote.quantity,
                quote.payable_amount, "0", "unpaid", "c" * 64, NOW, quote.platform_unit,
                quote.platform_quantity, quote.base_units_per_platform_unit, quote.unit_conversion_evidence_hash, quote.currency)
        if self.on_submit:
            self.on_submit(intent)
        if self.submit_exception:
            raise self.submit_exception

    def pay_order(self, intent):
        self._assert_intent_is_committed(intent)
        assert intent.platform_order_no == "platform-order-1"
        self.payments += 1
        if self.pay_lands:
            self.order = replace(self.order, status="paid", paid_amount="80")
        if self.on_pay:
            self.on_pay(intent)
        if self.pay_exception:
            raise self.pay_exception


@pytest.fixture
def execution(tmp_path):
    store = TestStore(tmp_path)
    now = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
    checks = {"product_name": "螺栓", "standard_check": "GB5783", "spec_check": "M6*30",
              "model_check": "8.8发黑", "warehouse_check": "自营", "pack_size": "10支/盒", "price_limit": "3"}
    price = {"task_id": "task-1", "profile_id": "P", "erp_code": "LS1000",
        "product_identity": "P|08390|ptype|sku|1", "data_version": "v1", "unit": "支",
        "weighted_sale_price": "3.75", "price_limit": "3", "tax_basis": "tax_inclusive",
        "source_hash": PRICE, "source_updated_at": now}
    frozen = {"profile_id": "P", "erp_code": "LS1000", "product_identity": "P|08390|ptype|sku|1",
        "base_shortage": "40", "order_nos": ["sales-1"], "platform": "淘钉钉", "platform_sku": "platform-1",
        "whitelist_checks": checks, "amount_limits": {"per_item": "2000", "per_batch": "4000", "max_items": 3},
        "source_data_version": "v1", "source_task_version": 1, "whitelist_source_hash": WH, "price_bound": price}
    with store.connect(write=True) as db:
        db.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE daily_work_settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE daily_work_tasks(id TEXT PRIMARY KEY,role TEXT,task_type TEXT,status TEXT,
                condition_state TEXT,profile_id TEXT,sku TEXT,payload TEXT,source_data_version TEXT);
            CREATE TABLE purchase_whitelist_sources(source_id TEXT PRIMARY KEY,source_hash TEXT);
            CREATE TABLE purchase_whitelist_entries(id TEXT PRIMARY KEY,profile_id TEXT,erp_code TEXT,
                platform TEXT,platform_sku TEXT,product_name TEXT,standard_check TEXT,spec_check TEXT,
                model_check TEXT,warehouse_check TEXT,pack_size TEXT,price_limit TEXT,enabled INTEGER,
                review_reason TEXT,source_id TEXT);
            CREATE TABLE purchase_price_bounds(task_id TEXT PRIMARY KEY,profile_id TEXT,erp_code TEXT,
                product_identity TEXT,data_version TEXT,unit TEXT,weighted_sale_price TEXT,price_limit TEXT,
                tax_basis TEXT,source_hash TEXT,source_updated_at TEXT);
            CREATE TABLE purchase_assistant_plans(id TEXT PRIMARY KEY,task_id TEXT UNIQUE,profile_id TEXT,
                whitelist_entry_id TEXT,frozen TEXT,status TEXT);
            CREATE TABLE purchase_assistant_jobs(id TEXT PRIMARY KEY,plan_id TEXT UNIQUE,status TEXT,
                detail TEXT DEFAULT '',platform_order_no TEXT DEFAULT '',paid_amount TEXT DEFAULT '',
                erp_order_no TEXT DEFAULT '',attempt_id TEXT DEFAULT '',started_at TEXT DEFAULT '',updated_at TEXT);
        """)
        db.execute("INSERT INTO settings VALUES ('data_version',?)", (json.dumps("v1"),))
        db.execute("INSERT INTO settings VALUES ('data_source_updated_at',?)", (json.dumps(now),))
        db.execute("INSERT INTO daily_work_settings VALUES ('projection_data_version','v1')")
        db.execute("INSERT INTO daily_work_settings VALUES ('projection_source_updated_at',?)", (now,))
        db.execute("INSERT INTO daily_work_tasks VALUES (?,?,?,?,?,?,?,?,?)", ("task-1", "purchase",
            "procurement_gap", "in_progress", "active", "P", "LS1000",
            json.dumps({"shortage": "40", "product_identity": frozen["product_identity"], "order_nos": ["sales-1"]}), "v1"))
        db.execute("INSERT INTO purchase_whitelist_sources VALUES (?,?)", ("source-1", WH))
        db.execute("INSERT INTO purchase_whitelist_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("white-1", "P", "LS1000", "淘钉钉", "platform-1", *checks.values(), 1, "", "source-1"))
        db.execute("INSERT INTO purchase_price_bounds VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(price.values()))
        db.execute("INSERT INTO purchase_assistant_plans VALUES (?,?,?,?,?,?)",
            ("plan-1", "task-1", "P", "white-1", json.dumps(frozen), "in_progress"))
        db.execute("INSERT INTO purchase_assistant_jobs(id,plan_id,status,updated_at) VALUES (?,?,?,?)",
            ("job-1", "plan-1", "queued", now))
    adapter = FakeAdapter(store)
    policy = ExecutionPolicy(True, True, True, "account-A")
    engine = PurchaseExecution(store, adapter, policy, clock=lambda: NOW)
    return engine, adapter, store


def assert_no_clicks(adapter):
    assert (adapter.submissions, adapter.payments) == (0, 0)


def test_default_off_has_no_platform_calls_or_job_state_change(execution):
    engine, adapter, store = execution
    default = PurchaseExecution(store, adapter, clock=lambda: NOW)
    assert default.execute("job-1")["status"] == "disabled"
    assert (adapter.inspections, adapter.lookups) == (0, 0)
    assert_no_clicks(adapter)
    with store.connect() as db:
        assert db.execute("SELECT status FROM purchase_assistant_jobs").fetchone()[0] == "queued"
        assert db.execute("SELECT count(*) FROM purchase_execution_attempts").fetchone()[0] == 0


def test_confirmed_order_and_payment_commit_before_each_click_and_replay_once(execution):
    engine, adapter, store = execution
    assert engine.execute("job-1")["status"] == "paid_confirmed"
    assert engine.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM purchase_execution_intents").fetchone()[0] == 2
        job = db.execute("SELECT * FROM purchase_assistant_jobs").fetchone()
        assert (job["paid_amount"], job["platform_order_no"], job["erp_order_no"]) == ("80", "platform-order-1", "")
        assert db.execute("SELECT status FROM purchase_assistant_plans").fetchone()[0] == "in_progress"


@pytest.mark.parametrize("phase", ["order", "payment"])
def test_timeout_after_landed_click_recovers_by_lookup_without_retry(execution, phase):
    engine, adapter, _ = execution
    setattr(adapter, "submit_exception" if phase == "order" else "pay_exception", TimeoutError("secret must not be logged"))
    assert engine.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


@pytest.mark.parametrize("phase", ["order", "payment"])
def test_process_crash_leaves_durable_unknown_and_restart_only_reconciles(execution, phase):
    engine, adapter, store = execution
    field = "submit_exception" if phase == "order" else "pay_exception"
    setattr(adapter, field, SystemExit("process death"))
    with pytest.raises(SystemExit):
        engine.execute("job-1")
    assert engine.result("job-1")["status"] == phase + "_result_unknown"
    setattr(adapter, field, None)
    restarted = PurchaseExecution(store, adapter, engine.policy, clock=lambda: NOW)
    assert restarted.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def test_missing_order_is_not_proof_of_safe_resubmit(execution):
    engine, adapter, _ = execution
    adapter.submit_lands = False
    adapter.submit_exception = TimeoutError()
    first = engine.execute("job-1")
    assert first["status"] == "order_result_unknown" and first["detail"] == "platform_order_not_found"
    for _ in range(3):
        assert engine.execute("job-1")["status"] == "order_result_unknown"
    assert (adapter.submissions, adapter.payments) == (1, 0)


def test_unpaid_after_payment_timeout_never_pays_again(execution):
    engine, adapter, _ = execution
    adapter.pay_lands = False
    adapter.pay_exception = TimeoutError()
    assert engine.execute("job-1")["status"] == "payment_result_unknown"
    assert engine.execute("job-1")["status"] == "payment_result_unknown"
    assert (adapter.submissions, adapter.payments) == (1, 1)
    adapter.order = replace(adapter.order, status="paid", paid_amount="80")
    assert engine.recover("job-1")["status"] == "paid_confirmed"
    assert adapter.payments == 1


def test_recovery_remains_read_only_when_writes_disabled_and_sources_revoked(execution):
    engine, adapter, store = execution
    adapter.pay_exception = SystemExit()
    with pytest.raises(SystemExit):
        engine.execute("job-1")
    with store.connect(write=True) as db:
        db.execute("UPDATE purchase_whitelist_entries SET enabled=0")
        db.execute("DELETE FROM purchase_price_bounds")
    disabled = PurchaseExecution(store, adapter, replace(engine.policy, enabled=False), clock=lambda: NOW)
    assert disabled.recover("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


@pytest.mark.parametrize("change,code", [
    ({"account_verified": False}, "platform_account_unverified"),
    ({"account_id": "other-account"}, "platform_account_unverified"),
    ({"complete_order_lookup": False}, "platform_order_recovery_unverified"),
    ({"durable_order_binding": False}, "platform_order_recovery_unverified"),
    ({"order_submission_is_unpaid": False}, "order_submit_unverified"),
    ({"order_submit_enabled": False}, "order_submit_unverified"),
    ({"platform": "unknown"}, "platform_not_allowed"),
    ({"account_verified": "false"}, "platform_account_unverified"),
    ({"order_submit_enabled": "false"}, "order_submit_unverified"),
])
def test_unverified_adapter_blocks_before_any_browser_work(execution, change, code):
    engine, adapter, _ = execution
    adapter.capabilities = replace(adapter.capabilities, **change)
    assert engine.execute("job-1")["detail"] == code
    assert adapter.inspections == 0
    assert_no_clicks(adapter)


@pytest.mark.parametrize("sql,params,code", [
    ("UPDATE purchase_whitelist_entries SET enabled=0", (), "purchase_whitelist_changed"),
    ("UPDATE purchase_whitelist_entries SET spec_check='M8*30'", (), "purchase_whitelist_changed"),
    ("UPDATE purchase_whitelist_sources SET source_hash=?", ("d" * 64,), "purchase_whitelist_changed"),
    ("UPDATE purchase_price_bounds SET source_hash=?", ("d" * 64,), "price_source_changed"),
    ("UPDATE purchase_price_bounds SET price_limit='2.9'", (), "price_source_changed"),
    ("UPDATE purchase_price_bounds SET unit='箱'", (), "price_source_changed"),
    ("UPDATE settings SET value=? WHERE key='data_version'", (json.dumps("v2"),), "task_projection_conflict"),
    ("UPDATE daily_work_tasks SET condition_state='resolved'", (), "purchase_task_not_active"),
    ("UPDATE daily_work_tasks SET payload=?", (json.dumps({"shortage": "39", "product_identity": "P|08390|ptype|sku|1"}),), "purchase_demand_changed"),
    ("DELETE FROM purchase_price_bounds", (), "price_source_missing"),
])
def test_live_source_mutations_block_without_any_click(execution, sql, params, code):
    engine, adapter, store = execution
    with store.connect(write=True) as db:
        db.execute(sql, params)
    result = engine.execute("job-1")
    assert result["status"] == "blocked" and result["detail"] == code
    assert_no_clicks(adapter)


@pytest.mark.parametrize("changes,code", [
    ({"quantity": "41"}, "platform_unit_conversion_mismatch"),
    ({"package_count": "4.5"}, "platform_quantity_or_packaging_changed"),
    ({"package_size": "100"}, "platform_packaging_changed"),
    ({"package_unit": ""}, "platform_packaging_changed"),
    ({"unit": "箱"}, "platform_identity_or_quote_changed"),
    ({"platform_sku": "different"}, "platform_identity_or_quote_changed"),
    ({"tax_basis": "tax_exclusive"}, "platform_identity_or_quote_changed"),
    ({"checkout_reference": ""}, "platform_identity_or_quote_changed"),
    ({"price_source_hash": "e" * 64}, "platform_identity_or_quote_changed"),
    ({"inclusive_unit_price": "4", "line_amount": "160", "payable_amount": "160"}, "platform_price_over_limit"),
    ({"line_amount": "79"}, "platform_amount_unverified"),
    ({"shipping_amount": "1"}, "platform_amount_unverified"),
    ({"shipping_amount": "41", "payable_amount": "121"}, "platform_price_over_limit"),
    ({"observed_at": NOW - 301}, "platform_quote_stale"),
    ({"observed_at": NOW + 1}, "platform_quote_stale"),
    ({"quantity": "NaN"}, "number_invalid"),
    ({"quantity": True}, "number_invalid"),
    ({"platform_unit": "base_unit:1"}, "platform_packaging_changed"),
    ({"platform_unit": ""}, "platform_packaging_changed"),
    ({"unit_conversion_evidence_hash": ""}, "platform_unit_conversion_unverified"),
    ({"base_units_per_platform_unit": "1000"}, "platform_unit_conversion_mismatch"),
    ({"currency": "USD"}, "platform_currency_unverified"),
])
def test_platform_quantity_price_identity_and_quote_proofs_are_required(execution, changes, code):
    engine, adapter, _ = execution
    adapter.quote_changes = changes
    result = engine.execute("job-1")
    assert result["status"] == "blocked" and result["detail"] == code
    assert_no_clicks(adapter)


def test_amount_limit_even_when_unit_price_is_valid(execution):
    engine, adapter, store = execution
    with store.connect(write=True) as db:
        frozen = json.loads(db.execute("SELECT frozen FROM purchase_assistant_plans").fetchone()[0])
        frozen["amount_limits"]["per_item"] = "79"
        db.execute("UPDATE purchase_assistant_plans SET frozen=?", (json.dumps(frozen),))
    assert engine.execute("job-1")["detail"] == "platform_amount_over_limit"
    assert_no_clicks(adapter)


def test_source_change_between_inspect_and_intent_is_atomically_rejected(execution):
    engine, adapter, store = execution
    def change(_):
        with store.connect(write=True) as db:
            db.execute("UPDATE purchase_price_bounds SET source_hash=?", ("d" * 64,))
    adapter.on_inspect = change
    assert engine.execute("job-1")["detail"] == "price_source_changed"
    assert_no_clicks(adapter)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM purchase_execution_intents").fetchone()[0] == 0


def test_source_revoked_after_order_blocks_payment_but_keeps_external_order(execution):
    engine, adapter, store = execution
    def revoke(_):
        with store.connect(write=True) as db:
            db.execute("UPDATE purchase_whitelist_entries SET enabled=0")
    adapter.on_submit = revoke
    result = engine.execute("job-1")
    assert result["detail"] == "purchase_whitelist_changed" and result["platform_order_no"] == "platform-order-1"
    assert (adapter.submissions, adapter.payments) == (1, 0)


def test_duplicate_lookup_or_incomplete_readback_keeps_unknown(execution):
    engine, adapter, _ = execution
    adapter.on_submit = lambda _: setattr(adapter, "duplicate", True)
    assert engine.execute("job-1")["detail"] == "platform_order_not_unique"
    adapter.duplicate = False
    adapter.lookup_complete = False
    assert engine.recover("job-1")["detail"] == "platform_lookup_incomplete"
    assert (adapter.submissions, adapter.payments) == (1, 0)


@pytest.mark.parametrize("changes,code", [
    ({"quantity": "40000"}, "platform_order_mismatch"),
    ({"order_no": "wrong"}, "platform_order_mismatch"),
    ({"paid_amount": "79.99"}, "platform_paid_amount_mismatch"),
    ({"evidence_hash": ""}, "platform_order_mismatch"),
    ({"observed_at": NOW - 301}, "platform_order_evidence_stale"),
])
def test_paid_readback_must_match_exact_order_quantity_amount_and_evidence(execution, changes, code):
    engine, adapter, _ = execution
    adapter.on_pay = lambda _: setattr(adapter, "order_changes", changes)
    assert engine.execute("job-1")["detail"] == code
    assert engine.execute("job-1")["status"] == "payment_result_unknown"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def test_order_cancelled_during_lookup_never_triggers_payment(execution):
    engine, adapter, _ = execution
    adapter.on_submit = lambda _: setattr(adapter, "order_changes", {"status": "cancelled"})
    assert engine.execute("job-1")["status"] == "manual_review"
    assert (adapter.submissions, adapter.payments) == (1, 0)


def test_explicit_order_and_payment_gates_are_independent(execution):
    engine, adapter, store = execution
    engine.policy = replace(engine.policy, allow_order_submit=False)
    assert engine.execute("job-1")["status"] == "ready"
    assert_no_clicks(adapter)
    engine.policy = replace(engine.policy, allow_order_submit=True, allow_payment=False)
    assert engine.execute("job-1")["status"] == "awaiting_payment"
    assert (adapter.submissions, adapter.payments) == (1, 0)
    engine.policy = replace(engine.policy, allow_payment=True)
    assert engine.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def test_atomic_lock_blocks_second_worker_and_releases_after_crash(execution):
    engine, adapter, store = execution
    entered, release = threading.Event(), threading.Event()
    def pause(_):
        entered.set()
        assert release.wait(3)
    adapter.on_submit = pause
    result = []
    worker = threading.Thread(target=lambda: result.append(engine.execute("job-1")))
    worker.start()
    assert entered.wait(3)
    other = PurchaseExecution(store, adapter, engine.policy, clock=lambda: NOW)
    assert other.execute("job-1")["detail"] == "execution_busy"
    release.set()
    worker.join(3)
    assert not worker.is_alive() and result[0]["status"] == "paid_confirmed"
    assert other.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def test_intent_tampering_is_blocked_without_retry(execution):
    engine, adapter, store = execution
    adapter.submit_lands = False
    assert engine.execute("job-1")["status"] == "order_result_unknown"
    with store.connect(write=True) as db:
        db.execute("UPDATE purchase_execution_intents SET payload='{}'")
    assert engine.execute("job-1")["detail"] == "execution_intent_corrupt"
    assert engine.result("job-1")["status"] == "order_result_unknown"
    assert (adapter.submissions, adapter.payments) == (1, 0)


def test_frozen_plan_tampering_cannot_grant_new_quantity(execution):
    engine, adapter, store = execution
    engine.policy = replace(engine.policy, allow_order_submit=False)
    assert engine.execute("job-1")["status"] == "ready"
    with store.connect(write=True) as db:
        frozen = json.loads(db.execute("SELECT frozen FROM purchase_assistant_plans").fetchone()[0])
        frozen["base_shortage"] = "400"
        db.execute("UPDATE purchase_assistant_plans SET frozen=?", (json.dumps(frozen),))
    engine.policy = replace(engine.policy, allow_order_submit=True)
    assert engine.execute("job-1")["detail"] == "frozen_plan_changed"
    assert_no_clicks(adapter)


def test_erp_base_unit_marker_needs_explicit_verified_platform_conversion(execution):
    engine, adapter, store = execution
    with store.connect(write=True) as db:
        frozen = json.loads(db.execute("SELECT frozen FROM purchase_assistant_plans").fetchone()[0])
        frozen["price_bound"]["unit"] = "base_unit:1"
        db.execute("UPDATE purchase_assistant_plans SET frozen=?", (json.dumps(frozen),))
        db.execute("UPDATE purchase_price_bounds SET unit='base_unit:1'")
    adapter.quote_changes = {"unit": "base_unit:1"}
    assert engine.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def test_final_checkout_changes_do_not_pay_an_old_external_order(execution):
    engine, adapter, _ = execution
    def change(_):
        adapter.quote_changes = {"inclusive_unit_price": "2.1", "line_amount": "84", "payable_amount": "84"}
    adapter.on_submit = change
    result = engine.execute("job-1")
    assert result["status"] == "blocked" and result["detail"] == "platform_frozen_checkout_changed"
    assert result["platform_order_no"] == "platform-order-1"
    assert (adapter.submissions, adapter.payments) == (1, 0)


def test_order_write_gate_revoked_does_not_prevent_paid_recovery(execution):
    engine, adapter, _ = execution
    adapter.pay_exception = SystemExit()
    with pytest.raises(SystemExit):
        engine.execute("job-1")
    adapter.capabilities = replace(adapter.capabilities, order_submit_enabled=False, payment_enabled=False)
    assert engine.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def _crash_after_platform_payment(directory):
    store = TestStore(Path(directory))
    adapter = FakeAdapter(store)
    def crash(_):
        (store.runtime / "fake-platform-receipt.json").write_text(json.dumps(asdict(adapter.order)))
        os._exit(73)
    adapter.on_pay = crash
    engine = PurchaseExecution(store, adapter, ExecutionPolicy(True, True, True, "account-A"), clock=lambda: NOW)
    engine.execute("job-1")


def test_actual_process_exit_releases_lock_and_payment_recovery_only_reads(execution):
    engine, adapter, store = execution
    worker = multiprocessing.get_context("spawn").Process(target=_crash_after_platform_payment,
                                                          args=(str(store.runtime),))
    worker.start()
    worker.join(10)
    if worker.is_alive():
        worker.terminate()
        worker.join(2)
    assert worker.exitcode == 73
    assert engine.result("job-1")["status"] == "payment_result_unknown"
    adapter.order = PlatformOrder(**json.loads((store.runtime / "fake-platform-receipt.json").read_text()))
    assert engine.recover("job-1")["status"] == "paid_confirmed"
    assert_no_clicks(adapter)


def test_no_hidden_secret_from_adapter_exception_is_recorded(execution):
    engine, adapter, store = execution
    adapter.submit_lands = False
    adapter.submit_exception = RuntimeError("PASSWORD-THIS-MUST-NOT-LEAK")
    assert engine.execute("job-1")["status"] == "order_result_unknown"
    with store.connect() as db:
        events = " ".join(row[0] for row in db.execute("SELECT details FROM purchase_execution_events"))
        assert "PASSWORD-THIS-MUST-NOT-LEAK" not in events


@pytest.mark.parametrize("sql,params,code", [
    ("INSERT INTO settings VALUES ('restoration_hold','true')", (), "restoration_hold"),
    ("INSERT INTO settings VALUES ('restoration_hold','unknown')", (), "restoration_hold"),
    ("UPDATE daily_work_settings SET value='v0' WHERE key='projection_data_version'", (), "task_projection_conflict"),
    ("UPDATE daily_work_settings SET value='old' WHERE key='projection_source_updated_at'", (), "task_projection_conflict"),
    ("DELETE FROM daily_work_settings", (), "task_projection_conflict"),
    ("UPDATE purchase_assistant_jobs SET status='awaiting_unit_review'", (), "purchase_job_not_queued"),
    ("UPDATE purchase_assistant_jobs SET status='awaiting_price_source'", (), "purchase_job_not_queued"),
])
def test_recovery_hold_stale_projection_or_unit_review_cannot_create_attempt(execution, sql, params, code):
    engine, adapter, store = execution
    with store.connect(write=True) as db:
        db.execute(sql, params)
    assert engine.execute("job-1")["detail"] == code
    assert adapter.inspections == 0
    assert_no_clicks(adapter)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM purchase_execution_attempts").fetchone()[0] == 0


def test_recovery_hold_enabled_after_order_does_not_erase_paid_fact_or_pay(execution):
    engine, adapter, store = execution
    adapter.pay_exception = SystemExit()
    with pytest.raises(SystemExit):
        engine.execute("job-1")
    with store.connect(write=True) as db:
        db.execute("INSERT INTO settings VALUES ('restoration_hold','true')")
    assert engine.recover("job-1")["status"] == "paid_confirmed"
    assert engine.result("job-1")["erp_status"] == "not_recorded"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def test_projection_refresh_failure_after_order_blocks_payment(execution):
    engine, adapter, store = execution
    def break_projection(_):
        with store.connect(write=True) as db:
            db.execute("UPDATE daily_work_settings SET value='v0' WHERE key='projection_data_version'")
    adapter.on_submit = break_projection
    result = engine.execute("job-1")
    assert result["status"] == "blocked" and result["detail"] == "task_projection_conflict"
    assert result["platform_order_no"] == "platform-order-1"
    assert (adapter.submissions, adapter.payments) == (1, 0)


def test_execution_consumes_real_assistant_enrollment_and_service_sql_contract(public_app, execution):
    from test_procurement_assistant import _task
    from core.store import dumps
    _task(public_app, code="LS1000")
    store = public_app.extensions["service"]["store"]
    _, _, fake_store = execution
    with fake_store.connect() as db:
        price = dict(db.execute("SELECT * FROM purchase_price_bounds").fetchone())
        checks = json.loads(db.execute("SELECT frozen FROM purchase_assistant_plans").fetchone()[0])["whitelist_checks"]
    price["task_id"] = "purchase:1"
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_source_updated_at'", (dumps(price["source_updated_at"]),))
        db.execute("UPDATE daily_work_settings SET value=? WHERE key='projection_source_updated_at'", (price["source_updated_at"],))
        db.execute("UPDATE daily_work_tasks SET payload=? WHERE id='purchase:1'",
            (dumps({"shortage": "40", "order_nos": ["S1"], "product_identity": price["product_identity"]}),))
        db.execute("""UPDATE purchase_whitelist_entries SET platform_sku='platform-1',product_name=?,
            standard_check=?,spec_check=?,model_check=?,warehouse_check=?,pack_size=?,price_limit=?""", tuple(checks.values()))
        db.execute("INSERT INTO purchase_price_bounds VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(price.values()))
    client = public_app.test_client()
    accepted = client.post("/api/v2/assistant/purchase/plan/purchase:1/enroll",
        headers=admin_session(client), json={"version": 1, "source_version": "v1", "idempotency_key": "core-real-enrollment"})
    assert accepted.status_code == 200, accepted.get_json()
    job_id = accepted.get_json()["job"]["id"]
    adapter = FakeAdapter(store)
    engine = PurchaseExecution(store, adapter, ExecutionPolicy(True, True, True, "account-A"), clock=lambda: NOW)
    assert engine.execute(job_id)["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


@pytest.mark.parametrize("external_status", ["paid", "cancelled", "rejected"])
def test_resumed_payment_rereads_external_terminal_status_and_never_clicks_again(execution, external_status):
    engine, adapter, _ = execution
    engine.policy = replace(engine.policy, allow_payment=False)
    assert engine.execute("job-1")["status"] == "awaiting_payment"
    adapter.order = replace(adapter.order, status=external_status,
                            paid_amount="80" if external_status == "paid" else "0")
    engine.policy = replace(engine.policy, allow_payment=True)
    assert engine.execute("job-1")["status"] == ("paid_confirmed" if external_status == "paid" else "manual_review")
    assert (adapter.submissions, adapter.payments) == (1, 0)


@pytest.mark.parametrize("external_status", ["paid", "cancelled"])
def test_payment_or_cancellation_during_final_inspect_prevents_payment_click(execution, external_status):
    engine, adapter, _ = execution
    def change(_):
        if adapter.inspections == 2:
            adapter.order = replace(adapter.order, status=external_status,
                                    paid_amount="80" if external_status == "paid" else "0")
    adapter.on_inspect = change
    assert engine.execute("job-1")["status"] == ("paid_confirmed" if external_status == "paid" else "manual_review")
    assert (adapter.submissions, adapter.payments) == (1, 0)


@pytest.mark.parametrize("gate", ["enabled", "allow_payment", "payment_enabled"])
def test_revoked_gate_during_final_inspect_is_rechecked_before_intent(execution, gate):
    engine, adapter, store = execution
    def revoke(_):
        if adapter.inspections == 2:
            if gate == "payment_enabled":
                adapter.capabilities = replace(adapter.capabilities, payment_enabled=False)
            else:
                engine.policy = replace(engine.policy, **{gate: False})
    adapter.on_inspect = revoke
    assert engine.execute("job-1")["detail"] == ("execution_disabled" if gate == "enabled" else "payment_disabled")
    assert (adapter.submissions, adapter.payments) == (1, 0)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM purchase_execution_intents WHERE phase='payment'").fetchone()[0] == 0


def test_arithmetically_valid_packaging_cannot_contradict_frozen_whitelist(execution):
    engine, adapter, _ = execution
    adapter.quote_changes = {"package_size": "20", "package_count": "2", "package_unit": "袋"}
    assert engine.execute("job-1")["detail"] == "platform_packaging_changed"
    assert_no_clicks(adapter)


def test_approved_thousand_piece_package_converts_price_and_quantity_explicitly(execution):
    engine, adapter, store = execution
    with store.connect(write=True) as db:
        frozen = json.loads(db.execute("SELECT frozen FROM purchase_assistant_plans").fetchone()[0])
        frozen["whitelist_checks"]["pack_size"] = "0.001千支/支"
        db.execute("UPDATE purchase_assistant_plans SET frozen=?", (json.dumps(frozen),))
        db.execute("UPDATE purchase_whitelist_entries SET pack_size='0.001千支/支'")
    adapter.quote_changes = {"platform_unit": "千支", "platform_quantity": "0.04",
        "base_units_per_platform_unit": "1000", "package_size": "0.001", "package_count": "40",
        "package_unit": "支", "inclusive_unit_price": "2000"}
    assert engine.execute("job-1")["status"] == "paid_confirmed"
    assert (adapter.submissions, adapter.payments) == (1, 1)


def test_resumed_lookup_cannot_replace_a_previously_confirmed_order_number(execution):
    engine, adapter, _ = execution
    engine.policy = replace(engine.policy, allow_payment=False)
    assert engine.execute("job-1")["status"] == "awaiting_payment"
    adapter.order = replace(adapter.order, order_no="replacement-order-2")
    engine.policy = replace(engine.policy, allow_payment=True)
    result = engine.execute("job-1")
    assert result["status"] == "blocked" and result["detail"] == "platform_order_mismatch"
    assert result["platform_order_no"] == "platform-order-1"
    assert (adapter.submissions, adapter.payments) == (1, 0)
