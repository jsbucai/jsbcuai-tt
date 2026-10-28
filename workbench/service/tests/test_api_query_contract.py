"""Published query generations, stock identity and label safety at the HTTP boundary."""
from pathlib import Path
from datetime import datetime, timedelta
import sys
import tempfile
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app
from core.store import dumps


IDENTITY = dict(profile_id='account', warehouse_id='warehouse-a', product_id='product',
                sku_id='variant', base_unit='件')


@pytest.fixture
def app():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='query-contract-', dir=ROOT / 'runtime') as runtime:
        yield create_app({'TESTING': True, 'AUTO_IMPORT': False}, runtime_dir=runtime)


def insert(app, kind, *items):
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps('version-one'),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('logistics_available','true')")
        for item in items:
            db.execute('INSERT OR REPLACE INTO records(kind,id,payload,sku,keywords,record_date,quantity,amount) VALUES (?,?,?,?,?,?,?,?)',
                (kind, item['id'], dumps(item), item.get('sku', '').upper(), item.get('sku', '').lower(), '', 1, 0))


def stock(identity='stock-a', **overrides):
    return dict(IDENTITY, id=identity, sku='LS001', product_name='螺栓', stock_quantity='12',
                available_stock='2', **overrides)


def order(identity='order-a', **overrides):
    return {**IDENTITY, 'id': identity, 'sku': 'LS001', 'product_name': '螺栓',
        'order_no': 'AD#1', 'shipping_id': '@10/01#AD#1', 'quantity': '2',
        'logistics_company': 'LS库存件', 'system_note': 'AD#PROJECT#1#DRAWING#螺栓#M10#8.8#锌#2#LS001',
        **overrides}


def test_order_inventory_uses_complete_identity_and_never_last_sku_row(app):
    inventory = [stock()]
    for index, key in enumerate(IDENTITY):
        value = stock(f'other-{index}')
        value.update({key: 'different', 'stock_quantity': str(100 + index)})
        inventory.append(value)
    insert(app, 'inventory', *inventory)
    insert(app, 'orders', order(), order('order-b', warehouse_id='different'),
           order('unknown-warehouse', warehouse_id=''), order('unmapped-product', product_id='missing'))
    response = app.test_client().get('/api/v2/orders?q=LS001').get_json()
    rows = {item['id']: item for item in response['items']}
    assert rows['order-a']['inventory']['stock_quantity'] == '12'
    assert rows['order-b']['inventory']['stock_quantity'] == '101'
    assert rows['unknown-warehouse']['inventory']['found'] is False
    assert rows['unknown-warehouse']['inventory']['reason'] == 'inventory_identity_incomplete'
    assert rows['unmapped-product']['inventory']['found'] is False
    assert response['inventory']['found'] is False and response['inventory']['ambiguous'] is True


def test_legacy_unique_sku_can_resolve_but_duplicates_are_ambiguous(app):
    queries = app.extensions['service']['queries']
    insert(app, 'inventory', {'id': 'one', 'sku': 'LS001', 'stock_quantity': '5'})
    assert queries.inventory_lookup('ls001')['stock_quantity'] == '5'
    insert(app, 'inventory', {'id': 'two', 'sku': 'LS001', 'stock_quantity': '90'})
    assert queries.inventory_lookup('LS001')['found'] is False
    assert queries.inventory_lookup('LS001')['ambiguous'] is True


def test_order_without_identity_cannot_borrow_even_one_modern_stock_row(app):
    insert(app, 'inventory', stock())
    unscoped = {key: value for key, value in order().items() if key not in IDENTITY}
    insert(app, 'orders', unscoped)
    result = app.test_client().get('/api/v2/orders?q=LS001').get_json()
    assert result['items'][0]['inventory']['found'] is False
    assert result['items'][0]['inventory']['reason'] == 'inventory_identity_incomplete'


