"""Local BIFF8 generation only; these tests never connect to an ERP."""
from dataclasses import asdict, replace
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

import pytest
import xlrd
import xlwt

from core.sales_web_import import (EXPECTED_HEADERS, WebImportConfig,
                                   build_sales_web_import_xls, catalog_spec_dimension)
from core.sales_web_transport import SalesWebImportTransport, WebImportSpec
from core.rules import note_delivery_date, parse_note
from core.store import dumps


def frozen_session():
    draft = {
        'customer_name': '客户甲', 'customer_order_no': 'PO-123',
        'delivery_date': '2026-10-15', 'note': '客户已确认',
        'tax_basis': 'tax_inclusive', 'tax_rate': '13', 'issues': [],
        'lines': [{
            'product_code': 'LS1000', 'product_name': '六角头螺栓',
            'quantity': '3', 'unit_price': '1.13', 'quoted_unit_price': '1.13',
            'untaxed_unit_price': '1.000', 'untaxed_line_amount': '3.00',
            'tax_basis': 'tax_inclusive', 'tax_rate': '13',
            'delivery_date': '2026-10-16', 'unit': '支', 'warehouse': '08390',
            'spec': 'M20*80', 'model': '8.8级特殊热浸锌',
            'catalog_summary': {'spec': 'M20*80', 'model': '8.8级特殊热浸锌',
                                'grade': '', 'surface': ''},
            'summary_dimension': 'M20X80',
            'summary_dimension_source': 'catalog_spec_shape',
            'resolved_identity': {'profile_id': 'P', 'warehouse_id': '08390',
                                  'product_id': 'p1', 'sku_id': 's1', 'base_unit': '1'},
            'issues': [],
        }],
    }
    payload = {'profile_id': 'P', 'customer_name': '客户甲',
               'fixed_sales_owner_id': 'sales', 'data_version': 'v1',
               'draft': draft, 'version': 3}
    return {'status': 'confirmed', 'profile_id': 'P', 'customer_name': '客户甲',
            'version': 3, 'confirmed_version': 3,
            'confirmed_hash': sha256(dumps(payload).encode()).hexdigest(),
            'confirmed_payload': dumps(payload), 'draft': dumps(draft)}


@pytest.fixture
def config(tmp_path):
    template = tmp_path / 'blank.xls'
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet('单据导入模板')
    for index, header in enumerate(EXPECTED_HEADERS):
        sheet.write(0, index, header)
    types = workbook.add_sheet('单据类型')
    types.write(0, 0, '销售订单')
    workbook.save(str(template))
    return WebImportConfig(template, tmp_path / 'one-row.xls', 'UL261007123456ABCDEF',
                           date(2026, 10, 7), 'P', 'v1', 'sales', '销售部',
                           '制单员甲', '经手人乙', '08390', '仓库丙')


def cell(sheet, header):
    return sheet.cell_value(1, EXPECTED_HEADERS.index(header))


def freeze_draft(session, draft):
    payload = json.loads(session['confirmed_payload'])
    payload['draft'] = draft
    session['confirmed_payload'] = dumps(payload)
    session['draft'] = dumps(draft)
    session['confirmed_hash'] = sha256(dumps(payload).encode()).hexdigest()


