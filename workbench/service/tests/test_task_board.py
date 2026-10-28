import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.daily_work import DailyWork, DailyWorkConflict
from core.customer_orders import CustomerOrders, WAREHOUSE
from core.task_board import TaskBoard
from core.order_print_state import ensure_schema as ensure_print_schema
from core.store import dumps
from test_daily_work import store, order, seed, user, enable_assignment, NOW


@pytest.fixture
def board(store, monkeypatch):
    for module in ('core.daily_work','core.customer_orders','core.task_board'):
        monkeypatch.setattr(module+'.timestamp',lambda:'2026-10-04T09:00:00+08:00')
    daily=DailyWork(store,now=lambda:NOW,auto_project=False)
    enable_assignment(daily)
    return TaskBoard(store,daily,CustomerOrders(store,daily))


def publish(board, rows, source='2026-10-04T08:00:00+08:00'):
    for item in rows:
        item['warehouse_id']=WAREHOUSE
    allocations=[]
    for item in rows:
        category=item['logistics_company']
        quantity=item['quantity']
        allocations.append({'demand':{'order':item['order_no'],'sku':item['product_id'],
            'product_code':item['sku'],'warehouse':json.dumps(['P',WAREHOUSE,item['sku_id'],item['base_unit']]),'quantity':quantity},
            'actual':quantity if category.endswith('库存件') else '0',
            'pending':quantity if category=='待到货' else '0',
            'shortage':quantity if category=='需采购' else '0'})
    seed(board.store,rows,{item['order_no']:item['logistics_company'] for item in rows},allocations,source=source)
    board.daily.refresh();board.customers.reconcile()


def pick(board, actor, role, **extra):
    listing=board.list(actor,role)
    return board.select(actor,{'role':role,'view':'mine','all':True,
        'data_version':listing['data_version'],'task_version':listing['task_version'],**extra})


def test_dashboard_counts_unique_customer_orders_and_matches_role_board(board):
    rows=[order('PO#1','LS1000','LS库存件'),order('PO#2','LS2000','需采购')]
    publish(board,rows)
    admin=user('admin',list(('sales','purchase','stock_packing','arrival_packing')),admin=True)
    result=board.dashboard(admin)
    assert result['company']['customer_orders']==1
    assert result['company']['unfinished_customer_orders']==1
    assert {entry['role'] for entry in result['roles']}=={'sales','purchase','stock_packing','arrival_packing'}
    for entry in result['roles']:
        assert entry['summary']['total']==board.list(admin,entry['role'],view='role')['total']
    with pytest.raises(DailyWorkConflict) as error:
        board.dashboard(user('buyer',['purchase']))
    assert error.value.code=='admin_required'


