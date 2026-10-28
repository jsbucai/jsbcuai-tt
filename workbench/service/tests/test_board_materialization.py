"""A fast page remains a view of the reader's exact committed facts."""
import pytest

from core.daily_work import DailyWorkConflict
from core.order_print_state import ensure_schema as ensure_print_schema
from test_daily_work import order, user, store
from test_task_board import board, publish
from test_task_board_two_stage import set_print


def test_pages_and_detail_share_one_complete_build_and_do_not_leak_mutable_results(board, monkeypatch):
    publish(board,[order(f'PO{i:03d}#1',f'LS{i:04d}','LS库存件') for i in range(60)])
    actor=user('packer',['stock_packing'])
    calls=[]
    original=board._build_uncached
    def counted(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(board,'_build_uncached',counted)
    first=board.list(actor,'stock_packing',work_state='todo')
    identity=first['items'][0]['id']
    first['items'][0]['owner_names'].append('不得污染缓存')
    first['assignees'].clear()
    second=board.list(actor,'stock_packing',work_state='todo',page=2,
                      print_state_version=first['print_state_version'])
    detail=board.detail(actor,identity,'stock_packing',work_state='todo')
    detail['lines'][0]['order_no']='不得污染缓存'
    fresh=board.detail(actor,identity,'stock_packing',work_state='todo')
    assert len(calls)==1 and len(second['items'])==10
    assert fresh['lines'][0]['order_no']!='不得污染缓存'
    assert '不得污染缓存' not in fresh['item']['owner_names'] and fresh['assignees']


def test_roles_and_actors_share_inputs_but_keep_permission_views_separate(board,monkeypatch):
    publish(board,[order('PO#1','LS1000','LS库存件'),order('PO#2','LS2000','需采购')])
    calls=[];original=board.customers._build
    def counted(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(board.customers,'_build',counted)
    admin=user('admin',['sales','stock_packing','purchase'],True)
    stock=board.list(admin,'stock_packing','role',work_state='todo')
    board.list(admin,'purchase','role')
    board.list(user('sales',['sales']),'sales',work_state='todo')
    assert board.list(user('buyer',['stock_packing']),'stock_packing',work_state='todo')['total']==0
    assert board.list(user('packer',['stock_packing']),'stock_packing',work_state='todo')['total']==stock['total']
    assert len(calls)==1
    with pytest.raises(DailyWorkConflict):
        board.list(user('admin',['sales']),'stock_packing','role',work_state='todo')


def test_assignment_and_fixed_owner_changes_invalidate_without_public_version_bump(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('packer',['stock_packing']);sales=user('sales',['sales'])
    first=board.list(actor,'stock_packing',work_state='todo')
    assert board.list(sales,'sales',work_state='todo')['total']==1
    with board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET assignee_id='buyer' WHERE role='stock_packing'")
        db.execute("UPDATE sales_customer_owners SET owner_id='' WHERE customer_name='客户甲'")
    assert board.list(sales,'sales',work_state='todo')['total']==0
    assert board.list(actor,'stock_packing',work_state='todo')['total']==0
    # Mutations reconstruct facts inside their own write transaction. Even if
    # a legacy writer omitted task_version, a warmed permission card is useless.
    with pytest.raises(DailyWorkConflict) as conflict:
        board.select(actor,{'role':'stock_packing','ids':[first['items'][0]['id']],
            'data_version':first['data_version'],'task_version':first['task_version']})
    assert conflict.value.code in {'task_version_changed','task_owner_conflict'}


def test_rollback_and_concurrent_wal_commit_do_not_mix_cache_generations(board,monkeypatch):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('packer',['stock_packing'])
    calls=[];original=board._build_uncached
    def counted(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(board,'_build_uncached',counted)
    first=board.list(actor,'stock_packing',work_state='todo')
    with pytest.raises(RuntimeError),board.store.connect(write=True) as db:
        db.execute("UPDATE daily_work_tasks SET assignee_id='buyer' WHERE role='stock_packing'")
        raise RuntimeError('rollback')
    assert board.list(actor,'stock_packing',work_state='todo')['total']==1 and len(calls)==1
    with board.store.read_snapshot(),board.store.connect() as db:
        db.execute('SELECT generation FROM task_board_materialization_generation').fetchone()
        set_print(board,'PO#1','printed')
        frozen=board.list(actor,'stock_packing',work_state='todo')
        assert frozen['print_state_version']==first['print_state_version'] and frozen['total']==1
    current=board.list(actor,'stock_packing',work_state='todo')
    assert current['print_state_version']!=first['print_state_version'] and current['total']==0
    assert len(calls)==2


def test_new_optional_table_is_monitored_after_warm_generation(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('packer',['stock_packing'])
    with board.store.connect(write=True) as db:
        db.execute('DROP TABLE IF EXISTS order_print_states')
    first=board.list(actor,'stock_packing',work_state='todo')
    ensure_print_schema(board.store)
    set_print(board,'PO#1','printed')
    current=board.list(actor,'stock_packing',work_state='todo')
    assert current['total']==0 and current['print_state_version']!=first['print_state_version']
    set_print(board,'PO#1','unprinted')
    assert board.list(actor,'stock_packing',work_state='todo')['total']==1


def test_cached_note_index_matches_direct_queries_and_source_changes(board):
    publish(board,[order('PO#1','LS1000','LS库存件')])
    actor=user('sales',['sales'])
    first=board.list(actor,'sales',work_state='todo');identity=first['items'][0]['id']
    with board.store.connect(write=True) as db:
        db.execute('''INSERT INTO customer_order_followup_events VALUES (?,?,?,?,?,?,?,?)''',
            ('old-note',identity,'old-note','sales','已与客户沟通','','','2026-10-04T09:00:00'))
        db.execute('''INSERT INTO customer_order_followup_events VALUES (?,?,?,?,?,?,?,?)''',
            ('system-note',identity,'system-note','system:assignment','自动派工','','','2026-10-04T09:01:00'))
    current=board.detail(actor,identity,'sales',work_state='todo')
    with board.store.connect() as db:
        assert current['notes']==board._sales_notes(db,identity)
    assert current['item']['has_note'] and current['item']['latest_note']=='已与客户沟通'
    publish(board,[],source='2026-10-04T10:00:00+08:00')
    assert board.list(actor,'sales',work_state='todo')['items']==[]
    with board.store.connect() as db:
        assert board._sales_notes(db,identity)[0]['note']=='已与客户沟通'


def test_materialization_is_bounded_and_never_publishes_failed_partial_view(board):
    cache=board._materialization
    for generation in range(5):
        for page in range(20):
            cache.get(('test',generation),('view',page),lambda: {'complete':True})
    assert len(cache._entries)<=cache.limit
    assert len({identity[0] for identity in cache._entries})<=2
    saved=list(cache._entries)
    def broken():
        raise RuntimeError('incomplete')
    with pytest.raises(RuntimeError):cache.get(('test',6),('broken',),broken)
    assert list(cache._entries)==saved
