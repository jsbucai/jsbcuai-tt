#!/usr/bin/env python3
"""Build private, disabled sales-upload candidates without changing live state.

The reviewed host preflight supplies template, tenant, employee and warehouse
identities. One complete read-only customer dictionary supplies exact bindings.
The App database is opened read-only, and only the two named active sales users
are mapped. This tool never uploads, enables a worker, or changes settings.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.sales_web_customer_preflight import build_customer_bindings
from core.sales_web_import import EXPECTED_HEADERS, EXPECTED_SHEETS, _validate_template
from core.sales_web_queue import POLICY_SETTING
from core.sales_web_transport import WebImportBlocked, validate_sales_header_limits
from tools.sales_web_host_preflight import ReadOnlyErpClient


HANDLERS = ("李瑄", "刘锋")
MAKER = "罗健树"
WAREHOUSE = "江苏优螺"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FILE_CHECKS = ("duplicate_sku_rows_preserved", "tax_rounding", "summary_and_note",
               "unit_piece", "all_lines_present")
HOST_FIELDS = ("enabled", "acceptance_complete", "profile_id", "account_id",
               "tenant_name", "operator_name", "template_path", "template_sha256",
               "warehouse_id", "warehouse_name", "department_name", "maker_name",
               "allowed_handlers", "runner_python", "collector_source_path",
               "credentials_path", "erp_vault_path", "browser_profile", "chromium_path")
IDENTITY_SOURCES = {"verified_web_identity_and_private_erp_vault",
                    "verified_web_readback_and_private_erp_vault"}


class EnablementBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _block(code):
    raise EnablementBlocked(code)


def _path(path, *, regular=True, private=False):
    if not isinstance(path, (str, Path)):
        _block("source_path_invalid")
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _block("source_path_invalid")
    try:
        for node in reversed((path, *path.parents)):
            mode = node.lstat().st_mode
            if stat.S_ISLNK(mode):
                _block("source_symlink_forbidden")
        mode = path.stat().st_mode
        if regular and not stat.S_ISREG(mode):
            _block("source_not_regular")
        if private and mode & 0o077:
            _block("source_not_private")
    except OSError:
        _block("source_unavailable")
    return path


def _json(path):
    path = _path(path, private=True)
    if path.stat().st_size > 2_000_000:
        _block("source_too_large")
    raw = path.read_bytes()
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError):
        _block("source_json_invalid")
    if not isinstance(value, dict):
        _block("source_json_invalid")
    return value, hashlib.sha256(raw).hexdigest()


def _text(value, code, *, limit=200):
    if (not isinstance(value, str) or not value or value != value.strip() or
            len(value) > limit or "\r" in value or "\n" in value):
        _block(code)
    return value


def _hash(value, code):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        _block(code)
    return value


def _fresh(value, code):
    try:
        seen = datetime.fromisoformat(value)
        if seen.tzinfo is None:
            raise ValueError("timezone")
        age = datetime.now(timezone.utc) - seen.astimezone(timezone.utc)
    except (TypeError, ValueError):
        _block(code)
    if not -timedelta(minutes=5) <= age <= timedelta(hours=24):
        _block(code)


def _reference(item, *, kind, name, profile, account):
    if not isinstance(item, dict) or item.get("name") != name or \
            type(item.get("match_count")) is not int or item["match_count"] != 1:
        _block("preflight_reference_invalid")
    identity = _text(item.get("erp_id"), "preflight_reference_invalid", limit=128)
    method = "erp.employee.listall" if kind == "employee" else "erp.stock.list"
    source = {"schema": "sales_reference_exact_lookup_v1", "kind": kind,
              "method": method, "profile_id": profile, "account_id": account,
              "full_name": name, "erp_id": identity, "active": True, "match_count": 1}
    expected = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    if item.get("source_hash") != expected:
        _block("preflight_reference_hash_mismatch")
    _fresh(item.get("observed_at"), "preflight_reference_stale")
    if (type(item.get("catalog_count")) is not int or item["catalog_count"] <= 0 or
            type(item.get("reported_total")) is not int or item["reported_total"] < 0):
        _block("preflight_reference_incomplete")
    basis = ("independent_page_sizes_with_terminal_empty" if kind == "employee"
             else "advertised_total_closed")
    if item.get("completeness_basis") != basis:
        _block("preflight_reference_incomplete")
    if kind == "warehouse" and item["catalog_count"] != item["reported_total"]:
        _block("preflight_reference_incomplete")
    return identity


def _preflight(report_path):
    report, report_sha = _json(report_path)
    host, account = report.get("host_config_candidate"), report.get("account")
    if (report.get("schema") != "sales_web_host_preflight_v1" or
            not isinstance(host, dict) or not isinstance(account, dict) or
            host.get("enabled") is not False or host.get("acceptance_complete") is not False or
            host.get("trial_authorization") is not None or
            report.get("maker_matches_erp_token_employee") is not True or
            # Older preflight reports verified tenant/operator identity while
            # reading a sample. The evidence label is a compatible alias; it
            # does not enable or require order readback in the new workflow.
            account.get("source") not in IDENTITY_SOURCES):
        _block("preflight_invalid")
    if set(host) - set(HOST_FIELDS) - {"trial_authorization"}:
        _block("preflight_host_fields_invalid")
    _fresh(report.get("observed_at_utc"), "preflight_stale")
    for field in ("profile_id", "account_id", "tenant_name", "operator_name"):
        _text(host.get(field), "preflight_scope_invalid")
        if account.get(field) != host[field]:
            _block("preflight_scope_mismatch")
    profile, account_id = host["profile_id"], _hash(host["account_id"], "preflight_scope_invalid")
    if (host.get("maker_name") != MAKER or host.get("warehouse_name") != WAREHOUSE or
            not isinstance(host.get("allowed_handlers"), list) or
            len(host["allowed_handlers"]) != 2 or set(host["allowed_handlers"]) != set(HANDLERS)):
        _block("preflight_business_scope_invalid")
    _text(host.get("department_name"), "preflight_department_missing")
    staff = report.get("staff")
    if not isinstance(staff, dict) or set(staff) != {MAKER, *HANDLERS}:
        _block("preflight_staff_invalid")
    snapshot = _hash(report.get("staff_snapshot_hash"), "preflight_staff_invalid")
    staff_ids = []
    staff_counts = []
    for name in (MAKER, *HANDLERS):
        staff_ids.append(_reference(staff[name], kind="employee", name=name,
                                    profile=profile, account=account_id))
        if staff[name].get("snapshot_hash") != snapshot:
            _block("preflight_staff_snapshot_mismatch")
        staff_counts.append((staff[name]["catalog_count"], staff[name]["reported_total"]))
    if len(set(staff_ids)) != 3:
        _block("preflight_staff_identity_not_distinct")
    if len(set(staff_counts)) != 1:
        _block("preflight_staff_snapshot_mismatch")
    warehouse_id = _reference(report.get("warehouse"), kind="warehouse", name=WAREHOUSE,
                              profile=profile, account=account_id)
    if host.get("warehouse_id") != warehouse_id:
        _block("preflight_warehouse_mismatch")
    template = _path(host.get("template_path", ""))
    template_sha = _hash(host.get("template_sha256"), "preflight_template_invalid")
    if (report.get("template_sha256") != template_sha or
            hashlib.sha256(template.read_bytes()).hexdigest() != template_sha):
        _block("preflight_template_changed")
    try:
        _validate_template(template).release_resources()
    except Exception:
        _block("preflight_template_invalid")
    # Credentials and API token are read only to confirm the report's scope;
    # no secret value is copied into candidates, errors, or stdout.
    vault, _ = _json(host.get("erp_vault_path", ""))
    token = vault.get("token")
    if (not isinstance(token, dict) or str(token.get("profileid")) != profile or
            str(token.get("employee_id")) != staff_ids[0]):
        _block("preflight_maker_token_mismatch")
    credentials, _ = _json(host.get("credentials_path", ""))
    if (credentials.get("company") != host["tenant_name"] or
            credentials.get("username") != host["operator_name"]):
        _block("preflight_credentials_scope_mismatch")
    for field in ("runner_python", "collector_source_path", "browser_profile", "chromium_path"):
        value = _text(host.get(field), "preflight_host_path_invalid", limit=4096)
        if not Path(value).is_absolute() or ".." in Path(value).parts:
            _block("preflight_host_path_invalid")
    return report, {key: host[key] for key in HOST_FIELDS}, report_sha


def _app_handlers(database, profile):
    path = _path(database)
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sync_batches" in tables:
                row = db.execute("SELECT profile FROM sync_batches WHERE profile<>'' AND "
                    "published_at IS NOT NULL ORDER BY published_at DESC,rowid DESC LIMIT 1").fetchone()
            else:
                row = None
            if row:
                actual = row[0]
            else:
                profiles = {json.loads(row[0]).get("profile_id") for row in db.execute(
                    "SELECT payload FROM records WHERE kind='inventory'")}
                if len(profiles) != 1:
                    _block("app_profile_not_unique")
                actual = next(iter(profiles))
            if actual != profile:
                _block("app_profile_mismatch")
            # No password hash, session token, or customer processing data is read.
            users = [dict(row) for row in db.execute(
                "SELECT id,display_name,roles,active FROM staff_users WHERE active=1")]
    except EnablementBlocked:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _block("app_staff_source_invalid")
    result = {}
    for name in HANDLERS:
        matches = [row for row in users if row["display_name"] == name]
        if len(matches) != 1:
            _block("app_sales_person_not_unique")
        user = matches[0]
        try:
            roles = json.loads(user["roles"])
        except (TypeError, ValueError):
            _block("app_sales_role_missing")
        if not isinstance(roles, list) or "sales" not in roles:
            _block("app_sales_role_missing")
        identity = _text(user["id"], "app_sales_person_invalid", limit=128)
        result[identity] = name
    if len(result) != 2:
        _block("app_sales_person_not_unique")
    return result


def _file_acceptance(path, host):
    if path is None:
        return False, None
    evidence, evidence_sha = _json(path)
    if (evidence.get("schema") != "sales_web_file_validation_v1" or
            evidence.get("profile_id") != host["profile_id"] or
            evidence.get("account_id") != host["account_id"] or
            evidence.get("template_sha256") != host["template_sha256"] or
            evidence.get("table_verified") is not True or
            evidence.get("completion_basis") != "web_import_confirmation" or
            evidence.get("erp_upload_performed") is not False or
            evidence.get("erp_readback_performed") is not False):
        _block("file_acceptance_scope_invalid")
    checks = evidence.get("checks")
    if not isinstance(checks, dict) or any(checks.get(key) is not True for key in FILE_CHECKS):
        _block("file_acceptance_incomplete")
    sample = _path(evidence.get("validated_xls_path", ""))
    expected_sha = _hash(evidence.get("validated_xls_sha256"), "file_acceptance_hash_invalid")
    if hashlib.sha256(sample.read_bytes()).hexdigest() != expected_sha:
        _block("file_acceptance_changed")
    if type(evidence.get("expected_rows")) is not int or evidence["expected_rows"] < 2:
        _block("file_acceptance_incomplete")
    # Reopen the bound XLS, not just the operator's JSON verdict. This is a
    # representative file acceptance, not a substitute for each frozen
    # production workbook's full transport validation.
    try:
        import xlrd
        book = xlrd.open_workbook(str(sample), on_demand=True)
        try:
            sheet = book.sheet_by_index(0)
            if (tuple(book.sheet_names()) != EXPECTED_SHEETS or
                    sheet.nrows != evidence["expected_rows"] + 1 or
                    sheet.ncols != len(EXPECTED_HEADERS) or
                    tuple(sheet.row_values(0)) != EXPECTED_HEADERS):
                raise ValueError("file shape")
            skus = []
            headers = []
            for number in range(1, sheet.nrows):
                row = dict(zip(EXPECTED_HEADERS, sheet.row_values(number)))
                if (row["单据类型"] != "销售订单" or row["单位"] != "支" or
                        row["部门名称"] != host["department_name"] or
                        row["制单人名称"] != host["maker_name"] or
                        row["经手人名称"] not in host["allowed_handlers"] or
                        row["仓库名称"] != host["warehouse_name"] or
                        row["明细仓库名称"] != host["warehouse_name"]):
                    raise ValueError("file scope")
                for field in ("单据编号", "往来单位名称", "商品编号", "商品名称", "摘要", "明细备注"):
                    _text(row[field], "file_acceptance_xls_invalid", limit=2000)
                validate_sales_header_limits(row["附加说明"], row["摘要"])
                quantity, price, amount, tax = [Decimal(str(row[field])) for field in
                    ("数量", "折前单价", "折前金额", "税率(%)")]
                if (not all(value.is_finite() for value in (quantity, price, amount, tax)) or
                        quantity <= 0 or price < 0 or amount < 0 or tax != 13 or
                        price != price.quantize(Decimal("0.001")) or
                        amount != amount.quantize(Decimal("0.01")) or
                        (quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != amount):
                    raise ValueError("file amounts")
                for field in ("录单日期", "交货日期"):
                    column = EXPECTED_HEADERS.index(field)
                    if sheet.cell_type(number, column) != xlrd.XL_CELL_DATE:
                        raise ValueError("file date")
                    xlrd.xldate_as_datetime(row[field], book.datemode)
                skus.append(row["商品编号"])
                headers.append(tuple(row[field] for field in
                    ("单据编号", "往来单位名称", "经手人名称", "摘要", "附加说明")))
            if len(set(skus)) == len(skus) or len(set(headers)) != 1:
                raise ValueError("duplicate SKU acceptance")
        finally:
            book.release_resources()
    except EnablementBlocked:
        raise
    except Exception:
        _block("file_acceptance_xls_invalid")
    return True, {"path": str(Path(path)), "sha256": evidence_sha,
                  "validated_xls_sha256": expected_sha,
                  "expected_rows": evidence["expected_rows"]}


def build_candidates(report_path, staff_db, *, file_validation=None, client=None):
    """Return disabled config/policy/review candidates; perform no local writes."""
    report, host, report_sha = _preflight(report_path)
    handlers = _app_handlers(staff_db, host["profile_id"])
    accepted, acceptance = _file_acceptance(file_validation, host)
    client = client or ReadOnlyErpClient(Path(host["erp_vault_path"]))
    try:
        customers = build_customer_bindings(client, expected_profile_id=host["profile_id"],
            expected_account_id=host["account_id"],
            web_account_identity=(host["profile_id"], host["account_id"]))
    except WebImportBlocked as error:
        _block(error.code)
    if not customers["bindings"]:
        _block("customer_bindings_empty")
    host.pop("trial_authorization", None)
    host.update(enabled=False, acceptance_complete=accepted)
    policy = {"enabled": False, "profile_id": host["profile_id"],
        "account_id": host["account_id"], "department_name": host["department_name"],
        "maker_name": host["maker_name"], "handler_by_owner": handlers,
        "warehouse_id": host["warehouse_id"], "warehouse_name": host["warehouse_name"],
        "template_sha256": host["template_sha256"],
        "customer_bindings": customers["bindings"]}
    review = {"schema": "sales_web_enablement_review_v1", "status": "disabled_candidates",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preflight_report_sha256": report_sha, "profile_id": host["profile_id"],
        "account_id": host["account_id"], "handler_by_owner": handlers,
        "customer_catalog_count": customers["catalog_count"],
        "customer_binding_count": len(customers["bindings"]),
        "customer_review_count": customers["review_count"],
        "customer_review_counts": customers["review_counts"],
        "duplicate_customer_name_count": customers["duplicate_name_count"],
        "customer_observed_at": customers["observed_at"],
        "file_validation": acceptance, "acceptance_complete": accepted,
        "customer_check": "fixed_mapping_and_fresh_api_before_first_upload",
        "completion_basis": "web_import_confirmation", "erp_readback_performed": False,
        "retail_test_authorization": False, "policy_setting": POLICY_SETTING,
        "active_database_written": False, "worker_enabled": False,
        "required_installation": ["private_host_paths_and_browser_profile",
                                  "review_and_explicit_install_after_backup"] +
            ([] if accepted else ["bound_file_validation"])}
    return {"host_config": host, "policy": policy, "review": review}


def write_candidates(output_dir, candidates):
    """Create one new private directory; never replace any file or active DB."""
    directory = Path(output_dir)
    if (not directory.is_absolute() or ".." in directory.parts or
            directory.exists() or directory.is_symlink()):
        _block("output_must_be_new")
    _path(directory.parent, regular=False)
    directory.mkdir(mode=0o700)
    result = {}
    for key, name in (("host_config", "host-config.candidate.json"),
                      ("policy", "policy.candidate.json"), ("review", "review.json")):
        raw = json.dumps(candidates[key], ensure_ascii=False, sort_keys=True,
                         indent=2).encode("utf-8")
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        result[key] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--staff-db", type=Path, required=True)
    parser.add_argument("--file-validation", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        candidates = build_candidates(args.report, args.staff_db,
                                      file_validation=args.file_validation)
        paths = write_candidates(args.output_dir, candidates)
        # Counts and paths only. The private JSONs contain identities, but no
        # password/token; raw source/vendor responses are never printed.
        print(json.dumps({"status": "disabled_candidates_ready", "files": paths,
            "customer_binding_count": candidates["review"]["customer_binding_count"],
            "customer_review_count": candidates["review"]["customer_review_count"],
            "acceptance_complete": candidates["review"]["acceptance_complete"]}, ensure_ascii=False))
        return 0
    except EnablementBlocked as error:
        code = error.code
    except Exception:
        code = "sales_enablement_prepare_failed"
    print(json.dumps({"status": "blocked", "code": code}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
