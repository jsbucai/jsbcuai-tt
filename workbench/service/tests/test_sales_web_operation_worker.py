"""Offline worker binding and recovery; no ERP browser or writes are used."""
import hashlib
import json
import sys
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
import xlwt

from core.sales_web_import import EXPECTED_HEADERS, WebImportConfig, _values
from core.sales_web_operation import (SalesHostScope, prepare_sales_spec,
                                      run_sales_import)
from core.sales_trial_authorization import TRIAL_SCHEMA
from core.sales_web_transport import SalesWebImportTransport, WebImportBlocked
from core.store import Store, dumps
from core.web_operations import WebOperations
from core.unit_policy import USER_UNIT_EVIDENCE
from test_sales_web_import import frozen_session, freeze_draft
from tools.web_operation_worker import (_reconcile_sales_once, _run_one,
                                        _sales_child_process, _validated_sales_runner_result)
from tools.sales_web_host_preflight import build_report
from tools import sales_web_operation_worker as sales_child


NUMBER = "UL261007123456ABCDEF"
SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime():
    with TemporaryDirectory(dir=SERVICE / "runtime") as name:
        yield Path(name)


@pytest.fixture
def sales_case(tmp_path):
    template = tmp_path / "blank.xls"
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("单据导入模板")
    for index, name in enumerate(EXPECTED_HEADERS):
        sheet.write(0, index, name)
    workbook.add_sheet("单据类型").write(0, 0, "销售订单")
    workbook.save(str(template))
    sha = hashlib.sha256(template.read_bytes()).hexdigest()
    host = SalesHostScope(True, True, "P", "account-A", "租户甲", "操作员甲",
                          template, sha, "08390", "仓库丙", "销售部", "制单员甲",
                          ("经手人乙",))
    session = frozen_session()
    session["id"] = "sales-session-1"
    draft = json.loads(session["draft"])
    draft["lines"][0]["unit_evidence"] = USER_UNIT_EVIDENCE
    freeze_draft(session, draft)
    details = {"order_date": "2026-10-07", "profile_id": "P",
               "account_id": "account-A", "data_version": "v1",
               "owner_id": "sales", "department_name": "销售部",
               "maker_name": "制单员甲", "handler_name": "经手人乙",
               "warehouse_id": "08390", "warehouse_name": "仓库丙",
               "template_sha256": sha}
    payload = {"schema": "sales_web_import_v1", "session_id": session["id"],
               "confirmed_version": session["confirmed_version"],
               "confirmed_hash": session["confirmed_hash"], "order_number": NUMBER,
               "profile_id": "P", "account_id": "account-A",
               "customer_name": "客户甲", "frozen_session": session,
               "import_config": details,
               "customer_binding": {"customer_name": "客户甲", "erp_customer_id": "customer-1",
                   "profile_id": "P", "account_id": "account-A", "match_count": 1,
                   "source": "erp_customer_exact_lookup",
                   "source_hash": hashlib.sha256(b"customer-1").hexdigest(),
                   "observed_at": datetime.now(timezone.utc).isoformat()},
               "unit_acceptance": {"base_unit_id": "1", "unit_name": "支",
                                   "source": USER_UNIT_EVIDENCE},
               "line_count": 1, "line_unit_evidence": [USER_UNIT_EVIDENCE]}
    projection = WebImportConfig(template, tmp_path / "unused.xls", NUMBER,
        date(2026, 10, 7), "P", "v1", "sales", "销售部", "制单员甲",
        "经手人乙", "08390", "仓库丙")
    payload["summary"] = _values(session, projection)[3]
    operation = {"id": uuid.uuid4().hex, "kind": "sales_order_import",
                 "role": "sales", "profile_id": "P", "target_id": NUMBER}
    return host, payload, operation