def test_one_confirmed_line_keeps_template_fields_dates_unit_and_summary(config):
    session = frozen_session()
    result = build_sales_web_import_xls(session, config)
    assert result.path == config.destination
    assert result.sha256 == sha256(config.destination.read_bytes()).hexdigest()
    assert result.confirmed_hash == session['confirmed_hash']
    assert config.destination.stat().st_mode & 0o777 == 0o600
    book = xlrd.open_workbook(str(config.destination), formatting_info=True)
    sheet = book.sheet_by_index(0)
    assert book.sheet_names() == ['单据导入模板', '单据类型']
    assert (sheet.nrows, sheet.ncols) == (2, 27)
    assert tuple(sheet.row_values(0)) == EXPECTED_HEADERS
    assert cell(sheet, '往来单位名称') == '客户甲'
    assert cell(sheet, '制单人名称') == '制单员甲'
    assert cell(sheet, '经手人名称') == '经手人乙'
    assert cell(sheet, '仓库名称') == cell(sheet, '明细仓库名称') == '仓库丙'
    assert cell(sheet, '附加说明') == '客户已确认'
    assert cell(sheet, '商品编号') == 'LS1000'
    assert cell(sheet, '商品名称') == '六角头螺栓'
    assert cell(sheet, '单位') == '支'
    assert cell(sheet, '数量') == 3
    assert cell(sheet, '折前单价') == 1
    assert cell(sheet, '折前金额') == 3
    assert cell(sheet, '税率(%)') == '13'
    assert xlrd.xldate_as_datetime(cell(sheet, '录单日期'), book.datemode).date() == date(2026, 10, 7)
    assert xlrd.xldate_as_datetime(cell(sheet, '交货日期'), book.datemode).date() == date(2026, 10, 16)
    assert sheet.cell_type(1, 0) == sheet.cell_type(1, 13) == xlrd.XL_CELL_DATE
    assert cell(sheet, '摘要') == result.summary
    assert cell(sheet, '明细备注') == result.detail_note
    assert result.summary == result.detail_note + '#交期=2026-10-16'
    assert result.lines[0].base_unit_id == '1'
    assert result.lines[0].warehouse_id == '08390'
    assert result.lines[0].sku_id == 's1'
    assert len(result.detail_note.split('#')) == 10
    assert len(result.summary.split('#')) == 11
    assert result.detail_note.split('#')[3].strip() == '-'
    assert result.detail_note.split('#')[5].strip() == 'M20X80'
    assert result.detail_note.split('#')[6:8] == ['-         ', '-         ']
    assert note_delivery_date(result.summary) == '2026-10-16'
    assert parse_note(result.summary)['sku'] == 'LS1000'
    assert book.sheet_by_index(1).cell_value(0, 0) == '销售订单'


@pytest.mark.parametrize(('raw', 'expected'), [
    ('M20*80', 'M20X80'), (' M20×80 ', 'M20X80'),
    ('m20x80', 'M20X80'), ('M6.5*30.5', 'M6.5X30.5'),
    ('GB/T5783', ''), ('8.8级特殊热浸锌', ''), ('GB/T5783 M20*80', ''),
    ('M20*30/11', ''), ('M20 80', ''), ('M20', ''), ('', ''), (None, ''),
])
def test_catalog_spec_dimension_accepts_only_clear_two_part_size(raw, expected):
    assert catalog_spec_dimension(raw) == expected


@pytest.mark.parametrize(('changes', 'message'), [
    ({'spec': 'GB/T5783', 'catalog_summary': {'spec': 'GB/T5783'},
      'model': 'M20X80'}, 'catalog spec is not a verified dimension'),
    ({'summary_dimension': 'M20X90'}, 'reviewed summary dimension differs'),
    ({'spec': 'M20*90'}, 'reviewed summary dimension differs'),
    ({'summary_dimension_source': 'model'}, 'summary dimension source is not verified'),
    ({'catalog_summary': {'spec': 'M20*30/11'}}, 'catalog spec is not a verified dimension'),
])
def test_non_catalog_or_conflicting_size_cannot_generate_file(config, changes, message):
    session = frozen_session()
    draft = json.loads(session['draft'])
    draft['lines'][0].update(changes)
    freeze_draft(session, draft)
    with pytest.raises(ValueError, match=message):
        build_sales_web_import_xls(session, config)
    assert not config.destination.exists()


