"""One-shot, production-account sales-order contract trial.

The script makes at most one erp.saleorder.add call, persists its intent before
network I/O, and never retries a write. It does not audit or delete the order.
Run with an explicitly supplied JSON config after read-only preflight.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from core.erp_api import ErpClient
from core.wsgjp_gateway import ApiError, business_signature, compact, post

VAULT = '/opt/ul-workbench/service/runtime/data/secrets/erp.json'
LEDGER = Path('/var/lib/ul-sales-order-trial')
WAREHOUSE = '江苏优螺'
TAX_RATE = Decimal('0.13')


def _read(client, method, params):
    if method not in {'erp.employee.listall', 'erp.btype.list', 'erp.stock.list', 'erp.ptype.list'}:
        raise ValueError('read method not allowed')
    record = client._ready()
    payload = {k: compact(v) if isinstance(v, (dict, list, bool)) else str(v)
               for k, v in params.items()}
    payload.update(appkey=record['credentials']['appkey'], token=record['token']['auth_token'],
                   method=method, timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    payload['sign'] = business_signature(payload, record['credentials']['signkey'])
    return post('/allnewapi', payload)


def _pages(client, method, collection, params=None):
    rows, seen = [], set()
    for page in range(1, 101):
        result = _read(client, method, dict(params or {}, pageno=page, pagesize=100))
        batch = result.get(collection) if isinstance(result, dict) else None
        if not isinstance(batch, list):
            raise ValueError(method + ': invalid collection')
        for item in batch:
            identity = str(item.get('id') or item.get('ptypeid') or '')
            if not identity or identity in seen:
                raise ValueError(method + ': missing or duplicate identity')
            seen.add(identity)
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise ValueError(method + ': page limit')


def _unique(rows, name, id_field='id'):
    rows = [x for x in rows if str(x.get('fullname', '')).strip() == name
            and x.get('deleted') is not True and x.get('isstop') is not True]
    if len(rows) != 1 or not rows[0].get(id_field):
        raise ValueError(name + ': not one active ERP identity')
    return rows[0]


def _money(value, places):
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _price(quote, basis, quantity):
    quote = Decimal(str(quote))
    quantity = Decimal(str(quantity))
    if not quote.is_finite() or not quantity.is_finite() or quote <= 0 or quantity <= 0:
        raise ValueError('positive finite price and quantity required')
    if basis not in {'tax_inclusive', 'tax_exclusive'}:
        raise ValueError('tax_basis must be explicit')
    untaxed = _money(quote / (1 + TAX_RATE) if basis == 'tax_inclusive' else quote, 3)
    gross = _money((quote if basis == 'tax_inclusive' else untaxed * (1 + TAX_RATE)) * quantity, 2)
    return untaxed, gross


def _summary(row, customer_po, delivery):
    name = str(row['product_name'])
    if '六角头螺栓' in name:
        name = '六角头螺栓'
    grade = str(row.get('grade', '') or '')
    if grade and grade.replace('.', '', 1).isdigit():
        grade += '级'
    values = [customer_po, 'UL随行测试', str(row['line_no']), '-', name,
              str(row.get('spec', '-') or '-').upper().replace('×', 'X').replace('*', 'X'),
              grade or '-', row.get('surface', '-') or '-', str(row['quantity']), row['product_code']]
    if any('#' in str(x) or '\n' in str(x) or '\r' in str(x) for x in values):
        raise ValueError('summary field has separator or newline')
    widths = (15, 15, 8, 18, 30, 20, 10, 10, 10)
    def pad(value, width):
        value = str(value).strip() or '-'
        used = sum(2 if ord(ch) > 127 else 1 for ch in value)
        return value + ' ' * max(width - used, 0)
    return '#'.join([*(pad(value, width) for value, width in zip(values, widths)),
                     str(values[-1]).strip()]) + '#交期=' + delivery


def _build(client, config):
    required = {'customer_name', 'salesperson', 'delivery_date', 'lines'}
    if not required.issubset(config):
        raise ValueError('missing trial config fields')
    delivery = date.fromisoformat(config['delivery_date']).isoformat()
    if delivery < datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat():
        raise ValueError('delivery in the past')
    customer = _unique(_pages(client, 'erp.btype.list', 'btypes'), config['customer_name'])
    salesperson = _unique(_pages(client, 'erp.employee.listall', 'employees'),
                          config['salesperson'])
    if config['salesperson'] not in {'李瑄', '刘锋'}:
        raise ValueError('salesperson not in authorized pair')
    warehouse = _unique(_pages(client, 'erp.stock.list', 'stocks'), WAREHOUSE)
    if str(warehouse['id']) != '29871293290508390':
        raise ValueError('unexpected warehouse')
    token = client._ready()['token']
    db = sqlite3.connect('file:/opt/ul-workbench/service/runtime/data/workbench.sqlite3?mode=ro', uri=True)
    try:
        reference = db.execute("SELECT payload FROM records WHERE kind='inventory' AND sku='LS3073' LIMIT 1").fetchone()
        if not reference or str(json.loads(reference[0]).get('profile_id')) != str(token.get('profileid')):
            raise ValueError('ERP token differs from published account')
    finally:
        db.close()
    maker = _unique(_pages(client, 'erp.employee.listall', 'employees'), '罗健树')
    if str(token.get('employee_id')) != str(maker['id']):
        raise ValueError('ERP token is not the confirmed maker')
    if not isinstance(config['lines'], list) or not 1 <= len(config['lines']) <= 2:
        raise ValueError('trial must have one or two lines')
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    # Sales management's exact-number filter accepts at most 20 characters.
    number = 'UL' + now.strftime('%y%m%d%H%M%S') + uuid.uuid4().hex[:6].upper()
    details, total, summaries = [], Decimal(0), []
    for idx, raw in enumerate(config['lines'], 1):
        code = str(raw['product_code']).strip().upper()
        products = _pages(client, 'erp.ptype.list', 'ptypes', {
            'begintime': '1900-01-01 00:00:00', 'endtime': '2100-01-01 00:00:00',
            'timetype': 1, 'isall': True, 'usercodes': [code]})
        matched = [p for p in products if str(p.get('usercode', '')).upper() == code
                   and p.get('deleted') is not True and p.get('isstop') is not True]
        if len(matched) != 1:
            raise ValueError(code + ': product identity not unique')
        product = matched[0]
        skus = product.get('skus')
        if not isinstance(skus, list) or len(skus) != 1:
            raise ValueError(code + ': SKU identity not unique')
        sku = skus[0]
        quantity = Decimal(str(raw['quantity']))
        untaxed, gross = _price(raw['quote_unit_price'], raw.get('tax_basis', 'tax_inclusive'), quantity)
        total += gross
        row = dict(raw, line_no=idx, product_name=str(product['fullname']), product_code=code)
        line_summary = _summary(row, str(config.get('customer_order_no') or number), delivery)
        summaries.append(line_summary)
        details.append({'ptypeid': str(product['ptypeid']), 'pfullname': str(product['fullname']),
                        'prop1': str(sku.get('prop1') or '0'), 'prop2': str(sku.get('prop2') or '0'),
                        'unit': 1, 'unitrate': 1, 'assqty': str(quantity),
                        'assdpprice': str(untaxed), 'asstpprice': str(untaxed),
                        'tax': 13, 'comment': line_summary})
    order = {'number': number, 'date': now.strftime('%Y-%m-%d %H:%M:%S'),
             'todate': delivery + ' 00:00:00', 'total': str(_money(total, 2)),
             'ktypeid': str(warehouse['id']), 'btypeid': str(customer['id']),
             'etypeid': str(salesperson['id']), 'summary': summaries[0],
             'comment': '', 'details': details}
    return order, {'customer': config['customer_name'], 'salesperson': config['salesperson'],
                   'maker': '罗健树', 'warehouse': WAREHOUSE, 'line_count': len(details),
                   'quoted_total': str(_money(total, 2))}


def _atomic(path, data):
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'x', encoding='utf-8') as out:
        os.chmod(temporary, 0o600)
        json.dump(data, out, ensure_ascii=False, sort_keys=True)
        out.flush(); os.fsync(out.fileno())
    os.replace(temporary, path)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {'--preview', '--submit'}:
        raise ValueError('usage: sales_order_trial.py --preview|--submit config.json')
    config_path = Path(sys.argv[2])
    config = json.loads(config_path.read_text(encoding='utf-8'))
    client = ErpClient(VAULT)
    with client.lock:
        order, facts = _build(client, config)
        request_hash = hashlib.sha256(compact(order).encode()).hexdigest()
        print('trial_preview', json.dumps({'number': order['number'], **facts,
            'request_sha256': request_hash}, ensure_ascii=False))
        if sys.argv[1] == '--preview':
            return
        LEDGER.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(LEDGER, 0o700)
        lock_path = LEDGER / 'one-production-trial.lock'
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                out.write(order['number'] + '\n'); out.flush(); os.fsync(out.fileno())
            entry = LEDGER / (order['number'] + '.json')
            _atomic(entry, {'status': 'intent_committed', 'order': order, 'facts': facts,
                            'request_sha256': request_hash})
            record = client._ready()
            c, t = record['credentials'], record['token']
            payload = {'saleorder': compact(order), 'appkey': c['appkey'],
                       'token': t['auth_token'], 'method': 'erp.saleorder.add',
                       'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            payload['sign'] = business_signature(payload, c['signkey'])
            try:
                result = post('/allnewapi', payload)  # Exactly one write call.
            except ApiError as exc:
                # Save only the adapter's bounded category/code. A transport
                # failure remains unknown; a vendor rejection is not retried.
                _atomic(entry, {'status': 'vendor_rejected' if exc.category not in {
                    'transport_or_response_error', 'response_too_large', 'invalid_response'
                } else 'result_unknown', 'order': order, 'facts': facts,
                    'request_sha256': request_hash, 'error_category': exc.category,
                    'error_code': exc.code})
                raise
            _atomic(entry, {'status': 'response_received', 'order': order, 'facts': facts,
                            'request_sha256': request_hash, 'response': result})
            print('trial_response', json.dumps({'number': order['number'], 'response': result}, ensure_ascii=False))
        except BaseException:
            # The persistent lock and intent remain. Never auto-resubmit.
            raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('trial_error', type(exc).__name__, str(exc)[:160], file=sys.stderr)
        sys.exit(1)
