"""Live App views cannot resurrect orders from task or accounting history."""
import json
import pytest
from core.daily_work import DailyWorkConflict
from core.queries import Queries
from core.store import dumps
from core.task_board import _packing_query
from test_daily_work import store, seed, order, user
from test_task_board import board, publish, pick
from test_task_board_two_stage import set_print


def test_same_product_deleted_member_exits_all_ranges_before_projection_refresh(board):
    rows=[order('PO#1','LS1','LS库存件'),order('PO#2','LS1','LS库存件')]
    publish(board,rows)
    actor=user('packer',['stock_packing']);admin=user('admin',[],True)
    before=board.list(actor,'stock_packing',packing_mode='print',work_state='todo')
    identity=before['items'][0]['id'];selection=pick(board,actor,'stock_packing')
    set_print(board,'PO#2','printed')
    # Publish complete replacement, deliberately leave yesterday's active tasks.
    seed(board.store,rows[:1],{'PO#1':'LS库存件'},version='v2')
    q=_packing_query('stock_packing','print')
    with board.store.connect() as db:
        assert db.execute('SELECT dirty FROM packing_index_meta').fetchone()[0]==1
        fallback=board._packing_inputs(db,'stock_packing','packer','mine',q)
        assert set(fallback['source'])=={('P','PO#1')}
        assert {t['order_no'] for t in fallback['tasks']['stock_packing']}=={'PO#1'}
    current=board.list(actor,'stock_packing',packing_mode='print',work_state='todo')
    assert current['order_count']==current['line_count']==current['total']==1
    card=current['items'][0]
    detail=board.detail(actor,card['id'],'stock_packing',packing_mode='print')
    assert [line['order_no'] for line in detail['lines']]==['PO#1']
    assert board.list(actor,'stock_packing',packing_mode='print',work_state='in_progress')['total']==0
    with pytest.raises(KeyError):board.detail(actor,identity,'stock_packing',packing_mode='print')
    assert [r['order_no'] for r in board.daily.list(actor,'stock_packing')['items']]==['PO#1']
    assert board.customers.list(admin,role='sales',view='role')['items'][0]['order_count']==1
    assert Queries(board.store).orders({'mode':'order_no','q':'PO#2'})['total']==0
    with pytest.raises(DailyWorkConflict):
        board.batch(actor,{'selection_id':selection['selection_id'],'action':'start','idempotency_key':'stale-scope'})
    board.daily.refresh();board.customers.reconcile()
    dashboard=board.dashboard(admin)
    for role in dashboard['roles']:
        assert role['summary']['total']==board.list(admin,role['role'],view='role')['total']
    with board.store.connect() as db:
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='PO#2'").fetchone()[0]=='printed'
        assert not db.execute("SELECT 1 FROM daily_work_tasks WHERE order_no='PO#2' AND condition_state='active'").fetchone()
        assert not db.execute("SELECT 1 FROM daily_work_tasks WHERE order_no='PO#2' AND business_completed_at<>''").fetchone()


@pytest.mark.parametrize('flags',[
    {'shadow_demand':True,'lifecycle_pending_review':True},
    {'shadow_demand':'true'},
    {'process_state':'500','lifecycle_state':'candidate'},
    {'process_state':'600','lifecycle_state':'archived'},
])
def test_shadow_rows_reserve_stock_in_storage_but_never_appear_in_app(board,flags):
    rows=[order('CURRENT#1','LS1','LS库存件'),order('OLD#1','LS2','LS库存件')]
    publish(board,rows);set_print(board,'OLD#1','printed')
    historical={**rows[1],**flags}
    seed(board.store,[rows[0],historical],{'CURRENT#1':'LS库存件','OLD#1':'LS库存件'},version='v2')
    actor=user('admin',[],True)
    assert Queries(board.store).orders({'mode':'order_no','q':'OLD#1'})['total']==0
    allorders=Queries(board.store).orders({'print_state':'all'})
    assert [r['order_no'] for r in allorders['items']]==['CURRENT#1']
    for mode in ('','print'):
        cards=board.list(actor,'stock_packing',view='role',packing_mode=mode,work_state='in_progress')
        assert cards['total']==0
    for role in ('sales','stock_packing'):
        for card in board.list(actor,role,view='role')['items']:
            assert all(line['order_no']!='OLD#1' for line in board.detail(actor,card['id'],role,view='role')['lines'])
    with board.store.connect() as db:
        assert db.execute("SELECT count(*) FROM records WHERE kind='orders'").fetchone()[0]==2
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='OLD#1'").fetchone()[0]=='printed'


def test_current_identity_review_still_visible_and_failed_fetch_does_not_hide(board):
    row=order('LIVE#1','LS1','LS库存件',source_review=True,lifecycle_pending_review=True)
    publish(board,[row])
    from core.order_print_state import ensure_schema
    ensure_schema(board.store)
    before=Queries(board.store).orders({'mode':'order_no','q':'LIVE#1'})
    from core.api_source import validate_snapshot, SourceIncomplete
    with pytest.raises(SourceIncomplete):validate_snapshot({'query_complete':False,'records':{'orders':[]}})
    assert Queries(board.store).orders({'mode':'order_no','q':'LIVE#1'})==before
    assert before['total']==1
    assert board.list(user('admin',[],True),'sales',view='role')['total']==1
