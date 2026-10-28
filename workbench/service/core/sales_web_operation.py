"""Bind a frozen sales confirmation to one host-side ERP web import.

The API only queues an immutable draft. This module runs in the dedicated
browser worker, where the private ERP template and login are available.
Unknown attempts are never retried: a durable transport marker permits only
recovery of the locally saved website confirmation, never an ERP lookup.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Mapping

from .sales_web_import import WebImportConfig, build_sales_web_import_xls
from .sales_web_transport import (SalesWebImportTransport, WebImportBlocked,
                                  WebImportOutcome, WebImportSpec, validate_sales_header_limits)
from .sales_trial_authorization import sales_write_mode, trial_request_matches
from .unit_policy import CONFIRMED_BASE_UNIT, USER_UNIT_EVIDENCE, VALID_UNIT_EVIDENCE


SCHEMA = "sales_web_import_v1"
LINE_SCHEMA = "sales_web_import_v2"
HEX = re.compile(r"[0-9a-f]{64}\Z")
ORDERS = re.compile(r"[A-Za-z0-9_-]{1,20}\Z")
MONEY = Decimal("0.01")


@dataclass(frozen=True)
class SalesHostScope:
    """Explicit acceptance and private paths supplied only on the N150 host."""

    enabled: bool
    acceptance_complete: bool
    profile_id: str
    account_id: str
    tenant_name: str
    operator_name: str
    template_path: Path
    template_sha256: str
    warehouse_id: str
    warehouse_name: str
    department_name: str
    maker_name: str
    allowed_handlers: tuple[str, ...]
    trial_authorization: Mapping | None = None


def _required_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise WebImportBlocked(name + "_invalid")
    return value.strip()


def _host_guard(host: SalesHostScope, *, readback_only=False) -> None:
    if not isinstance(host, SalesHostScope) or (
            not readback_only and sales_write_mode(
                host.enabled, host.acceptance_complete, host.trial_authorization) == "disabled"):
        raise WebImportBlocked("sales_writer_disabled")
    for name in ("profile_id", "account_id", "tenant_name", "operator_name",
                 "warehouse_id", "warehouse_name", "department_name", "maker_name"):
        _required_text(getattr(host, name), name)
    if (not isinstance(host.allowed_handlers, tuple) or not host.allowed_handlers or
            any(not isinstance(name, str) or not name for name in host.allowed_handlers)):
        raise WebImportBlocked("handler_allowlist_invalid")
    template = Path(host.template_path)
    if (not readback_only and (not HEX.fullmatch(host.template_sha256) or
            template.is_symlink() or not template.is_file() or
            hashlib.sha256(template.read_bytes()).hexdigest() != host.template_sha256)):
        raise WebImportBlocked("host_template_invalid")


def _binding(payload: Mapping, operation: Mapping, host: SalesHostScope,
             *, readback_only=False) -> tuple[dict, dict, str]:
    _host_guard(host, readback_only=readback_only)
    if not isinstance(payload, Mapping) or payload.get("schema") not in {SCHEMA, LINE_SCHEMA}:
        raise WebImportBlocked("sales_payload_schema_invalid")
    if operation.get("kind") != "sales_order_import" or operation.get("role") != "sales":
        raise WebImportBlocked("sales_operation_type_invalid")
    try:
        intent_id = str(uuid.UUID(operation["id"]))
        session = dict(payload["frozen_session"])
        details = dict(payload["import_config"])
        customer = dict(payload["customer_binding"])
        number = payload["order_number"]
        confirmed = json.loads(session["confirmed_payload"])
        draft = json.loads(session["draft"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise WebImportBlocked("sales_frozen_payload_invalid") from None
    if (not isinstance(number, str) or not ORDERS.fullmatch(number) or
            number != operation.get("target_id") or
            payload.get("session_id") != session.get("id") or
            payload.get("profile_id") != operation.get("profile_id") or
            payload.get("profile_id") != host.profile_id or
            payload.get("account_id") != host.account_id or
            details.get("profile_id") != host.profile_id or
            details.get("account_id") != host.account_id or
            session.get("profile_id") != host.profile_id or
            confirmed.get("profile_id") != host.profile_id or
            payload.get("customer_name") != session.get("customer_name") or
            payload.get("customer_name") != draft.get("customer_name") or
            payload.get("confirmed_version") != session.get("confirmed_version") or
            payload.get("confirmed_hash") != session.get("confirmed_hash") or
            (not readback_only and (
                details.get("template_sha256") != host.template_sha256 or
                details.get("warehouse_id") != host.warehouse_id or
                details.get("warehouse_name") != host.warehouse_name or
                details.get("department_name") != host.department_name or
                details.get("maker_name") != host.maker_name or
                details.get("handler_name") not in host.allowed_handlers))):
        raise WebImportBlocked("sales_frozen_scope_mismatch")
    if (details.get("owner_id") != confirmed.get("fixed_sales_owner_id") or
            details.get("data_version") != confirmed.get("data_version") or
            not isinstance(details.get("order_date"), str)):
        raise WebImportBlocked("sales_confirmation_scope_mismatch")
    if (customer.get("customer_name") != payload.get("customer_name") or
            customer.get("profile_id") != host.profile_id or
            customer.get("account_id") != host.account_id or
            customer.get("match_count") != 1 or
            customer.get("source") != "erp_customer_exact_lookup" or
            not isinstance(customer.get("erp_customer_id"), str) or
            not customer["erp_customer_id"].strip() or
            not isinstance(customer.get("source_hash"), str) or
            not HEX.fullmatch(customer["source_hash"])):
        raise WebImportBlocked("sales_customer_binding_invalid")
    try:
        seen = datetime.fromisoformat(customer["observed_at"])
        age = datetime.now(timezone.utc) - seen.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        raise WebImportBlocked("sales_customer_binding_invalid") from None
    if seen.tzinfo is None:
        raise WebImportBlocked("sales_customer_binding_invalid")
    if age < -timedelta(minutes=5):
        raise WebImportBlocked("sales_customer_binding_stale")
    units = payload.get("unit_acceptance")
    evidences = payload.get("line_unit_evidence")
    if (units != {"base_unit_id": "1", "unit_name": CONFIRMED_BASE_UNIT,
                  "source": USER_UNIT_EVIDENCE} or
            payload.get("line_count") != len(draft.get("lines", ())) or
            not isinstance(evidences, list) or len(evidences) != payload["line_count"] or
            any(value not in VALID_UNIT_EVIDENCE for value in evidences) or
            evidences != [line.get("unit_evidence") for line in draft["lines"]]):
        raise WebImportBlocked("sales_unit_binding_invalid")
    if payload.get('schema') == LINE_SCHEMA:
        documents = payload.get('document_numbers')
        line_documents = payload.get('line_documents')
        if (payload.get('document_mode') != 'line_document' or not isinstance(documents, list) or
                len(documents) != payload['line_count'] or not documents or
                any(not isinstance(value, str) or not ORDERS.fullmatch(value) for value in documents) or
                len(set(documents)) != len(documents) or documents[0] != number or
                not isinstance(line_documents, list) or len(line_documents) != len(documents) or
                any(not isinstance(value, dict) or value.get('line_id') != str(index) or
                    value.get('order_number') != documents[index - 1]
                    for index, value in enumerate(line_documents, 1))):
            raise WebImportBlocked('sales_document_binding_invalid')
    try:
        date.fromisoformat(details["order_date"])
    except ValueError:
        raise WebImportBlocked("sales_order_date_invalid") from None
    if (not readback_only and
            sales_write_mode(host.enabled, host.acceptance_complete,
                             host.trial_authorization) == "trial" and
            not trial_request_matches(host.trial_authorization, payload, operation)):
        raise WebImportBlocked("sales_trial_scope_mismatch")
    return session, details, intent_id


def _line_tax(line, identity: Mapping) -> dict[str, str]:
    net = Decimal(line.untaxed_line_amount)
    unit = Decimal(line.untaxed_unit_price)
    tax = (net * Decimal("0.13")).quantize(MONEY, rounding=ROUND_HALF_UP)
    taxed_unit = (unit * Decimal("1.13")).quantize(MONEY, rounding=ROUND_HALF_UP)
    result = {"line_id": line.line_id, "sku": line.sku,
            "sku_id": identity["sku_id"], "warehouse_id": identity["warehouse_id"],
            "base_unit_id": identity["base_unit"],
            "product_name": line.product_name, "delivery_date": line.delivery_date,
            "detail_note": line.detail_note, "quantity": line.quantity,
            "untaxed_unit_price": line.untaxed_unit_price,
            "untaxed_line_amount": line.untaxed_line_amount,
            "tax_rate_percent": "13", "tax_amount": format(tax, ".2f"),
            "taxed_unit_price": format(taxed_unit, ".2f"),
            "taxed_line_amount": format(net + tax, ".2f")}
    if line.order_number:
        result.update(order_number=line.order_number, summary=line.summary)
    return result


def prepare_sales_spec(payload: Mapping, operation: Mapping, host: SalesHostScope,
                       runtime: Path) -> WebImportSpec:
    """Validate the frozen XLS and its header/line projections before login."""
    session, details, intent_id = _binding(payload, operation, host)
    staging = Path(runtime) / "sales-web-import-staging"
    staging.mkdir(mode=0o700, exist_ok=True)
    if staging.is_symlink() or not staging.resolve().is_relative_to(Path(runtime).resolve()):
        raise WebImportBlocked("sales_staging_path_invalid")
    destination = staging / (intent_id + ".xls")
    try:
        config = WebImportConfig(Path(host.template_path), destination,
            payload["order_number"], date.fromisoformat(details["order_date"]),
            host.profile_id, details["data_version"], details["owner_id"],
            details["department_name"], details["maker_name"],
            details["handler_name"], details["warehouse_id"],
            details["warehouse_name"],
            'line_document' if payload['schema'] == LINE_SCHEMA else 'single_document',
            tuple(payload.get('document_numbers', ())) if payload['schema'] == LINE_SCHEMA else ())
        generated = build_sales_web_import_xls(session, config)
        if generated.summary != payload.get("summary"):
            raise WebImportBlocked("sales_summary_binding_invalid")
        draft = json.loads(session["draft"])
        if len(generated.lines) != len(draft["lines"]):
            raise WebImportBlocked("sales_line_count_mismatch")
        expected_lines = tuple(_line_tax(line, reviewed["resolved_identity"])
            for line, reviewed in zip(generated.lines, draft["lines"]))
        if payload['schema'] == LINE_SCHEMA:
            projected = [{'line_id': line.line_id, 'order_number': line.order_number,
                          'summary': line.summary, 'delivery_date': line.delivery_date}
                         for line in generated.lines]
            if projected != payload['line_documents']:
                raise WebImportBlocked('sales_document_binding_invalid')
            for line in generated.lines:
                validate_sales_header_limits(str(draft.get('note') or '').strip(), line.summary)
        first = expected_lines[0]
        net_total = sum(Decimal(line["untaxed_line_amount"]) for line in expected_lines)
        taxed_total = sum(Decimal(line["taxed_line_amount"]) for line in expected_lines)
        if (sales_write_mode(host.enabled, host.acceptance_complete,
                             host.trial_authorization) == "trial" and
                host.trial_authorization["taxed_total"] != format(taxed_total, ".2f")):
            # This candidate was never handed to the transport. Permit a
            # corrected private authorization to regenerate the same intent.
            generated.path.unlink(missing_ok=True)
            raise WebImportBlocked("sales_trial_total_mismatch")
        listing = {"number": generated.order_number, "date": details["order_date"],
                   "audit_status": "未审核", "shipping_status": "未提交",
                   "customer": draft["customer_name"], "warehouse": host.warehouse_name,
                   "salesperson": details["handler_name"],
                   "delivery_date": first["delivery_date"],
                   "untaxed_total": format(net_total, ".2f"),
                   "taxed_total": format(taxed_total, ".2f")}
        header = {"number": generated.order_number,
                  "maker": details["maker_name"], "summary": generated.summary,
                  "note": str(draft.get("note") or "").strip()}
        validate_sales_header_limits(header["note"], header["summary"])
        return WebImportSpec(intent_id, generated.order_number, host.profile_id,
            host.account_id, generated.path, generated.sha256, listing, header,
            details["department_name"], first["product_name"],
            str(draft.get("note") or "").strip(), generated.detail_note,
            expected_lines, "1", host.warehouse_id, payload['schema'],
            'line_document' if payload['schema'] == LINE_SCHEMA else 'single_document',
            tuple(payload.get('document_numbers', ())) if payload['schema'] == LINE_SCHEMA else ())
    except WebImportBlocked:
        raise
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise WebImportBlocked("sales_import_generation_failed") from error


def run_sales_import(payload: Mapping, operation: Mapping, host: SalesHostScope,
                     runtime: Path, browser, *, readback_only: bool = False) -> WebImportOutcome:
    """Upload once, or recover a local receipt without contacting the ERP.

    ``readback_only`` remains a compatibility name for receipt recovery. It
    never opens a browser, performs an ERP readback, or repeats an upload.
    """
    _, _, intent_id = _binding(payload, operation, host, readback_only=readback_only)
    transport = SalesWebImportTransport(Path(runtime))
    if readback_only:
        directory = transport._directory(intent_id)
        marker = directory / "attempt.json"
        if marker.is_symlink() or not marker.is_file():
            raise WebImportBlocked("sales_attempt_marker_missing")
        manifest, _ = transport._load(directory)
        if (manifest.get('schema', SCHEMA) != payload['schema'] or
                manifest['order_number'] != payload['order_number'] or
                (payload['schema'] == LINE_SCHEMA and
                 manifest.get('document_numbers') != payload['document_numbers'])):
            raise WebImportBlocked('sales_document_binding_invalid')
        # A saved confirmation is the only automatic recovery evidence.
        # Pass no browser so recovery cannot start a query or another upload.
        return transport.execute(intent_id, None)
    spec = prepare_sales_spec(payload, operation, host, Path(runtime))
    transport.prepare(spec)
    return transport.execute(intent_id, browser)
