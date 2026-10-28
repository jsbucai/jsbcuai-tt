"""Tests use only fabricated values and official public signature examples."""
import json
from urllib.parse import parse_qs, urlparse
import pytest
from gateway import ApiError, Client, compact, encrypt, decrypt, token_signature, business_signature, NoRedirect
from server import create_app, LOCAL, CALLBACK, CALLBACK_PATH
from vault import Vault

CREDS = {'appkey': '1' * 32, 'appsecret': 'A' * 32, 'signkey': 'test-sign-only'}
TOKEN = {'appkey': CREDS['appkey'], 'auth_token': 'fake-auth-token', 'refresh_token': 'fake-refresh-token',
         'profileid': '12345', 'expires_in': 6000, 'expires_at': 10000}


def test_token_signature_official_known_answer():
    p = 'UrWWyyCy3bUMOkn0Sc8r+j1WufHDh9bASG6krOASc+T/ldypdrkT0gcFTzMNNDZNvRf9fAFBZMEYOsgxTA2q1vzf0Ma64JMqQkUUG9dPQ2bSCcU2CEP1KC1NTkLMmELC'
    assert token_signature('68943923115886070418838901844741', p, 'lezitiancheng') == '37b29297b1b32db0847c40f6854f0a2b2ad907491bd2681e8657d3f53bff4844'


def test_business_signature_official_known_answer():
    assert business_signature({'foo': 1, 'bar': 2, 'foo_bar': 3, 'foobar': 4}, '987654321') == 'd2799c74324035eb562930b71735bfc3'


def test_exchange_and_response_decryption():
    def transport(path, payload):
        assert path == '/api/token'
        raw = json.loads(decrypt(payload['p'], CREDS['appsecret']))
        assert raw['AuthParam'] == 'fake-code' and raw['GrantType'] == 'auth_token'
        return {'response': encrypt(compact(TOKEN), CREDS['appsecret'])}
    assert Client(CREDS, transport).exchange('fake-code')['auth_token'] == 'fake-auth-token'


def test_refresh_uses_refresh_grant():
    def transport(path, payload):
        assert path == '/api/token'
        raw = json.loads(decrypt(payload['p'], CREDS['appsecret']))
        assert raw['AuthParam'] == 'fake-refresh-token'
        assert raw['GrantType'] == 'refresh_token'
        return {'response': encrypt(compact(TOKEN), CREDS['appsecret'])}
    assert Client(CREDS, transport).refresh('fake-refresh-token')['auth_token'] == 'fake-auth-token'


def test_reject_wrong_app_token_without_leaking_payload():
    malformed = dict(TOKEN, appkey='2' * 32)
    with pytest.raises(ApiError, match='invalid_token_response'):
        Client(CREDS, lambda *_: encrypt(compact(malformed), CREDS['appsecret'])).exchange('fake-code')


@pytest.mark.parametrize('method', ['erp.purchasebill.save', 'erp.ptype.delete', 'https://example.invalid', 'erp.ktype.list'])
def test_write_and_unknown_methods_blocked_before_transport(method):
    calls = []
    with pytest.raises(ApiError):
        Client(CREDS, lambda *x: calls.append(x)).read(method, 'fake', {})
    assert not calls


def test_post_parameters_and_redirect_boundary():
    calls = []
    Client(CREDS, lambda *x: calls.append(x)).read('erp.ptype.list', 'fake', {'usercodes': ['AA0001'], 'isall': False})
    assert calls[0][1]['usercodes'] == '["AA0001"]'
    assert calls[0][1]['isall'] == 'false'
    assert NoRedirect().redirect_request(None, None, 302, '', {}, 'http://bad.invalid') is None
    with pytest.raises(ApiError):
        Client(CREDS).read('erp.ptype.list', 'fake', {'token': 'override'})


class MemoryVault:
    def __init__(self):
        self.entries = {}
    def load(self, name):
        if name not in self.entries:
            raise FileNotFoundError()
        return self.entries[name].copy()
    def save_credentials(self, value):
        self.entries['credentials'] = value.copy()
    def save_token(self, value):
        self.entries['token'] = value.copy()


@pytest.fixture
def flow():
    vault = MemoryVault()
    calls, now = [], [100.0]
    class FakeClient(Client):
        def exchange(self, code):
            calls.append(('exchange', code))
            return TOKEN.copy()
        def read(self, method, token, params):
            calls.append((method, params))
            if method == 'erp.ptype.list':
                return {'ptypes': [{'usercode': 'AA0001', 'fullname': 'test', 'ptypeid': '67890'}], 'totalresults': 1}
            if method == 'erp.stock.list':
                return {'stocks': [{'id': '100', 'profileid': '12345', 'fullname': 'test warehouse'}], 'totalresults': 1}
            return {'goodsstocks': [{'ptypeid': '67890', 'ktypeid': '100', 'profileid': '12345', 'qty': 7, 'saleqty': 5}], 'totalresults': 1}
    app = create_app(vault, FakeClient, clock=lambda: now[0], refresh_before_seconds=300)
    app.testing = True
    client = app.test_client()
    client.get('/', base_url=LOCAL)
    with client.session_transaction(base_url=LOCAL) as s:
        csrf = s['csrf']
    yield app, client, vault, calls, csrf, now


def start(flow):
    _, client, _, _, csrf, _ = flow
    assert client.post('/configure', base_url=LOCAL, data=dict(CREDS, csrf=csrf)).status_code == 303
    response = client.post('/authorize', base_url=LOCAL, data={'csrf': csrf})
    parsed = urlparse(response.location)
    assert parsed.scheme == 'https' and parsed.hostname == 'authcentral.wsgjp.com'
    params = parse_qs(parsed.query)
    assert params['redirect_url'] == [CALLBACK]
    return {'keyword': params['keyword'][0], 'appkey': CREDS['appkey'], 'auth_code': 'fake-code'}


