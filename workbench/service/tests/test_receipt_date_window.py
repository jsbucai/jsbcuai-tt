"""Posting-window regressions for backdated purchase and assembly receipts."""
import copy
import json
from datetime import date

import pytest

from core.business_rules import calculate_snapshot
from core.classification.receipts import observation_day, reconcile
from core.classification_pipeline import calculate_query_snapshot
from core.erp_source import LiveSource, receipt_events_from_rows
from core.api_source import validate_snapshot
from test_business_rules import event, facts
from test_erp_source import Client


TODAY = date(2026, 10, 16)


def backdated_facts(kind='内部组装／拆卸'):
    receipt = dict(event('400', '2026-08-03'), type=kind,
                   document_no='ZZ-20260803-005', posted_at='2026-08-14T09:24:22+08:00')
    data = facts('215', events=[receipt])
    data['receipt_period'] = ['2026-08-07', TODAY.isoformat()]
    order = data['orders'][0]
    order.update(order_no='4400383966#21', order_date='2026-10-15', delivery_date='2026-10-21')
    order['lines'][0].update(product_code='LD2564', open_qty='60', delivery_date='2026-10-21')
    data['inventory'][0]['saleable_qty'] = '155'
    return data


@pytest.mark.parametrize('kind', ['进货入库单', '内部组装／拆卸'])
def test_backdated_bill_inside_posting_window_is_not_blocked_or_counted_twice(kind):
    data = backdated_facts(kind)
    before = copy.deepcopy(data)
    state = None
    for day in (TODAY, TODAY, date(2026, 10, 17)):
        data['receipt_period'][1] = day.isoformat()
        result = calculate_snapshot(data, json.loads(json.dumps(state)), day)
        assert result['conflicts'] == []
        assert result['plans'][0]['freightname'] == 'LD库存件'
        assert result['allocations'][0]['actual'] == 60
        state = result['receipt_state']
        assert state['events']['receipt-1']['date'] == '2026-08-03'
        assert state['events']['receipt-1']['posted_at'] == '2026-08-14T09:24:22+08:00'
        assert state['balances']['receipt-1'] == 215
        assert sum(x['quantity'] for x in next(iter(state['orders'].values()))['receipts']) == 60
    data['receipt_period'][1] = before['receipt_period'][1]
    assert data == before


@pytest.mark.parametrize('posted', [None, '2026-08-06T23:59:59+08:00',
                                    '2026-10-17T00:00:00+08:00', 'invalid'])
def test_uncovered_or_invalid_receipt_does_not_become_inventory(posted):
    data = backdated_facts()
    data['receipt_events'][0]['posted_at'] = posted
    result = calculate_snapshot(data, today=TODAY)
    assert result['conflicts'][0]['code'] == 'receipt_event_conflict'
    assert result['plans'] == [] and 'receipt_state' not in result


def test_coverage_uses_china_day_and_does_not_replace_business_date():
    assert observation_day({'date':'2026-08-03', 'posted_at':'2026-08-06T16:00:00Z'}) == '2026-08-07'
    assert observation_day({'date':'2026-08-03'}) == '2026-08-03'
    assert observation_day({'posted_date':'2026-08-03'}) == '2026-08-03'


def test_valid_old_receipt_does_not_hide_current_shortage():
    data = backdated_facts()
    data['orders'][0]['order_no'] = '4400383966#7'
    data['orders'][0]['lines'][0].update(product_code='LD2911', open_qty='84')
    data['inventory'][0].update(physical_qty='15', saleable_qty='-69')
    result = calculate_snapshot(data, today=TODAY)
    assert result['conflicts'] == []
    assert result['plans'][0]['freightname'] == '需采购'
    assert result['allocations'][0]['actual'] == 15
    assert result['allocations'][0]['shortage'] == 69


@pytest.mark.parametrize('missing,expected', [(True, 0), (False, 400)])
def test_backdated_receipt_removed_inside_covered_posting_window_is_reversed(missing, expected):
    lot = {'id':'lot', 'document':'ZZ-20260803-005', 'date':'2026-08-03',
           'posted_at':'2026-08-14T09:24:22+08:00', 'warehouse':'W', 'sku':'P',
           'type':'内部组装／拆卸', 'quantity':400}
    first = reconcile([lot], None, ('2026-08-07','2026-10-16'), {('W','P'):400})
    original = copy.deepcopy(first)
    second = reconcile([] if missing else [lot], first,
                       ('2026-08-07','2026-10-16'), {('W','P'):400})
    assert first == original
    assert second['balances']['lot'] == expected
    assert second['events']['lot']['quantity'] == expected
    again = reconcile([] if missing else [lot], json.loads(json.dumps(second)),
                      ('2026-08-07','2026-10-17'), {('W','P'):400})
    assert again['balances']['lot'] == expected


def test_uncovered_old_receipt_is_not_deleted_by_a_later_partial_window():
    lot = {'id':'lot', 'document':'IN1', 'date':'2026-08-10',
           'posted_at':'2026-08-06T12:00:00+08:00', 'warehouse':'W', 'sku':'P',
           'type':'进货入库单', 'quantity':400}
    state = {'version':1, 'events':{'lot':lot}, 'balances':{'lot':400}}
    result = reconcile([], state, ('2026-08-07','2026-10-16'), {('W','P'):400})
    assert result['balances']['lot'] == 400


def test_red_correction_of_backdated_receipt_does_not_resurrect_stock():
    data = backdated_facts()
    initial = calculate_snapshot(data, today=TODAY)
    data['receipt_events'][0]['quantity'] = '0'
    corrected = calculate_snapshot(data, initial['receipt_state'], TODAY)
    assert corrected['conflicts'] == []
    assert corrected['receipt_state']['balances']['receipt-1'] == 0
    again = calculate_snapshot(data, json.loads(json.dumps(corrected['receipt_state'])), TODAY)
    assert again['receipt_state'] == corrected['receipt_state']


def test_real_adapter_and_scope_pipeline_use_posting_window_without_widening_it():
    client = Client()
    client.stock_qty, client.stock_saleable = 215, 210
    client.receipt_bills = [{'vchcode':'ASM1', 'number':'ZZ-20260803-005',
        'vchtype':16, 'vchtypename':'生产组装', 'date':'2026-08-03',
        'overtime':'2026-08-14T09:24:22', 'ktypeid':'20',
        'details':[{'dlyorder':'in1','ptypeid':'30','skuid':'31','qty':400,'inout':0}]}]
    source = LiveSource(client, warehouse_id='20')
    data = validate_snapshot(source.collect())
    assert data['receipt_period'][0] == '2026-08-07'
    assert data['receipt_events'][0]['posted_date'] == '2026-08-03'
    before_id = data['receipt_events'][0]['event_id']
    rebuilt = receipt_events_from_rows(data['records']['purchases'])
    assert rebuilt[0]['event_id'] == before_id
    state = {'version':1, 'profile_id':'10', 'events':{}, 'balances':{}, 'orders':{}, 'scope_periods':{}}
    result = calculate_query_snapshot(data, state, today=date.today())
    assert not result['blocked_orders']
    assert result['plans'][0]['freightname'] == 'LS库存件'
    assert before_id in result['receipt_state']['events']