@pytest.mark.parametrize('path', ['/api/v2/orders?q=LS001', '/api/v2/inventory?sku=LS001', '/api/v2/purchases?q=LS001'])
def test_query_pagination_rejects_a_different_published_generation(app, path):
    insert(app, 'inventory', stock())
    client = app.test_client()
    first = client.get(path).get_json()
    assert first['data_version'] == 'version-one'
    assert client.get(path + '&expected_data_version=version-one&page=2').status_code == 200
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'", (dumps('version-two'),))
    changed = client.get(path + '&expected_data_version=version-one&page=2')
    assert changed.status_code == 409
    assert changed.get_json()['error'] == 'data_version_changed'
    assert changed.get_json()['data_version'] == 'version-two'
    assert 'items' not in changed.get_json()


def test_inventory_filters_by_warehouse_and_keeps_aggregate_in_same_scope(app):
    first, second = stock(), stock('stock-b')
    second.update(warehouse_id='warehouse-b', stock_quantity='900')
    insert(app, 'inventory', first, second)
    result = app.test_client().get('/api/v2/inventory', query_string={'sku': 'LS001', **IDENTITY}).get_json()
    assert result['total'] == 1 and result['total_stock'] == 12
    assert result['items'][0]['id'] == 'stock-a'


def test_missing_delivery_stays_visible_and_can_print_without_fake_date(app):
    insert(app, 'orders', order(shipping_id='', calculation_status='blocked',
        calculation_issues=[{'code': 'delivery_date_missing', 'reason': '缺少交货日期', 'scope': 'order'}]))
    client = app.test_client()
    result = client.get('/api/v2/orders?q=LS001').get_json()
    item = result['items'][0]
    assert item['printable'] is True
    assert item['calculation_status'] == 'blocked'
    assert item['issues'][0]['message'] == '缺少交货日期'
    prepared = client.post('/api/v2/print/jobs', json={'kind': 'order', 'item_ids': [item['id']],
        'expected_data_version': result['data_version']})
    assert prepared.status_code == 200
    assert prepared.get_json()['pages'][0]['fields']['qrValue'] == item['order_no']
    with app.extensions['service']['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM print_jobs').fetchone()[0] == 1


def test_user_confirmed_delivery_is_consistent_in_query_and_label(app):
    insert(app, 'orders', order(system_note='普通摘要', summary='普通摘要',
        delivery_date='2026-10-24', delivery_date_source='user_confirmed_batch',
        delivery_date_confirmation_id='approved-18'))
    item = app.test_client().get('/api/v2/orders?q=LS001').get_json()['items'][0]
    assert item['delivery_date'] == '2026-10-24'
    assert item['delivery_date_status'] == 'known'
    assert '交期待确认' not in item['print_warnings']
    from core.fields import order_fields
    assert order_fields(item)['deliveryDate'] == '2026-10-24'
    assert item['summary'] == '普通摘要'


def test_classification_issue_does_not_block_a_complete_label(app):
    insert(app, 'orders', order(calculation_status='blocked', logistics_company='',
        calculation_issues=[{'code': 'receipt_missing', 'reason': '入库账本待核对', 'scope': 'order'}]))
    client = app.test_client()
    item = client.get('/api/v2/orders?q=LS001').get_json()['items'][0]
    assert item['calculation_status'] == 'blocked' and item['printable'] is True
    response = client.post('/api/v2/print/jobs', json={'kind': 'order', 'item_ids': ['order-a'],
        'expected_data_version': 'version-one'})
    assert response.status_code == 200 and response.get_json()['labels'] == 1


def test_scanning_old_freight_number_finds_full_order_suffix_when_freight_is_unavailable(app):
    insert(app, 'orders', order(order_no='AD045#36', shipping_id='', source_shipping_id='',
        calculation_status='blocked'), order('other-order', order_no='AD045#360', shipping_id=''))
    response = app.test_client().get('/api/v2/orders', query_string={'q': '@10/20#AD045#36'})
    result = response.get_json()
    assert response.status_code == 200 and result['count'] == 1
    assert result['items'][0]['order_no'] == 'AD045#36'


