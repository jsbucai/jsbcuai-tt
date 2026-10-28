"""No real Excel, browser, ERP login, upload, or physical printing in these tests."""
import json
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import erp_adjustments as adapter
from core.adjustments import AdjustmentFailure


def receipt(rows=2):
    # Actual numbered completion wording from saved 5088 screenshots (no live request).
    return f'业务单据导入进度\n1,开始导入...\n2,开始检查格式...\n3,本次导入{rows}行数据，开始写入数据...\n4,----完成----\n关闭'


@dataclass(frozen=True)
class Config:
    base_dir: Path
    browser_profile_dir: Path
    keep_browser_open: bool = True
    chrome_executable: Path = Path('fake-chrome.exe')
    headless: bool = False
    slow_mo_ms: int = 20
    operation_timeout_ms: int = 90000

    @property
    def logs_dir(self):
        return self.base_dir / '自动化脚本' / 'logs'


class Sheet:
    def __init__(self, headers):
        self.cells = {(1, i): SimpleNamespace(Value=h) for i, h in enumerate(headers, 1)}
        self.UsedRange = SimpleNamespace(Columns=SimpleNamespace(Count=len(headers)), Rows=SimpleNamespace(Count=7))
        self.cleared = False

    def Cells(self, row, col):
        return self.cells.setdefault((row, col), SimpleNamespace())

    def Rows(self, row):
        return row

    def Range(self, first, last):
        assert (first, last) == (2, 7)
        def clear():
            self.cleared = True
        return SimpleNamespace(ClearContents=clear)


class Page:
    def __init__(self):
        self.submitted = False
        self.clicks = 0
        self.receipt = receipt()
        self.stale = ''
        self.body = ''
        self.click_error = False
        self.wait_error = False
        self.read_error = False
        self.files = []
        self.selected = []
        self.keys = []
        self.mouse_clicks = []
        self.native = True
        self.frames = [self]
        self.keyboard = SimpleNamespace(press=self.keys.append)
        self.mouse = SimpleNamespace(click=lambda *pos: self.mouse_clicks.append(pos))

    def locator(self, value):
        if value == 'select':
            select = SimpleNamespace(is_visible=lambda: True, select_option=lambda **kw: self.selected.append(kw['label']))
            return SimpleNamespace(count=lambda: int(self.native), nth=lambda index: select)
        if value == 'body':
            return SimpleNamespace(inner_text=lambda **kw: self.body)
        assert value == 'input[type=file]'
        def wait(**kw):
            if self.wait_error:
                raise TimeoutError('fake file input timeout')
        return SimpleNamespace(last=SimpleNamespace(wait_for=wait, set_input_files=self.files.append))

    def get_by_text(self, text, **kw):
        assert (text, kw) == ('导入文件', {'exact': True})
        def click(**kwargs):
            self.clicks += 1
            self.submitted = True
            if self.click_error:
                raise TimeoutError('fake click timeout after dispatch')
        return SimpleNamespace(last=SimpleNamespace(click=click))

    def evaluate(self, script):
        if 'fileInputs' in script:
            return {'x': 20, 'y': 30, 'width': 100, 'height': 20}
        if self.read_error and self.submitted:
            raise TimeoutError('fake receipt read failure')
        return self.receipt if self.submitted else self.stale

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path, **kw):
        Path(path).write_bytes(b'fake screenshot')


