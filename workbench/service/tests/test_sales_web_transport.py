"""Offline browser fakes only; these tests never contact or change the ERP."""

import hashlib
import json
import tempfile
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
import xlrd
import xlwt
from xlutils.copy import copy

from core.sales_web_transport import (SalesListResult, SalesWebImportTransport,
    WebImportBlocked, WebImportSpec, WEB_IMPORT_HEADERS)


SERVICE = Path(__file__).resolve().parents[1]
NUMBER = "UL26100713370454EE75"
LIST = {"number": NUMBER, "date": "2026-10-07", "audit_status": "未审核",
        "shipping_status": "未提交",
        "customer": "备用零售客户", "warehouse": "江苏优螺", "salesperson": "李瑄",
        "delivery_date": "2026-10-15", "untaxed_total": "1.00", "taxed_total": "1.13"}
DETAIL = {"number": NUMBER, "maker": "罗健树", "sku": "LS3073", "quantity": "1",
          "untaxed_unit_price": "1.000", "untaxed_line_amount": "1.00",
          "tax_rate_percent": "13", "tax_amount": "0.13",
          "taxed_unit_price": "1.13", "taxed_line_amount": "1.13",
          "summary": NUMBER + "#UL随行测试#1#LS3073#交期=2026-10-15", "note": "已确认"}
DETAIL_NOTE = DETAIL["summary"].removesuffix("#交期=2026-10-15")
PRODUCT = "六角头螺栓"