def test_legacy_snapshot_without_logistics_column_still_rejects_stock_scope(app):
    insert(app, 'orders', order())
    with app.extensions['service']['store'].connect(write=True) as db:
        db.execute("UPDATE settings SET value='false' WHERE key='logistics_available'")

    response = app.test_client().get('/api/v2/orders', query_string={
        'scope': 'stock', 'print_state': 'all'
    })

    assert response.status_code == 400
    assert response.get_json()['message'] == '订单副本缺少物流公司列，不能确定库存件范围'


def test_purchase_history_coverage_uses_posting_dates_and_rejects_older_requests(app):
    today = datetime.now()
    start = (today - timedelta(days=180)).strftime('%Y-%m-%d 00:00:00')
    end = today.isoformat(timespec='seconds')
    insert(app, 'purchases', {'id': 'receipt', 'sku': 'LS001', 'document_no': 'RECEIPT',
        'document_date': '2020-01-01', 'posted_at': end, 'quantity': '1', 'party': 'supplier'},
        {'id': 'old-receipt', 'sku': 'LS001', 'document_no': 'OLD', 'document_date': today.date().isoformat(),
         'posted_at': (today - timedelta(days=90)).isoformat(), 'quantity': '1', 'party': 'supplier'})
    with app.extensions['service']['store'].connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_query_scope',?)", (dumps({
            'kind': 'unshipped_documents', 'quantity_basis': 'reported_sales_quantity',
            'purchase_history_start': start, 'purchase_history_end': end}),))
    client = app.test_client()
    result = client.get('/api/v2/purchases?days=30').get_json()
    assert result['count'] == 1 and result['items'][0]['id'] == 'receipt'
    assert result['coverage']['kind'] == 'posted_purchase_history'
    assert result['coverage']['date_basis'] == 'posted_at'
    assert result['coverage']['pending_purchase_batches'] is False
    assert result['coverage']['start'] == start and result['coverage']['end'] == end
    assert result['latest_date'] == today.date().isoformat()
    assert client.get('/api/v2/purchases?days=180').status_code == 200
    outside = client.get('/api/v2/purchases?days=181')
    assert outside.status_code == 422
    assert outside.get_json()['error'] == 'query_coverage_unavailable'
    assert 'items' not in outside.get_json()
    changed = client.get('/api/v2/purchases?days=181&expected_data_version=old')
    assert changed.status_code == 409 and changed.get_json()['error'] == 'data_version_changed'


@pytest.mark.parametrize('overrides', [{'quantity': '0', 'system_note': ''},
                                      {'print_blocked': True, 'print_block_reason': '缺少单位'},
                                      {'printable': False}])
def test_source_or_required_label_fields_block_even_explicit_item_selection(app, overrides):
    insert(app, 'orders', order(**overrides))
    response = app.test_client().post('/api/v2/print/jobs', json={'kind': 'order', 'item_ids': ['order-a'],
        'expected_data_version': 'version-one'})
    assert response.status_code == 409 and response.get_json()['error'] == 'print_data_incomplete'


def test_prepared_labels_remain_frozen_after_query_record_becomes_unprintable(app):
    insert(app, 'orders', order())
    client = app.test_client()
    prepared = client.post('/api/v2/print/jobs', json={'kind': 'order', 'item_ids': ['order-a'],
        'expected_data_version': 'version-one'}).get_json()
    assert prepared['labels'] == 1
    insert(app, 'orders', order(shipping_id='', print_blocked=True))
    claim = client.post('/api/v2/print/jobs/' + prepared['job_id'] + '/claim', json={
        'user_initiated': True, 'transport': 'bluetooth', 'idempotency_key': 'one-user-print-operation'}).get_json()
    assert claim['pages'] == prepared['pages']
    assert claim['pages'][0]['fields']['shippingId'] == '@10/01#AD#1'