@pytest.fixture
def rig(monkeypatch):
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='erp-adapter-test-', dir=ROOT / 'runtime') as directory:
        runtime = Path(directory)
        template = runtime / 'template.xls'
        template.write_bytes(b'untouched fake template')
        monkeypatch.setattr(adapter, 'TEMPLATE_PATH', template)
        opened, workbooks, sessions = [], [], []
        sheets = []
        headers = list(adapter.ADJUSTMENT_IMPORT_HEADERS)
        com_counts = {'init': 0, 'uninit': 0, 'quit': 0}
        pythoncom = ModuleType('pythoncom')
        def count(key):
            com_counts[key] += 1
        pythoncom.CoInitialize = lambda: count('init')
        pythoncom.CoUninitialize = lambda: count('uninit')
        def dispatch(name):
            assert name == 'Excel.Application'
            def open_book(path, **kwargs):
                opened.append((path, kwargs))
                sheet = Sheet(headers)
                sheets.append(sheet)
                book = SimpleNamespace(saved=0, closed=0, Worksheets=lambda index: sheet)
                book.Save = lambda: setattr(book, 'saved', book.saved + 1)
                book.Close = lambda save: setattr(book, 'closed', book.closed + 1)
                workbooks.append(book)
                return book
            excel = SimpleNamespace(Workbooks=SimpleNamespace(Open=open_book), Quit=lambda: count('quit'))
            sessions.append(excel)
            return excel
        win32 = ModuleType('win32com')
        client = ModuleType('win32com.client')
        client.DispatchEx = dispatch
        win32.client = client
        monkeypatch.setitem(sys.modules, 'pythoncom', pythoncom)
        monkeypatch.setitem(sys.modules, 'win32com', win32)
        monkeypatch.setitem(sys.modules, 'win32com.client', client)
        config = Config(Path('E:/UL/ERP自动化'), Path('E:/UL/ERP自动化/自动化脚本/chrome-profile'))
        page = Page()
        browser = SimpleNamespace(closed=0, stopped=0, configs=[], owned=True)
        context = SimpleNamespace(close=lambda: setattr(browser, 'closed', browser.closed + 1))
        pw = SimpleNamespace(stop=lambda: setattr(browser, 'stopped', browser.stopped + 1))
        def launch(private_config):
            browser.configs.append(private_config)
            return pw, context, page, browser.owned
        erp = SimpleNamespace(load_config=lambda: config, ensure_logged_in=lambda *args: None,
                              js_click_text=lambda *a, **kw: pytest.fail('unexpected fallback confirmation'),
                              js_click_dialog_button=lambda *a: pytest.fail('unexpected confirmation'))
        purchase = SimpleNamespace(open_erp_business_import=lambda *args: None)
        monkeypatch.setattr(adapter, '_load_automation_modules', lambda: (erp, purchase))
        monkeypatch.setattr(adapter, 'launch_private_browser', launch)
        sender = adapter.ErpAdjustmentSender(runtime, receipt_timeout_seconds=.002)
        batch = {'id': str(uuid.uuid4()), 'created_at': '2026-09-10T11:22:33', 'type': 'loss', 'items': [
            {'sku': 'LS001', 'quantity': '2.5', 'note': 'DIN933  M12*40'},
            {'sku': 'LS001', 'quantity': '3', 'note': '=原样备注'},
        ]}
        yield SimpleNamespace(runtime=runtime, template=template, opened=opened, sheets=sheets, headers=headers,
                              workbooks=workbooks, sessions=sessions, com_counts=com_counts, config=config,
                              page=page, browser=browser, sender=sender, batch=batch, erp=erp)


@pytest.mark.parametrize('kind', ['loss', 'overflow'])
def test_one_file_per_type_preserves_vetted_columns_quantity_notes_and_private_profile(rig, kind):
    rig.batch['type'] = kind
    result = rig.sender.send(rig.batch)
    assert result['document_no'] == '2026-09-10-11-22-33-' + uuid.UUID(rig.batch['id']).hex[:8]
    assert len(result['document_no']) == 28
    assert len(rig.opened) == 1 and rig.page.clicks == 1
    assert rig.page.selected == [adapter.ADJUSTMENT_TYPES[kind]['document_type']]
    assert rig.template.read_bytes() == b'untouched fake template'
    path, options = rig.opened[0]
    assert Path(path).is_relative_to(rig.runtime / 'adjustments')
    assert options == {'UpdateLinks': 0, 'ReadOnly': False}
    sheet = rig.sheets[0]
    cell = lambda row, header: sheet.Cells(row, rig.headers.index(header) + 1)
    assert len(rig.headers) == 27 and sheet.cleared
    for row in (2, 3):
        assert cell(row, '商品编号').Value2 == 'LS001'
        assert cell(row, '单据编号').Value2 == result['document_no']
        assert cell(row, '单据类型').Value2 == adapter.ADJUSTMENT_TYPES[kind]['document_type']
        assert cell(row, '仓库名称').Value2 == '江苏优螺'
        assert cell(row, '制单人名称').Value2 == cell(row, '经手人名称').Value2 == '罗健树'
        assert cell(row, '录单日期').NumberFormat == 'yy-mm-dd'
    assert [cell(row, '数量').Value2 for row in (2, 3)] == [2.5, 3]
    assert cell(2, '明细备注').Value2 == 'DIN933  M12*40'
    assert cell(3, '明细备注').Value2 == '=原样备注'
    assert cell(3, '明细备注').NumberFormat == '@'
    assert rig.workbooks[0].saved == rig.workbooks[0].closed == 1
    assert rig.com_counts == {'init': 1, 'uninit': 1, 'quit': 1}
    private = rig.browser.configs[0]
    assert private is not rig.config and rig.config.keep_browser_open is True
    assert private.browser_profile_dir.is_relative_to(rig.runtime)
    assert private.logs_dir.is_relative_to(rig.runtime)
    assert private.keep_browser_open is False and rig.browser.closed == rig.browser.stopped == 1
    saved = json.loads((Path(path).parent / 'result.json').read_text(encoding='utf-8'))
    assert saved['row_count'] == 2 and saved['receipt'] == receipt()


