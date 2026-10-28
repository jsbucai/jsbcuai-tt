import tempfile
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app
from core.auth import Sessions
from core.store import Store, dumps


@pytest.fixture
def public_app():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / 'runtime', prefix='staff-auth-') as runtime:
        yield create_app({
            'TESTING': True,
            'AUTO_IMPORT': False,
            'SOURCE_MODE': 'api',
            'PUBLIC_MODE': True,
            'ADMIN_PASSWORD_HASH': generate_password_hash('administrator-password'),
            'ERP_CLIENT': SimpleNamespace(last_auth_error=None),
            'API_SOURCE': object(),
        }, runtime_dir=runtime)


def admin_session(client):
    response = client.post('/api/v2/auth/login', json={'password': 'administrator-password'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.get_json()['token']}


def create_worker(client, headers, **changes):
    body = {
        'username': 'buyer.one',
        'display_name': '采购一号',
        'password': 'worker-password',
        'roles': ['purchase'],
    }
    body.update(changes)
    return client.post('/api/v2/staff', json=body, headers=headers)


def seed_public_packaging_order(app, print_state='uncertain'):
    store = app.extensions['service']['store']
    summary = 'PO#PRJ#1#DRAW#品名#M8#8.8#锌#1#LS0001#交期=2026-10-05'
    item = {
        'id': 'S1:1', 'profile_id': 'P', 'order_no': 'S1', 'line_id': '1',
        'sku': 'LS0001', 'product_id': 'LS0001-p', 'sku_id': 'LS0001-s',
        'warehouse_id': '08390', 'base_unit': '个', 'product_name': '测试商品',
        'shipping_id': '@10/05#S1', 'summary': summary, 'system_note': summary,
        'quantity': '1', 'buyer': '客户甲', 'customer_source': 'wms_receiveraddress',
        'document_date': '2026-10-01', 'logistics_company': 'LS库存件',
        'calculation_status': 'ready', 'process_state': '',
    }
    calculation = {'plans': [{'tradeid': 'S1', 'freightname': 'LS库存件',
                              'freightnumber': '@10/05#S1'}],
                   'allocations': [], 'blocked_orders': []}
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps('v1'),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_source_updated_at',?)",
                   (dumps('2026-10-04T08:00:00+08:00'),))
        db.execute("INSERT INTO records(kind,id,payload,sku) VALUES ('orders',?,?,?)",
                   (item['id'], dumps(item), item['sku']))
        db.execute("INSERT INTO logistics_calculations VALUES (?,?,?,?,?)",
                   ('job-v1', 'P', 'v1', dumps(calculation),
                    '2026-10-04T08:00:00+08:00'))
        db.execute("""INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES ('P','S1',?,'phone','job','hash','unknown','2026-10-04T09:00:00+08:00')""",
                   (print_state,))
    app.extensions['service']['daily_work'].refresh(actor_id='test')
    return store


def test_legacy_sessions_are_bound_to_bootstrapped_admin():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / 'runtime', prefix='staff-schema-') as runtime:
        store = Store(runtime)
        with store.connect(write=True) as db:
            db.execute('CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL)')
            legacy_token = 'legacy-opaque-session'
            db.execute('INSERT INTO auth_sessions VALUES (?,?)',
                       (Sessions.digest(legacy_token), time.time() + 60))
        sessions = Sessions(store, generate_password_hash('old-password'))
        with store.connect() as db:
            columns = {row['name'] for row in db.execute('PRAGMA table_info(auth_sessions)')}
            admin = db.execute("SELECT * FROM staff_users WHERE id='admin'").fetchone()
        assert 'user_id' in columns
        assert admin['username'] == 'admin'
        assert sessions.current(legacy_token)['id'] == 'admin'
        token, error = sessions.login('old-password', '127.0.0.1')
        assert error is None
        current = sessions.current(token)
        assert current['id'] == 'admin' and current['is_admin'] is True
        assert current['roles'] == []
        assert 'password_hash' not in current


