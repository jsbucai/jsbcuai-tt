import json
import uuid
from datetime import datetime
from types import SimpleNamespace
import pytest
from test_api_migration import store
from test_query_publication import query_snapshot
from core.api_source import SourceChanged
from core.update_jobs import UpdateJobs
from core.freight_write_policy import FreightWritePolicy
from core.web_operations import WebOperations, WebOperationConflict
from core.work_schedule import workday_slot


def test_mutating_source_retries_same_job_with_original_write_scope(store):
    reads, writes = [], []
    def collect():
        reads.append(1)
        if len(reads) == 1:
            raise SourceChanged('订单分页期间数量变化')
        return query_snapshot()
    policy = FreightWritePolicy(max_per_job=1, allow_all=True)
    jobs = UpdateJobs(store, SimpleNamespace(collect=collect), lambda p:writes.append(p) or True, policy)
    job = jobs.enqueue(str(uuid.uuid4()), 'sync')
    assert jobs.process_one()
    waiting = jobs.get(job['id'])
    assert waiting['status'] == 'queued' and waiting['stage'] == 'waiting_source'
    assert not waiting['result']['published'] and not writes
    assert not jobs.process_one()  # Backoff does not spin or duplicate reads.
    with store.connect(write=True) as db:
        result = json.loads(db.execute('SELECT result FROM update_jobs WHERE id=?', (job['id'],)).fetchone()[0])
        result['next_retry_at'] = '2000-01-01'
        db.execute('UPDATE update_jobs SET result=? WHERE id=?', (json.dumps(result), job['id']))
    jobs.write_policy = FreightWritePolicy.unbounded_for_injected_sender()
    assert jobs.process_one()
    final = jobs.get(job['id'])
    assert final['result']['published'] and len(reads) == 2
    assert final['result']['write_policy'] == policy.freeze()
    assert len(writes) <= 1


def test_workday_schedule_keeps_preload_and_stops_at_nineteen():
    def slot(t): return workday_slot(datetime.fromisoformat('2026-10-12T'+t+'+08:00'))
    assert slot('07:29:59') is None
    assert slot('07:45:00').strftime('%H:%M') == '07:30'
    assert slot('08:14:59').strftime('%H:%M') == '08:00'
    assert slot('18:59:59').strftime('%H:%M') == '18:45'
    assert slot('19:00:00') is None
    assert slot('23:30:00') is None


def test_workday_queue_coalesces_but_retains_each_trigger(store):
    jobs = UpdateJobs(store, None, write_policy=FreightWritePolicy(max_per_job=5, allow_all=True))
    morning = jobs.enqueue_workday(datetime.fromisoformat('2026-10-12T07:30:00+08:00'))
    assert morning['mode'] == 'refresh'
    first = jobs.enqueue_workday(datetime.fromisoformat('2026-10-12T08:00:00+08:00'))
    later = jobs.enqueue_workday(datetime.fromisoformat('2026-10-12T08:15:00+08:00'))
    assert later['id'] == first['id'] and first['mode'] == 'sync'
    assert jobs.enqueue_workday(datetime.fromisoformat('2026-10-12T22:00:00+08:00')) is None
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM update_schedule_triggers').fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM update_jobs').fetchone()[0] == 2


def test_purchase_pause_releases_only_downloads_and_does_not_replay_backlog(store):
    queue = WebOperations(store)
    def add(kind, name):
        return queue.enqueue(kind=kind, actor_id='tester', role='sales' if kind=='sales_order_import' else 'purchase',
            profile_id='10', target_id=name, key=name, payload={})['operation']['id']
    first = add('purchase_download', 'download-first')
    add('purchase_download', 'download-second')
    sales = add('sales_order_import', 'sales-draft-one')
    taken = queue.claim_next('worker-0123456789')
    assert taken['id'] == first
    with pytest.raises(WebOperationConflict): queue.pause_purchase_downloads('login issue')
    queue.finish(first, 'worker-0123456789', status='needs_human', stage='登录验证', detail='验证码')
    assert queue.pause_purchase_downloads('login issue')['retired_downloads'] == 2
    with pytest.raises(WebOperationConflict): add('purchase_download', 'download-third')
    assert queue.claim_next('worker-0123456789')['id'] == sales
    queue.resume_purchase_downloads()
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='purchase_download' AND status='queued'").fetchone()[0] == 0


def test_api_and_browser_cannot_claim_together(store):
    queue = WebOperations(store)
    jobs = UpdateJobs(store, SimpleNamespace(collect=query_snapshot))
    queue.enqueue(kind='purchase_download', actor_id='test', role='purchase', profile_id='10',
                  target_id='download-first', key='download-first', payload={})
    jobs.enqueue(str(uuid.uuid4()), 'refresh')
    with store.connect(write=True) as db:
        db.execute("UPDATE update_jobs SET status='running'")
    assert queue.claim_next('worker-0123456789') is None
    with store.connect(write=True) as db:
        db.execute("UPDATE update_jobs SET status='queued'")
    queue.claim_next('worker-0123456789')
    assert not jobs.process_one()