def test_frozen_two_line_same_sku_builds_distinct_validated_workbook_lines(sales_case, runtime):
    host, payload, operation = sales_case
    session = payload["frozen_session"]
    draft = json.loads(session["draft"])
    draft["lines"].append(dict(draft["lines"][0], quantity="2", unit_price="2.26",
                               quoted_unit_price="2.26", untaxed_unit_price="2.000",
                               untaxed_line_amount="4.00"))
    freeze_draft(session, draft)
    payload["confirmed_hash"] = session["confirmed_hash"]
    payload["line_count"] = 2
    payload["line_unit_evidence"] = [USER_UNIT_EVIDENCE, USER_UNIT_EVIDENCE]
    spec = prepare_sales_spec(payload, operation, host, runtime)
    assert [line["sku"] for line in spec.lines_expected] == ["LS1000", "LS1000"]
    assert [line["line_id"] for line in spec.lines_expected] == ["1", "2"]
    assert [line["base_unit_id"] for line in spec.lines_expected] == ["1", "1"]
    assert spec.list_expected["untaxed_total"] == "7.00"
    assert spec.list_expected["taxed_total"] == "7.91"
    assert spec.detail_expected["summary"].endswith("#交期=2026-10-16")
    assert spec.detail_expected["note"] == draft.get("note", "").strip()
    assert SalesWebImportTransport(runtime).prepare(spec).is_file()


