"""Inactive integration routes must not acknowledge vendor auth or deliveries."""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app


@pytest.fixture
def app():
    with tempfile.TemporaryDirectory(prefix='reserved-test-', dir=ROOT / 'runtime') as temp:
        yield create_app({'TESTING': True, 'AUTO_IMPORT': False}, runtime_dir=Path(temp))


@pytest.mark.parametrize('endpoint', ['callback', 'notify'])
def test_never_acknowledges_or_reflects_auth_or_events(app, endpoint, caplog):
    path = '/integrations/wsgjp/' + endpoint
    marker = 'TEST-NOT-A-REAL-SECRET-7694'
    client = app.test_client()
    store = app.extensions['service']['store']
    with store.connect() as db:
        before = list(db.iterdump())
    requests = [
        ('GET', {}), ('HEAD', {}), ('OPTIONS', {}),
        ('GET', {'query_string': {'auth_code': marker, 'token': marker}}),
        ('HEAD', {'query_string': {'auth_code': marker}}),
        ('POST', {'json': {'auth_code': marker, 'sign': marker}}),
        ('POST', {'data': {'auth_code': marker}}),
        ('POST', {'data': marker, 'content_type': 'text/plain'}),
    ]
    for method, options in requests:
        response = client.open(path, method=method, **options)
        assert response.status_code == 503
        assert marker not in response.get_data(as_text=True)
        assert response.headers['X-UL-Integration-State'] == 'reserved-disabled'
        assert response.headers['Referrer-Policy'] == 'no-referrer'
        assert response.headers['Cache-Control'] == 'no-store'
        assert 'Location' not in response.headers
        if method == 'POST':
            assert response.get_json()['ok'] is False
            assert response.get_json()['enabled'] is False
    assert marker not in caplog.text
    with store.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize('endpoint', ['callback', 'notify'])
def test_original_lan_and_origin_restrictions_remain(app, endpoint):
    client = app.test_client()
    path = '/integrations/wsgjp/' + endpoint
    assert client.get(path, environ_overrides={'REMOTE_ADDR': '8.8.8.8'}).status_code == 403
    assert client.post(path, headers={'Origin': 'https://example.invalid'}).status_code == 403
    health = client.get('/api/v2/health').get_json()
    assert health['ok'] and health['capabilities']['wsgjp_api'] is False
    assert health['capabilities']['wsgjp_reserved_endpoints'] is True
