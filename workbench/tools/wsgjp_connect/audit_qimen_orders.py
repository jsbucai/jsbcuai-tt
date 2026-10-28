"""Fixed-method read-only Qimen probe; saves schema/counts, never raw orders."""
import argparse
from collections import Counter
from datetime import datetime
import json
import hashlib
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

from audit_order_sources import AuditReader, BASE, read_table, decimal
from gateway import ApiError, GATEWAY, NoRedirect, business_signature, compact

METHOD = 'qimen.taobao.erp.order.sync'  # Official documentation page 1781: query.
REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/qimen-order-audit.json'


def error_category(value):
    # Vendor errors may echo payloads. Return only recognized categories.
    message = str(value).lower()
    for label, needles in [
        ('permission_or_enablement', ('权限', '未开通', '白名单', 'not authorized')),
        ('unknown_method', ('接口不存在', '方法不存在', 'invalid method', 'method not found')),
        ('signature', ('签名', 'signature')),
        ('account', ('账套', '帐套', 'profile')),
        ('date_range', ('时间', '日期', 'date', 'time')),
        ('parameter', ('参数', 'parameter')),
    ]:
        if any(needle in message for needle in needles):
            return label
    return 'vendor_rejected'


def numeric_code(value):
    return str(value) if re.fullmatch(r'-?\d{1,8}', str(value)) else None


def unwrap(result):
    if not isinstance(result, dict):
        raise ApiError('invalid_response_shape')
    if 'iserror' in result:
        if result['iserror'] is not False:
            raise ApiError(error_category(result.get('errormessage')), numeric_code(result.get('errorcode')))
        result = result.get('response')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            raise ApiError('invalid_qimen_response') from None
    if not isinstance(result, dict):
        raise ApiError('invalid_qimen_response')
    if str(result.get('code')) != '0':
        raise ApiError(error_category(result.get('error_info')), numeric_code(result.get('error_code', result.get('code'))))
    return result


