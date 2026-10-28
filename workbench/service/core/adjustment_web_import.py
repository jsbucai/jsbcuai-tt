"""Linux XLS generation and single-attempt web transport for loss/overflow."""
import hashlib
import json
import os
import uuid
from contextlib import suppress
from datetime import date
from decimal import Decimal
from pathlib import Path

from .adjustments import AdjustmentFailure
from .erp_adjustments import (_items, _document_no, _write_json, receipt_complete,
                              AdjustmentUnknown, ADJUSTMENT_TYPES)
from .sales_web_import import _validate_template, _render, EXPECTED_HEADERS
from .sales_web_transport import PlaywrightSalesWebBrowser


class AdjustmentBrowser(PlaywrightSalesWebBrowser):
    def diagnostic(self):
        # Only the authenticated ERP main page, never password/verification
        # forms, frame source, cookies or arbitrary exception/URL text.
        from urllib.parse import urlsplit
        url = urlsplit(self.page.url)
        if url.scheme != 'https' or not url.path.lower().endswith('/main.gspx'):
            return {}
        self.account_identity()
        return {'control_phase': getattr(self, 'control_phase', ''),
                'page_text': self.page.locator('body').inner_text(timeout=2000)[:16000]}

    def click_upload_once(self):
        # The resource-limited N150 can take longer than five seconds to show
        # ERP's confirmation dialog. Waiting longer never repeats either click.
        self._session()
        self.control_phase = 'upload_button'
        self._ready_control(self.selectors.upload_selector).click(timeout=15000)
        self.control_phase = 'waiting_confirmation'
        self.page.get_by_text(self.selectors.confirm_text, exact=True).wait_for(
            state='visible', timeout=30000)
        self.control_phase = 'confirm_button'
        self._visible_text('确定').click(timeout=15000)
        self.control_phase = 'waiting_receipt'

    def _ready_control(self, selector, *, visible=True):
        # Opening the Wisej menu returns before the server-created import form
        # reaches the DOM. Wait for that one control, never click the menu again
        # or take the first of several ambiguous import windows.
        self.page.locator(selector).wait_for(
            state='visible' if visible else 'attached', timeout=15000)
        return self._one(selector, visible=visible)

    def stage_adjustment(self, path, kind):
        self._session()
        self._menu('.MenuRootCaption', '系统')
        self._menu('.MenuCaption', '导入导出')
        self._menu('.MenuCaption', '业务单据导入')
        doc = self._ready_control(self.selectors.document_type_selector)
        expected = ADJUSTMENT_TYPES[kind]['document_type']
        if doc.input_value().strip() != expected:
            doc.click()
            doc.press('Home')
            for _ in range(30):
                if doc.input_value().strip() == expected:
                    doc.press('Enter')
                    break
                doc.press('ArrowDown')
        if doc.input_value().strip() != expected:
            raise ValueError('报损报溢单据类型未能准确选中')
        control = self._ready_control(self.selectors.file_selector, visible=False)
        control.set_input_files(str(path))
        if control.evaluate("el=>el.files?.[0]?.name || ''") != path.name:
            raise ValueError('未能确认导入文件')
        self._ready_control(self.selectors.upload_selector)
        if self._progress_snapshot():
            raise ValueError('导入前存在旧进度窗口')
        self._session()


def workbook(template, batch, document_no, binding):
    import xlrd
    source = _validate_template(template)
    values = []
    for item in _items(batch):
        quantity = Decimal(item['quantity'])
        if len(quantity.as_tuple().digits) > 15:
            raise ValueError('数量超过 Excel 精确范围')
        values.append({'录单日期': date.fromisoformat(batch['created_at'][:10]),
            '单据编号': document_no, '单据类型': ADJUSTMENT_TYPES[batch['type']]['document_type'],
            '摘要': ADJUSTMENT_TYPES[batch['type']]['document_type'],
            '附加说明': 'UL随行库存调整 ' + batch['id'],
            '制单人名称': binding['maker_name'], '经手人名称': binding['maker_name'],
            '仓库名称': binding['warehouse_name'], '明细仓库名称': binding['warehouse_name'],
            '商品编号': item['sku'], '单位': '支', '数量': float(quantity), '明细备注': item.get('note', '')})
    raw = _render(source, values)
    readback = xlrd.open_workbook(file_contents=raw)
    sheet = readback.sheet_by_index(0)
    if sheet.nrows != len(values) + 1 or tuple(sheet.row_values(0)) != EXPECTED_HEADERS:
        raise ValueError('报损报溢表格行数或列名不一致')
    for index, expected in enumerate(values, 1):
        for column, name in enumerate(EXPECTED_HEADERS):
            actual, value = sheet.cell_value(index, column), expected.get(name, '')
            if name == '录单日期':
                if xlrd.xldate_as_datetime(actual, readback.datemode).date() != value:
                    raise ValueError('表格日期校验失败')
            elif name == '数量':
                if Decimal(str(actual)) != Decimal(str(value)):
                    raise ValueError('表格数量校验失败')
            elif actual != value:
                raise ValueError('表格字段校验失败: ' + name)
    return raw


