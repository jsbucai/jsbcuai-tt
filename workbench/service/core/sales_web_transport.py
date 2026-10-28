"""Durable, single-attempt transport for ERP sales-order web imports.

The caller supplies a reviewed XLS. Completion means a uniquely scoped web
import window reported the complete numbered import sequence and exactly the
expected row count. It does not mean ERP fields were read back or audited.

An attempt marker is fsynced before the upload click. Once the marker exists,
every invocation reads only a durable local completion receipt, even if the
first click timed out or the process was killed. Unknown attempts require
manual review. Never delete the marker to retry an uncertain attempt.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .store import guarded_runtime


ORDER_NUMBER = re.compile(r"[A-Za-z0-9_-]{1,20}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
LIST_REQUIRED = frozenset({"number", "date", "audit_status", "shipping_status", "customer",
                           "warehouse", "salesperson", "delivery_date",
                           "untaxed_total", "taxed_total"})
DETAIL_REQUIRED = frozenset({"number", "maker", "sku", "quantity",
                             "untaxed_unit_price", "untaxed_line_amount",
                             "tax_rate_percent", "tax_amount",
                             "taxed_unit_price", "taxed_line_amount", "summary", "note"})
DETAIL_HEADER_REQUIRED = frozenset({"number", "maker", "summary", "note"})
LINE_REQUIRED = frozenset({"line_id", "sku", "product_name", "delivery_date",
                           "detail_note", "quantity", "untaxed_unit_price",
                           "untaxed_line_amount", "tax_rate_percent", "tax_amount",
                           "taxed_unit_price", "taxed_line_amount", "sku_id",
                           "warehouse_id", "base_unit_id"})
LINE_DOCUMENT_REQUIRED = LINE_REQUIRED | {'order_number', 'summary'}
NUMERIC_FIELDS = frozenset({"quantity", "untaxed_total", "taxed_total",
                            "untaxed_unit_price", "untaxed_line_amount",
                            "tax_rate_percent", "tax_amount",
                            "taxed_unit_price", "taxed_line_amount"})
MAX_XLS_BYTES = 10 * 1024 * 1024
WEB_HEADER_TEXT_LIMIT = 180
WEB_IMPORT_HEADERS = (
    "录单日期", "单据编号", "单据类型", "往来单位编号", "往来单位名称", "部门名称",
    "摘要", "附加说明", "制单人名称", "经手人名称", "仓库名称", "收付款账户编号",
    "收付款账户金额", "交货日期", "商品条码", "商品编号", "商品名称", "明细仓库名称",
    "单位", "数量", "折前单价", "折前金额", "折扣", "税率(%)", "明细备注",
    "副单位数量", "是否赠品",
)


class WebImportBlocked(RuntimeError):
    """Preparation or preflight failed before any upload attempt."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_sales_header_limits(note: str, summary: str) -> None:
    """Use the observed UI limits until a longer import contract is proved."""
    for name, value in (("note", note), ("summary", summary)):
        if not isinstance(value, str):
            raise WebImportBlocked("readback_expectation_invalid")
        # HTML maxlength counts UTF-16 code units, including surrogate pairs.
        try:
            length = len(value.encode('utf-16-le')) // 2
        except UnicodeEncodeError:
            raise WebImportBlocked("readback_expectation_invalid") from None
        if length > WEB_HEADER_TEXT_LIMIT:
            raise WebImportBlocked("sales_" + name + "_exceeds_web_limit")


@dataclass(frozen=True)
class WebImportSpec:
    intent_id: str
    order_number: str
    profile_id: str
    account_id: str
    source_path: Path
    source_sha256: str
    list_expected: Mapping[str, str]
    detail_expected: Mapping[str, str]
    department_name: str
    product_name: str
    note: str
    detail_note: str
    # Omit for the original one-row interface. When present, each reviewed
    # line has its own frozen ordinal, SKU, note, date and price expectation.
    lines_expected: tuple[Mapping[str, str], ...] = ()
    # A reviewed base-unit-1 identity carries its single warehouse identity.
    # XLS units are checked locally; no ERP unit readback is claimed.
    base_unit_id: str = ""
    warehouse_id: str = ""
    schema: str = "sales_web_import_v1"
    document_mode: str = "single_document"
    document_numbers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SalesListResult:
    # complete means the ERP filter has completed, all matching pages were
    # inspected, and the total/result count agrees. A stale or partial view
    # must return False rather than an empty row list.
    complete: bool
    rows: tuple[Mapping[str, str], ...]


class SalesWebBrowser(Protocol):
    def account_identity(self) -> tuple[str, str]: ...
    def stage_sales_import(self, xls_path: Path) -> None: ...
    def click_upload_once(self) -> None: ...
    def visible_import_progress(self) -> str: ...


class SalesWebReadback(Protocol):
    """Site-specific, read-only list/detail reader for the current ERP UI."""

    def find_by_full_number(self, order_number: str) -> SalesListResult: ...
    def open_detail_readonly(self, row: Mapping[str, str]) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class PlaywrightUploadSelectors:
    """Wisej controls observed on the N150 ERP page on 2026-10-07."""

    authenticated_selector: str = ".MenuRootCaption"
    blocked_selector: str = ""
    document_type_selector: str = 'input[id$="$edVchFilter"]'
    file_selector: str = 'input[id$="$loadfile"]'
    upload_selector: str = 'button:has-text("导入文件")'
    confirm_text: str = "注意:系统将为您导入单据，是否继续?"
    progress_timeout_ms: int = 30000


