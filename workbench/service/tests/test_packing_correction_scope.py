"""Physical print corrections cannot reopen ERP-completed packaging work."""
import json

import pytest

from core.order_lifecycle import ensure_schema
from core.print_corrections import preview, apply
from core.print_jobs import JobConflict
from core.store import dumps
from test_daily_work import order, user, store
from test_task_board import board, publish
from test_task_board_two_stage import set_print


ADMIN = {'id': 'admin', 'display_name': '管理员', 'is_admin': True}


def lifecycle(board, number, state, error=''):
    ensure_schema(board.store)
    with board.store.connect(write=True) as db:
        db.execute('''INSERT INTO order_lifecycle_state
            (profile_id,order_no,lifecycle_state,process_state,confirmation_count,
             last_confirmed_at,ended_at,last_error,updated_at)
            VALUES ('P',?,?, '600',0,'2026-10-04T09:00:00',?,?, 'today')''',
            (number, state, '2026-10-04T09:00:00' if state == 'archived' else '', error))


def test_direct_600_archive_exits_board_without_500_counter_and_keeps_history(board):
    publish(board, [order('DONE#1', 'LS1000', 'LS库存件')])
    set_print(board, 'DONE#1', 'printed')
    lifecycle(board, 'DONE#1', 'archived')
    publish(board, [])
    actor = user('packer', ['stock_packing'])
    for phase in ('todo', 'in_progress'):
        assert board.list(actor, 'stock_packing', packing_mode='print', work_state=phase)['total'] == 0
    with board.store.connect() as db:
        task = db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing'").fetchone()
        assert task['status'] == 'business_completed' and task['condition_state'] == 'resolved'
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='DONE#1'").fetchone()[0] == 'printed'
        assert db.execute("SELECT COUNT(*) FROM daily_work_events WHERE event_type='erp_business_completed' AND task_id=?", (task['id'],)).fetchone()[0] == 1
    board.daily.refresh()
    assert board.history(actor, 'stock_packing', scope='archived')['total'] == 1


@pytest.mark.parametrize('state,error', [('release_pending', ''), ('archived', '查询异常')])
def test_removed_order_leaves_app_even_if_lifecycle_query_pending(board, state, error):
    publish(board, [order('WAIT#1', 'LS1000', 'LS库存件')])
    set_print(board, 'WAIT#1', 'reserved')
    lifecycle(board, 'WAIT#1', state, error)
    publish(board, [])
    assert board.list(user('packer', ['stock_packing']), 'stock_packing', packing_mode='print', work_state='in_progress')['order_count'] == 0
    with board.store.connect() as db:
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='WAIT#1'").fetchone()[0]=='reserved'


def test_scoped_reset_preserves_completed_facts_and_pending_review(board):
    publish(board, [order(n, 'LS1000', 'LS库存件') for n in ('PENDING#1', 'MISSING#1', 'DONE#1')])
    for n in ('PENDING#1', 'MISSING#1', 'DONE#1'):
        set_print(board, n, 'reserved')
    lifecycle(board, 'PENDING#1', 'release_pending')
    lifecycle(board, 'DONE#1', 'archived')
    publish(board, [order('PENDING#1', 'LS1000', 'LS库存件')])
    assert preview(board.store, ADMIN)['order_count'] == 0
    value = preview(board.store, ADMIN, 'stock_packing_processing')
    assert {r['order_no'] for r in value['orders']} == {'PENDING#1'}
    request = {key: value[key] for key in ('correction_id', 'preview_hash')}
    request.update(idempotency_key='packing-confirmation-scope-test', devices_stopped=True,
                   scope='unfulfilled')  # Apply must use the frozen server scope.
    with board.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('printing_maintenance','true')")
    result = apply(board.store, ADMIN, request)
    assert result['order_count'] == 1
    board.daily.refresh()
    actor = user('packer', ['stock_packing'])
    assert board.list(actor, 'stock_packing', packing_mode='print', work_state='in_progress')['total'] == 0
    todo = board.list(actor, 'stock_packing', packing_mode='print', work_state='todo')
    assert todo['order_count'] == 1
    with board.store.connect() as db:
        states = {r['order_no']: r['state'] for r in db.execute('SELECT * FROM order_print_states')}
        assert states == {'PENDING#1': 'unprinted', 'MISSING#1': 'reserved', 'DONE#1': 'reserved'}
        assert db.execute("SELECT fingerprint FROM order_print_states WHERE order_no='MISSING#1'").fetchone()[0] == 'fingerprint'
    with board.store.connect(write=True) as db:
        db.execute("UPDATE order_print_states SET state='printed' WHERE order_no='MISSING#1'")
    assert apply(board.store, ADMIN, request)['replayed']
    with board.store.connect() as db:
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='MISSING#1'").fetchone()[0] == 'printed'


def test_scoped_reset_rejects_task_version_change(board):
    publish(board, [order('PENDING#1', 'LS1000', 'LS库存件')])
    set_print(board, 'PENDING#1', 'reserved')
    value = preview(board.store, ADMIN, 'stock_packing_processing')
    with board.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('printing_maintenance','true')")
        db.execute("UPDATE daily_work_tasks SET version=version+1 WHERE role='stock_packing'")
    with pytest.raises(JobConflict, match='已变化'):
        apply(board.store, ADMIN, {**value, 'idempotency_key': 'stale-packing-confirmation', 'devices_stopped': True})
