"""New inventory view freezes filtered whole orders, never card-wide extras."""
import json

import pytest

from core.daily_work import DailyWorkConflict, PRINT_REVIEW_BLOCK
from core.order_print_state import ensure_schema as ensure_print_schema, invalidate_changed_print_state
from core.store import dumps
from test_daily_work import store, order, seed, user
from test_task_board import board, publish
from test_task_board_two_stage import set_print


def listing(board, **filters):
    return board.list(user('packer',['stock_packing']),'stock_packing',packing_mode='print',**filters)


def selection_body(result, **extra):
    return {'role':'stock_packing','view':'mine','packing_mode':'print','purpose':'print',
        'ids':[card['id'] for card in result['items']],
        'data_version':result['data_version'],'task_version':result['task_version'],
        'expected_print_state_version':result['print_state_version'],**extra}


def stored_scope(board,result):
    with board.store.connect() as db:
        return json.loads(db.execute('SELECT payload FROM daily_work_selections WHERE id=?',
            (result['selection_id'],)).fetchone()[0])


@pytest.mark.parametrize('status',['in_progress','waiting','packed','resolved'])
def test_legacy_manual_facts_do_not_start_new_print_view(board,status):
    publish(board,[order('ERP1','LS1','LS库存件')])
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET status=? WHERE role='stock_packing'",(status,))
    assert board.list(user('packer',['stock_packing']),'stock_packing',work_state='in_progress')['total']==1
    assert listing(board,work_state='todo')['total']==1
    assert listing(board,work_state='in_progress')['total']==0


@pytest.mark.parametrize('filters,expected',[
    ({'customer':'客户甲'},['ERP1','ERP3']),({'customer':'客户乙'},['ERP2']),
    ({'sku':'LS1','order_no':'ERP2'},['ERP2']),({'order_no':'PO-B'},['ERP2']),
    ({'keyword':'仅乙'},['ERP2']),({'order_date_from':'2026-10-02','order_date_to':'2026-10-03'},['ERP2']),
    ({'due_from':'2026-10-07','due_to':'2026-10-07'},['ERP2']),
    ({'print_state':'printed'},['ERP2']),
])
def test_filters_apply_to_orders_before_same_product_aggregation(board,filters,expected):
    rows=[order('ERP1','LS1','LS库存件',customer='客户甲',document_date='2026-10-01'),
          order('ERP2','LS1','LS库存件',customer='客户乙',document_date='2026-10-02',due='2026-10-07',product_name='仅乙商品'),
          order('ERP3','LS1','LS库存件',customer='客户甲',document_date='2026-10-04')]
    rows[1]['summary']=rows[1]['system_note']=rows[1]['summary'].replace('PO#','PO-B#',1)
    publish(board,rows);set_print(board,'ERP2','printed')
    result=listing(board,**filters)
    actor=user('packer',['stock_packing'])
    chosen=board.select(actor,selection_body(result,**filters))
    assert chosen['order_nos']==expected and chosen['order_count']==len(expected)
    assert stored_scope(board,chosen)['purpose']=='print'
    exported=board.export_snapshot(actor,{'role':'stock_packing','packing_mode':'print',
        'ids':[card['id'] for card in result['items']], 'expected_data_version':result['data_version'],
        'expected_task_version':result['task_version'],'expected_print_state_version':result['print_state_version'],**filters})
    assert sorted(order['order_no'] for card in exported['items'] for order in card['orders'])==expected
    for card in result['items']:
        detail=board.detail(actor,card['id'],'stock_packing',packing_mode='print',**filters)
        assert {line['order_no'] for line in detail['lines']}<=set(expected)


def test_same_sku_phase_counts_and_print_purpose_preserve_every_order(board):
    publish(board,[order('ERP1','LS1','LS库存件'),order('ERP2','LS1','LS库存件')])
    set_print(board,'ERP1','uncertain')
    with board.store.connect(write=True) as db:
        row=db.execute("SELECT id,payload FROM daily_work_tasks WHERE role='stock_packing' AND order_no='ERP1'").fetchone()
        payload=json.loads(row['payload']);payload['blocked_by']=PRINT_REVIEW_BLOCK
        db.execute("UPDATE daily_work_tasks SET status='waiting',payload=? WHERE id=?",(dumps(payload),row['id']))
    pending=listing(board,work_state='todo');progress=listing(board,work_state='in_progress')
    assert pending['summary']['work_state_counts']==progress['summary']['work_state_counts']=={'todo':1,'in_progress':1}
    assert pending['order_count']==progress['order_count']==1
    chosen=board.select(user('packer',['stock_packing']),selection_body(progress,work_state='in_progress'))
    assert chosen['order_nos']==['ERP1']
    with pytest.raises(DailyWorkConflict):
        board.select(user('packer',['stock_packing']),selection_body(progress,purpose='action',work_state='in_progress'))
    with pytest.raises(DailyWorkConflict) as error:
        board.batch(user('packer',['stock_packing']),{'selection_id':chosen['selection_id'],'action':'start','idempotency_key':'wrong-purpose'})
    assert error.value.code=='selection_purpose_conflict'


