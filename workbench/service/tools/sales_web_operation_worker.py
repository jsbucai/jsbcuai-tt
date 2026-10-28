#!/usr/bin/env python3
"""Host-only sales import subprocess, invoked inside the shared browser lane.

Private credentials, the accepted XLS template, Chromium and collector login
remain on N150. Frozen business parameters arrive through stdin; neither
credentials nor customer data are placed in command-line arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.sales_web_operation import (SalesHostScope, _binding, run_sales_import)
from core.sales_web_customer_preflight import verify_customer_binding
from core.erp_api import ErpClient
from core.sales_web_transport import (PlaywrightSalesWebBrowser,
                                      PlaywrightUploadSelectors, WebImportBlocked,
                                      WebImportOutcome)


def _private_config(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise WebImportBlocked("sales_host_config_not_private")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WebImportBlocked("sales_host_config_invalid") from None
    if not isinstance(result, dict):
        raise WebImportBlocked("sales_host_config_invalid")
    return result


def _host_scope(config: dict) -> SalesHostScope:
    try:
        if type(config["enabled"]) is not bool or type(config["acceptance_complete"]) is not bool:
            raise WebImportBlocked("sales_host_config_invalid")
        return SalesHostScope(
            config["enabled"], config["acceptance_complete"],
            config["profile_id"], config["account_id"], config["tenant_name"],
            config["operator_name"], Path(config["template_path"]),
            config["template_sha256"], config["warehouse_id"],
            config["warehouse_name"], config["department_name"],
            config["maker_name"], tuple(config["allowed_handlers"]),
            config.get("trial_authorization"))
    except (KeyError, TypeError, ValueError):
        raise WebImportBlocked("sales_host_config_invalid") from None


def _status(path: Path, status: str, code: str = "") -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump({"status": status, "code": code}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _attempt_exists(runtime: Path, operation: dict) -> bool:
    try:
        intent_id = str(uuid.UUID(operation["id"]))
    except (KeyError, ValueError, TypeError):
        return False
    return (runtime / "sales-web-import" / intent_id / "attempt.json").is_file()


def _customer_preflight(config: dict, payload: dict, host: SalesHostScope,
                        web_identity: tuple[str, str], *, readback_only: bool,
                        client_factory=ErpClient):
    """Fresh read-only ERP customer ID check immediately before first upload."""
    if readback_only:
        return None
    try:
        vault = Path(config["erp_vault_path"])
    except (KeyError, TypeError, ValueError):
        raise WebImportBlocked("sales_erp_vault_unavailable") from None
    if vault.is_symlink() or not vault.is_file() or vault.stat().st_mode & 0o077:
        raise WebImportBlocked("sales_erp_vault_not_private")
    return verify_customer_binding(client_factory(vault), payload["customer_binding"],
        expected_profile_id=host.profile_id, expected_account_id=host.account_id,
        web_account_identity=web_identity)


def _web_identity(page, host: SalesHostScope) -> tuple[str, str]:
    """Read only the logged-in tenant and staff labels; no sales editor."""
    url = page.url.lower().split("?", 1)[0]
    if not url.startswith("https://") or not url.endswith("/main.gspx"):
        raise WebImportBlocked("erp_main_page_missing")
    if page.title().strip() != f"网上管家婆 –【{host.tenant_name}】":
        raise WebImportBlocked("erp_tenant_mismatch")
    on_screen = ("e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0"
                 "&&r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}")
    operators = [item for item in page.locator("span.Label.ellipsis").all()
                 if item.is_visible() and item.inner_text().strip() == host.operator_name
                 and item.evaluate(on_screen)]
    if len(operators) != 1:
        raise WebImportBlocked("erp_operator_mismatch")
    if "您的账号已经在其他地方登录" in page.locator("body").inner_text():
        raise WebImportBlocked("erp_session_conflict")
    return host.profile_id, host.account_id


def run(config_path: Path, runtime: Path, operation: dict, payload: dict,
        readback_only: bool, status_path: Path) -> WebImportOutcome:
    config = _private_config(config_path)
    host = _host_scope(config)
    _binding(payload, operation, host, readback_only=readback_only)
    if (status_path.is_symlink() or
            not status_path.resolve().is_relative_to((runtime / "sales-web-worker-status").resolve())):
        raise WebImportBlocked("sales_status_path_invalid")
    # Recovery is local only: do not read credentials, load Playwright, log in,
    # or query customer/order data. A lost response without a durable receipt
    # remains unknown and needs manual review, never another upload.
    if readback_only:
        _status(status_path, "recovering_receipt")
        if not _attempt_exists(runtime, operation):
            outcome = WebImportOutcome(str(uuid.UUID(operation["id"])), operation["target_id"],
                "unknown", "sales_attempt_marker_missing", False, False, False)
        else:
            outcome = run_sales_import(payload, operation, host, runtime, None,
                                       readback_only=True)
        _status(status_path, outcome.status, outcome.code)
        return outcome
    _status(status_path, "logging_in")
    collector_root = Path(config["collector_source_path"])
    if not collector_root.is_absolute() or collector_root.is_symlink() or not collector_root.is_dir():
        raise WebImportBlocked("collector_source_unavailable")
    sys.path.insert(0, str(collector_root))
    from purchase_export_collector.collector import _account_scope, _login, _read_credentials
    from playwright.sync_api import sync_playwright

    credentials_path = Path(config["credentials_path"])
    if credentials_path.is_symlink() or credentials_path.stat().st_mode & 0o077:
        raise WebImportBlocked("sales_credentials_not_private")
    credentials = _read_credentials(credentials_path)
    if (credentials["company"], credentials["username"]) != (host.tenant_name, host.operator_name):
        raise WebImportBlocked("sales_login_identity_mismatch")
    browser_profile = Path(config["browser_profile"])
    if not browser_profile.is_absolute() or browser_profile.is_symlink():
        raise WebImportBlocked("sales_browser_profile_invalid")
    browser_profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    chromium = Path(config["chromium_path"])
    if not chromium.is_file() or chromium.is_symlink():
        raise WebImportBlocked("sales_chromium_unavailable")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(browser_profile), headless=False, executable_path=str(chromium),
            viewport={"width": 1920, "height": 930})
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            page = _login(page, credentials, wait_for_human=True,
                          status_paths=[status_path])
            account_id = _account_scope(credentials["company"], page.url)
            if account_id != host.account_id:
                raise WebImportBlocked("sales_account_identity_mismatch")
            identity_reader = lambda: _web_identity(page, host)
            _customer_preflight(config, payload, host, identity_reader(),
                                readback_only=False)
            browser = PlaywrightSalesWebBrowser(
                page, PlaywrightUploadSelectors(), identity=identity_reader)
            _status(status_path, "running")
            outcome = run_sales_import(payload, operation, host, runtime, browser,
                                       readback_only=readback_only)
            _status(status_path, outcome.status, outcome.code)
            return outcome
        finally:
            context.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--status-path", type=Path, required=True)
    parser.add_argument("--readback-only", action="store_true",
                        help="compatibility flag: recover local receipt only; no ERP query or upload")
    args = parser.parse_args(argv)
    try:
        frozen = json.load(sys.stdin)
        operation, payload = frozen["operation"], frozen["payload"]
        token = frozen["worker_token"]
        outcome = run(args.config, args.runtime, operation, payload,
                      args.readback_only, args.status_path)
        blocked_code = None
    except WebImportBlocked as error:
        attempted = (_attempt_exists(args.runtime, frozen.get("operation", {}))
                     if isinstance(locals().get("frozen"), dict) else False)
        blocked_code = None if attempted or args.readback_only else error.code
        if blocked_code is None:
            outcome = WebImportOutcome(str(uuid.UUID(operation["id"])),
                operation["target_id"], "unknown", error.code, attempted, False, False)
        else:
            outcome = None
    except Exception:
        # A page error after the durable marker may mean the ERP accepted the
        # only click. The parent holds the job for manual review, never retry.
        blocked_code = None
        outcome = WebImportOutcome(str(uuid.UUID(operation["id"])),
            operation["target_id"], "unknown", "sales_runner_unavailable",
            _attempt_exists(args.runtime, operation), False, False)
    from core.sales_assistant import SalesAssistant
    from core.store import Store
    result = SalesAssistant(Store(args.runtime)).finish_web_import(
        operation["id"], token, outcome=outcome, blocked_code=blocked_code)
    public_status = {"succeeded": "imported", "uncertain": "unknown"}.get(
        result["status"], result["status"])
    print(json.dumps({"status": public_status,
                      "code": result["result"].get("code", ""),
                      "reference": result["result"].get("reference", ""),
                      "unit_verified": result["result"].get("unit_verified") is True,
                      "completion_basis": result["result"].get("completion_basis", ""),
                      "erp_readback_performed": result["result"].get("erp_readback_performed"),
                      "table_verified": result["result"].get("table_verified"),
                      "file_sha256": result["result"].get("file_sha256", ""),
                      "imported_rows": result["result"].get("imported_rows", 0),
                      "expected_rows": result["result"].get("expected_rows", 0),
                      "document_numbers": result['result'].get('document_numbers', []),
                      "document_count": result['result'].get('document_count', 0)},
                     ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
