#!/usr/bin/env python3
"""Isolated, read-only N150 acceptance of an existing ERP sales order.

Run as ulcollector with the purchase collector's checked password-login code.
The only ERP interactions are opening the sales list/editor and searching an
existing document. No import, save, audit, or other business write is present.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from purchase_export_collector.collector import (
    CollectorError, _account_scope, _login, _read_credentials, _ready_erp_page,
)
from core.sales_web_readback import (ALL_FILTER, AUDIT_FILTERS, FILTER_FOR_STATUS,
                                     SalesReadbackScope, WisejSalesReadback)
from core.unit_policy import CONFIRMED_BASE_UNIT, web_readback_piece_basis


ORDER = "UL26100713370454EE75"
TENANT = "江苏优螺工业科技有限公司"
OPERATOR = "罗炳升"
PRODUCT = "LS3073"


def _private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _scope_from_private_sources(args, credentials: dict) -> tuple[str, str]:
    if (credentials["company"], credentials["username"]) != (TENANT, OPERATOR):
        raise RuntimeError("verified_login_identity_mismatch")
    # Read the existing ERP token without invoking refresh or displaying it.
    if args.erp_vault.is_symlink() or args.erp_vault.stat().st_mode & 0o077:
        raise RuntimeError("erp_vault_not_private")
    record = json.loads(args.erp_vault.read_text(encoding="utf-8-sig"))
    if record["credentials"]["appkey"] != record["token"]["appkey"]:
        raise RuntimeError("erp_vault_account_mismatch")
    profile_id = str(record["token"]["profileid"])
    if not profile_id:
        raise RuntimeError("erp_profile_id_missing")
    db = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    try:
        matches = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM records WHERE kind='inventory' AND sku=?", (PRODUCT,))]
    finally:
        db.close()
    scoped = [item for item in matches
              if str(item.get("profile_id")) == profile_id and item.get("sku") == PRODUCT]
    if not scoped:
        raise RuntimeError("published_inventory_profile_mismatch")
    if any(str(item.get("base_unit")) != "1" for item in scoped):
        raise RuntimeError("published_inventory_base_unit_mismatch")
    return profile_id, "1"


def _unit_diagnostic(reader: WisejSalesReadback, row: dict, screenshot: Path,
                     detail: dict, base_unit_id: str) -> dict:
    """Inspect only the existing editor row, then close its tab without saving."""
    lines = detail.get("lines")
    if not isinstance(lines, (tuple, list)) or not lines:
        raise RuntimeError("unit_detail_lines_missing")
    bases = []
    for line in lines:
        if not isinstance(line, dict) or "unit_raw" not in line:
            raise RuntimeError("unit_readback_unavailable")
        try:
            bases.append(web_readback_piece_basis(line["unit_raw"], line.get("unit_source"),
                                                   base_unit_id, line.get("unit")))
        except ValueError as error:
            raise RuntimeError(str(error)) from None
    number = row["number"]
    audit = FILTER_FOR_STATUS.get(row["audit_status"])
    if not audit:
        return {"verified": False, "reason": "audit_status_not_editable"}
    _, refreshed = reader._query_status(number, audit)
    if len(refreshed) != 1 or refreshed[0] != row:
        return {"verified": False, "reason": "sales_row_changed"}
    matching = [item for item in reader._main_rows()
                if reader._cells(item, {"number": 9})[9] == number]
    if len(matching) != 1:
        return {"verified": False, "reason": "sales_row_ambiguous"}
    matching[0].locator('a[_buttonvalue="modify"]').click()
    reader.page.wait_for_timeout(reader.settle_ms)
    try:
        if reader._one('input[id$="$edNumber"]:visible').input_value() != number:
            raise RuntimeError("editor_number_mismatch")
        data = reader.page.evaluate("""() => {
          const rows=[...document.querySelectorAll('tr.GridBodyRow')]
            .filter(r=>r.getBoundingClientRect().width>0 &&
                       r.querySelector('td[_columnindex="46"]') &&
                       (r.querySelector('td[_columnindex="1"]')?.innerText||'').trim()==='LS3073');
          return rows.map(r=>[...r.querySelectorAll('td[_columnindex]')].map(c=>({
            index:Number(c.getAttribute('_columnindex')),
            text:(c.innerText||'').trim(),
            title:c.getAttribute('title')||'',
            input:[...c.querySelectorAll('input')].map(x=>x.value||'').filter(Boolean)
          })));
        }""")
        screenshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        reader.page.screenshot(path=str(screenshot), full_page=True)
        screenshot.chmod(0o600)
        if len(data) != 1:
            return {"verified": False, "reason": "editor_product_row_ambiguous",
                    "row_count": len(data)}
        cells = data[0]
        explicit = [cell for cell in cells if cell["text"] == "支" or "支" in cell["input"]]
        notes = [cell["index"] for cell in cells if "#LS3073" in cell["text"]]
        if explicit and any(line.get("unit_source") == "column_absent" for line in lines):
            raise RuntimeError("erp_unit_schema_changed")
        unit_basis = (bases[0] if len(set(bases)) == 1 else
                      "mixed_user_blank_and_erp_explicit_piece")
        # Retain only the sample row's nonempty cells as private diagnostic
        # evidence. Blank/absent unit is accepted solely under the reviewed
        # base-unit-1 identity and the user's explicit piece-unit rule.
        return {"verified": True, "normalized_unit": CONFIRMED_BASE_UNIT,
                "unit_basis": unit_basis, "base_unit_id": base_unit_id,
                "explicit_piece_label_visible": bool(explicit),
                "explicit_piece_columns": [cell["index"] for cell in explicit],
                "detail_note_columns": notes,
                "cells": [cell for cell in cells if cell["text"] or cell["title"] or cell["input"]]}
    finally:
        reader._close_editor()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--erp-vault", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--lane-lock", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, default=Path("/usr/bin/chromium"))
    args = parser.parse_args()
    os.umask(0o077)
    credentials = _read_credentials(args.credentials)
    profile_id, base_unit_id = _scope_from_private_sources(args, credentials)
    args.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.lane_lock.open("a+") as lane:
        try:
            fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _private_json(args.status, {"status": "blocked", "reason": "browser_lane_busy"})
            return 2
        _private_json(args.status, {"status": "logging_in", "started_at":
                      datetime.now(timezone.utc).isoformat()})
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(args.profile), headless=False, executable_path=str(args.chromium),
                viewport={"width": 1920, "height": 930})
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15000)
                try:
                    page = _login(page, credentials)
                except CollectorError as error:
                    if str(error) not in {"manual_login_verification_required",
                                          "login_form_missing_or_manual_verification",
                                          "password_login_tab_missing"}:
                        raise
                    _private_json(args.status, {"status": "needs_human", "reason": str(error)})
                    page.screenshot(path=str(args.screenshot), full_page=True)
                    args.screenshot.chmod(0o600)
                    # Preserve this isolated headed browser. Do not click the
                    # login button again or let a later job use this lane.
                    while True:
                        ready = _ready_erp_page(page)
                        if ready is not None:
                            page = ready
                            break
                        time.sleep(2)
                account_id = _account_scope(credentials["company"], page.url)
                scope = SalesReadbackScope(profile_id, account_id, TENANT, OPERATOR,
                                           date(2026, 10, 7))
                reader = WisejSalesReadback(page, scope)
                if reader.account_identity() != (profile_id, account_id):
                    raise RuntimeError("erp_login_scope_mismatch")
                _private_json(args.status, {"status": "reading", "account_verified": True})
                first_pass = []
                for method_name in ("_ensure_sales_page", "_audit_options",
                                    "_range_includes_order", "_query_status"):
                    original = getattr(reader, method_name)

                    def traced(*a, _name=method_name, _original=original, **kw):
                        try:
                            value = _original(*a, **kw)
                            summary = ("set=" + ",".join(sorted(value)) if isinstance(value, set) else
                                       "rows=" + str(len(value[1])) if _name == "_query_status" else
                                       str(value) if isinstance(value, bool) else "ok")
                            first_pass.append({"step": _name, "filter": a[-1] if a else "",
                                               "result": summary})
                            return value
                        except Exception as error:
                            first_pass.append({"step": _name, "filter": a[-1] if a else "",
                                               "error": type(error).__name__ + ":" + str(error)[:120]})
                            raise

                    setattr(reader, method_name, traced)
                listing = reader.find_by_full_number(ORDER)
                if not listing.complete:
                    stages = {"first_pass": first_pass}
                    for name, action in (
                        ("sales_page", reader._ensure_sales_page),
                        ("date_range", reader._range_includes_order),
                        ("audit_options", reader._audit_options),
                    ):
                        try:
                            value = action()
                            stages[name] = (str(value)[:200] if name in {"date_range", "audit_options"}
                                            else "ok")
                        except Exception as error:
                            stages[name] = type(error).__name__ + ":" + str(error)[:120]
                            break
                    else:
                        for name in (*AUDIT_FILTERS, ALL_FILTER):
                            try:
                                total, rows = reader._query_status(ORDER, name)
                                stages[name] = {"pager_total": total, "visible_rows": len(rows),
                                                "exact_numbers": sum(row["number"] == ORDER for row in rows)}
                            except Exception as error:
                                stages[name] = type(error).__name__ + ":" + str(error)[:120]
                                break
                    _private_json(args.status, {"status": "lookup_incomplete", "stages": stages})
                if not listing.complete or len(listing.rows) != 1 or listing.rows[0]["number"] != ORDER:
                    raise RuntimeError("exact_number_lookup_incomplete_or_ambiguous")
                row = dict(listing.rows[0])
                original_close = reader._close_editor

                def capture_then_close():
                    cell_rows = page.evaluate("""() => [...document.querySelectorAll('tr.GridBodyRow')]
                      .filter(r=>r.getBoundingClientRect().width>0 &&
                                 r.querySelector('td[_columnindex="46"]'))
                      .map(r=>({class:String(r.className||''),
                        parentClass:String(r.parentElement?.className||''),
                        cells:[...r.querySelectorAll('td[_columnindex]')]
                          .map(c=>({index:Number(c.getAttribute('_columnindex')),
                                    text:(c.innerText||'').trim(),
                                    title:c.getAttribute('title')||''}))}))""")
                    _private_json(args.evidence.with_name("detail-cells.json"),
                                  {"number": ORDER, "rows": cell_rows})
                    return original_close()

                reader._close_editor = capture_then_close
                try:
                    detail = dict(reader.open_detail_readonly(row))
                except Exception as error:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    page.screenshot(path=str(args.screenshot), full_page=True)
                    args.screenshot.chmod(0o600)
                    _private_json(args.status, {"status": "detail_incomplete",
                        "reason": type(error).__name__ + ":" + str(error)[:120],
                        "editor_visible": page.locator('input[id$="$edNumber"]:visible').count(),
                        "dialogs": page.locator('.DialogCaption:visible').all_inner_texts()[:8],
                        "buttons": page.locator('button:visible').all_inner_texts()[-12:],
                        "body_tail": page.locator('body').inner_text()[-600:],
                        "center_modal": page.evaluate("""() => {
                          let e=document.elementFromPoint(innerWidth/2,innerHeight/2);
                          const out=[];
                          for(let i=0;e&&i<4;i++,e=e.parentElement)
                            out.push({tag:e.tagName,cls:String(e.className||'').slice(0,90),
                                      text:(e.innerText||'').trim().slice(0,300)});
                          return out;
                        }"""),
                        "screenshot": str(args.screenshot)})
                    raise
                diagnostic = _unit_diagnostic(reader, row, args.screenshot, detail, base_unit_id)
                evidence = {"source": "N150 isolated Playwright ERP browser, read-only",
                    "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "scope": {"profile_id": profile_id, "account_scope_sha256": account_id,
                              "tenant_verified": True, "operator_verified": True,
                              "published_base_unit_id": base_unit_id},
                    "exact_number_query_complete": True, "exact_number_count": 1,
                    "list": row, "detail": detail, "unit_diagnostic": diagnostic,
                    "screenshot": args.screenshot.name,
                    "no_business_write": True}
                _private_json(args.evidence, evidence)
                _private_json(args.status, {"status": "succeeded", "order_number": ORDER,
                    "audit_status": row["audit_status"],
                    "unit_verified": diagnostic["verified"],
                    "unit_basis": diagnostic["unit_basis"],
                    "evidence": str(args.evidence)})
                return 0
            finally:
                context.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Do not print credentials, page text, or a Playwright call log.
        print(json.dumps({"status": "failed", "reason": str(error)
                          if isinstance(error, (CollectorError, RuntimeError))
                          else type(error).__name__}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