@pytest.mark.parametrize('quantity', ['0', '-1', 'NaN', 'Infinity', '1e999', 'bad'])
def test_invalid_positive_quantity_rejected_before_excel_or_browser(rig, quantity):
    rig.batch['items'][0]['quantity'] = quantity
    with pytest.raises(AdjustmentFailure):
        rig.sender.send(rig.batch)
    assert not rig.opened and not rig.browser.configs


@pytest.mark.parametrize('text', ['', '导入成功', '业务单据导入进度\n4,----完成----',
                                  receipt(1), receipt() + '\n未导入数据提示Excel',
                                  receipt().replace('4,----完成----', '4,正在写入')])
def test_receipt_requires_count_completion_and_no_failure(text):
    assert not adapter.receipt_complete(text, 2)
    assert adapter.receipt_complete(receipt(), 2)


def test_click_timeout_marks_unknown_and_same_batch_never_submits_twice(rig):
    rig.page.click_error = True
    with pytest.raises(adapter.AdjustmentUnknown) as error:
        rig.sender.send(rig.batch)
    assert error.value.document_no == '2026-09-10-11-22-33-' + uuid.UUID(rig.batch['id']).hex[:8]
    marker = rig.runtime / 'adjustments' / rig.batch['id'] / 'submission.json'
    assert json.loads(marker.read_text(encoding='utf-8'))['document_no'] == error.value.document_no
    rig.page.click_error = False
    with pytest.raises(adapter.AdjustmentUnknown):
        rig.sender.send(rig.batch)
    assert rig.page.clicks == 1 and len(rig.opened) == 1
    assert rig.browser.closed == rig.browser.stopped == 1


@pytest.mark.parametrize('failure', ['no_receipt', 'wrong_count', 'partial_failure', 'read_error'])
def test_postsubmit_uncertainty_never_returns_success(rig, failure):
    if failure == 'no_receipt': rig.page.receipt = ''
    if failure == 'wrong_count': rig.page.receipt = receipt(1)
    if failure == 'partial_failure': rig.page.body = '未导入数据提示Excel'
    if failure == 'read_error': rig.page.read_error = True
    with pytest.raises(adapter.AdjustmentUnknown):
        rig.sender.send(rig.batch)
    folder = rig.runtime / 'adjustments' / rig.batch['id']
    assert (folder / 'submission.json').exists() and not (folder / 'result.json').exists()
    assert rig.page.clicks == 1


def test_safe_presubmit_failure_can_retry_same_document_without_duplicate_rows(rig):
    rig.page.wait_error = True
    with pytest.raises(AdjustmentFailure) as error:
        rig.sender.send(rig.batch)
    assert error.value.retryable and rig.page.clicks == 0
    assert not (rig.runtime / 'adjustments' / rig.batch['id'] / 'submission.json').exists()
    rig.page.wait_error = False
    rig.batch['created_at'] = '2026-09-11T23:59:59'  # The durable prepared number wins on recovery.
    rig.sender.send(rig.batch)
    assert rig.page.clicks == 1 and len(rig.opened) == 2
    assert rig.opened[0][0] == rig.opened[1][0]
    assert len(list((rig.runtime / 'adjustments').rglob('*.xls'))) == 1


def test_batch_content_cannot_change_under_same_uuid(rig):
    rig.page.wait_error = True
    with pytest.raises(AdjustmentFailure): rig.sender.send(rig.batch)
    rig.batch['items'][0]['quantity'] = '4'
    with pytest.raises(AdjustmentFailure) as error: rig.sender.send(rig.batch)
    assert error.value.retryable is False
    assert rig.page.clicks == 0 and len(rig.opened) == 1


