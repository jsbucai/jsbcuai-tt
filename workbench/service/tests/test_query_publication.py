"""Business regressions for partial classification and durable manual sync."""
import copy
import json
import sqlite3
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from test_api_migration import store, snapshot, order, inventory
from test_business_rules import facts, event, KEY, TODAY
from core.api_source import publish
from core.classification_pipeline import calculate_query_snapshot
from core.update_jobs import UpdateJobs
from core.store import Store
from core.print_jobs import JobConflict
from core.queries import Queries
from core.order_print_state import ensure_schema as ensure_print_schema


def query_snapshot(orders=None):
    data=snapshot(orders=orders)
    first = data['inventory'][0]
    total = sum(float(line['open_qty']) for order_row in data['orders'] for line in order_row['lines']
                if all(str(line.get(field, '')).strip() == str(first.get(field, '')).strip()
                       for field in ('profile_id','warehouse_id','product_id','sku_id','base_unit')))
    first['saleable_qty'] = str(float(first['physical_qty']) - total)
    data['query_complete']=True
    return data


def run(jobs,mode='sync'):
    identity=str(uuid.uuid4())
    jobs.enqueue(identity,mode)
    assert jobs.process_one()
    return jobs.get(identity)


def test_query_publication_survives_global_classification_gap(store):
    data=query_snapshot()
    data['calculation_issues']=[{'scope':'global','code':'pending_batches','reason':'采购批次缺少日期'}]
    calls=[]
    job=run(UpdateJobs(store,SimpleNamespace(collect=lambda:data),lambda target:calls.append(target)))
    assert job['status']=='partial'
    assert job['result']['query_status']=='published'
    assert job['result']['counts']['orders']==1
    assert len(store.rows('orders'))==1 and not calls
    row=store.rows('orders')[0]
    assert row['calculation_status']=='blocked' and row['logistics_company']==''
    assert row['shipping_id']=='@10/01#A#1'  # Known delivery dates remain usable for labels.
    with store.connect() as db:
        batch=db.execute('SELECT status,snapshot FROM sync_batches').fetchone()
        assert tuple(batch)==('published','{}')
        assert db.execute('SELECT count(*) FROM sync_issues').fetchone()[0]==1


def test_component_closure_propagates_multiline_without_blocking_independent_orders():
    a,b,c,d=[order(n) for n in ('A','B','C','D')]
    second=copy.deepcopy(a['lines'][0]);second.update(line_id='2',product_id='P2')
    b['lines'].append(second)
    c['lines'][0]['product_id']='P2'
    d['lines'][0]['product_id']='P3'
    data=query_snapshot([a,b,c,d])
    data['inventory'] += [dict(data['inventory'][0],product_id='P2'),
                          dict(data['inventory'][0],product_id='P3',saleable_qty='12')]
    data['calculation_issues']=[{'scope':'order','order_no':'A','code':'quantity','reason':'未完成量未核实'}]
    result=calculate_query_snapshot(data,today=TODAY)
    assert {p['tradeid'] for p in result['plans']}=={'D'}
    assert {b['order_no'] for b in result['blocked_orders']}=={'A','B','C'}


def test_partial_api_publication_keeps_stock_filter_available_for_ready_orders(store):
    a,b,c,d=[order(n) for n in ('A','B','C','D')]
    second=copy.deepcopy(a['lines'][0]);second.update(line_id='2',product_id='P2')
    b['lines'].append(second)
    c['lines'][0]['product_id']='P2'
    d['lines'][0]['product_id']='P3'
    data=query_snapshot([a,b,c,d])
    data['inventory'] += [dict(data['inventory'][0],product_id='P2'),
                          dict(data['inventory'][0],product_id='P3',saleable_qty='12')]
    data['calculation_issues']=[{'scope':'order','order_no':'A','code':'quantity','reason':'未完成量未核实'}]
    result=calculate_query_snapshot(data,today=TODAY)

    publish(store,data,result)
    ensure_print_schema(store)
    with store.connect() as db:
        assert json.loads(db.execute(
            "SELECT value FROM settings WHERE key='logistics_available'"
        ).fetchone()[0]) is True

    # A database published by the previous version may retain the false flag;
    # its source metadata lets the upgraded query path recover immediately,
    # without requiring another ERP sync first.
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value='false' WHERE key='logistics_available'")
    filtered=Queries(store).orders({'scope':'stock','print_state':'all'})
    assert {row['order_no'] for row in filtered['items']}=={'D'}
    assert filtered['items'][0]['logistics_company']=='LS库存件'


