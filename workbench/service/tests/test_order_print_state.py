"""Structured order filters and transactional whole-order printing."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app
from core.print_jobs import JobConflict
from core.store import dumps


IDENTITY = {'profile_id': 'profile-a', 'warehouse_id': '29871293290508390',
            'product_id': 'product-a', 'base_unit': '1'}


@pytest.fixture
def app():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='order-print-', dir=ROOT / 'runtime') as runtime:
        app = create_app({'TESTING': True, 'AUTO_IMPORT': False}, runtime_dir=runtime)
        store = app.extensions['service']['store']
        orders = [
            order('line-a1', 'ORDER-A', 'SKU-A', 'sku-a', '2026-09-01', '2026-10-01', '查找摘要'),
            order('line-a2', 'ORDER-A', 'SKU-B', 'sku-b', '2026-09-01', '2026-10-01', '查找摘要'),
            order('line-b1', 'ORDER-B', 'SKU-C', 'sku-c', '2026-09-02', '2026-10-02', '另一摘要'),
            order('line-c1', 'ORDER-C', 'SKU-D', 'sku-d', '', '', '无日期摘要'),
        ]
        inventory = [
            stock('stock-a', 'SKU-A', 'sku-a', 'A2'),
            stock('stock-b', 'SKU-B', 'sku-b', 'A2'),
            stock('stock-c', 'SKU-C', 'sku-c', 'A10'),
            stock('stock-d', 'SKU-D', 'sku-d', ''),
        ]
        with store.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps('orders-v1'),))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('logistics_available','true')")
            for kind, values in (('orders', orders), ('inventory', inventory)):
                for item in values:
                    db.execute('''INSERT INTO records
                        (kind,id,payload,sku,buyer,note,keywords,record_date,quantity,amount,logistics)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                        (kind, item['id'], dumps(item), item['sku'], item.get('buyer', ''),
                         item.get('system_note', ''), item['sku'].lower(), item.get('document_date', ''),
                         1, 0, item.get('logistics_company', '')))
        yield app


def order(identity, number, sku, sku_id, ordered, delivery, marker):
    suffix = f'#交期={delivery}' if delivery else ''
    return {**IDENTITY, 'sku_id': sku_id, 'id': identity, 'line_id': identity,
            'order_no': number, 'sku': sku, 'product_name': '螺栓', 'quantity': '1',
            'buyer': '客户甲' if number != 'ORDER-B' else '客户乙',
            'customer_name': '客户甲' if number != 'ORDER-B' else '客户乙',
            'customer_source': 'wms_receiveraddress',
            'shipping_id': f'@10/01#{number}', 'logistics_company': 'LS库存件',
            'document_date': ordered,
            'system_note': f'{number}#P#1#D#{marker}#M10#8.8#锌#1#{sku}{suffix}'}


def stock(identity, sku, sku_id, location):
    return {**IDENTITY, 'sku_id': sku_id, 'id': identity, 'sku': sku,
            'product_name': '螺栓', 'stock_quantity': '10', 'available_stock': '5',
            'location': location}


def put_lifecycle(store, state, process_state, last_error=''):
    """Insert lifecycle evidence across the additive schema's old/new shape."""
    with store.connect(write=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS order_lifecycle_state(
            profile_id TEXT NOT NULL, order_no TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL, process_state TEXT NOT NULL DEFAULT '',
            confirmation_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(profile_id,order_no))''')
        columns = db.execute('PRAGMA table_info(order_lifecycle_state)').fetchall()
        values = {'profile_id': 'profile-a', 'order_no': 'ORDER-B',
                  'lifecycle_state': state, 'process_state': process_state,
                  'confirmation_count': 1, 'last_error': last_error,
                  'last_business_snapshot': '{}', 'content_hash': 'test',
                  'first_seen_500_at': '2026-10-01T12:00:00',
                  'last_confirmed_at': '2026-10-01T12:00:00',
                  'last_observed_at': '2026-10-01T12:00:00',
                  'ended_at': '', 'updated_at': '2026-10-01T12:00:00'}
        required = []
        for column in columns:
            name = column['name']
            if name in values:
                required.append(name)
            elif column['notnull'] and column['dflt_value'] is None:
                values[name] = 0 if 'INT' in str(column['type'] or '').upper() else ''
                required.append(name)
        marks = ','.join('?' for _ in required)
        db.execute(f'''INSERT OR REPLACE INTO order_lifecycle_state
            ({','.join(required)}) VALUES ({marks})''', [values[name] for name in required])


def test_summary_alias_empty_structured_query_dates_and_natural_location_sort(app):
    client = app.test_client()
    by_order = client.get('/api/v2/orders', query_string={
        'mode': 'order_no', 'q': 'ORDER-A'}).get_json()
    assert by_order['mode'] == 'order_no'
    assert {item['order_no'] for item in by_order['items']} == {'ORDER-A'}
    summary = client.get('/api/v2/orders', query_string={
        'mode': 'summary', 'q': '查找摘要', 'sort': 'location'}).get_json()
    assert summary['mode'] == 'summary'
    assert [item['id'] for item in summary['items']] == ['line-a1', 'line-a2']
    assert all(item['summary'] == item['system_note'] for item in summary['items'])

    result = client.get('/api/v2/orders', query_string={
        'print_state': 'all', 'order_date_from': '2026-09-01', 'order_date_to': '2026-09-02',
        'delivery_date_from': '2026-10-01', 'delivery_date_to': '2026-10-02',
        'sort': 'location'}).get_json()
    assert [item['id'] for item in result['items']] == ['line-a1', 'line-a2', 'line-b1']
    assert [item['location'] for item in result['items']] == ['A2', 'A2', 'A10']
    assert result['print_state_counts'] == {
        'all': 2, 'unprinted': 2, 'reserved': 0, 'printed': 0, 'uncertain': 0}
    assert result['customer_options'] == ['客户乙', '客户甲']
    assert result['customer_count'] == 2
    assert result['items'][0]['order_date'] == '2026-09-01'
    assert result['items'][0]['delivery_date'] == '2026-10-01'
    assert client.get('/api/v2/orders?sort=location').get_json()['count'] == 0
    assert client.get('/api/v2/orders?print_state=bad').status_code == 400
    assert client.get('/api/v2/orders?print_state=all&order_date_from=2026/09/01').status_code == 400

    customer = client.get('/api/v2/orders', query_string={
        'print_state': 'all', 'customer': '客户甲'}).get_json()
    assert {item['order_no'] for item in customer['items']} == {'ORDER-A', 'ORDER-C'}
    assert customer['customer'] == '客户甲'
    assert customer['customer_options'] == ['客户乙', '客户甲']


def test_legacy_buyer_is_not_exposed_as_customer(app):
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='orders' AND id='line-b1'").fetchone()
        payload = json.loads(row[0])
        payload.pop('customer_source')
        payload.pop('customer_name')
        db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id='line-b1'", (dumps(payload),))
    result = app.test_client().get('/api/v2/orders?print_state=all').get_json()
    assert result['customer_options'] == ['客户甲']
    legacy = next(item for item in result['items'] if item['id'] == 'line-b1')
    assert legacy['buyer'] == ''
    assert app.test_client().get('/api/v2/orders', query_string={
        'print_state': 'all', 'customer': '客户乙'}).get_json()['count'] == 0


def test_non_unfulfilled_lifecycle_order_exits_all_current_views(app):
    client = app.test_client()
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='orders' AND id='line-b1'").fetchone()
        item = json.loads(row['payload'])
        item.update(printable=False, print_blocked=True,
                    print_block_reason='已出库未结存订单不参与包装打印',
                    lifecycle_state='candidate', process_state='500',
                    lifecycle_print_blocked=True, shadow_demand=True,
                    logistics_company='已完成订单需结存')
        db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id='line-b1'",
                   (dumps(item),))

    all_rows = client.get('/api/v2/orders?print_state=all').get_json()
    assert {item['order_no'] for item in all_rows['items']} == {'ORDER-A', 'ORDER-C'}
    assert all_rows['print_state_counts'] == {
        'all': 2, 'unprinted': 2, 'reserved': 0, 'printed': 0, 'uncertain': 0}
    unprinted = client.get('/api/v2/orders?print_state=unprinted').get_json()
    assert {item['order_no'] for item in unprinted['items']} == {'ORDER-A', 'ORDER-C'}
    assert client.get('/api/v2/orders?print_state=reserved').get_json()['count'] == 0
    assert client.get('/api/v2/orders?print_state=uncertain').get_json()['count'] == 0

    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'all': True, 'expected_data_version': 'orders-v1',
        'device_id': 'phone-a', 'query': {'print_state': 'unprinted'}}).get_json()
    assert {order['order_no'] for order in prepared['orders']} == {'ORDER-A', 'ORDER-C'}


def test_print_queue_eligibility_uses_every_line_of_the_whole_order(app):
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='orders' AND id='line-a2'").fetchone()
        item = json.loads(row['payload'])
        item.update(printable=False, print_blocked=True,
                    print_block_reason='同一订单另一行缺少打印字段')
        db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id='line-a2'",
                   (dumps(item),))
    client = app.test_client()
    all_rows = client.get('/api/v2/orders?q=SKU-A&print_state=all').get_json()
    assert all_rows['count'] == 1 and all_rows['items'][0]['id'] == 'line-a1'
    assert all_rows['items'][0]['print_queue_eligible'] is False
    assert all_rows['print_state_counts']['all'] == 0
    assert client.get('/api/v2/orders?q=SKU-A&print_state=unprinted').get_json()['count'] == 0


def test_printed_history_survives_later_lifecycle_print_block(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-C').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    path = '/api/v2/print/jobs/' + prepared['job_id']
    claimed = client.post(path + '/claim', json={
        'user_initiated': True, 'idempotency_key': 'printed-before-lifecycle',
        'transport': 'bluetooth', 'device_id': 'phone-a'}).get_json()
    assert client.post(path + '/result', json={
        'claim_token': claimed['claim_token'], 'status': 'submitted', 'bytes_sent': 100,
        'message': '完成', 'device_id': 'phone-a'}).status_code == 200

    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='orders' AND id='line-b1'").fetchone()
        item = json.loads(row['payload'])
        item.update(printable=False, print_blocked=True, lifecycle_state='shipped_unposted',
                    process_state='500', lifecycle_print_blocked=True, shadow_demand=True,
                    status='已出库未结存', logistics_company='已完成订单需结存')
        db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id='line-b1'",
                   (dumps(item),))

    all_rows = client.get('/api/v2/orders?print_state=all').get_json()
    assert 'ORDER-B' not in {item['order_no'] for item in all_rows['items']}
    assert all_rows['print_state_counts'] == {
        'all': 2, 'unprinted': 2, 'reserved': 0, 'printed': 0, 'uncertain': 0}
    assert client.get('/api/v2/orders?print_state=printed').get_json()['items']==[]
    with store.connect() as db:
        assert db.execute("SELECT state FROM order_print_states WHERE order_no='ORDER-B'").fetchone()[0]=='printed'
        assert db.execute("SELECT count(*) FROM order_print_audit WHERE order_no='ORDER-B'").fetchone()[0]>0


def test_whole_order_reservation_device_conflict_result_reset_and_invalidation(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    body = {'kind': 'order', 'item_ids': [selected['id']],
            'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}
    prepared = client.post('/api/v2/print/jobs', json=body)
    assert prepared.status_code == 200
    prepared = prepared.get_json()
    assert prepared['labels'] == 2 and prepared['order_count'] == 1
    assert prepared['orders'][0]['order_no'] == 'ORDER-A'
    assert [page['fields']['sku'] for page in prepared['pages']] == ['SKU-A', 'SKU-B']

    reserved = client.get('/api/v2/orders?print_state=reserved').get_json()
    assert reserved['count'] == 2 and reserved['print_state_counts']['reserved'] == 1
    conflict = client.post('/api/v2/print/jobs', json={**body, 'device_id': 'phone-b'})
    assert conflict.status_code == 409 and conflict.get_json()['error'] == 'order_print_conflict'

    path = '/api/v2/print/jobs/' + prepared['job_id']
    operation = {'user_initiated': True, 'idempotency_key': 'phone-a-operation-0001',
                 'transport': 'bluetooth'}
    assert client.post(path + '/claim', json=operation).status_code == 409
    assert client.post(path + '/claim', json={**operation, 'device_id': 'phone-b'}).status_code == 409
    claimed = client.post(path + '/claim', json={**operation, 'device_id': 'phone-a'}).get_json()
    report = {'claim_token': claimed['claim_token'], 'status': 'unknown',
              'message': '连接中断', 'bytes_sent': 100, 'device_id': 'phone-a'}
    assert client.post(path + '/result', json=report).get_json()['printed'] is False
    uncertain = client.get('/api/v2/orders?print_state=uncertain').get_json()
    assert uncertain['count'] == 2 and uncertain['print_state_counts']['uncertain'] == 1

    reset = client.post('/api/v2/orders/ORDER-A/print-state/reset', json={
        'profile_id': 'profile-a', 'device_id': 'phone-a'}).get_json()
    assert reset['print_state'] == 'unprinted' and reset['replayed'] is False
    replay = client.post('/api/v2/orders/ORDER-A/print-state/reset', json={
        'profile_id': 'profile-a'}).get_json()
    assert replay['replayed'] is True

    second = client.post('/api/v2/print/jobs', json=body).get_json()
    claimed = client.post('/api/v2/print/jobs/' + second['job_id'] + '/claim', json={
        **operation, 'idempotency_key': 'phone-a-operation-0002', 'device_id': 'phone-a'}).get_json()
    done = client.post('/api/v2/print/jobs/' + second['job_id'] + '/result', json={
        'claim_token': claimed['claim_token'], 'status': 'submitted', 'bytes_sent': 200,
        'message': '完成', 'device_id': 'phone-a'}).get_json()
    assert done['printed'] is True
    assert client.get('/api/v2/orders?print_state=printed').get_json()['count'] == 2

    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='orders' AND id='line-a1'").fetchone()
        item = json.loads(row[0])
        item['product_name'] = '螺栓（新）'
        db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id='line-a1'", (dumps(item),))
    changed = client.get('/api/v2/orders?print_state=all&q=SKU-A').get_json()['items'][0]
    assert changed['print_state'] == 'unprinted'
    assert changed['print_content_changed'] is True
    assert changed['print_state_reason'] == 'content_changed'
    with store.connect() as db:
        transitions = [(row['from_state'], row['to_state'], row['reason']) for row in db.execute(
            "SELECT from_state,to_state,reason FROM order_print_audit WHERE order_no='ORDER-A' ORDER BY created_at,rowid")]
    assert ('reserved', 'uncertain', 'transport_unknown') in transitions
    assert ('uncertain', 'unprinted', 'manual_reset') in transitions
    assert ('printed', 'unprinted', 'content_changed') in transitions


def test_print_all_uses_full_structured_filter_scope(app):
    client = app.test_client()
    payload = {'kind': 'order', 'all': True, 'device_id': 'phone-a',
               'expected_data_version': 'orders-v1', 'query': {
                   'q': '', 'print_state': 'unprinted',
                   'delivery_date_from': '2026-10-02', 'delivery_date_to': '2026-10-02',
                   'sort': 'delivery_date'}}
    prepared = client.post('/api/v2/print/jobs', json=payload).get_json()
    assert prepared['order_count'] == 1 and prepared['orders'][0]['order_no'] == 'ORDER-B'
    assert prepared['labels'] == 1 and prepared['pages'][0]['fields']['sku'] == 'SKU-C'


def test_two_devices_cannot_reserve_the_same_order_concurrently(app):
    jobs = app.extensions['service']['jobs']
    selected = app.test_client().get('/api/v2/orders?q=SKU-A').get_json()['items'][0]

    def prepare(device):
        try:
            return jobs.prepare({'kind': 'order', 'item_ids': [selected['id']],
                                 'expected_data_version': 'orders-v1', 'device_id': device})
        except JobConflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, ('phone-a', 'phone-b')))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count('order_print_conflict') == 1


def test_prepare_idempotency_recovers_lost_response_and_rejects_scope_change(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    payload = {'kind': 'order', 'item_ids': [selected['id']],
               'expected_data_version': 'orders-v1', 'device_id': 'phone-a',
               'prepare_idempotency_key': 'prepare-response-lost-001'}
    first = client.post('/api/v2/print/jobs', json=payload).get_json()
    replay = client.post('/api/v2/print/jobs', json=payload).get_json()
    assert replay['replayed'] is True
    assert replay['job_id'] == first['job_id']
    assert replay['labels'] == first['labels']
    assert replay['pages'] == first['pages']
    changed = client.post('/api/v2/print/jobs', json={
        **payload, 'device_id': 'phone-b'})
    assert changed.status_code == 409
    assert changed.get_json()['error'] == 'print_prepare_idempotency_conflict'
    claimed = client.post('/api/v2/print/jobs/' + first['job_id'] + '/claim', json={
        'user_initiated': True, 'idempotency_key': 'claim-after-prepare-replay',
        'transport': 'bluetooth', 'device_id': 'phone-a'})
    assert claimed.status_code == 200
    terminal_replay = client.post('/api/v2/print/jobs', json=payload)
    assert terminal_replay.status_code == 409
    assert terminal_replay.get_json()['error'] == 'print_prepare_conflict'


def test_concurrent_same_prepare_key_creates_one_job_and_one_reservation(app):
    jobs = app.extensions['service']['jobs']
    selected = app.test_client().get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    payload = {'kind': 'order', 'item_ids': [selected['id']],
               'expected_data_version': 'orders-v1', 'device_id': 'phone-a',
               'prepare_idempotency_key': 'concurrent-prepare-key-01'}

    def prepare(_index):
        return jobs.prepare(payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, range(2)))
    assert len({result['job_id'] for result in results}) == 1
    assert sorted(result['replayed'] for result in results) == [False, True]
    store = app.extensions['service']['store']
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM print_jobs").fetchone()[0] == 1
        reserves = db.execute("""SELECT count(*) FROM order_print_audit
            WHERE order_no='ORDER-A' AND to_state='reserved'""").fetchone()[0]
    assert reserves == 1


def test_prepared_job_cancel_is_atomic_idempotent_and_releases_reservation(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    path = '/api/v2/print/jobs/' + prepared['job_id'] + '/cancel'
    body = {'device_id': 'phone-a', 'idempotency_key': 'cancel-before-claim-0001',
            'reason': '用户在蓝牙发送前取消'}
    cancelled = client.post(path, json=body)
    assert cancelled.status_code == 200
    assert cancelled.get_json()['status'] == 'cancelled'
    assert cancelled.get_json()['bytes_sent'] == 0
    assert cancelled.get_json()['replayed'] is False
    replay = client.post(path, json=body).get_json()
    assert replay['status'] == 'cancelled' and replay['replayed'] is True
    conflict = client.post(path, json={**body,
        'idempotency_key': 'cancel-before-claim-0002'})
    assert conflict.status_code == 409
    assert conflict.get_json()['error'] == 'print_cancel_conflict'
    assert client.get('/api/v2/orders?q=SKU-A&print_state=unprinted').get_json()['count'] == 1
    store = app.extensions['service']['store']
    with store.connect() as db:
        audits = db.execute("""SELECT from_state,to_state,reason FROM order_print_audit
            WHERE order_no='ORDER-A' ORDER BY rowid""").fetchall()
    assert [(row['from_state'], row['to_state'], row['reason']) for row in audits] == [
        ('unprinted', 'reserved', 'prepare'), ('reserved', 'unprinted', 'cancelled')]


def test_cancel_rejects_wrong_device_and_claimed_job_without_releasing(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    cancel_path = '/api/v2/print/jobs/' + prepared['job_id'] + '/cancel'
    cancel = {'device_id': 'phone-b', 'idempotency_key': 'cancel-wrong-device-01',
              'reason': '错误设备取消'}
    wrong = client.post(cancel_path, json=cancel)
    assert wrong.status_code == 409 and wrong.get_json()['error'] == 'order_print_conflict'
    claim_path = '/api/v2/print/jobs/' + prepared['job_id'] + '/claim'
    claimed = client.post(claim_path, json={
        'user_initiated': True, 'idempotency_key': 'claim-before-cancel-01',
        'transport': 'bluetooth', 'device_id': 'phone-a'}).get_json()
    after_claim = client.post(cancel_path, json={**cancel, 'device_id': 'phone-a',
        'idempotency_key': 'cancel-after-claim-001'})
    assert after_claim.status_code == 409
    assert after_claim.get_json()['error'] == 'print_already_claimed'
    assert client.get('/api/v2/orders?q=SKU-A&print_state=reserved').get_json()['count'] == 1
    result = client.post('/api/v2/print/jobs/' + prepared['job_id'] + '/result', json={
        'claim_token': claimed['claim_token'], 'status': 'cancelled', 'bytes_sent': 0,
        'message': '领取后确认未发送', 'device_id': 'phone-a'})
    assert result.status_code == 200
    assert client.get('/api/v2/orders?q=SKU-A&print_state=unprinted').get_json()['count'] == 1


def test_concurrent_prepared_cancel_replays_once_and_audits_one_release(app):
    jobs = app.extensions['service']['jobs']
    selected = app.test_client().get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    prepared = jobs.prepare({'kind': 'order', 'item_ids': [selected['id']],
                             'expected_data_version': 'orders-v1', 'device_id': 'phone-a'})
    payload = {'device_id': 'phone-a', 'idempotency_key': 'concurrent-cancel-0001',
               'reason': '并发取消恢复'}

    def cancel(_index):
        return jobs.cancel(prepared['job_id'], payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(cancel, range(2)))
    assert sorted(result['replayed'] for result in results) == [False, True]
    store = app.extensions['service']['store']
    with store.connect() as db:
        releases = db.execute("""SELECT count(*) FROM order_print_audit
            WHERE order_no='ORDER-A' AND from_state='reserved' AND to_state='unprinted'""").fetchone()[0]
    assert releases == 1


def test_lost_claim_response_recovers_to_uncertain_with_original_key(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    claim_key = 'claim-response-lost-001'
    claim = client.post('/api/v2/print/jobs/' + prepared['job_id'] + '/claim', json={
        'user_initiated': True, 'idempotency_key': claim_key,
        'transport': 'bluetooth', 'device_id': 'phone-a'})
    assert claim.status_code == 200
    # Simulate a response lost before the claim token reached durable client state.
    recover_path = '/api/v2/print/jobs/' + prepared['job_id'] + '/recover'
    recovery = {'device_id': 'phone-a', 'idempotency_key': claim_key,
                'reason': '领取响应丢失，无法确认是否进入发送步骤'}
    recovered = client.post(recover_path, json=recovery)
    assert recovered.status_code == 200
    assert recovered.get_json()['status'] == 'unknown'
    assert recovered.get_json()['replayed'] is False
    assert client.get('/api/v2/orders?q=SKU-A&print_state=uncertain').get_json()['count'] == 1
    replay = client.post(recover_path, json=recovery).get_json()
    assert replay['status'] == 'unknown' and replay['replayed'] is True
    cancel = client.post('/api/v2/print/jobs/' + prepared['job_id'] + '/cancel', json={
        'device_id': 'phone-a', 'idempotency_key': 'cancel-after-recovery-1',
        'reason': '不得降级'} )
    assert cancel.status_code == 409 and cancel.get_json()['error'] == 'print_cancel_conflict'


def test_claim_recovery_rejects_prepared_wrong_key_and_wrong_device(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-A').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    path = '/api/v2/print/jobs/' + prepared['job_id']
    early = client.post(path + '/recover', json={
        'device_id': 'phone-a', 'idempotency_key': 'recover-before-claim-1',
        'reason': '尚未领取'})
    assert early.status_code == 409 and early.get_json()['error'] == 'print_not_claimed'
    claim_key = 'claim-for-recovery-0001'
    assert client.post(path + '/claim', json={
        'user_initiated': True, 'idempotency_key': claim_key,
        'transport': 'bluetooth', 'device_id': 'phone-a'}).status_code == 200
    wrong_key = client.post(path + '/recover', json={
        'device_id': 'phone-a', 'idempotency_key': 'claim-for-recovery-wrong',
        'reason': '错误操作标识'})
    assert wrong_key.status_code == 409
    assert wrong_key.get_json()['error'] == 'print_recovery_conflict'
    wrong_device = client.post(path + '/recover', json={
        'device_id': 'phone-b', 'idempotency_key': claim_key,
        'reason': '错误设备'})
    assert wrong_device.status_code == 409
    assert wrong_device.get_json()['error'] == 'order_print_conflict'
    assert client.get('/api/v2/orders?q=SKU-A&print_state=reserved').get_json()['count'] == 1


def test_task_owner_is_rechecked_inside_reserve_and_claim_transactions(app):
    client = app.test_client()
    jobs = app.extensions['service']['jobs']
    selected = client.get('/api/v2/orders?q=SKU-C').get_json()['items'][0]
    body = {'kind': 'order', 'item_ids': [selected['id']],
            'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}

    def deny_inside_reserve(_payload, _items, db=None):
        if db is not None:
            raise JobConflict('任务刚刚转交给其他包装人员', 'daily_work_owner_required')

    jobs.order_access_checker = deny_inside_reserve
    rejected = client.post('/api/v2/print/jobs', json=body)
    assert rejected.status_code == 409
    assert rejected.get_json()['error'] == 'daily_work_owner_required'
    assert client.get('/api/v2/orders?q=SKU-C&print_state=all').get_json()[
        'items'][0]['print_state'] == 'unprinted'

    denied = {'value': False}

    def deny_after_prepare(_payload, _items, db=None):
        if db is not None and denied['value']:
            raise JobConflict('任务刚刚转交给其他包装人员', 'daily_work_owner_required')

    jobs.order_access_checker = deny_after_prepare
    prepared = client.post('/api/v2/print/jobs', json=body).get_json()
    denied['value'] = True
    claim = client.post('/api/v2/print/jobs/' + prepared['job_id'] + '/claim', json={
        'user_initiated': True, 'idempotency_key': 'owner-race-operation-0001',
        'transport': 'bluetooth', 'device_id': 'phone-a'})
    assert claim.status_code == 409
    assert claim.get_json()['error'] == 'daily_work_owner_required'
    job = client.get('/api/v2/print/jobs/' + prepared['job_id']).get_json()
    assert job['status'] == 'cancelled' and job['sent'] is False
    assert client.get('/api/v2/orders?q=SKU-C&print_state=all').get_json()[
        'items'][0]['print_state'] == 'unprinted'


@pytest.mark.parametrize(('state', 'process_state', 'last_error'), [
    ('candidate', '500', ''),
    ('shipped_unposted', '500', ''),
    ('archived', '600', ''),
    ('active', '400', '奇门精确查询失败'),
])
def test_first_claim_rechecks_lifecycle_and_cancels_unsent_job(
        app, state, process_state, last_error):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-C').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    put_lifecycle(app.extensions['service']['store'], state, process_state, last_error)

    path = '/api/v2/print/jobs/' + prepared['job_id']
    rejected = client.post(path + '/claim', json={
        'user_initiated': True, 'idempotency_key': f'lifecycle-{state}-operation',
        'transport': 'bluetooth', 'device_id': 'phone-a'})
    assert rejected.status_code == 409
    assert rejected.get_json()['error'] == 'order_lifecycle_conflict'
    job = client.get(path).get_json()
    assert job['status'] == 'cancelled' and job['sent'] is False
    assert job['claim_rejection_code'] == 'order_lifecycle_conflict'
    order_result = client.get('/api/v2/orders?q=SKU-C&print_state=all').get_json()
    assert order_result['items'][0]['print_state'] == 'unprinted'
    with app.extensions['service']['store'].connect() as db:
        row = db.execute('SELECT idempotency_key,claim_token_hash FROM print_jobs WHERE id=?',
                         (prepared['job_id'],)).fetchone()
        assert row['idempotency_key'] is None and row['claim_token_hash'] is None
        audit = db.execute('''SELECT from_state,to_state,reason FROM order_print_audit
            WHERE order_no='ORDER-B' ORDER BY rowid DESC LIMIT 1''').fetchone()
        assert tuple(audit) == ('reserved', 'unprinted', 'cancelled')


def test_first_claim_rejects_shadow_record_even_without_lifecycle_row(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-C').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        row = db.execute("SELECT payload FROM records WHERE kind='orders' AND id='line-b1'").fetchone()
        item = json.loads(row['payload'])
        item.update(shadow_demand=True, lifecycle_print_blocked=True,
                    logistics_company='已完成订单需结存')
        db.execute("UPDATE records SET payload=? WHERE kind='orders' AND id='line-b1'",
                   (dumps(item),))
    rejected = client.post('/api/v2/print/jobs/' + prepared['job_id'] + '/claim', json={
        'user_initiated': True, 'idempotency_key': 'shadow-demand-operation',
        'transport': 'bluetooth', 'device_id': 'phone-a'})
    assert rejected.status_code == 409
    assert rejected.get_json()['error'] == 'order_lifecycle_conflict'


def test_first_claim_rejects_new_data_version_and_releases_reservation(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-C').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'", (dumps('orders-v2'),))
    request = {'user_initiated': True, 'idempotency_key': 'new-version-operation',
               'transport': 'bluetooth', 'device_id': 'phone-a'}
    path = '/api/v2/print/jobs/' + prepared['job_id']
    rejected = client.post(path + '/claim', json=request)
    assert rejected.status_code == 409
    assert rejected.get_json()['error'] == 'data_version_conflict'
    assert client.get(path).get_json()['status'] == 'cancelled'
    retried = client.post(path + '/claim', json=request)
    assert retried.status_code == 409
    assert retried.get_json()['error'] == 'data_version_conflict'
    assert client.get('/api/v2/orders?q=SKU-C&print_state=all').get_json()[
        'items'][0]['print_state'] == 'unprinted'


def test_lifecycle_change_does_not_cancel_a_job_already_claimed(app):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-C').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']],
        'expected_data_version': 'orders-v1', 'device_id': 'phone-a'}).get_json()
    path = '/api/v2/print/jobs/' + prepared['job_id']
    request = {'user_initiated': True, 'idempotency_key': 'already-claimed-operation',
               'transport': 'bluetooth', 'device_id': 'phone-a'}
    claimed = client.post(path + '/claim', json=request).get_json()
    put_lifecycle(app.extensions['service']['store'], 'candidate', '500')

    replayed = client.post(path + '/claim', json=request).get_json()
    assert replayed['status'] == 'claimed' and replayed['replayed'] is True
    assert 'claim_token' not in replayed
    reported = client.post(path + '/result', json={
        'claim_token': claimed['claim_token'], 'status': 'unknown', 'bytes_sent': 1,
        'message': '生命周期变化后人工核对', 'device_id': 'phone-a'}).get_json()
    assert reported['status'] == 'unknown'
    result = client.get('/api/v2/orders?q=SKU-C&print_state=all').get_json()
    assert result['items'][0]['print_state'] == 'uncertain'


@pytest.mark.parametrize(('bytes_sent', 'expected'), [(0, 'unprinted'), (1, 'uncertain')])
def test_failed_transport_only_releases_order_when_no_bytes_were_sent(app, bytes_sent, expected):
    client = app.test_client()
    selected = client.get('/api/v2/orders?q=SKU-C').get_json()['items'][0]
    prepared = client.post('/api/v2/print/jobs', json={
        'kind': 'order', 'item_ids': [selected['id']], 'expected_data_version': 'orders-v1',
        'device_id': 'phone-a'}).get_json()
    path = '/api/v2/print/jobs/' + prepared['job_id']
    claimed = client.post(path + '/claim', json={
        'user_initiated': True, 'idempotency_key': f'failed-operation-{bytes_sent:04}',
        'transport': 'bluetooth', 'device_id': 'phone-a'}).get_json()
    client.post(path + '/result', json={'claim_token': claimed['claim_token'], 'status': 'failed',
        'bytes_sent': bytes_sent, 'message': '发送失败', 'device_id': 'phone-a'})
    result = client.get('/api/v2/orders', query_string={'q': 'SKU-C', 'print_state': 'all'}).get_json()
    assert result['items'][0]['print_state'] == expected
