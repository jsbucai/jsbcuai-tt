import copy
import json
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.api_source import publish
from core.classification_pipeline import calculate_query_snapshot
from core.order_lifecycle import (OrderLifecycle, SHIPPED_UNPOSTED_NAME,
                                  PENDING_REVIEW_NAME, import_delivery_dates,
                                  release_receipt_orders)
from core.store import Store
from core.update_jobs import UpdateJobs


KEY = dict(profile_id='1', warehouse_id='2', product_id='3', sku_id='4',
           base_unit='1')


def raw(number, process):
    return {
        'trade_order': {'trade_id': number, 'extendProps': {'process_state': process}},
        'order_info': {'order_info': [{'recid':'1','spec_code':'LS01',
                                      'goods_name':'测试商品','sell_count':'5'}]},
    }


def order(number='A', *, day='2026-09-01', lifecycle=None):
    value = {
        'order_no': number, 'order_date': day, 'delivery_date': '2026-10-01',
        'lines': [{**KEY, 'line_id':'1', 'product_code':'LS01', 'open_qty':'5',
                   'receipt_evidence_complete':True,
                   'expedite_evidence_complete':True,
                   'allocated_receipts':[], 'expedite_required':False}],
    }
    if lifecycle:
        value.update(lifecycle)
    return value


def record(number='A', lifecycle=None):
    value = {**KEY, 'id':number + ':1', 'line_id':'1', 'order_no':number,
             'sku':'LS01', 'product_name':'测试商品', 'quantity':'5',
             'summary':'PO#P#1#D#测试商品#M10#8.8#锌#5#LS01#交期=2026-10-01',
             'document_date':'2026-09-01'}
    if lifecycle:
        value.update(lifecycle)
    return value


class Client:
    def __init__(self):
        self.exact = {}
        self.freights = [{'fullname':SHIPPED_UNPOSTED_NAME,'usercode':'settle'}]
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'erp.freight.list':
            return {'freights':copy.deepcopy(self.freights)}
        if method == 'qimen.taobao.erp.order.sync':
            number = params['extendProps']['trade_id']
            rows = self.exact.get(number, [])
            return {'code':'0', 'TotalResults':len(rows),
                    'trade_orders':{'trade_orders':copy.deepcopy(rows)}}
        raise AssertionError(method)


@pytest.fixture
def store():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / 'runtime', prefix='lifecycle-') as path:
        yield Store(path)


def persist(store, stage):
    with store.connect(write=True) as db:
        OrderLifecycle.persist(db, stage)


def seed_active(lifecycle, client, store):
    source, stage = lifecycle.prepare(client, '1', [raw('A', 400)],
                                      '2026-10-01T08:00:00+08:00')
    assert len(source) == 1
    orders, rows, stage = lifecycle.finalize(stage, [order()], [record()])
    persist(store, stage)
    return orders, rows