def test_local_only_and_csrf(flow):
    _, client, vault, _, _, _ = flow
    assert client.get('/', base_url='http://192.168.1.5:5192').status_code == 403
    assert client.get('/', base_url=LOCAL, environ_overrides={'REMOTE_ADDR': '192.168.1.99'}).status_code == 403
    assert client.get('/', base_url='http://evil.invalid:5192').status_code == 403
    assert client.post('/configure', base_url=LOCAL, data=CREDS).status_code == 403
    assert not vault.entries


@pytest.mark.parametrize('mutation', ['state', 'appkey', 'expired', 'duplicate', 'head'])
def test_invalid_callback_does_not_exchange(flow, mutation):
    _, client, _, calls, _, now = flow
    query = start(flow)
    if mutation == 'state': query['keyword'] = 'wrong'
    if mutation == 'appkey': query['appkey'] = 'wrong'
    if mutation == 'expired': now[0] += 601
    if mutation == 'duplicate': query['auth_code'] = ['fake', 'duplicate']
    response = client.open(CALLBACK, query_string=query, method='HEAD' if mutation == 'head' else 'GET')
    assert response.status_code in (400, 403)
    assert not calls


def test_callback_one_use_and_probe_no_secrets_in_ui(flow):
    _, client, vault, calls, csrf, _ = flow
    query = start(flow)
    response = client.get(CALLBACK, query_string=query)
    assert response.status_code == 303 and response.location == LOCAL + '/'
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    assert client.get(CALLBACK, query_string=query).status_code == 400
    assert len(calls) == 1
    assert vault.entries['token']['expires_at'] == 6100
    assert client.post('/probe', base_url=LOCAL, data={'csrf': csrf, 'sku': 'aa0001'}).status_code == 303
    page = client.get('/', base_url=LOCAL).get_data(as_text=True)
    assert '库存数量：7' in page and '可销售库存：5' in page
    for secret in [CREDS['appsecret'], CREDS['signkey'], TOKEN['auth_token'], TOKEN['refresh_token']]:
        assert secret not in page
    assert [c[0] for c in calls[1:]] == ['erp.ptype.list', 'erp.stock.list', 'erp.goodsstocks.list']


def test_cross_origin_rejected_even_with_csrf(flow):
    _, client, _, calls, csrf, _ = flow
    response = client.post('/configure', base_url=LOCAL, data=dict(CREDS, csrf=csrf), headers={'Origin': 'https://evil.invalid'})
    assert response.status_code == 403 and not calls


def test_form_origin_policy_without_loosening_csrf(flow):
    _, client, _, calls, csrf, _ = flow
    # Chromium may send Origin:null on form navigations with no-referrer.
    # Keep same-origin form policy and still reject opaque/untrusted origins.
    assert client.get('/', base_url=LOCAL).headers['Referrer-Policy'] == 'same-origin'
    assert client.post('/configure', base_url=LOCAL, data=dict(CREDS, csrf=csrf),
                       headers={'Origin': 'null'}).status_code == 403
    assert not calls


def test_windows_vault_encrypted_and_restricted(tmp_path):
    import win32security
    vault = Vault(tmp_path / 'vault')
    vault.save_credentials(CREDS)
    vault.save_token(TOKEN)
    assert vault.load('credentials') == CREDS and vault.load('token') == TOKEN
    assert CREDS['appsecret'].encode() not in (vault.root / 'credentials.dpapi').read_bytes()
    security = win32security.GetNamedSecurityInfo(str(vault.root), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    assert security.GetSecurityDescriptorDacl().GetAceCount() == 2


def test_near_expiry_token_refreshes_once_and_is_saved():
    vault = MemoryVault()
    vault.save_credentials(CREDS)
    vault.save_token(dict(TOKEN, expires_at=150, received_at=0, re_expires_in=10000))
    calls = []

    class FakeClient(Client):
        def refresh(self, refresh_token):
            calls.append(refresh_token)
            return dict(TOKEN, auth_token='fresh-auth-token', refresh_token='fresh-refresh-token',
                        expires_in=6000, re_expires_in=12000)

    app = create_app(vault, FakeClient, clock=lambda: 100, refresh_before_seconds=300)
    app.testing = True
    client = app.test_client()
    assert client.get('/', base_url=LOCAL).status_code == 200
    assert client.get('/', base_url=LOCAL).status_code == 200
    assert calls == ['fake-refresh-token']
    assert vault.entries['token']['auth_token'] == 'fresh-auth-token'
    assert vault.entries['token']['expires_at'] == 6100
    assert vault.entries['token']['refresh_expires_at'] == 12100


def test_refresh_failure_keeps_valid_old_token_and_hides_secrets():
    vault = MemoryVault()
    vault.save_credentials(CREDS)
    vault.save_token(dict(TOKEN, expires_at=250, received_at=0, re_expires_in=10000))

    class FakeClient(Client):
        def refresh(self, refresh_token):
            raise ApiError('transport_or_response_error')

    app = create_app(vault, FakeClient, clock=lambda: 100, refresh_before_seconds=300)
    app.testing = True
    page = app.test_client().get('/', base_url=LOCAL).get_data(as_text=True)
    assert '已取得账套授权' in page
    assert vault.entries['token']['auth_token'] == 'fake-auth-token'
    assert TOKEN['auth_token'] not in page and TOKEN['refresh_token'] not in page
