"""Private WMS totals must cover every demand line without guessed conversions."""
from copy import deepcopy
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest

from core.api_source import publish as publish_snapshot, validate_snapshot, SourceIncomplete
from core.classification_pipeline import calculate_query_snapshot
from core.customer_orders import CustomerOrders
from core.daily_work import DailyWork
from core.erp_source import LiveSource
from core.purchase_price_bounds import (match_wms_lines,sale_price_fact,refresh_price_bounds,
    persist_sale_price_facts,ensure_fact_schema,validate_sale_price_facts,TAX_BASIS,TAX_EVIDENCE,
    CONFIRMED_BASE_UNIT,USER_UNIT_EVIDENCE,USER_OVERRIDE_UNIT_EVIDENCE,ERP_UNIT_EVIDENCE)
from core.store import dumps
from core.wms_cache import WmsCache
from test_daily_work import order, store
from test_task_board import board,publish
from test_erp_source import Client


class PriceClient(Client):
    def __init__(self,named_units=True):
        super().__init__();self.stock_qty=0;self.stock_saleable=-5;self.incremental_empty=False
        self.unit_label='支' if named_units is True else '' if named_units is False else named_units
        self.orders[0]['order_info']['order_info'][0]['goods_unit']=self.unit_label
        # This unrelated Qimen field is deliberately nonsensical.
        self.orders[0]['order_info']['order_info'][0]['sell_price']='999999'
    def call(self,method,params):
        result=super().call(method,params)
        if method=='erp.ptype.list':result['ptypes'][0]['unit1']=self.unit_label
        if method=='erp.ewms.stockoutbill.list':
            if self.incremental_empty and params['timetype']==1:return {'orders':[],'totalresults':0}
            result['orders'][0]['goodslist'][0].update(total='56.5',price='999',assprice='999',tax='1',unitname=self.unit_label)
        return result


def snapshot_parts(rows,facts,source='2026-10-04T08:00:00+08:00'):
    return {'profile_id':'P','source_updated_at':source,'records':{'orders':rows},'wms_sale_price_facts':facts}


def fact_for(row,amount,unit='支',**extra):
    head={'tradeid':row['order_no'],'vchcode':'doc:'+row['order_no'],'ktypeid':row['warehouse_id']}
    line={'id':row['line_id'],'usercode':row['sku'],'ptypeid':row['product_id'],'skuid':row['sku_id'],
        'unit':'1','unitrate':'1','qty':row['quantity'],'assqty':row['quantity'],'total':amount,
        'tax':'1','unitname':unit,**extra}
    return sale_price_fact(row,line,head,{'unit_name':unit,'base_unit':'1'},'2026-10-04T08:00:00+08:00')


def price_order(number,amount,quantity='1',line_id='1'):
    row=order(number,'LS1000','需采购',line_id=line_id,unit_name='支',base_unit='1',quantity=quantity)
    row['id']=number+':'+line_id
    return row


