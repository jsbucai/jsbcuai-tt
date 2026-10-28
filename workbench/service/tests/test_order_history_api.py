"""Offline API and durable worker contract for manual sales-history reads."""

import copy
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app
from core.order_history_archive import SHANGHAI, QIMEN


def document(number='QJ0804#49', quantity='5'):
    return {'trade_order': {'trade_id': number,
                            'extendProps': {'process_state': 600}},
            'order_info': {'order_info': [{
                'recid': '10', 'spec_code': 'LS01', 'goods_name': '测试商品',
                'sell_count': quantity}]}}


class ReadOnlyErp:
    last_auth_error = None

    def __init__(self):
        self.rows = {'2026-08': [document()]}
        self.calls = []
        self.fail_month = None

    def account(self):
        return '79'

    def call(self, method, params):
        assert method == QIMEN  # A write or an unrelated ERP call fails the test.
        self.calls.append((method, copy.deepcopy(params)))
        month = params['extendProps']['start_time'][:7]
        if month == self.fail_month:
            return {'code': '500'}
        rows = self.rows.get(month, [])
        page = params['pageNo']
        size = params['pageSize']
        return {'code': '0', 'TotalResults': len(rows),
                'trade_orders': {'trade_orders': copy.deepcopy(
                    rows[(page - 1) * size:page * size])}}


@pytest.fixture
def configured():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / 'runtime', prefix='history-api-') as runtime:
        erp = ReadOnlyErp()
        app = create_app({'TESTING': True, 'AUTO_IMPORT': False,
                          'SOURCE_MODE': 'api', 'PUBLIC_MODE': True,
                          'ADMIN_PASSWORD_HASH': generate_password_hash('administrator-password'),
                          'ERP_CLIENT': erp, 'API_SOURCE': object()}, runtime_dir=runtime)
        yield app, erp


def admin_headers(client):
    login = client.post('/api/v2/auth/login',
                        json={'password': 'administrator-password'})
    assert login.status_code == 200
    return {'Authorization': 'Bearer ' + login.get_json()['token']}


def queue(client, headers, month='2026-08', identity=None, **extra):
    return client.post('/api/v2/order-history/jobs', headers=headers,
                       json={'id': identity or str(uuid.uuid4()), 'profile_id': '79',
                             'month': month, **extra})


def test_manual_admin_job_publishes_one_month_and_read_only_review_api(configured):
    app, erp = configured
    client = app.test_client()
    path = '/api/v2/order-history/coverage?profile_id=79&year=2026'
    assert client.get(path).status_code == 401
    headers = admin_headers(client)
    health = client.get('/api/v2/health', headers=headers).get_json()
    assert health['capabilities']['order_history_manual_read'] is True
    assert health['capabilities']['order_history_automatic'] is False
    assert client.get(path, headers=headers).get_json()['coverage']['archived_lines'] == 0

    job_response = queue(client, headers)
    assert job_response.status_code == 202
    job = job_response.get_json()['job']
    assert job['status'] == 'queued' and job['month'] == '2026-08'
    worker = app.extensions['service']['order_history_jobs']
    assert worker.process_one() is True
    assert worker.process_one() is False
    completed = client.get('/api/v2/order-history/jobs/' + job['id'],
                           headers=headers).get_json()['job']
    assert completed['status'] == 'succeeded'
    assert completed['result']['coverage']['unique_lines_in_window'] == 1
    assert completed['result']['review_required'] is True
    assert len(erp.calls) == 2  # One complete page plus its full replay.
    assert all(call[1]['extendProps']['bill_status'] == '6,7' for call in erp.calls)
    assert all(call[1]['extendProps']['query_time_type'] == 1 for call in erp.calls)

    coverage = client.get(path, headers=headers).get_json()['coverage']
    assert coverage['completed_months'] == ['2026-08']
    assert coverage['archived_lines'] == 1
    assert coverage['review_required'] is True
    assert coverage['lifetime_coverage_complete'] is False
    lines = client.get('/api/v2/order-history/lines?profile_id=79&product_code=LS01',
                       headers=headers).get_json()
    assert lines['total'] == 1
    assert lines['items'][0]['source_line_id'] == '10'
    assert lines['items'][0]['reported_quantity'] == '5'
    assert lines['items'][0]['quantity_basis'] == 'qimen_sell_count_history_unverified'
    assert lines['items'][0]['customer_name'] == ''
    assert lines['review_required'] is True
    assert client.get('/api/v2/order-history/lines?profile_id=79&product_code=OTHER',
                      headers=headers).get_json()['total'] == 0


