#!/usr/bin/env python3
"""Read-only N150 sales-worker configuration and exact ERP dictionary preflight.

Run as ulcollector after a verified ERP web-login identity check. Writes only a
private local review report; it never enables the sales writer, uploads a
workbook, or prints credentials and raw vendor responses.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.erp_api import ErpClient, ErpUnavailable
from core.sales_web_customer_preflight import (build_customer_binding,
                                                lookup_exact_reference,
                                                lookup_exact_references)
from core.sales_web_import import _validate_template


class ReadOnlyErpClient(ErpClient):
    """Do not mutate even the local API vault during a review-only probe."""

    def _refresh(self, record):
        raise ErpUnavailable("只读预检需要事先有效的 ERP 授权", "readonly_token_refresh_required")


def _private_file(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("private_source_unavailable")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("private_source_invalid")
    return data


def build_report(args, client=None) -> dict:
    credentials = _private_file(args.credentials)
    company = credentials.get("company")
    operator = credentials.get("username")
    if not isinstance(company, str) or not company or not isinstance(operator, str) or not operator:
        raise ValueError("web_login_identity_missing")
    evidence = _private_file(args.account_evidence)
    observed = datetime.fromisoformat(evidence["observed_at_utc"])
    if (observed.tzinfo is None or not timedelta(0) <=
            datetime.now(timezone.utc) - observed.astimezone(timezone.utc) <= timedelta(hours=24)):
        raise ValueError("web_login_evidence_stale")
    if (evidence.get("scope", {}).get("tenant_verified") is not True or
            evidence.get("scope", {}).get("operator_verified") is not True):
        raise ValueError("web_login_evidence_incomplete")
    collector_source = Path(args.collector_source)
    if not collector_source.is_absolute() or collector_source.is_symlink() or not collector_source.is_dir():
        raise ValueError("collector_source_unavailable")
    sys.path.insert(0, str(collector_source))
    from purchase_export_collector.collector import _account_scope
    account_id = _account_scope(company, args.erp_main_url)
    if account_id != evidence["scope"].get("account_scope_sha256"):
        raise ValueError("web_account_scope_changed")
    client = client or ReadOnlyErpClient(args.erp_vault)
    profile_id = client.account()
    if profile_id != evidence["scope"].get("profile_id"):
        raise ValueError("web_api_profile_mismatch")
    vault = _private_file(args.erp_vault)
    token = vault.get("token", {})
    if (not isinstance(token, dict) or str(token.get("profileid")) != profile_id or
            not str(token.get("employee_id") or "").strip()):
        raise ValueError("erp_maker_token_identity_missing")
    customer = build_customer_binding(client, args.customer,
        expected_profile_id=profile_id, expected_account_id=account_id,
        web_account_identity=(profile_id, account_id))
    def reference(kind, name):
        item = lookup_exact_reference(client, kind=kind, full_name=name,
            expected_profile_id=profile_id, expected_account_id=account_id,
            web_account_identity=(profile_id, account_id))
        return {"name": name, "erp_id": item.erp_id, "match_count": 1,
                "source_hash": item.source_hash, "observed_at": item.observed_at,
                "catalog_count": item.catalog_count,
                "reported_total": item.reported_total,
                "completeness_basis": item.completeness_basis}
    names = (args.maker, *args.handlers)
    if len(set(names)) != len(names):
        raise ValueError("employee_names_not_distinct")
    employees = lookup_exact_references(client, kind="employee", full_names=names,
        expected_profile_id=profile_id, expected_account_id=account_id,
        web_account_identity=(profile_id, account_id))
    staff = [{"name": name, "erp_id": item.erp_id, "match_count": 1,
              "source_hash": item.source_hash, "observed_at": item.observed_at,
              "catalog_count": item.catalog_count,
              "reported_total": item.reported_total,
              "completeness_basis": item.completeness_basis,
              "snapshot_hash": item.snapshot_hash}
             for name, item in employees.items()]
    if staff[0]["erp_id"] != str(token["employee_id"]):
        raise ValueError("erp_maker_token_identity_mismatch")
    warehouse = reference("warehouse", args.warehouse)
    template = Path(args.template)
    if template.is_symlink() or not template.is_file() or not os.access(template, os.R_OK):
        raise ValueError("original_template_unreadable")
    _validate_template(template)
    template_sha = hashlib.sha256(template.read_bytes()).hexdigest()
    source = getattr(args, "dictionary_evidence", None)
    if source:
        source = Path(source)
        _private_file(source)
        dictionary_evidence = {"path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    else:
        dictionary_evidence = None
    return {"schema": "sales_web_host_preflight_v1",
            "observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "account": {"profile_id": profile_id, "account_id": account_id,
                        "tenant_name": company, "operator_name": operator,
                        "source": "verified_web_identity_and_private_erp_vault"},
            "customer_binding": customer,
            "staff": {item["name"]: item for item in staff},
            "staff_snapshot_hash": staff[0]["snapshot_hash"],
            "maker_matches_erp_token_employee": True,
            "warehouse": warehouse,
            "prior_dictionary_probe": dictionary_evidence,
            "template_sha256": template_sha,
            "host_config_candidate": {
                "enabled": False, "acceptance_complete": False,
                "profile_id": profile_id, "account_id": account_id,
                "tenant_name": company, "operator_name": operator,
                "template_path": str(template), "template_sha256": template_sha,
                "warehouse_id": warehouse["erp_id"], "warehouse_name": args.warehouse,
                "department_name": args.department, "maker_name": args.maker,
                "allowed_handlers": list(args.handlers),
                "runner_python": str(args.runner_python),
                "collector_source_path": str(collector_source),
                "credentials_path": str(args.credentials),
                "erp_vault_path": str(args.erp_vault),
                "browser_profile": str(args.browser_profile),
                "chromium_path": str(args.chromium)}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("erp-vault", "credentials", "account-evidence", "erp-main-url",
                 "collector-source", "template", "customer", "maker", "warehouse",
                 "department", "runner-python", "browser-profile", "output"):
        parser.add_argument("--" + name, required=True,
                            type=Path if name in {"erp-vault", "credentials", "account-evidence",
                                                   "collector-source", "template", "runner-python",
                                                   "browser-profile", "output"} else str)
    parser.add_argument("--handler", action="append", dest="handlers", required=True)
    parser.add_argument("--dictionary-evidence", type=Path)
    parser.add_argument("--chromium", type=Path, default=Path("/usr/bin/chromium"))
    args = parser.parse_args(argv)
    try:
        report = build_report(args)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if output.is_symlink() or output.exists():
            raise ValueError("output_must_be_new")
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        raw = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode()
        with os.fdopen(descriptor, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        print(json.dumps({"status": "review_ready", "output": str(output),
                          "sha256": hashlib.sha256(raw).hexdigest()}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "blocked", "code": str(error)[:100]},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