def test_status500_requires_two_observations_then_600_archives(store):
    client = Client(); lifecycle = OrderLifecycle(store)
    seed_active(lifecycle, client, store)

    source, first = lifecycle.prepare(client, '1', [], '2026-10-01T09:00:00+08:00',
                                      shipped_rows=[raw('A', 500)])
    assert source == []
    orders, rows, first = lifecycle.finalize(first, [], [])
    assert orders[0]['shadow_demand'] is True
    assert orders[0]['lifecycle_state'] == 'candidate'
    assert rows[0]['print_blocked'] is True
    assert first['freight']['available'] is False
    persist(store, first)

    _, second = lifecycle.prepare(client, '1', [], '2026-10-01T10:00:00+08:00',
                                  shipped_rows=[raw('A', 500)])
    orders, _, second = lifecycle.finalize(second, [], [])
    assert orders[0]['lifecycle_state'] == 'shipped_unposted'
    assert orders[0]['lifecycle_confirmations'] == 2
    assert orders[0]['lifecycle_write_ready'] is True
    assert second['freight'] == {'code':'settle','available':True,'reason':''}
    persist(store, second)

    client.exact['A'] = [raw('A', 600)]
    source, pending = lifecycle.prepare(client, '1', [], '2026-10-01T11:00:00+08:00',
                                       shipped_rows=[])
    assert source == [] and pending['shadow_order_ids'] == ['A']
    assert pending['release_order_ids'] == []
    pending_orders, pending_rows, pending = lifecycle.finalize(pending, [], [])
    assert pending_orders[0]['lifecycle_state'] == 'release_pending'
    assert pending_orders[0]['lifecycle_pending_review'] is True
    assert pending_rows[0]['status'] == PENDING_REVIEW_NAME
    persist(store, pending)

    source, third = lifecycle.prepare(client, '1', [], '2026-10-01T12:00:00+08:00',
                                      shipped_rows=[])
    assert source == [] and third['shadow_order_ids'] == []
    assert third['release_order_ids'] == ['A']
    exact = next(params for method, params in client.calls
                 if method == 'qimen.taobao.erp.order.sync')
    assert exact['extendProps']['bill_status'] == '1,2,3,4,5,6,7'
    assert exact['extendProps']['trade_id'] == 'A'
    persist(store, third)
    with store.connect() as db:
        state = db.execute("SELECT lifecycle_state,process_state,confirmation_count "
                           "FROM order_lifecycle_state WHERE order_no='A'").fetchone()
        transitions = [tuple(row) for row in db.execute(
            "SELECT from_state,to_state FROM order_lifecycle_audit "
            "WHERE order_no='A' ORDER BY rowid")]
    assert tuple(state) == ('archived','600',2)
    assert transitions == [('', 'active'), ('active','candidate'),
                           ('candidate','shipped_unposted'),
                           ('shipped_unposted','release_pending'),
                           ('release_pending','archived')]


def test_release_pending_returns_to_confirmed_500_without_restarting_count(store):
    client = Client(); lifecycle = OrderLifecycle(store)
    seed_active(lifecycle, client, store)
    for hour in (9, 10):
        _, stage = lifecycle.prepare(client, '1', [],
            f'2026-10-01T{hour:02d}:00:00+08:00', shipped_rows=[raw('A', 500)])
        _, _, stage = lifecycle.finalize(stage, [], [])
        persist(store, stage)
    client.exact['A'] = [raw('A', 600)]
    _, pending = lifecycle.prepare(client, '1', [], '2026-10-01T11:00:00+08:00')
    _, _, pending = lifecycle.finalize(pending, [], [])
    persist(store, pending)

    _, restored = lifecycle.prepare(client, '1', [], '2026-10-01T12:00:00+08:00',
                                    shipped_rows=[raw('A', 500)])
    orders, _, restored = lifecycle.finalize(restored, [], [])
    assert orders[0]['lifecycle_state'] == 'shipped_unposted'
    assert orders[0]['lifecycle_confirmations'] == 2
    assert restored['release_order_ids'] == []


def test_exact_query_failure_keeps_shadow_and_marks_review(store):
    client = Client(); lifecycle = OrderLifecycle(store)
    seed_active(lifecycle, client, store)
    _, first = lifecycle.prepare(client, '1', [], '2026-10-01T09:00:00+08:00',
                                 shipped_rows=[raw('A', 500)])
    _, _, first = lifecycle.finalize(first, [], [])
    persist(store, first)
    # A failed or malformed exact response cannot remove demand.
    original_call = client.call
    def malformed(method, params):
        if method == 'qimen.taobao.erp.order.sync':
            return {'code':'0','trade_orders':{'trade_orders':[]}}  # no explicit zero count
        return original_call(method, params)
    client.call = malformed
    _, failed = lifecycle.prepare(client, '1', [], '2026-10-01T10:00:00+08:00',
                                  shipped_rows=[])
    orders, rows, failed = lifecycle.finalize(failed, [], [])
    assert failed['release_order_ids'] == []
    assert orders[0]['shadow_demand'] is True
    assert orders[0]['lifecycle_state'] == 'candidate'
    assert orders[0]['lifecycle_pending_review'] is True
    assert rows[0]['status'] == '订单状态待核对'
    assert rows[0]['printable'] is False
    assert rows[0]['print_blocked'] is True
    assert rows[0]['print_block_reason'] == '订单状态待核对，暂停包装打印'
    result = calculate_query_snapshot(legacy_snapshot(orders))
    plan = result['plans'][0]
    assert plan['freightname'] == PENDING_REVIEW_NAME
    assert plan['freightcode'] == '' and plan['write_enabled'] is False