def test_employee_cannot_enqueue_or_read_history(configured):
    app, erp = configured
    client = app.test_client()
    admin = admin_headers(client)
    staff = client.post('/api/v2/staff', headers=admin, json={
        'username': 'history.worker', 'display_name': '采购员',
        'password': 'worker-password', 'roles': ['purchase']})
    assert staff.status_code in (200, 201)
    token = client.post('/api/v2/auth/login', json={
        'username': 'history.worker', 'password': 'worker-password'}).get_json()['token']
    headers = {'Authorization': 'Bearer ' + token}
    assert queue(client, headers).status_code == 403
    assert client.get('/api/v2/order-history/coverage?profile_id=79',
                      headers=headers).status_code == 403
    assert client.get('/api/v2/order-history/lines?profile_id=79',
                      headers=headers).status_code == 403
    assert client.get('/api/v2/order-history/jobs', headers=headers).status_code == 403
    assert erp.calls == []


def test_job_frozen_scope_idempotency_one_at_a_time_and_closed_month(configured):
    app, erp = configured
    client = app.test_client()
    headers = admin_headers(client)
    identity = str(uuid.uuid4())
    first = queue(client, headers, identity=identity)
    assert first.status_code == 202
    assert queue(client, headers, identity=identity).get_json()['job']['id'] == identity
    changed = queue(client, headers, month='2026-09', identity=identity)
    assert changed.status_code == 409
    assert changed.get_json()['error'] == 'history_job_id_conflict'
    busy = queue(client, headers, month='2026-09')
    assert busy.status_code == 409 and busy.get_json()['error'] == 'history_busy'
    current = datetime.now(SHANGHAI).strftime('%Y-%m')
    assert queue(client, headers, month=current).status_code == 400
    app.extensions['service']['order_history_jobs'].process_one()
    covered = queue(client, headers)
    assert covered.status_code == 409
    assert covered.get_json()['error'] == 'history_month_complete'
    assert queue(client, headers, rescan=True).status_code == 202


def test_failed_month_keeps_previous_archive_and_exposes_review(configured):
    app, erp = configured
    client = app.test_client()
    headers = admin_headers(client)
    worker = app.extensions['service']['order_history_jobs']
    assert queue(client, headers).status_code == 202
    assert worker.process_one() is True
    baseline = app.extensions['service']['order_history_archive'].coverage('79')
    erp.fail_month = '2026-09'
    failed = queue(client, headers, month='2026-09')
    assert failed.status_code == 202
    assert worker.process_one() is True
    result = worker.get(failed.get_json()['job']['id'])
    assert result['status'] == 'blocked'
    assert result['result']['archive_unchanged_for_failed_month'] is True
    assert app.extensions['service']['order_history_archive'].coverage('79') == baseline
    assert client.get('/api/v2/order-history/lines?profile_id=79',
                      headers=headers).get_json()['total'] == 1


def test_interrupted_read_job_requeues_without_any_erp_write(configured):
    app, erp = configured
    client = app.test_client()
    headers = admin_headers(client)
    response = queue(client, headers)
    identity = response.get_json()['job']['id']
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        db.execute("UPDATE order_history_jobs SET status='running' WHERE id=?", (identity,))
    worker = app.extensions['service']['order_history_jobs']
    worker.recover()
    assert worker.get(identity)['stage'] == 'recovered'
    assert worker.process_one() is True
    assert worker.get(identity)['status'] == 'succeeded'
    assert len(erp.calls) == 2


def test_authorized_profile_mismatch_blocks_before_source_read(configured):
    app, erp = configured
    client = app.test_client()
    headers = admin_headers(client)
    response = client.post('/api/v2/order-history/jobs', headers=headers,
                           json={'id': str(uuid.uuid4()), 'profile_id': '80',
                                 'month': '2026-08'})
    assert response.status_code == 202
    worker = app.extensions['service']['order_history_jobs']
    assert worker.process_one() is True
    job = worker.get(response.get_json()['job']['id'])
    assert job['status'] == 'blocked'
    assert job['result']['message'] == 'authorized_profile_mismatch'
    assert erp.calls == []


def test_history_and_main_update_workers_do_not_compete_for_erp(configured):
    app, erp = configured
    client = app.test_client()
    headers = admin_headers(client)
    services = app.extensions['service']
    store = services['store']
    updates = services['updates']
    history = services['order_history_jobs']

    main_id = str(uuid.uuid4())
    updates.enqueue(main_id, 'refresh')
    denied = queue(client, headers)
    assert denied.status_code == 409
    assert denied.get_json()['error'] == 'update_busy'
    with store.connect(write=True) as db:
        db.execute("UPDATE update_jobs SET status='succeeded' WHERE id=?", (main_id,))

    response = queue(client, headers)
    assert response.status_code == 202
    history_id = response.get_json()['job']['id']
    with store.connect(write=True) as db:
        db.execute("UPDATE order_history_jobs SET status='running' WHERE id=?", (history_id,))
    second_main_id = str(uuid.uuid4())
    updates.enqueue(second_main_id, 'refresh')
    assert updates.process_one() is False
    assert updates.get(second_main_id)['status'] == 'queued'
    with store.connect(write=True) as db:
        db.execute("UPDATE order_history_jobs SET status='queued' WHERE id=?", (history_id,))
    assert history.process_one() is False  # Main update has priority once queued.
    assert history.get(history_id)['status'] == 'queued'
    assert erp.calls == []
