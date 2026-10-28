import copy
import json
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.business_rules import calculate_snapshot
from core.logistics import inventory_supply, purchase_pending
from core.classification.procurement import follow_up_date
from core.api_source import publish, SourceIncomplete, validate_snapshot
from core.update_jobs import UpdateJobs
from test_api_migration import store, snapshot, order, inventory

TODAY=date(2026,9,25)
KEY={'profile_id':'1','warehouse_id':'2','product_id':'3','sku_id':'4','base_unit':'件'}


def facts(physical='8', incoming='0', events=None, purchases=None):
    total_demand=sum(Decimal(line['open_qty']) for order in snapshot()['orders'] for line in order['lines'])
    saleable=Decimal(physical)-total_demand+Decimal(incoming)
    data=snapshot(physical=physical,saleable=str(saleable))
    data.update(rules_contract='v2',receipt_events_complete=True,purchase_batches_complete=True,
                receipt_period=['2026-08-25','2026-09-25'],receipt_events=events or [],purchase_batches=purchases or [])
    data['orders'][0]['lines'][0]['delivery_date']='2026-10-01'
    return data


def event(q='4',when='2026-09-25'):
    return dict(KEY,event_id='receipt-1',document_no='IN1',posted_date=when,type='进货入库单',quantity=q)


def batch(q='4'):
    return dict(KEY,batch_id='purchase-1',order_no='PO1',purchase_date='2026-09-20',
                supplier_id='B002',supplier_name='标准供应商',open_qty=q)


def test_stock_order_and_rework_are_both_pending_without_sales_demand():
    stock=dict(KEY,physical_qty='2',saleable_qty='2',stock_order_qty='3',rework_qty='5')
    assert purchase_pending(stock, '8')==Decimal('8')
    assert purchase_pending(dict(KEY,physical_qty='2',saleable_qty='2'), '8')==Decimal('8')
    ordinary=batch('3')
    rework=dict(batch('5'),batch_id='rework-1',order_no='RW1')
    data=facts('2','8',purchases=[ordinary,rework]);data['inventory'][0]=stock
    result=calculate_snapshot(data,today=TODAY)
    assert not result['conflicts']
    assert result['plans'][0]['freightname']=='待到货'
    assert result['allocations'][0]['pending']==6


@pytest.mark.parametrize('order_qty,rework_qty', [('3','4'),('-1','9'),('NaN','5')])
def test_pending_components_must_close_exactly(order_qty,rework_qty):
    data=facts('2','8',purchases=[batch('8')])
    data['inventory'][0].update(stock_order_qty=order_qty,rework_qty=rework_qty)
    assert calculate_snapshot(data,today=TODAY)['conflicts']


def test_pending_uses_decimal_and_total_demand_once():
    row=dict(KEY,physical_qty='1.2',saleable_qty='0.9',stock_order_qty='0.1',rework_qty='0.2')
    assert purchase_pending(row, '0.6')==Decimal('0.3')
    data=facts('0','8',purchases=[batch('8')])
    before=calculate_snapshot(data,today=TODAY)
    data['orders'][0]['lines'][0]['open_qty']='12'
    data['inventory'][0]['saleable_qty']='-4'
    after=calculate_snapshot(data,today=TODAY)
    assert before['allocations'][0]['pending']==8
    assert after['allocations'][0]['pending']==8
    assert after['plans'][0]['freightname']=='需采购'


def test_confirmed_equation_example_aggregates_all_fifo_demand():
    data=facts('10','40')
    data['inventory'][0]['saleable_qty']='30'
    data['orders'].append(copy.deepcopy(data['orders'][0]))
    data['orders'][0]['order_no']='A'
    data['orders'][0]['lines'][0]['open_qty']='12'
    data['orders'][1]['order_no']='B'
    data['orders'][1]['lines'][0]['open_qty']='8'
    result=calculate_snapshot(data,today=TODAY)
    assert not result['conflicts']
    assert {row['incoming'] for row in result['allocations']} == {40}
    assert sum(row['demand']['quantity'] for row in result['allocations']) == 20


def test_partial_real_arrival_completes_order_and_keeps_route():
    first=calculate_snapshot(facts('4','4',purchases=[batch()]),today=TODAY)
    assert first['plans'][0]['freightname']=='待到货'
    second=calculate_snapshot(facts(events=[event()]),first['receipt_state'],TODAY)
    assert second['plans'][0]['freightname']=='来货件'
    assert second['allocations'][0]['new_receipt_quantity']==4
    assert second['allocations'][0]['inventory_quantity']==4
    again=calculate_snapshot(facts(events=[event()]),second['receipt_state'],TODAY)
    assert again['plans'][0]['freightname']=='来货件'
    assert again['allocations'][0]['new_receipt_quantity']==0


def test_non_purchase_stock_fills_gap_and_later_arrival_does_not_reassign():
    first=calculate_snapshot(facts('4','4',purchases=[batch()]),today=TODAY)
    full=calculate_snapshot(facts('8','4',purchases=[batch()]),first['receipt_state'],TODAY)
    assert full['plans'][0]['freightname']=='LS库存件'
    later=calculate_snapshot(facts('12','0',events=[event()]),full['receipt_state'],TODAY)
    assert later['plans'][0]['freightname']=='LS库存件'


