"""One documented read-only bill-center query, with redacted field coverage output."""
from collections import Counter
from datetime import datetime
import json
import sys

from audit_order_sources import AuditReader, REPORT, BASE, read_table
from gateway import ApiError


def main():
    reader = AuditReader()
    now = datetime.now()
    result = reader.read('erp.bill.billcenterlist', {
        'groupcode': 'default', 'pageindex': 1, 'pagesize': 100,
        'overbegin': now.strftime('%Y-%m-%d 00:00:00'),
        'overend': now.strftime('%Y-%m-%d %H:%M:%S'),
        'showredbill': True, 'hascustom': True})
    bills = result.get('details', []) if isinstance(result, dict) else []
    if not isinstance(bills, list):
        raise ApiError('unexpected_bill_response')
    kinds = Counter((str(b.get('vchtype')), str(b.get('vchtypename'))) for b in bills)
    evidence = {'checked_at': now.isoformat(), 'method': 'erp.bill.billcenterlist',
                'groupcode': 'default', 'time_scope': 'today', 'page': 1, 'page_size': 100,
                'returned_count': len(bills), 'response_keys': sorted(result) if isinstance(result, dict) else [],
                'types': [{'code': k[0], 'name': k[1], 'count': v} for k, v in kinds.items()],
                'headers': sorted({k for b in bills for k in b}),
                'details': sorted({k for b in bills for d in b.get('details', []) for k in d}),
                'erp_writes': 0, 'not_proof_of_all_supported_types': True}
    source, _ = read_table(BASE / '销售订单统计列表数据.xls', {'订单编号', '商品编号', '未完成数量'})
    numbers = {str(r['订单编号']).strip() for r in source if r.get('订单编号') and r.get('商品编号')}
    references = [d.get('ordercode') for b in bills for d in b.get('details', [])]
    nonzero = [str(v).strip() for v in references if v not in (None, '', 0, '0')]
    evidence['detail_ordercode'] = {'total_fields': len(references), 'nonzero': len(nonzero),
                                  'matches_source_order_number': sum(v in numbers for v in nonzero),
                                  'all_numeric': all(v.isdigit() for v in nonzero)}
    ids = list(dict.fromkeys(v for v in nonzero if v.isdigit()))[:3]
    if ids:
        linked = reader.read('erp.bill.billcenterlist', {
            'groupcode': 'default', 'pageindex': 1, 'pagesize': 5,
            'overbegin': '2000-01-01 00:00:00', 'overend': now.strftime('%Y-%m-%d %H:%M:%S'),
            'vchcode': [int(v) for v in ids], 'showredbill': True, 'hascustom': True})
        rows = linked.get('details', []) if isinstance(linked, dict) else []
        evidence['observed_ordercode_lookup'] = {
            'requested_count': len(ids), 'returned_count': len(rows),
            'types': sorted({str(r.get('vchtypename')) for r in rows}),
            'source_order_number_matches': sum(str(r.get('number', '')).strip() in numbers for r in rows),
            'headers': sorted({k for r in rows for k in r}),
            'details': sorted({k for r in rows for d in r.get('details', []) for k in d}),
            'interpretation': 'ordercode meaning not documented; lookup is a read-only linkage check'}
    REPORT.with_name('order-query-discovery.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except ApiError as error:
        print(json.dumps({'status': 'stopped', 'category': error.category, 'code': error.code}))
        sys.exit(1)
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'type': type(error).__name__}))
        sys.exit(1)
