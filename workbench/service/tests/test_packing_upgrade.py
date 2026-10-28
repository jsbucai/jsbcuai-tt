import json
import time
from pathlib import Path
import pytest
from test_task_board import board,publish,WAREHOUSE
from test_daily_work import order,user,seed,store
from test_task_board_two_stage import set_print
from core.store import dumps
from core.business_confirmations import create,exchange,pending
from tools.weixin_sales_spool import Spool
from tools.weixin_business_confirmation import ingest


def test_partial_order_search_literal_and_customer_options_cover_all_pages(board):
    rows=[order(f'QJ0804#{i}','LS1','LS库存件') for i in range(65)]
    rows[-1]['receiver_address']='客户乙'
    rows[-1]['customer_name']='客户乙'
    rows[-1]['wms_receiveraddress']='客户乙'
    publish(board,rows)
    admin=user('admin',[],True)
    result=board.list(admin,'stock_packing','role',packing_mode='print',order_no='qj0804#6',order_no_match='contains',page_size=1)
    assert result['order_count']==6 # 6,60,61,62,63,64
    assert board.list(admin,'stock_packing','role',packing_mode='print',order_no='0804',order_no_match='contains')['order_count']==65
    assert board.list(admin,'stock_packing','role',packing_mode='print',order_no='%_',order_no_match='contains')['total']==0
    options=board.filter_options(admin,'stock_packing','role')['customers']
    assert len(options)==2
    for item in options:
        filtered=board.list(admin,'stock_packing','role',packing_mode='print',customer_key=item['id'])
        for card in filtered['items']:
            detail=board.detail(admin,card['id'],'stock_packing','role',packing_mode='print',customer_key=item['id'])
            assert {line['customer'] for line in detail['lines']}=={item['name']}


def test_arrival_partial_visible_readonly_and_printed_overdue_stays_processing(board):
    rows=[order('ARR1','LS1','需采购'),order('ARR2','LS2','异常')]
    for row in rows:row['warehouse_id']=WAREHOUSE
    allocations=[]
    for item in rows:
        partial=item['order_no']=='ARR1'
        allocations.append({'demand':{'order':item['order_no'],'sku':item['product_id'],'product_code':item['sku'],
            'warehouse':json.dumps(['P',WAREHOUSE,item['sku_id'],item['base_unit']]),'quantity':20},
            'actual':18 if partial else 20,'shortage':2 if partial else 0,'pending':0,
            'receipt_allocations':[{'event_id':item['order_no'],'date':'2026-09-20','document':'IN1','quantity':18 if partial else 20,
                'supplier_id':'supplier-a','supplier_name':'供应商甲','type':'进货入库单'}]})
    seed(board.store,rows,{r['order_no']:r['logistics_company'] for r in rows},allocations)
    board.daily.refresh();board.customers.reconcile()
    admin=user('admin',[],True)
    todo=board.list(admin,'arrival_packing','role',packing_mode='print',work_state='todo')
    assert todo['order_count']==1 and todo['items'][0]['arrival_partial']
    assert not todo['items'][0]['can_print'] and todo['items'][0]['arrival_shortage']=='2'
    options=board.filter_options(admin,'arrival_packing','role')['suppliers']
    assert len(options)==1 and options[0]['name']=='供应商甲'
    exception=board.list(admin,'arrival_packing','role',packing_mode='print',work_state='exception')
    assert exception['order_count']==1
    set_print(board,'ARR2','printed')
    progress=board.list(admin,'arrival_packing','role',packing_mode='print',work_state='in_progress',supplier=options[0]['id'])
    assert progress['order_count']==1 and progress['items'][0]['arrival_overdue']
    assert board.list(admin,'arrival_packing','role',packing_mode='print',work_state='exception')['order_count']==0