def workbook(path, number=NUMBER, kind="销售订单", unit="支", customer="备用零售客户", note="已确认"):
    book = xlwt.Workbook()
    sheet = book.add_sheet("单据导入模板")
    book.add_sheet("单据类型")
    for index, header in enumerate(WEB_IMPORT_HEADERS):
        sheet.write(0, index, header)
    values = {"单据编号": number, "单据类型": kind, "往来单位名称": customer,
              "部门名称": "销售部", "摘要": DETAIL["summary"], "附加说明": note,
              "制单人名称": "罗健树", "经手人名称": "李瑄", "仓库名称": "江苏优螺",
              "商品编号": "LS3073", "商品名称": PRODUCT, "明细仓库名称": "江苏优螺",
              "单位": unit, "数量": 1, "折前单价": 1, "折前金额": 1,
              "税率(%)": "13", "明细备注": DETAIL_NOTE}
    day_style = xlwt.easyxf(num_format_str="yyyy-mm-dd")
    for index, header in enumerate(WEB_IMPORT_HEADERS):
        if header in ("录单日期", "交货日期"):
            value = date.fromisoformat(LIST["date" if header == "录单日期" else "delivery_date"])
            sheet.write(1, index, value, day_style)
        elif header in values:
            sheet.write(1, index, values[header])
    book.save(str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def rig():
    with tempfile.TemporaryDirectory(dir=SERVICE / "runtime") as name:
        runtime = Path(name)
        source = runtime / "reviewed.xls"
        sha = workbook(source)
        spec = WebImportSpec(str(uuid.uuid4()), NUMBER, "profile-P", "account-A",
                             source, sha, LIST, DETAIL, "销售部", PRODUCT, "已确认", DETAIL_NOTE)
        transport = SalesWebImportTransport(runtime)
        yield transport, spec


def receipt(rows=1):
    return (f"业务单据导入进度\n1,开始导入...\n2,开始检查格式...\n"
            f"3,本次导入{rows}行数据，开始写入数据...\n4,----完成----\n关闭")


class Browser:
    def __init__(self):
        self.clicks = self.stages = self.lookups = self.details = self.identities = self.progress_calls = 0
        self.stage_error = self.click_error = self.progress_error = False
        self.progress = receipt()
        self.identity = ("profile-P", "account-A")

    def account_identity(self):
        self.identities += 1
        return self.identity

    def find_by_full_number(self, number):
        self.lookups += 1
        raise AssertionError("Automatic sales import must not query ERP orders")

    def open_detail_readonly(self, row):
        self.details += 1
        raise AssertionError("Automatic sales import must not open an ERP editor")

    def stage_sales_import(self, path):
        self.stages += 1
        assert path.name == "order.xls" and path.is_file()
        if self.stage_error:
            raise RuntimeError("login expired")

    def click_upload_once(self):
        self.clicks += 1
        if self.click_error:
            raise TimeoutError("click outcome unknown")

    def visible_import_progress(self):
        self.progress_calls += 1
        if self.progress_error:
            raise TimeoutError("result outcome unknown")
        return self.progress


def test_one_upload_completes_from_exact_web_receipt_without_erp_readback(rig):
    transport, spec = rig
    saved = transport.prepare(spec)
    browser = Browser()
    outcome = transport.execute(spec.intent_id, browser)
    assert outcome.status == "imported" and outcome.code == "web_import_confirmed"
    assert outcome.completion_basis == "web_import_confirmation"
    assert outcome.table_verified and outcome.file_sha256 == spec.source_sha256
    assert outcome.imported_rows == outcome.expected_rows == 1
    assert outcome.progress_observed and not outcome.unit_verified and not outcome.erp_readback_performed
    assert outcome.unit_basis == ""
    assert browser.clicks == browser.stages == 1 and browser.lookups == browser.details == 0
    assert saved != spec.source_path and saved.read_bytes() == spec.source_path.read_bytes()
    assert (saved.parent / "attempt.json").exists()
    evidence = json.loads((saved.parent / "completed.json").read_text())
    assert evidence["outcome"]["status"] == "imported" and evidence["file_sha256"] == spec.source_sha256
    assert evidence["progress_sha256"] == hashlib.sha256(receipt().encode()).hexdigest()
    assert not (saved.parent / "verified.json").exists()
    assert transport.execute(spec.intent_id, None) == outcome
    assert browser.clicks == browser.stages == 1 and browser.lookups == browser.details == 0


def test_legacy_spec_derives_note_only_for_verified_xls_cells(rig):
    transport, spec = rig
    details = {key: value for key, value in spec.detail_expected.items() if key != 'note'}
    legacy = replace(spec, detail_expected=details)
    assert transport._validate_spec(legacy)['detail_expected']['note'] == spec.note
    transport.prepare(legacy)
    browser = Browser()
    assert transport.execute(spec.intent_id, browser).status == 'imported'
    assert browser.details == browser.lookups == 0


def test_empty_note_is_allowed_only_when_xls_is_empty_too(rig):
    transport, spec = rig
    sha = workbook(spec.source_path, note='')
    empty = replace(spec, note='', source_sha256=sha, detail_expected={**spec.detail_expected, 'note': ''})
    transport.prepare(empty)
    assert transport.execute(spec.intent_id, Browser()).status == 'imported'


def test_nonempty_note_cannot_be_injected_after_import_when_xls_is_blank(rig):
    transport, spec = rig
    sha = workbook(spec.source_path, note='')
    with pytest.raises(WebImportBlocked, match='import_workbook_invalid'):
        transport.prepare(replace(spec, source_sha256=sha))
    assert not (transport.root / spec.intent_id / 'attempt.json').exists()


def test_expected_and_frozen_note_must_agree(rig):
    transport, spec = rig
    with pytest.raises(WebImportBlocked, match='readback_note_binding_invalid'):
        transport._validate_spec(replace(spec, detail_expected={**spec.detail_expected, 'note': '其他说明'}))


def test_observed_180_unit_header_limits_block_before_upload(rig):
    transport, spec = rig
    note = '字' * 180
    transport._validate_spec(replace(spec, note=note, detail_expected={**spec.detail_expected, 'note': note}))
    note += '字'
    with pytest.raises(WebImportBlocked, match='sales_note_exceeds_web_limit'):
        transport.prepare(replace(spec, note=note, detail_expected={**spec.detail_expected, 'note': note}))
    with pytest.raises(WebImportBlocked, match='sales_note_exceeds_web_limit'):
        transport._validate_spec(replace(spec, note='😀' * 91,
            detail_expected={**spec.detail_expected, 'note': '😀' * 91}))
    summary = spec.detail_expected['summary'].replace('#UL随行测试', '#' + 'X' * 200)
    with pytest.raises(WebImportBlocked, match='sales_summary_exceeds_web_limit'):
        transport.prepare(replace(spec, detail_expected={**spec.detail_expected, 'summary': summary}))
    assert not (transport.root / spec.intent_id / 'attempt.json').exists()


def test_existing_legacy_intent_preserves_note_binding_without_new_intent(rig):
    transport, spec = rig
    path = transport.prepare(spec)
    intent_path = path.parent / 'intent.json'
    old = json.loads(intent_path.read_text())
    old['detail_expected'].pop('note')
    intent_path.write_text(json.dumps(old, ensure_ascii=False))
    assert transport.prepare(spec) == path
    browser = Browser()
    assert transport.execute(spec.intent_id, browser).status == 'imported'
    assert browser.clicks == 1 and browser.details == browser.lookups == 0


def test_local_unit_verification_does_not_claim_erp_unit_readback(rig):
    transport, original = rig
    spec = replace(original, base_unit_id="1", warehouse_id="08390")
    transport.prepare(spec)
    outcome = transport.execute(spec.intent_id, Browser())
    assert outcome.status == 'imported' and outcome.table_verified
    assert not outcome.unit_verified and outcome.unit_basis == '' and not outcome.erp_readback_performed


def test_unknown_click_never_uploads_again_or_queries_erp_on_recovery(rig):
    transport, spec = rig
    transport.prepare(spec)
    browser = Browser(); browser.click_error = True
    first = transport.execute(spec.intent_id, browser)
    assert first.status == 'unknown' and first.code == 'upload_attempt_unknown'
    assert first.table_verified and first.completion_basis == ''
    counts = (browser.clicks, browser.stages, browser.identities, browser.progress_calls)
    browser.click_error = False
    second = transport.execute(spec.intent_id, browser)
    assert second.status == 'unknown' and second.code == 'web_import_requires_manual_review'
    assert counts == (browser.clicks, browser.stages, browser.identities, browser.progress_calls)
    assert browser.lookups == browser.details == 0


@pytest.mark.parametrize('progress', [
    '', '导入成功', '已点击导入文件', '业务单据导入进度\n4,----完成----',
    '业务单据导入进度\n1,开始导入...\n2,开始检查格式...\n3,本次导入1行数据，开始写入数据...',
    receipt(0), receipt(2), receipt() + '\n部分导入', receipt() + '\n错误',
    receipt().replace('4,----完成----', '4,导入失败'),
    receipt().replace('3,本次导入1行数据', '3,本次导入1行数据（跳过一项）'),
    receipt() + '\n未经识别的信息', receipt() + '\n' + receipt(),
    receipt().replace('2,开始检查格式...', '2,开始检查格式...\n2,开始检查格式...'),
    '__IMPORT_FAILURE__\n未导入数据提示Excel\n' + receipt(),
    receipt() + 'x' * 12000,
])
def test_incomplete_partial_ambiguous_or_wrong_row_receipt_remains_unknown(rig, progress):
    transport, spec = rig
    transport.prepare(spec)
    browser = Browser(); browser.progress = progress
    result = transport.execute(spec.intent_id, browser)
    assert result.status == 'unknown' and result.completion_basis == '' and not result.erp_readback_performed
    assert browser.clicks == 1 and browser.lookups == browser.details == 0
    assert transport.execute(spec.intent_id, None).status == 'unknown'
    assert browser.clicks == 1


def test_timeout_waiting_for_progress_is_unknown_without_readback(rig):
    transport, spec = rig
    transport.prepare(spec)
    browser = Browser(); browser.progress_error = True
    outcome = transport.execute(spec.intent_id, browser)
    assert outcome.status == 'unknown' and outcome.code == 'web_import_confirmation_unavailable'
    assert browser.clicks == 1 and browser.lookups == browser.details == 0
    assert transport.execute(spec.intent_id, None).status == 'unknown'


def test_stage_failure_does_not_record_attempt_and_can_be_retried(rig):
    transport, spec = rig
    saved = transport.prepare(spec)
    browser = Browser(); browser.stage_error = True
    with pytest.raises(WebImportBlocked, match='upload_stage_unavailable'):
        transport.execute(spec.intent_id, browser)
    assert not (saved.parent / 'attempt.json').exists() and browser.clicks == 0
    browser.stage_error = False
    assert transport.execute(spec.intent_id, browser).status == 'imported'
    assert browser.clicks == 1 and browser.lookups == browser.details == 0


def test_account_switch_blocks_upload_but_cached_recovery_never_opens_browser(rig):
    transport, spec = rig
    saved = transport.prepare(spec)
    browser = Browser(); browser.identity = ('profile-P', 'other-account')
    with pytest.raises(WebImportBlocked, match='account_mismatch'):
        transport.execute(spec.intent_id, browser)
    assert browser.clicks == 0 and not (saved.parent / 'attempt.json').exists()
    browser.identity = ('profile-P', 'account-A')
    assert transport.execute(spec.intent_id, browser).status == 'imported'
    before = browser.identities
    browser.identity = ('profile-P', 'other-account')
    assert transport.execute(spec.intent_id, browser).status == 'imported'
    assert browser.identities == before and browser.lookups == browser.details == 0


def test_second_intent_cannot_claim_same_number_for_same_account(rig):
    transport, spec = rig
    transport.prepare(spec)
    with pytest.raises(WebImportBlocked, match='order_number_claimed'):
        transport.prepare(replace(spec, intent_id=str(uuid.uuid4())))


def test_concurrent_intents_cannot_claim_one_number_twice(rig):
    transport, spec = rig
    def claim(item):
        try:
            return transport.prepare(item)
        except WebImportBlocked as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (spec, replace(spec, intent_id=str(uuid.uuid4())))))
    assert sum(isinstance(item, Path) for item in results) == 1
    assert results.count('order_number_claimed') == 1


