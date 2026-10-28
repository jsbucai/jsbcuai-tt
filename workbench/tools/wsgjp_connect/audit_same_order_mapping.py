"""Follow-up requested by user: compare a noted order across Qimen/WMS.

Separate focused investigation, maximum 12 calls. Credentials and raw business
values stay in memory. No writes to ERP or production services.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path

from audit_query_matrix import MatrixReader, rows, QIMEN
from audit_qimen_orders import expanded
from audit_order_sources import BASE, read_table, day
from audit_order_remark_trace import trace
from gateway import ApiError, compact

REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/same-order-mapping.json'


def decoded_leaves(value, path='$', depth=0):
    if depth > 15:
        raise ApiError('decode_depth_limit')
    if isinstance(value, dict):
        for key, child in value.items():
            yield from decoded_leaves(child, path + '.' + str(key), depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from decoded_leaves(child, path + '[]', depth + 1)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(('{', '[', '"')):
            try:
                parsed = json.loads(text)
            except ValueError:
                pass
            else:
                if parsed != value and isinstance(parsed, (dict, list, str)):
                    yield from decoded_leaves(parsed, path + '{decoded}', depth + 1)
                    return
        yield path, value
    else:
        yield path, value


class FocusedReader(MatrixReader):
    def read(self, method, params):
        if self.calls >= 12:
            raise ApiError('focused_call_limit')
        return super().read(method, params)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    if not parser.parse_args().run:
        parser.print_help()
        return
    reader = FocusedReader()
    now = datetime.now()
    local, meta = read_table(BASE / '销售订单统计列表数据.xls', {'订单编号', '商品编号', '单据备注'})
    by_order = {}
    for row in local:
        if row.get('订单编号') and row.get('商品编号'):
            by_order.setdefault(str(row['订单编号']).strip(), []).append(row)
    report = {'checked_at': now.isoformat(), 'erp_writes': 0, 'raw_response_saved': False,
              'scope': 'Focused same-order note mapping after user requested null-cause investigation', 'call_limit': 12}

    def qquery(ident=None):
        props = {'profile_id': int(reader.record['profileid']), 'query_time_type': 2,
                 'start_time': '2026-03-20 00:00:00', 'end_time': now.strftime('%Y-%m-%d %H:%M:%S')}
        if ident:
            props['trade_id'] = ident
        else:
            props['bill_status'] = '1,2,3,4,5'
        return reader.read(QIMEN, {'pageNo': '1', 'pageSize': '10', 'extendProps': compact(props)})

    discovered = qquery()
    known = next(str(r['trade_order']['trade_id']) for r in reversed(rows(discovered))
                 if any(s.get('单据备注') for s in by_order.get(str(r['trade_order']['trade_id']), [])))
    source = by_order[known]
    exact = qquery(known)
    qrows = rows(exact)
    if not qrows or any(str(r['trade_order']['trade_id']) != known for r in qrows):
        raise ApiError('exact_order_not_matched')
    raw_notes = {str(r['单据备注']) for r in source if r.get('单据备注')}
    report['source_note_lengths'] = sorted(map(len, raw_notes))
    report['source_lines'] = len(source)
    report['qimen_trace'] = trace(exact, source, meta['datemode'])
    states, all_keys, matches = Counter(), Counter(), Counter()
    for path, value in decoded_leaves(exact):
        all_keys[path] += 1
        if value is None:
            states[path + ':null'] += 1
        if isinstance(value, str) and any(value == n or value == n.replace(' ', '_') for n in raw_notes):
            matches[path] += 1
    report['generic_recursive_decode'] = {'all_scalar_paths': dict(all_keys), 'null_paths': dict(states),
                                           'source_note_matches': dict(matches)}
    nesting = []
    for r in qrows:
        ext = expanded(r['trade_order'].get('extendProps'))
        internal = ext.get('details', [])
        external = r.get('order_info', [])
        if isinstance(external, dict):
            external = external['order_info']
        line_extensions = [expanded(x.get('extendProps')) for x in external]
        nesting.append({'header_details_count': len(internal), 'outer_order_info_count': len(external),
            'header_detail_keys': sorted({k for x in internal for k in x}),
            'header_details_equal_outer_line_extensions': internal == line_extensions})
    report['nested_model_comparison'] = nesting

    order_day = day(source[0]['订货日期'], meta['datemode'])
    start = datetime.fromisoformat(order_day)
    params = {'begintime': start.strftime('%Y-%m-%d 00:00:00'),
              'endtime': (start + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00'),
              'timetype': 0, 'pageno': 1, 'pagesize': 100}
    report['wms_query_window'] = {'begin': params['begintime'], 'end': params['endtime']}
    report['wms_pages'] = []
    hits = []
    seen = set()
    for page in range(1, 9):
        if reader.calls >= 10:
            break
        value = reader.read('erp.ewms.stockoutbill.list', dict(params, pageno=page))
        found = rows(value)
        total = value.get('totalresults')
        report['wms_pages'].append({'page': page, 'count': len(found), 'reported_total': total})
        hits.extend(r for r in found if any(str(r.get(k, '')).strip() == known for k in ('number', 'tradeid', 'sourceid')))
        for r in found:
            seen.add(str(r.get('vchcode', '')))
        print(json.dumps({'stage': 'wms_date_window', 'page': page, 'count': len(found), 'matched_target': len(hits)}), flush=True)
        if hits or len(found) < 100 or isinstance(total, int) and page * 100 >= total:
            break
    report['wms_same_order_matches'] = len(hits)
    report['wms_same_order'] = []
    for r in hits:
        item = {'matching_identifier_fields': [k for k in ('number', 'tradeid', 'sourceid') if str(r.get(k, '')).strip() == known],
                'trace': trace(r, source, meta['datemode']),
                'comment_length': len(r.get('comment') or ''),
                'wms_primary_key_positive': str(r.get('vchcode', '')).isdigit() and int(r['vchcode']) > 0,
                'source_product_codes_match': sorted(str(x.get('usercode', '')) for x in r.get('goodslist', [])) == sorted(str(x['商品编号']) for x in source)}
        report['wms_same_order'].append(item)
    # Validate this one positive observed ID; insist the response is scoped, even if API ignores a filter.
    if hits and reader.calls < 12:
        wanted = hits[0].get('vchcode')
        if str(wanted).isdigit() and int(wanted) > 0:
            value = reader.read('erp.ewms.stockoutbill.query', {'vchcodes': [int(wanted)], 'status': 2})
            found = rows(value)
            report['exact_id_filter_validation'] = {'requested_id_count': 1, 'returned_count': len(found),
                'rows_with_requested_id': sum(str(r.get('vchcode')) == str(wanted) for r in found),
                'all_returned_ids_match_requested': bool(found) and all(str(r.get('vchcode')) == str(wanted) for r in found)}
    report['api_calls'] = reader.calls
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('qimen_trace', 'generic_recursive_decode', 'wms_same_order')}, ensure_ascii=True, indent=2))
    print(json.dumps({'matched_note_paths': [r['trace']['note_value_match_paths'] for r in report['wms_same_order']]}, ensure_ascii=True))


if __name__ == '__main__':
    main()