class PlaywrightSalesWebBrowser:
    """Linux-compatible Wisej upload control; automatic imports do no readback.

    The caller owns the isolated browser context and login. This class never
    supplies credentials or retries an upload or confirmation click.
    """

    def __init__(self, page, selectors: PlaywrightUploadSelectors, readback: SalesWebReadback | None = None,
                 identity_reader: Callable[[], tuple[str, str]] | None = None, *,
                 identity: Callable[[], tuple[str, str]] | None = None):
        if any(not getattr(selectors, name) for name in (
                "authenticated_selector", "document_type_selector",
                "file_selector", "upload_selector", "confirm_text")):
            raise ValueError("ERP upload selectors incomplete")
        if not 0 <= selectors.progress_timeout_ms <= 60000:
            raise ValueError("ERP progress timeout invalid")
        if identity_reader is not None and identity is not None or not callable(identity_reader or identity):
            raise ValueError("ERP identity reader invalid")
        self.page, self.selectors, self.readback = page, selectors, readback
        self.identity_reader = identity_reader or identity

    def _one(self, selector: str, *, visible=True):
        locator = self.page.locator(selector)
        if locator.count() != 1 or visible and not locator.is_visible():
            raise RuntimeError("ERP control missing or ambiguous")
        return locator

    def _session(self):
        if self.selectors.blocked_selector and self.page.locator(self.selectors.blocked_selector).count():
            blocked = self.page.locator(self.selectors.blocked_selector)
            if any(blocked.nth(i).is_visible() for i in range(blocked.count())):
                raise RuntimeError("ERP login, CAPTCHA or blocking dialog requires attention")
        authenticated = self.page.locator(self.selectors.authenticated_selector)
        if not any(item.is_visible() for item in authenticated.all()):
            raise RuntimeError("ERP authenticated navigation unavailable")

    @staticmethod
    def _on_screen(item):
        return item.evaluate("e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0"
                             "&&r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}")

    def _visible_text(self, text: str):
        matches = [item for item in self.page.get_by_text(text, exact=True).all()
                   if item.is_visible() and self._on_screen(item)]
        if len(matches) != 1:
            raise RuntimeError("ERP text control missing or ambiguous")
        return matches[0]

    def _menu(self, selector: str, text: str):
        matches = [item for item in self.page.locator(selector).all()
                   if item.is_visible() and item.inner_text().strip() == text
                   and self._on_screen(item)]
        if len(matches) != 1:
            raise RuntimeError("ERP menu missing or ambiguous")
        matches[0].click()

    def _close_import_dialogs(self) -> None:
        # The import progress window overlays the import form, which overlays
        # sales management. Close only a uniquely identified read-only window.
        titles = [item for item in self.page.get_by_text("业务单据导入进度", exact=True).all()
                  if item.is_visible() and self._on_screen(item)]
        if len(titles) > 1:
            raise RuntimeError("ERP import progress windows ambiguous")
        if titles:
            if not self._terminal_progress(self._progress_snapshot()):
                raise RuntimeError("ERP import still processing; retain progress window")
            parent = titles[0]
            for _ in range(12):
                parent = parent.locator("..")
                buttons = [item for item in parent.get_by_text("关闭", exact=True).all()
                           if item.is_visible() and self._on_screen(item)]
                if len(buttons) == 1:
                    buttons[0].click()
                    break
            else:
                raise RuntimeError("ERP progress close button missing")
        file_inputs = self.page.locator(self.selectors.file_selector)
        if any(item.is_visible() and self._on_screen(item) for item in file_inputs.all()):
            closes = [item for item in self.page.get_by_text("关闭", exact=True).all()
                      if item.is_visible() and self._on_screen(item)]
            if len(closes) != 1:
                raise RuntimeError("ERP import close button ambiguous")
            closes[0].click()

    def account_identity(self) -> tuple[str, str]:
        self._session()
        identity = self.identity_reader()
        if (not isinstance(identity, tuple) or len(identity) != 2 or
                any(not isinstance(part, str) or not part for part in identity)):
            raise RuntimeError("ERP account identity unavailable")
        return identity

    def find_by_full_number(self, order_number: str) -> SalesListResult:
        self._session()
        self._close_import_dialogs()
        result = self.readback.find_by_full_number(order_number)
        self._session()
        return result

    def open_detail_readonly(self, row: Mapping[str, str]) -> Mapping[str, object]:
        self._session()
        result = self.readback.open_detail_readonly(row)
        self._session()
        return result

    def stage_sales_import(self, xls_path: Path) -> None:
        if not xls_path.is_file() or xls_path.suffix.lower() != ".xls":
            raise RuntimeError("ERP import workbook unavailable")
        self._session()
        self._menu(".MenuRootCaption", "系统")
        self._menu(".MenuCaption", "导入导出")
        self._menu(".MenuCaption", "业务单据导入")
        doc = self._one(self.selectors.document_type_selector)
        if doc.input_value().strip() != "销售订单":
            doc.click()
            for _ in range(20):
                doc.press("ArrowDown")
                if doc.input_value().strip() == "销售订单":
                    doc.press("Enter")
                    break
        if doc.input_value().strip() != "销售订单":
            raise RuntimeError("ERP import document type not confirmed")
        self._one(self.selectors.file_selector, visible=False).set_input_files(str(xls_path))
        filename = self._one(self.selectors.file_selector, visible=False).evaluate(
            "el => el.files?.[0]?.name || ''")
        if filename != xls_path.name:
            raise RuntimeError("ERP import workbook selection not confirmed")
        self._one(self.selectors.upload_selector)
        if self._progress_snapshot():
            raise RuntimeError("ERP stale import progress dialog")
        self._session()

    def click_upload_once(self) -> None:
        self._session()
        self._one(self.selectors.upload_selector).click(timeout=5000)
        self.page.get_by_text(self.selectors.confirm_text, exact=True).wait_for(
            state="visible", timeout=5000)
        self._visible_text("确定").click(timeout=5000)

    def _progress_snapshot(self) -> str:
        script = r"""() => {
          const visible = el => {const r=el.getBoundingClientRect(),s=getComputedStyle(el);
            return r.width>0&&r.height>0&&r.left>=0&&r.top>=0&&r.right<=innerWidth
              &&r.bottom<=innerHeight&&s.display!=='none'&&s.visibility!=='hidden';};
          const captions=[...document.querySelectorAll('div,span,label,h1,h2,h3')]
            .filter(el=>visible(el)&&(el.innerText||'').trim()==='业务单据导入进度');
          // A Wisej caption may have nested wrappers carrying identical text;
          // count the leaf caption once, while distinct windows stay ambiguous.
          const titles=captions.filter(el=>!captions.some(other=>other!==el&&el.contains(other)));
          if(titles.length>1)return '__AMBIGUOUS__';
          const failure=[...document.querySelectorAll('div,span,label,h1,h2,h3')]
            .filter(el=>visible(el)&&/^(?:未导入数据提示Excel|导入失败|上传失败|格式错误)/.test((el.innerText||'').trim()))
            .map(el=>(el.innerText||'').trim()).find(value=>value.length<1000);
          if(!titles.length)return failure?'__IMPORT_FAILURE__\n'+failure:'';
          let el=titles[0];
          for(let i=0;el&&el!==document.body&&i<10;i++,el=el.parentElement){
            const value=(el.innerText||'')+'\n'+[...el.querySelectorAll('textarea')]
              .filter(visible).map(x=>x.value||'').join('\n');
            if(value.length<12000&&/开始导入|本次导入|开始检查格式/.test(value))
              return (failure?'__IMPORT_FAILURE__\n'+failure+'\n':'')+value;
          }
          return '业务单据导入进度';
        }"""
        texts = []
        for frame in getattr(self.page, "frames", None) or [self.page]:
            value = frame.evaluate(script)
            if value:
                texts.append(_text(value))
        if len(texts) > 1 or "__AMBIGUOUS__" in texts:
            raise RuntimeError("ERP import progress dialog ambiguous")
        return texts[0] if texts else ""

    @staticmethod
    def _terminal_progress(progress: str) -> bool:
        # Ignore any import instructions that may precede the actual progress
        # window in a broad Wisej text container.
        log = progress.rsplit("业务单据导入进度", 1)[-1]
        if any(word in log for word in (
                "未导入", "导入失败", "上传失败", "格式错误", "失败原因", "异常", "错误")):
            return True
        return bool(re.search(r"(?:^|\n)\s*\d+\s*[,，、.]\s*[-—－]+\s*完成\s*[-—－]+", log))

    def visible_import_progress(self) -> str:
        deadline = self.selectors.progress_timeout_ms
        elapsed = 0
        while True:
            self._session()
            progress = self._progress_snapshot()
            if self._terminal_progress(progress) or elapsed >= deadline:
                return progress
            interval = min(250, deadline - elapsed)
            self.page.wait_for_timeout(interval)
            elapsed += interval


