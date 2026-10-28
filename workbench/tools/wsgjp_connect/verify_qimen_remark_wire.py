"""Verify one OrderInfo.remark directly against HTTP JSON, without audit parsers."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

from audit_order_sources import AuditReader, BASE, read_table
from audit_qimen_orders import query, records
from gateway import ApiError, GATEWAY, NoRedirect, business_signature, compact

REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/qimen-remark-wire-check.json'


def walk(value, path='$'):
    if isinstance(value, dict):
        for key, child in value.items():
            current = path + '.' + key
            if key.casefold() == 'remark':
                yield current, child
            yield from walk(child, current)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + '[' + str(index) + ']')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    if not parser.parse_args().run:
        parser.print_help()
        return
    report = {'checked_at': datetime.now().isoformat(), 'erp_writes': 0,
              'method': 'qimen.taobao.erp.order.sync', 'raw_response_saved': False}
    try:
        reader = AuditReader()
        local, meta = read_table(BASE / '销售订单统计列表数据.xls', {'订单编号', '商品编号', '单据备注'})
        by_order = {}
        for row in local:
            if row.get('商品编号') and row.get('订单编号'):
                by_order.setdefault(str(row['订单编号']).strip(), []).append(row)
        start, end = '2026-03-20 00:00:00', datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        discovery = query(reader, start, end, '1,2,3,4,5', size=10)
        candidates = records(discovery.get('trade_orders'), 'trade_orders')
        known = next(str(item['trade_order']['trade_id']) for item in reversed(candidates)
                     if any(row.get('单据备注') for row in by_order.get(str(item['trade_order']['trade_id']), [])))
        local_rows = by_order[known]
        payload = {'method': 'qimen.taobao.erp.order.sync', 'pageNo': '1', 'pageSize': '5',
                   'extendProps': compact({'profile_id': int(reader.record['profileid']),
                                           'query_time_type': 2, 'start_time': start,
                                           'end_time': end, 'trade_id': known}),
                   'appkey': reader.credentials['appkey'], 'token': reader.record['auth_token'],
                   'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        payload['sign'] = business_signature(payload, reader.credentials['signkey'])
        req = urllib.request.Request(GATEWAY + '/allnewapi', data=urllib.parse.urlencode(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'}, method='POST')
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
            blob = response.read(2 * 1024 * 1024 + 1)
            report['http_status'] = response.status
        if len(blob) > 2 * 1024 * 1024:
            raise ApiError('response_too_large')
        wire_text = blob.decode('utf-8')
        body = json.loads(wire_text)
        report['wire_sha256'] = hashlib.sha256(blob).hexdigest()
        report['wire_bytes'] = len(blob)
        report['literal_remark_null_occurrences_in_wire'] = len(re.findall(r'"remark"\s*:\s*null\b', wire_text))
        if 'iserror' in body:
            if body['iserror'] is not False:
                raise ApiError('vendor_rejected')
            body = body['response']
        if isinstance(body, str):
            body = json.loads(body)
        if body.get('code') not in (0, '0'):
            raise ApiError('qimen_rejected')
        orders = body['trade_orders']
        if isinstance(orders, dict):
            orders = orders['trade_orders']
        if not isinstance(orders, list) or not orders:
            raise ApiError('exact_order_not_returned')
        if not all(str(item['trade_order']['trade_id']) == known for item in orders):
            raise ApiError('unexpected_order_returned')
        report['exact_order_matches'] = len(orders)
        report['source_note_nonempty_lines'] = sum(bool(row.get('单据备注')) for row in local_rows)
        report['source_note_lengths'] = [len(str(row['单据备注'])) for row in local_rows if row.get('单据备注')]
        report['source_modified_at'] = meta['modified_at']
        report['remark_fields'] = []
        for path, value in walk(body):
            entry = {'path': path, 'json_type': type(value).__name__,
                     'state': 'null' if value is None else 'empty' if value == '' else 'nonempty',
                     'matches_source_raw_note': any(value == str(row['单据备注']) for row in local_rows if row.get('单据备注'))}
            if value is None:
                entry['raw_value'] = None
            report['remark_fields'].append(entry)
        report['status'] = 'verified'
    except ApiError as error:
        report['error'] = {'category': error.category, 'code': error.code}
    except Exception as error:
        report['error'] = {'category': type(error).__name__}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
