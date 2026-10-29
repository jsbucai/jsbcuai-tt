"""Purchase-order draft adapter and the one-shot smoke script. No network."""
import json
from datetime import datetime, timezone
from pathlib import Path
import time

import pytest

from core.erp_api import ErpClient, ErpUnavailable, PURCHASE_ORDER_ADD_METHOD
from core.purchase_erp_adapter import (OFFICIAL_CONTRACT_HASH, READBACK_METHOD, assemble_order,
    attach_purchase_adapter, erp_line_prices, wire_purchaseorder)
from core.purchase_submission import PurchaseSubmission, PurchaseSubmissionPolicy
from core.purchase_workspace import PurchaseWorkspace
from core.purchase_workspace_data import WAREHOUSE
from core.wsgjp_gateway import ApiError
from scripts.erp_purchase_draft_smoketest import run
from test_daily_work import store  # noqa: F401  (pytest fixture)
from test_purchase_submission import ACTOR, draft_body, order_line
from test_purchase_workspace import inventory, receipt, seed

NOW = datetime(2026, 10, 28, 1, tzinfo=timezone.utc).timestamp()


class Catalog:
    def __init__(self, response=None):
        self.calls = []
        self.bodies = []
        self.allow_purchase_order = False
        self.response = {'vchcode': 321} if response is None else response

    def identity(self):
        return {'profile_id': 'P', 'employee_id': '77'}

    def call(self, method, params):
        self.calls.append(method)
        if method == PURCHASE_ORDER_ADD_METHOD:
            assert self.allow_purchase_order is True
            self.bodies.append(params['purchaseorder'])
            return self.response
        if method == 'erp.btype.list':
            return {'btypes': [{'id': '9001', 'usercode': 'S9001', 'fullname': '供应商甲', 'bcategory': 1, 'deleted': False, 'isstop': False}]}
        if method == 'erp.stock.list':
            return {'stocks': [{'id': WAREHOUSE, 'usercode': '001', 'fullname': '江苏优螺'}]}
        if method == 'erp.ptype.list':
            return {'ptypes': [{'ptypeid': '1001', 'usercode': 'A', 'fullname': '螺栓', 'deleted': False, 'isstop': False,
                                'skus': [{'skuid': '2001', 'prop1': 0, 'prop2': 0}]}]}
        raise AssertionError(method)


def test_official_body_keeps_price_and_omits_audit_and_readback_fields():
    order = assemble_order(
        number='ULCG202610280001', when=datetime(2026, 10, 28, 9, 0, 0),
        delivery=datetime(2026, 10, 28, 9, 0, 0), total=__import__('decimal').Decimal('1.50'),
        warehouse_id=WAREHOUSE, supplier_id='9001', employee_id='77', summary='测试',
        comment='测试单-可删除', product={'ptypeid': '1001', 'fullname': '螺栓'},
        sku={'prop1': 0, 'prop2': 0}, quantity=__import__('decimal').Decimal('1'),
        unit_price=__import__('decimal').Decimal('1.50'), tax=__import__('decimal').Decimal('0'))
    body = json.loads(wire_purchaseorder(order))
    assert body['number'] == 'ULCG202610280001'
    assert isinstance(body['btypeid'], int) and isinstance(body['details'][0]['ptypeid'], int)
    assert body['details'][0]['assdpprice'] == 1.5 and body['details'][0]['tax'] == 0
    assert body['details'][0]['assqty'] == 1 and body['total'] == 1.5
    assert 'draft' not in body and 'ischeck' not in body and 'settleaccounts' not in body
    assert READBACK_METHOD is None and len(OFFICIAL_CONTRACT_HASH) == 64


def test_workspace_tax_basis_becomes_untaxed_unit_and_thirteen_percent():
    from decimal import Decimal
    unit, gross, tax = erp_line_prices('1', '1.13', '含税')
    assert (unit, gross, tax) == (Decimal('1.000'), Decimal('1.13'), Decimal('13'))
    unit, gross, tax = erp_line_prices('20', '2.5', '含税')
    assert (unit, gross, tax) == (Decimal('2.212'), Decimal('49.99'), Decimal('13'))
    unit, gross, tax = erp_line_prices('20', '2.5', '未税')
    assert (unit, gross, tax) == (Decimal('2.500'), Decimal('56.50'), Decimal('13'))
    unit, gross, tax = erp_line_prices('22', '2.771', '含税')
    assert unit == Decimal('2.452') and unit.as_tuple().exponent == -3 and tax == Decimal('13')


