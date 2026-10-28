"""One documented read query, comparing only keys in memory. No ERP writes."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path

from audit_order_sources import AuditReader, BASE, attempt, read_table, decimal

METHOD = 'beefun.gjsstockbilldetail.list'
REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/purchase-source-audit.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    if not parser.parse_args().run:
        parser.print_help()
        return
    now = datetime.now()
    params = {'begintime': (now - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00'),
              'endtime': now.strftime('%Y-%m-%d %H:%M:%S'), 'pageno': 1, 'pagesize': 20}
    report = {'checked_at': now.isoformat(), 'method': METHOD, 'erp_writes': 0,
              'production_files_modified': False, 'query': params}
    try:
        reader = AuditReader()
        receipts, receipt_meta = read_table(BASE / '进货明细表数据.xls', {'单据编号', '商品编号', '数量', '单据类型'})
        purchases, purchase_meta = read_table(BASE / '近六个月进货订单统计列表数据.xls', {'订单编号', '商品编号', '未完成数量'})
        report['local_sources'] = [receipt_meta, purchase_meta]
        receipt_keys = {(str(r.get('单据编号') or '').strip(), str(r.get('商品编号') or '').strip()) for r in receipts if r.get('商品编号')}
        purchase_keys = {(str(r.get('订单编号') or '').strip(), str(r.get('商品编号') or '').strip()) for r in purchases if r.get('商品编号')}
        response, status = attempt(reader, METHOD, params)
        report['result'] = status
        if isinstance(response, dict) and isinstance(response.get('billdetails'), list):
            rows = response['billdetails']
            if any(not isinstance(r, dict) for r in rows):
                raise ValueError('unexpected_row_shape')
            keys = [(str(r.get('number') or '').strip(), str(r.get('pusercode') or '').strip()) for r in rows]
            status.update(
                reported_total=response.get('totalresults') if isinstance(response.get('totalresults'), int) else None,
                sample_rows=len(rows), fields=sorted({key for row in rows for key in row}),
                nonempty_field_counts=dict(Counter(k for row in rows for k, v in row.items() if v not in (None, ''))),
                matched_receipt_document_product_pairs=sum(key in receipt_keys for key in keys),
                matched_purchase_order_product_pairs=sum(key in purchase_keys for key in keys),
                qty_positive_rows=sum(decimal(r['qty']) > 0 for r in rows if r.get('qty') is not None),
                qty_negative_rows=sum(decimal(r['qty']) < 0 for r in rows if r.get('qty') is not None),
                notes=['Key presence comparison only, not quantity reconciliation',
                       'Local export and API capture times differ',
                       'One page sample does not establish full coverage'])
    except Exception as error:
        report['local_error'] = type(error).__name__
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    history = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else []
    history.append(report)
    REPORT.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
