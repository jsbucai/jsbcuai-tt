"""Read-only WMS inbound / pending purchase comparison; schema and counts only."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from audit_query_matrix import MatrixReader, rows
from audit_order_sources import BASE, read_table, day, decimal
from audit_same_order_mapping import decoded_leaves
from gateway import ApiError

METHOD = 'erp.ewms.stockinbill.list'
REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/wms-purchase-pending.json'


def inspect(value, local):
    index = defaultdict(list)
    for row in local:
        if row.get('订单编号') and row.get('商品编号'):
            index[str(row['订单编号']).strip()].append(row)
    bills = rows(value)
    stats, states, types, header_fields, detail_fields, paths = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    note_states, number_matches, compare = Counter(), Counter(), Counter()
    for bill in bills:
        stats['bills'] += 1
        header_fields.update(bill.keys())
        types[str(bill.get('vchtype'))] += 1
        states[str(bill.get('processstatus'))] += 1
        source = index.get(str(bill.get('number', '')).strip(), [])
        if source:
            stats['source_order_number_matches'] += 1
        for path, val in decoded_leaves(bill):
            if val not in (None, ''):
                paths[path] += 1
        for detail in bill.get('details') or []:
            stats['detail_rows'] += 1
            detail_fields.update(detail.keys())
            # Only count equality when an actual code field and source order both match.
            candidates = [(field, row) for field in ('usercode', 'pusercode', 'ptypeusercode', 'x_code')
                          for row in source if detail.get(field) and str(detail[field]).strip() == str(row['商品编号']).strip()]
            if not candidates:
                continue
            field, row = candidates[0]
            number_matches[field] += 1
            compare['same_order_product_lines'] += 1
            for qty_field in ('qty', 'assqty', 'leftqty', 'remainqty', 'receivedqty', 'finishqty'):
                if detail.get(qty_field) is None:
                    continue
                try:
                    qty = decimal(detail[qty_field])
                except Exception:
                    continue
                for source_field in ('订货数量', '完成数量', '终止完成数量', '未完成数量'):
                    if row.get(source_field) not in (None, '') and qty == decimal(row[source_field]):
                        compare[qty_field + ':equals_' + source_field] += 1
                if decimal(row.get('完成数量') or 0) > 0 and decimal(row.get('未完成数量') or 0) > 0:
                    compare['partial_source_line_' + qty_field + '_observed'] += 1
                    if qty == decimal(row['未完成数量']):
                        compare['partial_source_line_' + qty_field + '_equals_remaining'] += 1
    return {'response_keys': sorted(value) if isinstance(value, dict) else [],
            'reported_total': value.get('totalresults') if isinstance(value, dict) else None,
            'counts': dict(stats), 'vchtypes': dict(types), 'processstatuses': dict(states),
            'header_fields': sorted(header_fields), 'detail_fields': sorted(detail_fields),
            'nonempty_paths': dict(paths), 'source_code_fields': dict(number_matches),
            'quantity_equalities_not_yet_unit_semantics': dict(compare)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--followup', action='store_true', help='Continue existing audit within its 12-call limit')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    reader = MatrixReader()
    local, meta = read_table(BASE / '近六个月进货订单统计列表数据.xls', {'订单编号', '商品编号', '未完成数量'})
    now = datetime.now()
    begin, end = '2026-03-01 00:00:00', now.strftime('%Y-%m-%d %H:%M:%S')
    report = {'checked_at': now.isoformat(), 'erp_writes': 0, 'production_files_modified': False,
              'source': meta, 'cases': [], 'call_limit': 12}
    if args.followup:
        report = json.loads(REPORT.read_text(encoding='utf-8'))
        reader.calls = report['api_calls']
        report['followup_at'] = now.isoformat()
        report['followup_source'] = meta
    def run(label, params, method=METHOD):
        case = {'label': label, 'method': method,
                'time_type': params.get('timetype'), 'filter_statuses': params.get('processstatus'),
                'query_window': {k:v for k,v in params.items() if k in ('begintime','endtime','overbegin','overend')}}
        if reader.calls >= 12:
            raise ApiError('purchase_probe_call_limit')
        try:
            value = reader.read(method, params)
            if method == 'zyx.ewms.stockinbill.gettasks':
                taskids = value.get('taskids') if isinstance(value, dict) else None
                case.update(status='responded', response_keys=sorted(value) if isinstance(value, dict) else [],
                            task_count=len(taskids) if isinstance(taskids,list) else None,
                            reported_total=value.get('totalresults') if isinstance(value,dict) else None)
            else:
                case.update(status='responded', **inspect(value, local))
        except ApiError as error:
            case.update(status='error', category=error.category, code=error.code)
            value = None
        report['cases'].append(case)
        report['api_calls'] = reader.calls
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k:v for k,v in case.items() if k not in ('nonempty_paths',)},ensure_ascii=True), flush=True)
        return value
    base = {'begintime': begin, 'endtime': end, 'pageno': 1, 'pagesize': 100, 'timetype': 0}
    if args.followup:
        run('wide_created_no_status', dict(base,begintime='1900-01-01 00:00:00',endtime='2100-01-01 00:00:00'))
        run('wide_delivery_no_status', dict(base,begintime='1900-01-01 00:00:00',endtime='2100-01-01 00:00:00',timetype=1))
        run('wms_receiving_tasks', {'begintime':begin,'endtime':end,'pageno':1,'pagesize':100}, 'zyx.ewms.stockinbill.gettasks')
        run('billcenter_documented_purchase_type_7',
            {'pageindex':1,'pagesize':100,'overbegin':begin,'overend':end,'vchtype':[7],
             'showredbill':True,'hascustom':True},'erp.bill.billcenterlist')
        print(json.dumps({'finished':True,'api_calls':reader.calls,'report':str(REPORT)}),flush=True)
        return
    run('created_pending_statuses_0_1_2', dict(base, processstatus=[0,1,2]))
    run('created_no_status_filter', base)
    run('delivery_time_pending_statuses_0_1_2', dict(base, timetype=1, processstatus=[0,1,2]))
    valid = [r for r in local if r.get('订单编号') and r.get('商品编号') and decimal(r.get('未完成数量') or 0) > 0]
    recent = sorted(valid, key=lambda r: day(r['订货日期'],meta['datemode']), reverse=True)
    selected = []
    for partial in (True, False):
        item = next((r for r in recent if (decimal(r.get('完成数量') or 0) > 0) == partial), None)
        if item:
            selected.append(item)
    for idx, item in enumerate(selected):
        run('known_' + ('partial' if decimal(item.get('完成数量') or 0)>0 else 'unreceived') + '_order',
            dict(base, number=str(item['订单编号']).strip()))
    print(json.dumps({'finished':True,'api_calls':reader.calls,'report':str(REPORT)}),flush=True)


if __name__ == '__main__':
    main()