def test_human_confirmed_nonstandard_dimension_is_kept_verbatim(config):
    session = frozen_session()
    draft = json.loads(session['draft'])
    line = draft['lines'][0]
    line.update(spec='M20*30/11', catalog_summary={'spec': 'M20*30/11', 'model': '',
                                                 'grade': '', 'surface': ''},
                summary_dimension=' 1/4-20 UNC × 2" ',
                summary_dimension_source='human_confirmed',
                summary_metadata_confirmed=True,
                summary_metadata_actor='sales-user-1',
                summary_metadata_at='2026-10-07T10:11:12')
    freeze_draft(session, draft)
    result = build_sales_web_import_xls(session, config)
    assert result.detail_note.split('#')[5].startswith(' 1/4-20 UNC × 2" ')
    assert result.summary == result.detail_note + '#交期=2026-10-16'


@pytest.mark.parametrize(('change', 'message'), [
    ({'summary_dimension': ''}, 'human summary dimension is invalid'),
    ({'summary_dimension': ' '}, 'human summary dimension is invalid'),
    ({'summary_dimension': 'x' * 201}, 'human summary dimension is invalid'),
    ({'summary_dimension': 'M20#80'}, 'human summary dimension is invalid'),
    ({'summary_dimension': 'M20\n80'}, 'human summary dimension is invalid'),
    ({'summary_dimension': 123}, 'human summary dimension is invalid'),
    ({'summary_metadata_confirmed': False}, 'lacks confirmation'),
    ({'summary_metadata_actor': ''}, 'summary_metadata_actor is required'),
    ({'summary_metadata_at': ''}, 'summary_metadata_at is required'),
    ({'summary_metadata_at': 'not-a-date'}, 'timestamp is invalid'),
])
def test_human_dimension_without_valid_scalar_and_provenance_is_blocked(config, change, message):
    session = frozen_session()
    draft = json.loads(session['draft'])
    draft['lines'][0].update(summary_dimension='1/4-20 UNC × 2"',
                             summary_dimension_source='human_confirmed',
                             summary_metadata_confirmed=True,
                             summary_metadata_actor='sales-user-1',
                             summary_metadata_at='2026-10-07T10:11:12')
    draft['lines'][0].update(change)
    freeze_draft(session, draft)
    with pytest.raises(ValueError, match=message):
        build_sales_web_import_xls(session, config)
    assert not config.destination.exists()


def test_existing_output_is_never_overwritten(config):
    session = frozen_session()
    build_sales_web_import_xls(session, config)
    original = config.destination.read_bytes()
    with pytest.raises(FileExistsError):
        build_sales_web_import_xls(session, config)
    assert config.destination.read_bytes() == original


@pytest.mark.parametrize('change', ['version', 'hash', 'draft', 'warehouse', 'price',
                                     'unit', 'base_unit', 'issues', 'empty_lines', 'bad_date'])
def test_changed_or_unreviewed_draft_cannot_generate_file(config, change):
    session = frozen_session()
    if change == 'version':
        session['version'] += 1
    elif change == 'hash':
        session['confirmed_hash'] = '0' * 64
    elif change == 'draft':
        draft = json.loads(session['draft'])
        draft['lines'][0]['quantity'] = '4'
        session['draft'] = dumps(draft)
    else:
        payload = json.loads(session['confirmed_payload'])
        draft = payload['draft']
        line = draft['lines'][0]
        if change == 'warehouse':
            line['warehouse'] = 'other'
        elif change == 'price':
            line['untaxed_unit_price'] = '0.999'
        elif change == 'unit':
            line['unit'] = ''
        elif change == 'base_unit':
            line['resolved_identity']['base_unit'] = '2'
        elif change == 'issues':
            draft['issues'] = [{'code': 'review'}]
        elif change == 'empty_lines':
            draft['lines'].clear()
        elif change == 'bad_date':
            line['delivery_date'] = '2026-02-30'
        session['confirmed_payload'] = dumps(payload)
        session['draft'] = dumps(draft)
        session['confirmed_hash'] = sha256(dumps(payload).encode()).hexdigest()
    with pytest.raises(ValueError):
        build_sales_web_import_xls(session, config)
    assert not config.destination.exists()