def test_recovery_without_original_attempt_never_reaches_browser(sales_case, runtime):
    host, payload, operation = sales_case
    payload["customer_binding"]["observed_at"] = (
        datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    host = replace(host, enabled=False, acceptance_complete=False,
                   template_path=runtime / "template-no-longer-present")
    class NoBrowser:
        def __getattr__(self, name):
            raise AssertionError("receipt recovery must not reach a browser")
    with pytest.raises(WebImportBlocked, match="sales_attempt_marker_missing"):
        run_sales_import(payload, operation, host, runtime, NoBrowser(), readback_only=True)
    assert not (runtime / "sales-web-import-staging").exists()


def test_changed_confirmation_or_host_scope_cannot_generate_file(sales_case, runtime):
    host, payload, operation = sales_case
    payload["confirmed_hash"] = "0" * 64
    with pytest.raises(WebImportBlocked, match="sales_frozen_scope_mismatch"):
        prepare_sales_spec(payload, operation, host, runtime)
    assert not (runtime / "sales-web-import-staging").exists()


def test_old_customer_binding_can_pass_host_frozen_checks(sales_case, runtime):
    host, payload, operation = sales_case
    payload["customer_binding"]["observed_at"] = "2020-01-01T00:00:00+00:00"
    assert prepare_sales_spec(payload, operation, host, runtime).source_path.is_file()


@pytest.mark.parametrize("observed_at,code", [
    ("2020-01-01T00:00:00", "sales_customer_binding_invalid"),
    ((datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), "sales_customer_binding_stale"),
])
def test_invalid_or_future_customer_binding_blocks_host_before_file(sales_case, runtime, observed_at, code):
    host, payload, operation = sales_case
    payload["customer_binding"]["observed_at"] = observed_at
    with pytest.raises(WebImportBlocked, match=code):
        prepare_sales_spec(payload, operation, host, runtime)
    assert not (runtime / "sales-web-import-staging").exists()


def _trial_host(sales_case, runtime):
    host, payload, operation = sales_case
    reference = runtime / "reference"
    reference.mkdir()
    total = prepare_sales_spec(payload, operation, host, reference).list_expected["taxed_total"]
    authorization = {"schema": TRIAL_SCHEMA, "session_id": payload["session_id"],
        "confirmed_hash": payload["confirmed_hash"], "order_number": payload["order_number"],
        "customer_name": payload["customer_name"], "line_count": payload["line_count"],
        "taxed_total": total}
    return replace(host, enabled=False, acceptance_complete=False,
                   trial_authorization=authorization), payload, operation


def test_single_trial_requires_every_exact_frozen_field_and_total(sales_case, runtime):
    host, payload, operation = _trial_host(sales_case, runtime)
    assert prepare_sales_spec(payload, operation, host, runtime).list_expected[
        "taxed_total"] == host.trial_authorization["taxed_total"]
    for field, wrong in (("session_id", "another-session"),
                         ("confirmed_hash", "0" * 64),
                         ("order_number", "ULOTHER"),
                         ("customer_name", "另一客户"),
                         ("line_count", 2),
                         ("schema", "other")):
        altered = dict(host.trial_authorization, **{field: wrong})
        with pytest.raises(WebImportBlocked, match="sales_trial_scope_mismatch"):
            prepare_sales_spec(payload, operation,
                               replace(host, trial_authorization=altered), runtime)
    for altered in ({**host.trial_authorization, "extra": "ignored?"},
                    {k: v for k, v in host.trial_authorization.items() if k != "customer_name"}):
        with pytest.raises(WebImportBlocked, match="sales_trial_scope_mismatch"):
            prepare_sales_spec(payload, operation,
                               replace(host, trial_authorization=altered), runtime)
    wrong_total = runtime / "wrong-total"
    wrong_total.mkdir()
    with pytest.raises(WebImportBlocked, match="sales_trial_total_mismatch"):
        prepare_sales_spec(payload, operation, replace(host,
            trial_authorization=dict(host.trial_authorization, taxed_total="999.99")), wrong_total)
    assert prepare_sales_spec(payload, operation, host, wrong_total).list_expected[
        "taxed_total"] == host.trial_authorization["taxed_total"]


def test_trial_never_opens_other_order_and_does_not_enable_production(sales_case, runtime):
    host, payload, operation = _trial_host(sales_case, runtime)
    class NoBrowser:
        def __getattr__(self, name):
            raise AssertionError("mismatched trial reached ERP browser")
    altered = dict(operation, target_id="ULOTHER")
    with pytest.raises(WebImportBlocked):
        run_sales_import(payload, altered, host, runtime, NoBrowser())
    for disabled in (replace(host, trial_authorization=None),
                     replace(host, enabled=True),
                     replace(host, acceptance_complete=True)):
        with pytest.raises(WebImportBlocked, match="sales_writer_disabled"):
            prepare_sales_spec(payload, operation, disabled, runtime)


def test_trial_two_same_sku_lines_one_and_two_pieces_total_339(sales_case, runtime):
    host, payload, operation = sales_case
    session = payload["frozen_session"]
    draft = json.loads(session["draft"])
    first = draft["lines"][0]
    first["quantity"] = "1"
    first["untaxed_line_amount"] = "1.00"
    second = dict(first, quantity="2", untaxed_line_amount="2.00")
    draft["lines"].append(second)
    freeze_draft(session, draft)
    payload["confirmed_hash"] = session["confirmed_hash"]
    payload["line_count"] = 2
    payload["line_unit_evidence"] = [USER_UNIT_EVIDENCE, USER_UNIT_EVIDENCE]
    projection = WebImportConfig(host.template_path, runtime / "projection.xls", NUMBER,
        date(2026, 10, 7), "P", "v1", "sales", "销售部", "制单员甲",
        "经手人乙", "08390", "仓库丙")
    payload["summary"] = _values(session, projection)[3]
    trial, payload, operation = _trial_host((host, payload, operation), runtime)
    assert trial.trial_authorization["taxed_total"] == "3.39"
    spec = prepare_sales_spec(payload, operation, trial, runtime)
    assert spec.list_expected["taxed_total"] == "3.39"
    assert [(line["sku"], line["quantity"]) for line in spec.lines_expected] == [
        ("LS1000", "1"), ("LS1000", "2")]


def test_parent_worker_rejects_unscoped_trial_before_subprocess(sales_case, runtime, tmp_path):
    host, payload, operation = _trial_host(sales_case, runtime)
    config = tmp_path / "host.json"
    config.write_text(json.dumps({"enabled": False, "acceptance_complete": False,
        "trial_authorization": dict(host.trial_authorization)}), encoding="utf-8")
    config.chmod(0o600)
    queue = WebOperations(Store(runtime))
    token = "worker-token-123456"
    assert _sales_child_process(queue, operation, token, payload,
        config_path=config, readback_only=False)["code"] == "sales_host_runner_unavailable"
    changed = dict(payload, session_id="different-session")
    assert _sales_child_process(queue, operation, token, changed,
        config_path=config, readback_only=False)["code"] == "sales_trial_scope_mismatch"
    config.write_text(json.dumps({"enabled": False, "acceptance_complete": False}),
                      encoding="utf-8")
    assert _sales_child_process(queue, operation, token, payload,
        config_path=config, readback_only=False)["code"] == "sales_writer_disabled"


def test_interrupted_worker_claims_readback_only_once(tmp_path, runtime):
    queue = WebOperations(Store(runtime))
    payload = {"schema": "sales_web_import_v1", "order_number": NUMBER,
               "profile_id": "P"}
    inserted = queue.enqueue(kind="sales_order_import", actor_id="sales-1",
        role="sales", profile_id="P", target_id=NUMBER,
        key="submit:recover-0001", payload=payload, total_steps=4)
    first = queue.claim_next("worker-token-123456")
    assert first["id"] == inserted["operation"]["id"]
    queue.progress(first["id"], "worker-token-123456", step=2,
                   stage="ERP 网页处理中", detail="唯一上传进度")
    calls = []
    def runner(_queue, _operation, _token, _payload, *, config_path, readback_only):
        calls.append(readback_only)
        return {"status": "unknown", "code": "sales_attempt_marker_missing"}
    assert _run_one(queue, tmp_path / "no", tmp_path, tmp_path / "config",
                    sales_runner=runner) == first["id"]
    assert calls == [True]
    result = queue.detail({"id": "sales-1", "roles": ["sales"]}, first["id"])["operation"]
    assert result["status"] == "uncertain" and result["stage"] == "导入待核对"
    assert result["result"]["erp_readback_performed"] is False
    # Unknown receipt recovery is held; it cannot become another upload attempt or
    # block this lane in a retry loop silently.
    assert _run_one(queue, tmp_path / "no", tmp_path, tmp_path / "config",
                    sales_runner=runner) == "empty"
    assert calls == [True]


def test_sales_writer_remains_disabled_without_host_config(tmp_path, runtime):
    queue = WebOperations(Store(runtime))
    inserted = queue.enqueue(kind="sales_order_import", actor_id="sales-1",
        role="sales", profile_id="P", target_id=NUMBER,
        key="submit:disabled-0001", payload={"schema": "sales_web_import_v1",
            "order_number": NUMBER, "profile_id": "P"}, total_steps=4)
    assert _run_one(queue, tmp_path / "no", tmp_path) == inserted["operation"]["id"]
    result = queue.detail({"id": "sales-1", "roles": ["sales"]},
                          inserted["operation"]["id"])["operation"]
    assert result["status"] == "blocked"
    assert "sales_host_config_unavailable" in result["detail"]


def test_recovery_and_fallback_hold_keep_session_and_queue_in_sync(runtime):
    queue = WebOperations(Store(runtime))
    with queue.store.connect(write=True) as db:
        db.execute("""CREATE TABLE sales_assistant_sessions(
            id TEXT PRIMARY KEY,status TEXT NOT NULL,updated_at TEXT NOT NULL,
            submission_operation_id TEXT NOT NULL)""")
    inserted = queue.enqueue(kind="sales_order_import", actor_id="sales-1",
        role="sales", profile_id="P", target_id=NUMBER,
        key="submit:atomic-0001", payload={}, total_steps=4)["operation"]
    with queue.store.connect(write=True) as db:
        db.execute("INSERT INTO sales_assistant_sessions VALUES (?,?,?,?)",
                   ("session-1", "submitting", "", inserted["id"]))
    queue.claim_next("worker-token-123456")
    assert queue.recover_interrupted() == 1
    with queue.store.connect() as db:
        assert db.execute("SELECT status FROM sales_assistant_sessions").fetchone()[0] == "submission_unknown"
        assert db.execute("SELECT status FROM web_operations").fetchone()[0] == "uncertain"
    recovered = queue.claim_sales_reconciliation("recovery-token-123456")
    assert recovered["readback_only"] is True
    with queue.store.connect() as db:
        assert db.execute("SELECT status FROM sales_assistant_sessions").fetchone()[0] == "submitting"
    queue.hold_sales_operation(inserted["id"], "recovery-token-123456",
                               status="uncertain", code="readback_unavailable")
    with queue.store.connect() as db:
        assert db.execute("SELECT status FROM sales_assistant_sessions").fetchone()[0] == "submission_unknown"
        assert db.execute("SELECT status FROM web_operations").fetchone()[0] == "uncertain"
    assert queue.claim_sales_reconciliation("retry-token-123456") is None


def test_manual_unknown_reconciliation_is_explicit_readback_only(runtime):
    queue = WebOperations(Store(runtime))
    inserted = queue.enqueue(kind="sales_order_import", actor_id="sales-1",
        role="sales", profile_id="P", target_id=NUMBER,
        key="submit:manual-0001", payload={"schema": "sales_web_import_v1",
            "order_number": NUMBER, "profile_id": "P"}, total_steps=4)["operation"]
    queue.claim_next("initial-token-123456")
    queue.hold_sales_operation(inserted["id"], "initial-token-123456",
                               status="uncertain", code="web_result_unknown")
    calls = []
    def runner(_queue, _operation, _token, _payload, *, config_path, readback_only):
        calls.append(readback_only)
        return {"status": "unknown", "code": "sales_attempt_marker_missing"}
    assert _reconcile_sales_once(queue, inserted["id"], sales_runner=runner) == inserted["id"]
    assert calls == [True]
    assert _run_one(queue, runtime / "no", runtime,
                    sales_runner=runner) == "empty"
    assert calls == [True]


def test_host_preflight_is_read_only_and_generates_disabled_review_config(sales_case, tmp_path):
    host, _, _ = sales_case
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"company": "租户甲", "username": "操作员甲",
                                       "password": "not-used"}), encoding="utf-8")
    credentials.chmod(0o600)
    sys.path.insert(0, str(SERVICE.parent / "tools"))
    from purchase_export_collector.collector import _account_scope
    url = "https://bfuned5main.wsgjp.com.cn/Main.gspx"
    scope = _account_scope("租户甲", url)
    evidence = tmp_path / "web-evidence.json"
    evidence.write_text(json.dumps({"observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"profile_id": "P", "account_scope_sha256": scope,
                  "tenant_verified": True, "operator_verified": True}}), encoding="utf-8")
    evidence.chmod(0o600)
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"token": {"profileid": "P", "employee_id": "e1"}}),
                     encoding="utf-8")
    vault.chmod(0o600)
    class FakeClient:
        calls = []
        def account(self): return "P"
        def call(self, method, params):
            self.calls.append(method)
            assert method == "erp.employee.listall"
            rows = [{"id": item_id, "fullname": name, "deleted": 0, "isstop": 0}
                    for item_id, name in (("e1", "制单员甲"), ("e2", "经手人乙"))]
            page, size = params["pageno"], params["pagesize"]
            if size == 100:
                batch = rows if page == 1 else None
            else:
                batch = rows[page - 1:page] or None
            return {"employees": batch, "totalresults": 1}
        def pages(self, method, params, collection, identity, **kwargs):
            self.calls.append(method)
            values = {"erp.btype.list": [("c1", "客户甲")],
                      "erp.employee.listall": [("e1", "制单员甲"), ("e2", "经手人乙")],
                      "erp.stock.list": [("w1", "仓库丙")]}
            return [{"id": item_id, "fullname": name, "deleted": 0, "isstop": 0}
                    for item_id, name in values[method]]
    fake = FakeClient()
    args = SimpleNamespace(credentials=credentials, account_evidence=evidence,
        collector_source=SERVICE.parent / "tools", erp_main_url=url,
        erp_vault=vault, customer="客户甲", maker="制单员甲",
        handlers=["经手人乙"], warehouse="仓库丙", template=host.template_path,
        department="销售部", runner_python=tmp_path / "python",
        browser_profile=tmp_path / "browser", chromium=tmp_path / "chromium")
    result = build_report(args, client=fake)
    assert fake.calls.count("erp.btype.list") == 1
    assert result["customer_binding"]["erp_customer_id"] == "c1"
    assert result["warehouse"]["erp_id"] == "w1"
    assert result["staff"]["制单员甲"]["reported_total"] == 1
    assert result["staff"]["制单员甲"]["catalog_count"] == 2
    assert result["staff"]["制单员甲"]["completeness_basis"] == "independent_page_sizes_with_terminal_empty"
    assert result["staff"]["制单员甲"]["snapshot_hash"] == result["staff"]["经手人乙"]["snapshot_hash"]
    assert result["staff_snapshot_hash"] == result["staff"]["制单员甲"]["snapshot_hash"]
    assert result["host_config_candidate"]["enabled"] is False
    assert result["host_config_candidate"]["acceptance_complete"] is False
    assert result["host_config_candidate"]["account_id"] == scope
    assert result["account"]["source"] == "verified_web_identity_and_private_erp_vault"
    assert result["maker_matches_erp_token_employee"] is True


