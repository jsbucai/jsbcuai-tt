"""Read-only ERP dictionary check for a frozen sales web-import customer.

The ERP API token proves the profile; the already verified browser login
supplies the separate web account scope. Neither a cached catalog nor a
similar-name/retail customer can satisfy this check.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

from .sales_web_transport import WebImportBlocked


METHOD = "erp.btype.list"
COLLECTION = "btypes"
SOURCE = "erp_customer_exact_lookup"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_BINDING_FIELDS = ("customer_name", "erp_customer_id", "profile_id", "account_id",
                   "match_count", "source", "source_hash")
_REFERENCE_METHODS = {"employee": ("erp.employee.listall", "employees"),
                      "warehouse": ("erp.stock.list", "stocks")}


@dataclass(frozen=True)
class CustomerPreflightEvidence:
    """Safe to log: no raw customer name, ERP ID, or vendor response."""

    customer_id_sha256: str
    source_hash: str
    observed_at: str
    catalog_count: int


@dataclass(frozen=True)
class ExactReference:
    """For a private config builder; repr contains no raw dictionary identity."""

    kind: str
    erp_id: str = field(repr=False)
    id_sha256: str
    source_hash: str
    observed_at: str
    catalog_count: int
    reported_total: int
    completeness_basis: str
    snapshot_hash: str


def _text(value, code: str, *, limit: int = 200) -> str:
    if (not isinstance(value, str) or not value or value != value.strip() or
            len(value) > limit or "\r" in value or "\n" in value):
        raise WebImportBlocked(code)
    return value


def _scope(client, profile_id: str, account_id: str, web_account_identity) -> None:
    _text(profile_id, "sales_customer_scope_invalid", limit=128)
    if not isinstance(account_id, str) or not _HEX.fullmatch(account_id):
        raise WebImportBlocked("sales_customer_scope_invalid")
    if web_account_identity != (profile_id, account_id):
        raise WebImportBlocked("sales_customer_account_mismatch")
    try:
        actual_profile = client.account()
    except Exception:
        raise WebImportBlocked("sales_customer_lookup_unavailable") from None
    if actual_profile != profile_id:
        raise WebImportBlocked("sales_customer_profile_mismatch")


def _erp_id(row) -> str:
    if not isinstance(row, dict):
        raise ValueError("customer_row_invalid")
    raw = row.get("id")
    if isinstance(raw, bool) or raw is None:
        raise ValueError("customer_id_invalid")
    value = str(raw)
    if not value or value != value.strip() or len(value) > 128:
        raise ValueError("customer_id_invalid")
    alias = row.get("btypeid")
    if alias is not None and str(alias) != value:
        raise ValueError("customer_id_conflict")
    return value


def _active(row) -> bool:
    for key in ("deleted", "isstop"):
        value = row.get(key)
        if value is None or value is False or value == 0 or value in ("", "0", "false", "False"):
            continue
        if value is True or value == 1 or value in ("1", "true", "True"):
            return False
        raise ValueError("customer_status_unknown")
    return True


def _dictionary_rows(client, profile_id: str, account_id: str,
                     web_account_identity) -> list[dict]:
    """One complete dictionary request; malformed or duplicate IDs fail closed."""
    _scope(client, profile_id, account_id, web_account_identity)
    try:
        # pages() requires an unchanged total, complete page closure, and
        # unique nonempty IDs. A partial listing can never prove uniqueness.
        rows = client.pages(METHOD, {}, COLLECTION, _erp_id, size=100, limit=200)
        if not isinstance(rows, list):
            raise ValueError("customer_catalog_invalid")
        identities = [_erp_id(row) for row in rows]
        if len(set(identities)) != len(identities):
            raise ValueError("customer_duplicate_id")
        return rows
    except WebImportBlocked:
        raise
    except Exception:
        raise WebImportBlocked("sales_customer_lookup_unavailable") from None


def _binding_from_active_row(row: dict, customer_name: str, profile_id: str,
                             account_id: str, observed_at: str) -> dict:
    """Shared stable digest contract for single and full-dictionary bindings."""
    erp_id = _erp_id(row)
    digest_input = {"schema": "sales_customer_exact_lookup_v1", "method": METHOD,
                    "profile_id": profile_id, "account_id": account_id,
                    "customer_name": customer_name, "erp_customer_id": erp_id,
                    "active": True, "match_count": 1}
    source_hash = hashlib.sha256(json.dumps(
        digest_input, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"customer_name": customer_name, "erp_customer_id": erp_id,
            "profile_id": profile_id, "account_id": account_id,
            "match_count": 1, "source": SOURCE, "source_hash": source_hash,
            "observed_at": observed_at}


def _lookup(client, customer_name: str, profile_id: str, account_id: str,
            web_account_identity) -> tuple[dict, CustomerPreflightEvidence]:
    _text(customer_name, "sales_customer_name_invalid")
    rows = _dictionary_rows(client, profile_id, account_id, web_account_identity)
    try:
        matches = [row for row in rows if row.get("fullname") == customer_name]
        if len(matches) != 1:
            raise WebImportBlocked("sales_customer_exact_match_not_unique")
        row = matches[0]
        if not _active(row):
            raise WebImportBlocked("sales_customer_inactive")
        erp_id = _erp_id(row)
    except WebImportBlocked:
        raise
    except Exception:
        raise WebImportBlocked("sales_customer_lookup_unavailable") from None
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    binding = _binding_from_active_row(row, customer_name, profile_id, account_id, observed_at)
    evidence = CustomerPreflightEvidence(
        hashlib.sha256(erp_id.encode("utf-8")).hexdigest(),
        binding["source_hash"], observed_at, len(rows))
    return binding, evidence


def build_customer_binding(client, customer_name: str, *, expected_profile_id: str,
                           expected_account_id: str,
                           web_account_identity) -> dict:
    """Build a policy binding after a verified web login and fresh API lookup."""
    binding, _ = _lookup(client, customer_name, expected_profile_id,
                         expected_account_id, web_account_identity)
    return binding


def build_customer_bindings(client, *, expected_profile_id: str,
                            expected_account_id: str, web_account_identity) -> dict:
    """Build exact name→ID bindings from one complete read-only dictionary.

    Duplicate names, even when one row is stopped, never select a winner.
    ``review_count`` counts excluded rows; per-reason counts are mutually
    exclusive. This returns a private policy candidate, never enables upload.
    """
    rows = _dictionary_rows(client, expected_profile_id, expected_account_id,
                            web_account_identity)
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reviews = {"invalid_name": 0, "duplicate_name": 0,
               "inactive": 0, "unknown_status": 0}
    named = {}
    for row in rows:
        name = row.get("fullname")
        try:
            _text(name, "sales_customer_name_invalid")
        except WebImportBlocked:
            reviews["invalid_name"] += 1
            continue
        named.setdefault(name, []).append(row)
    bindings = {}
    for name, matches in named.items():
        if len(matches) != 1:
            reviews["duplicate_name"] += len(matches)
            continue
        row = matches[0]
        try:
            active = _active(row)
        except ValueError:
            reviews["unknown_status"] += 1
            continue
        if not active:
            reviews["inactive"] += 1
            continue
        bindings[name] = _binding_from_active_row(row, name, expected_profile_id,
                                                  expected_account_id, observed_at)
    return {"bindings": bindings, "catalog_count": len(rows),
            "review_count": sum(reviews.values()), "review_counts": reviews,
            "duplicate_name_count": sum(len(matches) > 1 for matches in named.values()),
            "observed_at": observed_at}


def verify_customer_binding(client, frozen_binding: Mapping, *,
                            expected_profile_id: str, expected_account_id: str,
                            web_account_identity) -> CustomerPreflightEvidence:
    """Re-query immediately before first upload and compare the frozen binding."""
    if not isinstance(frozen_binding, Mapping):
        raise WebImportBlocked("sales_customer_binding_invalid")
    try:
        customer_name = _text(frozen_binding.get("customer_name"),
                              "sales_customer_binding_invalid")
        frozen_id = _text(frozen_binding.get("erp_customer_id"),
                          "sales_customer_binding_invalid", limit=128)
        source_hash = frozen_binding.get("source_hash")
        if (frozen_binding.get("profile_id") != expected_profile_id or
                frozen_binding.get("account_id") != expected_account_id or
                type(frozen_binding.get("match_count")) is not int or
                frozen_binding["match_count"] != 1 or
                frozen_binding.get("source") != SOURCE or
                not isinstance(source_hash, str) or not _HEX.fullmatch(source_hash)):
            raise WebImportBlocked("sales_customer_binding_invalid")
        observed = datetime.fromisoformat(frozen_binding["observed_at"])
        if observed.tzinfo is None:
            raise ValueError("missing_timezone")
        age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
        # This is a durable name↔ID binding, not a cached freshness claim.
        # The mandatory live dictionary lookup below re-proves it every time.
        if age < -timedelta(minutes=5):
            raise WebImportBlocked("sales_customer_binding_stale")
    except WebImportBlocked:
        raise
    except (KeyError, TypeError, ValueError):
        raise WebImportBlocked("sales_customer_binding_invalid") from None
    live, evidence = _lookup(client, customer_name, expected_profile_id,
                             expected_account_id, web_account_identity)
    if (not hmac.compare_digest(live["erp_customer_id"], frozen_id) or
            not hmac.compare_digest(live["source_hash"], source_hash) or
            any(live[field] != frozen_binding.get(field)
                for field in _BINDING_FIELDS if field not in
                ("erp_customer_id", "source_hash"))):
        raise WebImportBlocked("sales_customer_binding_changed")
    return evidence


def _employee_listall_rows(client) -> tuple[list[dict], int]:
    """Prove listall closure independently when its reported total differs.

    The observed ERP response can advertise fewer employees than it returns.
    We preserve that discrepancy and require the same complete row set from
    two page sizes, terminal empty pages, and a stable repeat of the first
    page. This does not reinterpret or silently repair totalresults.
    """
    def page(number: int, size: int) -> tuple[list[dict], int]:
        result = client.call("erp.employee.listall",
                             {"pageno": number, "pagesize": size})
        if not isinstance(result, dict):
            raise ValueError("employee_response_invalid")
        raw = result.get("employees")
        if raw is None and number > 1:
            raw = []
        total = result.get("totalresults")
        if (not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw)
                or isinstance(total, bool) or not str(total).isdigit()):
            raise ValueError("employee_page_invalid")
        return raw, int(total)

    def indexed(rows: list[dict]) -> dict[str, str]:
        output = {}
        for row in rows:
            ident = _erp_id(row)
            if ident in output:
                raise ValueError("employee_duplicate_id")
            output[ident] = hashlib.sha256(json.dumps(
                row, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
        return output

    first, reported = page(1, 100)
    if not first or len(first) >= 100:
        raise ValueError("employee_listall_not_bounded")
    base = indexed(first)
    terminal, check_total = page(2, 100)
    if terminal or check_total != reported:
        raise ValueError("employee_listall_terminal_missing")
    single = []
    for number in range(1, len(first) + 1):
        rows, check_total = page(number, 1)
        if len(rows) != 1 or check_total != reported:
            raise ValueError("employee_listall_second_scan_incomplete")
        single.extend(rows)
    terminal, check_total = page(len(first) + 1, 1)
    if terminal or check_total != reported or indexed(single) != base:
        raise ValueError("employee_listall_second_scan_mismatch")
    repeated, check_total = page(1, 100)
    if check_total != reported or indexed(repeated) != base:
        raise ValueError("employee_listall_changed")
    return first, reported


def lookup_exact_references(client, *, kind: str, full_names: tuple[str, ...],
                            expected_profile_id: str, expected_account_id: str,
                            web_account_identity,
                            expected_erp_ids: Mapping[str, str] | None = None
                            ) -> dict[str, ExactReference]:
    """Resolve several names against one complete, shared dictionary snapshot.

    This is a read-only config-builder aid. The customer gate above remains the
    mandatory upload preflight; staff and warehouse configuration has its own
    frozen checks in the sales operation.
    """
    if kind not in _REFERENCE_METHODS:
        raise WebImportBlocked("sales_reference_kind_invalid")
    if not isinstance(full_names, tuple) or not full_names:
        raise WebImportBlocked("sales_reference_names_invalid")
    for name in full_names:
        _text(name, "sales_reference_name_invalid")
    if len(full_names) != len(set(full_names)):
        raise WebImportBlocked("sales_reference_names_invalid")
    if expected_erp_ids is not None and (
            not isinstance(expected_erp_ids, Mapping) or
            any(name not in full_names for name in expected_erp_ids)):
        raise WebImportBlocked("sales_reference_ids_invalid")
    _scope(client, expected_profile_id, expected_account_id, web_account_identity)
    method, collection = _REFERENCE_METHODS[kind]
    try:
        if kind == "employee":
            rows, reported_total = _employee_listall_rows(client)
            completeness_basis = "independent_page_sizes_with_terminal_empty"
        else:
            rows = client.pages(method, {}, collection, _erp_id, size=100, limit=200)
            reported_total = len(rows)
            completeness_basis = "advertised_total_closed"
        snapshot_hash = hashlib.sha256(json.dumps(
            {"schema": "sales_reference_snapshot_v1", "kind": kind,
             "method": method, "profile_id": expected_profile_id,
             "account_id": expected_account_id,
             "rows": sorted(rows, key=_erp_id)},
            ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        output = {}
        for full_name in full_names:
            matches = [row for row in rows if row.get("fullname") == full_name]
            if len(matches) != 1:
                raise WebImportBlocked("sales_reference_exact_match_not_unique")
            row = matches[0]
            if not _active(row):
                raise WebImportBlocked("sales_reference_inactive")
            erp_id = _erp_id(row)
            expected_id = expected_erp_ids.get(full_name) if expected_erp_ids else None
            if expected_id is not None and erp_id != _text(
                    expected_id, "sales_reference_id_invalid", limit=128):
                raise WebImportBlocked("sales_reference_id_mismatch")
            source_hash = hashlib.sha256(json.dumps(
                {"schema": "sales_reference_exact_lookup_v1", "kind": kind,
                 "method": method, "profile_id": expected_profile_id,
                 "account_id": expected_account_id, "full_name": full_name,
                 "erp_id": erp_id, "active": True, "match_count": 1},
                ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            output[full_name] = ExactReference(kind, erp_id,
                hashlib.sha256(erp_id.encode("utf-8")).hexdigest(), source_hash,
                observed_at, len(rows), reported_total, completeness_basis,
                snapshot_hash)
        return output
    except WebImportBlocked:
        raise
    except Exception:
        raise WebImportBlocked("sales_reference_lookup_unavailable") from None


def lookup_exact_reference(client, *, kind: str, full_name: str,
                           expected_profile_id: str, expected_account_id: str,
                           web_account_identity, expected_erp_id: str | None = None
                           ) -> ExactReference:
    """Compatibility wrapper for one named employee or warehouse."""
    ids = {full_name: expected_erp_id} if expected_erp_id is not None else None
    return lookup_exact_references(client, kind=kind, full_names=(full_name,),
        expected_profile_id=expected_profile_id,
        expected_account_id=expected_account_id,
        web_account_identity=web_account_identity,
        expected_erp_ids=ids)[full_name]
