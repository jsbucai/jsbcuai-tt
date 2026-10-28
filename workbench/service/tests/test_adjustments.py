import copy
import json
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.adjustments import AdjustmentConflict, AdjustmentFailure, AdjustmentJobs
from core.sql_queries import Queries
from core.store import Store, dumps


CLIENT = str(uuid.uuid4())
OTHER_CLIENT = str(uuid.uuid4())


def payload(kind='loss', **updates):
    return {'id': str(uuid.uuid4()), 'client_id': CLIENT, 'type': kind,
            'items': [{'sku': 'SKU001', 'name': '六角螺栓', 'spec': 'M12',
                       'model': '8.8', 'quantity': '2.500000', 'note': '包装发现短缺'}], **updates}


@pytest.fixture
def services():
    ROOT.joinpath('runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='adjustment-test-', dir=ROOT / 'runtime') as temp:
        store = Store(Path(temp))
        with store.connect(write=True) as db:
            for index in range(1, 102):
                sku = f'SKU{index:03}'
                db.execute('INSERT INTO records(kind,id,payload,sku) VALUES (?,?,?,?)',
                           ('inventory', sku, dumps({'id': sku, 'sku': sku, 'product_name': '螺栓'}), sku))
            db.execute("INSERT INTO print_jobs(id,status,frozen,metadata) VALUES ('print-before','unknown','{}','{}')")
        workers = []

        def factory(sender=None):
            worker = AdjustmentJobs(store, Queries(store), sender=sender)
            workers.append(worker)
            return worker

        yield store, factory
        for worker in workers:
            assert worker.stop(timeout=3), 'test sender did not stop'


def wait_for(worker, identity, status, client_id=CLIENT):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        batch = worker.get(identity, client_id)
        if batch['status'] == status:
            return batch
        time.sleep(0.01)
    pytest.fail(f"batch did not reach {status}: {worker.get(identity, client_id)}")


def test_manual_confirmation_is_audited_idempotent_and_never_resubmits(services):
    store, factory = services
    sent = []
    jobs, value = factory(lambda batch: sent.append(batch)), payload()
    jobs.enqueue(value)
    with store.connect(write=True) as db:
        db.execute("UPDATE adjustment_batches SET status='unknown',message='连接中断，需核对' WHERE id=?", (value['id'],))
    directory = store.runtime / 'adjustments' / value['id']
    directory.mkdir(parents=True)
    (directory / 'submission.json').write_text(json.dumps({'batch_id': value['id'], 'document_no': '2026-09-10-12-08-17-546a085c'}), 'utf-8')
    assert jobs.get(value['id'], CLIENT)['document_no'] == '2026-09-10-12-08-17-546a085c'
    confirmed = jobs.confirm(value['id'], {'client_id': CLIENT, 'confirmed': True})
    assert confirmed['status'] == 'succeeded' and confirmed['completion_source'] == 'user_verified'
    assert jobs.confirm(value['id'], {'client_id': CLIENT, 'confirmed': True}) == confirmed
    assert jobs.enqueue(value) == confirmed
    assert jobs.list(CLIENT) == []
    assert factory().get(value['id'], CLIENT) == confirmed
    assert sent == [] and jobs.thread is None
    with store.connect() as db:
        audits = db.execute('SELECT * FROM adjustment_confirmations').fetchall()
        assert len(audits) == 1
        assert audits[0]['previous_status'] == 'unknown' and audits[0]['previous_message'] == '连接中断，需核对'
        assert audits[0]['client_id'] == CLIENT and audits[0]['confirmed_at']
    with pytest.raises(AdjustmentConflict):
        jobs.retry(value['id'], CLIENT)


@pytest.mark.parametrize('status', ['queued', 'running', 'failed'])
def test_manual_confirmation_cannot_skip_in_flight_or_failed_work(services, status):
    store, factory = services
    jobs, value = factory(), payload()
    jobs.enqueue(value)
    with store.connect(write=True) as db:
        db.execute('UPDATE adjustment_batches SET status=? WHERE id=?', (status, value['id']))
    with pytest.raises(AdjustmentConflict):
        jobs.confirm(value['id'], {'client_id': CLIENT, 'confirmed': True})
    assert jobs.get(value['id'], CLIENT)['status'] == status


def test_manual_confirmation_requires_explicit_true_and_device_ownership(services):
    store, factory = services
    jobs, value = factory(), payload()
    jobs.enqueue(value)
    with store.connect(write=True) as db:
        db.execute("UPDATE adjustment_batches SET status='unknown' WHERE id=?", (value['id'],))
    for confirmed in (False, None, 'true', 1):
        with pytest.raises(ValueError):
            jobs.confirm(value['id'], {'client_id': CLIENT, 'confirmed': confirmed})
    with pytest.raises(KeyError):
        jobs.confirm(value['id'], {'client_id': OTHER_CLIENT, 'confirmed': True})
    assert jobs.get(value['id'], CLIENT)['status'] == 'unknown'


def test_enqueue_is_explicit_and_batch_payload_is_frozen_and_normalized(services):
    store, factory = services
    sent = []
    jobs = factory(lambda batch: sent.append(batch) or {'document_no': 'LOSS001'})
    value = payload()
    value['items'][0]['sku'] = ' sku001 '
    queued = jobs.enqueue(value)
    assert queued['status'] == 'queued'
    assert queued['items'][0]['sku'] == 'SKU001'
    assert queued['items'][0]['quantity'] == '2.5'
    value['items'][0]['quantity'] = '99'
    queued['items'][0]['note'] = 'changed outside database'
    assert jobs.get(value['id'], CLIENT)['items'][0]['quantity'] == '2.5'
    assert jobs.get(value['id'], CLIENT)['items'][0]['note'] == '包装发现短缺'
    assert jobs.thread is None and sent == []
    assert jobs.start() is True
    result = wait_for(jobs, value['id'], 'succeeded')
    assert result['document_no'] == 'LOSS001' and result['retryable'] is False
    assert sent[0]['status'] == 'running'
    assert jobs.list(CLIENT) == []
    with store.connect() as db:
        assert tuple(db.execute('SELECT id,status,frozen,metadata FROM print_jobs').fetchone()) == ('print-before','unknown','{}','{}')
        assert db.execute("SELECT count(*) FROM records WHERE kind='inventory'").fetchone()[0] == 101


@pytest.mark.parametrize('quantity', ['0', '-1', 'NaN', 'Infinity', float('nan'), float('inf'), True, None, '', '0.0000001', '2.1234567', '1e999999', '100000000.000001'])
def test_invalid_quantities_never_enqueue(services, quantity):
    _, factory = services
    jobs, value = factory(), payload()
    value['items'][0]['quantity'] = quantity
    with pytest.raises(ValueError):
        jobs.enqueue(value)
    assert jobs.list(CLIENT) == []


@pytest.mark.parametrize(('quantity', 'expected'), [('0.000001','0.000001'), (0.000001,'0.000001'), ('2.5000000','2.5'), ('1000','1000'), ('1e2','100'), ('100000000','100000000')])
def test_quantity_normalization_preserves_exact_six_decimal_values(services, quantity, expected):
    _, factory = services
    value = payload()
    value['items'][0]['quantity'] = quantity
    assert factory().enqueue(value)['items'][0]['quantity'] == expected


def test_unknown_sku_duplicates_bad_identity_and_oversized_batches_are_rejected(services):
    _, factory = services
    jobs = factory()
    invalid = [payload(type='other'), payload(id='not-a-uuid'), payload(client_id='phone1'), payload(items=[])]
    missing = payload()
    missing['items'][0]['sku'] = 'MISSING'
    duplicate = payload()
    duplicate['items'] *= 2
    long_note = payload()
    long_note['items'][0]['note'] = 'a' * 201
    over = payload(items=[{'sku': f'SKU{index:03}', 'quantity': '1'} for index in range(1, 102)])
    for value in invalid + [missing, duplicate, over, long_note]:
        with pytest.raises(ValueError):
            jobs.enqueue(value)
    assert jobs.list(CLIENT) == []


def test_concurrent_replayed_posts_send_once_and_modified_id_conflicts(services):
    _, factory = services
    entered, release = threading.Event(), threading.Event()
    sent = []

    def sender(batch):
        sent.append(batch['id'])
        entered.set()
        assert release.wait(3)
        return {'document_no': 'ONLY-ONCE'}

    jobs, value = factory(sender), payload()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: jobs.enqueue(copy.deepcopy(value)), range(16)))
        assert {item['id'] for item in replies} == {value['id']}
        jobs.start()
        assert entered.wait(3)
        assert jobs.enqueue(value)['status'] == 'running'
        changed = copy.deepcopy(value)
        changed['items'][0]['quantity'] = '3'
        with pytest.raises(AdjustmentConflict):
            jobs.enqueue(changed)
    finally:
        release.set()
    wait_for(jobs, value['id'], 'succeeded')
    assert jobs.enqueue(value)['status'] == 'succeeded'
    assert sent == [value['id']]


