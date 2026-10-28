"""Customer identity must be re-proven from a complete live ERP dictionary."""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.erp_api import ErpClient
from core.sales_web_customer_preflight import (
    build_customer_binding, build_customer_bindings, lookup_exact_reference, lookup_exact_references,
    verify_customer_binding)
from core.sales_web_transport import WebImportBlocked


PROFILE = "profile-1"
ACCOUNT = "a" * 64
WEB_IDENTITY = (PROFILE, ACCOUNT)
CUSTOMER = "精确客户甲"
CUSTOMER_ROW = {"id": "customer-12", "fullname": CUSTOMER,
                "deleted": False, "isstop": False}


class Dictionary:
    def __init__(self, rows=None, profile=PROFILE, error=None):
        self.rows = [dict(row) for row in (rows if rows is not None else [CUSTOMER_ROW])]
        self.profile = profile
        self.error = error
        self.calls = []
        self.raw_calls = []

    def account(self):
        return self.profile

    def pages(self, method, params, collection, identity, *, size, limit):
        self.calls.append((method, params, collection, size, limit))
        if self.error:
            raise self.error
        seen = set()
        for row in self.rows:
            item_id = identity(row)
            if item_id in seen:
                raise ValueError("duplicate id")
            seen.add(item_id)
        return self.rows

    def call(self, method, params):
        self.raw_calls.append((method, params["pageno"], params["pagesize"]))
        assert method == "erp.employee.listall"
        start = (params["pageno"] - 1) * params["pagesize"]
        rows = self.rows[start:start + params["pagesize"]]
        return {"employees": rows if rows else None,
                "totalresults": max(len(self.rows) - 1, 0)}


def binding(client=None):
    return build_customer_binding(client or Dictionary(), CUSTOMER,
        expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
        web_account_identity=WEB_IDENTITY)


def verify(client, frozen):
    return verify_customer_binding(client, frozen,
        expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
        web_account_identity=WEB_IDENTITY)


def blocked(code, callback):
    with pytest.raises(WebImportBlocked) as caught:
        callback()
    assert caught.value.code == code
    assert CUSTOMER not in str(caught.value)


def test_binding_and_live_preflight_use_same_readonly_exact_lookup():
    client = Dictionary([{"id": "other", "fullname": CUSTOMER + "分店"},
                         CUSTOMER_ROW, {"id": "third", "fullname": "别的客户"}])
    frozen = binding(client)
    assert frozen["erp_customer_id"] == "customer-12"
    assert frozen["match_count"] == 1
    assert frozen["source"] == "erp_customer_exact_lookup"
    assert len(frozen["source_hash"]) == 64
    assert client.calls == [("erp.btype.list", {}, "btypes", 100, 200)]
    evidence = verify(client, frozen)
    assert evidence.source_hash == frozen["source_hash"]
    assert evidence.catalog_count == 3
    assert evidence.customer_id_sha256 != frozen["erp_customer_id"]
    assert CUSTOMER not in repr(evidence)
    assert "customer-12" not in repr(evidence)


@pytest.mark.parametrize("rows,code", [
    ([{"id": "retail", "fullname": "备用零售客户"}],
     "sales_customer_exact_match_not_unique"),
    ([CUSTOMER_ROW, {"id": "customer-13", "fullname": CUSTOMER}],
     "sales_customer_exact_match_not_unique"),
    ([{**CUSTOMER_ROW, "isstop": True}], "sales_customer_inactive"),
    ([{**CUSTOMER_ROW, "deleted": "1"}], "sales_customer_inactive"),
    ([{**CUSTOMER_ROW, "isstop": "?"}], "sales_customer_lookup_unavailable"),
    ([{**CUSTOMER_ROW, "btypeid": "other"}],
     "sales_customer_lookup_unavailable"),
])
def test_missing_duplicate_inactive_and_ambiguous_customer_are_blocked(rows, code):
    blocked(code, lambda: binding(Dictionary(rows)))


def test_id_or_hash_change_after_freeze_blocks_upload():
    frozen = binding()
    changed = Dictionary([{**CUSTOMER_ROW, "id": "customer-13"}])
    blocked("sales_customer_binding_changed", lambda: verify(changed, frozen))
    tampered = dict(frozen, source_hash="b" * 64)
    blocked("sales_customer_binding_changed", lambda: verify(Dictionary(), tampered))


def test_account_and_profile_must_be_proven_before_dictionary_query():
    client = Dictionary(profile="other")
    blocked("sales_customer_profile_mismatch", lambda: binding(client))
    assert not client.calls
    blocked("sales_customer_account_mismatch", lambda:
        build_customer_binding(Dictionary(), CUSTOMER,
            expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
            web_account_identity=(PROFILE, "b" * 64)))
    blocked("sales_customer_binding_invalid", lambda:
        verify(Dictionary(), dict(binding(), account_id="b" * 64)))


def test_unavailable_or_incomplete_lookup_fails_closed():
    blocked("sales_customer_lookup_unavailable", lambda:
        binding(Dictionary(error=TimeoutError("secret vendor body"))))
    blocked("sales_customer_lookup_unavailable", lambda:
        binding(Dictionary([CUSTOMER_ROW, CUSTOMER_ROW])))


