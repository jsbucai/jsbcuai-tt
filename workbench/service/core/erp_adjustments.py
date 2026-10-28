"""Versioned 5088 adjustment adapter, isolated from its queue and browser profile.

Business layout and document selection originate in 5088 app.py:2100-2311.
Positive receipt grammar was observed in existing, read-only screenshots:
20260909_084111-报溢单-upload.png and 20260829_121622-报损单-upload.png.
No ERP/browser activity occurs until send() is explicitly invoked by the worker.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .adjustments import AdjustmentFailure
from .store import guarded_runtime

TEMPLATE_PATH = Path(r'E:\UL\ERP自动化\业务单据导入模板报损报溢.xls')
ERP_SCRIPT_DIR = Path(r'E:\UL\ERP自动化\自动化脚本')
ADJUSTMENT_TYPES = {
    'loss': {'document_type': '报损单', 'prefix': 'ERP报损单导入', 'arrow_down': 5},
    'overflow': {'document_type': '报溢单', 'prefix': 'ERP报溢单导入', 'arrow_down': 6},
}
ADJUSTMENT_IMPORT_HEADERS = [
    '录单日期', '单据编号', '单据类型', '往来单位编号', '往来单位名称', '部门名称',
    '摘要', '附加说明', '制单人名称', '经手人名称', '仓库名称', '收付款账户编号',
    '收付款账户金额', '交货日期', '商品条码', '商品编号', '商品名称', '明细仓库名称',
    '单位', '数量', '折前单价', '折前金额', '折扣', '税率(%)', '明细备注', '副单位数量', '是否赠品',
]
FAILURE_WORDS = ('未导入', '导入失败', '上传失败', '格式错误', '失败原因', '异常', '错误', '失败')
PAGE_FAILURE_WORDS = ('未导入数据提示Excel', '未导入', '导入失败', '上传失败', '格式错误')
PROGRESS_TITLE = '业务单据导入进度'
DOCUMENT_NO_RE = re.compile(r'\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-[0-9a-f]{8}')


class AdjustmentUnknown(RuntimeError):
    def __init__(self, document_no):
        self.document_no = document_no
        super().__init__(f'ERP 提交结果待确认，禁止重复上传；请核对单据 {document_no}')


class AdjustmentImportConfig:
    erp_purchase_import_url = ''
    erp_purchase_import_file_selector = 'input[type=file]'
    erp_purchase_import_submit_text = '导入文件'


def _clean(value):
    return str(value if value is not None else '').replace('\r', ' ').replace('\n', ' ').strip()


def _document_no(batch, batch_id):
    try:
        created = datetime.fromisoformat(str(batch.get('created_at', '')).replace('Z', '+00:00'))
    except ValueError:
        raise AdjustmentFailure('批次缺少固定创建时间，尚未提交', retryable=True)
    # Keep 5088's timestamp/UUID-suffix grammar and maximum observed length.
    return created.strftime('%Y-%m-%d-%H-%M-%S') + '-' + uuid.UUID(batch_id).hex[:8]


def _saved_document_no(path, fallback):
    try:
        value = json.loads(path.read_text(encoding='utf-8')).get('document_no', '')
        return value if isinstance(value, str) and DOCUMENT_NO_RE.fullmatch(value) else fallback
    except (ValueError, OSError, AttributeError):
        return fallback


def _items(batch):
    rows = batch.get('items')
    if batch.get('type') not in ADJUSTMENT_TYPES or not isinstance(rows, list) or not rows:
        raise AdjustmentFailure('报损报溢类型或明细无效，尚未提交', retryable=True)
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise AdjustmentFailure('报损报溢明细格式无效，尚未提交', retryable=True)
        sku = _clean(row.get('sku')).upper()
        try:
            quantity = Decimal(str(row.get('quantity', '')).replace(',', ''))
            numeric = float(quantity)
            valid = quantity.is_finite() and quantity > 0 and math.isfinite(numeric) and numeric > 0
        except (InvalidOperation, ValueError, OverflowError):
            valid = False
        if not sku or not valid:
            raise AdjustmentFailure('每条明细均须有商品编号及大于零的有效数量，尚未提交', retryable=True)
        if row.get('type', batch['type']) != batch['type']:
            raise AdjustmentFailure('同一导入单不能混合报损与报溢，尚未提交', retryable=True)
        output.append({'sku': sku, 'quantity': numeric, 'note': _clean(row.get('note'))})
    return output


def write_adjustment_import_file(template_path, output_path, adjustment_type, items, document_no):
    """Copy the vetted 27-column template, then write a single workbook per batch."""
    import pythoncom
    import win32com.client

    if not template_path.is_file():
        raise RuntimeError('报损报溢导入模板不存在')
    shutil.copyfile(template_path, output_path)
    pythoncom.CoInitialize()
    excel = workbook = None
    try:
        excel = win32com.client.DispatchEx('Excel.Application')
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.EnableEvents = False
        excel.AutomationSecurity = 3
        workbook = excel.Workbooks.Open(str(output_path), UpdateLinks=0, ReadOnly=False)
        sheet = workbook.Worksheets(1)
        header_to_col = {_clean(sheet.Cells(1, col).Value): col
                         for col in range(1, sheet.UsedRange.Columns.Count + 1)}
        missing = [header for header in ADJUSTMENT_IMPORT_HEADERS if header not in header_to_col]
        if missing:
            raise RuntimeError('报损报溢模板表头不完整：' + ', '.join(missing))
        if sheet.UsedRange.Rows.Count > 1:
            sheet.Range(sheet.Rows(2), sheet.Rows(sheet.UsedRange.Rows.Count)).ClearContents()
        kind = ADJUSTMENT_TYPES[adjustment_type]['document_type']
        for index, item in enumerate(items, start=2):
            values = {'录单日期': date.today(), '单据编号': document_no, '单据类型': kind,
                      '摘要': kind, '制单人名称': '罗健树', '经手人名称': '罗健树',
                      '仓库名称': '江苏优螺', '商品编号': item['sku'], '数量': item['quantity'],
                      '明细备注': item['note']}
            for header, value in values.items():
                cell = sheet.Cells(index, header_to_col[header])
                if isinstance(value, date):
                    cell.Value2 = (value - date(1899, 12, 30)).days
                    cell.NumberFormat = 'yy-mm-dd'
                else:
                    if isinstance(value, str):
                        cell.NumberFormat = '@'  # Preserve SKU/notes literally, including leading zeroes.
                    cell.Value2 = value
        workbook.Save()
    finally:
        if workbook is not None:
            with contextlib.suppress(Exception):
                workbook.Close(False)
        if excel is not None:
            with contextlib.suppress(Exception):
                excel.Quit()
        pythoncom.CoUninitialize()
    return output_path


def _load_automation_modules():
    if str(ERP_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(ERP_SCRIPT_DIR))
    # Do not import the 5088 Flask app, run CLI main(), or load purchase settings.
    return importlib.import_module('erp_automation'), importlib.import_module('purchase_automation')


def launch_private_browser(config):
    """The original launcher's persistent-context branch; deliberately no CDP path.

    Never temporarily mutate os.environ: other threads may launch ERP concurrently.
    All callers retain the original (pw, context, page, close_context) ownership API.
    """
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    context = None
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(config.browser_profile_dir), executable_path=str(config.chrome_executable),
            headless=config.headless, slow_mo=config.slow_mo_ms, accept_downloads=True,
            viewport={'width': 1920, 'height': 930},
            args=['--no-first-run', '--disable-features=Translate', '--window-position=0,0',
                  '--window-size=1920,1080', '--start-maximized'],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(config.operation_timeout_ms)
        return pw, context, page, True
    except Exception:
        if context is not None:
            with contextlib.suppress(Exception):
                context.close()
        with contextlib.suppress(Exception):
            pw.stop()
        raise


def _progress_text(page):
    # Read only the visible import-progress dialog, never the whole page's old history.
    script = r"""() => {
        const visible = el => {const r=el.getBoundingClientRect(), s=getComputedStyle(el);
            return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';};
        const text = el => (el.innerText || '') + '\n' +
            [...el.querySelectorAll('textarea')].filter(visible).map(x=>x.value||'').join('\n');
        const titles=[...document.querySelectorAll('div,span,label,h1,h2,h3')]
            .filter(el=>visible(el) && (el.innerText||'').trim()==='业务单据导入进度');
        if (!titles.length) return '';
        for(const title of titles) {
            let el=title;
            for(let i=0;el && el!==document.body && i<10;i++,el=el.parentElement) {
                const value=text(el);
                if (value.length<12000 && /开始导入|本次导入|开始检查格式/.test(value)) return value;
            }
        }
        return '业务单据导入进度';
    }"""
    texts = []
    for frame in getattr(page, 'frames', None) or [page]:
        value = frame.evaluate(script)
        if value:
            texts.append(str(value))
    if len(texts) > 1:
        raise RuntimeError('存在多个导入进度窗口，无法确认本次结果')
    return texts[0] if texts else ''


def receipt_complete(text, expected_rows):
    """Require the exact observed numbered receipt sequence and matching row count."""
    if not text or PROGRESS_TITLE not in text or any(word in text for word in FAILURE_WORDS):
        return False
    count = re.search(r'本次导入\s*(\d+)\s*行数据\s*[,，]\s*开始写入数据', text)
    if not count or int(count.group(1)) != expected_rows:
        return False
    return bool(re.search(r'开始导入', text) and re.search(r'开始检查格式', text)
                and re.search(r'(?:^|\n)\s*\d+\s*[,，、.]\s*[-—－]+\s*完成\s*[-—－]+\s*(?:\n|$)', text))


def _wait_for_receipt(page, expected_rows, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = _progress_text(page)
        body = page.locator('body').inner_text(timeout=2000)
        if any(word in body for word in PAGE_FAILURE_WORDS):
            raise RuntimeError('ERP 存在未导入提示，可能部分导入，必须核对单据')
        if text and any(word in text for word in FAILURE_WORDS):
            raise RuntimeError('ERP 回执包含失败信息，可能部分导入，必须核对单据')
        if receipt_complete(text, expected_rows):
            return text
        page.wait_for_timeout(500)
    raise RuntimeError('未取得本批条数一致的完成回执，必须核对 ERP 单据')


def _confirm_import(page, erp):
    # Preserve the established confirmation compatibility only while its prompt exists.
    # Never click the import button again, even when the first click timed out.
    page.wait_for_timeout(1000)
    if '是否继续' not in page.locator('body').inner_text(timeout=2000):
        return
    try:
        erp.js_click_dialog_button(page, '是否继续', '确定')
        return
    except Exception as exc:
        # This exact source error occurs before mouse.click. A click/wait timeout
        # is ambiguous and must never fall back to a second confirmation click.
        if not isinstance(exc, erp.AutomationError) or not str(exc).startswith('找不到弹窗按钮:'):
            raise
    if '是否继续' not in page.locator('body').inner_text(timeout=2000):
        return
    try:
        erp.js_click_text(page, '确定', exact=True)
        return
    except Exception as exc:
        if not isinstance(exc, erp.AutomationError) or not str(exc).startswith('找不到可点击文本:'):
            raise
    if '是否继续' in page.locator('body').inner_text(timeout=2000):
        page.mouse.click(648, 493)


def _write_json(path, payload, *, exclusive=False):
    if exclusive:
        with path.open('x', encoding='utf-8') as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        return
    temp = path.with_name(path.name + '.tmp')
    if temp.resolve().parent != path.resolve().parent:
        raise ValueError('临时文件不得通过链接离开批次目录')
    with temp.open('w', encoding='utf-8') as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


class ErpAdjustmentSender:
    def __init__(self, runtime, *, receipt_timeout_seconds=60):
        self.runtime = guarded_runtime(runtime)
        self.output_root = self._path(self.runtime / 'adjustments')
        self.output_root.mkdir(exist_ok=True)
        self.receipt_timeout_seconds = receipt_timeout_seconds

    def _path(self, path):
        if not path.resolve().is_relative_to(self.runtime):
            raise ValueError('ERP 适配器写路径不得离开本服务 runtime')
        return path

    def send(self, batch):
        rows = _items(batch)
        try:
            batch_id = str(uuid.UUID(str(batch.get('id', ''))))
        except ValueError:
            raise AdjustmentFailure('批次编号必须为有效 UUID，尚未提交', retryable=True)
        document_no = _document_no(batch, batch_id)
        directory = self._path(self.output_root / batch_id)
        directory.mkdir(exist_ok=True)
        marker = self._path(directory / 'submission.json')
        if marker.exists():
            raise AdjustmentUnknown(_saved_document_no(marker, document_no))
        preparation = self._path(directory / 'prepared.json')
        if preparation.exists():
            document_no = _saved_document_no(preparation, document_no)
        info = ADJUSTMENT_TYPES[batch['type']]
        output_path = self._path(directory / f"{info['prefix']}_{document_no}.xls")
        payload = {'batch_id': batch_id, 'type': batch['type'], 'items': rows}
        content_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if preparation.exists():
            if json.loads(preparation.read_text(encoding='utf-8')).get('content_sha256') != content_hash:
                raise AdjustmentFailure('同一批次内容发生变化，拒绝上传', retryable=False)
        else:
            _write_json(preparation, {'document_no': document_no, 'content_sha256': content_hash,
                                     'row_count': len(rows), **payload}, exclusive=True)
        attempted = False
        pw = context = page = None
        close_context = False
        stage = '生成导入文件'
        try:
            write_adjustment_import_file(TEMPLATE_PATH, output_path, batch['type'], rows, document_no)
            stage = '加载独立浏览器'
            erp, purchase = _load_automation_modules()
            config = erp.load_config()
            private_base = self._path(self.output_root / 'session')
            private_profile = self._path(private_base / 'chrome-profile')
            private_profile.mkdir(parents=True, exist_ok=True)
            config = replace(config, base_dir=private_base, browser_profile_dir=private_profile,
                             keep_browser_open=False)
            self._path(config.logs_dir).mkdir(parents=True, exist_ok=True)
            pw, context, page, close_context = launch_private_browser(config)
            stage = '登录 ERP'
            erp.ensure_logged_in(page, config)
            stage = '选择导入单据'
            purchase.open_erp_business_import(page, AdjustmentImportConfig())
            file_input = page.locator('input[type=file]').last
            file_input.wait_for(state='visible', timeout=12000)
            select_adjustment_document_type(page, info['document_type'], info['arrow_down'])
            file_input.set_input_files(str(output_path))
            if _progress_text(page):
                raise RuntimeError('提交前存在旧导入进度窗口')
            # Persist BEFORE the first attempt. A torn marker is also a no-resubmit marker.
            _write_json(marker, {'document_no': document_no, 'batch_id': batch_id,
                                 'content_sha256': content_hash, 'row_count': len(rows),
                                 'attempted_at': datetime.now().isoformat()}, exclusive=True)
            attempted = True
            page.get_by_text('导入文件', exact=True).last.click(timeout=5000)
            _confirm_import(page, erp)
            receipt = _wait_for_receipt(page, len(rows), self.receipt_timeout_seconds)
            result = {'document_no': document_no, 'message': f"ERP {info['document_type']}导入完成，共 {len(rows)} 行",
                      'row_count': len(rows), 'receipt': receipt, 'confirmed_at': datetime.now().isoformat()}
            _write_json(self._path(directory / 'result.json'), result)
            return {'document_no': document_no, 'message': result['message']}
        except Exception as exc:
            if attempted or marker.exists():
                raise AdjustmentUnknown(document_no) from exc
            if isinstance(exc, AdjustmentFailure):
                raise
            raise AdjustmentFailure(f'{stage}失败，尚未尝试提交，可重试（{type(exc).__name__}）', retryable=True) from exc
        finally:
            if page is not None and (attempted or marker.exists()):
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(self._path(directory / 'result.png')), full_page=True)
            if context is not None and close_context:
                with contextlib.suppress(Exception):
                    context.close()
            if pw is not None:
                with contextlib.suppress(Exception):
                    pw.stop()


def select_adjustment_document_type(page, document_type, arrow_down_count):
    native_selects = page.locator("select")
    for index in range(native_selects.count()):
        select = native_selects.nth(index)
        try:
            if not select.is_visible():
                continue
            select.select_option(label=document_type)
            return
        except Exception:
            continue

    import_box = None
    with contextlib.suppress(Exception):
        import_box = page.evaluate(
            """() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const fileInputs = [...document.querySelectorAll('input[type=file]')]
                    .filter(visible)
                    .map(el => el.getBoundingClientRect())
                    .sort((a, b) => (b.width * b.height) - (a.width * a.height));
                if (!fileInputs.length) return null;
                const fileRect = fileInputs[0];
                const fileCenterY = fileRect.y + fileRect.height / 2;
                const inputs = [...document.querySelectorAll('input[type=text], [role=combobox], .combo-text')]
                    .filter(visible)
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        const centerY = r.y + r.height / 2;
                        return {
                            x: r.x,
                            y: r.y,
                            width: r.width,
                            height: r.height,
                            score: Math.abs(centerY - fileCenterY) * 20 + Math.abs(fileRect.x - (r.x + r.width))
                        };
                    })
                    .filter(r => r.width >= 50 && Math.abs((r.y + r.height / 2) - fileCenterY) <= 45 && r.x < fileRect.x)
                    .sort((a, b) => a.score - b.score);
                return inputs[0] || null;
            }"""
        )
    if not import_box:
        raise RuntimeError(f"无法找到业务单据类型下拉框：{document_type}")

    page.mouse.click(import_box["x"] + import_box["width"] / 2, import_box["y"] + import_box["height"] / 2)
    page.wait_for_timeout(250)
    # ERP 自定义下拉框沿用稳定的键盘规则：报损 5 次，报溢 6 次。
    for _ in range(arrow_down_count):
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(80)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