def test_undefined_scope_blocks_all_instead_of_skipping_demand():
    data=query_snapshot([order('A'),order('B')])
    data['orders'][0]['lines'][0]['warehouse_id']=None
    result=calculate_query_snapshot(data,today=TODAY)
    assert not result['plans'] and len(result['blocked_orders'])==2


def test_runtime_conflict_blocks_later_orders_in_same_stock_scope():
    a,b=order('A'),order('B')
    a['lines'][0]['expedite_evidence_complete']=False
    result=calculate_query_snapshot(query_snapshot([a,b]),today=TODAY)
    assert not result['plans'] and len(result['blocked_orders'])==2


def test_hash_and_target_order_are_stable_when_api_order_changes(store):
    data=query_snapshot([order('B'),order('A')])
    before=publish(store,data,calculate_query_snapshot(data,today=TODAY))
    data['orders'].reverse();data['records']['orders'].reverse()
    for row in data['records']['orders']:
        keys=list(row); keys.reverse()
        reordered={k:row[k] for k in keys};row.clear();row.update(reordered)
    after=publish(store,data,calculate_query_snapshot(data,today=TODAY))
    assert before==after


def test_query_sync_without_sender_and_repeat_active_uuid(store):
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:query_snapshot()))
    first=jobs.enqueue(str(uuid.uuid4()))
    assert jobs.enqueue(str(uuid.uuid4()))['id']==first['id']
    assert jobs.process_one()
    result=jobs.get(first['id'])
    assert result['result']['published'] and result['result']['logistics_status']=='unavailable'


def test_automatic_modes_are_read_only_and_reconcile_controls_full_read(store):
    flags=[];sent=[]
    class Source:
        def collect(self, progress=None, full_reconcile=False):
            flags.append(full_reconcile)
            return query_snapshot()
    jobs=UpdateJobs(store,Source(),lambda target:sent.append(target))
    refreshed=run(jobs,'refresh')
    reconciled=run(jobs,'reconcile')
    assert flags==[False,True]
    assert sent==[]
    assert refreshed['result']['logistics_status']=='preview'
    assert reconciled['result']['logistics_status']=='preview'


def test_automatic_mode_keeps_each_scheduled_run_and_deduplicates_same_slot(store):
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:query_snapshot()))
    first=jobs.enqueue_automatic('refresh','2026-10-06T07:30:00+08:00')
    assert jobs.process_one()
    repeated=jobs.enqueue_automatic('refresh','2026-10-06T07:30:00+08:00')
    assert repeated['id']==first['id'] and repeated['status']=='succeeded'
    second=jobs.enqueue_automatic('refresh','2026-10-06T11:30:00+08:00')
    assert second['id']!=first['id'] and second['status']=='queued'
    assert jobs.process_one()
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM update_jobs WHERE mode='refresh'").fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM sync_batches WHERE job_id=?',
                          (first['id'],)).fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM logistics_calculations WHERE job_id=?',
                          (first['id'],)).fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM sync_batches WHERE job_id=?',
                          (second['id'],)).fetchone()[0]==1


def test_monthly_reconcile_uses_its_own_slot_and_deduplicates_after_morning(store):
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:query_snapshot()))
    first=jobs.enqueue_automatic('reconcile','2026-11-01T03:40:00+08:00')
    assert first['result']['scheduled_for']=='2026-11-01T03:40:00+08:00'
    assert jobs.process_one()
    repeated=jobs.enqueue_automatic('reconcile','2026-11-01T11:30:00+08:00')
    assert repeated['id']==first['id'] and repeated['status']=='succeeded'
    following=jobs.enqueue_automatic('reconcile','2026-12-01T03:45:00+08:00')
    assert following['id']!=first['id']
    assert following['result']['scheduled_for']=='2026-12-01T03:40:00+08:00'
    before_slot=jobs.enqueue_automatic('reconcile','2027-01-01T03:39:00+08:00')
    assert before_slot['id']==following['id']
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM update_jobs WHERE mode='reconcile'").fetchone()[0]==2