def test_missing_template_header_closes_only_owned_excel_and_never_uploads(rig):
    rig.headers.remove('单据编号')
    with pytest.raises(AdjustmentFailure) as error:
        rig.sender.send(rig.batch)
    assert error.value.retryable
    assert rig.workbooks[0].saved == 0 and rig.workbooks[0].closed == 1
    assert rig.com_counts == {'init': 1, 'uninit': 1, 'quit': 1}
    assert not rig.browser.configs and rig.page.clicks == 0


def test_existing_progress_cannot_be_mistaken_for_current_success(rig):
    rig.page.stale = receipt()
    with pytest.raises(AdjustmentFailure) as error:
        rig.sender.send(rig.batch)
    assert error.value.retryable and rig.page.clicks == 0


def test_torn_marker_prevents_any_submit_even_when_fsync_raises(rig, monkeypatch):
    original = adapter._write_json
    def torn(path, payload, **kwargs):
        original(path, payload, **kwargs)
        if path.name == 'submission.json':
            raise OSError('fake disk sync failure')
    monkeypatch.setattr(adapter, '_write_json', torn)
    with pytest.raises(adapter.AdjustmentUnknown): rig.sender.send(rig.batch)
    assert rig.page.clicks == 0
    with pytest.raises(adapter.AdjustmentUnknown): rig.sender.send(rig.batch)
    assert len(rig.opened) == 1


def test_context_ownership_flag_is_respected(rig):
    rig.browser.owned = False
    rig.sender.send(rig.batch)
    assert rig.browser.closed == 0 and rig.browser.stopped == 1


@pytest.mark.parametrize('count', [5, 6])
def test_vetted_custom_document_dropdown_keeps_keyboard_rule(count):
    page = Page()
    page.native = False
    adapter.select_adjustment_document_type(page, '报损单' if count == 5 else '报溢单', count)
    assert page.keys == ['ArrowDown'] * count + ['Enter']
    assert page.mouse_clicks == [(70, 40)]


def test_private_launcher_ignores_cdp_without_mutating_environment(monkeypatch):
    calls = []
    page = SimpleNamespace(set_default_timeout=lambda value: calls.append(('timeout', value)))
    context = SimpleNamespace(pages=[page])
    def persistent(**kwargs):
        calls.append(('launch', kwargs))
        return context
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=persistent,
        connect_over_cdp=lambda *a: pytest.fail('must never connect to shared CDP')))
    module = ModuleType('playwright.sync_api')
    module.sync_playwright = lambda: SimpleNamespace(start=lambda: pw)
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', module)
    monkeypatch.setenv('ERP_CDP_URL', 'http://do-not-connect.invalid:9222')
    config = Config(Path('private'), Path('private/profile'))
    actual = adapter.launch_private_browser(config)
    assert actual == (pw, context, page, True)
    assert calls[0][1]['user_data_dir'] == str(config.browser_profile_dir)
    assert adapter.os.environ['ERP_CDP_URL'] == 'http://do-not-connect.invalid:9222'


def test_confirmation_timeout_cannot_click_fallback_again(rig):
    calls = []
    rig.page.body = '是否继续'
    rig.erp.AutomationError = RuntimeError
    def ambiguous(*args):
        calls.append('first-confirmation')
        raise TimeoutError('first confirmation may already have been dispatched')
    rig.erp.js_click_dialog_button = ambiguous
    with pytest.raises(adapter.AdjustmentUnknown):
        rig.sender.send(rig.batch)
    assert calls == ['first-confirmation'] and rig.page.clicks == 1
    assert not rig.page.mouse_clicks


@pytest.mark.parametrize('fallback', ['text', 'coordinate'])
def test_confirmation_fallback_only_when_source_reports_nothing_clicked(fallback):
    class AutomationError(Exception):
        pass
    page = Page()
    page.body = '是否继续'
    clicks = []
    def missing_dialog(*args):
        raise AutomationError('找不到弹窗按钮: 是否继续 / 确定')
    def text_confirm(*args, **kwargs):
        if fallback == 'coordinate':
            raise AutomationError('找不到可点击文本: 确定')
        clicks.append('text')
    erp = SimpleNamespace(AutomationError=AutomationError, js_click_dialog_button=missing_dialog,
                          js_click_text=text_confirm)
    adapter._confirm_import(page, erp)
    assert clicks == (['text'] if fallback == 'text' else [])
    assert page.mouse_clicks == ([(648, 493)] if fallback == 'coordinate' else [])