@pytest.mark.parametrize('named_units',[True,False,'个','PCS','箱','件'])
def test_current_live_totals_are_private_atomic_and_diagnostics_never_set_sale_price(store,named_units):
    client=PriceClient(named_units);cache=WmsCache(store)
    cache.put_order_dates('10',{'A#2':'2026-09-24'},'test_verified',datetime.now(ZoneInfo('Asia/Shanghai')))
    snapshot=validate_snapshot(LiveSource(client,warehouse_id='20',wms_cache=cache).collect())
    fact=snapshot['wms_sale_price_facts'][0]
    assert fact['inclusive_amount']=='56.5' and fact['quantity']=='5'
    assert fact['tax_basis']==TAX_BASIS and fact['tax_evidence']==TAX_EVIDENCE
    assert fact['unit']==CONFIRMED_BASE_UNIT
    assert fact['unit_evidence']==(ERP_UNIT_EVIDENCE if named_units is True else
        USER_UNIT_EVIDENCE if named_units is False else USER_OVERRIDE_UNIT_EVIDENCE)
    assert json.loads(fact['raw_unit_names'])==dict.fromkeys(('wms','order','inventory'),client.unit_label)
    if named_units is False:
        assert snapshot['records']['orders'][0]['unit_name']==''
        assert snapshot['records']['inventory'][0]['unit_name']==''
    else:
        assert snapshot['records']['orders'][0]['unit_name']==client.unit_label
        assert snapshot['records']['inventory'][0]['unit_name']==client.unit_label
    assert not {'total','price','assprice','tax','sell_price'} & snapshot['records']['orders'][0].keys()
    receipt_state={'version':1,'profile_id':snapshot['profile_id'],'events':{},'balances':{},'orders':{},'scope_periods':{}}
    calculation=calculate_query_snapshot(snapshot,receipt_state);version=publish_snapshot(store,snapshot,calculation)
    with store.connect(write=True) as db:
        db.execute('INSERT INTO logistics_calculations VALUES (?,?,?,?,?)',
            ('priced-source',snapshot['profile_id'],version,dumps(calculation),snapshot['source_updated_at']))
    daily=DailyWork(store,auto_project=False);daily.refresh();CustomerOrders(store,daily).reconcile()
    result=refresh_price_bounds(store)
    assert result['published']==1
    with store.connect() as db:
        cap=dict(db.execute('SELECT * FROM purchase_price_bounds').fetchone())
        assert cap['weighted_sale_price']=='11.3' and cap['price_limit']=='9.04' and cap['data_version']==version
        assert cap['unit']==CONFIRMED_BASE_UNIT
        assert db.execute('SELECT count(*) FROM wms_sale_price_facts').fetchone()[0]==1
        stored=db.execute('SELECT unit_evidence,source_hash FROM wms_sale_price_facts').fetchone()
        assert stored['unit_evidence']==fact['unit_evidence']
        assert stored['source_hash']==fact['source_hash']
        assert len(db.execute('PRAGMA table_info(wms_sale_price_facts)').fetchall())==17
        public=json.loads(db.execute("SELECT payload FROM records WHERE kind='orders'").fetchone()[0])
        assert 'inclusive_amount' not in public and 'total' not in public
    cached=WmsCache(store).get('10',{'A#2'})['A#2']['goodslist'][0]
    assert cached['total']=='56.5' and cached['price']=='999' and cached['assprice']=='999'
    assert cached['unitname']==client.unit_label


def test_old_private_cache_has_no_price_until_full_wms_recapture(store):
    cache=WmsCache(store);client=Client();now=datetime.now(ZoneInfo('Asia/Shanghai'))
    old=client.call('erp.ewms.stockoutbill.list',{'timetype':0,'pagesize':200})['orders']
    cache.commit('10',{'A#2'},old,now,full=True)
    current=PriceClient();current.incremental_empty=True
    source=LiveSource(current,warehouse_id='20',wms_cache=cache)
    assert source.collect()['wms_sale_price_facts']==[]
    full=source.collect(full_reconcile=True)
    assert full['wms_sale_price_facts'][0]['inclusive_amount']=='56.5'
    assert full['source_counts']['wms_cache_mode']=='full'


def test_unique_ids_resolve_duplicate_product_lines_but_split_and_reused_rows_do_not():
    items=[{'recid':'1','spec_code':'LS1000','sell_count':'2'},
           {'recid':'2','spec_code':'LS1000','sell_count':'2'}]
    goods=[{'id':'1','usercode':'LS1000','assqty':'2','total':'10'},
           {'id':'2','usercode':'LS1000','assqty':'2','total':'20'}]
    assert [row['total'] for row in match_wms_lines(items,{'goodslist':goods})]==['10','20']
    assert match_wms_lines([{**item,'recid':'unknown'} for item in items],{'goodslist':goods})==[{},{}]
    assert match_wms_lines([{**item,'recid':'unknown'} for item in items],{'goodslist':goods[:1]})==[{},{}]
    split=[{**row,'sourcedetailid':'source'} for row in goods]
    assert match_wms_lines([{'recid':'source','spec_code':'LS1000','sell_count':'2'}],{'goodslist':split})==[{}]


@pytest.mark.parametrize('changes',[
    {'total':None},{'tax':None},{'tax':'13'},{'unitrate':'100'},{'unit':'2'},
    {'assqty':'2'},{'qty':'2'},{'total':'0'},{'unitname':'2'},{'ptypeid':'wrong'},
])
def test_missing_or_incompatible_price_unit_tax_evidence_never_produces_fact(changes):
    row=price_order('PO#1','10')
    assert fact_for(row,'10',**changes) is None


def test_unnamed_base_unit_decimal_quantities_use_explicit_user_confirmation(board):
    row=price_order('PO#1','10','2.00');row['unit_name']='';publish(board,[row])
    fact=fact_for(row,'10',unit='',qty='2.0',assqty=2.0)
    assert fact['unit']==CONFIRMED_BASE_UNIT and fact['quantity']=='2' and fact['unit_evidence']==USER_UNIT_EVIDENCE
    with board.store.connect(write=True) as db:persist_sale_price_facts(db,snapshot_parts([row],[fact]),'v1')
    assert refresh_price_bounds(board.store)['published']==1
    with board.store.connect() as db:
        cap=dict(db.execute('SELECT * FROM purchase_price_bounds').fetchone())
    assert cap['unit']==CONFIRMED_BASE_UNIT and cap['weighted_sale_price']=='5' and cap['price_limit']=='4'


