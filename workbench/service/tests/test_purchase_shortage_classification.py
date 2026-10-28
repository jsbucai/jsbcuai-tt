"""Aged partial receipts must not hide the remaining purchase obligation."""
import copy
from datetime import date
from decimal import Decimal

import pytest

from core.business_rules import calculate_snapshot
from core.logistics import calculate
from core.daily_work import DailyWork
from test_api_migration import order, inventory
from test_business_rules import facts, event, TODAY
from test_daily_work import store, seed, order as task_order, user, NOW


def demand_twenty(physical, pending, *, due='2026-10-01'):
    data = facts(str(physical), str(pending),
                 events=[event(str(physical), '2026-09-17')] if physical else [])
    data['orders'][0]['lines'][0]['open_qty'] = '20'
    data['orders'][0]['delivery_date'] = due
    data['orders'][0]['lines'][0]['delivery_date'] = due
    data['inventory'][0]['saleable_qty'] = str(Decimal(physical) + Decimal(pending) - 20)
    return data


@pytest.mark.parametrize('physical,pending', [(18, 0), (0, 18), (10, 8)])
@pytest.mark.parametrize('due', ['2026-10-01', '2026-09-01', None])
def test_uncovered_two_pieces_remain_purchase_gap_even_when_arrival_or_due_is_old(physical, pending, due):
    data = demand_twenty(physical, pending, due=due)
    result = calculate_snapshot(data, today=TODAY)
    assert not result['conflicts']
    assert result['plans'][0]['freightname'] == '需采购'
    row = result['allocations'][0]
    assert (row['actual'], row['pending'], row['shortage']) == (physical, pending, 2)
    assert row['category'] == row['order_category'] == '需采购'
    assert bool(row['receipt_warning']) == bool(physical)
    repeat = calculate_snapshot(data, result['receipt_state'], TODAY)
    assert repeat['plans'] == result['plans']
    assert repeat['receipt_state'] == result['receipt_state']


@pytest.mark.parametrize('due,expected', [('2026-10-01', '待到货'), ('2026-09-28', '催货')])
def test_fully_purchased_but_partly_arrived_is_waiting_supply(due, expected):
    result = calculate_snapshot(demand_twenty(18, 2, due=due), today=TODAY)
    assert not result['conflicts']
    assert result['plans'][0]['freightname'] == expected
    row = result['allocations'][0]
    assert row['shortage'] == 0 and row['pending'] == 2
    assert row['receipt_warning'] and row['dispatch_route'] is None


@pytest.mark.parametrize('age,expected', [(0, '来货件'), (7, '来货件'), (8, '异常')])
def test_fully_arrived_still_uses_existing_seven_day_packing_warning(age, expected):
    data = demand_twenty(20, 0)
    data['receipt_events'][0]['posted_date'] = f'2026-09-{25-age:02d}'
    result = calculate_snapshot(data, today=TODAY)
    assert not result['conflicts']
    assert result['plans'][0]['freightname'] == expected
    assert result['allocations'][0]['dispatch_route'] == 'arrival'


@pytest.mark.parametrize('pending,expected', [(0, '需采购'), (2, '待到货')])
def test_ready_old_line_cannot_override_other_product_shortage(pending, expected):
    data = demand_twenty(18, pending)
    other = copy.deepcopy(data['orders'][0]['lines'][0])
    other.update(line_id='2', product_id='9', product_code='LD0002', open_qty='4')
    data['orders'][0]['lines'].append(other)
    data['inventory'].append(dict(data['inventory'][0], product_id='9', physical_qty='4', saleable_qty='0'))
    data['receipt_events'].append(dict(event('4', '2026-09-17'), product_id='9', event_id='receipt-2'))
    result = calculate_snapshot(data, today=TODAY)
    assert not result['conflicts']
    assert result['plans'][0]['freightname'] == expected
    assert {row['category'] for row in result['allocations']} == {expected, '异常'}
    assert all(row['order_category'] == expected for row in result['allocations'])


def test_fifo_only_short_order_gets_purchase_gap():
    data = demand_twenty(18, 0)
    second = copy.deepcopy(data['orders'][0])
    data['orders'][0]['order_no'] = 'FIRST'
    data['orders'][0]['lines'][0]['open_qty'] = '18'
    second['order_no'] = 'SECOND'
    second['lines'][0]['open_qty'] = '2'
    data['orders'].append(second)
    result = calculate_snapshot(data, today=TODAY)
    assert not result['conflicts']
    assert {p['tradeid']:p['freightname'] for p in result['plans']} == {'FIRST':'异常', 'SECOND':'需采购'}
    assert sum(row['shortage'] for row in result['allocations']) == 2
    assert sum(row['actual'] for row in result['allocations']) == 18


@pytest.mark.parametrize('physical,pending,expected', [(18, 0, '需采购'), (10, 8, '需采购'),
    (18, 2, '待到货'), (20, 0, '异常')])
def test_legacy_classification_agrees(physical, pending, expected):
    item = order(q='20')
    item['lines'][0]['allocated_receipts'] = [{'allocation_id':'old', 'qty':str(physical),
                                              'received_date':'2026-09-17'}]
    result = calculate([item], inventory(str(physical), str(physical + pending - 20)), TODAY)
    assert not result['conflicts'] and result['plans'][0]['freightname'] == expected


def test_legacy_multiline_shortage_beats_ready_old_receipt():
    item = order(q='18')
    item['lines'][0]['allocated_receipts'] = [{'allocation_id':'old', 'qty':'18',
                                              'received_date':'2026-09-17'}]
    other = copy.deepcopy(item['lines'][0])
    other.update(line_id='2', open_qty='2', allocated_receipts=[])
    item['lines'].append(other)
    result = calculate([item], inventory('18', '-2'), TODAY)
    assert not result['conflicts'] and result['plans'][0]['freightname'] == '需采购'
    assert len(result['allocations']) == 2
    assert sum(Decimal(row['gap']) for row in result['allocations']) == 2


def test_shortage_generates_two_piece_procurement_task_and_no_packing_exception(store):
    result = calculate_snapshot(demand_twenty(18, 0), today=TODAY)
    number = result['plans'][0]['tradeid']
    item = task_order(number, 'LS0001', '需采购')
    item['product_id'] = '3'
    item['quantity'] = '20'
    seed(store, [item], {number:'需采购'}, result['allocations'])
    engine = DailyWork(store, now=lambda: NOW, auto_project=False)
    engine.refresh()
    tasks = engine.list(user('admin', [], True), view='all')['items']
    purchasing = [row for row in tasks if row['role'] == 'purchase']
    assert len(purchasing) == 1
    assert purchasing[0]['task_type'] == 'procurement_gap'
    assert Decimal(purchasing[0]['payload']['shortage']) == 2
    assert not any(row['role']=='stock_packing' for row in tasks)
    arrival=[row for row in tasks if row['role']=='arrival_packing']
    assert len(arrival)==1 and arrival[0]['task_type']=='arrival_shortage_waiting'
    assert arrival[0]['payload']['read_only'] and arrival[0]['payload']['arrival_partial']
    assert Decimal(arrival[0]['payload']['arrival_shortage'])==2