def test_freeze_timestamp_and_source_are_checked_before_live_query():
    frozen = binding()
    client = Dictionary()
    blocked("sales_customer_binding_invalid", lambda:
        verify(client, dict(frozen, source="hand_entered")))
    blocked("sales_customer_binding_invalid", lambda:
        verify(client, dict(frozen, observed_at="2020-01-01T00:00:00")))
    blocked("sales_customer_binding_stale", lambda:
        verify(client, dict(frozen, observed_at=(
            datetime.now(timezone.utc) + timedelta(days=1)).isoformat())))
    assert not client.calls


def test_old_name_id_binding_is_live_reverified_without_daily_expiry():
    frozen = dict(binding(), observed_at="2020-01-01T00:00:00+00:00")
    client = Dictionary()
    evidence = verify(client, frozen)
    assert evidence.source_hash == frozen["source_hash"]
    assert evidence.observed_at != frozen["observed_at"]
    assert client.calls == [("erp.btype.list", {}, "btypes", 100, 200)]


@pytest.mark.parametrize("rows,error,code", [
    ([{**CUSTOMER_ROW, "id": "changed"}], None, "sales_customer_binding_changed"),
    ([{**CUSTOMER_ROW, "isstop": True}], None, "sales_customer_inactive"),
    ([{**CUSTOMER_ROW, "deleted": "1"}], None, "sales_customer_inactive"),
    ([CUSTOMER_ROW, {**CUSTOMER_ROW, "id": "duplicate-name"}], None,
     "sales_customer_exact_match_not_unique"),
    ([], TimeoutError("network unavailable"), "sales_customer_lookup_unavailable"),
])
def test_old_binding_still_blocks_fresh_changed_inactive_duplicate_or_unavailable_customer(rows, error, code):
    frozen = dict(binding(), observed_at="2020-01-01T00:00:00+00:00")
    client = Dictionary(rows, error=error)
    blocked(code, lambda: verify(client, frozen))
    assert len(client.calls) == 1


def test_bulk_customer_bindings_fetch_once_share_exact_digest_and_exclude_review_rows():
    rows = [CUSTOMER_ROW, {"id": "customer-2", "fullname": "精确客户乙"},
            {"id": "duplicate-1", "fullname": "重名客户"},
            {"id": "duplicate-2", "fullname": "重名客户", "isstop": True},
            {"id": "stopped", "fullname": "停用客户", "isstop": True},
            {"id": "invalid-name", "fullname": " 不能自动去空格 "},
            {"id": "unknown-status", "fullname": "状态不明客户", "deleted": "?"}]
    client = Dictionary(rows)
    result = build_customer_bindings(client, expected_profile_id=PROFILE,
        expected_account_id=ACCOUNT, web_account_identity=WEB_IDENTITY)
    assert client.calls == [("erp.btype.list", {}, "btypes", 100, 200)]
    assert set(result["bindings"]) == {CUSTOMER, "精确客户乙"}
    assert result["catalog_count"] == 7 and result["review_count"] == 5
    assert result["review_counts"] == {
        "duplicate_name": 2, "inactive": 1, "invalid_name": 1, "unknown_status": 1}
    assert result["duplicate_name_count"] == 1
    single = binding(Dictionary(rows))
    assert result["bindings"][CUSTOMER]["source_hash"] == single["source_hash"]
    assert result["bindings"][CUSTOMER]["erp_customer_id"] == single["erp_customer_id"]
    assert all(row["observed_at"] == result["observed_at"] for row in result["bindings"].values())
    assert verify(Dictionary(rows), result["bindings"][CUSTOMER]).source_hash == single["source_hash"]


@pytest.mark.parametrize("rows,error", [
    ([CUSTOMER_ROW, CUSTOMER_ROW], None),
    ([{**CUSTOMER_ROW, "btypeid": "conflicting-id"}], None),
    ([], TimeoutError("partial dictionary unavailable")),
])
def test_bulk_bindings_do_not_publish_partial_or_invalid_id_dictionary(rows, error):
    blocked("sales_customer_lookup_unavailable", lambda:
        build_customer_bindings(Dictionary(rows, error=error), expected_profile_id=PROFILE,
            expected_account_id=ACCOUNT, web_account_identity=WEB_IDENTITY))


def test_real_erp_client_allowlist_and_complete_pagination(tmp_path):
    vault = tmp_path / "erp.json"
    vault.write_text(json.dumps({"credentials": {"appkey": "1" * 32,
        "appsecret": "A" * 32, "signkey": "private-signing-key"},
        "token": {"appkey": "1" * 32, "profileid": PROFILE,
                  "auth_token": "private-token", "expires_at": time.time() + 3600}}))
    vault.chmod(0o600)
    calls = []

    def transport(endpoint, payload):
        calls.append((endpoint, payload["method"], payload["pageno"]))
        assert payload["method"] == "erp.btype.list"
        assert payload["token"] == "private-token"
        return {"btypes": [CUSTOMER_ROW], "totalresults": 1}

    client = ErpClient(vault, transport=transport)
    frozen = binding(client)
    assert verify(client, frozen).catalog_count == 1
    assert calls == [("/allnewapi", "erp.btype.list", "1")] * 2