def test_dashboard_completed_work_keeps_actual_actor_after_transfer(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    admin=user('admin',['sales','purchase','stock_packing','arrival_packing'],admin=True)
    with board.store.connect(write=True) as db:
        task=dict(db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing'").fetchone())
        for identity,event,actor in [('complete-1','packed','packer'),('complete-2','packed','packer'),
                                     ('erp-1','erp_business_completed','admin')]:
            db.execute("INSERT INTO daily_work_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity,task['id'],event,actor,'in_progress','packed','完成','',identity,1,'{}',NOW.isoformat()))
        db.execute("UPDATE daily_work_tasks SET assignee_id='admin' WHERE id=?",(task['id'],))
    result=board.dashboard(admin)
    role=next(row for row in result['roles'] if row['role']=='stock_packing')
    people={row['user_id']:row for row in role['people']}
    assert people['packer']['completed_today']==1
    assert people['admin']['completed_today']==0
    assert role['summary']['business_completed_today']==1
    detail=board.completions(admin,'stock_packing','packer')
    assert detail['total']==1 and detail['items'][0]['task_id']==task['id']
    assert board.completions(admin,'stock_packing',source='erp')['total']==1
    with pytest.raises(DailyWorkConflict):
        board.completions(user('packer',['stock_packing']),'stock_packing')


def test_dashboard_empty_archive_still_marks_current_snapshot_only(board):
    from core.order_history_archive import OrderHistoryArchive
    publish(board,[order('PO#1','LS1000','LS库存件')])
    board.history_archive=OrderHistoryArchive(board.store)
    result=board.dashboard(user('admin',['sales'],admin=True))
    assert result['company']['history_coverage']=='current_snapshot_only'
    assert result['company']['history_review_required'] is True


def test_packing_card_shows_customer_sales_separately_from_worker(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    with board.store.connect(write=True) as db:
        db.execute("UPDATE sales_customer_owners SET source='customer_confirmed',status='assigned',owner_id='sales'")
    card=board.list(user('packer',['stock_packing']),'stock_packing')['items'][0]
    assert card['assignee_id']=='packer'
    assert card['customer_sales_owners'][0]['owner_id']=='sales'


def test_history_remains_visible_to_actual_worker_after_transfer(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('packer',['stock_packing'])
    chosen=pick(board,actor,'stock_packing')
    board.batch(actor,{'selection_id':chosen['selection_id'],'idempotency_key':'before-transfer','action':'start'})
    identity=board.history(actor,'stock_packing')['items'][0]['id']
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET assignee_id='admin' WHERE id=?",(identity,))
    assert board.history(actor,'stock_packing')['total']==1
    assert any(event['actor_id']=='packer' for event in board.history_detail(actor,identity,'stock_packing')['events'])
    assert board.list(actor,'stock_packing')['total']==0
    outsider=user('nobody',['stock_packing'])
    assert board.history(outsider,'stock_packing')['total']==0
    with pytest.raises(DailyWorkConflict):
        board.history_detail(outsider,identity,'stock_packing')


def test_export_snapshot_contains_full_order_lines_without_task_mutations(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('packer',['stock_packing'])
    listing=board.list(actor,'stock_packing')
    identity=listing['items'][0]['id']
    with board.store.connect() as db:
        before=(db.execute('SELECT count(*) FROM daily_work_selections').fetchone()[0],
                db.execute('SELECT count(*) FROM daily_work_batches').fetchone()[0])
    result=board.export_snapshot(actor,{'role':'stock_packing','view':'mine','ids':[identity],
        'expected_data_version':listing['data_version'],'expected_task_version':listing['task_version']})
    assert result['total']==1
    assert result['items'][0]['orders'][0]['items'][0]['sku']=='LS1000'
    assert result['items'][0]['orders'][0]['items'][0]['quantity']=='1'
    with board.store.connect() as db:
        assert before==(db.execute('SELECT count(*) FROM daily_work_selections').fetchone()[0],
                        db.execute('SELECT count(*) FROM daily_work_batches').fetchone()[0])
    with pytest.raises(DailyWorkConflict) as error:
        board.export_snapshot(actor,{'role':'stock_packing','view':'mine','ids':[identity],
            'expected_data_version':'obsolete','expected_task_version':listing['task_version']})
    assert error.value.code=='data_version_changed'
    # A published ERP generation can precede task projection. Matching version
    # tokens alone must not allow an export mixing the two generations.
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_settings SET value='old-generation' WHERE key='projection_data_version'")
    stale=board.list(actor,'stock_packing')
    assert stale['tasks_current'] is False
    with pytest.raises(DailyWorkConflict) as error:
        board.export_snapshot(actor,{'role':'stock_packing','view':'mine','ids':[identity],
            'expected_data_version':stale['data_version'],'expected_task_version':stale['task_version']})
    assert error.value.code=='task_version_changed'


def test_processed_and_archived_task_history_keeps_actions(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('packer',['stock_packing'])
    selected=pick(board,actor,'stock_packing')
    board.batch(actor,{'selection_id':selected['selection_id'],'idempotency_key':'history-start',
                       'action':'start'})
    processed=board.history(actor,'stock_packing')
    assert processed['total']==1 and processed['items'][0]['status']=='in_progress'
    identity=processed['items'][0]['id']
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET condition_state='resolved' WHERE id=?",(identity,))
    archived=board.history(actor,'stock_packing',scope='archived')
    assert archived['total']==1 and archived['items'][0]['id']==identity
    detail=board.history_detail(actor,identity,'stock_packing')
    assert any(event['event_type']=='start' for event in detail['events'])


def test_complete_pages_same_product_and_combination(board):
    rows=[order(f'PO{i}#1',f'LS{i:04d}','LS库存件') for i in range(110)]
    rows += [order('REPEAT#1','LS0000','LS库存件'),order('REPEAT#2','LS0000','LS库存件')]
    multi=order('MIX#1','LSA','LS库存件')
    second=order('MIX#1','LSB','LS库存件',id='MIX#1:2',line_id='2')
    rows += [multi,second]
    publish(board,rows)
    actor=user('packer',['stock_packing'])
    first=board.list(actor,'stock_packing')
    assert first['total']==111 and len(first['items'])==50 and first['has_more']
    pages=[first,board.list(actor,'stock_packing',page=2),board.list(actor,'stock_packing',page=3)]
    ids=[item['id'] for page in pages for item in page['items']]
    assert len(ids)==len(set(ids))==111 and not pages[-1]['has_more']
    same=next(item for page in pages for item in page['items'] if item['sku']=='LS0000')
    assert same['order_count']==3 and same['quantity']=='3'
    detail=board.detail(actor,same['id'],'stock_packing')
    assert len(detail['lines'])==3
    combo=next(item for page in pages for item in page['items'] if item['kind']=='combination')
    detail=board.detail(actor,combo['id'],'stock_packing')
    assert len(detail['lines'])==1 and len(detail['lines'][0]['product_lines'])==2
    assert combo['quantity']==''
    selected=pick(board,actor,'stock_packing')
    assert selected['object_count']==113 and len(selected['order_nos'])==113
    started=board.batch(actor,{'selection_id':selected['selection_id'],'idempotency_key':'begin-all','action':'start'})
    assert started['processed']==113
    replay=board.batch(actor,{'selection_id':selected['selection_id'],'idempotency_key':'begin-all','action':'start'})
    assert replay['replayed']


def test_role_read_access_does_not_hide_other_packable_quantity(board):
    publish(board,[order('PO#1','LS1','LS库存件')])
    with board.store.connect(write=True) as db:
        db.execute("INSERT INTO staff_users SELECT 'other','other','包装乙',password_hash,roles,0,1,0,created_at,updated_at FROM staff_users WHERE id='packer'")
    actor=user('other',['stock_packing'])
    assert board.list(actor,'stock_packing')['total']==0
    all_items=board.list(actor,'stock_packing',view='role')['items']
    assert len(all_items)==1 and all_items[0]['quantity']=='1' and not all_items[0]['can_act']
    with pytest.raises(DailyWorkConflict):
        board.select(actor,{'role':'stock_packing','view':'role','ids':[all_items[0]['id']],
            'data_version':'v1','task_version':board.daily.version()['task_version']})
    with pytest.raises(DailyWorkConflict):board.list(user('sales',['sales']),'stock_packing',view='role')


def test_product_reassignment_preserves_every_historical_operation(board):
    rows=[order(f'PO#{i}','LS1','LS库存件') for i in range(1,7)]
    publish(board,rows)
    ensure_print_schema(board.store)
    with board.store.connect(write=True) as db:
        db.execute("INSERT INTO staff_users SELECT 'other','other','包装乙',password_hash,roles,0,1,0,created_at,updated_at FROM staff_users WHERE id='packer'")
        tasks=db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing' ORDER BY order_no").fetchall()
        # Owner table defines stable same-product destination, legacy tasks split.
        db.execute("UPDATE daily_work_tasks SET assignee_id='other' WHERE role='stock_packing'")
        for i,event in enumerate(('start','transfer')):
            db.execute('INSERT INTO daily_work_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ('history'+str(i),tasks[i]['id'],event,'other','todo','todo','','',None,1,'{}','2026-10-03'))
        db.execute("INSERT INTO order_print_audit(id,profile_id,order_no,from_state,to_state,device_id,job_id,reason,created_at) VALUES ('old','P',?,'reserved','unprinted','d','j','reset','2026-10-03')",(tasks[2]['order_no'],))
        db.execute("UPDATE daily_work_tasks SET status='packed' WHERE id=?",(tasks[3]['id'],))
    board.daily.reconcile_product_assignments()
    with board.store.connect() as db:
        now=db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing' ORDER BY order_no").fetchall()
    assert [row['assignee_id'] for row in now]==['other']*4+['packer']*2


def test_old_published_tasks_migrate_without_erp_refresh(board):
    publish(board,[order('PO#1','LS1','LS库存件')],source='2026-10-03T08:00:00+08:00')
    actor=user('packer',['stock_packing'])
    assert board.list(actor,'stock_packing')['total']==0
    board.daily.reconcile_product_assignments(actor_id='migration-v8')
    listing=board.list(actor,'stock_packing')
    assert listing['total']==1 and listing['stale']


def test_batch_all_or_nothing_version_owner_and_print_guard(board):
    publish(board,[order('PO#1','LS1','LS库存件'),order('PO#2','LS2','LS库存件')])
    actor=user('packer',['stock_packing'])
    selection=pick(board,actor,'stock_packing')
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET version=version+1 WHERE order_no='PO#2'")
    with pytest.raises(DailyWorkConflict):
        board.batch(actor,{'selection_id':selection['selection_id'],'idempotency_key':'atomic','action':'start'})
    with board.store.connect() as db:
        assert db.execute("SELECT count(*) FROM daily_work_tasks WHERE status='in_progress'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM daily_work_batches").fetchone()[0]==0
    fresh=pick(board,actor,'stock_packing')
    board.batch(actor,{'selection_id':fresh['selection_id'],'idempotency_key':'atomic','action':'start'})
    ensure_print_schema(board.store)
    with board.store.connect(write=True) as db:
        db.execute("INSERT INTO order_print_states VALUES ('P','PO#2','uncertain','d','j','f','unknown','2026-10-04')")
    with pytest.raises(DailyWorkConflict):board.batch(actor,{'selection_id':fresh['selection_id'],'idempotency_key':'packed','action':'packed'})
    with board.store.connect() as db:
        assert db.execute("SELECT count(*) FROM daily_work_tasks WHERE status='packed'").fetchone()[0]==0


def test_sales_purchase_batch_waiting_promises_and_daily_record(board):
    publish(board,[order('PO#1','FB1','需采购'),order('PO#2','FB1','需采购'),order('NEXT#1','FB1','待到货')])
    buyer=user('buyer',['purchase']);seller=user('sales',['sales'])
    purchase=board.list(buyer,'purchase')
    assert purchase['total']==1
    product=purchase['items'][0]
    assert product['shortage']=='2' and product['pending']=='1' and product['customer_order_impact']==2
    detail=board.detail(buyer,product['id'],'purchase')
    assert len(detail['related_groups'])==2
    selected=pick(board,buyer,'purchase')
    body={'selection_id':selected['selection_id'],'idempotency_key':'follow','action':'followup',
        'result':'已安排采购','waiting_for':'到货','promised_date':'2026-10-08','next_follow_up_date':'2026-10-07'}
    result=board.batch(buyer,body)
    assert result['processed']==1
    listing=board.list(buyer,'purchase')
    assert listing['total']==1 and listing['items'][0]['status']=='waiting'
    assert listing['items'][0]['promised_date']=='2026-10-08'
    assert listing['summary']['actions_completed_today']==1
    assert board.batch(buyer,body)['replayed']
    sale=pick(board,seller,'sales')
    board.batch(seller,{'selection_id':sale['selection_id'],'idempotency_key':'sales-follow','action':'followup','result':'等待回复'})
    listing=board.list(seller,'sales')
    assert listing['total']==2 and all(item['status']=='waiting' for item in listing['items'])
    assert listing['summary']['actions_completed_today']==2


def test_procurement_detail_never_infers_customer_from_unverified_buyer(board):
    item = order('PO#1', 'FB1', '需采购', customer_source='',
                 customer_name='', buyer='历史账号不能当客户')
    publish(board, [item])
    actor = user('buyer', ['purchase'])
    product = board.list(actor, 'purchase')['items'][0]
    detail = board.detail(actor, product['id'], 'purchase')
    assert detail['lines'] and all(line['customer'] == '' for line in detail['lines'])


def test_filters_versions_restoration_and_completed_lifecycle(board):
    publish(board,[order('PO#1','LS1','LS库存件'),order('LATER#1','LS2','LS库存件',due='2026-10-09')])
    actor=user('packer',['stock_packing'])
    assert board.list(actor,'stock_packing',due_from='2026-10-05',due_to='2026-10-05')['total']==1
    listing=board.list(actor,'stock_packing')
    with pytest.raises(DailyWorkConflict):board.list(actor,'stock_packing',page=2,task_version=listing['task_version']-1)
    selection=pick(board,actor,'stock_packing')
    body={'selection_id':selection['selection_id'],'idempotency_key':'start','action':'start'}
    board.batch(actor,body)
    with board.store.connect(write=True) as db:db.execute("INSERT INTO settings VALUES ('restoration_hold','true')")
    assert board.batch(actor,body)['replayed']
    with pytest.raises(DailyWorkConflict):board.batch(actor,{**body,'idempotency_key':'next','action':'packed'})
    with board.store.connect(write=True) as db:
        db.execute("DELETE FROM settings WHERE key='restoration_hold'")
        for row in db.execute("SELECT id,payload FROM records WHERE kind='orders'").fetchall():
            payload=json.loads(row['payload']);payload.update(process_state='500',lifecycle_state='candidate',lifecycle_pending_review=True)
            db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id=?",(dumps(payload),row['id']))
    sales=board.list(user('sales',['sales']),'sales',view='role')
    assert sales['items']==[]
    with board.store.connect(write=True) as db:
        for row in db.execute("SELECT id,payload FROM records WHERE kind='orders'").fetchall():
            payload=json.loads(row['payload']);payload.update(lifecycle_state='shipped_unposted',lifecycle_pending_review=False,confirmation_count=2)
            db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id=?",(dumps(payload),row['id']))
    assert board.list(user('sales',['sales']),'sales',view='role')['total']==0


def test_numeric_unit_identity_is_not_displayed_as_a_quantity_suffix(board):
    item=order('PO#1','LS1','LS库存件',base_unit='1',quantity='369')
    publish(board,[item])
    with board.store.connect(write=True) as db:
        inv={key:item[key] for key in ('profile_id','warehouse_id','product_id','sku_id','base_unit')}
        inv.update(id='inv',spec='M8×30',unit_name='')
        db.execute("INSERT INTO records(kind,id,payload,sku) VALUES ('inventory','inv',?,'LS1')",(dumps(inv),))
    actor=user('packer',['stock_packing'])
    card=board.list(actor,'stock_packing')['items'][0]
    assert card['quantity']=='369' and card['unit']=='' and card['unit_note']=='基本单位未命名'
    assert card['specification']=='M8×30'
    line=board.detail(actor,card['id'],'stock_packing')['lines'][0]
    assert line['unit']=='' and line['product_lines'][0]['unit']==''
    with board.store.connect(write=True) as db:
        inv['unit_name']='个';db.execute("UPDATE records SET payload=? WHERE kind='inventory'",(dumps(inv),))
    assert board.list(actor,'stock_packing')['items'][0]['unit']=='个'


def test_legacy_json_inventory_identity_keeps_purchase_task_id_and_events(board):
    publish(board,[order('PO#1','FB1','需采购')])
    with board.store.connect(write=True) as db:
        task=db.execute("SELECT * FROM daily_work_tasks WHERE role='purchase'").fetchone()
        legacy='purchase:procurement_gap:allocation:P|'+json.dumps(['P',WAREHOUSE,'FB1-s','个'])+'|FB1-p|FB1'
        db.execute("UPDATE daily_work_tasks SET business_key=? WHERE id=?",(legacy,task['id']))
    buyer=user('buyer',['purchase'])
    board.daily.action(task['id'],buyer,'wait','old-followup',task['version'],'供应已联系','2026-10-08')
    # A new awaiting-arrival order for the same identity must not create a
    # replacement task or erase the existing owner, waiting date, or event.
    publish(board,[order('PO#1','FB1','需采购'),order('NEW#1','FB1','待到货')])
    with board.store.connect() as db:
        active=db.execute("SELECT * FROM daily_work_tasks WHERE role='purchase' AND condition_state='active'").fetchall()
        assert len(active)==1 and active[0]['id']==task['id']
        assert active[0]['status']=='waiting' and active[0]['next_follow_up_date']=='2026-10-08'
        assert db.execute("SELECT count(*) FROM daily_work_events WHERE task_id=? AND idempotency_key='old-followup'",(task['id'],)).fetchone()[0]==1


def test_customer_quantity_change_promotes_same_followup_group(board):
    publish(board,[order('PO#1','FB1','需采购',due='2026-10-30')])
    actor=user('sales',['sales'])
    selection=pick(board,actor,'sales')
    board.batch(actor,{'selection_id':selection['selection_id'],'idempotency_key':'follow','action':'followup','result':'等待回复','next_follow_up_date':'2026-10-20'})
    before=board.list(actor,'sales')['items'][0]
    publish(board,[order('PO#1','FB1','需采购',due='2026-10-30',quantity='2')])
    after=board.list(actor,'sales')['items'][0]
    assert after['id']==before['id'] and after['lines_changed']
    assert after['next_follow_up_date']=='2026-10-20'


def test_purchase_promises_survive_same_source_and_new_requirement_refresh(board):
    rows=[order('PO#1','FB1','需采购',due='2026-10-30')]
    publish(board,rows)
    actor=user('buyer',['purchase'])
    selection=pick(board,actor,'purchase')
    board.batch(actor,{'selection_id':selection['selection_id'],'idempotency_key':'supply-promise','action':'followup',
        'result':'已安排采购','waiting_for':'供应确认','promised_date':'2026-10-18','next_follow_up_date':'2026-10-15'})
    with board.store.connect() as db:original=db.execute("SELECT id FROM daily_work_tasks WHERE role='purchase'").fetchone()[0]
    board.daily.refresh()
    purchase=board.list(actor,'purchase')['items'][0]
    sale=board.list(user('sales',['sales']),'sales')['items'][0]
    assert purchase['promised_date']==sale['promised_date']=='2026-10-18'
    assert not purchase['lines_changed']
    # Quantity increases and transition from purchase gap to arranged arrival
    # keep the same ongoing product obligation and its human commitments.
    rows=[order('PO#1','FB1','待到货',due='2026-10-30',quantity='3')]
    publish(board,rows)
    with board.store.connect() as db:
        active=db.execute("SELECT * FROM daily_work_tasks WHERE role='purchase' AND condition_state='active'").fetchone()
        assert active['id']==original
        payload=json.loads(active['payload'])
        assert payload['promised_date']=='2026-10-18' and payload['waiting_for']=='供应确认'
        assert payload['followup_result']=='已安排采购'
    purchase=board.list(actor,'purchase')['items'][0]
    sale=board.list(user('sales',['sales']),'sales')['items'][0]
    assert purchase['lines_changed'] and purchase['status']=='waiting'
    assert sale['promised_date']=='2026-10-18' and sale['lines_changed']
    assert len(board.list(actor,'purchase')['today_events'])==1



@pytest.mark.parametrize('clear_mode', ['orders_removed', 'stock_filled'])
def test_purchase_new_demand_cycle_does_not_inherit_old_product_card_followup(board, clear_mode):
    actor = user('buyer', ['purchase'])
    original = order('OLD#1', 'FB1', '需采购', due='2026-10-30')
    publish(board, [original])
    first_card = board.list(actor, 'purchase')['items'][0]
    selection = pick(board, actor, 'purchase')
    board.batch(actor, {
        'selection_id': selection['selection_id'], 'idempotency_key': 'old-demand-followup',
        'action': 'followup', 'result': '已安排采购', 'waiting_for': '旧供应商甲',
        'promised_date': '2026-10-18', 'next_follow_up_date': '2026-10-15',
        'note': '仅适用于 OLD#1 的旧采购承诺',
    })
    old_detail = board.detail(actor, first_card['id'], 'purchase')
    old_task_id = old_detail['lines'][0]['task_id']
    with board.store.connect() as db:
        old_task = dict(db.execute('SELECT * FROM daily_work_tasks WHERE id=?',
                                   (old_task_id,)).fetchone())
        old_payload = json.loads(old_task['payload'])
        old_events = [dict(event) for event in db.execute(
            'SELECT * FROM daily_work_events WHERE task_id=? ORDER BY rowid', (old_task_id,))]
    assert old_payload['promised_date'] == '2026-10-18'
    assert old_payload['waiting_for'] == '旧供应商甲'
    assert old_payload['followup_result'] == '已安排采购'

    # Both disappearance and full stock coverage end the former obligation.
    cleared = [] if clear_mode == 'orders_removed' else [
        order('OLD#1', 'FB1', 'LS库存件', due='2026-10-30')]
    publish(board, cleared, source='2026-10-04T09:00:00+08:00')
    assert board.list(actor, 'purchase')['total'] == 0
    archived = board.history(actor, 'purchase', scope='archived')
    assert archived['total'] == 1 and archived['items'][0]['id'] == old_task_id

    # The product-card key is intentionally stable, but the task cycle is new.
    publish(board, cleared + [order('NEW#1', 'FB1', '需采购', due='2026-11-01')],
            source='2026-10-04T10:00:00+08:00')
    current = board.list(actor, 'purchase')
    assert current['total'] == 1
    card = current['items'][0]
    assert card['id'] == first_card['id']
    assert card['status'] == 'todo' and not card['lines_changed']
    assert all(card[field] == '' for field in
               ('promised_date', 'waiting_for', 'result', 'note', 'next_follow_up_date'))
    detail = board.detail(actor, card['id'], 'purchase')
    assert {line['order_no'] for line in detail['lines']} == {'NEW#1'}
    task_ids = {line['task_id'] for line in detail['lines']}
    assert len(task_ids) == 1 and old_task_id not in task_ids
    with board.store.connect() as db:
        new_task = dict(db.execute('SELECT * FROM daily_work_tasks WHERE id=?',
                                   (next(iter(task_ids)),)).fetchone())
        assert new_task['business_key'] == old_task['business_key']
        assert new_task['cycle'] == old_task['cycle'] + 1
        # The regression must not pass by deleting old card notes or task history.
        saved_card = db.execute('SELECT * FROM task_board_followups WHERE object_id=?',
                                (card['id'],)).fetchone()
        assert saved_card['promised_date'] == '2026-10-18'
        assert saved_card['waiting_for'] == '旧供应商甲'
        saved_task = db.execute('SELECT * FROM daily_work_tasks WHERE id=?',
                                (old_task_id,)).fetchone()
        assert saved_task['condition_state'] == 'resolved'
        assert json.loads(saved_task['payload']) == old_payload
        after_events = {event['id']: dict(event) for event in db.execute(
            'SELECT * FROM daily_work_events WHERE task_id=?', (old_task_id,))}
        assert all(after_events[event['id']] == event for event in old_events)
    retained = board.history_detail(actor, old_task_id, 'purchase')
    assert retained['item']['payload']['promised_date'] == '2026-10-18'
    assert any(event['actor_id'] == actor['id'] and
               event['note'] == '已安排采购：仅适用于 OLD#1 的旧采购承诺'
               for event in retained['events'])


def test_starting_a_mixed_new_and_in_progress_product_batch_is_safe(board):
    publish(board,[order('PO#1','LS1','LS库存件'),order('PO#2','LS1','LS库存件')])
    actor=user('packer',['stock_packing'])
    card=board.list(actor,'stock_packing')['items'][0]
    detail=board.detail(actor,card['id'],'stock_packing')
    first=detail['lines'][0]
    board.daily.action(first['task_id'],actor,'start','first-start',first['version'])
    selection=pick(board,actor,'stock_packing')
    board.batch(actor,{'selection_id':selection['selection_id'],'idempotency_key':'both-start','action':'start'})
    with board.store.connect() as db:
        assert db.execute("SELECT count(*) FROM daily_work_tasks WHERE role='stock_packing' AND status='in_progress'").fetchone()[0]==2
        assert db.execute("SELECT count(*) FROM daily_work_events WHERE event_type='start'").fetchone()[0]==2


def test_same_display_code_different_unit_identity_is_not_merged(board):
    rows=[order('PO#1','LS1','LS库存件',base_unit='个'),
          order('PO#2','LS1','LS库存件',base_unit='盒')]
    publish(board,rows)
    actor=user('packer',['stock_packing'])
    result=board.list(actor,'stock_packing')
    assert result['total']==2
    assert {(item['quantity'],item['unit']) for item in result['items']}=={('1','个'),('1','盒')}
    with board.store.connect() as db:
        original={row['order_no']:row['assignee_id'] for row in db.execute("SELECT order_no,assignee_id FROM daily_work_tasks WHERE role='stock_packing'")}
    publish(board,list(reversed(rows)))
    with board.store.connect() as db:
        after={row['order_no']:row['assignee_id'] for row in db.execute("SELECT order_no,assignee_id FROM daily_work_tasks WHERE role='stock_packing'")}
    assert original==after
