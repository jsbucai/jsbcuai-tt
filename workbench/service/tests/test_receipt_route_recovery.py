"""An unchanged arrival lot must survive temporary FIFO changes and restarts."""
import copy
import pytest

from core.business_rules import calculate_snapshot
from test_business_rules import TODAY, event, facts


def sibling_facts(physical='2', include_shipped=True, received='2026-09-25'):
    data = facts(physical, events=[event('2', received)])
    first = data['orders'][0]
    first['order_no'] = 'AD044-C#109'
    first['lines'][0].update(open_qty='1', product_code='JL0303')
    second = copy.deepcopy(first)
    second['order_no'] = 'AD044-D#43'
    data['orders'] = [first, second] if include_shipped else [second]
    # ERP may reduce physical before the shipped sibling leaves the open list.
    data['inventory'][0].update(physical_qty=physical, saleable_qty='0')
    return data


def row(result, order='AD044-D#43'):
    assert not result['conflicts']
    return next(r for r in result['allocations'] if r['demand']['order'] == order)


@pytest.mark.parametrize('received,expected', [('2026-09-25', '来货件'), ('2026-09-17', '异常')])
def test_shipped_sibling_fifo_gap_recovers_same_arrival_lot(received, expected):
    first = calculate_snapshot(sibling_facts(received=received), today=TODAY)
    assert row(first)['category'] == expected
    gap = calculate_snapshot(sibling_facts('1', received=received), first['receipt_state'], TODAY)
    assert row(gap)['actual'] == 0 and row(gap)['category'] == '待到货'
    restored_facts = sibling_facts('1', False, received)
    recovered = calculate_snapshot(restored_facts, gap['receipt_state'], TODAY)
    assert row(recovered)['category'] == expected
    assert row(recovered)['dispatch_route'] == 'arrival'
    assert row(recovered)['new_receipt_quantity'] == 0
    assert row(recovered)['receipt_allocations'][0]['quantity'] == 1
    assert recovered['receipt_state']['balances'] == gap['receipt_state']['balances']
    again = calculate_snapshot(restored_facts, copy.deepcopy(recovered['receipt_state']), TODAY)
    assert row(again)['category'] == expected
    assert again['receipt_state'] == recovered['receipt_state']


def test_existing_wrong_inventory_route_self_repairs_from_retained_arrival_parts():
    data = sibling_facts('1', False)
    before = calculate_snapshot(data, today=TODAY)
    legacy = copy.deepcopy(before['receipt_state'])
    for order in legacy['orders'].values():
        order['route'] = 'inventory'
    repaired = calculate_snapshot(data, legacy, TODAY)
    assert row(repaired)['category'] == '来货件'
    assert repaired['receipt_state'] == before['receipt_state']
    assert next(iter(legacy['orders'].values()))['route'] == 'inventory'


def test_partial_arrival_remains_arrival_when_existing_stock_finishes_gap():
    partial = calculate_snapshot(facts('4', '4', events=[event()]), today=TODAY)
    assert partial['allocations'][0]['category'] == '待到货'
    ready = calculate_snapshot(facts('8', events=[event()]), partial['receipt_state'], TODAY)
    actual = ready['allocations'][0]
    assert actual['category'] == '来货件'
    assert actual['new_receipt_quantity'] == 0 and actual['inventory_quantity'] == 4


def test_preorder_receipt_is_still_inventory():
    result = calculate_snapshot(facts(events=[event('8', '2026-08-25')]), today=TODAY)
    assert not result['conflicts']
    assert result['allocations'][0]['dispatch_route'] == 'inventory'
    assert result['allocations'][0]['receipt_allocations'] == []


def test_receipt_removed_and_replaced_by_unrelated_stock_does_not_keep_arrival():
    first = calculate_snapshot(facts(events=[event('8')]), today=TODAY)
    ready = calculate_snapshot(facts(events=[event('0')]), first['receipt_state'], TODAY)
    assert not ready['conflicts']
    assert ready['allocations'][0]['dispatch_route'] == 'inventory'
    assert ready['allocations'][0]['receipt_allocations'] == []


def test_ready_inventory_is_not_reassigned_by_later_unrelated_receipt():
    first = calculate_snapshot(facts(), today=TODAY)
    later = calculate_snapshot(facts('12', events=[event()]), first['receipt_state'], TODAY)
    assert not later['conflicts']
    assert later['allocations'][0]['dispatch_route'] == 'inventory'
    assert later['allocations'][0]['receipt_allocations'] == []


def test_real_shortage_stays_purchase_even_with_aged_arrival_parts():
    result = calculate_snapshot(facts('4', '0', events=[event('4', '2026-09-17')]), today=TODAY)
    assert not result['conflicts']
    assert result['allocations'][0]['category'] == '需采购'
    assert result['allocations'][0]['dispatch_route'] is None