def query(reader, start, end, status, page=1, size=5, trade_ids=None):
    if not 1 <= page <= 25 or not 1 <= size <= 100:
        raise ApiError('probe_page_limit')
    props = {'profile_id': int(reader.record['profileid']), 'query_time_type': 2,
             'start_time': start, 'end_time': end}
    if status:
        props['bill_status'] = status
    if trade_ids:
        props['trade_id'] = ','.join(trade_ids)
    payload = {'pageNo': str(page), 'pageSize': str(size), 'extendProps': compact(props),
               'method': METHOD, 'appkey': reader.credentials['appkey'],
               'token': reader.record['auth_token'],
               'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    payload['sign'] = business_signature(payload, reader.credentials['signkey'])
    request = urllib.request.Request(
        GATEWAY + '/allnewapi', data=urllib.parse.urlencode(payload).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ApiError('response_too_large')
        result = json.loads(raw)
    except ApiError:
        raise
    except Exception:
        raise ApiError('transport_or_response_error') from None
    return unwrap(result)


def expanded(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def records(value, key):
    if isinstance(value, dict):
        value = value.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ApiError('unexpected_' + key + '_shape')
    return value


def inspect_result(result, local):
    orders = records(result.get('trade_orders'), 'trade_orders')
    local_by_number = {}
    for row in local:
        local_by_number.setdefault(str(row['订单编号']).strip(), []).append(row)
    heads, head_exts, lines, line_exts = [], [], [], []
    matches, notes, products = Counter(), Counter(), Counter()
    remark_fields = ('buyer_remark', 'seller_remark', 'seller_append_remark', 'seller_flag')
    remark_states = {field: Counter() for field in remark_fields}
    for order in orders:
        head = order.get('trade_order') or {}
        ext = expanded(head.get('extendProps'))
        heads.append(head)
        head_exts.append(ext)
        matching_numbers = set()
        for field in ('trade_id', 'trade_no', 'raw_no'):
            value = str(head.get(field, '')).strip()
            if value in local_by_number:
                matches[field] += 1
                matching_numbers.add(value)
        local_rows = [row for number in matching_numbers for row in local_by_number[number]]
        for field in remark_fields:
            raw_value = head.get(field)
            if field not in head:
                state = 'missing'
            elif raw_value is None:
                state = 'null'
            elif raw_value == '':
                state = 'empty_string'
            elif str(raw_value) == '0':
                state = 'zero_value'
            else:
                state = 'other_nonempty'
            remark_states[field][state] += 1
            value = '' if raw_value is None else str(raw_value)
            if value:
                notes[field + '_nonempty'] += 1
                if any(value == str(row.get('单据备注') or '') for row in local_rows):
                    notes[field + '_original_exact'] += 1
                if any(value == str(row.get('单据备注') or '').replace(' ', '_') for row in local_rows):
                    notes[field + '_spaces_to_underscores_exact'] += 1
        for line in records(order.get('order_info'), 'order_info'):
            line_ext = expanded(line.get('extendProps'))
            lines.append(line)
            line_exts.append(line_ext)
            for label, value in [('goods_code', line.get('goods_code')),
                                 ('spec_code', line.get('spec_code')),
                                 ('extend_x_code', line_ext.get('x_code'))]:
                if any(str(value or '').strip() == str(row.get('商品编号') or '').strip() for row in local_rows):
                    products[label + '_local_match'] += 1
    def coverage(rows):
        return dict(sorted(Counter(k for row in rows for k, v in row.items() if v not in (None, '', [], {})).items()))
    def fields(rows):
        return sorted({k for row in rows for k in row})
    def id_quality(rows, key):
        values = [str(row.get(key) or '').strip() for row in rows]
        nonzero = [value for value in values if value and value != '0']
        return {'nonzero': len(nonzero), 'unique_nonzero': len(set(nonzero)), 'empty_or_zero': len(values) - len(nonzero)}
    return {'status': 'success', 'response_fields': sorted(result),
            'total_results': int(result['totalResults']) if str(result.get('totalResults', '')).isdigit() else None,
            'upper_total_results': int(result['TotalResults']) if str(result.get('TotalResults', '')).isdigit() else None,
            'returned_orders': len(orders), 'returned_lines': len(lines),
            'order_number_match_counts': dict(matches), 'remark_comparison': dict(notes),
            'remark_field_states': {field: dict(counts) for field, counts in remark_states.items()},
            'product_comparison': dict(products), 'header_nonempty_fields': coverage(heads),
            'header_extended_nonempty_fields': coverage(head_exts),
            'line_nonempty_fields': coverage(lines), 'line_extended_nonempty_fields': coverage(line_exts),
            'header_fields': fields(heads), 'header_extended_fields': fields(head_exts),
            'identifier_quality': {'trade_id': id_quality(heads, 'trade_id'),
                                   'trade_no': id_quality(heads, 'trade_no'),
                                   'header_vchcode': id_quality(head_exts, 'vchcode'),
                                   'line_vchcode': id_quality(line_exts, 'vchcode')},
            'line_fields': fields(lines), 'line_extended_fields': fields(line_exts),
            'nested_detail_fields': sorted({k for ext in head_exts for row in (ext.get('details') or []) if isinstance(row, dict) for k in row})}


def full_audit(reader, start, end, status, local):
    all_orders, seen_rows, pages, expected = [], set(), [], None
    duplicate_rows = 0
    for page in range(1, 26):
        result = query(reader, start, end, status, page=page, size=100)
        orders = records(result.get('trade_orders'), 'trade_orders')
        total = result.get('TotalResults')
        if not str(total).isdigit() or int(total) > 2500:
            raise ApiError('unknown_or_excessive_total')
        total = int(total)
        if expected is not None and total != expected:
            raise ApiError('total_changed_during_paging')
        expected = total
        for order in orders:
            fingerprint = hashlib.sha256(compact(order).encode('utf-8')).hexdigest()
            if fingerprint in seen_rows:
                duplicate_rows += 1
            seen_rows.add(fingerprint)
        all_orders.extend(orders)
        pages.append({'page': page, 'rows': len(orders), 'reported_total': total,
                      'lowercase_totalResults': int(result['totalResults']) if str(result.get('totalResults', '')).isdigit() else None})
        if len(all_orders) == expected:
            break
        if len(orders) != 100 or len(all_orders) > expected:
            raise ApiError('incomplete_page_sequence')
        time.sleep(0.2)
    if len(all_orders) != expected:
        raise ApiError('page_limit_reached')
    aggregated = {'code': '0', 'TotalResults': expected, 'totalResults': len(all_orders),
                  'trade_orders': {'trade_orders': all_orders}}
    stats = inspect_result(aggregated, local)
    # The lower-case value in the aggregate is synthetic, not an API total.
    stats.pop('total_results', None)
    stats.pop('response_fields', None)
    source_ids = {str(row['订单编号']).strip() for row in local}
    returned_ids = {str(item['trade_order'].get('trade_id') or '') for item in all_orders}
    stats.update(pages=pages, unique_response_rows=len(seen_rows), exact_duplicate_response_rows=duplicate_rows,
                 query_window_pages_exhausted=True, exact_response_rows_unique=duplicate_rows == 0,
                 empty_trade_id_rows=sum(not str(item['trade_order'].get('trade_id') or '').strip() for item in all_orders),
                 repeated_trade_id_rows=len(all_orders) - len(returned_ids),
                 unique_trade_ids=len(returned_ids),
                 local_orders_not_returned=len(source_ids - returned_ids),
                 returned_orders_not_in_local=len(returned_ids - source_ids),
                 local_order_matches=len(source_ids & returned_ids))
    note_sources = {str(row['订单编号']).strip() for row in local if row.get('单据备注')}
    stats['matched_orders_with_nonempty_source_remark'] = len(note_sources & returned_ids)
    if all_orders:
        known = str(all_orders[0]['trade_order']['trade_id'])
        exact = query(reader, start, end, '', size=5, trade_ids=[known])
        exact_rows = records(exact.get('trade_orders'), 'trade_orders')
        stats['single_returned_order_filter'] = {
            'returned_orders': len(exact_rows),
            'all_returned_match_requested': bool(exact_rows) and all(str(row['trade_order'].get('trade_id')) == known for row in exact_rows)}
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--all-statuses', action='store_true')
    parser.add_argument('--verify', action='store_true', help='Two pages plus one exact order query, at most three reads')
    parser.add_argument('--full', action='store_true', help='At most 25 pages / 2500 orders plus one exact lookup')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    start, end = '2026-03-20 00:00:00', datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = '' if args.all_statuses else '1,2,3,4,5'
    report = {'checked_at': datetime.now().isoformat(), 'method': METHOD, 'erp_writes': 0,
              'query_start': start, 'query_end': end, 'query_time_type': 2,
              'bill_status': status or 'omitted', 'page': 1, 'page_size': 100 if args.full else 20 if args.verify else 5}
    try:
        reader = AuditReader()
        local, meta = read_table(BASE / '销售订单统计列表数据.xls', {'订单编号', '商品编号', '未完成数量'})
        report['local_source'] = meta
        local = [row for row in local if row.get('商品编号') and row.get('订单编号') and decimal(row.get('未完成数量', 0)) > 0]
        report['local_unfinished_orders'] = len({str(row['订单编号']) for row in local})
        if args.full:
            report['full_result'] = full_audit(reader, start, end, status, local)
        elif not args.verify:
            report['result'] = inspect_result(query(reader, start, end, status), local)
        else:
            probes, seen = [], set()
            for page in (1, 2):
                result = query(reader, start, end, status, page=page, size=20)
                stats = inspect_result(result, local)
                ids = [str(item['trade_order']['trade_id']) for item in records(result['trade_orders'], 'trade_orders')]
                stats['page'] = page
                stats['repeated_trade_ids_across_pages'] = len(seen.intersection(ids))
                seen.update(ids)
                probes.append(stats)
            note_orders = []
            for row in reversed(local):
                number = str(row['订单编号']).strip()
                if row.get('单据备注') and number not in note_orders:
                    note_orders.append(number)
                if len(note_orders) == 3:
                    break
            if note_orders:
                result = query(reader, start, end, '', size=20, trade_ids=note_orders)
                stats = inspect_result(result, local)
                stats['query'] = 'three_known_orders_with_source_notes'
                stats['requested_orders'] = len(note_orders)
                stats['returned_unrequested_orders'] = sum(str(item['trade_order'].get('trade_id')) not in note_orders for item in records(result['trade_orders'], 'trade_orders'))
                probes.append(stats)
            report['verification'] = probes
    except ApiError as error:
        report['result'] = {'status': 'error', 'category': error.category, 'code': error.code}
    except Exception as error:
        report['result'] = {'status': 'local_error', 'category': type(error).__name__}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else []
    existing.append(report)
    REPORT.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