def test_reserved_blocks_entire_print_selection_but_image_scope_is_readonly(board):
    publish(board,[order('ERP1','LS1','LS库存件'),order('ERP2','LS2','LS库存件')])
    set_print(board,'ERP2','reserved')
    result=listing(board)
    with pytest.raises(DailyWorkConflict) as error:
        board.select(user('packer',['stock_packing']),selection_body(result,all=True))
    assert error.value.code=='print_state_conflict'
    with board.store.connect() as db:assert db.execute('SELECT count(*) FROM daily_work_selections').fetchone()[0]==0
    readonly=board.select(user('packer',['stock_packing']),selection_body(result,purpose='image_export',all=True))
    assert readonly['order_nos']==['ERP1','ERP2']
    with board.store.connect() as db:
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='ERP2'").fetchone()[0]=='reserved'


def test_reserved_selection_names_actual_lock_operator_not_task_assignee(board):
    publish(board,[order('ERP1','LS1','LS库存件')]);set_print(board,'ERP1','reserved')
    with board.store.connect(write=True) as db:
        db.execute('INSERT INTO print_jobs(id,status,frozen,metadata) VALUES (?,?,?,?)',
            ('job','prepared','{}',dumps({'actor_id':'real-operator','actor_name':'实际打印人乙'})))
    result=listing(board)
    assert result['items'][0]['assignee_name']=='包装甲'
    with pytest.raises(DailyWorkConflict) as error:
        board.select(user('packer',['stock_packing']),selection_body(result))
    assert '实际打印人乙' in str(error.value) and '包装甲' not in str(error.value)


def test_print_selection_does_not_grant_owner_or_identity_permissions(board):
    publish(board,[order('ERP1','LS1','LS库存件')]);set_print(board,'ERP1','printed')
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET assignee_id='' WHERE role='stock_packing'")
    admin=user('admin',[],admin=True)
    result=board.list(admin,'stock_packing',view='role',packing_mode='print')
    assert not result['items'][0]['can_print']
    with pytest.raises(DailyWorkConflict):board.select(admin,selection_body(result,view='role'))
    assert board.select(admin,selection_body(result,view='role',purpose='image_export'))['order_count']==1


def test_filter_scope_and_print_version_cannot_expand_old_selection(board):
    publish(board,[order('ERP1','LS1','LS库存件'),order('ERP2','LS1','LS库存件')])
    result=listing(board,order_no='ERP1')
    with pytest.raises(DailyWorkConflict):
        board.select(user('packer',['stock_packing']),selection_body(result))
    chosen=board.select(user('packer',['stock_packing']),selection_body(result,order_no='ERP1'))
    set_print(board,'ERP2','printed')
    with pytest.raises(DailyWorkConflict) as error:
        board.select(user('packer',['stock_packing']),selection_body(result,order_no='ERP1'))
    assert error.value.code=='print_state_version_changed'
    assert stored_scope(board,chosen)['order_nos']==['ERP1']


