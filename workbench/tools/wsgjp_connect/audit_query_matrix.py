"""Explicit, bounded diagnostic of documented order READ APIs.

No ERP writes, no production changes, no raw responses or business values saved.
The production client's allowlist is not changed. --run is required.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

from audit_order_sources import AuditReader, BASE, read_table, day
from audit_order_remark_trace import trace, leaves
from audit_qimen_orders import unwrap, error_category, numeric_code, expanded
from gateway import ApiError, GATEWAY, NoRedirect, business_signature, compact

QIMEN = 'qimen.taobao.erp.order.sync'
READS = frozenset({QIMEN, 'erp.salebill.list',
    'erp.ewms.stockoutbill.list', 'erp.ewms.stockoutbill.query',
    'erp.ewms.stockinbill.list',
    'zyx.ewms.stockinbill.gettasks', 'erp.ewms.stockinbill.getpostinfo', 'erp.bill.billcenterlist',
    'erp.atype.billlist',
    'beefun.gjssaleandbackbill.list', 'beefun.gjssaleandbackbilldetail.list',
    'beefun.deliver.getdeliverbilllist', 'beefun.deliver.getdeliverbilllistlimitinfo',
    'beefun.eshop.list', 'erp.bill.getsalebilllist', 'erp.bill.getsalebilldetails'})
REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/order-query-matrix.json'
ROW_KEYS = ('trade_orders', 'bills', 'orders', 'billdetails', 'eshopdeliverbills',
            'EShopDeliverBills', 'eshops', 'details')
IDENTIFIERS = frozenset({'trade_id', 'raw_no', 'number', 'tradeid', 'orderid',
                        'ordercode', 'realordercode', 'sourceid'})


class MatrixReader(AuditReader):
    def read(self, method, params):
        if method not in READS:
            raise ApiError('matrix_read_only_method_not_allowed')
        if {'appkey', 'method', 'token', 'timestamp', 'sign'} & params.keys():
            raise ApiError('reserved_parameters')
        if self.calls >= 48:
            raise ApiError('matrix_call_limit')
        for key in ('pageNo', 'pageno'):
            if key in params and not 1 <= int(params[key]) <= 25:
                raise ApiError('matrix_page_limit')
        for key in ('pageSize', 'pagesize'):
            if key in params and not 1 <= int(params[key]) <= 100:
                raise ApiError('matrix_page_size_limit')
        if method == 'erp.ewms.stockoutbill.query':
            ids = params.get('numbers') or params.get('sourceids') or params.get('vchcodes')
            if not isinstance(ids, list) or not ids or len(ids) > 200:
                raise ApiError('exact_query_requires_nonempty_identifiers')
            if params.get('vchcodes') and any(int(v) <= 0 for v in params['vchcodes']):
                raise ApiError('exact_query_requires_positive_vchcodes')
        self.calls += 1
        time.sleep(0.3)
        payload = {k: compact(v) if isinstance(v, (list, dict, bool)) else str(v)
                   for k, v in params.items()}
        payload.update(method=method, appkey=self.credentials['appkey'],
                       token=self.record['auth_token'],
                       timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        payload['sign'] = business_signature(payload, self.credentials['signkey'])
        request = urllib.request.Request(GATEWAY + '/allnewapi',
            data=urllib.parse.urlencode(payload).encode('utf-8'),
            headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'}, method='POST')
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ApiError('response_too_large')
            value = json.loads(raw)
        except ApiError:
            raise
        except Exception:
            raise ApiError('transport_or_response_error') from None
        if method == QIMEN:
            return unwrap(value)
        if not isinstance(value, dict):
            raise ApiError('unexpected_gateway_shape')
        if value.get('iserror') is not False:
            raise ApiError(error_category(value.get('errormessage')), numeric_code(value.get('errorcode')))
        return value.get('response')


def rows(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ApiError('unrecognized_record_collection')
    for key in ROW_KEYS:
        found = value.get(key)
        if isinstance(found, dict):
            found = found.get(key)
        if isinstance(found, list):
            return found
    raise ApiError('unrecognized_record_collection')


def analyze(value, by_order, datemode):
    counts, notes, matches, dates, schema = Counter(records=0), Counter(), Counter(), Counter(), Counter()
    matched = set()
    for row in rows(value):
        counts['records'] += 1
        if not isinstance(row, dict):
            counts['unexpected_record_type'] += 1
            continue
        ids = {str(v).strip() for path, v in leaves(row)
               if path.rsplit('.', 1)[-1] in IDENTIFIERS and str(v).strip() in by_order}
        matched.update(ids)
        if ids:
            counts['source_matching_records'] += 1
        local = [r for ident in ids for r in by_order[ident]]
        observed = trace(row, local, datemode)
        notes.update(observed['note_field_states'])
        matches.update(observed['note_value_match_paths'])
        dates.update(observed['date_value_matches_not_semantic_mapping'])
        schema.update(observed['nonempty_scalar_paths'])
    counts['distinct_matched_source_orders'] = len(matched)
    keys = sorted(value) if isinstance(value, dict) else []
    totals = {k: v for k, v in value.items() if k.lower() in ('totalresults', 'totalcount', 'total')
              and isinstance(v, (int, float))} if isinstance(value, dict) else {}
    return {'counts': dict(counts), 'response_keys': keys, 'reported_totals': totals,
            'note_states': dict(notes), 'source_note_matches': dict(matches),
            'date_day_equalities_not_due_mapping': dict(dates), 'nonempty_paths': dict(schema)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--focused', action='store_true', help='Continue the saved matrix within its total call limit')
    parser.add_argument('--deep', action='store_true', help='Validate nested Qimen models and date-matched WMS records')
    parser.add_argument('--validate', action='store_true', help='Validate zero identifiers and nested extension handling')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    now = datetime.now()
    end = now.strftime('%Y-%m-%d %H:%M:%S')
    begin = '2026-03-20 00:00:00'
    recent = (now - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00')
    report = {'checked_at': now.isoformat(), 'erp_writes': 0, 'production_files_modified': False,
              'call_limit': 48, 'cases': [],
              'not_guessed': ['tradeArc', 'tradeStatus', 'timeType', 'customerId', 'CreateTypes', 'ownerid'],
              'limitations': ['Samples are not complete synchronization',
                  'Different APIs cover different business stages',
                  'Matching a date value does not prove delivery-date semantics']}
    reader = MatrixReader()
    if args.focused or args.deep or args.validate:
        report = json.loads(REPORT.read_text(encoding='utf-8'))
        reader.calls = report['api_calls']
        report['focused_at'] = now.isoformat()
        # Clarify the old summary using its already-recorded detail traces, no fabricated zero.
        for item in report['cases']:
            if 'single_document_trace' in item:
                item['counts'] = {'collection_count_not_recorded_in_initial_run': True}
                item['summary_note'] = 'Use single_document_trace; initial generic collector omitted details.'
    local, meta = read_table(BASE / '销售订单统计列表数据.xls', {'订单编号', '商品编号', '单据备注', '到货日期'})
    by_order = {}
    for row in local:
        if row.get('订单编号') and row.get('商品编号'):
            by_order.setdefault(str(row['订单编号']).strip(), []).append(row)
    report['source'] = meta

    def save():
        report['api_calls'] = reader.calls
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def run(label, method, params):
        item = {'label': label, 'method': method}
        try:
            value = reader.read(method, params)
            try:
                item.update(status='responded', **analyze(value, by_order, meta['datemode']))
            except ApiError as error:
                item.update(status='responded_unrecognized_shape', category=error.category,
                            response_keys=sorted(value) if isinstance(value, dict) else [])
        except ApiError as error:
            item.update(status='error', category=error.category, code=error.code)
            value = None
        report['cases'].append(item)
        save()
        print(json.dumps({k: item[k] for k in ('label', 'status', 'category', 'code', 'counts',
                                              'reported_totals', 'source_note_matches') if k in item}), flush=True)
        return value

    def qcase(label, time_type=2, status=None, trade_ids=None, top=None, start=begin):
        props = {'profile_id': int(reader.record['profileid']), 'start_time': start, 'end_time': end}
        if time_type is not None:
            props['query_time_type'] = time_type
        if status is not None:
            props['bill_status'] = status
        if trade_ids:
            props['trade_id'] = ','.join(trade_ids)
        params = {'pageNo': '1', 'pageSize': '20', 'extendProps': compact(props)}
        params.update(top or {})
        return run(label, QIMEN, params)

    if args.validate:
        base = qcase('validation_qimen_nested_extensions', status='5')
        qr = rows(base)
        first = next(r for r in qr if str(r.get('trade_order', {}).get('trade_id')) in by_order)
        ident = str(first['trade_order']['trade_id'])
        extdetails = Counter()
        for r in qr:
            ext = expanded(r.get('trade_order', {}).get('extendProps'))
            value = ext.get('details')
            extdetails[type(value).__name__ + ':' + ('empty' if not value else 'nonempty')] += 1
        source_day = day(by_order[ident][0]['订货日期'], meta['datemode'])
        window = datetime.fromisoformat(source_day)
        response = run('validation_wms_identifier_fields', 'erp.ewms.stockoutbill.list',
            {'begintime': window.strftime('%Y-%m-%d 00:00:00'),
             'endtime': (window + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00'),
             'timetype': 0, 'pageno': 1, 'pagesize': 100})
        stats = Counter()
        for r in rows(response):
            stats['records'] += 1
            for field in ('vchcode', 'number', 'tradeid', 'sourceid'):
                v = r.get(field)
                state = 'missing' if field not in r else 'null' if v is None else 'empty' if v == '' else 'zero' if str(v) == '0' else 'nonzero'
                stats[field + ':' + state] += 1
                if str(v).strip() in by_order:
                    stats[field + ':matches_source_order'] += 1
        report['identifier_validation'] = {'wms_field_states': dict(stats), 'qimen_header_extension_details_states': dict(extdetails),
            'warning': 'Earlier exact-query result was overbroad; it does not prove lookup by source order number or vchcode works.'}
        save()
        print(json.dumps({'finished': True, 'api_calls': reader.calls, 'identifier_validation': report['identifier_validation']}), flush=True)
        return

    if args.deep:
        baseline = qcase('deep_qimen_model_validation', status='5')
        qr = rows(baseline)
        structural = {'order_container_type': type(baseline.get('trade_orders')).__name__,
                      'header_extension_types': Counter(), 'line_extension_types': Counter(),
                      'erp_process_states': Counter(), 'online_trade_states': Counter(),
                      'header_extension_fields': set(), 'line_extension_fields': set()}
        known = []
        for order in qr:
            head = order.get('trade_order', {})
            ext = expanded(head.get('extendProps'))
            structural['header_extension_types'][type(head.get('extendProps')).__name__] += 1
            structural['header_extension_fields'].update(ext)
            # These are bounded status enums, not customer values.
            for source, target in [(ext.get('process_state'), 'erp_process_states'),
                                   (head.get('trade_status'), 'online_trade_states')]:
                if str(source).isdigit() and 0 <= int(source) <= 600:
                    structural[target][str(source)] += 1
            ident = str(head.get('trade_id', ''))
            if ident in by_order:
                known.append(ident)
            lines = order.get('order_info', [])
            if isinstance(lines, dict):
                lines = lines.get('order_info', [])
            for line in lines:
                structural['line_extension_types'][type(line.get('extendProps')).__name__] += 1
                structural['line_extension_fields'].update(expanded(line.get('extendProps')))
        report['qimen_model_validation'] = {k: sorted(v) if isinstance(v, set) else dict(v) if isinstance(v, Counter) else v
                                            for k, v in structural.items()}
        save()
        if known:
            # Document says comma-separated IDs. Verify 2 single IDs vs their pair.
            qcase('deep_exact_first', trade_ids=known[:1], status='5')
            qcase('deep_exact_second', trade_ids=known[1:2], status='5')
            qcase('deep_exact_pair_documented_comma', trade_ids=known[:2], status='5')
            source_day = day(by_order[known[0]][0]['订货日期'], meta['datemode'])
            window = datetime.fromisoformat(source_day)
            params = {'begintime': window.strftime('%Y-%m-%d 00:00:00'),
                      'endtime': (window + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00'),
                      'timetype': 0, 'pageno': 1, 'pagesize': 100}
            found = run('deep_wms_same_order_date_p1', 'erp.ewms.stockoutbill.list', params)
            allrows = rows(found) if found is not None else []
            total = found.get('totalresults', 0) if isinstance(found, dict) else 0
            if isinstance(total, int):
                for page in range(2, min(3, (total + 99) // 100) + 1):
                    extra = run('deep_wms_same_order_date_p' + str(page), 'erp.ewms.stockoutbill.list', dict(params, pageno=page))
                    if extra is not None:
                        allrows.extend(rows(extra))
            if allrows:
                matched = [r for r in allrows if any(str(r.get(k, '')).strip() in by_order
                                                   for k in ('number', 'tradeid', 'sourceid'))]
                selected = (matched or allrows)[:3]
                run('deep_wms_exact_observed_vchcodes', 'erp.ewms.stockoutbill.query',
                    {'vchcodes': [int(r['vchcode']) for r in selected if r.get('vchcode')], 'status': 2})
                # If document number differs from original trade ID, use the observed number.
                run('deep_wms_exact_observed_numbers', 'erp.ewms.stockoutbill.query',
                    {'numbers': [str(r['number']) for r in selected if r.get('number')], 'status': 2})
        print(json.dumps({'finished': True, 'api_calls': reader.calls, 'model_validation': report['qimen_model_validation']}), flush=True)
        return

    if args.focused:
        base = qcase('focused_qimen_unshipped', status='5')
        known = [str(r.get('trade_order', {}).get('trade_id')) for r in rows(base)
                 if str(r.get('trade_order', {}).get('trade_id')) in by_order][:20]
        if known:
            qcase('focused_qimen_exact_single', trade_ids=known[:1], status='1,2,3,4,5,6,7')
            # Common-parameter documentation requires lowercase wire parameter names.
            exact = run('focused_wms_known_numbers_lowercase', 'erp.ewms.stockoutbill.query', {'numbers': known, 'status': 2})
            found = rows(exact) if exact is not None else []
            if found:
                vchcodes = [int(r['vchcode']) for r in found if r.get('vchcode')]
                if vchcodes:
                    run('focused_wms_same_vchcodes', 'erp.ewms.stockoutbill.query', {'vchcodes': vchcodes, 'status': 2})
                sourceids = [str(r['sourceid']) for r in found if r.get('sourceid')]
                if sourceids:
                    run('focused_wms_same_sourceids', 'erp.ewms.stockoutbill.query', {'sourceids': sourceids, 'status': 2})
                # Cross-check diverse orders, not only adjacent records from a query page.
                other = [n for n in sorted(by_order) if n not in known]
                mixed = other[::max(1, len(other) // 100)][:100]
                run('focused_wms_diverse_source_orders', 'erp.ewms.stockoutbill.query', {'numbers': mixed, 'status': 2})
        print(json.dumps({'finished': True, 'api_calls': reader.calls, 'report': str(REPORT)}), flush=True)
        return

    # Page 404 explicitly describes a query, including line comment.
    for full in (True, False):
        run('draft_broad_all' if full else 'draft_broad_shop', 'erp.salebill.list',
            {'begintime': begin, 'endtime': end, 'pageno': 1, 'pagesize': 100, 'showfullsalebill': full})

    allq = qcase('qimen_created_no_status')
    known = [str(r.get('trade_order', {}).get('trade_id')) for r in rows(allq)
             if str(r.get('trade_order', {}).get('trade_id')) in by_order][:3]
    qcase('qimen_created_all_states', status='1,2,3,4,5,6,7')
    for state in range(1, 8):
        qcase('qimen_created_state_' + str(state), status=str(state))
    qcase('qimen_shipped_no_status', time_type=1)
    qcase('qimen_shipped_states_6_7', time_type=1, status='6,7')
    qcase('qimen_omitted_time_type', time_type=None)
    qcase('qimen_with_top_date_range', top={'startDate': begin, 'endDate': end})
    if known:
        qcase('qimen_exact_known_all_states', trade_ids=known, status='1,2,3,4,5,6,7')
    shops = run('shop_registry', 'beefun.eshop.list', {})
    for index, shop in enumerate(rows(shops)[:3]):
        name = shop.get('fullname') or shop.get('name')
        if name:
            qcase('qimen_shop_' + str(index + 1), top={'shopName': name})

    for mode in (0, 1):
        run('wms_range_' + ('created' if mode == 0 else 'modified'), 'erp.ewms.stockoutbill.list',
            {'begintime': begin, 'endtime': end, 'timetype': mode, 'pageno': 1, 'pagesize': 20})
    if known:
        run('wms_exact_known_numbers', 'erp.ewms.stockoutbill.query', {'numbers': known, 'status': 2})
    for method, label in [('beefun.gjssaleandbackbill.list', 'gjs_posted_headers'),
                          ('beefun.gjssaleandbackbilldetail.list', 'gjs_posted_lines')]:
        run(label, method, {'begintime': recent, 'endtime': end, 'pageno': 1, 'pagesize': 20})
    for mode in (1, 2):
        run('limited_delivery_time_' + str(mode), 'beefun.deliver.getdeliverbilllistlimitinfo',
            {'querytimetype': mode, 'starttime': begin, 'endtime': end, 'pageno': 1, 'pagesize': 20,
             'billstatus': '1,2,3,4,5,6,7'})
    run('full_delivery_include_deleted', 'beefun.deliver.getdeliverbilllist',
        {'starttime': begin, 'endtime': end, 'pageno': 1, 'pagesize': 20, 'deleted': '1'})
    # Explicitly retain type -1. Inspect three source-matching posted details.
    sale_params = {'dlytype': -1, 'begintime': recent, 'endtime': end, 'pageno': 1, 'pagesize': 100}
    first = run('sales_all_types_first_page', 'erp.bill.getsalebilllist', sale_params)
    candidates = [b for b in rows(first) if str(b.get('number', '')).strip() in by_order]
    if not candidates and isinstance(first, dict):
        total = first.get('totalresults', 0)
        if isinstance(total, int) and 100 < total <= 2500:
            last = run('sales_all_types_last_page', 'erp.bill.getsalebilllist',
                       dict(sale_params, pageno=(total + 99) // 100))
            candidates = [b for b in rows(last) if str(b.get('number', '')).strip() in by_order]
    for index, bill in enumerate(candidates[:3]):
        result = run('posted_detail_' + str(index + 1), 'erp.bill.getsalebilldetails', {'vchcode': bill['vchcode']})
        if isinstance(result, dict):
            # Some detail responses are a single object, not a bills array.
            report['cases'][-1]['single_document_trace'] = trace(result, by_order[str(bill['number']).strip()], meta['datemode'])
            save()
    print(json.dumps({'finished': True, 'api_calls': reader.calls, 'report': str(REPORT)}), flush=True)


if __name__ == '__main__':
    main()
