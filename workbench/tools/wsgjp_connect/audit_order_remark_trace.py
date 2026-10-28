"""Bounded cross-interface note trace. Only documented reads; aggregate output."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from audit_order_sources import AuditReader, BASE, read_table, day, attempt
from audit_qimen_orders import query, records, expanded
from gateway import ApiError

REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/order-remark-trace.json'


def leaves(value, path='$', depth=0):
    if depth > 12:
        raise ApiError('nested_response_limit')
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, path + '.' + str(key), depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from leaves(child, path + '[]', depth + 1)
    elif isinstance(value, str) and path.endswith('.extendProps') and value.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(value)
        except ValueError:
            yield path, value
        else:
            yield from leaves(parsed, path + '{json}', depth + 1)
    else:
        yield path, value


def trace(value, source_rows, datemode):
    raw_notes = {str(r['单据备注']) for r in source_rows if r.get('单据备注')}
    system_notes = {note.replace(' ', '_') for note in raw_notes}
    due = {day(r['到货日期'], datemode) for r in source_rows if r.get('到货日期')}
    ordered = {day(r['订货日期'], datemode) for r in source_rows if r.get('订货日期')}
    states, matches, dates, nonempty_paths = Counter(), Counter(), Counter(), Counter()
    for path, val in leaves(value):
        if val not in (None, '', [], {}):
            nonempty_paths[path] += 1
        if re.search(r'remark|memo|message|comment|summary|seller_flag', path, re.I):
            state = 'null' if val is None else 'empty' if val == '' else 'zero' if str(val) == '0' else 'nonempty'
            states[path + ':' + state] += 1
        if not isinstance(val, str) or not val:
            continue
        if val in raw_notes:
            matches[path + ':raw_exact'] += 1
        if val in system_notes:
            matches[path + ':system_exact'] += 1
        if any(len(note) >= 8 and note in val for note in raw_notes | system_notes):
            matches[path + ':contains_full_note'] += 1
        if re.search(r'date|time|due', path, re.I):
            observed = val.replace('/', '-').replace('T', ' ')[:10]
            if observed in due:
                dates[path + ':equals_source_due_day'] += 1
            if observed in ordered:
                dates[path + ':equals_source_order_day'] += 1
    return {'source_lines': len(source_rows), 'source_nonempty_note_variants': len(raw_notes),
            'note_field_states': dict(states), 'note_value_match_paths': dict(matches),
            'date_value_matches_not_semantic_mapping': dict(dates),
            'nonempty_scalar_paths': dict(nonempty_paths)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    if not parser.parse_args().run:
        parser.print_help()
        return
    now = datetime.now()
    end = now.strftime('%Y-%m-%d %H:%M:%S')
    start = '2026-03-20 00:00:00'
    report = {'checked_at': now.isoformat(), 'erp_writes': 0, 'production_files_modified': False,
              'scope': 'Known Qimen orders and up to three source-matching posted sales documents'}
    try:
        reader = AuditReader()
        local, meta = read_table(BASE / '销售订单统计列表数据.xls', {'订单编号', '商品编号', '未完成数量', '单据备注'})
        by_order = {}
        for row in local:
            if row.get('订单编号') and row.get('商品编号'):
                by_order.setdefault(str(row['订单编号']).strip(), []).append(row)
        report['source'] = meta
        response = query(reader, start, end, '1,2,3,4,5', size=10)
        orders = records(response.get('trade_orders'), 'trade_orders')
        selected = [order for order in orders if str(order.get('trade_order', {}).get('trade_id')) in by_order][:3]
        report['known_qimen_orders'] = []
        for item in selected:
            ident = str(item['trade_order']['trade_id'])
            report['known_qimen_orders'].append(trace(item, by_order[ident], meta['datemode']))
        if not selected:
            raise ApiError('no_source_matching_qimen_sample')
        known = str(selected[0]['trade_order']['trade_id'])
        exact = query(reader, start, end, '', size=5, trade_ids=[known])
        report['qimen_exact_no_status'] = trace(exact, by_order[known], meta['datemode'])
        report['qimen_exact_no_status']['returned_orders'] = len(records(exact.get('trade_orders'), 'trade_orders'))
        for label, method, params in [
            ('same_order_limited_delivery', 'beefun.deliver.getdeliverbilllistlimitinfo',
             {'pageno': 1, 'pagesize': 10, 'tradeid': known}),
            ('same_order_full_delivery', 'beefun.deliver.getdeliverbilllist',
             {'pageno': 1, 'pagesize': 10, 'tradeid': known, 'deleted': '1',
              'starttime': '2000-01-01 00:00:00', 'endtime': end})]:
            result, status = attempt(reader, method, params)
            if isinstance(result, dict):
                found = result.get('eshopdeliverbills', result.get('EShopDeliverBills', []))
                status['returned_orders'] = len(found) if isinstance(found, list) else None
                status['trace'] = trace(result, by_order[known], meta['datemode'])
            report[label] = status
        base = {'dlytype': -1, 'begintime': (now - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00'),
                'endtime': end, 'pageno': 1, 'pagesize': 100}
        first, status = attempt(reader, 'erp.bill.getsalebilllist', base)
        report['sales_lookup'] = status
        bills = first.get('bills', []) if isinstance(first, dict) else []
        total = first.get('totalresults', 0) if isinstance(first, dict) else 0
        matches = [b for b in bills if str(b.get('number', '')).strip() in by_order]
        if not matches and isinstance(total, int) and 100 < total <= 1000:
            base['pageno'] = (total + 99) // 100
            last, last_status = attempt(reader, 'erp.bill.getsalebilllist', base)
            report['sales_last_page_lookup'] = last_status
            if isinstance(last, dict):
                matches = [b for b in last.get('bills', []) if str(b.get('number', '')).strip() in by_order]
        report['sales_lookup']['matching_source_orders_in_examined_pages'] = len(matches)
        sample = matches[:3]
        report['posted_order_details'] = []
        for bill in sample:
            src = by_order[str(bill['number']).strip()]
            result, detail_status = attempt(reader, 'erp.bill.getsalebilldetails', {'vchcode': bill['vchcode']})
            item = {'header': trace(bill, src, meta['datemode']), 'detail_query': detail_status}
            if result is not None:
                item['detail_trace'] = trace(result, src, meta['datemode'])
            report['posted_order_details'].append(item)
        if sample:
            result, status = attempt(reader, 'erp.bill.billcenterlist', {
                'pageindex': 1, 'pagesize': 10, 'groupcode': 'default',
                'overbegin': '2000-01-01 00:00:00', 'overend': end,
                'vchcode': [int(b['vchcode']) for b in sample], 'hascustom': True, 'showredbill': True})
            report['posted_order_custom_fields'] = status
            if isinstance(result, dict):
                status['returned_bills'] = len(result.get('details', []))
                status['traces'] = [trace(b, by_order.get(str(b.get('number', '')).strip(), []), meta['datemode']) for b in result.get('details', [])]
        report['standard_api_calls'] = reader.calls
        report['qimen_api_calls'] = 2
    except ApiError as error:
        report['error'] = {'category': error.category, 'code': error.code}
    except Exception as error:
        report['local_error'] = type(error).__name__
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # Console stays compact; report also contains only schema/counts.
    def compact_view(value):
        if isinstance(value, dict):
            return {key: compact_view(child) for key, child in value.items() if key not in ('nonempty_scalar_paths', 'headers')}
        if isinstance(value, list):
            return [compact_view(child) for child in value]
        return value
    print(json.dumps(compact_view(report), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