def test_idempotent_replay_survives_inventory_changes_and_respects_ownership(services):
    store, factory = services
    jobs, value = factory(), payload()
    jobs.enqueue(value)
    with store.connect(write=True) as db:
        db.execute("DELETE FROM records WHERE kind='inventory'")
    replay = copy.deepcopy(value)
    replay['items'][0]['quantity'] = '2.50'
    assert jobs.enqueue(replay)['status'] == 'queued'
    with pytest.raises(AdjustmentConflict):
        jobs.enqueue({**value, 'client_id': OTHER_CLIENT})
    with pytest.raises(KeyError):
        jobs.get(value['id'], OTHER_CLIENT)
    with pytest.raises(KeyError):
        jobs.retry(value['id'], OTHER_CLIENT)
    assert jobs.list(OTHER_CLIENT) == []
    with pytest.raises(ValueError):
        jobs.enqueue(payload())


def test_one_worker_serializes_batches_from_different_clients_and_instances(services):
    _, factory = services
    entered, release = threading.Event(), threading.Event()
    sent = []

    def sender(batch):
        sent.append(batch['id'])
        if len(sent) == 1:
            entered.set()
            assert release.wait(3)
        return {'document_no': f'DOC-{len(sent)}'}

    jobs, other = factory(sender), factory(sender)
    first, second = payload(), payload('overflow', client_id=OTHER_CLIENT)
    try:
        jobs.enqueue(first)
        assert jobs.start()
        assert entered.wait(3)
        assert other.start() is False
        other.enqueue(second)
        assert jobs.get(first['id'], CLIENT)['status'] == 'running'
        assert other.get(second['id'], OTHER_CLIENT)['status'] == 'queued'
        assert sent == [first['id']]
    finally:
        release.set()
    wait_for(jobs, first['id'], 'succeeded')
    wait_for(other, second['id'], 'succeeded', OTHER_CLIENT)
    assert sent == [first['id'], second['id']]