def test_live_customer_preflight_runs_only_before_first_upload(sales_case, tmp_path, monkeypatch):
    host, payload, _ = sales_case
    identity = (host.profile_id, host.account_id)
    assert sales_child._customer_preflight({}, payload, host, identity,
                                           readback_only=True) is None
    vault = tmp_path / "erp.json"
    vault.write_text("{}", encoding="utf-8")
    vault.chmod(0o600)
    calls = []
    marker = object()
    def verified(client, binding, **kwargs):
        calls.append((client, binding, kwargs))
        return marker
    monkeypatch.setattr(sales_child, "verify_customer_binding", verified)
    result = sales_child._customer_preflight(
        {"erp_vault_path": str(vault)}, payload, host, identity,
        readback_only=False, client_factory=lambda path: str(path))
    assert result is marker
    assert len(calls) == 1
    assert calls[0][1] == payload["customer_binding"]
    assert calls[0][2]["web_account_identity"] == identity


class ConfirmedUploadBrowser:
    """A complete website receipt; any order lookup/editor access is a failure."""
    def __init__(self, host, *, fail_click=False):
        self.identity = host.profile_id, host.account_id
        self.clicks = 0
        self.stages = 0
        self.fail_click = fail_click

    def account_identity(self):
        return self.identity

    def stage_sales_import(self, path):
        assert path.is_file()
        self.stages += 1

    def click_upload_once(self):
        self.clicks += 1
        if self.fail_click:
            raise TimeoutError("response lost after unique upload")

    def visible_import_progress(self):
        return ("业务单据导入进度\n1,开始导入\n2,开始检查格式\n"
                "3,本次导入1行数据，开始写入数据\n4,-----完成-----\n关闭")

    def __getattr__(self, name):
        raise AssertionError("unexpected ERP lookup/editor access: " + name)