def test_sorted_query_cached_once_for_pages_detail_and_cross_page_selection(board,monkeypatch):
    rows=[order(f'ERP{i:03d}',f'LS{i:03d}','LS库存件',document_date=f'2026-09-{i%28+1:02d}',location=f'B{i+1}-1') for i in range(120)]
    publish(board,rows);calls=[]
    original=board._build_uncached
    def build(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(board,'_build_uncached',build)
    pages=[listing(board,sort='location',page_size=30,page=i) for i in range(1,5)]
    assert len(calls)==1
    assert all(page['total']==120 and page['order_count']==120 for page in pages)
    assert len({card['id'] for page in pages for card in page['items']})==120
    assert pages[0]['items'][0]['location']=='B1-1' and pages[-1]['items'][-1]['location']=='B120-1'
    actor=user('packer',['stock_packing'])
    board.detail(actor,pages[0]['items'][0]['id'],'stock_packing',packing_mode='print',sort='location')
    assert len(calls)==1
    result=board.select(actor,selection_body(pages[0],sort='location',ids=[pages[0]['items'][0]['id'],pages[3]['items'][-1]['id']]))
    assert result['card_count']==result['order_count']==2
    assert listing(board,sort='order_date')['items'][0]['order_date']=='2026-09-01'


def test_content_change_keeps_phase_but_removed_source_leaves_current_views(board):
    publish(board,[order('ERP1','LS1','LS库存件')]);set_print(board,'ERP1','printed')
    with board.store.connect(write=True) as db:
        assert invalidate_changed_print_state(db,'P','ERP1','new-fingerprint')
        row=db.execute("SELECT id,payload FROM daily_work_tasks WHERE role='stock_packing'").fetchone()
        payload=json.loads(row['payload']);payload.update(label_review_required=True,blocked_by='订单标签内容已变化，需重新核对包装结果')
        db.execute('UPDATE daily_work_tasks SET payload=? WHERE id=?',(dumps(payload),row['id']))
    active=listing(board,work_state='in_progress')['items'][0]
    assert active['work_label']=='标签内容变化待核对' and not active['can_print']
    # An explicit later correction must never resurrect an older printed audit.
    with board.store.connect(write=True) as db:
        db.execute("UPDATE order_print_states SET reason='manual_reset' WHERE order_no='ERP1'")
    assert listing(board,work_state='todo')['total']==1
    set_print(board,'ERP1','printed')
    seed(board.store,[],{},version='v2');board.daily.refresh();board.customers.reconcile()
    assert listing(board,work_state='in_progress')['total']==0
    with board.store.connect() as db:
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='ERP1'").fetchone()[0]=='printed'


def test_latest_print_actor_is_a_fact_not_current_assignee_and_invalidates_cache(board):
    publish(board,[order('ERP1','LS1','LS库存件')]);set_print(board,'ERP1','printed')
    first=listing(board)
    assert first['items'][0]['latest_print_actor_name']==''
    with board.store.connect(write=True) as db:
        db.execute('''INSERT INTO order_print_audit
            (id,profile_id,order_no,from_state,to_state,device_id,job_id,reason,created_at,actor_id,actor_name)
            VALUES ('audit1','P','ERP1','reserved','printed','device','job','submitted','2026-10-04T09:03:00','former','原打印人')''')
    current=listing(board)
    assert current['print_state_version']==first['print_state_version']
    assert current['items'][0]['assignee_name']=='包装甲'
    assert current['items'][0]['latest_print_actor_name']=='原打印人'
    assert current['items'][0]['latest_print_at']=='2026-10-04T09:03:00'
    line=board.detail(user('packer',['stock_packing']),current['items'][0]['id'],'stock_packing',packing_mode='print')['lines'][0]
    assert line['latest_print_actor_id']=='former' and len(line['state_revision'])==64


def test_delivery_and_order_date_sort_use_filtered_member_dates(board):
    rows=[order('ERP1','LS1','LS库存件',due='2026-10-09',document_date='2026-10-03'),
          order('ERP2','LS2','LS库存件',due='2026-10-07',document_date='2026-10-04'),
          order('ERP3','LS3','LS库存件',due='2026-10-08',document_date='2026-10-02')]
    publish(board,rows)
    assert [card['sku'] for card in listing(board,sort='delivery_date')['items']]==['LS2','LS3','LS1']
    assert [card['sku'] for card in listing(board,sort='order_date')['items']]==['LS3','LS1','LS2']


def test_shared_summary_po_does_not_require_line_sku_or_quantity_match(board):
    item=order('ERP1','LS1','LS库存件')
    item['summary']=item['system_note']=item['summary'].replace('PO#','WRONG-PO#',1).replace('#1#LS1','#9#LS1')
    publish(board,[item])
    assert listing(board,order_no='WRONG-PO')['total']==1
    assert listing(board,order_no='ERP1')['total']==1


@pytest.mark.parametrize('state',['unprinted','printed'])
def test_non_unfulfilled_orders_exit_columns_before_lifecycle_confirmation(board,state):
    publish(board,[order('ERP1','LS1','LS库存件')]);set_print(board,'ERP1',state)
    item=order('ERP1','LS1','已完成订单需结存',process_state='500',
        lifecycle_state='shipped_unposted',confirmation_count=1)
    publish(board,[item])
    expected='in_progress' if state=='printed' else 'todo'
    assert listing(board,work_state=expected)['total']==0
    publish(board,[{**item,'confirmation_count':2}])
    assert listing(board,work_state='todo')['total']==listing(board,work_state='in_progress')['total']==0
    with board.store.connect() as db:
        # ERP closure leaves the old printing fact intact as business history.
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='ERP1'").fetchone()[0]==state


@pytest.mark.parametrize('filters',[{'page_size':0},{'page_size':101},{'sort':'price'},
    {'print_state':'pending'},{'order_date_from':'2026-02-30'},{'due_from':'2026-10-02','due_to':'2026-10-01'}])
def test_new_print_filters_validate_before_loading(board,filters):
    with pytest.raises(ValueError):listing(board,**filters)


def test_other_roles_and_legacy_page_sizes_remain_unchanged(board):
    publish(board,[order('ERP1','LS1','LS库存件')])
    admin=user('admin',[],admin=True)
    for role in ('purchase','sales'):
        with pytest.raises(ValueError):board.list(admin,role,view='role',packing_mode='print')
    with pytest.raises(ValueError):board.list(admin,'stock_packing',view='role',page_size=20)
    assert listing(board)['page_size']==50
    assert board.list(admin,'arrival_packing',view='role',packing_mode='print',page_size=20)['page_size']==20