def test_automatic_jobs_queue_behind_existing_job_without_becoming_manual_write(store):
    calls=[]
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:calls.append(1) or query_snapshot()))
    manual=jobs.enqueue(str(uuid.uuid4()),'preview')
    morning=jobs.enqueue_automatic('refresh','2026-10-06T07:30:00+08:00')
    noon=jobs.enqueue_automatic('refresh','2026-10-06T11:30:00+08:00')
    assert len({manual['id'],morning['id'],noon['id']})==3
    assert [job['status'] for job in jobs.history()]==['queued','queued','queued']
    assert jobs.process_one() and jobs.process_one() and jobs.process_one()
    assert len(calls)==3
    assert [jobs.get(job['id'])['result']['logistics_status']
            for job in (morning,noon)]==['preview','preview']


def test_failed_task_projection_retries_without_recollecting_erp(store):
    reads=[];refreshes=[]
    def projection(**kwargs):
        refreshes.append(kwargs)
        if len(refreshes)==1:
            raise RuntimeError('temporary local projection failure')
        return {'counts':{'tasks':2}}
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:reads.append(1) or query_snapshot()),
                    after_publish=projection)
    identity=jobs.enqueue_automatic('refresh','2026-10-06T07:30:00+08:00')['id']
    assert jobs.process_one()
    assert jobs.get(identity)['result']['daily_work_status']=='failed'
    with store.connect(write=True) as db:
        db.execute("UPDATE update_projection_retries SET next_retry_at='2000-01-01' WHERE job_id=?",
                   (identity,))
    assert jobs.retry_projection()
    assert jobs.get(identity)['result']['daily_work_status']=='updated'
    assert len(reads)==1 and len(refreshes)==2
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM update_projection_retries').fetchone()[0]==0


def test_older_projection_retry_cannot_overwrite_newer_publication(store):
    calls=[]
    def projection(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('projection unavailable')
    jobs=UpdateJobs(store,SimpleNamespace(collect=query_snapshot),after_publish=projection)
    identity=jobs.enqueue_automatic('refresh','2026-10-06T07:30:00+08:00')['id']
    assert jobs.process_one()
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'",(json.dumps('newer-version'),))
        db.execute("UPDATE update_projection_retries SET next_retry_at='2000-01-01' WHERE job_id=?",(identity,))
    assert jobs.retry_projection()
    assert jobs.get(identity)['result']['daily_work_status']=='superseded'
    assert len(calls)==1
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM update_projection_retries').fetchone()[0]==0


def test_restart_before_any_write_can_read_again(store):
    calls=[]
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:calls.append(1) or query_snapshot()))
    identity=jobs.enqueue(str(uuid.uuid4()))['id']
    jobs.mark(identity,'running','reading')
    jobs.recover()
    assert jobs.get(identity)['status']=='queued'
    assert jobs.process_one() and calls==[1]


def test_unknown_blocks_shared_stock_current_and_next_job_until_audited(store):
    data=query_snapshot([order('A'),order('B')]);calls=[]
    def timeout(target):
        calls.append(target['tradeid'])
        raise TimeoutError()
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:copy.deepcopy(data)),timeout)
    first=run(jobs)
    assert calls==['A'] and first['status']=='unknown'
    second=run(jobs)
    assert calls==['A']
    assert {a['tradeid'] for a in second['result']['attempts']}=={'A'}
    attempt=first['result']['attempts'][0]['id']
    with pytest.raises(ValueError): jobs.resolve_attempt(attempt,'not_applied','')
    jobs.resolve_attempt(attempt,'not_applied','已在 ERP 查看，两项均未变更')
    jobs.resolve_attempt(attempt,'not_applied','重复确认不得重复写审计')
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM freight_resolution_audit').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM freight_cache').fetchone()[0]==0
    jobs.sender=lambda target:calls.append(target['tradeid']) or True
    third=run(jobs)
    assert third['result']['succeeded']==2 and calls==['A','A','B']
    assert run(jobs)['result']['succeeded']==0