def test_concurrent_same_intent_executes_one_upload(rig):
    transport, spec = rig
    transport.prepare(spec)
    browser = Browser()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: transport.execute(spec.intent_id, browser), range(2)))
    assert all(item.status == 'imported' for item in results)
    assert results[0] == results[1]
    assert browser.clicks == browser.stages == 1 and browser.details == browser.lookups == 0


def test_existing_marker_blocks_click_after_torn_marker_write(rig, monkeypatch):
    from core import sales_web_transport as module
    transport, spec = rig
    saved = transport.prepare(spec)
    original = module._write_exclusive
    def torn(path, data):
        original(path, data)
        if path.name == 'attempt.json':
            raise OSError('simulated fsync uncertainty')
    monkeypatch.setattr(module, '_write_exclusive', torn)
    browser = Browser()
    with pytest.raises(OSError):
        transport.execute(spec.intent_id, browser)
    assert (saved.parent / 'attempt.json').exists() and browser.clicks == 0
    assert transport.execute(spec.intent_id, None).status == 'unknown'
    assert browser.clicks == 0 and browser.lookups == browser.details == 0


def test_old_verified_receipt_is_not_web_import_confirmation(rig):
    transport, spec = rig
    saved = transport.prepare(spec)
    (saved.parent / 'attempt.json').write_text('')
    (saved.parent / 'verified.json').write_text(json.dumps({'status': 'verified', 'code': 'verified'}))
    browser = Browser()
    assert transport.execute(spec.intent_id, browser).status == 'unknown'
    assert browser.clicks == browser.stages == browser.identities == browser.lookups == browser.details == 0


