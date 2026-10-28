"""Small read-only official ERP client. No credentials in URLs, logs or errors."""
import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

GATEWAY = 'https://apigateway.wsgjp.com.cn'
READ_METHODS = frozenset({'erp.ptype.list', 'erp.goodsstocks.list', 'erp.stock.list'})


class ApiError(Exception):
    def __init__(self, category, code=None):
        self.category, self.code = category, code
        super().__init__(category)  # Do not include vendor body, parameters or tokens.


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def validate_credentials(value):
    import re
    if not isinstance(value, dict):
        raise ApiError('invalid_credentials')
    if not re.fullmatch(r'\d{32}', value.get('appkey', '')):
        raise ApiError('invalid_appkey_format')
    if not re.fullmatch(r'[A-Za-z0-9]{32}', value.get('appsecret', '')):
        raise ApiError('invalid_appsecret_format')
    if not 1 <= len(value.get('signkey', '')) <= 128:
        raise ApiError('invalid_signkey_format')


def encrypt(value, secret):
    padder = padding.PKCS7(128).padder()
    raw = padder.update(value.encode('utf-8')) + padder.finalize()
    ctx = Cipher(algorithms.AES(secret.encode('ascii')), modes.CBC(secret[5:21].encode('ascii'))).encryptor()
    return base64.b64encode(ctx.update(raw) + ctx.finalize()).decode('ascii')


def decrypt(value, secret):
    ctx = Cipher(algorithms.AES(secret.encode('ascii')), modes.CBC(secret[5:21].encode('ascii'))).decryptor()
    raw = ctx.update(base64.b64decode(value, validate=True)) + ctx.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(raw) + unpadder.finalize()).decode('utf-8')


def token_signature(appkey, p, signkey):
    return hashlib.sha256(compact({'appkey': appkey, 'p': p, 'signkey': signkey}).encode('utf-8')).hexdigest()


def business_signature(params, signkey):
    raw = ''.join(k + str(params[k]) for k in sorted(params) if k != 'sign') + signkey
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to another destination or HTTP.


def post(path, params):
    if path not in {'/api/token', '/allnewapi'}:
        raise ApiError('endpoint_not_allowed')
    req = urllib.request.Request(GATEWAY + path, data=urllib.parse.urlencode(params).encode('utf-8'),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ApiError('response_too_large')
        result = json.loads(raw)
    except ApiError:
        raise
    except Exception:
        raise ApiError('transport_or_response_error') from None
    if not isinstance(result, dict):
        raise ApiError('invalid_response')
    if result.get('iserror') is not False:
        code = result.get('errorcode')
        raise ApiError('vendor_rejected', code if isinstance(code, int) else None)
    return result.get('response')


class Client:
    def __init__(self, credentials, transport=post):
        validate_credentials(credentials)
        self.credentials, self.transport = credentials, transport

    def _token(self, grant_type, auth_param):
        if grant_type not in {'auth_token', 'refresh_token'}:
            raise ApiError('invalid_grant_type')
        if not isinstance(auth_param, str) or not 1 <= len(auth_param) <= 2048:
            raise ApiError('invalid_auth_parameter')
        c = self.credentials
        p = encrypt(compact({'TimeStamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                             'GrantType': grant_type, 'AuthParam': auth_param}), c['appsecret'])
        result = self.transport('/api/token', {'appkey': c['appkey'], 'p': p,
                                'sign': token_signature(c['appkey'], p, c['signkey'])})
        try:
            encrypted = result['response'] if isinstance(result, dict) else result
            token = json.loads(decrypt(encrypted, c['appsecret']))
            if token.get('appkey') != c['appkey'] or not token.get('auth_token') or not token.get('profileid'):
                raise ValueError()
            if float(token['expires_in']) <= 0:
                raise ValueError()
            return token
        except Exception:
            raise ApiError('invalid_token_response') from None

    def exchange(self, auth_code):
        return self._token('auth_token', auth_code)

    def refresh(self, refresh_token):
        return self._token('refresh_token', refresh_token)

    def read(self, method, token, params):
        if method not in READ_METHODS:
            raise ApiError('read_only_method_not_allowed')
        if {'method', 'appkey', 'token', 'timestamp', 'sign'} & params.keys():
            raise ApiError('reserved_parameters')
        payload = {key: compact(value) if isinstance(value, (list, dict, bool)) else str(value) for key, value in params.items()}
        payload.update(appkey=self.credentials['appkey'], method=method, token=token,
                       timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        payload['sign'] = business_signature(payload, self.credentials['signkey'])
        return self.transport('/allnewapi', payload)