def test_named_and_user_confirmed_unnamed_same_base_units_merge_with_individual_provenance(board):
    rows=[price_order('PO#1','10'),price_order('PO#2','20')];rows[1]['unit_name']='';publish(board,rows)
    facts=[fact_for(rows[0],'10'),fact_for(rows[1],'20',unit='')]
    with board.store.connect(write=True) as db:persist_sale_price_facts(db,snapshot_parts(rows,facts),'v1')
    assert facts[0]['unit_evidence']==ERP_UNIT_EVIDENCE and facts[1]['unit_evidence']==USER_UNIT_EVIDENCE
    assert refresh_price_bounds(board.store)['published']==1


def test_mixed_raw_labels_are_audited_without_changing_base_unit_quantity():
    row=price_order('PO#1','10');row['unit_name']='个'
    fact=fact_for(row,'10','箱')
    assert fact['unit']=='支' and fact['quantity']=='1'
    assert fact['unit_evidence']==USER_OVERRIDE_UNIT_EVIDENCE
    assert json.loads(fact['raw_unit_names'])=={'wms':'箱','order':'个','inventory':'箱'}
    changed=deepcopy(fact);changed['raw_unit_names']=dumps({'wms':'箱','order':'件','inventory':'箱'})
    with pytest.raises(ValueError):
        validate_sale_price_facts(snapshot_parts([row],[changed]))
    snapshot=snapshot_parts([row],[fact]);snapshot['records']['inventory']=[{
        **{key:row[key] for key in ('profile_id','warehouse_id','product_id','sku_id','base_unit')},
        'unit_name':'PCS'}]
    with pytest.raises(ValueError):validate_sale_price_facts(snapshot)


def test_pre_unit_evidence_schema_upgrades_additively_and_needs_new_collection(board):
    rows=[price_order('PO#1','10')];publish(board,rows)
    fact=fact_for(rows[0],'10')
    old_fields=('profile_id','order_no','line_id','product_identity','erp_code',
        'quantity','inclusive_amount','unit','tax_basis','tax_evidence','raw_tax',
        'source_doc_id','source_line_id','source_hash','source_updated_at')
    with board.store.connect(write=True) as db:
        columns=['data_version',*old_fields]
        db.execute('CREATE TABLE wms_sale_price_facts ('+','.join(name+' TEXT NOT NULL' for name in columns)+')')
        db.execute('INSERT INTO wms_sale_price_facts VALUES ('+','.join('?' for _ in columns)+')',
            ('v1',*[fact[key] for key in old_fields]))
        ensure_fact_schema(db)
    # A later initialization/connection must neither drop facts nor pretend
    # their old unspecified unit provenance has already been verified.
    with board.store.connect(write=True) as db:
        ensure_fact_schema(db)
        old=dict(db.execute('SELECT * FROM wms_sale_price_facts').fetchone())
        assert old['unit_evidence']==''
        assert old['source_hash']==fact['source_hash']
        assert len(db.execute('PRAGMA table_info(wms_sale_price_facts)').fetchall())==17
    assert refresh_price_bounds(board.store)['published']==0
    with board.store.connect(write=True) as db:persist_sale_price_facts(db,snapshot_parts(rows,[fact]),'v1')
    assert refresh_price_bounds(board.store)['published']==1


def test_two_same_product_lines_and_different_order_quantities_weight_once(board):
    rows=[price_order('PO#1','20','2','one'),price_order('PO#1','30','2','two'),
          price_order('PO#2','60','6','one')]
    publish(board,rows)
    # publish helper assigns warehouse and sets generation; import exact same
    # stable lines privately only after its full snapshot has been published.
    facts=[fact_for(row,amount) for row,amount in zip(rows,('20','30','60'))]
    with board.store.connect(write=True) as db:
        persist_sale_price_facts(db,snapshot_parts(rows,facts),'v1')
    assert refresh_price_bounds(board.store)['published']==1
    with board.store.connect() as db:
        cap=dict(db.execute('SELECT * FROM purchase_price_bounds').fetchone())
    assert cap['weighted_sale_price']=='11' and cap['price_limit']=='8.8'
    assert cap['product_identity']=='P|'+rows[0]['warehouse_id']+'|LS1000-p|LS1000-s|1'


