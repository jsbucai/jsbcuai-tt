"""Verified non-stock demand must not remain an untouched packing task."""
import json

import pytest

from core.store import dumps
from test_daily_work import order, user, store
from test_task_board import board, publish
from test_task_board_two_stage import set_print
from test_daily_work import seed
from core.order_print_state import order_fingerprint


def stock_task(board):
    with board.store.connect() as db:
        return dict(db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing' ORDER BY cycle DESC LIMIT 1").fetchone())


@pytest.mark.parametrize('category', ['催货', '需采购', '待到货', '来货件'])
def test_unprinted_unstarted_stock_task_retires_and_returns_as_new_cycle(board, category):
    publish(board, [order('LW0918#16', 'LD1830', 'LD库存件')])
    original = stock_task(board)
    # Reproduce the prior retained scope-review payload, including obsolete
    # printed snapshot. Only the independent print fact is authoritative.
    with board.store.connect(write=True) as db:
        payload = json.loads(original['payload'])
        payload.update(source_scope_review=True, read_only=True, print_state='printed',
                       blocked_by='库存包装条件已变化，请核对 ERP 与实际处理')
        db.execute('UPDATE daily_work_tasks SET payload=? WHERE id=?', (dumps(payload), original['id']))
    set_print(board, 'LW0918#16', 'unprinted')
    publish(board, [order('LW0918#16', 'LD1830', category)])
    actor = user('packer', ['stock_packing'])
    for phase in ('todo', 'in_progress'):
        assert board.list(actor, 'stock_packing', packing_mode='print', work_state=phase)['total'] == 0
    closed = stock_task(board)
    assert closed['condition_state'] == 'resolved' and closed['status'] == 'todo'
    assert closed['assignee_id'] == original['assignee_id'] and not closed['business_completed_at']
    assert board.history(actor, 'stock_packing', scope='archived')['total'] == 1
    board.daily.refresh()
    with board.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM daily_work_events WHERE task_id=? AND event_type='condition_resolved'", (original['id'],)).fetchone()[0] == 1
    publish(board, [order('LW0918#16', 'LD1830', 'LD库存件')])
    current = stock_task(board)
    assert current['id'] != original['id'] and current['cycle'] == 2 and current['condition_state'] == 'active'


@pytest.mark.parametrize('status,print_state', [('in_progress','unprinted'), ('waiting','unprinted'),
    ('packed','unprinted'), ('todo','printed'), ('todo','reserved'), ('todo','uncertain')])
def test_actual_work_is_retained_for_review(board, status, print_state):
    publish(board, [order('LW0918#16', 'LD1830', 'LD库存件')])
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET status=? WHERE role='stock_packing'", (status,))
    set_print(board, 'LW0918#16', print_state)
    publish(board, [order('LW0918#16', 'LD1830', '催货')])
    task = stock_task(board)
    assert task['condition_state'] == 'active'
    assert json.loads(task['payload'])['read_only']


@pytest.mark.parametrize('case', ['missing', 'blocked', 'lifecycle_error', 'source_review'])
def test_uncertain_source_is_never_treated_as_verified_category_change(board, case):
    publish(board, [order('LW0918#16', 'LD1830', 'LD库存件')])
    changed = order('LW0918#16', 'LD1830', '催货')
    if case == 'blocked': changed['calculation_status'] = 'blocked'
    if case == 'lifecycle_error': changed['lifecycle_error'] = '读取失败'
    if case == 'source_review': changed['source_review'] = True
    publish(board, [] if case == 'missing' else [changed])
    assert stock_task(board)['condition_state'] == 'active'


@pytest.mark.parametrize('category',['来货件','异常'])
@pytest.mark.parametrize('state',['printed','uncertain','reserved'])
def test_receipt_role_correction_keeps_print_facts_without_duplicate_ready_work(board,category,state):
    item=order('26976236#44','LM2504','LM库存件')
    publish(board,[item])
    old=stock_task(board)
    set_print(board,item['order_no'],state)
    with board.store.connect(write=True) as db:
        db.execute('UPDATE order_print_states SET fingerprint=?',(order_fingerprint([item]),))
        before=[tuple(r) for r in db.execute('SELECT * FROM order_print_states')]
    changed={**item,'logistics_company':category}
    allocation={'demand':{'order':item['order_no'],'sku':item['product_id'],
        'product_code':item['sku'],'warehouse':json.dumps(['P',item['warehouse_id'],item['sku_id'],item['base_unit']]),'quantity':1},
        'actual':1,'shortage':0,'pending':0,'dispatch_route':'arrival',
        'receipt_allocations':[{'event_id':'verified-receipt','document':'ZZ-1','date':'2026-09-20',
            'quantity':1,'type':'内部组装／拆卸'}]}
    seed(board.store,[changed],{item['order_no']:category},[allocation],version='new-arrival')
    board.daily.refresh();board.customers.reconcile()
    assert stock_task(board)['condition_state']==('resolved' if state=='printed' else 'active')
    with board.store.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM order_print_states')]==before
        arrival=db.execute("SELECT * FROM daily_work_tasks WHERE role='arrival_packing' AND condition_state='active'").fetchone()
        assert arrival
        events=[dict(r) for r in db.execute("SELECT * FROM daily_work_events WHERE task_id=? AND event_type='packing_role_corrected'",(old['id'],))]
        assert len(events)==(1 if state=='printed' else 0)
        if state=='printed':assert json.loads(events[0]['payload'])['replacement_task_id']==arrival['id']
    board.daily.refresh()
    assert stock_task(board)['condition_state']==('resolved' if state=='printed' else 'active')
