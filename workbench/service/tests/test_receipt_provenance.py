"""Receipt routing depends on observed source times, not the last route label."""
import copy

from core.business_rules import calculate_snapshot
from core.classification.receipts import observed_time, receipt_lag_proven
from core.order_lifecycle import release_receipt_orders
from test_business_rules import TODAY, event, facts


def timed(data, observed='2026-09-25T16:32:39+08:00', covered='2026-09-25T16:20:00+08:00'):
    data['receipt_covered_through'] = covered
    for stock in data['inventory']:
        stock['stock_observed_at'] = observed
    return data


def inbound(q='4', posted='2026-09-25 16:30:32'):
    return dict(event(q), posted_at=posted)


def good(data, state=None):
    result = calculate_snapshot(data, state, TODAY)
    assert not result['conflicts'], result['conflicts']
    return result


def ready_entry(result):
    return next(iter(result['receipt_state']['orders'].values()))


def test_late_receipt_repairs_ready_unknown_stock_without_new_physical_increase():
    first = good(timed(facts('4', '4')))
    lag = good(timed(facts('8')), first['receipt_state'])
    assert lag['allocations'][0]['category'] == 'LS库存件'
    late = good(timed(facts('8', events=[inbound()]),
        observed='2026-09-25T17:00:00+08:00', covered='2026-09-25T17:00:01+08:00'), lag['receipt_state'])
    assert late['allocations'][0]['category'] == '来货件'
    assert late['allocations'][0]['inventory_quantity'] == 4
    again = good(timed(facts('8', events=[inbound()]),
        observed='2026-09-25T17:30:00+08:00', covered='2026-09-25T17:30:01+08:00'), late['receipt_state'])
    assert again['allocations'][0]['category'] == '来货件'
    assert again['receipt_state']['balances'] == late['receipt_state']['balances']


def test_retained_false_part_can_be_repaired_only_by_verified_old_window():
    # Model an older executable that persisted arrival=False. Exact historical
    # read timestamps are attached by an audited migration, not guessed here.
    lag = good(facts())
    old = good(facts(events=[inbound()]), lag['receipt_state'])
    assert not ready_entry(old)['receipts'][0]['arrival']
    migrated = copy.deepcopy(old['receipt_state'])
    entry = next(iter(migrated['orders'].values()))
    entry['inventory_ready_observed_at'] = '2026-09-25T16:32:39+08:00'
    entry['inventory_ready_receipt_covered_through'] = '2026-09-25T16:20:00+08:00'
    repaired = good(timed(facts(events=[inbound()])), migrated)
    assert ready_entry(repaired)['receipts'][0]['arrival']
    assert repaired['allocations'][0]['category'] == '来货件'
    assert repaired['allocations'][0]['new_receipt_quantity'] == 0
    assert not next(iter(old['receipt_state']['orders'].values()))['receipts'][0]['arrival']


def test_legacy_without_verified_window_does_not_guess_source():
    first = good(facts())
    late = good(timed(facts(events=[inbound()])), first['receipt_state'])
    assert late['allocations'][0]['category'] == 'LS库存件'
    assert 'inventory_ready_observed_at' not in ready_entry(late)


def test_truly_later_receipt_does_not_reassign_ready_inventory():
    first = good(timed(facts(), observed='2026-09-25T16:00:00+08:00', covered='2026-09-25T16:00:01+08:00'))
    later = good(timed(facts('12', events=[inbound()]), observed='2026-09-25T17:00:00+08:00', covered='2026-09-25T17:00:01+08:00'), first['receipt_state'])
    assert later['allocations'][0]['category'] == 'LS库存件'
    assert ready_entry(later)['inventory_ready_observed_at'] == '2026-09-25T16:00:00+08:00'


def test_ready_observation_is_not_advanced_by_a_poll():
    first = good(timed(facts()))
    later = good(timed(facts(), observed='2026-09-25T17:00:00+08:00', covered='2026-09-25T17:00:01+08:00'), first['receipt_state'])
    assert ready_entry(later) == ready_entry(first)