def _recovery_config(path, host):
    # Deliberately omit every credential, browser, collector and API path.
    # Receipt recovery must still work even when those services are unavailable.
    path.write_text(json.dumps({
        "enabled": False, "acceptance_complete": False,
        "profile_id": host.profile_id, "account_id": host.account_id,
        "tenant_name": host.tenant_name, "operator_name": host.operator_name,
        "template_path": str(path.parent / "missing-template.xls"),
        "template_sha256": host.template_sha256,
        "warehouse_id": host.warehouse_id, "warehouse_name": host.warehouse_name,
        "department_name": host.department_name, "maker_name": host.maker_name,
        "allowed_handlers": list(host.allowed_handlers)}), encoding="utf-8")
    path.chmod(0o600)


def test_website_confirmation_completes_without_order_readback(sales_case, runtime):
    host, payload, operation = sales_case
    browser = ConfirmedUploadBrowser(host)
    outcome = run_sales_import(payload, operation, host, runtime, browser)
    assert (outcome.status, outcome.code) == ("imported", "web_import_confirmed")
    assert outcome.completion_basis == "web_import_confirmation"
    assert outcome.erp_readback_performed is False and outcome.unit_verified is False
    assert outcome.table_verified and outcome.file_sha256
    assert outcome.imported_rows == outcome.expected_rows == 1
    assert browser.clicks == browser.stages == 1
    # No second upload and no query, even if the original browser is unusable.
    recovered = run_sales_import(payload, operation, host, runtime, None, readback_only=True)
    assert recovered == outcome and browser.clicks == 1


