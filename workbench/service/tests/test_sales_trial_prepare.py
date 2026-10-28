"""The second sales trial is a private file preparation, never an ERP write."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

import pytest
import xlrd
import xlwt

from core.sales_web_import import EXPECTED_HEADERS
from core.store import dumps
from tools.sales_trial_prepare import RUNTIME_PARENT, main, prepare


@pytest.fixture
def inputs(tmp_path):
    template = tmp_path / 'original-blank.xls'
    book = xlwt.Workbook()
    sheet = book.add_sheet('单据导入模板')
    for index, header in enumerate(EXPECTED_HEADERS):
        sheet.write(0, index, header)
    book.add_sheet('单据类型').write(0, 0, '销售订单')
    book.save(str(template))
    template.chmod(0o600)
    template_hash = sha256(template.read_bytes()).hexdigest()
    catalog = tmp_path / 'catalog.sqlite3'
    product = {'profile_id': 'P', 'warehouse_id': '08390',
        'product_id': 'product-1', 'sku_id': 'sku-1', 'base_unit': '1',
        'sku': 'LS3073', 'product_name': '六角头螺栓',
        'spec': 'M20*80', 'model': '8.8级特殊热浸锌',
        'unit_name': '', 'warehouse_name': '江苏优螺'}
    with sqlite3.connect(catalog) as db:
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute('CREATE TABLE records(kind TEXT,id TEXT PRIMARY KEY,sku TEXT,payload TEXT)')
        db.execute("INSERT INTO settings VALUES ('data_version',?)", (dumps('catalog-v1'),))
        db.execute("INSERT INTO records VALUES ('inventory','r1','LS3073',?)", (dumps(product),))
    binding = {'customer_name': '备用零售客户', 'erp_customer_id': 'customer-1',
        'profile_id': 'P', 'account_id': 'a'*64, 'match_count': 1,
        'source': 'erp_customer_exact_lookup', 'source_hash': 'b'*64,
        'observed_at': datetime.now(timezone.utc).isoformat()}
    report = {'schema': 'sales_web_host_preflight_v1',
        'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'account': {'profile_id': 'P', 'account_id': 'a'*64,
                    'tenant_name': '测试租户', 'operator_name': '测试操作员'},
        'customer_binding': binding,
        'staff': {'罗健树': {'match_count': 1, 'erp_id': 'maker-1'},
                  '李瑄': {'match_count': 1, 'erp_id': 'handler-1'}},
        'warehouse': {'erp_id': '08390', 'match_count': 1},
        'maker_matches_erp_token_employee': True,
        'template_sha256': template_hash,
        'host_config_candidate': {'enabled': False, 'acceptance_complete': False,
            'profile_id': 'P', 'account_id': 'a'*64, 'tenant_name': '测试租户',
            'operator_name': '测试操作员', 'template_path': str(template),
            'template_sha256': template_hash, 'warehouse_id': '08390',
            'warehouse_name': '江苏优螺', 'department_name': '销售部',
            'maker_name': '罗健树', 'allowed_handlers': ['李瑄', '刘锋']}}
    preflight = tmp_path / 'preflight.json'
    preflight.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    preflight.chmod(0o600)
    runtime = RUNTIME_PARENT / ('sales-trial-' + uuid.uuid4().hex)
    yield report, preflight, catalog, template, runtime
    shutil.rmtree(runtime, ignore_errors=True)


def _write_report(report, path):
    path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    path.chmod(0o600)


def test_prepares_two_separate_ls3073_lines_without_queue_or_erp_write(inputs):
    report, preflight, catalog, _, runtime = inputs
    original_catalog = sha256(catalog.read_bytes()).hexdigest()
    delivery = datetime.now(timezone.utc).date() + timedelta(days=7)
    outcome = prepare(preflight, catalog, runtime, delivery)
    assert outcome['status'] == 'prepared_not_uploaded'
    review = json.loads((runtime / 'review.json').read_text())
    payload = json.loads((runtime / 'frozen-payload.json').read_text())
    assert review['order_number'] == payload['order_number']
    assert review['trial_authorization_candidate'] == {
        'schema': 'sales_web_trial_v2', 'session_id': payload['session_id'],
        'confirmed_hash': payload['confirmed_hash'], 'order_number': payload['order_number'],
        'customer_name': '备用零售客户', 'line_count': 2, 'taxed_total': '3.39',
        'payload_schema':'sales_web_import_v2','document_mode':'line_document',
        'document_numbers':payload['document_numbers']}
    assert [(line['sku'], line['quantity']) for line in review['lines']] == [
        ('LS3073', '1'), ('LS3073', '2')]
    assert [line['summary_dimension_source'] for line in review['lines']] == [
        'catalog_spec_shape', 'catalog_spec_shape']
    assert [line['summary_dimension'] for line in review['lines']] == ['M20X80', 'M20X80']
    assert review['summary'].split('#')[5].strip() == 'M20X80'
    assert 'M20*80' not in review['summary']
    assert review['summary'].endswith('#交期=' + delivery.isoformat())
    assert (review['untaxed_total'], review['taxed_total']) == ('3.00', '3.39')
    assert sha256((runtime / 'review-order.xls').read_bytes()).hexdigest() == outcome['file_sha256']
    book = xlrd.open_workbook(str(runtime / 'review-order.xls'))
    sheet = book.sheet_by_index(0)
    assert sheet.nrows == 3 and sheet.ncols == 27
    column = {name: index for index, name in enumerate(EXPECTED_HEADERS)}
    assert [sheet.cell_value(index, column['商品编号']) for index in (1, 2)] == ['LS3073', 'LS3073']
    assert [sheet.cell_value(index,column['单据编号']) for index in (1,2)] == payload['document_numbers']
    assert len(set(payload['document_numbers']))==2
    assert all(sheet.cell_value(index,column['摘要'])==sheet.cell_value(index,column['明细备注'])
               for index in (1,2))
    assert [sheet.cell_value(index, column['数量']) for index in (1, 2)] == [1, 2]
    assert [sheet.cell_value(index, column['折前金额']) for index in (1, 2)] == [1, 2]
    assert [sheet.cell_value(index, column['税率(%)']) for index in (1, 2)] == ['13', '13']
    assert [sheet.cell_value(index, column['单位']) for index in (1, 2)] == ['支', '支']
    with sqlite3.connect(runtime / 'workbench.sqlite3') as db:
        policy = json.loads(db.execute("SELECT value FROM settings WHERE key='sales_web_import_policy'").fetchone()[0])
        assert policy['enabled'] is False
        assert db.execute('SELECT count(*) FROM web_operations').fetchone()[0] == 0
        assert db.execute('SELECT status FROM sales_assistant_sessions').fetchone()[0] == 'confirmed'
    assert sha256(catalog.read_bytes()).hexdigest() == original_catalog
    assert runtime.stat().st_mode & 0o777 == 0o700
    for name in ('review.json', 'frozen-payload.json', 'review-order.xls', 'workbench.sqlite3'):
        assert (runtime / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('change,code', [
    (lambda report: report['host_config_candidate'].update(enabled=True), 'sales_preflight_scope_invalid'),
    (lambda report: report['customer_binding'].update(customer_name='其他客户'), 'sales_preflight_scope_invalid'),
    (lambda report: report['host_config_candidate'].update(maker_name='其他人'), 'sales_preflight_scope_invalid'),
    (lambda report: report['host_config_candidate'].update(allowed_handlers=['刘锋']), 'sales_preflight_scope_invalid'),
    (lambda report: report.update(observed_at_utc='2026-01-01T00:00:00+00:00'), 'sales_preflight_scope_invalid'),
])
def test_wrong_scope_blocks_before_creating_runtime(inputs, change, code):
    report, preflight, catalog, _, runtime = inputs
    change(report)
    _write_report(report, preflight)
    with pytest.raises(ValueError, match=code):
        prepare(preflight, catalog, runtime, datetime.now(timezone.utc).date() + timedelta(days=7))
    assert not runtime.exists()


def test_catalog_shape_and_private_path_are_required(inputs):
    report, preflight, catalog, _, runtime = inputs
    with sqlite3.connect(catalog) as db:
        raw = json.loads(db.execute('SELECT payload FROM records').fetchone()[0])
        raw['spec'] = 'M20'
        db.execute('UPDATE records SET payload=?', (dumps(raw),))
    with pytest.raises(ValueError, match='catalog_product_not_reviewed'):
        prepare(preflight, catalog, runtime, datetime.now(timezone.utc).date() + timedelta(days=7))
    assert not runtime.exists()
    with sqlite3.connect(catalog) as db:
        raw['spec'] = 'M20*80'
        db.execute('UPDATE records SET payload=?', (dumps(raw),))
        other_unit = dict(raw, base_unit='2', sku_id='other-sku')
        db.execute("INSERT INTO records VALUES ('inventory','r2','LS3073',?)",
                   (dumps(other_unit),))
    with pytest.raises(ValueError, match='catalog_identity_not_unique'):
        prepare(preflight, catalog, runtime, datetime.now(timezone.utc).date() + timedelta(days=7))
    assert not runtime.exists()
    with pytest.raises(ValueError, match='isolated_runtime_required'):
        from tools.sales_trial_prepare import _new_runtime
        _new_runtime(catalog.parent / 'sales-trial-outside')
    preflight.chmod(0o644)
    with pytest.raises(ValueError, match='private_source_unavailable'):
        prepare(preflight, catalog, runtime, datetime.now(timezone.utc).date() + timedelta(days=7))


def test_cli_failure_reports_only_safe_code(inputs, capsys):
    _, preflight, catalog, _, runtime = inputs
    preflight.chmod(0o644)
    result = main(['--report', str(preflight), '--catalog', str(catalog),
        '--runtime', str(runtime), '--delivery',
        (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()])
    captured = capsys.readouterr()
    assert result == 2 and captured.out == ''
    assert json.loads(captured.err)['code'] == 'private_source_unavailable'
    assert '备用零售客户' not in captured.err
    assert not runtime.exists()
