"""Bounded, read-only ERP dictionary preflight for a single sales-order trial.

Run on N150 with PYTHONPATH=/opt/ul-workbench/service. Never prints tokens or
raw vendor responses. This intentionally has no write method.
"""
import json
import sys
from datetime import datetime

from core.erp_api import ErpClient
from core.wsgjp_gateway import business_signature, compact, post

VAULT = '/opt/ul-workbench/service/runtime/data/secrets/erp.json'
METHODS = {'erp.employee.listall', 'erp.btype.list', 'erp.stock.list', 'erp.ptype.list'}


def read(client, method, params):
    if method not in METHODS:
        raise ValueError('read-only method restriction')
    with client.lock:
        record = client._ready()
        c, t = record['credentials'], record['token']
        payload = {k: compact(v) if isinstance(v, (dict, list, bool)) else str(v)
                   for k, v in params.items()}
        payload.update(appkey=c['appkey'], token=t['auth_token'], method=method,
                       timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        payload['sign'] = business_signature(payload, c['signkey'])
        return post('/allnewapi', payload)


def pages(client, method, collection, params=None, max_pages=20):
    found, expected, seen = [], None, set()
    for page in range(1, max_pages + 1):
        result = read(client, method, dict(params or {}, pageno=page, pagesize=100))
        if not isinstance(result, dict) or not isinstance(result.get(collection), list):
            raise ValueError(method + ': invalid response')
        count = result.get('totalresults')
        if isinstance(count, bool) or not str(count).isdigit():
            raise ValueError(method + ': invalid total')
        if expected is not None and expected != int(count):
            print('pagination_total_changed', method, expected, count)
        expected = int(count)
        batch = result[collection]
        for item in batch:
            digest = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if digest in seen:
                raise ValueError(method + ': repeated row')
            seen.add(digest)
        found.extend(batch)
        if len(batch) < 100:
            print('pagination_observed', method, 'rows', len(found), 'advertised', expected)
            return found
    raise ValueError(method + ': page limit')


def main():
    client = ErpClient(VAULT)
    print('profile_suffix', client.account()[-5:])
    token = client._ready()['token']
    print('token_identity', json.dumps({k: token.get(k) for k in ('employee_id', 'employeeid', 'user_id', 'userid', 'operatorid')}, ensure_ascii=False))
    employees = pages(client, 'erp.employee.listall', 'employees')
    print('employee_count', len(employees))
    for name in ('李瑄', '刘锋', '罗健树'):
        matched = [x for x in employees if str(x.get('fullname', '')).strip() == name]
        print('employee', name, json.dumps([
            {k: x.get(k) for k in ('id', 'employeeid', 'etypeid', 'fullname', 'usercode', 'isstop', 'deleted', 'isloginuser')}
            for x in matched], ensure_ascii=False))
    stocks = pages(client, 'erp.stock.list', 'stocks')
    print('warehouse_count', len(stocks))
    matched = [x for x in stocks if str(x.get('fullname', '')).strip() == '江苏优螺']
    print('warehouse', json.dumps([{k: x.get(k) for k in ('id', 'fullname', 'usercode')}
                                   for x in matched], ensure_ascii=False))
    customers = pages(client, 'erp.btype.list', 'btypes', max_pages=100)
    print('customer_count', len(customers))
    markers = ('测试', '内部', '自用', '江苏优螺', '本公司', '样品', '备用零售客户')
    matched = [x for x in customers if any(m in str(x.get('fullname', '')) for m in markers)]
    print('internal_customer_candidates', json.dumps([
        {k: x.get(k) for k in ('id', 'fullname', 'usercode', 'isstop', 'deleted')}
        for x in matched[:30]], ensure_ascii=False))
    if len(matched) > 30:
        print('internal_customer_candidates_truncated', len(matched))
    products = pages(client, 'erp.ptype.list', 'ptypes', {
        'begintime': '1900-01-01 00:00:00', 'endtime': '2100-01-01 00:00:00',
        'timetype': 1, 'isall': True, 'usercodes': ['LS3073']}, max_pages=3)
    matched = [x for x in products if str(x.get('usercode', '')).upper() == 'LS3073']
    print('sample_product_count', len(matched))
    print('sample_product', json.dumps([
        {k: x.get(k) for k in ('ptypeid', 'fullname', 'usercode', 'deleted', 'isstop', 'skus')}
        for x in matched], ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('preflight_error', type(exc).__name__, str(exc)[:120], file=sys.stderr)
        sys.exit(1)
