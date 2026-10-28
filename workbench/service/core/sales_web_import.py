"""Prepare legacy XLS web-import rows from a frozen sales-assistant draft.

This module only creates a local file. It has no ERP client, browser, upload,
submission, or retry path. The caller must supply the original blank template
and explicitly reviewed account, staff, warehouse, date, and document number.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Mapping

from .sales_pricing import VAT_PERCENT, quote_price
from .store import dumps
from .unit_policy import CONFIRMED_BASE_UNIT


EXPECTED_HEADERS = (
    '录单日期', '单据编号', '单据类型', '往来单位编号', '往来单位名称',
    '部门名称', '摘要', '附加说明', '制单人名称', '经手人名称', '仓库名称',
    '收付款账户编号', '收付款账户金额', '交货日期', '商品条码', '商品编号',
    '商品名称', '明细仓库名称', '单位', '数量', '折前单价', '折前金额',
    '折扣', '税率(%)', '明细备注', '副单位数量', '是否赠品',
)
EXPECTED_SHEETS = ('单据导入模板', '单据类型')
_CATALOG_DIMENSION = re.compile(r'M\d+(?:\.\d+)?[X*×]\d+(?:\.\d+)?', re.IGNORECASE)


@dataclass(frozen=True)
class WebImportConfig:
    template_path: Path
    destination: Path
    order_number: str
    order_date: date
    profile_id: str
    data_version: str
    owner_id: str
    department_name: str
    maker_name: str
    handler_name: str
    warehouse_id: str
    warehouse_name: str
    # Explicit and frozen: old intents retain their single-document projection.
    document_mode: str = 'single_document'
    document_numbers: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebImportResult:
    path: Path
    sha256: str
    order_number: str
    confirmed_version: int
    confirmed_hash: str
    summary: str
    detail_note: str
    lines: tuple[WebImportLine, ...] = ()


@dataclass(frozen=True)
class WebImportLine:
    line_id: str
    sku: str
    product_name: str
    quantity: str
    untaxed_unit_price: str
    untaxed_line_amount: str
    delivery_date: str
    detail_note: str
    sku_id: str
    warehouse_id: str
    base_unit_id: str
    order_number: str = ''
    summary: str = ''


def _required(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} is required')
    result = value.strip()
    if any(char in result for char in '#\r\n'):
        raise ValueError(f'{label} contains a summary separator or newline')
    return result


def _day(value, label: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f'{label} must be a date without a time')
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f'{label} must be YYYY-MM-DD')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{label} must be YYYY-MM-DD') from None
    if result.isoformat() != value:
        raise ValueError(f'{label} must be YYYY-MM-DD')
    return result


def _number(value, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or len(str(value)) > 80:
        raise ValueError(f'{label} is invalid')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'{label} is invalid') from None
    if not result.is_finite() or result < 0 or positive and result == 0:
        raise ValueError(f'{label} is invalid')
    if result.adjusted() > 15 or result.as_tuple().exponent < -12:
        raise ValueError(f'{label} is outside supported precision')
    # Excel stores numbers with 15 significant digits. Do not silently round
    # a reviewed quantity or amount during legacy BIFF8 serialization.
    if Decimal(str(float(result))) != result:
        raise ValueError(f'{label} cannot be represented in Excel')
    return result


def _frozen_draft(session: Mapping) -> tuple[dict, dict, int, str]:
    session = dict(session)
    try:
        version = session['version']
        confirmed_version = session['confirmed_version']
        confirmed_hash = session['confirmed_hash']
        payload = json.loads(session['confirmed_payload'])
        current_draft = json.loads(session['draft'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('confirmed sales session is incomplete') from None
    if (not isinstance(payload, dict) or session.get('status') != 'confirmed' or isinstance(version, bool)
            or not isinstance(version, int) or version < 1
            or confirmed_version != version or payload.get('version') != version):
        raise ValueError('sales draft version is not confirmed')
    if (not isinstance(confirmed_hash, str) or len(confirmed_hash) != 64
            or hashlib.sha256(dumps(payload).encode()).hexdigest() != confirmed_hash):
        raise ValueError('confirmed sales draft hash differs')
    draft = payload.get('draft')
    if (not isinstance(draft, dict) or draft != current_draft
            or payload.get('customer_name') != session.get('customer_name')
            or draft.get('customer_name') != session.get('customer_name')
            or payload.get('profile_id') != session.get('profile_id')):
        raise ValueError('confirmed sales draft differs from the session')
    if (draft.get('issues') != [] or not isinstance(draft.get('lines'), list)
            or not 1 <= len(draft['lines']) <= 500):
        raise ValueError('web import requires 1-500 reviewed sales lines')
    return payload, draft, version, confirmed_hash


def catalog_spec_dimension(raw: object) -> str:
    """Accept only an unambiguous size from this account's raw catalog spec."""
    if not isinstance(raw, str):
        return ''
    value = raw.strip()
    if not _CATALOG_DIMENSION.fullmatch(value):
        return ''
    return value.upper().replace('*', 'X').replace('×', 'X')