def test_distinct_line_delivery_dates_are_verified_in_the_workbook(sales_case, runtime):
    host, payload, operation = sales_case
    session = payload["frozen_session"]
    draft = json.loads(session["draft"])
    draft["lines"].append(dict(draft["lines"][0], delivery_date="2026-10-20"))
    freeze_draft(session, draft)
    payload["confirmed_hash"] = session["confirmed_hash"]
    payload["line_count"] = 2
    payload["line_unit_evidence"] = [USER_UNIT_EVIDENCE, USER_UNIT_EVIDENCE]
    spec = prepare_sales_spec(payload, operation, host, runtime)
    assert [line["delivery_date"] for line in spec.lines_expected] == ["2026-10-16", "2026-10-20"]
    # prepare() validates both XLS row dates against their frozen values.
    assert SalesWebImportTransport(runtime).prepare(spec).is_file()


@pytest.mark.parametrize("fail_click", [False, True])
def test_host_restart_recovers_only_local_receipt_without_login_or_api(
        sales_case, runtime, tmp_path, monkeypatch, fail_click):
    host, payload, operation = sales_case
    browser = ConfirmedUploadBrowser(host, fail_click=fail_click)
    original = run_sales_import(payload, operation, host, runtime, browser)
    assert browser.clicks == 1
    config = tmp_path / "host-private.json"
    _recovery_config(config, host)
    def prohibited(*args, **kwargs):
        raise AssertionError("receipt recovery called ERP customer lookup")
    monkeypatch.setattr(sales_child, "_customer_preflight", prohibited)
    status = runtime / "sales-web-worker-status" / "recovery.json"
    recovered = sales_child.run(config, runtime, operation, payload, True, status)
    assert recovered.erp_readback_performed is False
    if fail_click:
        assert original.status == recovered.status == "unknown"
        assert recovered.code == "web_import_requires_manual_review"
        assert not (runtime / "sales-web-import" / str(uuid.UUID(operation["id"])) / "completed.json").exists()
    else:
        assert recovered == original and recovered.status == "imported"
    assert json.loads(status.read_text())["status"] == recovered.status
    assert browser.clicks == 1