def test_unknown_applied_sets_only_verified_target_success_cache(store):
    data=query_snapshot();jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:data),lambda target:None)
    first=run(jobs);attempt=first['result']['attempts'][0]['id']
    resolved=jobs.resolve_attempt(attempt,'applied','已逐项核对公司和单号一致')
    assert resolved['status']=='succeeded'
    assert run(jobs)['result']['unchanged']==1
    with pytest.raises(JobConflict):jobs.resolve_attempt(attempt,'not_applied','不能覆盖原人工结论')


def test_partial_ledger_keeps_blocked_component_and_commits_only_valid_component(store):
    data=facts(events=[event()]);data['query_complete']=True
    first_order=data['orders'][0]
    second=copy.deepcopy(first_order);second['order_no']='B';second['lines'][0]['product_id']='P2'
    data['orders'].append(second)
    data['inventory'].append(dict(data['inventory'][0],product_id='P2'))
    data['receipt_events'].append(dict(event(),event_id='second',document_no='IN2',product_id='P2'))
    initial=calculate_query_snapshot(dict(data,receipt_state_bootstrap_verified=True,
        receipt_initial_state={'profile_id':'1','version':1,'events':{},'balances':{}}),today=TODAY)
    assert not initial['conflicts']
    previous=initial['receipt_state']
    data['calculation_issues']=[{'scope':'order','order_no':'A#1','code':'date','reason':'交期缺项'}]
    data['receipt_events'][1]['quantity']='5'
    result=calculate_query_snapshot(data,previous,today=TODAY)
    assert [p['tradeid'] for p in result['plans']]==['B']
    assert result['receipt_state']['events']['receipt-1']==previous['events']['receipt-1']
    assert result['receipt_state']['balances']['receipt-1']==previous['balances']['receipt-1']
    assert result['receipt_state']['events']['second']['quantity']==5
    old_keys={k for k in previous['orders'] if json.loads(k)[0]=='A#1'}
    assert all(result['receipt_state']['orders'][k]==previous['orders'][k] for k in old_keys)