def test_username_login_me_and_admin_only_staff_management(public_app):
    client = public_app.test_client()
    headers = admin_session(client)
    reconciled = []
    public_app.extensions['service']['sessions'].on_staff_changed = (
        lambda actor_id: reconciled.append(actor_id) or {'assigned': 0})
    created = create_worker(client, headers)
    assert created.status_code == 201
    assert created.get_json()['task_reconcile']['ok'] is True
    assert reconciled == ['admin']
    worker = created.get_json()['staff']
    assert worker['roles'] == ['purchase'] and worker['packing_limit'] == 3
    assert 'password_hash' not in worker

    repeated = create_worker(client, headers)
    assert repeated.status_code == 200
    assert repeated.get_json()['created'] is False
    assert repeated.get_json()['staff']['id'] == worker['id']
    conflict = create_worker(client, headers, display_name='另一个名字')
    assert conflict.status_code == 409 and conflict.get_json()['error'] == 'username_exists'

    login = client.post('/api/v2/auth/login', json={
        'username': 'BUYER.ONE', 'password': 'worker-password'})
    assert login.status_code == 200
    worker_headers = {'Authorization': 'Bearer ' + login.get_json()['token']}
    me = client.get('/api/v2/me', headers=worker_headers).get_json()['user']
    assert me['id'] == worker['id'] and me['display_name'] == '采购一号'
    assert client.get('/api/v2/staff', headers=worker_headers).status_code == 403
    assert create_worker(client, worker_headers, username='forbidden').status_code == 403

    listed = client.get('/api/v2/staff', headers=headers).get_json()['staff']
    assert {item['username'] for item in listed} == {'admin', 'buyer.one'}
    assert all('password_hash' not in item for item in listed)