def test_verified_delivery_cache_is_atomic_and_backfills_historical_shadow(store):
    client = Client(); lifecycle = OrderLifecycle(store)
    seed_active(lifecycle, client, store)
    with store.connect() as db:
        cached = db.execute('''SELECT delivery_date,source
            FROM erp_order_delivery_cache WHERE profile_id='1' AND order_no='A' ''').fetchone()
    assert tuple(cached) == ('2026-10-01', 'active_wms_summary')

    result = import_delivery_dates(store, '1', {'HIST#1':'2026-09-30'},
                                   observed_at='2026-10-01T12:00:00+08:00')
    assert result['installed'] == 1
    _, stage = lifecycle.prepare(client, '1', [], '2026-10-01T13:00:00+08:00',
                                 shipped_rows=[raw('HIST#1', 500)])
    assert stage['delivery_date_hints']['HIST#1'] == '2026-09-30'
    with pytest.raises(ValueError, match='拒绝覆盖'):
        import_delivery_dates(store, '1', {'HIST#1':'2026-10-02'})


def legacy_snapshot(orders):
    total = sum(int(line['open_qty']) for order in orders for line in order['lines'])
    return {
        'query_complete':True, 'profile_id':'1',
        'orders':orders,
        'inventory':[{**KEY,'physical_qty':'5','saleable_qty':str(5-total)}],
        'calculation_issues':[],
        'lifecycle_freight':{'available':False,'code':'','reason':'字典缺失'},
    }


def test_shadow_demand_consumes_fifo_but_uses_lifecycle_override():
    shadow = order('A', day='2026-09-01', lifecycle={
        'shadow_demand':True, 'lifecycle_state':'candidate',
        'lifecycle_write_ready':False, 'lifecycle_pending_review':False})
    later = order('B', day='2026-09-02')
    result = calculate_query_snapshot(legacy_snapshot([shadow, later]))
    allocation = {row['order_no']:row for row in result['allocations']}
    plans = {plan['tradeid']:plan for plan in result['plans']}
    assert allocation['A']['physical'] == '5'
    assert allocation['B']['physical'] == '0'
    assert plans['A']['freightname'] == SHIPPED_UNPOSTED_NAME
    assert plans['A']['write_enabled'] is False and plans['A']['freightcode'] == ''
    assert plans['B']['freightname'] == '需采购'
    assert result['write_blocks'][0]['code'] == 'lifecycle_confirmation_pending'


def test_shadow_without_delivery_date_still_reserves_stock_for_later_order():
    shadow = order('A', day='2026-09-01', lifecycle={
        'shadow_demand':True, 'lifecycle_state':'candidate',
        'lifecycle_write_ready':False, 'lifecycle_pending_review':False})
    shadow['delivery_date'] = None
    shadow['lines'][0]['delivery_date'] = None
    later = order('B', day='2026-09-02')
    later['lines'][0]['delivery_date'] = later['delivery_date']
    data = legacy_snapshot([shadow, later])
    data.update(rules_contract='v2', receipt_events_complete=True,
                receipt_period=['2026-08-07','2026-10-01'], receipt_events=[])
    previous = {'version':1,'profile_id':'1','events':{},'balances':{},
                'orders':{},'scope_periods':{}}
    result = calculate_query_snapshot(data, previous, today=date(2026,10,1))
    allocation = {row['demand']['order']:row for row in result['allocations']}
    plans = {plan['tradeid']:plan for plan in result['plans']}
    assert allocation['A']['actual'] == 5
    assert allocation['B']['actual'] == 0
    assert plans['A']['freightname'] == SHIPPED_UNPOSTED_NAME
    assert plans['A']['write_enabled'] is False and plans['A']['freightnumber'] == ''
    assert plans['B']['freightname'] == '需采购'
    assert not result['blocked_orders']