def prepare_workbook(runtime, template, batch, binding):
    identity = str(uuid.UUID(batch['id']))
    root = Path(runtime) / 'adjustments' / identity
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.resolve().is_relative_to(Path(runtime).resolve()):
        raise ValueError('批次目录无效')
    number = _document_no(batch, identity)
    digest = hashlib.sha256(json.dumps([batch['items'], binding, batch['type']],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    saved = root / 'prepared.json'
    if saved.exists():
        previous = json.loads(saved.read_text())
        if previous['content_sha256'] != digest:
            raise ValueError('库存批次内容已变化，拒绝覆盖')
        number = previous['document_no']
    else:
        _write_json(saved, {'batch_id': identity, 'document_no': number,
                           'content_sha256': digest}, exclusive=True)
    path = root / 'import.xls'
    raw = workbook(template, batch, number, binding)
    if (root / 'submission.json').exists():
        return path, number
    with path.open('wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return path, number


def submit_once(path, number, batch, browser, binding, progress=lambda value: None):
    root = path.parent
    marker, receipt_path = root / 'submission.json', root / 'receipt.json'
    if marker.exists():
        if receipt_path.exists():
            saved = json.loads(receipt_path.read_text())
            if saved.get('batch_id') == batch['id'] and saved.get('document_no') == number and receipt_complete(saved.get('receipt', ''), len(batch['items'])):
                return {'document_no': number, 'message': 'ERP 已完成本批导入（从原回执恢复）'}
        raise AdjustmentUnknown(number)
    try:
        if browser.account_identity() != (binding['profile_id'], binding['account_id']):
            raise ValueError('ERP 账套身份不一致')
        progress('正在核对导入表格和单据类型')
        browser.stage_adjustment(path, batch['type'])
    except Exception as error:
        _save_diagnostic(root, batch, 'before_upload', error, browser)
        raise AdjustmentFailure('导入前核对未通过，尚未提交 ERP，可核对后重试', retryable=True) from None
    _write_json(marker, {'batch_id': batch['id'], 'document_no': number,
        'file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'row_count': len(batch['items'])}, exclusive=True)
    progress('正在导入 ERP，等待本次完成回执，请勿重复提交')
    try:
        browser.click_upload_once()
        receipt = browser.visible_import_progress()
        if not receipt_complete(receipt, len(batch['items'])):
            raise ValueError('ERP 完成回执不完整')
        _write_json(receipt_path, {'batch_id': batch['id'], 'document_no': number, 'receipt': receipt}, exclusive=True)
    except Exception as error:
        _save_diagnostic(root, batch, 'after_attempt', error, browser)
        raise AdjustmentUnknown(number) from None
    return {'document_no': number, 'message': f"ERP {ADJUSTMENT_TYPES[batch['type']]['document_type']}导入完成，共 {len(batch['items'])} 行，未自动审核"}


def _save_diagnostic(root, batch, phase, error, browser):
    # One bounded private file per batch. Diagnostic failures must never turn
    # an uncertain upload into a retryable failure or erase its attempt marker.
    data = {'batch_id': batch['id'], 'phase': phase, 'error_type': type(error).__name__}
    with suppress(Exception):
        if callable(getattr(browser, 'diagnostic', None)):
            data.update(browser.diagnostic())
    with suppress(Exception):
        _write_json(root/'diagnostic.json', data)