def test_existing_database_migration_backs_up_and_preserves_records(store):
    with store.connect(write=True) as db:
        db.execute("INSERT INTO settings VALUES ('keep','true')")
        db.execute('PRAGMA user_version=0')
    reopened=Store(store.runtime)
    backups=list((store.runtime/'migration-backups').glob('*.sqlite3'))
    assert len(backups)==1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT value FROM settings WHERE key=\'keep\'').fetchone()[0]=='true'
        assert db.execute('PRAGMA user_version').fetchone()[0]==0
    with reopened.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==11
        migrated={row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {'inventory_movements','inventory_snapshots',
                'inventory_sync_watermarks','product_images',
                'packing_product_owners','daily_work_selections',
                'daily_work_batches','task_board_followups'} <= migrated
        assert db.execute('SELECT value FROM settings WHERE key=\'keep\'').fetchone()[0]=='true'
    Store(store.runtime)
    assert len(list((store.runtime/'migration-backups').glob('*.sqlite3')))==1


def test_v5_print_prepare_migration_is_backed_up_before_schema_change(store):
    """The response-loss key must never be added by an unversioned startup repair."""
    with store.connect(write=True) as db:
        db.execute('DROP INDEX IF EXISTS print_job_prepare_key')
        db.execute('ALTER TABLE print_jobs DROP COLUMN prepare_idempotency_key')
        db.execute('''INSERT INTO print_jobs
            (id,status,frozen,metadata,idempotency_key,claim_token_hash)
            VALUES ('legacy-job','prepared','{}','{}',NULL,NULL)''')
        db.execute('PRAGMA user_version=5')

    reopened = Store(store.runtime)
    backups = list((store.runtime/'migration-backups').glob('before-v6-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 5
        old_columns = {row[1] for row in backup.execute('PRAGMA table_info(print_jobs)')}
        assert 'prepare_idempotency_key' not in old_columns
        assert backup.execute("SELECT status FROM print_jobs WHERE id='legacy-job'").fetchone()[0] == 'prepared'
    with reopened.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 11
        columns = {row['name'] for row in db.execute('PRAGMA table_info(print_jobs)')}
        assert 'prepare_idempotency_key' in columns
        assert db.execute("SELECT status FROM print_jobs WHERE id='legacy-job'").fetchone()[0] == 'prepared'
        index = db.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                           "AND name='print_job_prepare_key'").fetchone()
        assert index and 'UNIQUE INDEX' in index['sql'].upper()


def test_v7_board_migration_preserves_existing_followup_and_print_records(store):
    with store.connect(write=True) as db:
        for table in ('packing_product_owners', 'daily_work_selections',
                      'daily_work_batches', 'task_board_followups'):
            db.execute(f'DROP TABLE {table}')
        db.execute("""INSERT INTO customer_order_followups
            (group_id,profile_id,warehouse_id,customer_order_no,assignee_id,status,
             next_follow_up_date,version,source_data_version,source_fingerprint,created_at,updated_at) VALUES
            ('group1','P','08390','PO','seller','waiting','2026-10-06',4,
             'version1','fingerprint','2026-10-04','2026-10-04')""")
        db.execute("""INSERT INTO customer_order_followup_events VALUES
            ('event1','group1','key1','seller','已沟通','采购','2026-10-06','2026-10-04')""")
        db.execute("""INSERT INTO print_jobs
            (id,status,frozen,metadata,prepare_idempotency_key)
            VALUES ('print1','unknown','{}','{}','prepare1')""")
        db.execute('PRAGMA user_version=7')
    reopened = Store(store.runtime)
    backups = list((store.runtime / 'migration-backups').glob('before-v8-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 7
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_work_batches'").fetchone()
    with reopened.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 11
        assert db.execute("SELECT version FROM customer_order_followups WHERE group_id='group1'").fetchone()[0] == 4
        assert db.execute("SELECT note FROM customer_order_followup_events WHERE id='event1'").fetchone()[0] == '已沟通'
        assert db.execute("SELECT status FROM print_jobs WHERE id='print1'").fetchone()[0] == 'unknown'
        assert db.execute('SELECT count(*) FROM daily_work_batches').fetchone()[0] == 0


def test_read_progress_and_restart_after_publication_keep_data_version(store):
    class Source:
        def collect(self,progress=None):
            progress({'source':'orders','current':12,'total':30})
            during=jobs.latest()
            assert during['stage']=='reading'
            assert during['progress']=={'completed':12,'total':30}
            return query_snapshot()
    jobs=UpdateJobs(store,Source(),lambda target:True)
    result=run(jobs)
    version=result['data_version'];published=result['published_at']
    jobs.mark(result['id'],'running','writing',result['result'])
    jobs.recover()
    recovered=jobs.get(result['id'])
    assert recovered['status']=='unknown'
    assert recovered['data_version']==version and recovered['published_at']==published
    assert jobs.process_one() is False


def test_independent_valid_scope_failure_does_not_roll_back_publication(store):
    a,b=order('A'),order('B');b['lines'][0]['product_id']='P2'
    data=query_snapshot([a,b]);data['inventory'].append(dict(data['inventory'][0],product_id='P2'))
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:data),lambda target:target['tradeid']=='A')
    result=run(jobs)
    assert result['status']=='partial' and result['published_at'] and result['data_version']
    assert len(store.rows('orders'))==2
    assert result['result']['succeeded']==1 and result['result']['failed']==1
    with store.connect() as db:
        assert [row[0] for row in db.execute('SELECT tradeid FROM freight_cache')]==['A']


def test_changed_unknown_order_brings_new_shared_scope_into_hold(store):
    data=query_snapshot([order('A')])
    calls=[]
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:copy.deepcopy(data)),lambda target:calls.append(target['tradeid']) or None)
    run(jobs)
    data['orders'][0]['lines'][0]['product_id']='P2'
    data['inventory'].append(dict(data['inventory'][0],product_id='P2'))
    b=copy.deepcopy(data['orders'][0]);b['order_no']='B';data['orders'].append(b)
    row=copy.deepcopy(data['records']['orders'][0]);row.update(id='B:1',order_no='B');data['records']['orders'].append(row)
    result=run(jobs)
    assert calls==['A']
    assert set(result['result']['unknown_orders'])=={'A','B'}