def test_exact_timestamp_window_and_timezones():
    prior = {'inventory_ready_observed_at':'2026-09-25T08:32:39Z',
             'inventory_ready_receipt_covered_through':'2026-09-25T16:20:00+08:00'}
    assert receipt_lag_proven(prior, {'posted_at':'2026-09-25 16:30:32'})
    assert not receipt_lag_proven(prior, {'posted_at':'2026-09-25 16:20:00'})
    assert not receipt_lag_proven(prior, {'posted_at':'2026-09-25 16:33:00'})
    assert not receipt_lag_proven(prior, {'posted_at':'invalid'})
    assert observed_time(None) is None
    assert observed_time('2026-09-25') is None


def test_physical_drop_retains_claim_without_replenishing_any_lot():
    first = good(facts(events=[event('8')]))
    low = good(facts('0', events=[event('8')]), first['receipt_state'])
    assert low['receipt_state']['balances']['receipt-1'] == 0
    assert ready_entry(low)['arrival_claims'] == [{'id':'receipt-1', 'quantity':8}]
    assert low['allocations'][0]['category'] == '需采购'
    recovered = calculate_snapshot(facts('8', events=[event('8')]), low['receipt_state'], TODAY)
    assert recovered['conflicts'][0]['code'] == 'receipt_provenance_gap'
    assert not recovered['plans'] and 'receipt_state' not in recovered
    assert low['receipt_state']['balances']['receipt-1'] == 0


def test_verified_replacement_arrival_covers_old_claim_without_inventing_stock():
    first = good(facts(events=[event('8')]))
    low = good(facts('0', events=[event('8')]), first['receipt_state'])
    replacement = dict(event('8'), event_id='replacement', document_no='IN-REPLACEMENT')
    data = facts(events=[event('8'), replacement])
    after = good(data, low['receipt_state'])
    row = after['allocations'][0]
    assert row['category'] == '来货件' and row['actual'] == 8
    assert row['receipt_allocations'][0]['event_id'] == 'replacement'
    assert ready_entry(after)['arrival_claims'] == [{'id':'receipt-1','quantity':8}]
    assert after['receipt_state']['balances']['receipt-1'] == 0
    assert after['receipt_state']['balances']['replacement'] == 8
    repeated = good(data, after['receipt_state'])
    assert repeated['receipt_state'] == after['receipt_state']


def test_pre_order_replacement_stock_cannot_cover_historical_arrival_claim():
    first = good(facts(events=[event('8')]))
    low = good(facts('0', events=[event('8')]), first['receipt_state'])
    replacement = dict(event('8', '2026-08-31'), event_id='older', document_no='IN-OLDER')
    result = calculate_snapshot(facts(events=[event('8'), replacement]), low['receipt_state'], TODAY)
    assert result['conflicts'][0]['code'] == 'receipt_provenance_gap'


def test_voided_inbound_can_release_historical_claim():
    first = good(facts(events=[event('8')]))
    voided = good(facts(events=[event('0')]), first['receipt_state'])
    assert voided['allocations'][0]['category'] == 'LS库存件'
    assert not ready_entry(voided).get('arrival_claims')


def test_open_quantity_reduction_keeps_only_current_scope():
    first = good(facts(events=[event('8')]))
    reduced = facts('4', events=[event('8')])
    reduced['orders'][0]['lines'][0]['open_qty'] = '4'
    reduced['inventory'][0]['saleable_qty'] = '0'
    after = good(reduced, first['receipt_state'])
    assert ready_entry(after)['arrival_claims'] == [{'id':'receipt-1', 'quantity':4}]
    assert after['allocations'][0]['category'] == '来货件'