def test_print_reset_route_repairs_historical_review_and_reports_task_refresh(public_app, monkeypatch):
    client = public_app.test_client()
    headers = admin_session(client)
    store = seed_public_packaging_order(public_app)
    engine = public_app.extensions['service']['daily_work']
    original_guard = engine.assert_print_reset_access
    guard_transactions = []

    def checked_guard(*args, **kwargs):
        guard_transactions.append(bool(kwargs.get('db') and kwargs['db'].in_transaction))
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(engine, 'assert_print_reset_access', checked_guard)
    before = client.get('/api/v2/daily-work?view=all', headers=headers).get_json()
    assert any(item['task_type'] == 'print_history_review' for item in before['items'])

    response = client.post('/api/v2/orders/S1/print-state/reset',
                           json={'profile_id': 'P', 'device_id': 'phone'}, headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body['print_state'] == 'unprinted'
    assert body['task_refresh']['ok'] is True
    assert body['task_refresh']['status'] == 'succeeded'
    assert guard_transactions == [True]
    after = client.get('/api/v2/daily-work?view=all', headers=headers).get_json()
    assert any(item['task_type'] == 'whole_order_pack' and item['order_no'] == 'S1'
               and item['status'] == 'todo' for item in after['items'])
    assert not any(item['task_type'] == 'print_history_review' for item in after['items'])
    with store.connect() as db:
        review = db.execute("""SELECT condition_state FROM daily_work_tasks
            WHERE task_type='print_history_review' AND order_no='S1'""").fetchone()
        assert review['condition_state'] == 'resolved'


def test_print_reset_remains_committed_when_task_refresh_fails(public_app, monkeypatch):
    client = public_app.test_client()
    headers = admin_session(client)
    store = seed_public_packaging_order(public_app, print_state='printed')
    engine = public_app.extensions['service']['daily_work']

    def fail_refresh(**_kwargs):
        raise RuntimeError('simulated task refresh failure')

    monkeypatch.setattr(engine, 'refresh', fail_refresh)
    response = client.post('/api/v2/orders/S1/print-state/reset',
                           json={'profile_id': 'P', 'device_id': 'phone'}, headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body['print_state'] == 'unprinted'
    assert body['task_refresh'] == {
        'ok': False,
        'status': 'failed',
        'error': 'task_refresh_failed',
        'message': '打印状态已重置；每日任务刷新失败，请稍后重新更新任务',
    }
    with store.connect() as db:
        state = db.execute("""SELECT state FROM order_print_states
            WHERE profile_id='P' AND order_no='S1'""").fetchone()
    assert state['state'] == 'unprinted'


def test_assigned_worker_can_reset_historical_print_review(public_app):
    client = public_app.test_client()
    admin_headers = admin_session(client)
    worker = create_worker(client, admin_headers, username='packer.one',
                           display_name='包装一号', roles=['stock_packing']).get_json()['staff']
    seed_public_packaging_order(public_app)
    task = next(item for item in client.get('/api/v2/daily-work?view=all',
                                             headers=admin_headers).get_json()['items']
                if item['task_type'] == 'print_history_review')
    transferred = client.post('/api/v2/daily-work/tasks/' + task['id'] + '/transfer', json={
        'assignee_id': worker['id'], 'idempotency_key': 'assign-history-review-1',
        'version': task['version'], 'reason': '交由现场包装人员核对'}, headers=admin_headers)
    assert transferred.status_code == 200
    token = client.post('/api/v2/auth/login', json={
        'username': 'packer.one', 'password': 'worker-password'}).get_json()['token']
    worker_headers = {'Authorization': 'Bearer ' + token}
    reset = client.post('/api/v2/orders/S1/print-state/reset', json={
        'profile_id': 'P', 'device_id': 'phone'}, headers=worker_headers)
    assert reset.status_code == 200
    assert reset.get_json()['print_state'] == 'unprinted'
    assert reset.get_json()['task_refresh']['ok'] is True


def test_staff_validation_patch_deactivation_and_password_reset(public_app):
    client = public_app.test_client()
    headers = admin_session(client)
    worker = create_worker(client, headers).get_json()['staff']
    worker_login = client.post('/api/v2/auth/login', json={
        'username': 'buyer.one', 'password': 'worker-password'}).get_json()['token']

    invalid = create_worker(client, headers, username='bad name', roles=['unknown'])
    assert invalid.status_code == 400
    updated = client.patch('/api/v2/staff/' + worker['id'], json={
        'display_name': '采购负责人',
        'roles': ['purchase', 'arrival_packing'],
        'packing_limit': 5,
    }, headers=headers)
    assert updated.status_code == 200
    assert updated.get_json()['staff']['roles'] == ['purchase', 'arrival_packing']
    assert updated.get_json()['staff']['packing_limit'] == 5

    reset = client.post('/api/v2/staff/' + worker['id'] + '/password',
                        json={'password': 'replacement-password'}, headers=headers)
    assert reset.status_code == 200 and reset.get_json()['sessions_revoked'] is True
    old_headers = {'Authorization': 'Bearer ' + worker_login}
    assert client.get('/api/v2/me', headers=old_headers).status_code == 401
    assert client.post('/api/v2/auth/login', json={
        'username': 'buyer.one', 'password': 'worker-password'}).status_code == 401
    assert client.post('/api/v2/auth/login', json={
        'username': 'buyer.one', 'password': 'replacement-password'}).status_code == 200

    deactivated = client.patch('/api/v2/staff/' + worker['id'],
                               json={'active': False}, headers=headers)
    assert deactivated.status_code == 200 and deactivated.get_json()['staff']['active'] is False
    assert client.post('/api/v2/auth/login', json={
        'username': 'buyer.one', 'password': 'replacement-password'}).status_code == 401
    last_admin = client.patch('/api/v2/staff/admin', json={'active': False}, headers=headers)
    assert last_admin.status_code == 409 and last_admin.get_json()['error'] == 'last_admin_required'


def test_login_throttle_is_scoped_per_account_behind_shared_proxy(public_app):
    client = public_app.test_client()
    headers = admin_session(client)
    assert create_worker(client, headers).status_code == 201
    for _ in range(5):
        assert client.post('/api/v2/auth/login', json={
            'username': 'admin', 'password': 'wrong-password'}).status_code == 401
    assert client.post('/api/v2/auth/login', json={
        'username': 'admin', 'password': 'administrator-password'}).status_code == 429
    worker = client.post('/api/v2/auth/login', json={
        'username': 'buyer.one', 'password': 'worker-password'})
    assert worker.status_code == 200


def test_login_throttle_bounds_fake_accounts_from_one_proxy(public_app):
    client = public_app.test_client()
    headers = admin_session(client)
    assert create_worker(client, headers).status_code == 201
    for index in range(50):
        response = client.post('/api/v2/auth/login', json={
            'username': f'missing-{index}', 'password': 'wrong-password'})
        assert response.status_code == 401
    blocked = client.post('/api/v2/auth/login', json={
        'username': 'buyer.one', 'password': 'worker-password'})
    assert blocked.status_code == 429
    store = public_app.extensions['service']['store']
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM auth_attempts').fetchone()[0] <= 51