def test_public_erp_writes_require_admin_and_default_update_uses_sync(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='query-auth-', dir=ROOT / 'runtime') as runtime:
        app = create_app({'TESTING': True, 'AUTO_IMPORT': False, 'SOURCE_MODE': 'api', 'PUBLIC_MODE': True,
            'ADMIN_PASSWORD_HASH': generate_password_hash('test-password'),
            'ERP_CLIENT': SimpleNamespace(last_auth_error=None), 'API_SOURCE': object()}, runtime_dir=runtime)
        updates = app.extensions['service']['updates']
        calls = []
        monkeypatch.setattr(updates, 'resolve_attempt', lambda *args: calls.append(args) or {'id': args[0], 'status': 'succeeded'}, raising=False)
        monkeypatch.setattr(updates, 'enqueue', lambda identity, mode, request=None: {
            'id': identity, 'mode': mode, 'request': request, 'status': 'queued'})
        client = app.test_client()
        path = '/api/v2/freight-attempts/attempt-id/resolve'
        payload = {'resolution': 'applied', 'note': '已在ERP页面核对'}
        assert client.post(path, json=payload).status_code == 401
        assert not calls
        login = client.post('/api/v2/auth/login', json={'password': 'test-password', 'device_name': 'test'})
        headers = {'Authorization': 'Bearer ' + login.get_json()['token']}
        result = client.post(path, json=payload, headers=headers).get_json()
        assert result['attempt']['status'] == 'succeeded'
        assert calls == [('attempt-id', 'applied', '已在ERP页面核对')]
        for endpoint in ('/api/v2/update-jobs', '/api/v2/data/refresh'):
            result = client.post(endpoint, json={'id': 'operation-id'}, headers=headers)
            assert result.status_code == 202 and result.get_json()['job']['mode'] == 'sync'
        worker = client.post('/api/v2/staff', headers=headers, json={
            'username': 'worker.one', 'display_name': '包装一号',
            'password': 'worker-password', 'roles': ['stock_packing'],
        }).get_json()['staff']
        worker_login = client.post('/api/v2/auth/login', json={
            'username': worker['username'], 'password': 'worker-password'})
        worker_headers = {'Authorization': 'Bearer ' + worker_login.get_json()['token']}
        assert client.post(path, json=payload, headers=worker_headers).status_code == 403
        for endpoint in ('/api/v2/update-jobs', '/api/v2/data/refresh'):
            denied = client.post(endpoint, json={'id': 'worker-operation'}, headers=worker_headers)
            assert denied.status_code == 403 and denied.get_json()['error'] == 'admin_required'
        subset = client.post('/api/v2/update-jobs', headers=worker_headers, json={
            'id': 'subset-after-adjustment', 'mode': 'inventory_subset',
            'product_ids': ['product-one'],
        })
        assert subset.status_code == 202
        assert subset.get_json()['job']['request'] == {
            'product_ids': ['product-one'], 'identities': []}
        blocked = client.post('/api/v2/adjustment-batches', json={}, headers=headers)
        assert blocked.status_code == 409 and blocked.get_json()['error'] == 'capability_unavailable'
        scope = {'kind': 'unshipped_documents', 'start': '1900-01-01',
                 'end': '2100-01-01', 'quantity_basis': 'reported_sales_quantity'}
        with app.extensions['service']['store'].connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('data_query_scope',?)", (dumps(scope),))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('data_quantity_basis',?)", (dumps('reported_sales_quantity'),))
        health = client.get('/api/v2/health', headers=headers).get_json()
        assert health['capabilities']['source_contract_verified'] is False
        assert health['query_scope'] == scope and health['quantity_basis'] == 'reported_sales_quantity'
        queried = client.get('/api/v2/orders?q=LS001', headers=headers).get_json()
        assert queried['query_scope'] == scope and queried['quantity_basis'] == 'reported_sales_quantity'