@pytest.mark.parametrize("result", [
    {"btypes": [CUSTOMER_ROW], "totalresults": 2},
    {"btypes": [CUSTOMER_ROW], "totalresults": True},
    {"btypes": "wrong", "totalresults": 1},
])
def test_real_erp_client_rejects_nonclosed_pages(tmp_path, result):
    vault = tmp_path / "erp.json"
    vault.write_text(json.dumps({"credentials": {"appkey": "1" * 32,
        "appsecret": "A" * 32, "signkey": "key"},
        "token": {"appkey": "1" * 32, "profileid": PROFILE,
                  "auth_token": "token", "expires_at": time.time() + 3600}}))
    vault.chmod(0o600)
    client = ErpClient(vault, transport=lambda *_: result)
    blocked("sales_customer_lookup_unavailable", lambda: binding(client))


@pytest.mark.parametrize("kind,collection", [
    ("employee", "employees"), ("warehouse", "stocks")])
def test_named_staff_and_warehouse_use_exact_unique_readonly_identity(kind, collection):
    client = Dictionary([{"id": "ref-1", "fullname": "配置中的全名"}])
    ref = lookup_exact_reference(client, kind=kind, full_name="配置中的全名",
        expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
        web_account_identity=WEB_IDENTITY, expected_erp_id="ref-1")
    assert ref.erp_id == "ref-1"
    assert ref.erp_id not in repr(ref)
    if kind == "employee":
        assert client.calls == []
        assert client.raw_calls == [("erp.employee.listall", 1, 100),
                                    ("erp.employee.listall", 2, 100),
                                    ("erp.employee.listall", 1, 1),
                                    ("erp.employee.listall", 2, 1),
                                    ("erp.employee.listall", 1, 100)]
        assert ref.reported_total == 0 and ref.catalog_count == 1
        assert ref.completeness_basis == "independent_page_sizes_with_terminal_empty"
    else:
        assert client.calls == [("erp.stock.list", {}, collection, 100, 200)]
        assert ref.completeness_basis == "advertised_total_closed"
    blocked("sales_reference_id_mismatch", lambda:
        lookup_exact_reference(client, kind=kind, full_name="配置中的全名",
            expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
            web_account_identity=WEB_IDENTITY, expected_erp_id="other"))


def test_named_reference_never_selects_near_match_or_duplicate():
    client = Dictionary([{"id": "ref-1", "fullname": "姓名甲分部"}])
    blocked("sales_reference_exact_match_not_unique", lambda:
        lookup_exact_reference(client, kind="employee", full_name="姓名甲",
            expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
            web_account_identity=WEB_IDENTITY))
    duplicate = Dictionary([{"id": "ref-1", "fullname": "姓名甲"},
                            {"id": "ref-2", "fullname": "姓名甲"}])
    blocked("sales_reference_exact_match_not_unique", lambda:
        lookup_exact_reference(duplicate, kind="employee", full_name="姓名甲",
            expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
            web_account_identity=WEB_IDENTITY))


def test_employee_listall_uses_second_pagination_when_advertised_total_is_short():
    rows = [{"id": f"employee-{i}", "fullname": f"职员{i}",
             "deleted": False, "isstop": False} for i in range(14)]
    client = Dictionary(rows)
    item = lookup_exact_reference(client, kind="employee", full_name="职员13",
        expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
        web_account_identity=WEB_IDENTITY)
    assert item.erp_id == "employee-13"
    assert (item.catalog_count, item.reported_total) == (14, 13)
    assert len(client.raw_calls) == 18


def test_three_staff_share_one_complete_snapshot_and_hash():
    rows = [{"id": f"employee-{i}", "fullname": f"职员{i}",
             "deleted": False, "isstop": False} for i in range(14)]
    client = Dictionary(rows)
    result = lookup_exact_references(client, kind="employee",
        full_names=("职员1", "职员2", "职员13"),
        expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
        web_account_identity=WEB_IDENTITY)
    assert {item.erp_id for item in result.values()} == {
        "employee-1", "employee-2", "employee-13"}
    assert len({item.snapshot_hash for item in result.values()}) == 1
    assert len({item.observed_at for item in result.values()}) == 1
    assert len(client.raw_calls) == 18


def test_employee_second_scan_or_terminal_page_change_blocks():
    class Drifting(Dictionary):
        def call(self, method, params):
            result = super().call(method, params)
            if (params["pageno"], params["pagesize"]) == (2, 1):
                result["employees"] = [{"id": "changed", "fullname": "别的人"}]
            return result
    blocked("sales_reference_lookup_unavailable", lambda:
        lookup_exact_reference(Drifting([
            {"id": "employee-1", "fullname": "职员1"},
            {"id": "employee-2", "fullname": "职员2"}]),
            kind="employee", full_name="职员1",
            expected_profile_id=PROFILE, expected_account_id=ACCOUNT,
            web_account_identity=WEB_IDENTITY))