def test_date_change_requires_explicit_provenance_migration():
    first = good(facts(events=[event('8', '2026-09-24')]))
    changed = facts(events=[event('8', '2026-09-24')])
    changed['orders'][0]['order_date'] = '2026-09-25'
    result = calculate_snapshot(changed, first['receipt_state'], TODAY)
    assert result['conflicts'][0]['code'] == 'receipt_order_date_changed'
    assert not result['plans']


def test_confirmed_release_removes_claims_without_restoring_balance():
    first = good(facts(events=[event('8')]))
    low = good(facts('0', events=[event('8')]), first['receipt_state'])
    number = next(iter(low['allocations']))['demand']['order']
    released, ids = release_receipt_orders(low['receipt_state'], [number])
    assert ids and not released['orders']
    assert released['balances']['receipt-1'] == 0


def test_ld1630_two_order_quantities_recover_the_same_existing_batch():
    def setup(physical, events, incoming, observed, covered):
        data = timed(facts(str(physical), events=events), observed=observed, covered=covered)
        first = data['orders'][0]
        first['order_no'] = '26976236#15'
        first['lines'][0].update(open_qty='24', product_code='LD1630')
        second = copy.deepcopy(first)
        second['order_no'] = '26976236#16'
        second['lines'][0]['open_qty'] = '12'
        data['orders'] = [first, second]
        data['inventory'][0]['saleable_qty'] = str(physical - 36 + incoming)
        return data
    before = good(setup(11, [], 40, '2026-09-25T16:17:00+08:00', '2026-09-25T16:17:01+08:00'))
    assert [r['category'] for r in before['allocations']] == ['待到货', '待到货']
    lag = good(setup(51, [], 0, '2026-09-25T16:32:39+08:00', '2026-09-25T16:20:00+08:00'), before['receipt_state'])
    assert [r['category'] for r in lag['allocations']] == ['LD库存件', 'LD库存件']
    after = good(setup(51, [inbound('40')], 0, '2026-09-25T16:45:00+08:00', '2026-09-25T16:45:01+08:00'), lag['receipt_state'])
    assert [r['category'] for r in after['allocations']] == ['来货件', '来货件']
    assert [r['receipt_allocations'][0]['quantity'] for r in after['allocations']] == [13, 12]
    assert after['receipt_state']['balances']['receipt-1'] == 40


def test_dormant_claim_recovers_before_unknown_stock_is_used():
    first = good(facts(events=[event('8')]))
    # Another reservation temporarily suppresses the FIFO quota without
    # reducing physical stock or the underlying receipt balance.
    reserved = facts('8', events=[event('8')])
    reserved['inventory'][0]['saleable_qty'] = '-8'
    gap = good(reserved, first['receipt_state'])
    assert gap['allocations'][0]['actual'] == 0
    assert not ready_entry(gap)['receipts']
    assert ready_entry(gap)['arrival_claims']
    recovered = good(facts('16', events=[event('8')]), gap['receipt_state'])
    assert recovered['allocations'][0]['category'] == '来货件'
    assert recovered['allocations'][0]['receipt_allocations'][0]['quantity'] == 8


def test_explicit_partial_inbound_correction_and_repurchase_can_finish_order():
    first = good(facts(events=[event('8')]))
    corrected = dict(event('4'), event_id='receipt-2', document_no='IN2')
    after = good(facts(events=[event('4'), corrected]), first['receipt_state'])
    assert after['allocations'][0]['category'] == '来货件'
    assert ready_entry(after)['arrival_claims'] == [
        {'id':'receipt-1', 'quantity':4}, {'id':'receipt-2', 'quantity':4}]


def test_explicit_return_and_repurchase_can_finish_order():
    first = good(facts(events=[event('8')]))
    returned = dict(event('-4'), event_id='return-1', document_no='RETURN', type='进货退货单')
    new = dict(event('4'), event_id='receipt-2', document_no='IN2')
    after = good(facts(events=[event('8'), returned, new]), first['receipt_state'])
    assert after['allocations'][0]['category'] == '来货件'
    assert ready_entry(after)['arrival_claims'] == [
        {'id':'receipt-1', 'quantity':4}, {'id':'receipt-2', 'quantity':4}]
    repeated = good(facts(events=[event('8'), returned, new]), after['receipt_state'])
    assert ready_entry(repeated) == ready_entry(after)



