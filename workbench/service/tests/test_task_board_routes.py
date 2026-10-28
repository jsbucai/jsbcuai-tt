"""HTTP authentication and integration contract for the compact task board."""
from test_staff_auth import public_app, admin_session, create_worker


def employee_headers(client, username='buyer.one'):
    response = client.post('/api/v2/auth/login', json={
        'username': username, 'password': 'worker-password'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.get_json()['token']}


def test_board_requires_login_and_role_scope_is_read_only(public_app):
    client = public_app.test_client()
    assert client.get('/api/v2/daily-work/board?role=purchase').status_code == 401
    admin = admin_session(client)
    worker = create_worker(client, admin).get_json()['staff']
    headers = employee_headers(client)
    for view in ('mine', 'role'):
        result = client.get('/api/v2/daily-work/board', query_string={
            'role': 'purchase', 'view': view}, headers=headers)
        assert result.status_code == 200
        data = result.get_json()
        assert data['page_size'] == 50 and data['total'] == 0
        assert isinstance(data['items'], list) and isinstance(data['today_events'], list)
        assert any(item['id'] == worker['id'] for item in data['filter_options']['assignees'])
    result = client.get('/api/v2/daily-work/board?role=sales&view=role', headers=headers)
    assert result.status_code == 403 and result.get_json()['error'] == 'role_forbidden'
    assert client.get('/api/v2/staff', headers=headers).status_code == 403


def test_board_version_conflicts_do_not_return_partial_pages(public_app):
    client = public_app.test_client()
    headers = admin_session(client)
    response = client.get('/api/v2/daily-work/board', query_string={
        'role': 'stock_packing', 'view': 'role', 'page': 2,
        'data_version': 'expired-version'}, headers=headers)
    assert response.status_code == 409
    assert response.get_json()['error'] == 'data_version_changed'
    assert 'items' not in response.get_json()
    response = client.post('/api/v2/daily-work/selections', json={
        'role': 'stock_packing', 'view': 'mine', 'all': True}, headers=headers)
    assert response.status_code == 400


def test_board_rejects_invalid_filters_and_unknown_batch_selections(public_app):
    client = public_app.test_client()
    headers = admin_session(client)
    for params in ({'page': 0}, {'due_from': 'bad-date'},
                   {'due_from': '2026-10-07', 'due_to': '2026-10-05'},
                   {'assignee_id': 'worker', 'view': 'mine'}):
        response = client.get('/api/v2/daily-work/board',
            query_string={'role': 'sales', **params}, headers=headers)
        assert response.status_code == 400
    response = client.post('/api/v2/daily-work/batches', json={
        'selection_id': 'not-present', 'idempotency_key': 'new-key', 'action': 'start'},
        headers=headers)
    assert response.status_code == 404


def test_admin_dashboard_and_export_snapshot_are_scoped(public_app):
    client=public_app.test_client()
    assert client.get('/api/v2/daily-work/dashboard').status_code==401
    admin=admin_session(client)
    create_worker(client,admin)
    employee=employee_headers(client)
    assert client.get('/api/v2/daily-work/dashboard',headers=employee).status_code==403
    dashboard=client.get('/api/v2/daily-work/dashboard',headers=admin)
    assert dashboard.status_code==200
    assert len(dashboard.get_json()['roles'])==4
    assert dashboard.get_json()['company']['customer_orders']==0
    response=client.post('/api/v2/daily-work/export-snapshot',headers=employee,json={
        'role':'sales','view':'role','ids':['not-visible'],
        'expected_data_version':'v1','expected_task_version':0})
    assert response.status_code==403


def test_two_stage_route_validates_role_scope_and_sales_action_versions(public_app):
    client=public_app.test_client();headers=admin_session(client)
    for role in ('sales','stock_packing'):
        response=client.get('/api/v2/daily-work/board',headers=headers,
            query_string={'role':role,'work_state':'todo'})
        assert response.status_code==200
        assert response.get_json()['summary']['work_state_counts']=={'todo':0,'in_progress':0}
        assert len(response.get_json()['print_state_version'])==64
        stale=client.get('/api/v2/daily-work/board',headers=headers,
            query_string={'role':role,'work_state':'todo','page':2,'print_state_version':'outdated-print-version'})
        assert stale.status_code==409 and stale.get_json()['error']=='print_state_version_changed'
    for role in ('purchase',):
        assert client.get('/api/v2/daily-work/board',headers=headers,
            query_string={'role':role,'work_state':'todo'}).status_code==400
    assert client.post('/api/v2/daily-work/groups/unknown/actions',json={'action':'start'}).status_code==401
    assert client.post('/api/v2/daily-work/groups/unknown/actions',headers=headers,
        json={'action':'start','idempotency_key':'key','version':0}).status_code==400
    assert client.post('/api/v2/daily-work/groups/unknown/actions',headers=headers,
        json={'action':'invalid','idempotency_key':'key','version':0}).status_code==400