def test_repeated_sku_keeps_two_frozen_rows_and_independent_notes(config):
    session = frozen_session()
    draft = json.loads(session['draft'])
    second = dict(draft['lines'][0], quantity='2', unit_price='2.26',
                  quoted_unit_price='2.26', untaxed_unit_price='2.000',
                  untaxed_line_amount='4.00', delivery_date='2026-10-17')
    draft['lines'].append(second)
    freeze_draft(session, draft)
    result = build_sales_web_import_xls(session, config)
    book = xlrd.open_workbook(str(config.destination), formatting_info=True)
    sheet = book.sheet_by_index(0)
    at = lambda row, header: sheet.cell_value(row, EXPECTED_HEADERS.index(header))
    assert sheet.nrows == 3
    assert tuple(line.line_id for line in result.lines) == ('1', '2')
    assert tuple(line.sku for line in result.lines) == ('LS1000', 'LS1000')
    assert (at(1, '数量'), at(2, '数量')) == (3, 2)
    assert (at(1, '折前金额'), at(2, '折前金额')) == (3, 4)
    assert at(1, '明细备注').split('#')[2].strip() == '1'
    assert at(2, '明细备注').split('#')[2].strip() == '2'
    assert at(1, '摘要') == at(2, '摘要') == result.summary
    assert result.summary == result.lines[0].detail_note + '#交期=2026-10-16'
    assert xlrd.xldate_as_datetime(at(2, '交货日期'), book.datemode).date() == date(2026, 10, 17)
    assert at(1, '单位') == at(2, '单位') == '支'


def test_generated_repeated_sku_rows_prepare_as_one_reviewed_import(config):
    session = frozen_session()
    draft = json.loads(session['draft'])
    draft['lines'].append(dict(draft['lines'][0], quantity='2', unit_price='2.26',
                               quoted_unit_price='2.26', untaxed_unit_price='2.000',
                               untaxed_line_amount='4.00'))
    freeze_draft(session, draft)
    runtime_root = Path(__file__).resolve().parents[1] / 'runtime'
    with TemporaryDirectory(dir=runtime_root) as folder:
        runtime = Path(folder)
        result = build_sales_web_import_xls(session, replace(config, destination=runtime / 'two-lines.xls'))
        tax = (('0.39', '1.13', '3.39'), ('0.52', '2.26', '4.52'))
        lines = tuple({**asdict(line), 'tax_rate_percent': '13', 'tax_amount': amount,
                       'taxed_unit_price': unit, 'taxed_line_amount': gross}
                      for line, (amount, unit, gross) in zip(result.lines, tax))
        listing = {'number': result.order_number, 'date': '2026-10-07',
                   'audit_status': '未审核', 'shipping_status': '未提交',
                   'customer': '客户甲', 'warehouse': '仓库丙', 'salesperson': '经手人乙',
                   'delivery_date': '2026-10-16', 'untaxed_total': '7.00',
                   'taxed_total': '7.91'}
        header = {'number': result.order_number, 'maker': '制单员甲', 'summary': result.summary}
        spec = WebImportSpec(str(uuid.uuid4()), result.order_number, 'P', 'account-A',
                             result.path, result.sha256, listing, header, '销售部',
                             '六角头螺栓', '客户已确认', result.detail_note, lines)
        staged = SalesWebImportTransport(runtime).prepare(spec)
        assert staged.is_file() and staged.read_bytes() == result.path.read_bytes()


def test_scope_and_template_must_match_confirmed_draft(config):
    session = frozen_session()
    with pytest.raises(ValueError, match='scope'):
        build_sales_web_import_xls(session, replace(config, owner_id='other'))
    assert not config.destination.exists()
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet('单据导入模板')
    sheet.write(0, 0, 'changed')
    workbook.add_sheet('单据类型')
    workbook.save(str(config.template_path))
    with pytest.raises(ValueError, match='template'):
        build_sales_web_import_xls(session, config)
    assert not config.destination.exists()