@dataclass(frozen=True)
class WebImportOutcome:
    intent_id: str
    order_number: str
    status: str
    code: str
    attempted: bool
    progress_observed: bool
    unit_verified: bool
    unit_basis: str = ""
    completion_basis: str = ""
    erp_readback_performed: bool = False
    table_verified: bool = False
    file_sha256: str = ""
    imported_rows: int = 0
    expected_rows: int = 0
    document_numbers: tuple[str, ...] = ()
    document_count: int = 0


def _web_import_confirmation(progress: str, expected_rows: int) -> tuple[str, int]:
    """Accept only the observed Wisej terminal receipt, never a button click.

    The row count is the batch count reported before writing; completion must
    follow it with no failures or partial-result hints. No ERP readback follows.
    """
    if not isinstance(progress, str) or not progress or len(progress) > 12000:
        return "web_import_progress_unconfirmed", 0
    title = "业务单据导入进度"
    if progress.count(title) != 1:
        return "web_import_progress_ambiguous", 0
    log = progress.split(title, 1)[1]
    if "__IMPORT_FAILURE__" in progress or any(word in log for word in (
            "未导入", "失败", "异常", "错误", "部分", "跳过", "忽略", "取消", "警告")):
        return "web_import_failed_or_partial", 0
    starts = list(re.finditer(r"(?:^|\n)\s*(\d+)\s*[,，、.]\s*开始导入", log))
    checks = list(re.finditer(r"(?:^|\n)\s*(\d+)\s*[,，、.]\s*开始检查格式", log))
    counts = list(re.finditer(r"(?:^|\n)\s*(\d+)\s*[,，、.]\s*本次导入\s*(\d+)\s*行数据\s*[,，]\s*开始写入数据", log))
    completions = list(re.finditer(r"(?:^|\n)\s*(\d+)\s*[,，、.]\s*[-—－]+\s*完成\s*[-—－]+\s*(?:\n|$)", log))
    if any(len(items) > 1 for items in (starts, checks, counts, completions)):
        return "web_import_progress_ambiguous", 0
    if not all(len(items) == 1 for items in (starts, checks, counts, completions)):
        return "web_import_progress_unconfirmed", 0
    sequence = [items[0] for items in (starts, checks, counts, completions)]
    numbers = [int(item.group(1)) for item in sequence]
    if (numbers != sorted(set(numbers)) or
            [item.start() for item in sequence] != sorted(item.start() for item in sequence)):
        return "web_import_progress_unconfirmed", 0
    imported = int(counts[0].group(2))
    if imported != expected_rows or imported <= 0:
        return "web_import_row_count_mismatch", imported
    trailing = log[completions[0].end():].strip()
    if trailing not in ("", "关闭"):
        return "web_import_progress_unconfirmed", 0
    return "web_import_confirmed", imported


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
    finally:
        # fdopen owns the descriptor after successful construction.
        pass
    _fsync_directory(path.parent)


