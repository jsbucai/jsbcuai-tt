"""Configuration preparation is read-only; no browser or ERP document writes."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
import xlwt

from core.sales_web_import import EXPECTED_HEADERS
from tools import sales_web_enablement as tool


def private_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return path


class Client:
    def __init__(self, profile="P"):
        self.profile = profile
        self.calls = []

    def account(self):
        return self.profile

    def pages(self, method, params, collection, identity, **kwargs):
        self.calls.append(method)
        assert method == "erp.btype.list" and collection == "btypes"
        rows = [
            {"id": "c1", "fullname": "客户甲", "deleted": 0, "isstop": 0},
            {"id": "c2", "fullname": "同名客户", "deleted": 0, "isstop": 0},
            {"id": "c3", "fullname": "同名客户", "deleted": 0, "isstop": 1},
            {"id": "c4", "fullname": "停用客户", "deleted": 0, "isstop": 1},
            {"id": "c5", "fullname": "未知状态", "deleted": "?", "isstop": 0},
            {"id": "c6", "fullname": "备用零售客户", "deleted": 0, "isstop": 0},
        ]
        for row in rows:
            assert identity(row) == row["id"]
        return rows


def reference(kind, name, identity, observed, account):
    source = {"schema": "sales_reference_exact_lookup_v1", "kind": kind,
        "method": "erp.employee.listall" if kind == "employee" else "erp.stock.list",
        "profile_id": "P", "account_id": account, "full_name": name,
        "erp_id": identity, "active": True, "match_count": 1}
    return {"name": name, "erp_id": identity, "match_count": 1,
        "source_hash": sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        "observed_at": observed, "catalog_count": 3, "reported_total": 1 if kind == "employee" else 3,
        "completeness_basis": "independent_page_sizes_with_terminal_empty" if kind == "employee"
                              else "advertised_total_closed", "snapshot_hash": "b" * 64}


@pytest.fixture
def case(tmp_path):
    template = tmp_path / "original.xls"
    book = xlwt.Workbook()
    sheet = book.add_sheet("单据导入模板")
    for column, header in enumerate(EXPECTED_HEADERS):
        sheet.write(0, column, header)
    book.add_sheet("单据类型").write(0, 0, "销售订单")
    book.save(str(template))
    account = "a" * 64
    observed = datetime.now(timezone.utc).isoformat()
    credentials = private_json(tmp_path / "credentials.json", {
        "company": "公司甲", "username": "操作员甲", "password": "never-output-secret"})
    vault = private_json(tmp_path / "vault.json", {
        "token": {"profileid": "P", "employee_id": "maker", "access_token": "never-output-token"}})
    staff = {name: reference("employee", name, identity, observed, account)
             for name, identity in ((tool.MAKER, "maker"), ("李瑄", "e1"), ("刘锋", "e2"))}
    template_sha = sha256(template.read_bytes()).hexdigest()
    host = {"enabled": False, "acceptance_complete": False,
        "profile_id": "P", "account_id": account, "tenant_name": "公司甲", "operator_name": "操作员甲",
        "template_path": str(template), "template_sha256": template_sha,
        "warehouse_id": "w1", "warehouse_name": tool.WAREHOUSE,
        "department_name": "销售部", "maker_name": tool.MAKER,
        "allowed_handlers": list(tool.HANDLERS), "runner_python": str(tmp_path / "python"),
        "collector_source_path": str(tmp_path / "collector"),
        "credentials_path": str(credentials), "erp_vault_path": str(vault),
        "browser_profile": str(tmp_path / "new-browser"), "chromium_path": "/usr/bin/chromium"}
    report = {"schema": "sales_web_host_preflight_v1", "observed_at_utc": observed,
        "account": {key: host[key] for key in ("profile_id", "account_id", "tenant_name", "operator_name")},
        "host_config_candidate": host, "staff": staff, "staff_snapshot_hash": "b" * 64,
        "maker_matches_erp_token_employee": True, "template_sha256": template_sha,
        "warehouse": reference("warehouse", tool.WAREHOUSE, "w1", observed, account)}
    report["account"]["source"] = "verified_web_identity_and_private_erp_vault"
    report_path = private_json(tmp_path / "preflight.json", report)
    database = tmp_path / "app.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE staff_users(id TEXT,display_name TEXT,roles TEXT,active INTEGER);"
                         "CREATE TABLE records(kind TEXT,payload TEXT);")
        db.executemany("INSERT INTO staff_users VALUES (?,?,?,?)", [
            ("sales-one", "李瑄", '["sales"]', 1),
            ("sales-two", "刘锋", '["sales","purchase"]', 1),
            ("old-two", "刘锋", '["sales"]', 0),
            ("buyer", "采购员", '["purchase"]', 1)])
        db.execute("INSERT INTO records VALUES ('inventory',?)", (json.dumps({"profile_id": "P"}),))
    return SimpleNamespace(root=tmp_path, report=report, path=report_path, db=database,
                           template=template, host=host)


def save(case):
    private_json(case.path, case.report)


def sample_evidence(case):
    sample = case.root / "review.xls"
    book = xlwt.Workbook()
    sheet = book.add_sheet("单据导入模板")
    for column, header in enumerate(EXPECTED_HEADERS):
        sheet.write(0, column, header)
    date_style = xlwt.easyxf(num_format_str="yyyy-mm-dd")
    for number, quantity in ((1, 1), (2, 2)):
        row = {"单据类型": "销售订单", "单据编号": "ULFILETEST", "往来单位名称": "备用零售客户",
            "制单人名称": tool.MAKER, "经手人名称": "李瑄", "部门名称": "销售部",
            "仓库名称": tool.WAREHOUSE, "明细仓库名称": tool.WAREHOUSE, "单位": "支",
            "摘要": "PO#项目#1#图纸#螺钉#M6X30#8.8#锌#1#LS1#交期=2026-10-15",
            "附加说明": "仅表格验收", "商品编号": "LS1", "商品名称": "螺钉",
            "明细备注": "验收行" + str(number), "录单日期": datetime(2026, 10, 7),
            "交货日期": datetime(2026, 10, 15), "数量": quantity,
            "折前单价": 1.0, "折前金额": quantity, "税率(%)": 13}
        for column, header in enumerate(EXPECTED_HEADERS):
            sheet.write(number, column, row.get(header, ""), date_style if
                        header in ("录单日期", "交货日期") else xlwt.Style.default_style)
    book.add_sheet("单据类型").write(0, 0, "销售订单")
    book.save(str(sample))
    value = {"schema": "sales_web_file_validation_v1", "profile_id": "P",
        "account_id": case.host["account_id"], "template_sha256": case.host["template_sha256"],
        "table_verified": True, "completion_basis": "web_import_confirmation",
        "erp_upload_performed": False, "erp_readback_performed": False,
        "validated_xls_path": str(sample), "validated_xls_sha256": sha256(sample.read_bytes()).hexdigest(),
        "expected_rows": 2, "checks": {key: True for key in tool.FILE_CHECKS}}
    return private_json(case.root / "file-validation.json", value), value


def test_prepares_two_enabled_exact_sales_users_and_complete_customer_list_without_writes(case):
    before = sha256(case.db.read_bytes()).hexdigest()
    client = Client()
    result = tool.build_candidates(case.path, case.db, client=client)
    assert client.calls == ["erp.btype.list"]
    assert sha256(case.db.read_bytes()).hexdigest() == before
    assert result["policy"]["handler_by_owner"] == {"sales-one": "李瑄", "sales-two": "刘锋"}
    assert set(result["policy"]["customer_bindings"]) == {"客户甲", "备用零售客户"}
    assert result["review"]["customer_review_count"] == 4
    assert result["review"]["customer_review_counts"] == {
        "invalid_name": 0, "duplicate_name": 2, "inactive": 1, "unknown_status": 1}
    assert result["review"]["duplicate_customer_name_count"] == 1
    assert result["host_config"]["enabled"] is False
    assert result["host_config"]["acceptance_complete"] is False
    assert result["policy"]["enabled"] is False
    assert "retail_test_session_id" not in result["policy"]
    assert "trial_authorization" not in result["host_config"]
    assert "never-output" not in json.dumps(result)


def test_explicit_bound_file_acceptance_still_never_enables(case):
    evidence, _ = sample_evidence(case)
    result = tool.build_candidates(case.path, case.db, file_validation=evidence, client=Client())
    assert result["host_config"]["acceptance_complete"] is True
    assert result["host_config"]["enabled"] is False
    assert result["policy"]["enabled"] is False
    assert result["review"]["erp_readback_performed"] is False
    assert result["review"]["file_validation"]["expected_rows"] == 2


def test_legacy_real_report_identity_source_keeps_all_scope_checks_and_original(case):
    case.report["account"]["source"] = "verified_web_readback_and_private_erp_vault"
    # Older generator has these report-level fields too; they must not grant
    # a testing-customer exception or request any ERP readback in candidates.
    case.report["customer_binding"] = {"customer_name": "备用零售客户"}
    case.report["prior_dictionary_probe"] = None
    save(case)
    original = case.path.read_bytes()
    evidence, _ = sample_evidence(case)
    client = Client()
    result = tool.build_candidates(case.path, case.db, file_validation=evidence, client=client)
    assert case.path.read_bytes() == original
    assert client.calls == ["erp.btype.list"]
    assert result["host_config"]["acceptance_complete"] is True
    assert result["review"]["erp_readback_performed"] is False
    assert result["host_config"]["enabled"] is False
    assert "retail_test_session_id" not in result["policy"]
    case.report["account"]["profile_id"] = "OTHER"
    save(case)
    with pytest.raises(tool.EnablementBlocked, match="preflight_scope_mismatch"):
        tool.build_candidates(case.path, case.db, file_validation=evidence, client=client)


@pytest.mark.parametrize("mutation,code", [
    (lambda r: r["account"].update(profile_id="other"), "preflight_scope_mismatch"),
    (lambda r: r["account"].update(source="unverified_web_identity"), "preflight_invalid"),
    (lambda r: r["host_config_candidate"].update(account_id="unknown"), "preflight_scope_mismatch"),
    (lambda r: r["host_config_candidate"].update(enabled=True), "preflight_invalid"),
    (lambda r: r["host_config_candidate"].update(trial_authorization={}), "preflight_invalid"),
    (lambda r: r["host_config_candidate"].update(password="secret"), "preflight_host_fields_invalid"),
    (lambda r: r["host_config_candidate"].update(maker_name="wrong"), "preflight_business_scope_invalid"),
    (lambda r: r["host_config_candidate"].update(warehouse_id="wrong"), "preflight_warehouse_mismatch"),
    (lambda r: r["staff"]["刘锋"].update(source_hash="0" * 64), "preflight_reference_hash_mismatch"),
    (lambda r: r["staff"]["李瑄"].update(snapshot_hash="0" * 64), "preflight_staff_snapshot_mismatch"),
    (lambda r: r["staff"]["李瑄"].update(catalog_count=4), "preflight_staff_snapshot_mismatch"),
    (lambda r: r["warehouse"].update(reported_total=1), "preflight_reference_incomplete"),
    (lambda r: r.update(observed_at_utc=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()), "preflight_stale"),
])
def test_rejects_misbound_or_unaccepted_report_before_customer_api(case, mutation, code):
    mutation(case.report)
    save(case)
    client = Client()
    with pytest.raises(tool.EnablementBlocked, match=code):
        tool.build_candidates(case.path, case.db, client=client)
    assert client.calls == []


@pytest.mark.parametrize("sql,code", [
    ("UPDATE staff_users SET display_name='刘峰' WHERE id='sales-two'", "app_sales_person_not_unique"),
    ("UPDATE staff_users SET active=0 WHERE id='sales-one'", "app_sales_person_not_unique"),
    ("UPDATE staff_users SET roles='[\"purchase\"]' WHERE id='sales-one'", "app_sales_role_missing"),
    ("UPDATE staff_users SET active=1 WHERE id='old-two'", "app_sales_person_not_unique"),
    ("UPDATE records SET payload='{\"profile_id\":\"OTHER\"}'", "app_profile_mismatch"),
])
def test_staff_ambiguity_role_or_profile_requires_review(case, sql, code):
    with sqlite3.connect(case.db) as db:
        db.execute(sql)
    with pytest.raises(tool.EnablementBlocked, match=code):
        tool.build_candidates(case.path, case.db, client=Client())


def test_customer_api_profile_mismatch_never_outputs_candidate(case):
    with pytest.raises(tool.EnablementBlocked, match="sales_customer_profile_mismatch"):
        tool.build_candidates(case.path, case.db, client=Client("OTHER"))


@pytest.mark.parametrize("which,patch,code", [
    ("erp_vault_path", {"token": {"profileid": "OTHER", "employee_id": "maker"}}, "preflight_maker_token_mismatch"),
    ("erp_vault_path", {"token": {"profileid": "P", "employee_id": "e1"}}, "preflight_maker_token_mismatch"),
    ("credentials_path", {"company": "OTHER", "username": "操作员甲"}, "preflight_credentials_scope_mismatch"),
])
def test_current_private_credentials_and_maker_must_still_match_report(case, which, patch, code):
    private_json(Path(case.host[which]), patch)
    client = Client()
    with pytest.raises(tool.EnablementBlocked, match=code):
        tool.build_candidates(case.path, case.db, client=client)
    assert client.calls == []


@pytest.mark.parametrize("patch,code", [
    ({"profile_id": "OTHER"}, "file_acceptance_scope_invalid"),
    ({"template_sha256": "0" * 64}, "file_acceptance_scope_invalid"),
    ({"erp_readback_performed": True}, "file_acceptance_scope_invalid"),
    ({"expected_rows": 3}, "file_acceptance_xls_invalid"),
    ({"checks": {}}, "file_acceptance_incomplete"),
])
def test_file_acceptance_is_scope_and_real_xls_bound(case, patch, code):
    path, value = sample_evidence(case)
    value.update(patch)
    private_json(path, value)
    with pytest.raises(tool.EnablementBlocked, match=code):
        tool.build_candidates(case.path, case.db, file_validation=path, client=Client())


def test_changed_template_or_sample_is_rejected(case):
    path, value = sample_evidence(case)
    sample = Path(value["validated_xls_path"])
    sample.write_bytes(sample.read_bytes() + b"changed")
    with pytest.raises(tool.EnablementBlocked, match="file_acceptance_changed"):
        tool.build_candidates(case.path, case.db, file_validation=path, client=Client())
    case.template.write_bytes(case.template.read_bytes() + b"changed")
    with pytest.raises(tool.EnablementBlocked, match="preflight_template_changed"):
        tool.build_candidates(case.path, case.db, client=Client())


def test_file_acceptance_json_cannot_bless_non_workbook_even_when_hash_matches(case):
    path, value = sample_evidence(case)
    sample = Path(value["validated_xls_path"])
    sample.write_bytes(b"not an XLS workbook")
    value["validated_xls_sha256"] = sha256(sample.read_bytes()).hexdigest()
    private_json(path, value)
    with pytest.raises(tool.EnablementBlocked, match="file_acceptance_xls_invalid"):
        tool.build_candidates(case.path, case.db, file_validation=path, client=Client())


def test_duplicate_json_fields_are_not_silently_selected(case):
    case.path.write_text('{"schema":"wrong","schema":"sales_web_host_preflight_v1"}')
    with pytest.raises(tool.EnablementBlocked, match="source_json_invalid"):
        tool.build_candidates(case.path, case.db, client=Client())


def test_private_json_symlink_and_parent_link_are_rejected(case):
    case.path.chmod(0o644)
    with pytest.raises(tool.EnablementBlocked, match="source_not_private"):
        tool.build_candidates(case.path, case.db, client=Client())
    case.path.chmod(0o600)
    linked = case.root / "linked.json"
    linked.symlink_to(case.path)
    with pytest.raises(tool.EnablementBlocked, match="source_symlink_forbidden"):
        tool.build_candidates(linked, case.db, client=Client())
    linked_parent = case.root / "linked-dir"
    linked_parent.symlink_to(case.root, target_is_directory=True)
    with pytest.raises(tool.EnablementBlocked, match="source_symlink_forbidden"):
        tool.build_candidates(linked_parent / case.path.name, case.db, client=Client())


def test_writes_only_new_private_candidate_directory_and_never_overwrites(case):
    candidates = tool.build_candidates(case.path, case.db, client=Client())
    output = case.root / "new-candidate"
    files = tool.write_candidates(output, candidates)
    assert output.stat().st_mode & 0o777 == 0o700
    for key, info in files.items():
        path = Path(info["path"])
        assert path.stat().st_mode & 0o777 == 0o600
        assert sha256(path.read_bytes()).hexdigest() == info["sha256"]
        assert json.loads(path.read_text()) == candidates[key]
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(tool.EnablementBlocked, match="output_must_be_new"):
        tool.write_candidates(output, candidates)
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