def test_print_does_not_rebuild_customer_source_index_and_heartbeat_keeps_cache(board,monkeypatch):
    publish(board,[order('PO1','LS1','LS库存件')])
    actor=user('packer',['stock_packing'])
    board.list(actor,'stock_packing',packing_mode='print',compact=True)
    def forbidden(*args,**kwargs):raise AssertionError('unrelated write rebuilt customer source')
    monkeypatch.setattr(board.customers,'_build',forbidden)
    set_print(board,'PO1','printed')
    assert board.list(actor,'stock_packing',packing_mode='print')['items'][0]['work_state']=='in_progress'
    with board.store.connect(write=True) as db:db.execute("INSERT OR REPLACE INTO settings VALUES ('adjustment_web_heartbeat','now')")
    assert board.list(actor,'stock_packing',packing_mode='print')['total']==1


def test_printed_order_becoming_partial_is_visible_once_with_current_gap(board):
    row=order('ARR1','LS1','来货件');row['warehouse_id']=WAREHOUSE;row['quantity']='20'
    def publish_quantity(quantity,category,version):
        item={**row,'logistics_company':category}
        allocation={'demand':{'order':'ARR1','sku':row['product_id'],'product_code':row['sku'],
            'warehouse':json.dumps(['P',WAREHOUSE,row['sku_id'],row['base_unit']]),'quantity':20},
            'actual':quantity,'shortage':20-quantity,'pending':0,
            'receipt_allocations':[{'event_id':'IN1','date':'2026-10-03','document':'IN1','quantity':quantity,'type':'进货入库单'}]}
        seed(board.store,[item],{'ARR1':category},[allocation],version=version)
        board.daily.refresh();board.customers.reconcile()
    publish_quantity(20,'来货件','v1');set_print(board,'ARR1','printed')
    publish_quantity(18,'需采购','v2')
    actor=user('admin',[],True)
    page=board.list(actor,'arrival_packing','role',packing_mode='print',work_state='in_progress')
    assert page['order_count']==1 and len(page['items'])==1
    card=page['items'][0]
    assert card['quantity']=='20' and card['arrival_received']=='18' and card['arrival_shortage']=='2'
    assert not card['can_print']
    detail=board.detail(actor,card['id'],'arrival_packing','role',packing_mode='print')
    assert len(detail['lines'])==1


def test_business_confirmation_exact_owner_scope_expiry_and_replay(store,tmp_path):
    root=tmp_path/'spool';root.mkdir(mode=0o700)
    spool=Spool(root)
    code=create(store,'printing_stopped',{'order':'QJ0804#26','job':'frozen'},'仅确认旧手机打印流程已退出')
    exchange(store,spool,'owner')
    assert pending(store)[0]['status']=='pending'
    def message(text,identity):return {'message_id':identity,'item_list':[{'type':1,'text_item':{'text':text}}]}
    received=[]
    ingest((spool,'owner'),message('已经好了','m0'),received.append)
    assert received and pending(store)[0]['status']=='pending'
    ingest((spool,'other'),message('确认事项 '+code,'m1'),None)
    exchange(store,spool,'owner');assert pending(store)[0]['status']=='pending'
    ingest((spool,'owner'),message('确认事项 '+code,'m2'),None)
    ingest((spool,'owner'),message('确认事项 '+code,'m2'),None)
    exchange(store,spool,'owner')
    assert pending(store)[0]['status']=='approved'
    with store.connect() as db:assert db.execute('SELECT receipt FROM business_confirmations WHERE id=?',(code,)).fetchone()[0]=='m2'


def test_expired_confirmation_remains_visible_without_execution_authority(store,tmp_path):
    root=tmp_path/'spool';root.mkdir(mode=0o700);spool=Spool(root)
    code=create(store,'business_review',{'order':'QJ0804#26'},'仍需处理',ttl=-1)
    exchange(store,spool,'owner')
    assert pending(store)[0]['id']==code and pending(store)[0]['status']=='expired'
    assert pending(store)[0]['delivery']=='pending'