def test_return_and_rolling_window_never_replenish_consumed_receipts():
    first=calculate_snapshot(facts(events=[event('8','2026-09-20')]),today=TODAY)
    reduced=calculate_snapshot(facts('4','0',events=[event('8','2026-09-20')]),first['receipt_state'],TODAY)
    repeat=calculate_snapshot(facts('4','0',events=[event('8','2026-09-20')]),reduced['receipt_state'],TODAY)
    assert repeat['receipt_state']['balances']==reduced['receipt_state']['balances']
    assert sum(repeat['receipt_state']['balances'].values())==4
    assert repeat['plans'][0]['freightname']=='需采购'


@pytest.mark.parametrize('age,expected',[(7,'来货件'),(8,'异常')])
def test_receipt_warning_boundary(age,expected):
    result=calculate_snapshot(facts(events=[event('2',f'2026-09-{25-age:02d}')]),today=TODAY)
    assert result['plans'][0]['freightname']==expected


def test_urging_uses_due_minus_three_natural_days_and_only_pending_supply():
    data=facts('4','4')
    data['orders'][0]['delivery_date']='2026-09-28'
    data['orders'][0]['lines'][0]['delivery_date']='2026-09-28'
    pending=calculate_snapshot(data,today=TODAY)
    assert pending['plans'][0]['freightname']=='催货'
    full=calculate_snapshot(facts('8','4'),today=TODAY)
    assert full['plans'][0]['freightname']=='LS库存件'
    shortage=calculate_snapshot(facts('0','4'),today=TODAY)
    assert shortage['plans'][0]['freightname']=='需采购'
    assert follow_up_date(date(2026,10,1))==date(2026,9,28)


def test_total_transit_does_not_require_purchase_batch_details():
    result=calculate_snapshot(facts('0','8',purchases=[]),today=TODAY)
    assert not result['conflicts']
    assert result['plans'][0]['freightname']=='待到货'


def test_account_isolation_and_missing_due_remains_local():
    data=facts();data['orders'][0]['lines'][0]['delivery_date']=None
    result=calculate_snapshot(data,today=TODAY)
    assert not result['conflicts'] and result['procurement']['urging_data_issues']
    data=facts(events=[dict(event(),profile_id='other')])
    with pytest.raises(SourceIncomplete):validate_snapshot(data)
    bad_state={'profile_id':'other','version':1,'events':{},'balances':{}}
    assert calculate_snapshot(facts(),bad_state,TODAY)['conflicts']


def test_negative_net_is_other_draft_reservation_not_invalid_pending_supply():
    data=facts('165','0')
    first=copy.deepcopy(data['orders'][0]);first['order_no']='26975764#29'
    first['lines'][0]['open_qty']='24'
    second=copy.deepcopy(data['orders'][0]);second['order_no']='26975764#30'
    second['lines'][0]['open_qty']='120'
    data['orders']=[first,second]
    data['inventory'][0].update(physical_qty='165',saleable_qty='16')

    balance=inventory_supply(data['inventory'][0], '144')
    assert balance == {
        'physical':Decimal('165'), 'pending':Decimal('0'),
        'inferred_other_reserved':Decimal('5'),
        'effective_physical':Decimal('160'),
    }
    result=calculate_snapshot(data,today=TODAY)
    assert not result['conflicts']
    assert sum(row['actual'] for row in result['allocations']) == 144
    assert {row['pending'] for row in result['allocations']} == {0}
    assert {row['physical_stock'] for row in result['allocations']} == {165}
    assert {row['effective_physical'] for row in result['allocations']} == {160}
    assert {row['inferred_other_reserved'] for row in result['allocations']} == {5}


def test_other_draft_reservation_larger_than_physical_floors_usable_stock_at_zero():
    balance=inventory_supply(dict(KEY,physical_qty='2',saleable_qty='-3'), '0')
    assert balance['inferred_other_reserved']==Decimal('5')
    assert balance['effective_physical']==Decimal('0')


def test_negative_physical_linked_to_order_remains_a_calculation_conflict():
    data=facts()
    data['inventory'][0].update(physical_qty='-443',saleable_qty='-443')
    result=calculate_snapshot(data,today=TODAY)
    assert result['conflicts'] and not result['plans']


def test_ledger_publication_and_failed_publish_are_atomic(store):
    data=facts(events=[event()]);result=calculate_snapshot(data,today=TODAY)
    publish(store,data,result)
    with store.connect() as db:before=db.execute("SELECT value FROM settings WHERE key='receipt_ledger_v1'").fetchone()[0]
    broken=copy.deepcopy(data);broken['records']['inventory'][0]['document_date']={'invalid':'sqlite value'}
    with pytest.raises(Exception):publish(store,broken,dict(result,receipt_state={'changed':True}))
    with store.connect() as db:assert db.execute("SELECT value FROM settings WHERE key='receipt_ledger_v1'").fetchone()[0]==before


def test_first_worker_update_requires_verified_ledger_bootstrap(store):
    data=facts();jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:data))
    identity=str(uuid.uuid4());jobs.enqueue(identity);jobs.process_one()
    assert jobs.get(identity)['status']=='blocked'
    assert '入库账本' in jobs.get(identity)['result']['message']