def _reviewed_dimension(line: dict) -> str:
    if line.get('summary_dimension_source') == 'human_confirmed':
        dimension = line.get('summary_dimension')
        if (not isinstance(dimension, str) or not dimension.strip() or len(dimension) > 200
                or any(char in dimension for char in '#\r\n')):
            raise ValueError('human summary dimension is invalid')
        if line.get('summary_metadata_confirmed') is not True:
            raise ValueError('human summary dimension lacks confirmation')
        _required(line.get('summary_metadata_actor'), 'summary_metadata_actor')
        recorded_at = _required(line.get('summary_metadata_at'), 'summary_metadata_at')
        try:
            datetime.fromisoformat(recorded_at)
        except ValueError:
            raise ValueError('human summary dimension timestamp is invalid') from None
        return dimension
    catalog = line.get('catalog_summary')
    if not isinstance(catalog, dict) or line.get('summary_dimension_source') != 'catalog_spec_shape':
        raise ValueError('summary dimension source is not verified')
    raw_spec = catalog.get('spec')
    dimension = catalog_spec_dimension(raw_spec)
    if not dimension:
        raise ValueError('catalog spec is not a verified dimension')
    if (line.get('spec') != raw_spec or line.get('summary_dimension') != dimension):
        raise ValueError('reviewed summary dimension differs from catalog spec')
    return dimension


def _summary_parts(draft: dict, line: dict, line_id: int) -> list[str]:
    """Use the established 10 fields; unavailable reviewed facts stay '-' ."""
    quantity = format(_number(line.get('quantity'), 'quantity', positive=True), 'f')
    product_name = _required(line.get('product_name'), 'product_name')
    product_code = _required(line.get('product_code'), 'product_code')
    po = _required(draft.get('customer_order_no'), 'customer_order_no')
    grade = str(line.get('grade') or '').strip()
    if grade and grade.replace('.', '', 1).isdigit():
        grade += '级'
    dimension = _reviewed_dimension(line)
    parts = [po, str(line.get('project_no') or '-'), str(line_id),
             str(line.get('drawing_no') or '-'), product_name, dimension,
             grade or '-', str(line.get('surface') or '-'), quantity, product_code]
    for index, value in enumerate(parts):
        if index == 5:
            # The recorded human dimension is part of the frozen draft. Its
            # whitespace and symbols (other than separators/newlines) are not
            # silently rewritten when constructing the note.
            continue
        parts[index] = _required(value, f'summary field {index + 1}')
    return parts


def _detail_note(parts: list[str]) -> str:
    widths = (15, 15, 8, 18, 30, 20, 10, 10, 10)

    def pad(value: str, width: int) -> str:
        used = sum(2 if ord(char) > 127 else 1 for char in value)
        return value + ' ' * max(width - used, 0)

    return '#'.join([*(pad(value, width) for value, width in zip(parts, widths)), parts[-1]])


