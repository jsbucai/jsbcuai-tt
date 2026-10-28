"""Read all sales-outbound types in a bounded recent window; no ERP writes."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
import sys

from audit_order_sources import AuditReader, BASE, REPORT, read_table
from gateway import ApiError


def fetch_all_sales(reader, begin, end, page_size=100, max_pages=10):
    bills, seen, total = [], set(), None
    for page in range(1, max_pages + 1):
        response = reader.read('erp.bill.getsalebilllist', {
            'dlytype': -1, 'begintime': begin, 'endtime': end,
            'pageno': page, 'pagesize': page_size})
        if not isinstance(response, dict) or not isinstance(response.get('bills'), list):
            raise ApiError('sales_response_invalid')
        count = int(response['totalresults'])
        if count < 0 or count > max_pages * page_size:
            raise ApiError('audit_window_exceeds_limit')
        if total is not None and count != total:
            raise ApiError('sales_total_changed_during_paging')
        total = count
        rows = response['bills']
        if len(rows) > page_size:
            raise ApiError('sales_page_size_exceeded')
        for row in rows:
            identity = row.get('vchcode')
            if not isinstance(identity, (str, int)) or isinstance(identity, bool) or not str(identity):
                raise ApiError('sales_id_invalid')
            identity = str(identity)
            if identity in seen:
                raise ApiError('sales_duplicate_id')
            seen.add(identity)
            bills.append(row)
        if len(bills) == total:
            return bills, {'reported_total': total, 'unique_count': len(seen),
                           'pages': page, 'count_and_unique_id_check': 'passed',
                           'snapshot_consistency': 'not_guaranteed_by_api'}
        if len(bills) > total or not rows:
            raise ApiError('sales_page_coverage_incomplete')
    raise ApiError('sales_page_budget_exhausted')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    reader = AuditReader()
    now = datetime.now()
    begin = (now - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00')
    end = now.strftime('%Y-%m-%d %H:%M:%S')
    bills, pagination = fetch_all_sales(reader, begin, end)
    source, source_meta = read_table(BASE / '销售订单统计列表数据.xls', {'商品编号', '订单编号', '未完成数量'})
    source = [r for r in source if r.get('商品编号') and r.get('订单编号')]
    order_numbers = {str(r['订单编号']).strip() for r in source}
    raw_notes = {str(r['单据备注']) for r in source if r.get('单据备注')}
    detail_reports = []
    for bill in bills[:2]:
        response = reader.read('erp.bill.getsalebilldetails', {'vchcode': bill['vchcode']})
        if not isinstance(response, dict) or not isinstance(response.get('details'), list):
            raise ApiError('sales_detail_response_invalid')
        rows = response['details']
        detail_reports.append({'count': len(rows), 'reported_total': response.get('totalresults'),
                               'fields': sorted({k for row in rows for k in row})})
    result = {'checked_at': now.isoformat(), 'begin': begin, 'end': end, 'dlytype': -1,
              'business_flow': 'ordinary orders processed through online-store shipping UI; user confirmed',
              'pagination': pagination,
              'returned_types': dict(Counter(str(b.get('dlytype')) for b in bills)),
              'posted_count': sum(bool(b.get('overtime')) for b in bills),
              'direct_number_matches_unfinished_source': sum(str(b.get('number', '')).strip() in order_numbers for b in bills),
              'nonempty_note_exact_matches_source': sum(bool(b.get('comment')) and str(b.get('comment')) in raw_notes for b in bills),
              'header_fields': sorted({k for b in bills for k in b}), 'detail_samples': detail_reports,
              'source': source_meta, 'erp_writes': 0, 'api_calls': reader.calls,
              'full_unfinished_order_replacement_verified': False,
              'limitations': ['Recent sales-outbound documents; not all unfinished sales orders',
                              'Outbound document numbers need not equal original order numbers',
                              'Equal row count and unique IDs do not prove an atomic upstream snapshot']}
    path = REPORT.with_name('sales-all-types-audit.json')
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except ApiError as error:
        print(json.dumps({'status': 'stopped', 'category': error.category, 'code': error.code}))
        sys.exit(1)
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'type': type(error).__name__}))
        sys.exit(1)
