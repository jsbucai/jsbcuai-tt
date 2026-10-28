"""Independently read the prepared two-line XLS; never upload or change it."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path

import xlrd

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'outputs/evidence/sales-two-line-trial-20261007'
HEADERS = ('录单日期', '单据编号', '单据类型', '往来单位编号', '往来单位名称',
           '部门名称', '摘要', '附加说明', '制单人名称', '经手人名称', '仓库名称',
           '收付款账户编号', '收付款账户金额', '交货日期', '商品条码', '商品编号',
           '商品名称', '明细仓库名称', '单位', '数量', '折前单价', '折前金额',
           '折扣', '税率(%)', '明细备注', '副单位数量', '是否赠品')


def main():
    file = SOURCE / 'review-order.xls'
    before = file.read_bytes()
    reviewed = json.loads((SOURCE / 'review.json').read_text())
    digest = sha256(before).hexdigest()
    assert digest == reviewed['file_sha256']
    book = xlrd.open_workbook(file_contents=before, formatting_info=True)
    assert book.sheet_names() == ['单据导入模板', '单据类型']
    sheet = book.sheet_by_index(0)
    assert sheet.ncols == 27 and tuple(sheet.row_values(0)) == HEADERS
    assert sheet.nrows == len(reviewed['lines']) + 1 == 3
    observed = []
    net_total = Decimal('0')
    for index, original in enumerate(reviewed['lines'], 1):
        values = {}
        for column, label in enumerate(HEADERS):
            cell = sheet.cell(index, column)
            values[label] = (xlrd.xldate_as_datetime(cell.value, book.datemode).date().isoformat()
                             if cell.ctype == xlrd.XL_CELL_DATE else cell.value)
        assert values['单据类型'] == '销售订单'
        for column, expected in [('单据编号', reviewed['order_number']),
                                  ('往来单位名称', reviewed['customer']),
                                  ('制单人名称', reviewed['maker']),
                                  ('经手人名称', reviewed['handler']),
                                  ('仓库名称', reviewed['warehouse']),
                                  ('明细仓库名称', reviewed['warehouse']),
                                  ('单位', '支'), ('交货日期', reviewed['delivery_date']),
                                  ('摘要', reviewed['summary']), ('商品编号', original['sku'])]:
            assert values[column] == expected, (index, column)
        assert values['录单日期'] == '2026-10-07'
        qty = Decimal(str(values['数量']))
        net = Decimal(str(values['折前单价']))
        amount = Decimal(str(values['折前金额']))
        # Independent arithmetic uses the reviewed 1.13 inclusive quotation,
        # without importing the application's pricing or rendering functions.
        expected_net = (Decimal('1.13') / Decimal('1.13')).quantize(Decimal('.001'), ROUND_HALF_UP)
        expected_amount = (qty * expected_net).quantize(Decimal('.01'), ROUND_HALF_UP)
        assert qty == Decimal(original['quantity'])
        assert net == expected_net == Decimal(original['untaxed_unit_price'])
        assert amount == expected_amount == Decimal(original['untaxed_amount'])
        assert Decimal(str(values['税率(%)'])) == 13
        detail = values['明细备注']
        parts = [part.strip() for part in detail.split('#')]
        assert len(parts) == 10 and parts[2] == str(index)
        assert parts[5] == 'M20X80' and parts[8] == original['quantity'] and parts[9] == original['sku']
        assert reviewed['summary'] == reviewed['lines'][0]['detail_note'] + '#交期=' + reviewed['delivery_date']
        assert len(values['摘要'].encode('utf-16-le')) // 2 <= 180
        assert values['附加说明'] and values['附加说明'] != values['摘要']
        net_total += amount
        observed.append({'row': index + 1, 'sku': values['商品编号'],
                         'quantity': format(qty, 'f'), 'unit': values['单位'],
                         'net_unit_price': format(net.quantize(Decimal('.001')), 'f'),
                         'net_amount': format(amount.quantize(Decimal('.01')), 'f'),
                         'delivery_date': values['交货日期']})
    assert observed[0]['sku'] == observed[1]['sku']
    assert [Decimal(row['quantity']) for row in observed] == [Decimal(1), Decimal(2)]
    assert net_total == Decimal('3.00')
    gross = (net_total * Decimal('1.13')).quantize(Decimal('.01'), ROUND_HALF_UP)
    assert gross == Decimal(reviewed['taxed_total']) == Decimal('3.39')
    assert file.read_bytes() == before
    result = {'observed_at_local': datetime.now().astimezone().isoformat(),
              'status': 'table_verified_only_not_uploaded', 'source': str(file),
              'source_sha256': digest, 'source_unchanged': True,
              'sheets': book.sheet_names(), 'range': '单据导入模板!A1:AA3',
              'rows': observed, 'net_total': '3.00', 'gross_total': '3.39',
              'duplicate_sku_rows_preserved': True,
              'completion_basis_selected': 'web_import_confirmation',
              'erp_upload_performed': False, 'erp_readback_performed': False,
              'additional_sample_upload': 'cancelled_by_current_user_instruction'}
    (SOURCE / 'file-only-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