def build_source_summary(line: Mapping, catalog: Mapping, source_ref: Mapping | None = None) -> dict:
    """Server projection of trusted source evidence, never client summary fields.

    Keep customer wording when it exists, even for an explicitly adopted
    equivalent ERP product. Missing attributes use verified catalog facts;
    no grade or coating is invented. Mirrors NAS remark_spec's local display
    normalization without changing the catalog identity.
    """
    source = source_ref if isinstance(source_ref, Mapping) else {}
    raw = lambda key: str(source.get(key) or '').strip()
    standard = raw('original_standard') or str(catalog.get('standard') or '').strip()
    standard = re.sub(r'-(?:19|20)\d{2}$', '', standard.replace(' ', ''))
    spec = raw('original_spec') or str(line.get('summary_dimension') or catalog.get('spec') or '').strip()
    spec = spec.upper().replace('×', 'X').replace('*', 'X')
    # Old NAS accepted any trailing integer here, which would lose the 25 in
    # M6-25. Remove only an explicit grade repeated after a diameter×length.
    explicit_grade = raw('original_grade') or str(line.get('grade') or catalog.get('grade') or '').strip()
    explicit_grade = explicit_grade.upper().removesuffix('级')
    if explicit_grade and 'X' in spec and spec.endswith('-' + explicit_grade):
        spec = spec[:-(len(explicit_grade) + 1)]
    if standard and spec.upper().startswith(standard.upper()):
        dimension = spec
    else:
        dimension = f'{standard} {spec}'.strip()
    if sum(2 if ord(char) > 127 else 1 for char in dimension) > 20:
        dimension = dimension.replace('GB/T', 'GBT')
    name = raw('original_name') or str(line.get('product_name') or '').strip()
    material = raw('original_material')
    represented = re.sub(r'\s+', '', name + ' ' + explicit_grade).upper()
    grade_represents_material = ((material == '304' and explicit_grade.startswith('A2')) or
                                (material == '316' and explicit_grade.startswith('A4')))
    if material and re.sub(r'\s+', '', material).upper() not in represented and not grade_represents_material:
        name += '(' + material + ')'
    return {
        'project_no': raw('project_no') or str(line.get('project_no') or '').strip(),
        'customer_line_no': raw('customer_line_no'),
        'customer_part_no': raw('customer_part_no') or raw('drawing_no') or str(line.get('drawing_no') or '').strip(),
        'name': name,
        'spec': dimension,
        'grade': raw('original_grade') or str(line.get('grade') or catalog.get('grade') or '').strip(),
        'surface': raw('original_surface') or str(line.get('surface') or catalog.get('surface') or '').strip(),
        'source_ref_id': raw('id'),
    }


def _line_document_summary_parts(draft: dict, line: dict, ordinal: int) -> list[str]:
    source = line.get('source_summary')
    if not isinstance(source, dict):
        # Manual drafts have no attachment source. Their confirmed fields and
        # verified catalog remain the evidence; arbitrary client snapshots are
        # ignored by SalesAssistant._draft before confirmation.
        source = build_source_summary(line, line.get('catalog_summary') or {}, line.get('source_ref'))
    grade = str(source.get('grade') or '').strip()
    if grade and grade.replace('.', '', 1).isdigit():
        grade += '级'
    parts = [draft.get('customer_order_no'), source.get('project_no') or '-',
             source.get('customer_line_no') or str(ordinal), source.get('customer_part_no') or '-',
             source.get('name'), source.get('spec'), grade or '-', source.get('surface') or '-',
             format(_number(line.get('quantity'), 'quantity', positive=True), 'f'), line.get('product_code')]
    try:
        return [_required(value, f'summary field {index + 1}') for index, value in enumerate(parts)]
    except ValueError as error:
        if any('#' in str(value) for value in parts):
            raise ValueError('原始项目号、料号或摘要含 #，当前十段格式不能无损导入；请保留输入并核对分隔符') from error
        raise