def _vault(tmp_path):
    path = tmp_path / 'erp.json'
    path.write_text(json.dumps({'credentials': {'appkey': '1' * 32, 'appsecret': 'A' * 32, 'signkey': 'sign'},
        'token': {'appkey': '1' * 32, 'profileid': '42', 'employee_id': '77', 'auth_token': 'secret',
                  'expires_at': time.time() + 3600}}))
    path.chmod(0o600)
    return path


def test_purchase_write_is_off_until_allowed_and_does_not_retry(tmp_path):
    path = _vault(tmp_path)
    calls = []

    def transport(endpoint, payload):
        calls.append(payload['token'])
        raise ApiError('account', 15)

    client = ErpClient(path, transport=transport)
    with pytest.raises(ErpUnavailable):
        client.call(PURCHASE_ORDER_ADD_METHOD, {'purchaseorder': '{}'})
    assert calls == []
    client.allow_purchase_order = True
    with pytest.raises(ErpUnavailable) as caught:
        client.call(PURCHASE_ORDER_ADD_METHOD, {'purchaseorder': '{}'})
    assert calls == ['secret'] and caught.value.code == 15
    assert client.identity() == {'profile_id': '42', 'employee_id': '77'}


def _numeric_item():
    item = inventory()
    item['product_id'] = '1001'
    item['sku_id'] = '2001'
    row = receipt(item, '9001', '2026-10-01')
    row['party'] = '供应商甲'
    return item, row


def test_workspace_write_uses_receipt_and_never_sends_twice(store):
    item, row = _numeric_item()
    seed(store, [item], [row])
    catalog = Catalog()
    catalog.allow_purchase_order = True
    policy = PurchaseSubmissionPolicy(True, True, 'P', 'P')
    from core.purchase_erp_adapter import PurchaseOrderDraftAdapter
    adapter = PurchaseOrderDraftAdapter(catalog, profile_id='P', account_id='P', employee_id='77', clock=lambda: NOW)
    submission = PurchaseSubmission(store, adapter, policy, clock=lambda: NOW)
    engine = PurchaseWorkspace(store, submission)
    listing, item_row, alloc = order_line(engine)
    result = engine.submit_erp_draft(ACTOR, draft_body(listing, item_row, alloc))
    assert result['status'] == 'awaiting_human_check'
    assert result['erp_order_no'].startswith('ULP') and result['erp_line_no'] == '321'
    assert '人工' in result['message'] or '核对' in result['message']
    assert catalog.calls.count(PURCHASE_ORDER_ADD_METHOD) == 1
    sent = json.loads(catalog.bodies[0])
    assert sent['btypeid'] == 9001 and sent['details'][0]['ptypeid'] == 1001
    assert 'draft' not in sent and 'ischeck' not in sent
    assert sent['details'][0]['tax'] == 13
    assert sent['details'][0]['assdpprice'] == 2.212 and sent['details'][0]['asstpprice'] == 2.212
    assert sent['total'] == 49.99
    kept = engine.detail(ACTOR, item_row['id'])['allocations'][0]
    assert kept['state'] == 'ordered'
    assert kept['details']['erp_draft_status'] == 'pending_human_check'
    assert kept['details']['erp_submit_status'] == 'awaiting_human_check'
    again = engine.submit_erp_draft(ACTOR, draft_body(engine.listing(ACTOR, {}), item_row, alloc, idempotency_key='again-key-001'))
    assert again['status'] == 'awaiting_human_check' and catalog.calls.count(PURCHASE_ORDER_ADD_METHOD) == 1
    restarted = PurchaseOrderDraftAdapter(Catalog(), profile_id='P', account_id='P', employee_id='77', clock=lambda: NOW)
    workspace = PurchaseWorkspace(store, PurchaseSubmission(store, restarted, policy, clock=lambda: NOW))
    replay = workspace.submit_erp_draft(ACTOR, draft_body(workspace.listing(ACTOR, {}), item_row, alloc, idempotency_key='restart-key-01'))
    assert replay['erp_order_no'] == result['erp_order_no'] and restarted.client.calls == []


