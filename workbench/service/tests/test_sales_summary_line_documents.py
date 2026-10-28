"""Synthetic offline proof of the App/NAS summary and per-line ERP documents."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
from hashlib import sha256
import json
import uuid

import pytest
import xlrd
from xlutils.copy import copy as copy_xls

from core.rules import parse_note, note_delivery_date
from core.sales_web_import import WebImportConfig, _values, build_source_summary
from core.sales_web_operation import prepare_sales_spec, run_sales_import
from core.sales_web_queue import line_document_numbers
from core.sales_web_transport import SalesWebImportTransport, WebImportBlocked
from core.sales_trial_authorization import trial_request_matches, TRIAL_FIELDS
from tools.web_operation_worker import _validated_sales_runner_result
from test_sales_web_import import freeze_draft
from test_sales_web_operation_worker import sales_case, runtime
from test_sales_assistant import engine, create, edit_body
from test_daily_work import store, user


@pytest.fixture
def line_case(sales_case):
    host, payload, operation = sales_case
    payload, operation = deepcopy(payload), deepcopy(operation)
    session = payload['frozen_session']
    draft = json.loads(session['draft'])
    source = {'id': 'source-1', 'original_name': '客户原六角头螺栓',
              'original_spec': 'M20×80-4.8', 'original_standard': 'GB/T5781-2016',
              'original_grade': '4.8', 'original_surface': '镀锌',
              'customer_line_no': '010', 'customer_part_no': 'PART-010',
              'project_no': 'PROJECT-1'}
    draft['lines'][0]['source_ref'] = source
    draft['lines'][0]['source_summary'] = build_source_summary(draft['lines'][0],
        {**draft['lines'][0]['catalog_summary'], 'standard': 'GB/T5783'}, source)
    second = dict(draft['lines'][0], quantity='2', unit_price='2.26', quoted_unit_price='2.26',
                  untaxed_unit_price='2.000', untaxed_line_amount='4.00', delivery_date='2026-10-17')
    second['source_ref'] = {**source, 'id': 'source-2', 'customer_line_no': 'A10',
                            'customer_part_no': 'PART-A10'}
    second['source_summary'] = build_source_summary(second, second['catalog_summary'], second['source_ref'])
    draft['lines'].append(second)
    freeze_draft(session, draft)
    numbers = line_document_numbers('P', session['id'], 2)
    payload.update(schema='sales_web_import_v2', document_mode='line_document',
                   order_number=numbers[0], document_numbers=list(numbers),
                   confirmed_hash=session['confirmed_hash'], line_count=2,
                   line_unit_evidence=[draft['lines'][0]['unit_evidence']]*2)
    operation['target_id'] = numbers[0]
    config = WebImportConfig(host.template_path, host.template_path.parent/'unused-v2.xls', numbers[0],
        date(2026,10,7), 'P', 'v1', 'sales', host.department_name, host.maker_name,
        host.allowed_handlers[0], host.warehouse_id, host.warehouse_name, 'line_document', numbers)
    _, _, _, summary, lines = _values(session, config)
    payload['summary'] = summary
    payload['line_documents'] = [{'line_id': line.line_id, 'order_number': line.order_number,
        'summary': line.summary, 'delivery_date': line.delivery_date} for line in lines]
    return host, payload, operation


class Browser:
    def __init__(self, rows=2): self.rows, self.stages, self.clicks = rows, 0, 0
    def account_identity(self): return 'P', 'account-A'
    def stage_sales_import(self, path): self.stages += 1
    def click_upload_once(self): self.clicks += 1
    def visible_import_progress(self):
        return f'业务单据导入进度\n1,开始导入\n2,开始检查格式\n3,本次导入{self.rows}行数据,开始写入数据\n4,-----完成-----\n关闭'
    def sales_list(self, *args): raise AssertionError('ERP sales readback is forbidden')
    def sales_detail(self, *args): raise AssertionError('ERP sales readback is forbidden')


def test_repeated_sku_keeps_customer_line_part_attributes_and_each_due_in_g_y(line_case, runtime):
    host, payload, operation = line_case
    spec = prepare_sales_spec(payload, operation, host, runtime)
    assert spec.schema == 'sales_web_import_v2' and spec.document_mode == 'line_document'
    assert spec.document_numbers == tuple(payload['document_numbers'])
    assert len(set(spec.document_numbers)) == 2 and all(len(number) == 20 for number in spec.document_numbers)
    book = xlrd.open_workbook(str(spec.source_path))
    rows = book.sheet_by_index(0)
    for ordinal, (customer_line, part, quantity, due) in enumerate(
            [('010', 'PART-010', '3', '2026-10-16'), ('A10', 'PART-A10', '2', '2026-10-17')], 1):
        g, y = rows.cell_value(ordinal,6), rows.cell_value(ordinal,24)
        assert g == y == spec.lines_expected[ordinal-1]['summary']
        parsed = parse_note(g)
        assert parsed['lineNo'] == customer_line and parsed['drawingNo'] == part
        assert parsed['name'] == '客户原六角头螺栓' and parsed['spec'] == 'GB/T5781 M20X80'
        assert parsed['grade'] == '4.8级' and parsed['surface'] == '镀锌'
        assert parsed['sku'] == 'LS1000' and parsed['quantity'] == quantity
        assert note_delivery_date(g) == due
        assert rows.cell_value(ordinal,1) == spec.document_numbers[ordinal-1]
        assert rows.cell_value(ordinal,16) == '六角头螺栓'  # ERP actual name, not source alias
        assert rows.cell_value(ordinal,7) == '客户已确认' and '客户已确认' not in g
    transport = SalesWebImportTransport(runtime)
    staged = transport.prepare(spec)
    manifest = json.loads((staged.parent/'intent.json').read_text())
    assert manifest['document_numbers'] == payload['document_numbers']
    browser = Browser()
    result = transport.execute(spec.intent_id, browser)
    assert result.status == 'imported' and result.document_numbers == spec.document_numbers
    assert result.document_count == 2 and result.imported_rows == 2 and not result.erp_readback_performed
    assert browser.clicks == browser.stages == 1
    assert transport.execute(spec.intent_id, None) == result


def test_partial_receipt_freezes_entire_batch_and_never_reuploads(line_case, runtime):
    host, payload, operation = line_case
    browser = Browser(rows=1)
    outcome = run_sales_import(payload, operation, host, runtime, browser)
    assert outcome.status == 'unknown' and outcome.code == 'web_import_row_count_mismatch'
    later = Browser(rows=2)
    recovered = run_sales_import(payload, operation, replace(host,enabled=False), runtime, later,readback_only=True)
    assert recovered.status == 'unknown' and later.clicks == later.stages == 0
    assert recovered.document_numbers == tuple(payload['document_numbers'])


def test_all_numbers_checked_before_new_claim_or_any_upload(line_case, runtime):
    host, payload, operation = line_case
    spec = prepare_sales_spec(payload, operation, host, runtime)
    transport = SalesWebImportTransport(runtime)
    transport.prepare(spec)
    manifest = transport._validate_spec(spec)
    second = {**manifest, 'intent_id':str(uuid.uuid4()),
              'order_number':'ULOTHER0000000000001',
              'document_numbers':['ULOTHER0000000000001',spec.document_numbers[1]]}
    with transport._account_lock(second), pytest.raises(WebImportBlocked,match='order_number_claimed'):
        transport._claim_number(second)
    assert len(list(transport.order_claims.glob('*.json'))) == 2


@pytest.mark.parametrize('column,value',[(1,'ULOTHER'),(6,'tampered'),(13,0),(24,'tampered')])
def test_each_document_number_summary_due_and_note_bound_to_frozen_file(line_case,runtime,column,value):
    host,payload,operation=line_case
    spec=prepare_sales_spec(payload,operation,host,runtime)
    book=xlrd.open_workbook(str(spec.source_path),formatting_info=True)
    changed=copy_xls(book);changed.get_sheet(0).write(2,column,value);book.release_resources()
    changed.save(str(spec.source_path))
    mutated=replace(spec,source_sha256=sha256(spec.source_path.read_bytes()).hexdigest())
    with pytest.raises(WebImportBlocked,match='import_workbook_invalid'):
        SalesWebImportTransport(runtime).prepare(mutated)
    assert not list((runtime/'sales-web-import').rglob('attempt.json'))


def test_projects_with_hash_preserve_input_and_block_instead_of_silent_segments(line_case,runtime):
    host,payload,operation=line_case
    session=payload['frozen_session'];draft=json.loads(session['draft'])
    draft['lines'][0]['source_summary']['project_no']='PROJECT#A'
    freeze_draft(session,draft);payload['confirmed_hash']=session['confirmed_hash']
    with pytest.raises(WebImportBlocked,match='sales_import_generation_failed'):
        prepare_sales_spec(payload,operation,host,runtime)
    assert json.loads(session['draft'])['lines'][0]['source_summary']['project_no']=='PROJECT#A'
    assert not list(runtime.rglob('attempt.json'))


@pytest.mark.parametrize('spec,grade,expected',[
    ('M6-25','','M6-25'), ('M6-1.0-30','','M6-1.0-30'),
    ('M6X30-8.8','8.8','M6X30'), ('M6X30-12','','M6X30-12')])
def test_summary_never_mistakes_length_or_pitch_for_grade(spec,grade,expected):
    summary=build_source_summary({}, {}, {'original_spec':spec,'original_grade':grade})
    assert summary['spec']==expected


def test_material_is_only_appended_when_explicit_and_not_already_in_original_grade():
    assert build_source_summary({}, {}, {'original_name':'螺栓','original_material':'304'})['name']=='螺栓(304)'
    assert build_source_summary({}, {}, {'original_name':'螺栓','original_material':'304',
        'original_grade':'A2-70'})['name']=='螺栓'
    assert build_source_summary({'product_name':'螺栓'}, {'material':'304'})['name']=='螺栓'


def test_client_cannot_supply_or_replace_server_source_summary(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    body=edit_body(session)
    body['lines'][0].update(source_summary={'name':'伪造原文','spec':'M1','customer_line_no':'010'},
                          source_ref={'id':'forged','original_name':'伪造原文','original_spec':'M1'})
    edited=engine.edit(actor,session['id'],body)['session']
    line=edited['draft']['lines'][0]
    assert not line.get('source_ref')
    assert line['source_summary']['name']=='螺栓' and line['source_summary']['customer_line_no']==''
    repeat={**edited['draft'],'version':edited['version'],'idempotency_key':'replace-source-summary'}
    repeat['lines'][0]['source_summary']={'name':'再次伪造','grade':'12.9'}
    next_view=engine.edit(actor,session['id'],repeat)['session']
    assert next_view['draft']['lines'][0]['source_summary']['name']=='螺栓'
    assert next_view['draft']['lines'][0]['source_summary']['grade']=='8.8'


def test_v2_outer_worker_requires_entire_frozen_document_list(line_case):
    _,payload,_=line_case
    valid={'status':'imported','code':'web_import_confirmed','reference':payload['order_number'],
           'completion_basis':'web_import_confirmation','erp_readback_performed':False,
           'unit_verified':False,'table_verified':True,'file_sha256':'a'*64,
           'imported_rows':2,'expected_rows':2,'document_numbers':payload['document_numbers'],'document_count':2}
    assert _validated_sales_runner_result(valid,payload['order_number'],2,payload['document_numbers'])==valid
    for mutation in ({'document_numbers':payload['document_numbers'][:1]}, {'document_count':1},
                     {'document_numbers':list(reversed(payload['document_numbers']))}):
        assert _validated_sales_runner_result({**valid,**mutation},payload['order_number'],2,
            payload['document_numbers'])['status']=='unknown'


def test_trial_v1_cannot_expand_to_v2_and_new_trial_binds_all_documents(line_case):
    _,payload,operation=line_case
    trial={'schema':'sales_web_trial_v2','session_id':payload['session_id'],
        'confirmed_hash':payload['confirmed_hash'],'order_number':payload['order_number'],
        'customer_name':payload['customer_name'],'line_count':2,'taxed_total':'7.91',
        'payload_schema':'sales_web_import_v2','document_mode':'line_document',
        'document_numbers':payload['document_numbers']}
    assert trial_request_matches(trial,payload,operation)
    old={key:value for key,value in trial.items() if key in TRIAL_FIELDS}
    old['schema']='sales_web_trial_v1'
    assert not trial_request_matches(old,payload,operation)
    for mutation in ({'document_numbers':payload['document_numbers'][:1]},
                     {'document_numbers':list(reversed(payload['document_numbers']))},
                     {'payload_schema':'sales_web_import_v1'},
                     {'document_mode':'single_document'}, {'line_count':1}):
        assert not trial_request_matches({**trial,**mutation},payload,operation)