def test_shadow_demand_is_included_in_pending_supply_equation():
    shadow = order('A', day='2026-09-01', lifecycle={
        'shadow_demand':True, 'lifecycle_state':'candidate',
        'lifecycle_write_ready':False, 'lifecycle_pending_review':False})
    later = order('B', day='2026-09-02')
    data = legacy_snapshot([shadow, later])
    data['inventory'][0].update(physical_qty='0', saleable_qty='0')
    result = calculate_query_snapshot(data)
    allocations = {row['order_no']:row for row in result['allocations']}
    plans = {plan['tradeid']:plan for plan in result['plans']}
    assert allocations['A']['transit'] == '5'
    assert allocations['B']['transit'] == '5'
    assert plans['B']['freightname'] == '待到货'


def test_initial_shadow_without_verified_order_date_uses_conservative_fifo_hold():
    shadow = order('A', lifecycle={
        'shadow_demand':True, 'lifecycle_state':'shipped_unposted',
        'lifecycle_write_ready':False, 'lifecycle_pending_review':False,
        'lifecycle_order_date_fallback':True})
    shadow['order_date'] = ''
    shadow['allocation_order_date'] = '1900-01-01'
    later = order('B', day='2026-09-02')
    result = calculate_query_snapshot(legacy_snapshot([shadow, later]))
    allocation = {row['order_no']:row for row in result['allocations']}
    plans = {plan['tradeid']:plan for plan in result['plans']}
    assert allocation['A']['physical'] == '5'
    assert allocation['B']['physical'] == '0'
    assert plans['A']['freightname'] == SHIPPED_UNPOSTED_NAME
    assert plans['A']['write_enabled'] is False
    # Date uncertainty affects the same FIFO scope, so the later order is not
    # allowed to receive an ERP classification derived from guessed ordering.
    assert plans['B']['write_enabled'] is False
    assert {item['code'] for item in result['write_blocks']} == {
        'lifecycle_order_date_unverified', 'lifecycle_scope_pending_review'}


def test_confirmed_shadow_uses_only_dictionary_code_and_missing_code_never_writes():
    attributes = {'shadow_demand':True, 'lifecycle_state':'shipped_unposted',
                  'lifecycle_write_ready':True, 'lifecycle_pending_review':False}
    data = legacy_snapshot([order('A', lifecycle=attributes)])
    data['lifecycle_freight'] = {'available':True,'code':'ERP-REAL','reason':''}
    plan = calculate_query_snapshot(data)['plans'][0]
    assert plan['freightname'] == SHIPPED_UNPOSTED_NAME
    assert plan['freightcode'] == 'ERP-REAL' and plan['write_enabled'] is True

    data['orders'][0]['lifecycle_write_ready'] = False
    data['lifecycle_freight'] = {'available':False,'code':'','reason':'字典缺失'}
    plan = calculate_query_snapshot(data)['plans'][0]
    assert plan['freightcode'] == '' and plan['write_enabled'] is False


def test_600_release_removes_only_archived_receipt_order():
    a = json.dumps(['A','warehouse','product','2026-09-01'], ensure_ascii=False)
    b = json.dumps(['B','warehouse','product','2026-09-02'], ensure_ascii=False)
    state = {'version':1,'profile_id':'1','events':{},'balances':{},
             'orders':{a:{},b:{}},'scope_periods':{}}
    released, keys = release_receipt_orders(state, ['A'])
    assert keys == [a]
    assert set(released['orders']) == {b}
    assert set(state['orders']) == {a,b}  # pure helper

    snapshot = {'query_complete':True,'rules_contract':'v2','profile_id':'1',
                'orders':[],'inventory':[],'receipt_events':[],
                'receipt_events_complete':True,'calculation_issues':[],
                'receipt_release_order_nos':['A']}
    result = calculate_query_snapshot(snapshot, state)
    assert set(result['receipt_state']['orders']) == {b}
    assert result['released_receipt_orders'] == [a]