def test_missing_vchcode_does_not_send_a_second_order(store):
    item, row = _numeric_item()
    seed(store, [item], [row])
    catalog = Catalog(response={'vchcode': 0})
    catalog.allow_purchase_order = True
    policy = PurchaseSubmissionPolicy(True, True, 'P', 'P')
    from core.purchase_erp_adapter import PurchaseOrderDraftAdapter
    adapter = PurchaseOrderDraftAdapter(catalog, profile_id='P', account_id='P', employee_id='77', clock=lambda: NOW)
    engine = PurchaseWorkspace(store, PurchaseSubmission(store, adapter, policy, clock=lambda: NOW))
    listing, item_row, alloc = order_line(engine)
    result = engine.submit_erp_draft(ACTOR, draft_body(listing, item_row, alloc))
    assert result['status'] == 'submission_unknown' and result['detail'] == 'purchase_write_rejected'
    assert 'ULP' in result['message'] and catalog.calls.count(PURCHASE_ORDER_ADD_METHOD) == 1
    again = engine.submit_erp_draft(ACTOR, draft_body(engine.listing(ACTOR, {}), item_row, alloc, idempotency_key='reject-key-001'))
    assert again['status'] == 'submission_unknown' and catalog.calls.count(PURCHASE_ORDER_ADD_METHOD) == 1


def _args(**extra):
    base = ['--supplier-code', 'S9001', '--product-code', 'A', '--warehouse-code', '001',
            '--quantity', '1', '--unit-price', '1.50', '--remark', '测试单-可删除']
    for key, value in extra.items():
        base.extend(['--' + key.replace('_', '-'), value] if value is not True else ['--' + key.replace('_', '-')])
    return base


def test_smoke_script_is_dry_run_until_number_and_confirm(tmp_path, capsys):
    catalog = Catalog()

    def factory(allow):
        return catalog

    assert run(_args(), factory, lock_path=str(tmp_path / 'lock')) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['dry_run'] is True and printed['method'] == PURCHASE_ORDER_ADD_METHOD
    assert printed['business_body']['purchaseorder']['details'][0]['assqty'] == 1
    assert printed['business_body']['purchaseorder']['details'][0]['tax'] == 13
    assert printed['business_body']['purchaseorder']['details'][0]['assdpprice'] == 1.5
    assert 'appkey' not in printed['business_body']['purchaseorder']
    assert 'secret' not in json.dumps(printed['business_body'])
    assert PURCHASE_ORDER_ADD_METHOD not in catalog.calls
    assert run(_args(confirm_write=True), factory, lock_path=str(tmp_path / 'lock')) == 2
    assert PURCHASE_ORDER_ADD_METHOD not in catalog.calls
    assert run(_args(quantity='2'), factory, lock_path=str(tmp_path / 'lock2')) == 1
    assert run(_args(remark='普通备注'), factory, lock_path=str(tmp_path / 'lock3')) == 1
    assert PURCHASE_ORDER_ADD_METHOD not in catalog.calls


def test_smoke_script_sends_once_and_reports_no_readback(tmp_path, capsys):
    catalog = Catalog()
    lock = tmp_path / 'once.lock'
    code = run(_args(confirm_write=True, number='ULCG202610280001'), lambda allow: catalog, lock_path=str(lock))
    assert code == 0
    text = capsys.readouterr().out
    decoder, chunks, cursor = json.JSONDecoder(), [], 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        value, cursor = decoder.raw_decode(text, cursor)
        chunks.append(value)
    assert chunks[-1]['sent'] is True and chunks[-1]['vchcode'] == '321'
    assert chunks[-1]['number'] == 'ULCG202610280001' and chunks[-1]['readback_method'] is None
    assert catalog.calls.count(PURCHASE_ORDER_ADD_METHOD) == 1
    again = run(_args(confirm_write=True, number='ULCG202610280001'), lambda allow: catalog, lock_path=str(lock))
    assert again == 1 and catalog.calls.count(PURCHASE_ORDER_ADD_METHOD) == 1


def test_attach_requires_flags_and_account_pin(monkeypatch):
    submission = PurchaseSubmission.__new__(PurchaseSubmission)
    submission.policy = PurchaseSubmissionPolicy()
    submission.adapter = None
    assert attach_purchase_adapter(submission, Catalog()) is False
    submission.policy = PurchaseSubmissionPolicy(True, True, '42', '42')
    client = Catalog()
    client.allow_purchase_order = False
    client.identity = lambda: {'profile_id': '42', 'employee_id': '77'}
    assert attach_purchase_adapter(submission, client) is False
    client.allow_purchase_order = True
    assert attach_purchase_adapter(submission, client) is True
    assert submission.adapter.capabilities.acceptance_basis == 'write_receipt_human_check'
    assert submission.adapter.capabilities.creates_unapproved_draft is False