def _replace_json(path: Path, value: Mapping) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    try:
        with temporary.open("xb") as target:
            os.chmod(temporary, 0o600)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _text(value: object) -> str:
    return str(value).replace("\r\n", "\n").strip() if value is not None else ""


def _note_parts(value: object) -> tuple[str, ...]:
    parts = tuple(part.strip() for part in str(value).split("#"))
    if len(parts) != 10 or not all(parts):
        raise ValueError("line note format")
    return parts


def _document_note_parts(value: str, delivery_date: str) -> tuple[str, ...]:
    suffix = '#交期=' + delivery_date
    if not value.endswith(suffix):
        raise ValueError('line summary delivery')
    return _note_parts(value.removesuffix(suffix))


def _outcome_record(outcome: WebImportOutcome, *, legacy: bool) -> dict:
    result = asdict(outcome)
    result['document_numbers'] = list(outcome.document_numbers)
    if legacy:
        # Preserve exact historical receipt shape and recovery semantics.
        result.pop('document_numbers')
        result.pop('document_count')
    return result


def _line_amounts_valid(line: Mapping[str, str]) -> bool:
    try:
        quantity = Decimal(line["quantity"])
        unit_price = Decimal(line["untaxed_unit_price"])
        net = Decimal(line["untaxed_line_amount"])
        tax = Decimal(line["tax_amount"])
        gross = Decimal(line["taxed_line_amount"])
        taxed_unit = Decimal(line["taxed_unit_price"])
        rate = Decimal(line["tax_rate_percent"])
        return (all(value.is_finite() for value in (quantity, unit_price, net, tax, gross, taxed_unit, rate))
                and quantity > 0 and unit_price >= 0 and net >= 0 and tax >= 0
                and rate == 13 and
                (quantity * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == net
                and (net * Decimal("0.13")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == tax
                and gross == net + tax and taxed_unit >= unit_price)
    except (KeyError, InvalidOperation, ValueError):
        return False


def _validate_sales_workbook(content: bytes, manifest: Mapping) -> None:
    """Match every XLS business cell to the persisted, reviewed import intent."""
    try:
        import xlrd
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
        try:
            if workbook.sheet_names() != ["单据导入模板", "单据类型"]:
                raise ValueError("sheets")
            sheet = workbook.sheet_by_index(0)
            lines = manifest.get("lines_expected") or [manifest["detail_expected"]]
            if (sheet.nrows != len(lines) + 1 or sheet.ncols != len(WEB_IMPORT_HEADERS) or
                    tuple(sheet.row_values(0)) != WEB_IMPORT_HEADERS):
                raise ValueError("sales row")
            listing, detail = manifest["list_expected"], manifest["detail_expected"]
            line_documents = manifest.get('schema') == 'sales_web_import_v2'
            for row_index, line in enumerate(lines, start=1):
                expected = {
                    "录单日期": listing["date"], "单据编号": line['order_number'] if line_documents else manifest["order_number"],
                    "单据类型": "销售订单", "往来单位名称": listing["customer"],
                    "部门名称": manifest["department_name"], "摘要": line['summary'] if line_documents else detail["summary"],
                    "附加说明": manifest["note"], "制单人名称": detail["maker"],
                    "经手人名称": listing["salesperson"], "仓库名称": listing["warehouse"],
                    "交货日期": line.get("delivery_date", listing["delivery_date"]),
                    "商品编号": line["sku"],
                    "商品名称": line.get("product_name", manifest["product_name"]),
                    "明细仓库名称": listing["warehouse"], "单位": "支",
                    "数量": line["quantity"], "折前单价": line["untaxed_unit_price"],
                    "折前金额": line["untaxed_line_amount"],
                    "税率(%)": line["tax_rate_percent"],
                    "明细备注": line.get("detail_note", manifest["detail_note"]),
                }
                for column, header in enumerate(WEB_IMPORT_HEADERS):
                    value = sheet.cell_value(row_index, column)
                    wanted = expected.get(header, "")
                    if header in ("录单日期", "交货日期"):
                        if (sheet.cell_type(row_index, column) != xlrd.XL_CELL_DATE or
                                xlrd.xldate_as_datetime(value, workbook.datemode).date().isoformat() != wanted):
                            raise ValueError(header)
                    elif header in ("数量", "折前单价", "折前金额", "税率(%)"):
                        actual_number, expected_number = Decimal(str(value)), Decimal(str(wanted))
                        if not actual_number.is_finite() or actual_number != expected_number:
                            raise ValueError(header)
                    elif _text(value) != wanted:
                        raise ValueError(header)
        finally:
            workbook.release_resources()
    except ImportError:
        raise WebImportBlocked("xls_reader_unavailable") from None
    except Exception:
        raise WebImportBlocked("import_workbook_invalid") from None


class SalesWebImportTransport:
    def __init__(self, runtime: Path):
        self.runtime = guarded_runtime(runtime)
        self.root = self.runtime / "sales-web-import"
        if not self.root.resolve().is_relative_to(self.runtime):
            raise ValueError("sales web import directory escapes runtime")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.account_locks = self.root / ".account-locks"
        if not self.account_locks.resolve().is_relative_to(self.root):
            raise ValueError("sales web import lock directory escapes runtime")
        self.account_locks.mkdir(mode=0o700, exist_ok=True)
        self.order_claims = self.root / ".order-claims"
        if not self.order_claims.resolve().is_relative_to(self.root):
            raise ValueError("sales web import claim directory escapes runtime")
        self.order_claims.mkdir(mode=0o700, exist_ok=True)

    def _directory(self, intent_id: str) -> Path:
        try:
            ident = str(uuid.UUID(str(intent_id)))
        except (TypeError, ValueError, AttributeError):
            raise WebImportBlocked("intent_id_invalid") from None
        if ident != intent_id:
            raise WebImportBlocked("intent_id_invalid")
        path = self.root / ident
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise WebImportBlocked("intent_path_invalid")
        return path

    @contextmanager
    def _lock(self, directory: Path):
        directory.mkdir(mode=0o700, exist_ok=True)
        lock = directory / ".lock"
        with lock.open("a+b") as handle:
            os.chmod(lock, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def _account_lock(self, manifest: Mapping):
        # Serialize all web imports for one ERP account, including separate
        # intents with the same order number. A second stage can only run
        # after the first attempt and local receipt handling have finished.
        identity = json.dumps([manifest["profile_id"], manifest["account_id"]],
                              ensure_ascii=False, separators=(",", ":")).encode()
        lock = self.account_locks / (_sha256(identity) + ".lock")
        with lock.open("a+b") as handle:
            os.chmod(lock, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _claim_number(self, manifest: Mapping) -> None:
        numbers = manifest.get('document_numbers') or [manifest['order_number']]
        missing = []
        # Caller holds the account lock. Check the entire batch before writing
        # any new claim; all claims are durable before any upload is staged.
        for number in numbers:
            binding = [manifest["profile_id"], manifest["account_id"], number]
            key = _sha256(json.dumps(binding, ensure_ascii=False, separators=(",", ":")).encode())
            path = self.order_claims / (key + ".json")
            claim = {"binding": binding, "intent_id": manifest["intent_id"]}
            if path.exists():
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise WebImportBlocked("order_number_claim_corrupt") from None
                if saved != claim:
                    raise WebImportBlocked("order_number_claimed")
            else:
                missing.append((path, claim))
        for path, claim in missing:
            _write_exclusive(path, json.dumps(claim, ensure_ascii=False, sort_keys=True).encode())

    @staticmethod
    def _validate_spec(spec: WebImportSpec) -> dict:
        if not isinstance(spec, WebImportSpec) or not ORDER_NUMBER.fullmatch(spec.order_number):
            raise WebImportBlocked("order_number_invalid")
        line_documents = spec.schema == 'sales_web_import_v2'
        if (spec.schema not in {'sales_web_import_v1', 'sales_web_import_v2'} or
                (line_documents and (spec.document_mode != 'line_document' or
                 not isinstance(spec.document_numbers, tuple) or not spec.document_numbers or
                 len(spec.document_numbers) != len(spec.lines_expected) or
                 any(not isinstance(value, str) or not ORDER_NUMBER.fullmatch(value) for value in spec.document_numbers) or
                 len(set(spec.document_numbers)) != len(spec.document_numbers) or
                 spec.document_numbers[0] != spec.order_number)) or
                (not line_documents and (spec.document_mode != 'single_document' or spec.document_numbers))):
            raise WebImportBlocked('document_mode_invalid')
        if not isinstance(spec.source_sha256, str) or not HASH.fullmatch(spec.source_sha256):
            raise WebImportBlocked("file_hash_invalid")
        if any(not isinstance(part, str) or not part or len(part) > 128
               for part in (spec.profile_id, spec.account_id)):
            raise WebImportBlocked("account_identity_invalid")
        if (spec.base_unit_id not in ("", "1") or
                not isinstance(spec.warehouse_id, str) or
                (spec.base_unit_id == "1" and not _text(spec.warehouse_id)) or
                any(char in spec.warehouse_id for char in "\r\n")):
            raise WebImportBlocked("base_unit_identity_invalid")
        if (any(not isinstance(part, str) or not _text(part)
                for part in (spec.department_name, spec.product_name, spec.detail_note)) or
                not isinstance(spec.note, str) or any("\n" in part or "\r" in part
                for part in (spec.department_name, spec.product_name, spec.note, spec.detail_note))):
            raise WebImportBlocked("workbook_expectation_invalid")
        if not isinstance(spec.list_expected, Mapping) or not isinstance(spec.detail_expected, Mapping):
            raise WebImportBlocked("readback_expectation_invalid")
        lists, details = dict(spec.list_expected), dict(spec.detail_expected)
        # Legacy callers did not repeat the note inside detail_expected. The
        # immutable top-level note supplies the XLS expectation. No note is
        # injected through an ERP editor after the upload.
        if "note" not in details:
            details["note"] = spec.note
        elif details["note"] != spec.note:
            raise WebImportBlocked("readback_note_binding_invalid")
        validate_sales_header_limits(spec.note, details.get("summary"))
        strict_lines = bool(spec.lines_expected)
        if (not LIST_REQUIRED.issubset(lists) or
                not (DETAIL_HEADER_REQUIRED if strict_lines else DETAIL_REQUIRED).issubset(details)):
            raise WebImportBlocked("readback_expectation_incomplete")
        if lists["number"] != spec.order_number or details["number"] != spec.order_number:
            raise WebImportBlocked("readback_number_mismatch")
        if lists["audit_status"] != "未审核" or lists["shipping_status"] != "未提交":
            raise WebImportBlocked("readback_status_invalid")
        try:
            if date.fromisoformat(lists["delivery_date"]) < date.fromisoformat(lists["date"]):
                raise ValueError("delivery")
        except (TypeError, ValueError):
            raise WebImportBlocked("readback_date_invalid") from None
        if (not strict_lines and not details["summary"].startswith(spec.order_number + "#") or
                not details["summary"].endswith("#交期=" + lists["delivery_date"])):
            raise WebImportBlocked("readback_summary_invalid")
        if any(not isinstance(k, str) or not isinstance(v, str) or (k != "note" and not _text(v))
               for fields in (lists, details) for k, v in fields.items()):
            raise WebImportBlocked("readback_expectation_invalid")
        for fields in (lists, details):
            for key, value in fields.items():
                if key in NUMERIC_FIELDS:
                    try:
                        if not Decimal(value).is_finite():
                            raise ValueError("nonfinite")
                    except (InvalidOperation, ValueError):
                        raise WebImportBlocked("readback_expectation_invalid") from None
        if strict_lines:
            if (not isinstance(spec.lines_expected, (tuple, list)) or
                    not 1 <= len(spec.lines_expected) <= 500 or
                    set(details) != DETAIL_HEADER_REQUIRED):
                raise WebImportBlocked("readback_lines_invalid")
            lines = []
            warehouse_id = ""
            for ordinal, item in enumerate(spec.lines_expected, start=1):
                if not isinstance(item, Mapping):
                    raise WebImportBlocked("readback_lines_invalid")
                line = dict(item)
                if not line_documents:
                    # Additive WebImportLine fields default empty for v1.
                    for key in ('order_number', 'summary'):
                        if line.get(key) == '':
                            line.pop(key)
                if (set(line) != (LINE_DOCUMENT_REQUIRED if line_documents else LINE_REQUIRED) or
                        any(not isinstance(key, str) or not isinstance(value, str) or not _text(value)
                            for key, value in line.items()) or
                        line["line_id"] != str(ordinal) or
                        line["base_unit_id"] != "1" or
                        not _line_amounts_valid(line)):
                    raise WebImportBlocked("readback_lines_invalid")
                if not warehouse_id:
                    warehouse_id = line["warehouse_id"]
                if line["warehouse_id"] != warehouse_id:
                    raise WebImportBlocked("warehouse_identity_mismatch")
                try:
                    if line_documents:
                        validate_sales_header_limits(spec.note, line['summary'])
                        parts = _document_note_parts(line['summary'], line['delivery_date'])
                        if (line['detail_note'] != line['summary'] or
                                line['order_number'] != spec.document_numbers[ordinal - 1]):
                            raise ValueError('line document identity')
                    else:
                        parts = _note_parts(line["detail_note"])
                    line_day = date.fromisoformat(line["delivery_date"])
                    if ((not line_documents and parts[2] != line["line_id"]) or parts[8] != line["quantity"] or
                            parts[9] != line["sku"] or line_day < date.fromisoformat(lists["date"])):
                        raise ValueError("line identity")
                except (TypeError, ValueError):
                    raise WebImportBlocked("readback_lines_invalid") from None
                lines.append(line)
            if (spec.base_unit_id not in ("", "1") or
                    spec.warehouse_id not in ("", warehouse_id)):
                raise WebImportBlocked("base_unit_identity_invalid")
            try:
                if line_documents:
                    notes_match = details['summary'] == lines[0]['summary'] == lines[0]['detail_note'] == spec.detail_note
                else:
                    notes_match = (_note_parts(details["summary"].removesuffix("#交期=" + lists["delivery_date"]))
                                   == _note_parts(lines[0]["detail_note"])
                                   == _note_parts(spec.detail_note))
            except ValueError:
                notes_match = False
            if not notes_match or spec.product_name != lines[0]["product_name"]:
                raise WebImportBlocked("readback_summary_invalid")
            try:
                if (Decimal(lists["untaxed_total"]) != sum(Decimal(item["untaxed_line_amount"]) for item in lines)
                        or Decimal(lists["taxed_total"]) != sum(Decimal(item["taxed_line_amount"]) for item in lines)):
                    raise ValueError("totals")
            except (InvalidOperation, ValueError):
                raise WebImportBlocked("readback_amount_invalid") from None
        else:
            if not _line_amounts_valid(details):
                raise WebImportBlocked("readback_amount_invalid")
            try:
                if (Decimal(lists["untaxed_total"]) != Decimal(details["untaxed_line_amount"])
                        or Decimal(lists["taxed_total"]) != Decimal(details["taxed_line_amount"])):
                    raise ValueError("totals")
            except (InvalidOperation, ValueError):
                raise WebImportBlocked("readback_amount_invalid") from None
        manifest = {"intent_id": spec.intent_id, "order_number": spec.order_number,
                "profile_id": spec.profile_id, "account_id": spec.account_id,
                "file_sha256": spec.source_sha256, "list_expected": lists,
                "detail_expected": details, "department_name": spec.department_name,
                "product_name": spec.product_name, "note": spec.note,
                "detail_note": spec.detail_note}
        if strict_lines:
            manifest["lines_expected"] = lines
            manifest["base_unit_id"] = "1"
            manifest["warehouse_id"] = warehouse_id
        elif spec.base_unit_id:
            manifest["base_unit_id"] = spec.base_unit_id
            manifest["warehouse_id"] = spec.warehouse_id
        if line_documents:
            manifest.update(schema=spec.schema, document_mode=spec.document_mode,
                            document_numbers=list(spec.document_numbers))
        return manifest

    def prepare(self, spec: WebImportSpec) -> Path:
        """Persist a reviewed workbook and immutable import intent."""
        manifest = self._validate_spec(spec)
        directory = self._directory(spec.intent_id)
        source = Path(spec.source_path)
        if source.suffix.lower() != ".xls" or not source.is_file():
            raise WebImportBlocked("import_file_invalid")
        if source.stat().st_size > MAX_XLS_BYTES:
            raise WebImportBlocked("import_file_too_large")
        content = source.read_bytes()
        if not content or len(content) > MAX_XLS_BYTES or _sha256(content) != spec.source_sha256:
            raise WebImportBlocked("import_file_changed")
        _validate_sales_workbook(content, manifest)
        with self._lock(directory):
            with self._account_lock(manifest):
                self._claim_number(manifest)
                manifest_path = directory / "intent.json"
                output = directory / "order.xls"
                if manifest_path.exists():
                    try:
                        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        raise WebImportBlocked("intent_corrupt") from None
                    if isinstance(saved, dict) and isinstance(saved.get('detail_expected'), dict):
                        saved = {**saved, 'detail_expected': {**saved['detail_expected'],
                            'note': saved['detail_expected'].get('note', saved.get('note'))}}
                    if saved != manifest or not output.is_file() or _sha256(output.read_bytes()) != spec.source_sha256:
                        raise WebImportBlocked("intent_conflict")
                    return output
                if output.exists():
                    if not output.is_file() or _sha256(output.read_bytes()) != spec.source_sha256:
                        raise WebImportBlocked("intent_incomplete")
                else:
                    _write_exclusive(output, content)
                _write_exclusive(manifest_path, json.dumps(
                    manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
                return output

    def _load(self, directory: Path) -> tuple[dict, Path]:
        try:
            manifest = json.loads((directory / "intent.json").read_text(encoding="utf-8"))
            output = directory / "order.xls"
            content = output.read_bytes()
            if (manifest["intent_id"] != directory.name or
                    not HASH.fullmatch(manifest["file_sha256"]) or
                    _sha256(content) != manifest["file_sha256"]):
                raise ValueError("changed")
            spec = WebImportSpec(manifest["intent_id"], manifest["order_number"],
                                 manifest["profile_id"], manifest["account_id"], output,
                                 manifest["file_sha256"], manifest["list_expected"],
                                 manifest["detail_expected"], manifest["department_name"],
                                 manifest["product_name"], manifest["note"],
                                 manifest["detail_note"], tuple(manifest.get("lines_expected", ())),
                                 manifest.get("base_unit_id", ""), manifest.get("warehouse_id", ""),
                                 manifest.get('schema', 'sales_web_import_v1'),
                                 manifest.get('document_mode', 'single_document'),
                                 tuple(manifest.get('document_numbers', ())))
            validated = self._validate_spec(spec)
            compatible = {**manifest, 'detail_expected': {**manifest['detail_expected'],
                          'note': manifest['detail_expected'].get('note', manifest.get('note'))}}
            if validated != compatible:
                raise ValueError("changed")
            _validate_sales_workbook(content, validated)
            return validated, output
        except (OSError, ValueError, KeyError, TypeError):
            raise WebImportBlocked("intent_corrupt") from None

    @staticmethod
    def _outcome(manifest: Mapping, status: str, code: str, *, progress_observed=False,
                 imported_rows=0) -> WebImportOutcome:
        return WebImportOutcome(
            manifest["intent_id"], manifest["order_number"], status, code, True,
            progress_observed, False, "",
            "web_import_confirmation" if status == "imported" else "", False,
            True, manifest["file_sha256"], imported_rows,
            len(manifest.get("lines_expected") or [manifest["detail_expected"]]),
            tuple(manifest.get('document_numbers', ())), len(manifest.get('document_numbers', ())))

    def _reconcile(self, directory: Path, manifest: dict, browser=None,
                   progress_observed=False) -> WebImportOutcome:
        """Recover only a bound local receipt; never query ERP or upload again."""
        try:
            saved = json.loads((directory / "completed.json").read_text(encoding="utf-8"))
            expected_rows = len(manifest.get("lines_expected") or [manifest["detail_expected"]])
            progress = saved["progress_text"]
            code, count = _web_import_confirmation(progress, expected_rows)
            outcome = self._outcome(manifest, "imported", "web_import_confirmed",
                                    progress_observed=True, imported_rows=count)
            binding = {"intent_id": manifest["intent_id"], "order_number": manifest["order_number"],
                       "file_sha256": manifest["file_sha256"], "expected_rows": expected_rows,
                       "progress_sha256": _sha256(progress.encode("utf-8"))}
            legacy = manifest.get('schema', 'sales_web_import_v1') == 'sales_web_import_v1'
            if not legacy:
                binding.update(schema=manifest['schema'], document_numbers=manifest['document_numbers'])
            saved_outcome = saved.get('outcome')
            if legacy and isinstance(saved_outcome, dict):
                # New callers may have serialized default additive fields.
                # Only empty defaults are compatible with a legacy intent.
                saved_outcome = dict(saved_outcome)
                if saved_outcome.get('document_numbers') in (None, [], ()) and saved_outcome.get('document_count', 0) == 0:
                    saved_outcome.pop('document_numbers', None)
                    saved_outcome.pop('document_count', None)
            if (code == "web_import_confirmed" and
                    all(saved.get(key) == value for key, value in binding.items()) and
                    saved_outcome == _outcome_record(outcome, legacy=legacy)):
                return outcome
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        return self._outcome(manifest, "unknown", "web_import_requires_manual_review",
                             progress_observed=progress_observed)

    def execute(self, intent_id: str, browser: SalesWebBrowser | None) -> WebImportOutcome:
        """Upload at most once; completion uses only the exact web import receipt.

        XLS cells and account identity are checked before recording an attempt.
        After that marker, ambiguous responses remain unknown and cannot trigger
        another upload. Recovery uses local receipts without any browser calls.
        """
        directory = self._directory(intent_id)
        with self._lock(directory):
            manifest, output = self._load(directory)
            with self._account_lock(manifest):
                return self._execute_locked(directory, manifest, output, browser)

    def _execute_locked(self, directory: Path, manifest: dict, output: Path,
                        browser: SalesWebBrowser | None) -> WebImportOutcome:
        marker = directory / "attempt.json"
        if marker.exists():
            if manifest.get('schema') == 'sales_web_import_v2':
                try:
                    saved_attempt = json.loads(marker.read_text(encoding='utf-8'))
                    expected = {key: manifest[key] for key in
                                ('intent_id', 'order_number', 'schema', 'document_numbers')}
                    expected['file_sha256'] = manifest['file_sha256']
                    if not isinstance(saved_attempt, dict) or any(saved_attempt.get(key) != value for key, value in expected.items()):
                        raise ValueError('attempt binding')
                except (OSError, ValueError, TypeError):
                    return self._outcome(manifest, 'unknown', 'web_import_requires_manual_review')
            return self._reconcile(directory, manifest)
        try:
            if browser.account_identity() != (manifest["profile_id"], manifest["account_id"]):
                raise WebImportBlocked("account_mismatch")
        except WebImportBlocked:
            raise
        except Exception:
            raise WebImportBlocked("preflight_unavailable") from None
        # A durable local account/order-number claim already prevents a second
        # intent from using this number. There is no ERP order search here.
        self._claim_number(manifest)
        content = output.read_bytes()
        if _sha256(content) != manifest["file_sha256"]:
            raise WebImportBlocked("import_file_changed")
        _validate_sales_workbook(content, manifest)
        try:
            browser.stage_sales_import(output)
            if browser.account_identity() != (manifest["profile_id"], manifest["account_id"]):
                raise WebImportBlocked("account_mismatch")
        except WebImportBlocked:
            raise
        except Exception:
            raise WebImportBlocked("upload_stage_unavailable") from None
        attempt = {
            "intent_id": manifest["intent_id"], "order_number": manifest["order_number"],
            "file_sha256": manifest["file_sha256"],
            "recorded_at": datetime.now(timezone.utc).isoformat()
        }
        if manifest.get('schema') == 'sales_web_import_v2':
            attempt.update(schema=manifest['schema'], document_numbers=manifest['document_numbers'])
        _write_exclusive(marker, json.dumps(attempt, ensure_ascii=False, sort_keys=True).encode())
        try:
            browser.click_upload_once()
        except Exception:
            return self._outcome(manifest, "unknown", "upload_attempt_unknown")
        try:
            raw_progress = browser.visible_import_progress()
            progress = raw_progress if isinstance(raw_progress, str) else ""
        except Exception:
            return self._outcome(manifest, "unknown", "web_import_confirmation_unavailable")
        expected_rows = len(manifest.get("lines_expected") or [manifest["detail_expected"]])
        code, count = _web_import_confirmation(progress, expected_rows)
        if progress:
            with suppress(OSError):
                _replace_json(directory / "progress.json", {"text": progress[:12000],
                    "file_sha256": manifest["file_sha256"], "expected_rows": expected_rows,
                    "observed_at": datetime.now(timezone.utc).isoformat()})
        if code != "web_import_confirmed":
            return self._outcome(manifest, "unknown", code, progress_observed=bool(progress),
                                 imported_rows=count)
        result = self._outcome(manifest, "imported", code, progress_observed=True,
                               imported_rows=count)
        receipt = {"intent_id": manifest["intent_id"], "order_number": manifest["order_number"],
                   "file_sha256": manifest["file_sha256"], "expected_rows": expected_rows,
                   "progress_text": progress, "progress_sha256": _sha256(progress.encode("utf-8")),
                   "observed_at": datetime.now(timezone.utc).isoformat(),
                   "outcome": _outcome_record(result, legacy=manifest.get('schema') != 'sales_web_import_v2')}
        if manifest.get('schema') == 'sales_web_import_v2':
            receipt.update(schema=manifest['schema'], document_numbers=manifest['document_numbers'])
        try:
            _replace_json(directory / "completed.json", receipt)
        except OSError:
            return self._outcome(manifest, "unknown", "web_import_confirmation_not_persisted",
                                 progress_observed=True, imported_rows=count)
        return result