@pytest.mark.parametrize('key,value', [('file_sha256','f'*64), ('expected_rows',2),
    ('intent_id',str(uuid.uuid4())), ('order_number','OTHER'), ('progress_sha256','f'*64),
    ('progress_text',receipt(2))])
def test_cached_confirmation_must_match_original_file_intent_and_rows(rig, key, value):
    transport, spec = rig
    saved = transport.prepare(spec)
    assert transport.execute(spec.intent_id, Browser()).status == 'imported'
    path = saved.parent / 'completed.json'
    receipt_data = json.loads(path.read_text()); receipt_data[key] = value
    path.write_text(json.dumps(receipt_data))
    assert transport.execute(spec.intent_id, None).status == 'unknown'


def test_confirmation_persistence_failure_stays_unknown_and_cannot_resubmit(rig, monkeypatch):
    from core import sales_web_transport as module
    transport, spec = rig
    transport.prepare(spec)
    original = module._replace_json
    def fail_receipt(path, value):
        if path.name == 'completed.json':
            raise OSError('disk full')
        return original(path, value)
    monkeypatch.setattr(module, '_replace_json', fail_receipt)
    browser = Browser()
    result = transport.execute(spec.intent_id, browser)
    assert result.status == 'unknown' and result.code == 'web_import_confirmation_not_persisted'
    assert transport.execute(spec.intent_id, None).status == 'unknown' and browser.clicks == 1