def test_restart_marks_interrupted_running_unknown_and_resumes_only_queued(services):
    store, factory = services
    jobs = factory()
    interrupted, pending = payload(), payload('overflow')
    jobs.enqueue(interrupted)
    jobs.enqueue(pending)
    with store.connect(write=True) as db:
        db.execute("UPDATE adjustment_batches SET status='running' WHERE id=?", (interrupted['id'],))
    sent = []
    recovered = factory(lambda batch: sent.append(batch['id']) or {'document_no': 'RECOVERED'})
    assert sent == [] and recovered.thread is None
    recovered.start()
    unknown = wait_for(recovered, interrupted['id'], 'unknown')
    assert not unknown['retryable']
    assert interrupted['id'][:8] in unknown['message']
    wait_for(recovered, pending['id'], 'succeeded')
    assert sent == [pending['id']]
    assert [item['id'] for item in recovered.list(CLIENT)] == [interrupted['id']]
    with pytest.raises(AdjustmentConflict):
        recovered.retry(interrupted['id'], CLIENT)
    assert recovered.enqueue(interrupted)['status'] == 'unknown'


def test_retry_requires_explicit_safe_failure_and_never_happens_on_its_own(services):
    _, factory = services
    sent = []

    def sender(batch):
        sent.append(batch['id'])
        if len(sent) == 1:
            raise AdjustmentFailure('登录失败，未提交单据', retryable=True)
        return {'document_no': 'RETRY-OK'}

    jobs, value = factory(sender), payload()
    jobs.enqueue(value)
    jobs.start()
    failed = wait_for(jobs, value['id'], 'failed')
    assert failed['retryable'] is True
    assert jobs.enqueue(value)['status'] == 'failed'
    assert sent == [value['id']]
    assert jobs.retry(value['id'], CLIENT)['status'] == 'queued'
    result = wait_for(jobs, value['id'], 'succeeded')
    assert result['document_no'] == 'RETRY-OK'
    assert sent == [value['id'], value['id']]
    with pytest.raises(AdjustmentConflict):
        jobs.retry(value['id'], CLIENT)


