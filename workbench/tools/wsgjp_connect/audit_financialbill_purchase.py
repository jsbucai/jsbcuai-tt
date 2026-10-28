"""Read page-644 financial bills for known purchase/receipt controls, max 3 calls.

Only query the named documents; do not save or output financial amounts, contacts,
notes, credentials or raw responses. This does not change production clients.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re

from audit_query_matrix import MatrixReader
from audit_order_sources import BASE, read_table, day, decimal
from audit_same_order_mapping import decoded_leaves
from gateway import ApiError

METHOD = 'erp.atype.billlist'
REPORT = Path(__file__).resolve().parents[2] / 'runtime/wsgjp-connect/financialbill-purchase.json'


def case_value(obj, name):
    keys = [k for k in obj if k.casefold() == name.casefold()]
    if len(keys) != 1:
        raise ApiError('ambiguous_or_missing_' + name.lower())
    return obj[keys[0]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    if not parser.parse_args().run:
        parser.print_help()
        return
    reader = MatrixReader()
    now = datetime.now()
    purchases, pm = read_table(BASE/'近六个月进货订单统计列表数据.xls', {'订单编号','商品编号','未完成数量'})
    receipts, rm = read_table(BASE/'进货明细表数据.xls', {'单据编号','商品编号','单据类型'})
    pending = sorted([r for r in purchases if r.get('订单编号') and r.get('商品编号') and decimal(r.get('未完成数量') or 0)>0],
                     key=lambda r: day(r['订货日期'],pm['datemode']), reverse=True)
    partial = next(r for r in pending if decimal(r.get('完成数量') or 0)>0)
    untouched = next(r for r in pending if decimal(r.get('完成数量') or 0)==0)
    receipt = max((r for r in receipts if r.get('单据编号') and r.get('商品编号') and r.get('单据类型')=='进货入库单'),
                  key=lambda r: day(r['单据日期'],rm['datemode']))
    selections = [('unreceived_purchase', 7, str(untouched['订单编号']).strip()),
                  ('partially_received_purchase', 7, str(partial['订单编号']).strip()),
                  ('posted_receipt_control', 34, str(receipt['单据编号']).strip())]
    report = {'checked_at':now.isoformat(),'method':METHOD,'documentation_page':644,'erp_writes':0,
              'raw_response_saved':False,'production_files_modified':False,
              'source_modified_at':{'purchases':pm['modified_at'],'receipts':rm['modified_at']},'cases':[]}
    for label, kind, number in selections:
        item = {'label':label,'requested_vchtype':kind,'exact_number_filter':True}
        try:
            if reader.calls >= 3:
                raise ApiError('financial_probe_call_limit')
            value = reader.read(METHOD, {'starttime':'2000-01-01 00:00:00',
                 'endtime':now.strftime('%Y-%m-%d %H:%M:%S'),'vchtype':kind,'number':number})
            if not isinstance(value,dict):
                raise ApiError('unexpected_financial_response')
            bills = case_value(value,'Bills')
            if not isinstance(bills,list):
                raise ApiError('unexpected_financial_bills')
            head_fields, line_fields, paths = set(),set(),set()
            same = 0
            linecount = 0
            for bill in bills:
                head_fields.update(bill)
                same += str(case_value(bill,'Number')).strip()==number
                details = case_value(bill,'Details')
                if not isinstance(details,list):
                    raise ApiError('unexpected_financial_details')
                for line in details:
                    line_fields.update(line)
                    linecount += 1
                paths.update(path for path,_ in decoded_leaves(bill))
            item.update(status='responded',response_keys=sorted(value),returned_bills=len(bills),
                exact_number_matches=same,returned_detail_lines=linecount,header_fields=sorted(head_fields),
                detail_fields=sorted(line_fields),quantity_or_product_named_paths=sorted(p for p in paths
                    if re.search(r'qty|quantity|count|ptype|product|goods|remain|unfinish|received',p,re.I)),
                all_scalar_paths=sorted(paths))
        except ApiError as error:
            item.update(status='error',category=error.category,code=error.code)
        report['cases'].append(item)
        print(json.dumps({k:v for k,v in item.items() if k!='all_scalar_paths'},ensure_ascii=True),flush=True)
    report['api_calls']=reader.calls
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