def test_xls_is_revalidated_immediately_before_browser_staging(rig, monkeypatch):
    from core import sales_web_transport as module
    transport, spec = rig
    transport.prepare(spec)
    original = module._validate_sales_workbook
    checks = []
    def verify(content, manifest):
        checks.append((hashlib.sha256(content).hexdigest(), manifest['note']))
        return original(content, manifest)
    monkeypatch.setattr(module, '_validate_sales_workbook', verify)
    browser = Browser()
    assert transport.execute(spec.intent_id, browser).status == 'imported'
    assert checks and all(item == (spec.source_sha256, '已确认') for item in checks)


def test_corrupt_or_replaced_workbook_cannot_be_staged(rig):
    transport, spec = rig
    sha = workbook(spec.source_path, kind='销售单')
    with pytest.raises(WebImportBlocked, match='import_workbook_invalid'):
        transport.prepare(replace(spec, source_sha256=sha))
    sha = workbook(spec.source_path)
    saved = transport.prepare(replace(spec, source_sha256=sha))
    saved.write_bytes(b'changed frozen workbook')
    browser = Browser()
    with pytest.raises(WebImportBlocked, match='intent_corrupt'):
        transport.execute(spec.intent_id, browser)
    assert browser.clicks == browser.stages == 0


@pytest.mark.parametrize('change', [{'unit': ''}, {'unit': '盒'}, {'customer': '其他客户'}, {'kind': '销售单'}])
def test_business_cell_mismatch_blocks_before_browser(rig, change):
    transport, spec = rig
    sha = workbook(spec.source_path, **change)
    with pytest.raises(WebImportBlocked, match='import_workbook_invalid'):
        transport.prepare(replace(spec, source_sha256=sha))


@pytest.fixture
def multi_rig():
    with tempfile.TemporaryDirectory(dir=SERVICE / "runtime") as name:
        runtime = Path(name)
        source = runtime / "reviewed-multi.xls"
        line1 = {"line_id": "1", "sku": "LS3073", "product_name": PRODUCT,
                 "sku_id": "sku-1", "warehouse_id": "08390", "base_unit_id": "1",
                 "delivery_date": "2026-10-15", "detail_note":
                 "PO-123#项目甲#1#图号甲#六角头螺栓#M20X80#8.8级#镀锌#1#LS3073",
                 "quantity": "1", "untaxed_unit_price": "0.040", "untaxed_line_amount": "0.04",
                 "tax_rate_percent": "13", "tax_amount": "0.01",
                 "taxed_unit_price": "0.05", "taxed_line_amount": "0.05"}
        line2 = {**line1, "line_id": "2", "detail_note":
                 "PO-123#项目甲#2#图号甲#六角头螺栓#M20X80#8.8级#镀锌#1#LS3073"}
        lines = (line1, line2)
        listing = {**LIST, "untaxed_total": "0.08", "taxed_total": "0.10"}
        header = {"number": NUMBER, "maker": "罗健树",
                  "summary": line1["detail_note"] + "#交期=2026-10-15", "note": "已确认"}
        book = xlwt.Workbook()
        sheet = book.add_sheet("单据导入模板")
        book.add_sheet("单据类型")
        for index, name in enumerate(WEB_IMPORT_HEADERS):
            sheet.write(0, index, name)
        day_style = xlwt.easyxf(num_format_str="yyyy-mm-dd")
        for row_index, line in enumerate(lines, start=1):
            values = {"单据编号": NUMBER, "单据类型": "销售订单",
                      "往来单位名称": listing["customer"], "部门名称": "销售部",
                      "摘要": header["summary"], "附加说明": "已确认",
                      "制单人名称": header["maker"], "经手人名称": listing["salesperson"],
                      "仓库名称": listing["warehouse"], "商品编号": line["sku"],
                      "商品名称": line["product_name"], "明细仓库名称": listing["warehouse"],
                      "单位": "支", "数量": int(line["quantity"]),
                      "折前单价": float(line["untaxed_unit_price"]),
                      "折前金额": float(line["untaxed_line_amount"]),
                      "税率(%)": line["tax_rate_percent"], "明细备注": line["detail_note"]}
            for index, name in enumerate(WEB_IMPORT_HEADERS):
                if name in ("录单日期", "交货日期"):
                    sheet.write(row_index, index, date.fromisoformat(
                        listing["date"] if name == "录单日期" else line["delivery_date"]), day_style)
                elif name in values:
                    sheet.write(row_index, index, values[name])
        book.save(str(source))
        spec = WebImportSpec(str(uuid.uuid4()), NUMBER, "profile-P", "account-A",
                             source, hashlib.sha256(source.read_bytes()).hexdigest(),
                             listing, header, "销售部", PRODUCT, "已确认", line1["detail_note"],
                             lines, "1", "08390")
        yield SalesWebImportTransport(runtime), spec


