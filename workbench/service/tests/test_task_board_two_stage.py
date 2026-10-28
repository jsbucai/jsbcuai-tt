"""Two-stage work is independent of notes, legacy status and print outcomes."""
import json

import pytest

from core.daily_work import DailyWorkConflict
from core.order_print_state import ensure_schema as ensure_print_schema
from core.store import dumps
from test_daily_work import order, user, seed, store
from test_task_board import board, publish


def sales_request(board, actor, identity, action, key, note=''):
    current=board.detail(actor,identity,'sales',view='role' if actor['is_admin'] else 'mine',work_state='todo')
    return {'action':action,'idempotency_key':key,'version':current['item']['version'],
        'expected_data_version':current['data_version'],'expected_task_version':current['task_version'],
        **({'note':note} if note else {})}


def test_sales_note_does_not_start_and_start_reset_keep_notes(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('sales',['sales'])
    card=board.list(actor,'sales',work_state='todo')['items'][0]
    assert card['work_state']=='todo' and not card['has_note']
    body=sales_request(board,actor,card['id'],'note','note-1','已询问客户')
    result=board.sales_action(actor,card['id'],body)
    assert result['work_state']=='todo' and result['latest_note']=='已询问客户'
    body=sales_request(board,actor,card['id'],'start','start-1')
    result=board.sales_action(actor,card['id'],body)
    assert result['work_state']=='in_progress'
    assert board.list(actor,'sales',work_state='todo')['total']==0
    active=board.list(actor,'sales',work_state='in_progress')
    assert active['summary']['work_state_counts']=={'todo':0,'in_progress':1}
    assert active['items'][0]['has_note']
    assert board.sales_action(actor,card['id'],body)['replayed']
    with board.store.connect() as db:
        follow=db.execute('SELECT * FROM customer_order_followups WHERE group_id=?',(card['id'],)).fetchone()
        assert follow['next_follow_up_date']==''
        assert db.execute('SELECT count(*) FROM customer_order_work_actions').fetchone()[0]==2
    board.sales_action(actor,card['id'],sales_request(board,actor,card['id'],'reset_to_todo','reset'))
    assert board.list(actor,'sales',work_state='todo')['items'][0]['latest_note']=='已询问客户'
    assert len(board.detail(actor,card['id'],'sales')['notes'])==1
    history=board.customers.history(actor)
    assert history['total']==1 and history['items'][0]['latest_note']=='已询问客户'
    assert [event['event_type'] for event in board.customers.history_detail(actor,card['id'])['events']]==['reset_to_todo','start','note']
    publish(board,[order('PO#1','LS1000','LS库存件')],source='2026-10-05T08:00:00+08:00')
    assert board.list(actor,'sales',work_state='todo')['total']==1


def test_sales_conflicts_rollback_and_replay_survives_hold(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('sales',['sales'])
    identity=board.list(actor,'sales',work_state='todo')['items'][0]['id']
    stale=sales_request(board,actor,identity,'start','stale')
    good=sales_request(board,actor,identity,'start','good')
    original=board.sales_action(actor,identity,good)
    with pytest.raises(DailyWorkConflict):board.sales_action(actor,identity,stale)
    with pytest.raises(DailyWorkConflict) as conflict:
        board.sales_action(actor,identity,{**good,'action':'reset_to_todo'})
    assert conflict.value.code=='idempotency_conflict'
    admin=user('admin',['sales'],admin=True)
    with board.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('restoration_hold','true')")
    assert board.sales_action(actor,identity,good)=={**original,'replayed':True}
    with pytest.raises(DailyWorkConflict) as conflict:
        board.sales_action(admin,identity,sales_request(board,admin,identity,'note','hold','备注'))
    assert conflict.value.code=='restoration_hold'
    with board.store.connect() as db:
        assert db.execute('SELECT count(*) FROM customer_order_work_actions').fetchone()[0]==1


def test_sales_fixed_owner_required_and_identity_conflict_stays_visible(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('sales',['sales']);admin=user('admin',['sales'],admin=True)
    identity=board.list(actor,'sales',work_state='todo')['items'][0]['id']
    with board.store.connect(write=True) as db:
        db.execute("UPDATE sales_customer_owners SET owner_id='' ")
    assert board.list(actor,'sales',work_state='todo')['total']==0
    card=board.list(admin,'sales',view='role',work_state='todo')['items'][0]
    assert not card['can_act']
    with pytest.raises(DailyWorkConflict) as conflict:
        board.sales_action(admin,identity,sales_request(board,admin,identity,'start','no-owner'))
    assert conflict.value.code=='customer_identity_review'


def set_print(board, number, state):
    ensure_print_schema(board.store)
    with board.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO order_print_states VALUES (?,?,?,?,?,?,?,?)",
            ('P',number,state,'device','job','fingerprint','','2026-10-04T09:00:00+08:00'))


def test_stock_phase_cards_freeze_exact_order_ranges_and_counts(board):
    publish(board,[order('PO#1','LS1000','LS库存件'),order('PO#2','LS1000','LS库存件')])
    actor=user('packer',['stock_packing'])
    old=board.list(actor,'stock_packing')['items'][0]
    assert old['order_count']==2
    set_print(board,'PO#1','printed')
    todo=board.list(actor,'stock_packing',work_state='todo')
    progress=board.list(actor,'stock_packing',work_state='in_progress')
    assert todo['summary']['work_state_counts']==progress['summary']['work_state_counts']=={'todo':1,'in_progress':1}
    pending,printed=todo['items'][0],progress['items'][0]
    assert pending['id'].startswith('phase:todo:') and printed['id'].startswith('phase:in_progress:')
    assert pending['id']!=printed['id'] and pending['order_count']==printed['order_count']==1
    assert printed['print_state']=='printed' and printed['work_label']=='待出库'
    assert [line['order_no'] for line in board.detail(actor,pending['id'],'stock_packing')['lines']]==['PO#2']
    assert board.detail(actor,old['id'],'stock_packing')['item']['order_count']==2
    selected=board.select(actor,{'role':'stock_packing','view':'mine','all':True,'work_state':'todo',
        'data_version':todo['data_version'],'task_version':todo['task_version']})
    assert selected['order_nos']==['PO#2']
    snapshot=board.export_snapshot(actor,{'role':'stock_packing','ids':[printed['id']],
        'expected_data_version':todo['data_version'],'expected_task_version':todo['task_version']})
    assert [order['order_no'] for order in snapshot['items'][0]['orders']]==['PO#1']
    set_print(board,'PO#2','reserved')
    with pytest.raises(DailyWorkConflict):
        board.select(actor,{'role':'stock_packing','ids':[pending['id']],
            'data_version':todo['data_version'],'task_version':todo['task_version']})


@pytest.mark.parametrize('state,label',[('reserved','打印中'),('uncertain','打印待核对'),('printed','待出库')])
def test_printing_outcomes_enter_progress_without_packing_fact(board,state,label):
    publish(board,[order('PO#1','LS1000','LS库存件')]);actor=user('packer',['stock_packing'])
    set_print(board,'PO#1',state)
    result=board.list(actor,'stock_packing',work_state='in_progress')
    assert result['items'][0]['work_label']==label
    with board.store.connect() as db:
        assert db.execute("SELECT status FROM daily_work_tasks WHERE role='stock_packing'").fetchone()[0]=='todo'
    set_print(board,'PO#1','unprinted')
    assert board.list(actor,'stock_packing',work_state='todo')['total']==1


def test_source_missing_exits_current_views_but_keeps_notes_on_return(board):
    rows=[order('PO#1','LS1000','LS库存件')]
    publish(board,rows)
    actor=user('sales',['sales']);packer=user('packer',['stock_packing'])
    identity=board.list(actor,'sales',work_state='todo')['items'][0]['id']
    board.sales_action(actor,identity,sales_request(board,actor,identity,'start','start'))
    board.sales_action(actor,identity,sales_request(board,actor,identity,'note','note','等待客户'))
    seed(board.store,[],{},version='v2');board.daily.refresh();board.customers.reconcile()
    assert board.list(actor,'sales',work_state='in_progress')['total']==0
    assert board.list(packer,'stock_packing',work_state='todo')['total']==0
    with pytest.raises(KeyError):board.detail(actor,identity,'sales')
    with board.store.connect() as db:
        assert db.execute("SELECT note FROM customer_order_work_actions WHERE action='note'").fetchone()[0]=='等待客户'
        assert not db.execute("SELECT 1 FROM daily_work_tasks WHERE business_completed_at<>''").fetchone()
    from core.daily_work import DailyWork
    from core.customer_orders import CustomerOrders
    from core.task_board import TaskBoard
    daily=DailyWork(board.store,auto_project=False)
    restarted=TaskBoard(board.store,daily,CustomerOrders(board.store,daily))
    assert restarted.list(actor,'sales',work_state='in_progress')['total']==0
    publish(board,rows)
    returned=board.list(actor,'sales',work_state='in_progress')['items'][0]
    assert returned['id']==identity and returned['latest_note']=='等待客户'


def test_purchase_and_arrival_keep_old_behavior_and_reject_phase_filter(board):
    publish(board,[order('PO#1','LS1000','需采购'),order('ARR#1','FB1000','异常')])
    admin=user('admin',[],admin=True)
    for role in ('purchase','arrival_packing'):
        assert board.list(admin,role,view='role')['total']>0
    with pytest.raises(ValueError):board.list(admin,'purchase',view='role',work_state='todo')
    assert 'exception' in board.list(admin,'arrival_packing',view='role',work_state='todo')['summary']['work_state_counts']


def test_packer_start_does_not_start_sales_and_legacy_sales_start_has_no_note(board):
    publish(board,[order('PO#1','LS1000','LS库存件',due='2026-10-03')])
    packer=user('packer',['stock_packing']);sales=user('sales',['sales'])
    stock=board.list(packer,'stock_packing')['items'][0]
    detail=board.detail(packer,stock['id'],'stock_packing')['lines'][0]
    board.daily.action(detail['task_id'],packer,'start','packer-start',detail['version'])
    assert board.list(sales,'sales',work_state='todo')['total']==1
    with board.store.connect() as db:
        task=db.execute("SELECT id,version FROM daily_work_tasks WHERE role='sales'").fetchone()
    board.daily.action(task['id'],sales,'start','sales-start',task['version'])
    card=board.list(sales,'sales',work_state='in_progress')['items'][0]
    assert not card['has_note'] and card['latest_note']==''


def test_source_category_change_retains_old_stock_readonly_and_one_executable_role(board):
    item=order('PO#1','LS1000','LS库存件')
    publish(board,[item]);actor=user('packer',['stock_packing','arrival_packing'])
    set_print(board,'PO#1','printed')
    # The same order can move to arrival after ERP corrections. Preserve its
    # old progress for review, never permit both roles to act on the scope.
    publish(board,[{**item,'logistics_company':'异常'}])
    stock=board.list(actor,'stock_packing',work_state='in_progress')['items'][0]
    arrival=board.list(actor,'arrival_packing')['items'][0]
    assert not stock['can_act'] and '条件已变化' in stock['blocked_by']
    assert arrival['order_count']==1
    assert sum(card['can_act'] for card in (stock,arrival))<=1


def test_partial_removed_scope_allows_work_on_remaining_order(board):
    rows=[order('PO#1','LS1000','LS库存件'),order('PO#2','LS2000','LS库存件')]
    publish(board,rows);actor=user('sales',['sales'])
    identity=board.list(actor,'sales',work_state='todo')['items'][0]['id']
    publish(board,[rows[0]])
    card=board.list(actor,'sales',work_state='todo')['items'][0]
    assert card['order_count']==1 and card['can_act']
    assert [r['order_no'] for r in board.detail(actor,identity,'sales')['lines']]==['PO#1']
    board.sales_action(actor,identity,sales_request(board,actor,identity,'note','remaining','继续跟进'))
    publish(board,rows)
    assert board.list(actor,'sales',work_state='todo')['items'][0]['can_act']


def test_same_second_notes_return_newest_and_erp_needs_two_confirmations(board):
    publish(board,[order('PO#1','LS1000','LS库存件')]);actor=user('sales',['sales'])
    identity=board.list(actor,'sales',work_state='todo')['items'][0]['id']
    for index,note in enumerate(('第一条','第二条')):
        board.sales_action(actor,identity,sales_request(board,actor,identity,'note',str(index),note))
    assert board.list(actor,'sales',work_state='todo')['items'][0]['latest_note']=='第二条'
    item=order('PO#1','LS1000','已完成订单需结存',process_state='500',lifecycle_state='shipped_unposted',confirmation_count=1)
    publish(board,[item])
    assert board.list(actor,'sales',work_state='todo')['total']==0
    publish(board,[{**item,'confirmation_count':2}])
    assert board.list(actor,'sales',work_state='todo')['total']==0


def test_other_phone_print_and_cancel_invalidates_phase_ids_not_frozen_selection(board):
    publish(board,[order('PO#1','LS1000','LS库存件'),order('PO#2','LS1000','LS库存件')])
    actor=user('packer',['stock_packing'])
    original=board.list(actor,'stock_packing',work_state='todo')
    old_id=original['items'][0]['id']
    selected=board.select(actor,{'role':'stock_packing','ids':[old_id],
        'data_version':original['data_version'],'task_version':original['task_version']})
    set_print(board,'PO#1','printed')
    after_print=board.list(actor,'stock_packing',work_state='todo')
    assert after_print['task_version']==original['task_version']
    assert after_print['items'][0]['id']!=old_id
    with pytest.raises(KeyError):board.detail(actor,old_id,'stock_packing')
    with pytest.raises(DailyWorkConflict):
        board.select(actor,{'role':'stock_packing','ids':[old_id],
            'data_version':original['data_version'],'task_version':original['task_version']})
    with pytest.raises(DailyWorkConflict):
        board.export_snapshot(actor,{'role':'stock_packing','ids':[old_id],
            'expected_data_version':original['data_version'],'expected_task_version':original['task_version']})
    one_id=after_print['items'][0]['id']
    set_print(board,'PO#1','unprinted')
    assert board.list(actor,'stock_packing',work_state='todo')['items'][0]['id']==old_id
    with pytest.raises(KeyError):board.detail(actor,one_id,'stock_packing')
    with pytest.raises(DailyWorkConflict):
        board.export_snapshot(actor,{'role':'stock_packing','ids':[one_id],
            'expected_data_version':original['data_version'],'expected_task_version':original['task_version']})
    with board.store.connect() as db:
        scope=json.loads(db.execute('SELECT payload FROM daily_work_selections WHERE id=?',(selected['selection_id'],)).fetchone()[0])
    assert scope['order_nos']==['PO#1','PO#2'] and len(scope['objects'])==2


def test_source_missing_stock_scope_disappears(board):
    rows=[order('PO#1','LS1000','LS库存件'),order('PO#2','LS1000','LS库存件')]
    publish(board,rows);actor=user('packer',['stock_packing'])
    before=board.list(actor,'stock_packing',work_state='todo')['items'][0]
    seed(board.store,[],{},version='v2');board.daily.refresh();board.customers.reconcile()
    assert board.list(actor,'stock_packing',work_state='todo')['total']==0
    with pytest.raises(KeyError):board.detail(actor,before['id'],'stock_packing')


def test_print_version_guards_pagination_restore_and_snapshot_without_bumping_tasks(board,monkeypatch):
    rows=[order(f'PO{i:03d}#1',f'LS{i:04d}','LS库存件') for i in range(60)]
    publish(board,rows);actor=user('packer',['stock_packing'])
    original_build=board._build
    snapshots=[]
    def checked_build(db,*args,**kwargs):
        snapshots.append(db.in_transaction)
        return original_build(db,*args,**kwargs)
    monkeypatch.setattr(board,'_build',checked_build)
    first=board.list(actor,'stock_packing',work_state='todo')
    token=first['print_state_version']
    assert len(token)==64 and first['has_more'] and snapshots==[True]
    assert board.daily.version()['print_state_version']==token
    assert board.daily.list(actor,'stock_packing')['print_state_version']==token
    printed_number=board.detail(actor,first['items'][0]['id'],'stock_packing')['lines'][0]['order_no']
    unchanged_id=first['items'][1]['id']
    set_print(board,printed_number,'printed')
    assert board.daily.version()['task_version']==first['task_version']
    with pytest.raises(DailyWorkConflict) as conflict:
        board.list(actor,'stock_packing',work_state='todo',page=2,
            data_version=first['data_version'],task_version=first['task_version'],print_state_version=token)
    assert conflict.value.code=='print_state_version_changed'
    with pytest.raises(DailyWorkConflict) as conflict:
        board.detail(actor,unchanged_id,'stock_packing',print_state_version=token)
    assert conflict.value.code=='print_state_version_changed'
    with pytest.raises(DailyWorkConflict) as conflict:
        board.select(actor,{'role':'stock_packing','ids':[unchanged_id],
            'data_version':first['data_version'],'task_version':first['task_version'],
            'expected_print_state_version':token})
    assert conflict.value.code=='print_state_version_changed'
    with pytest.raises(DailyWorkConflict) as conflict:
        board.export_snapshot(actor,{'role':'stock_packing','ids':[unchanged_id],
            'expected_data_version':first['data_version'],'expected_task_version':first['task_version'],
            'expected_print_state_version':token})
    assert conflict.value.code=='print_state_version_changed'
    fresh=board.list(actor,'stock_packing',work_state='todo')
    second=board.list(actor,'stock_packing',work_state='todo',page=2,print_state_version=fresh['print_state_version'])
    assert fresh['total']==59 and len(second['items'])==9
    # Omitting the additive guard retains the old client's readable contract.
    assert board.list(actor,'stock_packing',page=2)['page']==2
    selected=board.select(actor,{'role':'stock_packing','ids':[unchanged_id],
        'data_version':fresh['data_version'],'task_version':fresh['task_version'],
        'expected_print_state_version':fresh['print_state_version']})
    assert selected['print_state_version']==fresh['print_state_version']


def test_print_version_hash_is_complete_stable_and_empty_table_compatible(board):
    from core.daily_work import _print_state_version
    initial=board.daily.version()['print_state_version']
    ensure_print_schema(board.store)
    assert board.daily.version()['print_state_version']==initial
    set_print(board,'PO#1','printed')
    token=board.daily.version()['print_state_version']
    for field,value in [('fingerprint','different-label'),('job_id','different-job'),
                        ('updated_at','different-time'),('reason','人工核对'),('device_id','other-phone')]:
        with board.store.connect(write=True) as db:
            db.execute('UPDATE order_print_states SET '+field+'=? WHERE order_no=?',(value,'PO#1'))
        changed=board.daily.version()['print_state_version']
        assert changed!=token
        token=changed
    with board.store.connect() as db:
        assert token==_print_state_version(db)


def test_board_single_response_reads_print_metadata_and_cards_from_same_snapshot(board,monkeypatch):
    publish(board,[order('PO#1','LS1000','LS库存件'),order('PO#2','LS2000','LS库存件')])
    actor=user('packer',['stock_packing'])
    ensure_print_schema(board.store)
    before=board.daily.version()['print_state_version']
    original=board._meta
    changed=False
    def concurrent_print_after_metadata(db):
        nonlocal changed
        result=original(db)
        if not changed:
            changed=True
            # A separate WAL writer commits after the token is read but before
            # the card builder reads print states in this same response.
            set_print(board,'PO#1','printed')
        return result
    monkeypatch.setattr(board,'_meta',concurrent_print_after_metadata)
    frozen=board.list(actor,'stock_packing',work_state='todo')
    assert frozen['print_state_version']==before and frozen['total']==2
    current=board.list(actor,'stock_packing',work_state='todo')
    assert current['print_state_version']!=before and current['total']==1