def test_any_uncovered_line_or_unit_difference_withholds_entire_task_and_clears_old_caps(board):
    rows=[price_order('PO#1','10'),price_order('PO#2','20')];publish(board,rows)
    with board.store.connect(write=True) as db:persist_sale_price_facts(db,snapshot_parts(rows,[fact_for(row,'10') for row in rows]),'v1')
    assert refresh_price_bounds(board.store)['published']==1
    with board.store.connect(write=True) as db:db.execute("DELETE FROM wms_sale_price_facts WHERE order_no='PO#2'")
    result=refresh_price_bounds(board.store)
    assert result['published']==0 and result['review']==1
    with board.store.connect() as db:assert db.execute('SELECT count(*) FROM purchase_price_bounds').fetchone()[0]==0
    rows[1]['base_unit']='2'
    assert fact_for(rows[1],'20','箱') is None


def test_snapshot_switch_and_stale_projection_never_inherit_old_price_facts(board):
    rows=[price_order('PO#1','10')];publish(board,rows)
    with board.store.connect(write=True) as db:persist_sale_price_facts(db,snapshot_parts(rows,[fact_for(rows[0],'10')]),'v1')
    assert refresh_price_bounds(board.store)['published']==1
    with board.store.connect(write=True) as db:db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)",(dumps('v2'),))
    result=refresh_price_bounds(board.store)
    assert result['status']=='tasks_not_current' and result['published']==0
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_settings SET value='v2' WHERE key='projection_data_version'")
        db.execute("UPDATE daily_work_tasks SET source_data_version='v2'")
    assert refresh_price_bounds(board.store)['published']==0


def test_private_amount_change_invalidates_bound_when_public_version_stays_the_same(board):
    rows=[price_order('PO#1','10')];publish(board,rows)
    snapshot=snapshot_parts(rows,[fact_for(rows[0],'10')])
    with board.store.connect(write=True) as db:persist_sale_price_facts(db,snapshot,'v1')
    assert refresh_price_bounds(board.store)['published']==1
    with board.store.connect() as db:before=dict(db.execute('SELECT * FROM purchase_price_bounds').fetchone())
    # Independently bind the actual cap inputs, even if an internal repair
    # accidentally retains the old evidence hash.
    snapshot['wms_sale_price_facts'][0]['inclusive_amount']='20'
    with board.store.connect(write=True) as db:persist_sale_price_facts(db,snapshot,'v1')
    assert refresh_price_bounds(board.store)['published']==1
    with board.store.connect() as db:after=dict(db.execute('SELECT * FROM purchase_price_bounds').fetchone())
    assert before['data_version']==after['data_version']=='v1'
    assert after['source_hash']!=before['source_hash']
    assert after['weighted_sale_price']=='20' and after['price_limit']=='16'


@pytest.mark.parametrize('review_field',['shadow_demand','lifecycle_pending_review'])
def test_lifecycle_review_lines_cannot_supply_procurement_caps(board,review_field):
    rows=[price_order('PO#1','10')];publish(board,rows)
    with board.store.connect(write=True) as db:
        persist_sale_price_facts(db,snapshot_parts(rows,[fact_for(rows[0],'10')]),'v1')
        row=deepcopy(rows[0]);row[review_field]=True
        db.execute("UPDATE records SET payload=? WHERE kind='orders'",(dumps(row),))
    assert refresh_price_bounds(board.store)['published']==0


def test_invalid_price_stage_rolls_back_whole_publish_and_same_public_version_can_change_price(store):
    client=PriceClient();source=LiveSource(client,warehouse_id='20');snapshot=source.collect()
    version=publish_snapshot(store,snapshot,calculate_query_snapshot(snapshot))
    changed=deepcopy(snapshot);changed['wms_sale_price_facts'][0]['inclusive_amount']='20'
    assert publish_snapshot(store,changed,calculate_query_snapshot(changed))==version
    with store.connect() as db:assert db.execute('SELECT inclusive_amount FROM wms_sale_price_facts').fetchone()[0]=='20'
    invalid=deepcopy(changed);invalid['wms_sale_price_facts'][0]['quantity']='500'
    with pytest.raises(SourceIncomplete):publish_snapshot(store,invalid,calculate_query_snapshot(invalid))
    with store.connect() as db:assert db.execute('SELECT inclusive_amount FROM wms_sale_price_facts').fetchone()[0]=='20'
