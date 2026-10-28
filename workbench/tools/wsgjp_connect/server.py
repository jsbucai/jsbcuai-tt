"""Isolated, operator-driven ERP authorization/read probe. Does not import 5191.

Setup/probes are loopback-only, OAuth callback is one-use state protected. Vendor
credentials and token live under the current Windows user's DPAPI vault only.
No background synchronization or write methods. Tokens refresh on demand.
"""
import hmac
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

from flask import Flask, abort, make_response, redirect, render_template_string, request, session
from gateway import ApiError, Client
from vault import Vault

PORT = 5192
REFRESH_BEFORE_SECONDS = 24 * 60 * 60
REFRESH_RETRY_SECONDS = 5 * 60
LOCAL = f'http://127.0.0.1:{PORT}'
CALLBACK = f'http://192.168.1.5:{PORT}/integrations/wsgjp/callback'
CALLBACK_PATH = '/integrations/wsgjp/callback'
STYLE = """*{box-sizing:border-box}body{margin:0;background:#f4f5f6;color:#202124;font:16px/1.65 'Microsoft YaHei',sans-serif}main{max-width:740px;margin:45px auto;padding:32px;background:white;border:1px solid #e2e4e7;border-radius:16px}header{border-left:5px solid #d71920;padding-left:16px;margin-bottom:28px}h1{margin:0;font-size:26px}p{color:#60646c}label{display:block;margin-top:16px;font-weight:600}input{width:100%;padding:12px;border:1px solid #c9cdd3;border-radius:8px;font:inherit}button{background:#d71920;color:white;border:0;border-radius:8px;padding:12px 22px;font:inherit;cursor:pointer;margin-top:22px}a{color:#b91620}small{color:#60646c}.notice{padding:14px;background:#f5f6f7;border-radius:8px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;border-bottom:1px solid #eee;padding:10px}code{overflow-wrap:anywhere}@media(max-width:600px){main{margin:12px;padding:20px}}"""
PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>UL 随行 · 管家婆连接验证</title><style>{{ style }}</style><main><header><small>UL 随行 · 独立验证工具</small><h1>连接管家婆</h1></header><p>在当前电脑完成账套授权，再只读核对商品与库存。现有 App 继续使用原数据同步。</p><div class="notice">{{ message }}</div>
{% if not configured %}<form action="/configure" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><label>App Key<input name="appkey" autocomplete="off" required inputmode="numeric" maxlength="32"></label><label>App Secret<input type="password" name="appsecret" autocomplete="new-password" required maxlength="32"></label><label>Sign Key<input type="password" name="signkey" autocomplete="new-password" required maxlength="128"></label><small>只保存在当前 Windows 用户的加密存储中，不进入 APK、代码或日志。</small><br><button>保存本机连接信息</button></form>
{% elif not authorized %}<form action="/authorize" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><p>下一步前往管家婆官方页面登录并确认账套授权。ERP 密码只输入在官方页面。</p><button>打开管家婆官方授权</button></form>
{% else %}<p>已取得账套授权（账套尾号 {{ profile }}）。此工具仅开放商品、仓库、库存查询。</p><form action="/probe" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><label>核对商品编号<input name="sku" placeholder="例如 LS3073" pattern="[A-Za-z]{2}[0-9]{4}" maxlength="6" required></label><button>只读查询商品与库存</button></form>{% endif %}
{% if configured and not authorized %}<form action="/edit" method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button>更正本机连接信息</button></form>{% endif %}
{% if result %}<h2>查询结果</h2><table><tbody>{% for key, value in result.items() %}<tr><th>{{ key }}</th><td>{{ value }}</td></tr>{% endfor %}</tbody></table>{% endif %}<p><small>测试端口 5192 · 消息推送未启用 · 5088 / 5191 保持原有工作方式</small></p></main></html>"""
ERRORS = {'transport_or_response_error': '请求未完成或响应无法解析。未自动重试，请检查连接。',
          'invalid_token_response': '授权响应无法校验，请核对 App Secret；需要重新发起授权。',
          'account_mismatch': '刷新后的账套与原授权不一致，已拒绝保存。',
          'invalid_credentials': '请核对三项连接信息。',
          'invalid_appkey_format': 'App Key 应为 32 位数字。',
          'invalid_appsecret_format': 'App Secret 应为 32 位英文字母与数字。',
          'invalid_signkey_format': '请填写 Sign Key。'}
VENDOR_ERRORS = {8: '应用不存在，请核对 App Key。', 9: '应用已过期。', 10: '签名校验失败，请核对连接信息。',
                 11: '当前出口 IP 未获准访问。', 13: 'Token 无效。', 15: '账套授权失败。', 19: '接口限流，请稍后再试。'}


def create_app(vault=None, client_factory=Client, clock=time.time,
               refresh_before_seconds=REFRESH_BEFORE_SECONDS):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secrets.token_bytes(32), MAX_CONTENT_LENGTH=4096,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_NAME='ul_erp_probe')
    vault = vault or Vault()
    lock = threading.Lock()
    token_lock = threading.Lock()
    state = {'pending': None, 'busy': False, 'message': '请先填写管家婆应用的三项连接信息。',
             'result': None, 'refresh_retry_after': 0.0}
    try:
        credentials = vault.load('credentials')
        client_factory(credentials)
        state['message'] = '本机连接信息已就绪，等待账套授权。'
    except FileNotFoundError:
        credentials = None
    app.extensions['connection'] = state

    def stamp_token(token, previous=None):
        value = dict(token)
        now = clock()
        if previous and not value.get('refresh_token'):
            value['refresh_token'] = previous.get('refresh_token')
        if previous and not value.get('re_expires_in'):
            value['re_expires_in'] = previous.get('re_expires_in')
        value['received_at'] = now
        value['expires_at'] = now + float(value['expires_in'])
        if value.get('re_expires_in'):
            value['refresh_expires_at'] = now + float(value['re_expires_in'])
        elif previous and previous.get('refresh_expires_at'):
            value['refresh_expires_at'] = previous['refresh_expires_at']
        return value

    def load_token_record():
        try:
            value = vault.load('token')
        except FileNotFoundError:
            return None
        if not credentials or value.get('appkey') != credentials['appkey']:
            return None
        return value

    def refresh_deadline(value):
        explicit = value.get('refresh_expires_at')
        if explicit:
            return float(explicit)
        if value.get('received_at') is not None and value.get('re_expires_in'):
            return float(value['received_at']) + float(value['re_expires_in'])
        return None

    def token_record():
        value = load_token_record()
        if value is None:
            return None
        now = clock()
        expires_at = float(value.get('expires_at', 0))
        if expires_at > now + refresh_before_seconds:
            return value
        if state['refresh_retry_after'] > now:
            return value if expires_at > now + 60 else None
        refresh_token = value.get('refresh_token')
        deadline = refresh_deadline(value)
        if not refresh_token or (deadline is not None and deadline <= now + 60):
            return value if expires_at > now + 60 else None

        with token_lock:
            value = load_token_record()
            if value is None:
                return None
            now = clock()
            expires_at = float(value.get('expires_at', 0))
            if expires_at > now + refresh_before_seconds:
                return value
            try:
                refreshed = client_factory(credentials).refresh(value['refresh_token'])
                if str(refreshed.get('profileid')) != str(value.get('profileid')):
                    raise ApiError('account_mismatch')
                refreshed = stamp_token(refreshed, previous=value)
                vault.save_token(refreshed)
                state['refresh_retry_after'] = 0.0
                state['message'] = 'Token 已自动续期，账套连接可继续使用。'
                return refreshed
            except ApiError as error:
                state['refresh_retry_after'] = now + REFRESH_RETRY_SECONDS
                state['message'] = error_message(error) + ' 现有 Token 未被覆盖。'
            except Exception:
                state['refresh_retry_after'] = now + REFRESH_RETRY_SECONDS
                state['message'] = '自动续期结果未能安全保存，现有 Token 未被覆盖。'
            return value if expires_at > now + 60 else None

    def error_message(error):
        if error.category == 'vendor_rejected':
            return VENDOR_ERRORS.get(error.code, '管家婆拒绝了请求（错误码 %s）。未执行任何写入。' % error.code)
        return ERRORS.get(error.category, '连接验证未完成，请检查配置。')

    @app.before_request
    def boundary():
        if request.host not in {f'127.0.0.1:{PORT}', f'192.168.1.5:{PORT}'}:
            abort(403, description='host_not_allowed')
        if request.path == CALLBACK_PATH:
            if request.host != f'192.168.1.5:{PORT}' or request.method != 'GET':
                abort(403)
            return
        if request.remote_addr != '127.0.0.1' or request.host != f'127.0.0.1:{PORT}':
            abort(403, description='local_only')
        if request.method == 'POST':
            if request.headers.get('Origin') not in (None, LOCAL):
                abort(403, description='origin_not_allowed')
            csrf = session.get('csrf')
            if not csrf:
                abort(403, description='session_missing')
            if not hmac.compare_digest(csrf, request.form.get('csrf', '')):
                abort(403, description='csrf_mismatch')

    @app.after_request
    def headers(response):
        # A no-referrer navigation policy can make same-origin HTML form POSTs
        # send Origin:null. Keep strict Origin checking; only callback pages need
        # to suppress same-origin referrers because their URL contains auth_code.
        response.headers.update({'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer' if request.path == CALLBACK_PATH else 'same-origin',
            'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://authcentral.wsgjp.com; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.get('/')
    def home():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        record = token_record()
        return render_template_string(PAGE, style=STYLE, csrf=session['csrf'], configured=bool(credentials),
            authorized=bool(record), profile=str(record['profileid'])[-4:] if record else '',
            message=state['message'], result=state['result'])

    @app.post('/configure')
    def configure():
        nonlocal credentials
        with lock:
            if state['busy'] or state['pending']:
                abort(409)
            candidate = {key: request.form.get(key, '').strip() for key in ('appkey', 'appsecret', 'signkey')}
            try:
                client_factory(candidate)
                vault.save_credentials(candidate)
                credentials = candidate
                state['message'] = '连接信息已在本机加密保存，等待账套授权。'
            except ApiError as error:
                state['message'] = error_message(error)
        return redirect('/', 303)

    @app.post('/edit')
    def edit():
        nonlocal credentials
        with lock:
            if state['busy']:
                abort(409)
            state['pending'], credentials = None, None
            state['message'] = '请重新填写三项本机连接信息。'
        return redirect('/', 303)

    @app.post('/authorize')
    def authorize():
        if not credentials:
            abort(409)
        with lock:
            if state['busy']:
                abort(409)
            nonce = secrets.token_urlsafe(32)
            state['pending'] = {'nonce': nonce, 'expires': clock() + 600}
            state['message'] = '官方授权页面已打开，等待授权结果（10 分钟有效）。'
        return redirect('https://authcentral.wsgjp.com/account/login?' + urlencode({
            'appkey': credentials['appkey'], 'redirect_url': CALLBACK, 'keyword': nonce}), 303)

    @app.get(CALLBACK_PATH)
    def callback():
        # Invalid/replayed callbacks cannot invalidate an operator's pending flow.
        with lock:
            pending = state['pending']
            fields = ('keyword', 'appkey', 'auth_code')
            valid = (credentials and pending and pending['expires'] > clock() and not state['busy']
                and all(len(request.args.getlist(k)) == 1 for k in fields)
                and hmac.compare_digest(request.args.get('keyword', ''), pending['nonce'])
                and hmac.compare_digest(request.args.get('appkey', ''), credentials['appkey'])
                and 1 <= len(request.args.get('auth_code', '')) <= 2048)
            if not valid:
                return make_response('Authorization callback rejected. Restart from the local connection page.', 400)
            state['pending'], state['busy'] = None, True
        try:
            token = stamp_token(client_factory(credentials).exchange(request.args['auth_code']))
            vault.save_token(token)
            state['message'] = '账套授权成功。请只读核对一个商品和对应库存。'
        except ApiError as error:
            state['message'] = error_message(error)
        except Exception:
            state['message'] = '本机保存授权结果失败。请检查加密存储，不会自动重复兑换授权码。'
        finally:
            with lock:
                state['busy'] = False
        return redirect(LOCAL + '/', 303)

    @app.post('/probe')
    def probe():
        record = token_record()
        sku = request.form.get('sku', '').strip().upper()
        if not record or not re.fullmatch('[A-Z]{2}[0-9]{4}', sku):
            abort(400)
        with lock:
            if state['busy']:
                abort(409)
            state['busy'] = True
        try:
            client = client_factory(credentials)
            result = client.read('erp.ptype.list', record['auth_token'], {
                'begintime': '2000-01-01 00:00:00', 'endtime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'pageno': 1, 'pagesize': 1, 'timetype': 1, 'usercodes': [sku], 'isall': False})
            products = result.get('ptypes', [])
            exact = [p for p in products if p.get('usercode') == sku and not p.get('deleted')]
            if len(exact) != 1 or int(result.get('totalresults', 0)) != 1:
                state['result'] = {'商品编号': sku, '结果': '没有唯一匹配商品，请核对编号或账套。'}
            else:
                p = exact[0]
                # Keep data in memory; do not publish into the live app database.
                state['result'] = {'商品编号': sku, '名称': p.get('fullname', ''), '规格': p.get('standard', ''),
                    '型号': p.get('type', ''), 'ERP 商品 ID': p.get('ptypeid', '')}
                warehouses = client.read('erp.stock.list', record['auth_token'], {'pageno': 1, 'pagesize': 20})
                stocks = warehouses.get('stocks', [])
                if len(stocks) != int(warehouses.get('totalresults', -1)) or not stocks:
                    raise ApiError('warehouse_scope_incomplete')
                if any(str(w.get('profileid')) != str(record['profileid']) for w in stocks):
                    raise ApiError('account_mismatch')
                ids = [int(w['id']) for w in stocks]
                names = {str(w['id']): w.get('fullname', str(w['id'])) for w in stocks}
                inventory = client.read('erp.goodsstocks.list', record['auth_token'], {
                    'ptypeids': [int(p['ptypeid'])], 'ktypeids': ids, 'pageno': 1, 'pagesize': 50})
                rows = inventory.get('goodsstocks', [])
                if len(rows) != int(inventory.get('totalresults', -1)):
                    raise ApiError('inventory_scope_incomplete')
                state['result']['库存记录数'] = len(rows)
                for index, row in enumerate(rows, 1):
                    if (str(row.get('ptypeid')) != str(p['ptypeid']) or str(row.get('ktypeid')) not in names
                            or str(row.get('profileid')) != str(record['profileid'])):
                        raise ApiError('inventory_scope_mismatch')
                    state['result'][f'库存 {index} · {names[str(row["ktypeid"])]}'] = (
                        f'库存数量：{row.get("qty", "未返回")}；可销售库存：{row.get("saleqty", "未返回")}；'
                        f'SKU：{row.get("skuid", "未返回")}')
            state['message'] = '只读查询完成。未修改 ERP 或现有 App 数据。'
        except ApiError as error:
            state['message'], state['result'] = error_message(error), None
        except Exception:
            state['message'], state['result'] = '接口响应格式与文档不一致，已停止验证。', None
        finally:
            with lock:
                state['busy'] = False
        return redirect('/', 303)

    @app.errorhandler(500)
    def internal(error):
        return 'Local connection tool error. No business writes are supported.', 500

    return app


if __name__ == '__main__':
    from pathlib import Path
    import os
    from waitress import serve
    logging.disable(logging.CRITICAL)  # No HTTP URLs, raw auth failures or secrets in logs.
    application = create_app()
    runtime = Path(__file__).resolve().parents[2] / 'runtime' / 'wsgjp-connect'
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / 'server.json').write_text(json.dumps({'pid': os.getpid(), 'port': PORT}), encoding='utf-8')
    serve(application, listen=f'127.0.0.1:{PORT} 192.168.1.5:{PORT}', threads=4, max_request_body_size=4096)
