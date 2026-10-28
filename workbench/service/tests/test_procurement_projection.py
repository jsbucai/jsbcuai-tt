import json
import sqlite3

import pytest

from core.procurement_projection import read_cards, needs_procurement, ProjectionUnavailable
from core.dot_readonly import PROCUREMENT_COLUMNS, ALLOWED_TABLES
from core.store import dumps
from test_task_board import board, publish, order, user, WAREHOUSE
from test_daily_work import store


def projected(board):
    with board.store.read_snapshot(), board.store.connect() as db:
        return len([c for c in read_cards(db, 'P', WAREHOUSE, 'v1') if needs_procurement(c)])


@pytest.mark.parametrize('case', ['normal','same_sku','same_order','names','blocked','partial','legacy','completed','zero'])
def test_matches_formal_board(board,case):
    rows=[order('PO#1','S1','需采购'),order('PO#2','S1','需采购'),
          order('OTHER#1','S2','待到货'),order('STOCK#1','S3','LS库存件')]
    if case=='same_sku':rows.append(order('PO#3','S1','需采购',product_id='different-product'))
    if case=='same_order':rows.append(order('PO#1','S4','需采购',id='another-line'))
    if case=='names':rows[1]['customer_name']='不同客户'
    if case=='blocked':rows[0]['lifecycle_pending_review']=True
    if case=='completed':rows[0].update(lifecycle_state='shipped_unposted',process_state='500',lifecycle_confirmations=2)
    if case=='zero':rows=[]
    publish(board,rows)
    if case=='partial':
        with board.store.connect(write=True) as db:
            c=json.loads(db.execute('SELECT payload FROM logistics_calculations').fetchone()[0])
            c['allocations'][0].update(actual='0.25',pending='0.25',shortage='0.5')
            db.execute('UPDATE logistics_calculations SET payload=?',(dumps(c),))
    if case=='legacy':
        with board.store.connect(write=True) as db:
            db.execute("UPDATE daily_work_tasks SET payload=json_remove(payload,'$.product_identity') WHERE role='purchase'")
    actor=user('buyer',['purchase'])
    original=board.list(actor,'purchase',view='role',status='procurement')['total']
    assert projected(board)==original
    if case=='same_sku':assert original==2 # Stable identity, never distinct(sku).
    if case=='normal':assert original==1 # Two lines for one product, transit/stock excluded.


def test_read_projection_never_reads_staff_notes_or_writes(board):
    publish(board,[order('PO#1','S1','需采购')])
    reads=set()
    with board.store.read_snapshot(), board.store.connect() as db:
        db.execute("SELECT value FROM json_each('[]')").fetchall()
        before=db.total_changes
        def authorizer(action,table,column,*_):
            if action==sqlite3.SQLITE_READ:
                reads.add((table,column))
                return sqlite3.SQLITE_OK if table in ALLOWED_TABLES or column in PROCUREMENT_COLUMNS.get(table,set()) else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_TRANSACTION) else sqlite3.SQLITE_DENY
        db.set_authorizer(authorizer)
        try:
            assert len([c for c in read_cards(db,'P',WAREHOUSE,'v1') if needs_procurement(c)])==1
            for sql in ('SELECT password_hash FROM staff_users','SELECT * FROM customer_order_followup_events','DELETE FROM records'):
                with pytest.raises(sqlite3.DatabaseError):db.execute(sql)
            assert db.total_changes==before
        finally:db.set_authorizer(None)


def test_missing_calculation_is_not_zero_and_foreign_tasks_rejected(board):
    publish(board,[])
    with board.store.connect() as db:
        with pytest.raises(ProjectionUnavailable,match='calculation_unavailable'):read_cards(db,'P',WAREHOUSE,'absent')
    publish(board,[order('PO#1','S1','需采购')])
    with board.store.connect(write=True) as db:db.execute("UPDATE daily_work_tasks SET profile_id='foreign' WHERE role='purchase'")
    with board.store.connect() as db:
        with pytest.raises(ProjectionUnavailable,match='projection_scope_mismatch'):read_cards(db,'P',WAREHOUSE,'v1')