def test_large_shared_component_keeps_diagnostics_linear_and_bounded(store):
    # Production scale: a missing field on every one of 2,400 pending orders.
    orders=[order(f'A{i:04d}') for i in range(2400)]
    data=query_snapshot(orders)
    data['calculation_issues']=[{'scope':'order','order_no':o['order_no'],
        'code':'delivery_date_missing','reason':'交货日期缺少验证依据'} for o in orders]
    result=calculate_query_snapshot(data,today=TODAY)
    assert len(result['source_issues'])==2400
    assert len(result['issues'])==0 and len(result['warnings'])==2400
    assert len(result['plans'])==2400 and all(p['write_enabled'] is False for p in result['plans'])
    assert len(result['scope_groups'])==1
    assert len(json.dumps(result,ensure_ascii=False))<4_000_000
    publish(store,data,result)
    assert len(store.rows('orders'))==2400


def test_wide_inventory_component_stores_keys_once_not_per_order():
    orders=[order(f'A{i:04d}') for i in range(400)]
    for index,o in enumerate(orders):
        o['lines'][0]['product_id']=f'P{index}'
        extra=copy.deepcopy(o['lines'][0]);extra.update(line_id='2',product_id=f'P{index+1}')
        o['lines'].append(extra)
    data=query_snapshot(orders)
    data['calculation_issues']=[{'scope':'global','code':str(i),'reason':f'问题{i}'} for i in range(1000)]
    result=calculate_query_snapshot(data,today=TODAY)
    assert len(result['scope_groups']['0'])==401
    assert sum(len(keys) for keys in result['order_scopes'].values())==800
    assert len(result['issues'])<=400*8
    assert len(json.dumps(result,ensure_ascii=False))<3_000_000


def test_missing_delivery_on_neighbor_does_not_invalidate_verified_print_date(store):
    data=query_snapshot([order('A'),order('B')])
    data['calculation_issues']=[{'scope':'order','order_no':'A','code':'delivery_date_missing','reason':'交货日期缺少依据'}]
    result=calculate_query_snapshot(data,today=TODAY)
    publish(store,data,result)
    rows={row['order_no']:row for row in store.rows('orders')}
    assert rows['A']['shipping_id']==''
    assert rows['B']['shipping_id']=='@10/01#B'
    assert rows['B']['calculation_status']=='ready'


def test_resolution_cannot_finish_a_still_running_update(store):
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:query_snapshot()),lambda target:None)
    result=run(jobs);attempt=result['result']['attempts'][0]['id']
    jobs.mark(result['id'],'running','writing',result['result'])
    with pytest.raises(JobConflict):jobs.resolve_attempt(attempt,'applied','核对时任务仍在写入')
    assert jobs.get(result['id'])['status']=='running'


def test_resolved_unknown_does_not_claim_held_unattempted_orders_succeeded(store):
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:query_snapshot([order('A'),order('B')])),lambda target:None)
    first=run(jobs);attempt=first['result']['attempts'][0]['id']
    jobs.resolve_attempt(attempt,'applied','A 的目标值已核对生效')
    result=jobs.get(first['id'])
    assert result['status']=='partial'
    assert result['result']['deferred_orders']==['B']
    assert result['result']['succeeded']==1 and result['result']['unknown']==0


def test_query_coverage_metadata_is_atomic_and_does_not_change_record_version(store):
    data=query_snapshot()
    data['query_scope']={'kind':'unshipped_documents','start':'1900-01-01','end':'2100-01-01',
        'statuses':'1,2,3,4,5','quantity_basis':'reported_sales_quantity',
        'purchase_history_start':'2026-03-01','purchase_history_end':'2026-09-26'}
    data['source_counts']={'order_documents':1,'products':10}
    calculation=calculate_query_snapshot(data,today=TODAY)
    version=publish(store,data,calculation)
    data['query_scope']['purchase_history_start']='2026-04-01'
    assert publish(store,data,calculation)==version
    with store.connect() as db:
        saved=json.loads(db.execute("SELECT value FROM settings WHERE key='data_query_scope'").fetchone()[0])
        basis=json.loads(db.execute("SELECT value FROM settings WHERE key='data_quantity_basis'").fetchone()[0])
    assert saved==data['query_scope'] and basis=='reported_sales_quantity'
    sources={source['kind']:source for source in store.source_list()}
    assert sources['purchases']['query_scope']['purchase_history_start']=='2026-04-01'
    assert sources['orders']['quantity_basis']=='reported_sales_quantity'
    result=run(UpdateJobs(store,SimpleNamespace(collect=lambda:data)))['result']
    assert result['query_scope']==saved and result['quantity_basis']==basis
    assert result['source_counts']==data['source_counts']
