"""Purchase ERP draft submission. Fake adapters only: no network, payment, or audit."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import uuid

import pytest

from core.purchase_submission import (MESSAGES, PurchaseAdapterCapabilities, PurchaseDraftLookup,
    PurchaseDraftRequest, PurchaseSubmission, PurchaseSubmissionBlocked, PurchaseSubmissionPolicy,
    PreparedPurchaseDraft, ErpPurchaseDraft, _hash, policy_from_environ)
from core.purchase_workspace import PurchaseConflict, PurchaseWorkspace
from core.purchase_workspace_data import refresh_index
from test_purchase_workspace import ACTOR, inventory, receipt, seed
from test_daily_work import store, user  # noqa: F401  (pytest fixture)

NOW = datetime(2026, 10, 28, 1, tzinfo=timezone.utc).timestamp()


class FakeAdapter:
    def __init__(self):
        self.capabilities = PurchaseAdapterCapabilities(
            'P', 'acct', 'erp.purchaseorder.add', 'a' * 64,
            True, True, True, True, True, True, True, False, False)
        self.inspections = self.lookups = self.writes = 0
        self.orders = []
        self.lines = set()
        self.submit_mode = 'success'
        self.prefetch = False
        self.lookup_error = False
        self.store = None

    def _make(self, intent, quantity=None):
        line = intent.request.line if quantity is None else replace(intent.request.line, quantity=quantity)
        return ErpPurchaseDraft('CG-ERP-1', '1', intent.request.profile_id, intent.request.account_id,
            intent.request.external_reference, intent.request.supplier_erp_id, 'unapproved', False,
            line, NOW, 'f' * 64)

    def inspect(self, request):
        self.inspections += 1
        return PreparedPurchaseDraft(_hash(asdict(request)), request.profile_id, request.account_id,
            request.external_reference, request.supplier_erp_id, request.audit_status, True, request.line,
            'b' * 64, 'a' * 64, NOW, 'e' * 64)

    def lookup(self, intent):
        self.lookups += 1
        if self.lookup_error:
            raise TimeoutError('lookup failed')
        matched = [order for order in self.orders if order.external_reference == intent.request.external_reference]
        if self.prefetch and not matched:
            matched = [self._make(intent)]
            self.orders = matched
        return PurchaseDraftLookup(True, 'P', 'acct', intent.request.external_reference, tuple(matched))

    def submit_once(self, intent):
        self.writes += 1
        with self.store.connect() as db:
            assert db.execute('SELECT count(*) FROM purchase_submission_intents').fetchone()[0] >= 1
        key = (intent.request.line.supplier_id, intent.request.line.platform_order_no, intent.request.line.platform_line_no)
        if key in self.lines:
            raise RuntimeError('duplicate erp line')
        if self.submit_mode == 'crash_before_write':
            raise SystemExit('power loss')
        if self.submit_mode != 'timeout_no_order':
            quantity = '19' if self.submit_mode == 'mismatch' else None
            self.orders.append(self._make(intent, quantity))
            self.lines.add(key)
        if self.submit_mode == 'crash_after_write':
            raise SystemExit('power loss')
        if str(self.submit_mode).startswith('timeout'):
            raise TimeoutError('unknown outcome')


def policy_on():
    return PurchaseSubmissionPolicy(True, True, 'P', 'acct')


def make(store, policy=None, adapter=None):
    adapter = adapter or FakeAdapter()
    submission = PurchaseSubmission(store, adapter, policy if policy is not None else policy_on(), clock=lambda: NOW)
    adapter.store = store
    return PurchaseWorkspace(store, submission), adapter


def prepare(store, codes=('A',)):
    items = [inventory(code) for code in codes]
    history = [receipt(item, 'S', '2026-10-01') for item in items]
    seed(store, items, history)
    return history


def order_line(engine, sku='A', order_no='PO-1'):
    listing = engine.listing(ACTOR, {})
    item = next(row for row in listing['items'] if row['sku'] == sku)
    engine.mutate(ACTOR, {'action': 'review_ack', 'source_token': listing['source_token'],
        'items': [{'id': item['id'], 'version': item['version']}], 'idempotency_key': str(uuid.uuid4()),
        'note': '已核对需求'})
    listing = engine.listing(ACTOR, {})
    item = next(row for row in listing['items'] if row['sku'] == sku)
    alloc = item['allocations'][0]
    engine.mutate(ACTOR, {'action': 'record_purchase', 'source_token': listing['source_token'],
        'items': [{'id': item['id'], 'version': item['version']}], 'idempotency_key': str(uuid.uuid4()),
        'allocation_id': alloc['id'], 'quantity': '20', 'amount': '50', 'tax_basis': '含税',
        'platform_order_no': order_no})
    listing = engine.listing(ACTOR, {})
    item = next(row for row in listing['items'] if row['sku'] == sku)
    return listing, item, item['allocations'][0]


def draft_body(listing, item, alloc, **extra):
    return {'idempotency_key': str(uuid.uuid4()), 'allocation_id': alloc['id'], 'item_id': item['id'],
            'version': item['version'], 'source_token': listing['source_token'], 'draft_confirmed': True, **extra}


def allocation(engine, item_id):
    return engine.detail(ACTOR, item_id)['allocations'][0]


def test_env_flags_require_both_exact_ones(monkeypatch):
    monkeypatch.delenv('UL_PURCHASE_ERP_SUBMIT', raising=False)
    monkeypatch.delenv('UL_PURCHASE_ERP_ALLOW_SUBMIT', raising=False)
    assert policy_from_environ().enabled is False and policy_from_environ().allow_submit is False
    monkeypatch.setenv('UL_PURCHASE_ERP_SUBMIT', '1')
    assert policy_from_environ().enabled is False
    monkeypatch.setenv('UL_PURCHASE_ERP_ALLOW_SUBMIT', 'true')
    assert policy_from_environ().enabled is False
    monkeypatch.setenv('UL_PURCHASE_ERP_ALLOW_SUBMIT', '1')
    opened = policy_from_environ()
    assert opened.enabled is True and opened.allow_submit is True and opened.audit_status == 'unapproved'


def test_flag_off_blocks_write_and_keeps_local_record(store):
    prepare(store)
    engine, adapter = make(store, policy=PurchaseSubmissionPolicy())
    assert engine.erp_submit_status()['erp_submit_enabled'] is False
    assert engine.erp_submit_status()['erp_draft_ready'] is False
    listing, item, alloc = order_line(engine)
    assert alloc['state'] == 'ordered'
    with pytest.raises(PurchaseSubmissionBlocked) as error:
        engine.submit_erp_draft(ACTOR, draft_body(listing, item, alloc))
    assert error.value.code == 'purchase_submission_disabled'
    assert error.value.message == MESSAGES['purchase_submission_disabled']
    assert adapter.inspections == adapter.lookups == adapter.writes == 0
    kept = allocation(engine, item['id'])
    assert kept['state'] == 'ordered' and 'erp_draft_no' not in kept['details'] and 'erp_submit_status' not in kept['details']
    with engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM purchase_submission_attempts').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM purchase_submission_intents').fetchone()[0] == 0


def test_flag_on_without_adapter_does_not_pretend_success(store):
    prepare(store)
    submission = PurchaseSubmission(store, None, policy_on(), clock=lambda: NOW)
    engine = PurchaseWorkspace(store, submission)
    listing, item, alloc = order_line(engine)
    result = engine.submit_erp_draft(ACTOR, draft_body(listing, item, alloc))
    assert result['status'] == 'blocked' and result['detail'] == 'purchase_adapter_unavailable'
    assert result['erp_order_no'] == '' and result['erp_counted'] is False
    kept = allocation(engine, item['id'])
    assert kept['state'] == 'ordered' and 'erp_draft_no' not in kept['details']
    with engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM purchase_submission_intents').fetchone()[0] == 0
    status = engine.erp_submit_status()
    assert status['erp_submit_enabled'] is True and status['erp_draft_ready'] is False


def test_mock_records_intent_replays_and_does_not_count_or_pay(store):
    history = prepare(store)
    engine, adapter = make(store)
    listing, item, alloc = order_line(engine)
    body = draft_body(listing, item, alloc)
    result = engine.submit_erp_draft(ACTOR, body)
    assert result['status'] == 'submitted' and result['erp_order_no'] == 'CG-ERP-1' and result['erp_line_no'] == '1'
    assert result['replayed'] is False and adapter.writes == 1 and len(adapter.lines) == 1
    again = engine.submit_erp_draft(ACTOR, body)
    assert again['replayed'] is True and again['erp_order_no'] == 'CG-ERP-1' and adapter.writes == 1
    other = engine.submit_erp_draft(ACTOR, draft_body(engine.listing(ACTOR, {}), item, alloc, idempotency_key='second-key-01'))
    assert other['status'] == 'submitted' and other['replayed'] is False and adapter.writes == 1
    with pytest.raises(PurchaseSubmissionBlocked) as error:
        engine.submit_erp_draft(ACTOR, {**body, 'version': body['version'] + 1})
    assert error.value.code == 'purchase_submission_idempotency_conflict'
    kept = allocation(engine, item['id'])
    assert kept['state'] == 'ordered'
    assert kept['details']['erp_draft_no'] == 'CG-ERP-1' and kept['details']['erp_draft_status'] == 'unapproved'
    assert kept['details']['erp_submit_status'] == 'submitted'
    assert 'erp_counted_method' not in kept['details'] and 'erp_order_no' not in kept['details']
    with engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM purchase_submission_intents').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM purchase_submission_bindings').fetchone()[0] == 1
    fresh = engine.listing(ACTOR, {})
    current = next(row for row in fresh['items'] if row['id'] == item['id'])
    with pytest.raises(PurchaseConflict, match='刷新'):
        engine.mutate(ACTOR, {'action': 'confirm_erp_counted', 'source_token': fresh['source_token'],
            'items': [{'id': current['id'], 'version': current['version']}], 'idempotency_key': str(uuid.uuid4()),
            'allocation_id': alloc['id'], 'erp_order_no': 'CG-ERP-1', 'erp_line_no': '1',
            'erp_quantity': '20', 'erp_counted_confirmed': True})
    assert allocation(engine, item['id'])['state'] == 'ordered'
    seed(store, [inventory(available='-10')], history, version='2')
    refresh_index(store)
    fresh = engine.listing(ACTOR, {})
    current = next(row for row in fresh['items'] if row['sku'] == 'A')
    engine.mutate(ACTOR, {'action': 'confirm_erp_counted', 'source_token': fresh['source_token'],
        'items': [{'id': current['id'], 'version': current['version']}], 'idempotency_key': str(uuid.uuid4()),
        'allocation_id': alloc['id'], 'erp_order_no': 'AUD-1', 'erp_line_no': '1',
        'erp_quantity': '20', 'erp_counted_confirmed': True})
    counted = allocation(engine, item['id'])
    assert counted['state'] == 'erp_counted' and counted['details']['erp_counted_method'] == 'human_verified'
    assert counted['details']['erp_order_no'] == 'AUD-1' and counted['details']['erp_draft_no'] == 'CG-ERP-1'
    assert adapter.writes == 1


def test_unknown_readback_never_submits_twice_even_if_flag_turns_off(store):
    prepare(store)
    engine, adapter = make(store)
    adapter.submit_mode = 'timeout_no_order'
    listing, item, alloc = order_line(engine)
    body = draft_body(listing, item, alloc)
    result = engine.submit_erp_draft(ACTOR, body)
    assert result['status'] == 'submission_unknown' and result['detail'] == 'purchase_readback_not_found'
    assert adapter.writes == 1 and result['erp_order_no'] == ''
    kept = allocation(engine, item['id'])
    assert kept['state'] == 'ordered' and kept['details']['erp_submit_status'] == 'submission_unknown'
    assert 'erp_draft_no' not in kept['details']
    resumed = PurchaseSubmission(store, adapter, PurchaseSubmissionPolicy(), clock=lambda: NOW)
    workspace = PurchaseWorkspace(store, resumed)
    again = workspace.submit_erp_draft(ACTOR, draft_body(engine.listing(ACTOR, {}), item, alloc, idempotency_key='recover-key-1'))
    assert again['status'] == 'submission_unknown' and adapter.writes == 1 and adapter.inspections == 1


@pytest.mark.parametrize('mode,status', [('timeout_after_write', 'submitted'), ('mismatch', 'submission_unknown')])
def test_readback_after_one_write_does_not_send_a_second_line(store, mode, status):
    prepare(store)
    engine, adapter = make(store)
    adapter.submit_mode = mode
    listing, item, alloc = order_line(engine)
    result = engine.submit_erp_draft(ACTOR, draft_body(listing, item, alloc))
    assert result['status'] == status and adapter.writes == 1 and len(adapter.lines) == 1
    again = engine.submit_erp_draft(ACTOR, draft_body(engine.listing(ACTOR, {}), item, alloc, idempotency_key='again-key-001'))
    assert again['status'] == status and adapter.writes == 1
    assert allocation(engine, item['id'])['state'] == 'ordered'


@pytest.mark.parametrize('mode,status', [('crash_before_write', 'submission_unknown'), ('crash_after_write', 'submitted')])
def test_power_loss_leaves_intent_that_can_only_be_looked_up(store, mode, status):
    prepare(store)
    engine, adapter = make(store)
    adapter.submit_mode = mode
    listing, item, alloc = order_line(engine)
    body = draft_body(listing, item, alloc)
    with pytest.raises(SystemExit):
        engine.submit_erp_draft(ACTOR, body)
    restarted = PurchaseSubmission(store, adapter, policy_on(), clock=lambda: NOW)
    workspace = PurchaseWorkspace(store, restarted)
    assert workspace.submit_erp_draft(ACTOR, body)['status'] == status
    assert adapter.writes == 1 and adapter.inspections == 1


def test_same_platform_line_cannot_create_a_second_erp_line(store):
    prepare(store, ('A', 'B'))
    engine, adapter = make(store)
    first_listing, first, first_alloc = order_line(engine, 'A', 'PO-9')
    _, second, second_alloc = order_line(engine, 'B', 'PO-9')
    assert first_alloc['details']['platform_line_no'] != second_alloc['details']['platform_line_no']
    with engine.store.connect(write=True) as db:
        details = json.loads(db.execute('SELECT details FROM purchase_workspace_allocations WHERE id=?', (second_alloc['id'],)).fetchone()[0])
        details['platform_line_no'] = first_alloc['details']['platform_line_no']
        db.execute('UPDATE purchase_workspace_allocations SET details=? WHERE id=?', (json.dumps(details, ensure_ascii=False), second_alloc['id']))
    result = engine.submit_erp_draft(ACTOR, draft_body(first_listing, first, first_alloc))
    assert result['status'] == 'submitted' and adapter.writes == 1
    listing = engine.listing(ACTOR, {})
    second = next(row for row in listing['items'] if row['sku'] == 'B')
    second_alloc = next(row for row in second['allocations'] if row['id'] == second_alloc['id'])
    blocked = engine.submit_erp_draft(ACTOR, draft_body(listing, second, second_alloc))
    assert blocked['status'] == 'blocked' and blocked['detail'] == 'purchase_duplicate_line'
    assert adapter.writes == 1 and len(adapter.lines) == 1
    replay = engine.submit_erp_draft(ACTOR, draft_body(listing, second, second_alloc, idempotency_key='dup-replay-01'))
    assert replay['status'] == 'blocked' and replay['detail'] == 'purchase_duplicate_line'
    assert adapter.writes == 1 and adapter.inspections == 2
    other = allocation(engine, second['id'])
    assert other['state'] == 'ordered' and 'erp_draft_no' not in other['details']


def test_existing_draft_is_reused_without_another_write(store):
    prepare(store)
    engine, adapter = make(store)
    adapter.prefetch = True
    listing, item, alloc = order_line(engine)
    result = engine.submit_erp_draft(ACTOR, draft_body(listing, item, alloc))
    assert result['status'] == 'submitted' and result['erp_order_no'] == 'CG-ERP-1'
    assert adapter.writes == 0 and adapter.lookups == 1
    assert allocation(engine, item['id'])['details']['erp_draft_no'] == 'CG-ERP-1'
    assert allocation(engine, item['id'])['state'] == 'ordered'


def test_payment_or_unverified_contract_never_calls_submit(store):
    prepare(store)
    engine, adapter = make(store)
    adapter.capabilities = replace(adapter.capabilities, allows_payment=True)
    listing, item, alloc = order_line(engine)
    result = engine.submit_erp_draft(ACTOR, draft_body(listing, item, alloc))
    assert result['status'] == 'blocked' and result['detail'] == 'purchase_payment_or_audit_forbidden'
    assert adapter.inspections == adapter.writes == 0
    adapter.capabilities = replace(adapter.capabilities, allows_payment=False, write_contract_verified=False)
    again = engine.submit_erp_draft(ACTOR, draft_body(engine.listing(ACTOR, {}), item, alloc, idempotency_key='contract-key-1'))
    assert again['status'] == 'blocked' and again['detail'] == 'purchase_write_contract_unverified'
    assert adapter.writes == 0
    assert allocation(engine, item['id'])['state'] == 'ordered'


def test_http_bootstrap_and_draft_route_stay_off(store):
    from types import SimpleNamespace
    from app import create_app
    from werkzeug.security import generate_password_hash
    with store.connect(write=True) as db:
        db.execute('UPDATE staff_users SET password_hash=?', (generate_password_hash('test-only'),))
    app = create_app({'TESTING': True, 'AUTO_IMPORT': False, 'SOURCE_MODE': 'api', 'PUBLIC_MODE': True,
        'ERP_CLIENT': SimpleNamespace(last_auth_error=None), 'API_SOURCE': object(),
        'ADMIN_PASSWORD_HASH': generate_password_hash('test-only'), 'PURCHASE_WEB_ORIGIN': 'http://localhost'},
        runtime_dir=store.runtime)
    client = app.test_client()
    login = client.post('/api/v2/purchase-workspace/login', json={'username': 'buyer', 'password': 'test-only'},
                        headers={'Origin': 'http://localhost'})
    assert login.status_code == 200
    boot = client.get('/api/v2/purchase-workspace/bootstrap')
    assert boot.json['erp_submit_enabled'] is False and boot.json['erp_draft_ready'] is False
    assert boot.json['browser_enabled'] is False and '暂未开放' in boot.json['message']
    denied = client.post('/api/v2/purchase-workspace/erp-draft', json={
        'idempotency_key': 'route-key-01', 'allocation_id': 'missing', 'item_id': 'missing',
        'version': 1, 'source_token': 'tok', 'draft_confirmed': True},
        headers={'Origin': 'http://localhost', 'X-CSRF-Token': login.json['csrf']})
    assert denied.status_code == 409 and denied.json['error'] == 'purchase_submission_disabled'
    assert '不会写 ERP' in denied.json['message']


def test_draft_confirmation_and_role_are_required(store):
    prepare(store)
    engine, adapter = make(store)
    listing, item, alloc = order_line(engine)
    with pytest.raises(PurchaseSubmissionBlocked) as error:
        engine.submit_erp_draft(ACTOR, draft_body(listing, item, alloc, draft_confirmed=False))
    assert error.value.code == 'purchase_draft_confirmation_required'
    with pytest.raises(PurchaseConflict):
        engine.submit_erp_draft(user('packer', ['stock_packing']), draft_body(listing, item, alloc))
    assert adapter.writes == 0
    with engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM purchase_submission_attempts').fetchone()[0] == 0


def test_saved_uneven_amount_is_sent_without_retyping_quantity(store):
    """22 支 at 含税 ¥60.97. The confirm box does not send quantity or amount again.

    60.97 / 22 repeats for 27 places. Lines recorded before the price was rounded
    still have that full string, and the checker used to reject it.
    """
    from decimal import Decimal
    from core.purchase_submission import saved_deal
    from test_purchase_workspace import inventory, receipt, seed
    product = inventory(available='-22')
    seed(store, [product], [receipt(product, 'S', '2026-10-01')])
    engine, adapter = make(store)
    listing = engine.listing(ACTOR, {})
    item = listing['items'][0]
    engine.mutate(ACTOR, {'action': 'review_ack', 'source_token': listing['source_token'],
        'items': [{'id': item['id'], 'version': item['version']}], 'idempotency_key': str(uuid.uuid4()),
        'note': '已核对需求'})
    listing = engine.listing(ACTOR, {})
    item = next(row for row in listing['items'] if row['sku'] == 'A')
    alloc = item['allocations'][0]
    engine.mutate(ACTOR, {'action': 'record_purchase', 'source_token': listing['source_token'],
        'items': [{'id': item['id'], 'version': item['version']}], 'idempotency_key': str(uuid.uuid4()),
        'allocation_id': alloc['id'], 'quantity': '22', 'amount': '60.97', 'tax_basis': '含税',
        'platform_order_no': 'CG20261029001'})
    listing = engine.listing(ACTOR, {})
    item = next(row for row in listing['items'] if row['sku'] == 'A')
    alloc = item['allocations'][0]
    assert Decimal(alloc['quantity']) == Decimal('22')
    assert Decimal(alloc['details']['amount']) == Decimal('60.97')
    assert Decimal(alloc['details']['unit_price']).as_tuple().exponent >= -12
    raw = format((Decimal('60.97') / Decimal('22')).normalize(), 'f')
    assert Decimal(raw).as_tuple().exponent < -12
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE purchase_workspace_allocations SET details=json_set(details, '$.unit_price', ?) WHERE id=?",
                   (raw, alloc['id']))
    body = draft_body(listing, item, alloc)
    assert 'quantity' not in body and 'amount' not in body
    qty, price, money = saved_deal('22', '60.97', raw)
    assert Decimal(qty) == Decimal('22') and Decimal(money) == Decimal('60.97')
    assert abs(Decimal(price) * Decimal(qty) - Decimal(money)) <= Decimal('0.01')
    result = engine.submit_erp_draft(ACTOR, body)
    assert result['detail'] != 'purchase_number_invalid'
    assert result['status'] == 'submitted' and adapter.writes == 1
    with engine.store.connect() as db:
        payload = json.loads(db.execute('SELECT payload FROM purchase_submission_intents').fetchone()[0])
    line = payload['request']['line']
    assert line['quantity'] == '22' and line['amount'] == '60.97'
    assert line['tax_basis'] == '含税' and line['platform_order_no'] == 'CG20261029001'
    assert abs(Decimal(line['unit_price']) * Decimal('22') - Decimal('60.97')) <= Decimal('0.01')