def test_host_receipt_recovery_without_marker_never_initializes_login(sales_case, runtime, tmp_path):
    host, payload, operation = sales_case
    config = tmp_path / "host-private.json"
    _recovery_config(config, host)
    status = runtime / "sales-web-worker-status" / "recovery.json"
    outcome = sales_child.run(config, runtime, operation, payload, True, status)
    assert outcome.status == "unknown" and outcome.code == "sales_attempt_marker_missing"
    assert not outcome.attempted and not outcome.erp_readback_performed
    assert not (runtime / "sales-web-import-staging").exists()


def test_parent_accepts_only_explicit_bound_website_confirmation():
    result = {"status": "imported", "code": "web_import_confirmed", "reference": NUMBER,
              "completion_basis": "web_import_confirmation", "erp_readback_performed": False,
              "unit_verified": False, "table_verified": True, "file_sha256": "a" * 64,
              "imported_rows": 1, "expected_rows": 1}
    assert _validated_sales_runner_result(result, NUMBER, 1) == result
    for changed in ({"status": "verified"}, {"code": "verified"}, {"reference": "OTHER"},
                    {"completion_basis": "erp_readback"}, {"erp_readback_performed": True},
                    {"erp_readback_performed": 0}, {"unit_verified": True}, {"unit_verified": 0},
                    {"table_verified": False}, {"file_sha256": "not-a-hash"},
                    {"imported_rows": True}, {"expected_rows": 2}):
        assert _validated_sales_runner_result(dict(result, **changed), NUMBER, 1) == {
            "status": "unknown", "code": "sales_runner_result_invalid"}
    assert _validated_sales_runner_result(result, NUMBER, 2)["status"] == "unknown"
    assert _validated_sales_runner_result({"status": "imported"}, NUMBER, 1)["status"] == "unknown"


@pytest.mark.parametrize("issue", ["", "url", "tenant", "operator_missing", "operator_duplicate", "session_conflict"])
def test_login_identity_reads_only_tenant_and_operator(sales_case, issue):
    host, _, _ = sales_case
    class Operator:
        def is_visible(self): return True
        def inner_text(self): return host.operator_name
        def evaluate(self, script): return True
    class Labels:
        def all(self):
            count = 0 if issue == "operator_missing" else 2 if issue == "operator_duplicate" else 1
            return [Operator() for _ in range(count)]
    class Body:
        def inner_text(self):
            return "您的账号已经在其他地方登录" if issue == "session_conflict" else "业务主界面"
    class Page:
        url = "https://erp.example/Main.gspx" if issue != "url" else "https://erp.example/Login.gspx"
        def title(self):
            tenant = "错误租户" if issue == "tenant" else host.tenant_name
            return f"网上管家婆 –【{tenant}】"
        def locator(self, selector):
            if selector == "span.Label.ellipsis": return Labels()
            if selector == "body": return Body()
            raise AssertionError("unexpected sales/editor access: " + selector)
    if issue:
        with pytest.raises(WebImportBlocked): sales_child._web_identity(Page(), host)
    else:
        assert sales_child._web_identity(Page(), host) == (host.profile_id, host.account_id)