def _values(session: Mapping, config: WebImportConfig):
    payload, draft, version, confirmed_hash = _frozen_draft(session)
    for key in ('profile_id', 'data_version', 'owner_id'):
        _required(getattr(config, key), key)
    if (payload.get('profile_id') != config.profile_id
            or payload.get('data_version') != config.data_version
            or payload.get('fixed_sales_owner_id') != config.owner_id):
        raise ValueError('confirmed sales scope differs from import configuration')
    order_date = _day(config.order_date, 'order_date')
    order_number = _required(config.order_number, 'order_number')
    if len(order_number) > 20:
        raise ValueError('order_number exceeds ERP search limit')
    if not all(char.isascii() and (char.isalnum() or char in '-_') for char in order_number):
        raise ValueError('order_number contains unsupported characters')
    if config.document_mode not in {'single_document', 'line_document'}:
        raise ValueError('document mode is invalid')
    line_documents = config.document_mode == 'line_document'
    numbers = config.document_numbers
    if line_documents:
        if (not isinstance(numbers, tuple) or len(numbers) != len(draft['lines']) or
                not numbers or numbers[0] != order_number or len(set(numbers)) != len(numbers) or
                any(not isinstance(number, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', number) for number in numbers)):
            raise ValueError('frozen line document numbers are invalid')
    elif numbers:
        raise ValueError('legacy single document cannot carry line document numbers')
    if str(draft.get('tax_rate')) != VAT_PERCENT:
        raise ValueError('tax rate differs from reviewed draft')
    note = str(draft.get('note') or '').strip()
    if any(char in note for char in '\r\n'):
        raise ValueError('note contains a newline')
    common = {
        '录单日期': order_date, '单据编号': order_number, '单据类型': '销售订单',
        '往来单位名称': _required(draft.get('customer_name'), 'customer_name'),
        '部门名称': _required(config.department_name, 'department_name'),
        '附加说明': note, '制单人名称': _required(config.maker_name, 'maker_name'),
        '经手人名称': _required(config.handler_name, 'handler_name'),
        '仓库名称': _required(config.warehouse_name, 'warehouse_name'),
        '明细仓库名称': _required(config.warehouse_name, 'warehouse_name'),
        '单位': CONFIRMED_BASE_UNIT, '税率(%)': VAT_PERCENT,
    }
    values, result_lines = [], []
    for line_id, line in enumerate(draft['lines'], start=1):
        if not isinstance(line, dict) or line.get('issues') != []:
            raise ValueError('sales line still needs review')
        identity = line.get('resolved_identity')
        if (not isinstance(identity, dict)
                or set(identity) != {'profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit'}
                or not all(isinstance(value, str) and value for value in identity.values())
                or identity.get('profile_id') != config.profile_id
                or identity.get('warehouse_id') != config.warehouse_id
                or line.get('warehouse') != config.warehouse_id):
            raise ValueError('confirmed warehouse identity differs')
        if line.get('unit') != CONFIRMED_BASE_UNIT or identity.get('base_unit') != '1':
            raise ValueError('confirmed base unit differs')
        delivery = _day(line.get('delivery_date') or draft.get('delivery_date'), 'delivery_date')
        if delivery < order_date:
            raise ValueError('delivery_date precedes order_date')
        quantity = _number(line.get('quantity'), 'quantity', positive=True)
        quoted = _number(line.get('unit_price'), 'quoted_unit_price')
        if line.get('tax_basis') not in {'tax_inclusive', 'tax_exclusive'} or str(line.get('tax_rate')) != VAT_PERCENT:
            raise ValueError('tax basis or rate differs from reviewed draft')
        if _number(line.get('quoted_unit_price'), 'quoted_unit_price') != quoted:
            raise ValueError('quoted_unit_price differs from reviewed draft')
        untaxed_unit, untaxed_amount = quote_price(quantity, quoted, line['tax_basis'])
        if (_number(line.get('untaxed_unit_price'), 'untaxed_unit_price') != Decimal(untaxed_unit)
                or _number(line.get('untaxed_line_amount'), 'untaxed_line_amount') != Decimal(untaxed_amount)):
            raise ValueError('tax conversion differs from reviewed draft')
        parts = (_line_document_summary_parts(draft, line, line_id) if line_documents
                 else _summary_parts(draft, line, line_id))
        detail_note = _detail_note(parts)
        row_number = numbers[line_id - 1] if line_documents else order_number
        row_summary = detail_note + '#交期=' + delivery.isoformat()
        if line_documents:
            if len(row_summary.encode('utf-16-le')) // 2 > 180:
                raise ValueError(f'第 {line_id} 行摘要超过网页 180 字符限制，请缩短经确认的原文摘要，输入已保留')
            detail_note = row_summary
        product_name = _required(line.get('product_name'), 'product_name')
        result_lines.append(WebImportLine(str(line_id), parts[-1], product_name, format(quantity, 'f'),
                                          untaxed_unit, untaxed_amount, delivery.isoformat(), detail_note,
                                          identity['sku_id'], identity['warehouse_id'], identity['base_unit'],
                                          row_number if line_documents else '', row_summary if line_documents else ''))
        values.append({**common, '单据编号': row_number, '交货日期': delivery,
                       '商品编号': parts[-1], '商品名称': product_name,
                       '数量': float(quantity), '折前单价': float(Decimal(untaxed_unit)),
                       '折前金额': float(Decimal(untaxed_amount)), '明细备注': detail_note})
    summary = result_lines[0].summary if line_documents else result_lines[0].detail_note + '#交期=' + result_lines[0].delivery_date
    for row, result in zip(values, result_lines):
        row['摘要'] = result.summary if line_documents else summary
    return values, version, confirmed_hash, summary, tuple(result_lines)


def _validate_template(template_path: Path):
    import xlrd

    source = xlrd.open_workbook(str(template_path), formatting_info=True)
    if tuple(source.sheet_names()) != EXPECTED_SHEETS:
        raise ValueError('ERP import template sheets changed')
    sheet = source.sheet_by_index(0)
    if (sheet.nrows != 1 or sheet.ncols != len(EXPECTED_HEADERS)
            or tuple(sheet.row_values(0)) != EXPECTED_HEADERS):
        raise ValueError('ERP import template is not blank or headers changed')
    return source


def _render(source, values: list[dict]) -> bytes:
    import xlwt
    from xlutils.copy import copy

    workbook = copy(source)
    target = workbook.get_sheet(0)
    date_style = xlwt.easyxf(num_format_str='yyyy-mm-dd')
    price_style = xlwt.easyxf(num_format_str='0.000')
    amount_style = xlwt.easyxf(num_format_str='0.00')
    for row_index, row in enumerate(values, start=1):
        for column, header in enumerate(EXPECTED_HEADERS):
            style = (date_style if header in ('录单日期', '交货日期') else
                     price_style if header == '折前单价' else
                     amount_style if header == '折前金额' else xlwt.Style.default_style)
            target.write(row_index, column, row.get(header, ''), style)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _verify(source, raw: bytes, values: list[dict]) -> None:
    import xlrd

    result = xlrd.open_workbook(file_contents=raw, formatting_info=True)
    if tuple(result.sheet_names()) != EXPECTED_SHEETS:
        raise ValueError('rendered workbook sheets changed')
    sheet = result.sheet_by_index(0)
    if sheet.nrows != len(values) + 1 or sheet.ncols != len(EXPECTED_HEADERS) or tuple(sheet.row_values(0)) != EXPECTED_HEADERS:
        raise ValueError('rendered workbook rows or headers changed')
    for row_index, row in enumerate(values, start=1):
        for column, header in enumerate(EXPECTED_HEADERS):
            expected = row.get(header, '')
            actual = sheet.cell_value(row_index, column)
            if header in ('录单日期', '交货日期'):
                if (sheet.cell_type(row_index, column) != xlrd.XL_CELL_DATE or
                        xlrd.xldate_as_datetime(actual, result.datemode).date() != expected):
                    raise ValueError(f'{header} is not the expected Excel date')
            elif isinstance(expected, float):
                if Decimal(str(actual)) != Decimal(str(expected)):
                    raise ValueError(f'{header} differs after XLS serialization')
            elif actual != expected:
                raise ValueError(f'{header} differs after XLS serialization')
    before, after = source.sheet_by_index(1), result.sheet_by_index(1)
    if (before.nrows, before.ncols) != (after.nrows, after.ncols):
        raise ValueError('document-type reference sheet changed')
    for row in range(before.nrows):
        if before.row_values(row) != after.row_values(row):
            raise ValueError('document-type reference sheet changed')


def build_sales_web_import_xls(session: Mapping, config: WebImportConfig) -> WebImportResult:
    """Create a new local .xls; never overwrite a file or contact the ERP."""
    if not isinstance(config, WebImportConfig):
        raise TypeError('config must be WebImportConfig')
    values, version, confirmed_hash, summary, result_lines = _values(session, config)
    template_path = Path(config.template_path)
    destination = Path(config.destination)
    if destination.suffix.lower() != '.xls' or not destination.is_absolute():
        raise ValueError('destination must be an absolute .xls path')
    if not template_path.is_file() or not destination.parent.is_dir():
        raise ValueError('template or destination directory is unavailable')
    if template_path.resolve() == destination.resolve():
        raise ValueError('destination cannot be the source template')
    source = _validate_template(template_path)
    raw = _render(source, values)
    _verify(source, raw, values)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(raw).digest():
            raise ValueError('saved workbook differs from verified bytes')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return WebImportResult(destination, hashlib.sha256(raw).hexdigest(), values[0]['单据编号'],
                           version, confirmed_hash, summary, result_lines[0].detail_note,
                           result_lines)
