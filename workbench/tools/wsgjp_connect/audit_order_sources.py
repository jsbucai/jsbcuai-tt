"""Bounded, read-only source audit. Never writes ERP or production app data.

Run explicitly with --run. Outputs field coverage and comparison counts only;
credentials, customer details, order numbers and raw API bodies are not saved.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sys
import time

from gateway import ApiError, business_signature, compact, post
from vault import Vault

METHODS = frozenset({'erp.freight.list', 'erp.bill.billcenterlist',
                     'beefun.deliver.getdeliverbilllist',
                     'beefun.deliver.getdeliverbilllistlimitinfo',
                     'erp.bill.getsalebilllist', 'erp.bill.getsalebilldetails',
                     'beefun.eshop.list', 'beefun.gjsstockbilldetail.list'})
BASE = Path(r'E:\UL\ERP自动化\每日更新数据')
REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/order-source-audit.json'


def read_table(path, required):
    # xlrd is pure Python. No Excel/COM launch or workbook recalculation.
    try:
        import xlrd
    except ImportError:
        import os
        sys.path.append(str(Path(os.environ['APPDATA']) / 'Python/Python310/site-packages'))
        import xlrd
    blob = path.read_bytes()
    book = xlrd.open_workbook(file_contents=blob, logfile=io.StringIO())
    sheet = book.sheet_by_index(0)
    rows = getattr(sheet, '_cell_values', [])
    for index, row in enumerate(rows[:30]):
        if required.issubset(row):
            headers = [str(v).strip() for v in row]
            records = [dict(zip(headers, r)) for r in rows[index + 1:]
                       if any(v not in ('', None) for v in r)]
            return records, {'file': path.name, 'sha256': hashlib.sha256(blob).hexdigest(),
                             'modified_at': datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                             'headers': [h for h in headers if h], 'datemode': book.datemode}
    raise ValueError('source_headers_missing')


def decimal(value):
    return Decimal(str(value))


def day(value, datemode=0):
    if isinstance(value, (float, int)):
        import xlrd
        return xlrd.xldate_as_datetime(value, datemode).date().isoformat()
    return str(value).strip().replace('/', '-')[:10]


class AuditReader:
    def __init__(self):
        self.credentials = Vault().load('credentials')
        self.record = Vault().load('token')
        if self.record.get('expires_at', 0) <= time.time() + 60:
            raise ApiError('token_expired')
        if self.record.get('appkey') != self.credentials.get('appkey'):
            raise ApiError('account_mismatch')
        self.calls = 0

    def read(self, method, params):
        if method not in METHODS:
            raise ApiError('audit_read_only_method_not_allowed')
        if {'appkey', 'method', 'token', 'timestamp', 'sign'} & params.keys():
            raise ApiError('reserved_parameters')
        if self.calls >= 12:
            raise ApiError('audit_call_limit')
        self.calls += 1
        payload = {k: compact(v) if isinstance(v, (list, dict, bool)) else str(v)
                   for k, v in params.items()}
        payload.update(method=method, appkey=self.credentials['appkey'],
                       token=self.record['auth_token'],
                       timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        payload['sign'] = business_signature(payload, self.credentials['signkey'])
        return post('/allnewapi', payload)


def attempt(reader, method, params):
    try:
        result = reader.read(method, params)
        return result, {'status': 'responded', 'response_type': type(result).__name__,
                        'response_keys': sorted(result) if isinstance(result, dict) else []}
    except ApiError as error:
        return None, {'status': 'error', 'category': error.category, 'code': error.code}


def compare_receipts(bills, local_rows, datemode, exported_at):
    local = defaultdict(lambda: Decimal(0))
    for row in local_rows:
        if row.get('商品编号') and row.get('单据类型') in {'进货入库单', '进货退货单'}:
            key = (str(row.get('单据编号', '')).strip(), str(row['商品编号']).strip(),
                   str(row.get('仓库', '')).strip(), day(row.get('单据日期'), datemode))
            local[key] += decimal(row['数量'])
    local_docs = {key[0] for key in local}
    comparison = Counter()
    line_keys = set()
    for bill in bills:
        comparison['documents'] += 1
        remote = defaultdict(lambda: Decimal(0))
        red = bool(bill.get('redold') or bill.get('redword'))
        if red:
            comparison['red_flag_documents'] += 1
        for row in bill.get('details', []):
            line_keys.update(row)
            comparison['api_lines'] += 1
            key = (str(bill.get('number', '')).strip(), str(row.get('usercode', '')).strip(),
                   str(row.get('kfullname') or bill.get('kfullname') or '').strip(),
                   str(bill.get('date', ''))[:10])
            remote[key] += decimal(row['qty'])
        for key, qty in remote.items():
            if key not in local:
                comparison['not_found_in_export'] += 1
                after = str(bill.get('overtime', '')).replace(' ', 'T') > exported_at
                if key[0] not in local_docs:
                    comparison['missing_document_posted_after_export' if after else 'missing_document_other'] += 1
                else:
                    comparison['document_exists_but_line_key_differs'] += 1
            elif qty == local[key]:
                comparison['quantity_equal'] += 1
            else:
                comparison['quantity_different'] += 1
    return {'counts': dict(comparison), 'line_fields': sorted(line_keys),
            'comparison_key': ['document_number', 'product_code', 'warehouse_name', 'business_date'],
            'limitations': ['Read-only sample, not full-period coverage',
                            'API and export capture times differ',
                            'Red reversal/return sign semantics require separate verification']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    reader = AuditReader()
    now = datetime.now()
    end = now.strftime('%Y-%m-%d %H:%M:%S')
    sales, sales_meta = read_table(BASE / '销售订单统计列表数据.xls', {'订单编号', '商品编号', '未完成数量'})
    receipts, receipt_meta = read_table(BASE / '进货明细表数据.xls', {'单据编号', '商品编号', '数量', '单据类型'})
    purchases, purchase_meta = read_table(BASE / '近六个月进货订单统计列表数据.xls', {'订单编号', '商品编号', '未完成数量'})
    valid_sales = [r for r in sales if r.get('商品编号') and r.get('订单编号') and decimal(r.get('未完成数量', 0)) > 0]
    sample_orders = []
    for row in sorted(valid_sales, key=lambda r: (day(r.get('订货日期'), sales_meta['datemode']), str(r['订单编号'])), reverse=True):
        order = str(row['订单编号']).strip()
        if order not in sample_orders:
            sample_orders.append(order)
        if len(sample_orders) == 3:
            break
    report = {'checked_at': now.isoformat(), 'erp_writes': 0, 'production_files_modified': False,
              'sources': [sales_meta, purchase_meta, receipt_meta],
              'sales_export': {'unfinished_lines': len(valid_sales),
                               'unfinished_orders': len({str(r['订单编号']) for r in valid_sales}),
                               'selected_order_count': len(sample_orders)},
              'full_api_ready': False}
    freight, status = attempt(reader, 'erp.freight.list', {})
    if isinstance(freight, dict):
        records = freight.get('freights', [])
        codes = Counter(r.get('usercode') for r in records)
        status.update(count=len(records), duplicate_code_count=sum(v > 1 for v in codes.values()),
                      mapping=[{'name': r.get('fullname'), 'code': r.get('usercode')} for r in records])
    report['freight_dictionary'] = status
    response, status = attempt(reader, 'erp.bill.billcenterlist', {
        'pageindex': 1, 'pagesize': 20, 'overbegin': (now - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00'),
        'overend': end, 'vchtype': [34], 'showredbill': True, 'hascustom': True})
    if isinstance(response, dict) and isinstance(response.get('details'), list):
        bills = response['details']
        status.update(compare_receipts(bills, receipts, receipt_meta['datemode'], receipt_meta['modified_at']))
        status['header_fields'] = sorted({k for b in bills for k in b})
        status['returned_bill_types'] = sorted({b.get('vchtype') for b in bills})
    report['receipt_sample'] = status
    for label, orders in [('selected_orders', sample_orders)]:
        if not orders:
            continue
        response, status = attempt(reader, 'beefun.deliver.getdeliverbilllist', {
            'starttime': '2026-03-01 00:00:00', 'endtime': end, 'pageno': 1, 'pagesize': 10,
            'tradeid': ';'.join(orders), 'deleted': '1'})
        if isinstance(response, dict):
            bills = response.get('eshopdeliverbills', response.get('EShopDeliverBills', []))
            if isinstance(bills, list):
                status.update(returned_count=len(bills), total=response.get('totalresults'),
                              header_fields=sorted({k for b in bills for k in b}))
                matched = [b for b in bills if str(b.get('tradeid', b.get('TradeId'))) in orders]
                status['exact_order_matches'] = len(matched)
                status['remark_comparison'] = 'pending_field_mapping' if matched else 'no_matching_order'
        report[label] = status
    if sample_orders:
        response, status = attempt(reader, 'beefun.deliver.getdeliverbilllistlimitinfo', {
            'pageno': 1, 'pagesize': 10, 'tradeid': ';'.join(sample_orders)})
        if isinstance(response, dict):
            bills = response.get('eshopdeliverbills', [])
            if isinstance(bills, list):
                status.update(returned_count=len(bills), total=response.get('totalresults'),
                              header_fields=sorted({k for b in bills for k in b}),
                              detail_fields=sorted({k for b in bills for row in b.get('details', []) for k in row}),
                              exact_order_matches=sum(str(b.get('tradeid')) in sample_orders for b in bills))
        report['selected_orders_nonsensitive'] = status
        if status.get('status') == 'responded' and status.get('returned_count') == 0:
            response, broad_status = attempt(reader, 'beefun.deliver.getdeliverbilllistlimitinfo', {
                'pageno': 1, 'pagesize': 5, 'querytimetype': 2,
                'starttime': (now - timedelta(days=7)).strftime('%Y-%m-%d 00:00:00'), 'endtime': end})
            if isinstance(response, dict):
                bills = response.get('eshopdeliverbills', [])
                broad_status.update(returned_count=len(bills), total=response.get('totalresults'),
                                    header_fields=sorted({k for b in bills for k in b}))
            report['recent_deliveries_nonsensitive'] = broad_status
    report['api_calls'] = reader.calls
    report['unverified'] = ['sales_order_unfinished_qty_due_date_termination',
                            'purchase_order_pending_qty_due_date_termination',
                            'raw_remark_read_and_system_remark_write_mapping',
                            'full_pagination_and_red_return_semantics', 'order_freight_write_and_readback']
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except ApiError as error:
        print(json.dumps({'status': 'stopped', 'category': error.category, 'code': error.code}))
        sys.exit(1)
    except Exception as error:
        # Avoid raw exception messages potentially containing business data.
        print(json.dumps({'status': 'stopped', 'type': type(error).__name__}))
        sys.exit(1)