def test_separate_red_inbound_retires_claim_then_repurchase_is_arrival():
    first = good(facts(events=[event('8')]))
    red = dict(event('-8'), event_id='red-receipt', document_no='IN-RED')
    zero = good(facts('0', events=[event('8'), red]), first['receipt_state'])
    assert zero['allocations'][0]['category'] == '需采购'
    assert not ready_entry(zero).get('arrival_claims')
    assert sum(zero['receipt_state']['balances'].values()) == 0
    repeated = good(facts('0', events=[event('8'), red]), zero['receipt_state'])
    assert repeated['receipt_state'] == zero['receipt_state']
    new = dict(event('8'), event_id='new-receipt', document_no='IN-NEW')
    after = good(facts(events=[event('8'), red, new]), repeated['receipt_state'])
    assert after['allocations'][0]['category'] == '来货件'
    assert ready_entry(after)['arrival_claims'] == [{'id':'new-receipt', 'quantity':8}]
    assert after['receipt_state']['balances']['receipt-1'] == 0
    assert after['receipt_state']['balances']['new-receipt'] == 8


def test_red_inbound_reversal_never_replenishes_original_lot():
    first = good(facts(events=[event('8')]))
    red = dict(event('-8'), event_id='red-receipt', document_no='IN-RED')
    zero = good(facts('0', events=[event('8'), red]), first['receipt_state'])
    reversed_red = dict(red, quantity='0')
    after = good(facts(events=[event('8'), reversed_red]), zero['receipt_state'])
    assert after['receipt_state']['balances']['receipt-1'] == 0
    assert after['allocations'][0]['receipt_allocations'] == []
    assert after['allocations'][0]['category'] == 'LS库存件'
    repeated = good(facts(events=[event('8'), reversed_red]), after['receipt_state'])
    assert repeated['receipt_state'] == after['receipt_state']


def test_negative_inbound_quantity_revision_consumes_only_new_negative_delta():
    first = good(facts(events=[event('8')]))
    red = dict(event('-4'), event_id='red-receipt', document_no='IN-RED')
    partial = good(facts('4', events=[event('8'), red]), first['receipt_state'])
    assert partial['receipt_state']['balances']['receipt-1'] == 4
    assert ready_entry(partial)['arrival_claims'] == [{'id':'receipt-1', 'quantity':4}]
    more_red = dict(red, quantity='-6')
    lower = good(facts('2', events=[event('8'), more_red]), partial['receipt_state'])
    assert lower['receipt_state']['balances']['receipt-1'] == 2
    assert ready_entry(lower)['arrival_claims'] == [{'id':'receipt-1', 'quantity':2}]
    # Reducing -6 back to -4 is not permission to restore consumed sources.
    undo = good(facts('4', events=[event('8'), red]), lower['receipt_state'])
    assert undo['receipt_state']['balances']['receipt-1'] == 2
    assert ready_entry(undo)['arrival_claims'] == [{'id':'receipt-1', 'quantity':2}]


def test_inbound_sign_change_consumes_signed_difference_once():
    # +4 -> -4 means a net reduction of 8, not 4 and not 12.
    original = dict(event('4'), event_id='original', document_no='IN-ORIGINAL')
    other = dict(event('8'), event_id='other', document_no='IN-OTHER')
    first = good(facts('12', events=[original, other]))
    negative = dict(original, quantity='-4')
    after = good(facts('4', events=[negative, other]), first['receipt_state'])
    assert sum(after['receipt_state']['balances'].values()) == 4
    assert after['receipt_state']['balances']['original'] == 0
    assert after['receipt_state']['balances']['other'] == 4
    repeated = good(facts('4', events=[negative, other]), after['receipt_state'])
    assert repeated['receipt_state'] == after['receipt_state']