@pytest.mark.parametrize(('failure', 'status'), [(TimeoutError('secret request contents'), 'unknown'), (AdjustmentFailure('商品不允许报损'), 'failed')])
def test_uncertain_or_permanent_failure_cannot_be_retried(services, failure, status):
    _, factory = services
    sent = []

    def sender(batch):
        sent.append(batch['id'])
        raise failure

    jobs, value = factory(sender), payload()
    jobs.enqueue(value)
    jobs.start()
    result = wait_for(jobs, value['id'], status)
    assert not result['retryable']
    assert 'secret' not in result['message']
    with pytest.raises(AdjustmentConflict):
        jobs.retry(value['id'], CLIENT)
    assert jobs.enqueue(value)['status'] == status
    assert sent == [value['id']]


def test_missing_document_number_is_unknown_and_unconfigured_worker_does_not_run(services):
    _, factory = services
    unconfigured, value = factory(), payload()
    unconfigured.enqueue(value)
    with pytest.raises(ValueError):
        unconfigured.start()
    assert unconfigured.get(value['id'], CLIENT)['status'] == 'queued'
    configured = factory(lambda batch: {'message': 'response lost'})
    configured.start()
    result = wait_for(configured, value['id'], 'unknown')
    assert not result['retryable'] and result['document_no'] == ''


@pytest.mark.parametrize(('document_no', 'stored'), [('BS-20260910-001','BS-20260910-001'), ('secret\nrequest',''), ('x' * 129,''), (123,'')])
def test_unknown_keeps_only_safe_document_number_or_batch_reference(services, document_no, stored):
    _, factory = services

    def sender(batch):
        failure = TimeoutError('secret HTTP response')
        failure.document_no = document_no
        raise failure

    jobs, value = factory(sender), payload()
    jobs.enqueue(value)
    jobs.start()
    result = wait_for(jobs, value['id'], 'unknown')
    assert result['document_no'] == stored
    assert (stored or value['id'][:8]) in result['message']
    assert 'secret' not in result['message']


def test_local_completion_failure_never_repeats_sender_and_worker_can_continue(services, monkeypatch):
    _, factory = services
    sent = []
    jobs = factory(lambda batch: sent.append(batch['id']) or {'document_no': 'ACCEPTED'})
    first, second = payload(), payload('overflow')
    original = jobs._finish
    failed_once = False

    def fail_once(batch, status, *args, **kwargs):
        nonlocal failed_once
        if not failed_once and status == 'succeeded':
            failed_once = True
            raise OSError('temporary local disk failure')
        return original(batch, status, *args, **kwargs)

    monkeypatch.setattr(jobs, '_finish', fail_once)
    jobs.enqueue(first)
    jobs.enqueue(second)
    jobs.start()
    result = wait_for(jobs, first['id'], 'unknown')
    assert not result['retryable']
    assert result['document_no'] == 'ACCEPTED'
    wait_for(jobs, second['id'], 'succeeded')
    assert sent == [first['id'], second['id']]
    assert jobs.thread.is_alive()


def test_graceful_stop_finishes_current_batch_but_leaves_next_queued(services):
    _, factory = services
    entered, release = threading.Event(), threading.Event()
    sent = []

    def sender(batch):
        sent.append(batch['id'])
        entered.set()
        assert release.wait(3)
        return {'document_no': 'STOPPED-CLEANLY'}

    jobs, first, second = factory(sender), payload(), payload('overflow')
    try:
        jobs.enqueue(first)
        jobs.enqueue(second)
        jobs.start()
        assert entered.wait(3)
        assert jobs.stop(timeout=0.01) is False
        assert jobs.get(first['id'], CLIENT)['status'] == 'running'
    finally:
        release.set()
    assert jobs.stop(timeout=3)
    assert jobs.get(first['id'], CLIENT)['status'] == 'succeeded'
    assert jobs.get(second['id'], CLIENT)['status'] == 'queued'
    assert sent == [first['id']]
