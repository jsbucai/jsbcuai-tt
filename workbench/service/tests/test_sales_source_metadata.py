from io import BytesIO

import pytest
from openpyxl import Workbook

from core.sales_source_parser import parse_excel, parse_text


def workbook(*rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    out = BytesIO()
    book.save(out)
    book.close()
    return out.getvalue()


def test_customer_part_only_order_retains_source_without_guessing_erp_code():
    raw = workbook(
        ['客户料号', '数量', '含税单价', '客户订单号', '项目号', '客户行号', '标准'],
        ['Part.A-012', 2, 1.13, 'PO-001', 'P#A', '007', 'GB/T 5782'],
    )
    lines, reports = parse_excel(raw, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                'att-test', 'a' * 64, '测试客户')
    assert len(lines) == 1
    line = lines[0]
    assert line['product_code'] == ''
    assert line['source_ref']['customer_part_no'] == 'Part.A-012'
    assert line['source_ref']['customer_order_no'] == 'PO-001'
    assert line['source_ref']['project_no'] == 'P#A'
    assert line['source_ref']['customer_line_no'] == '007'
    assert line['source_ref']['original_standard'] == 'GB/T 5782'
    assert line['source_ref']['match_basis'] == 'manual_code_required'
    assert line['tax_basis'] == 'tax_inclusive'
    assert any(r['reason'] == 'source_line_needs_match' and r['blocking'] for r in reports)


def test_erp_code_and_customer_part_are_independent_source_fields():
    raw = workbook(['商品编码', '客户料号', '商品名称', '数量'],
                   ['LS1000', 'LS-1000-A', '测试螺栓', 3])
    lines, _ = parse_excel(raw, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                          'att-test', 'a' * 64, '测试客户')
    assert lines[0]['product_code'] == 'LS1000'
    assert lines[0]['source_ref']['customer_part_no'] == 'LS-1000-A'
    assert lines[0]['source_ref']['match_basis'] == 'explicit_code'


def test_conflicting_customer_part_columns_are_not_silently_overwritten():
    raw = workbook(['客户料号', '图号', '数量'], ['Part-A', 'Part-B', 1])
    with pytest.raises(ValueError, match='表头字段重复'):
        parse_excel(raw, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'att-test', 'a' * 64, '测试客户')


def test_labeled_part_text_keeps_quantity_quote_and_requires_product_review():
    lines, reports = parse_text('客户料号=Part.A-012,2,1.13', 'message-test', 'a' * 64)
    assert lines[0]['product_code'] == ''
    assert lines[0]['quantity'] == '2' and lines[0]['unit_price'] == '1.13'
    assert lines[0]['source_ref']['customer_part_no'] == 'Part.A-012'
    assert reports[0]['blocking']


def test_name_spec_text_is_candidate_input_not_an_automatic_sku():
    lines, reports = parse_text('内六角圆柱头螺钉,M6*30,2,1.13', 'message-test', 'a' * 64)
    assert lines[0]['product_code'] == ''
    assert lines[0]['quantity'] == '2' and lines[0]['unit_price'] == '1.13'
    assert lines[0]['source_ref']['original_name'] == '内六角圆柱头螺钉'
    assert lines[0]['source_ref']['original_spec'] == 'M6*30'
    assert reports[0]['blocking']


def test_nonorder_sentence_is_preserved_without_guessed_quantity_or_price():
    lines, reports = parse_text('帮我看看这个商品应该用什么', 'message-test', 'a' * 64)
    assert lines[0]['product_code'] == lines[0]['quantity'] == lines[0]['unit_price'] == ''
    assert reports[0]['preview'] == '帮我看看这个商品应该用什么'
    assert reports[0]['blocking']