def test_publish_persists_lifecycle_and_keeps_candidate_unprintable(store):
    client = Client(); lifecycle = OrderLifecycle(store)
    source, stage = lifecycle.prepare(client, '1', [], '2026-10-01T09:00:00+08:00',
                                      shipped_rows=[raw('A', 500)])
    assert len(source) == 1
    orders, rows, stage = lifecycle.finalize(stage, [order()], [record()])
    snapshot = {
        'query_complete':True, 'rules_contract':'v1', 'profile_id':'1',
        'source_updated_at':'2026-10-01T09:00:00+08:00',
        'orders':orders, 'inventory':[{**KEY,'physical_qty':'5','saleable_qty':'0'}],
        'records':{'orders':rows,
                   'inventory':[{**KEY,'id':'stock','sku':'LS01','stock_quantity':'5',
                                 'available_stock':'5'}], 'purchases':[]},
        'calculation_issues':[], 'order_lifecycle':stage,
        'receipt_release_order_nos':[], 'lifecycle_freight':stage['freight'],
    }
    result = calculate_query_snapshot(snapshot)
    publish(store, snapshot, result)
    saved = store.rows('orders')[0]
    assert saved['logistics_company'] == SHIPPED_UNPOSTED_NAME
    assert saved['printable'] is False and saved['print_blocked'] is True
    with store.connect() as db:
        row = db.execute("SELECT lifecycle_state,confirmation_count "
                         "FROM order_lifecycle_state WHERE order_no='A'").fetchone()
    assert tuple(row) == ('candidate',1)


def candidate_snapshot(store):
    client = Client(); lifecycle = OrderLifecycle(store)
    _, stage = lifecycle.prepare(client, '1', [], '2026-10-01T09:00:00+08:00',
                                 shipped_rows=[raw('A', 500)])
    orders, rows, stage = lifecycle.finalize(stage, [order()], [record()])
    return {
        'query_complete':True, 'rules_contract':'v1', 'profile_id':'1',
        'source_updated_at':'2026-10-01T09:00:00+08:00',
        'orders':orders, 'inventory':[{**KEY,'physical_qty':'5','saleable_qty':'0'}],
        'records':{'orders':rows,
                   'inventory':[{**KEY,'id':'stock','sku':'LS01','stock_quantity':'5',
                                 'available_stock':'5'}], 'purchases':[]},
        'calculation_issues':[], 'order_lifecycle':stage,
        'receipt_release_order_nos':[], 'lifecycle_freight':stage['freight'],
    }


def test_lifecycle_commit_rolls_back_with_failed_publication(store, monkeypatch):
    snapshot = candidate_snapshot(store)
    snapshot['order_lifecycle']['delivery_date_updates'] = {'A':'2026-10-01'}
    calculation = calculate_query_snapshot(snapshot)
    original = OrderLifecycle.persist

    def fail_after_lifecycle_insert(db, stage):
        original(db, stage)
        raise RuntimeError('late failure')

    monkeypatch.setattr(OrderLifecycle, 'persist', staticmethod(fail_after_lifecycle_insert))
    with pytest.raises(RuntimeError, match='late failure'):
        publish(store, snapshot, calculation)
    assert store.rows('orders') == []
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM order_lifecycle_state').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM erp_order_delivery_cache').fetchone()[0] == 0


def test_candidate_local_plan_never_reaches_erp_sender(store):
    snapshot = candidate_snapshot(store)
    calls = []
    source = type('Source', (), {'collect':lambda self:copy.deepcopy(snapshot)})()
    jobs = UpdateJobs(store, source, lambda target:calls.append(target) or True)
    identity = str(uuid.uuid4())
    jobs.enqueue(identity, 'update')
    jobs.process_one()
    result = jobs.get(identity)
    assert calls == []
    assert result['status'] == 'partial'
    assert result['result']['write_blocked_orders'] == ['A']
    assert result['result']['changes'] == []