def test_multiline_duplicate_sku_is_one_import_and_counts_every_xls_line(multi_rig):
    transport, spec = multi_rig
    transport.prepare(spec)
    browser = Browser(); browser.progress = receipt(2)
    result = transport.execute(spec.intent_id, browser)
    assert result.status == 'imported' and result.imported_rows == result.expected_rows == 2
    assert result.table_verified and not result.erp_readback_performed and not result.unit_verified
    assert browser.clicks == 1 and browser.lookups == browser.details == 0
    assert transport.execute(spec.intent_id, None) == result


def test_multiline_partial_row_count_is_unknown_without_new_upload(multi_rig):
    transport, spec = multi_rig
    transport.prepare(spec)
    browser = Browser(); browser.progress = receipt(1)
    result = transport.execute(spec.intent_id, browser)
    assert result.status == 'unknown' and result.code == 'web_import_row_count_mismatch'
    assert result.imported_rows == 1 and result.expected_rows == 2
    assert transport.execute(spec.intent_id, None).status == 'unknown' and browser.clicks == 1
    assert browser.lookups == browser.details == 0


def test_multiline_distinct_delivery_dates_are_checked_in_xls_without_readback(multi_rig):
    transport, spec = multi_rig
    changed = dict(spec.lines_expected[1], delivery_date='2026-10-16')
    original = xlrd.open_workbook(str(spec.source_path), formatting_info=True)
    edited = copy(original)
    edited.get_sheet(0).write(2, WEB_IMPORT_HEADERS.index('交货日期'), date(2026, 10, 16),
                            xlwt.easyxf(num_format_str='yyyy-mm-dd'))
    edited.save(str(spec.source_path))
    spec = replace(spec, source_sha256=hashlib.sha256(spec.source_path.read_bytes()).hexdigest(),
                   lines_expected=(spec.lines_expected[0], changed))
    transport.prepare(spec)
    browser = Browser(); browser.progress = receipt(2)
    assert transport.execute(spec.intent_id, browser).status == 'imported'
    assert browser.lookups == browser.details == 0


@pytest.mark.parametrize("column,value", [
    (15, "OTHER-SKU"), (18, ""), (19, 2), (24, "wrong line note"),
])
def test_multiline_second_xls_row_must_match_reviewed_line(multi_rig, column, value):
    transport, spec = multi_rig
    original = xlrd.open_workbook(str(spec.source_path), formatting_info=True)
    edited = copy(original)
    edited.get_sheet(0).write(2, column, value)
    edited.save(str(spec.source_path))
    changed = replace(spec, source_sha256=hashlib.sha256(spec.source_path.read_bytes()).hexdigest())
    with pytest.raises(WebImportBlocked, match="import_workbook_invalid"):
        transport.prepare(changed)


def test_multiline_manifest_rejects_duplicate_id_and_wrong_line_tax(multi_rig):
    transport, spec = multi_rig
    duplicate = dict(spec.lines_expected[1], line_id="1")
    with pytest.raises(WebImportBlocked, match="readback_lines_invalid"):
        transport.prepare(replace(spec, lines_expected=(spec.lines_expected[0], duplicate)))
    wrong_tax = dict(spec.lines_expected[1], tax_amount="0.00")
    with pytest.raises(WebImportBlocked, match="readback_lines_invalid"):
        transport.prepare(replace(spec, lines_expected=(spec.lines_expected[0], wrong_tax)))


def test_multiline_base_unit_and_single_warehouse_identity_cannot_be_overridden(multi_rig):
    transport, spec = multi_rig
    wrong_unit = dict(spec.lines_expected[1], base_unit_id="2")
    with pytest.raises(WebImportBlocked, match="readback_lines_invalid"):
        transport.prepare(replace(spec, lines_expected=(spec.lines_expected[0], wrong_unit)))
    wrong_warehouse = dict(spec.lines_expected[1], warehouse_id="other")
    with pytest.raises(WebImportBlocked, match="warehouse_identity_mismatch"):
        transport.prepare(replace(spec, lines_expected=(spec.lines_expected[0], wrong_warehouse)))

