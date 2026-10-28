"""Real Guanjiapo purchase-order adapter. One unaudited save, no readback.

Official page 317, method ``erp.purchaseorder.add`` (提交进货订单). The same
ShowDoc item ``erpapi`` was read from
http://d7.mygjp.com.cn:8021/server/index.php?s=/api/page/info&page_id=317
because http://d7safe.mygjp.com.cn:8020 returned an Aliyun 405 from this
environment. Page id 317 there is titled 提交进货订单 and names this method.
Page 1355 in that item is 申请开发者账号-标准api, the project entry, not a
second method list. The method list is the item menu.

The parameter table has no 审核, 草稿, ischeck, or draft field. By contrast,
page 1085 ``erp.purchasebill.save`` (提交进货入库单, a different document)
says ``draft | bool | 是否存为草稿``. This adapter never calls that method.
The return table lists only ``vchcode`` (单据ID). ``number`` is a request
field: 单据编号(不填时自动生成). It is not a response field.

The erpapi menu under 进货单API lists only 提交进货订单, 提交进货入库单, and
管金所进货单详情. 单据草稿 lists sales/fee/receipt drafts, not purchase
orders. There is no read method that fetches an 进货订单 by 单据编号.
``erp.bill.billcenterlist`` is already allow-listed for other documents; page
1708 常用单据类型 does not include 进货订单, and the query has no number
filter and no audit-status field. It is not wired as purchase-order readback.
Acceptance is the write receipt plus a human check in 进货订单管理.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from time import time
from zoneinfo import ZoneInfo

from .purchase_submission import (ErpPurchaseDraft, PreparedPurchaseDraft, PurchaseAdapterCapabilities,
    PurchaseDraftLookup, PurchaseDraftRequest, PurchaseSubmissionBlocked, _hash)

OFFICIAL_WRITE_METHOD = 'erp.purchaseorder.add'
OFFICIAL_WRITE_REFERENCE = 'http://d7safe.mygjp.com.cn:8020/web/#/erpapi/317'
READBACK_METHOD = None
ZONE = ZoneInfo('Asia/Shanghai')
HEADER_FIELDS = ('number', 'date', 'todate', 'total', 'ktypeid', 'btypeid', 'etypeid',
                 'deptid', 'summary', 'comment', 'details')
DETAIL_FIELDS = ('ptypeid', 'pfullname', 'prop1', 'prop2', 'unit', 'unitrate', 'assqty',
                 'assdpprice', 'asstpprice', 'tax', 'comment')
FORBIDDEN_FIELDS = frozenset({
    'draft', 'ischeck', 'ischecked', 'checked', 'audit', 'auditor', 'audited',
    'settleaccounts', 'payment', 'preference', 'pay', 'verify',
})
CONTRACT = {
    'method': OFFICIAL_WRITE_METHOD,
    'reference': OFFICIAL_WRITE_REFERENCE,
    'root': 'purchaseorder',
    'header_fields': HEADER_FIELDS,
    'detail_fields': DETAIL_FIELDS,
    'audit_or_draft_field': None,
    'response_fields': ('vchcode',),
    'response_number_field': None,
    'readback_method': None,
}
OFFICIAL_CONTRACT_HASH = hashlib.sha256(_hash(CONTRACT).encode()).hexdigest()
PTYPE_WINDOW = {'begintime': '1900-01-01 00:00:00', 'endtime': '2100-01-01 00:00:00', 'timetype': 1, 'isall': True}


class PurchaseDirectoryError(Exception):
    def __init__(self, message):
        super().__init__(message)


@dataclass(frozen=True)
class WriteOutcome:
    accepted: bool
    draft: ErpPurchaseDraft | None = None
    detail: str = ''


def document_number(external_reference, explicit=None):
    """20-character number we send, because the response does not return one."""
    if explicit:
        if not re.fullmatch(r'[A-Za-z0-9-]{4,20}', explicit):
            raise ValueError('单据编号需为 4 到 20 位字母、数字或连字符')
        return explicit
    if not re.fullmatch(r'ULP-[0-9a-f]{32}', external_reference or ''):
        raise ValueError('提交引用无法生成单据编号')
    return 'ULP' + external_reference[4:21]


def ulong(value, label, allow_zero=False):
    text = str(value if value is not None else '').strip()
    if not text.isdecimal():
        raise PurchaseDirectoryError(label + ' 不是有效的 ERP 数字身份')
    number = int(text)
    if number >= 2 ** 64 or number < 0 or (number == 0 and not allow_zero):
        raise PurchaseDirectoryError(label + ' 不是有效的 ERP 数字身份')
    return number


def decimal_json(value):
    """JSON text with exact decimals left as numbers, matching the sales wire encoder."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('nonfinite')
        return format(value, 'f')
    if isinstance(value, dict):
        return '{' + ','.join(decimal_json(k) + ':' + decimal_json(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(decimal_json(v) for v in value) + ']'
    return __import__('json').dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def wire_purchaseorder(order):
    unknown = (set(order) - set(HEADER_FIELDS)) | FORBIDDEN_FIELDS.intersection(order)
    if unknown or set(order['details'][0]) - set(DETAIL_FIELDS) or FORBIDDEN_FIELDS.intersection(order['details'][0]):
        raise PurchaseDirectoryError('进货订单含有文档以外的字段')
    if len(order['details']) != 1:
        raise PurchaseDirectoryError('一次只写入一行进货订单')
    return decimal_json(order)


def vchcode_of(response):
    if not isinstance(response, dict):
        return ''
    value = response.get('vchcode')
    if isinstance(value, bool) or not str(value).isdecimal() or int(value) <= 0:
        return ''
    return str(int(value))


def _pages(client, method, params, collection, limit=50):
    rows = []
    for page in range(1, limit + 1):
        result = client.call(method, dict(params, pageno=page, pagesize=100))
        batch = result.get(collection) if isinstance(result, dict) else None
        if not isinstance(batch, list):
            raise PurchaseDirectoryError(method + ' 返回不完整')
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise PurchaseDirectoryError(method + ' 超过本次核对范围')


def _stopped(row):
    return row.get('deleted') is True or row.get('isstop') is True


def find_supplier(client, *, supplier_id='', supplier_code=''):
    if supplier_id:
        rows = _pages(client, 'erp.btype.list', {'ids': [str(supplier_id)]}, 'btypes', limit=5)
        matched = [row for row in rows if str(row.get('id')) == str(supplier_id) and not _stopped(row)]
    else:
        code = supplier_code.strip().casefold()
        matched = []
        for category in (1, 0):
            rows = _pages(client, 'erp.btype.list', {'bcategory': category}, 'btypes')
            matched = [row for row in rows if str(row.get('usercode', '')).strip().casefold() == code and not _stopped(row)]
            if matched:
                break
    if len(matched) != 1:
        raise PurchaseDirectoryError('供应商编号必须对应唯一、未停用、未删除的往来单位')
    row = matched[0]
    category = row.get('bcategory')
    if category in (2, '2'):
        raise PurchaseDirectoryError('这个编号是客户，不是供应商')
    return row


def find_warehouse(client, *, warehouse_id='', warehouse_code=''):
    rows = _pages(client, 'erp.stock.list', {}, 'stocks')
    if warehouse_id:
        matched = [row for row in rows if str(row.get('id')) == str(warehouse_id)]
    else:
        code = warehouse_code.strip().casefold()
        matched = [row for row in rows if str(row.get('usercode', '')).strip().casefold() == code]
    if len(matched) != 1:
        raise PurchaseDirectoryError('仓库编号必须对应唯一仓库')
    return matched[0]


def find_product(client, product_code, sku_id=''):
    code = product_code.strip()
    rows = _pages(client, 'erp.ptype.list', dict(PTYPE_WINDOW, usercodes=[code]), 'ptypes', limit=5)
    matched = [row for row in rows if str(row.get('usercode', '')).strip().casefold() == code.casefold() and not _stopped(row)]
    if len(matched) != 1:
        raise PurchaseDirectoryError('商品编号必须对应唯一、未停用、未删除的商品')
    product = matched[0]
    skus = product.get('skus') if isinstance(product.get('skus'), list) else []
    if sku_id:
        chosen = [row for row in skus if str(row.get('skuid') or row.get('id') or '') == str(sku_id)]
    elif len(skus) == 1:
        chosen = skus
    elif not skus:
        chosen = [{'prop1': 0, 'prop2': 0}]
    else:
        raise PurchaseDirectoryError('这个商品有多个 SKU，请指定 --sku-id')
    if len(chosen) != 1:
        raise PurchaseDirectoryError('SKU 身份不唯一')
    return product, chosen[0]


def base_unit(product):
    units = product.get('units') if isinstance(product.get('units'), list) else []
    if not units:
        return 1, Decimal(1)
    chosen = [row for row in units if str(row.get('ucode')) == '1']
    if len(chosen) != 1:
        raise PurchaseDirectoryError('商品没有唯一的基本单位（ucode=1），没有猜测单位')
    rate = chosen[0].get('urate', 1)
    try:
        parsed = Decimal(str(rate))
    except InvalidOperation:
        raise PurchaseDirectoryError('商品单位换算率无效') from None
    if parsed == 0:
        parsed = Decimal(1)
    if parsed <= 0:
        raise PurchaseDirectoryError('商品单位换算率无效')
    return 1, parsed


def assemble_order(*, number, when, delivery, total, warehouse_id, supplier_id, employee_id,
                   summary, comment, product, sku, quantity, unit_price, tax):
    unit, rate = base_unit(product)
    prop1 = ulong(sku.get('prop1') or 0, '颜色', allow_zero=True)
    prop2 = ulong(sku.get('prop2') or 0, '尺码', allow_zero=True)
    order = {
        'number': number,
        'date': when.strftime('%Y-%m-%d %H:%M:%S'),
        'todate': delivery.strftime('%Y-%m-%d %H:%M:%S'),
        'total': total,
        'ktypeid': ulong(warehouse_id, '仓库'),
        'btypeid': ulong(supplier_id, '供应商'),
        'etypeid': ulong(employee_id, '经手人'),
        'deptid': 0,
        'summary': summary[:200],
        'comment': comment[:500],
        'details': [{
            'ptypeid': ulong(product.get('ptypeid'), '商品'),
            'pfullname': str(product.get('fullname') or '')[:200],
            'prop1': prop1,
            'prop2': prop2,
            'unit': unit,
            'unitrate': rate,
            'assqty': quantity,
            'assdpprice': unit_price,
            'asstpprice': unit_price,
            'tax': tax,
            'comment': comment[:500],
        }],
    }
    return order


def _money(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PurchaseDirectoryError('数量或单价无效') from None
    if not number.is_finite() or number <= 0:
        raise PurchaseDirectoryError('数量或单价必须是正数')
    return number


def _day(value, today):
    if not value:
        return today
    try:
        day = datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise PurchaseDirectoryError('交货日期格式应为 YYYY-MM-DD') from None
    return day


class PurchaseOrderDraftAdapter:
    """Workspace adapter. Writes once. Lookup cannot see ERP purchase orders."""

    def __init__(self, client, *, profile_id, account_id, employee_id, clock=time):
        self.client, self.employee_id, self.clock = client, str(employee_id), clock
        self.capabilities = PurchaseAdapterCapabilities(
            profile_id, account_id, OFFICIAL_WRITE_REFERENCE, OFFICIAL_CONTRACT_HASH,
            True, True, False, False, False, False, False, False, False,
            'write_receipt_human_check')
        self._pending, self._outcomes = {}, {}

    def _moment(self):
        return datetime.fromtimestamp(self.clock(), ZONE)

    def inspect(self, request: PurchaseDraftRequest) -> PreparedPurchaseDraft:
        if request.audit_status != 'unapproved':
            raise PurchaseSubmissionBlocked('purchase_payment_or_audit_forbidden')
        if request.profile_id != self.capabilities.profile_id or request.account_id != self.capabilities.account_id:
            raise PurchaseSubmissionBlocked('purchase_erp_account_unverified')
        try:
            if self.client.identity()['profile_id'] != request.profile_id:
                raise PurchaseSubmissionBlocked('purchase_erp_account_unverified')
            parts = request.line.product_identity.split('|')
            if len(parts) != 5 or parts[0] != request.profile_id:
                raise PurchaseDirectoryError('商品身份不完整')
            _, warehouse_id, product_id, sku_id, _unit = parts
            supplier = find_supplier(self.client, supplier_id=request.supplier_erp_id)
            warehouse = find_warehouse(self.client, warehouse_id=warehouse_id)
            product, sku = find_product(self.client, request.line.product_code, sku_id)
            if str(product.get('ptypeid')) != product_id or str(warehouse.get('id')) != warehouse_id:
                raise PurchaseDirectoryError('商品或仓库身份与采购行不一致')
            quantity, price = _money(request.line.quantity), _money(request.line.unit_price)
            amount = Decimal(str(request.line.amount))
            if not amount.is_finite() or abs(price * quantity - amount) > Decimal('0.01'):
                raise PurchaseDirectoryError('单价乘数量与成交金额不一致')
            moment = self._moment()
            delivery = request.line.delivery_date
            day = _day(delivery, moment.date()) if delivery else moment.date()
            if day < moment.date():
                day = moment.date()
            number = document_number(request.external_reference)
            basis = '含税' if request.line.tax_basis == '含税' else '未税'
            comment = (f'{request.line.note} 单据{number} 引用{request.external_reference} '
                       f'税价口径{basis}。单价原样写入折前/折后单价，税率按官方默认0，未拆税。'
                       '请在进货订单管理核对未审核、未付款、未入库。').strip()
            order = assemble_order(
                number=number, when=moment, delivery=datetime.combine(day, moment.time()),
                total=price * quantity, warehouse_id=warehouse['id'], supplier_id=supplier['id'],
                employee_id=self.employee_id, summary=f'进货订单{number}', comment=comment,
                product=product, sku=sku, quantity=quantity, unit_price=price, tax=Decimal(0))
            wire = wire_purchaseorder(order)
        except PurchaseSubmissionBlocked:
            raise
        except PurchaseDirectoryError as error:
            raise PurchaseSubmissionBlocked('purchase_inspection_unverified') from error
        self._pending[request.external_reference] = wire
        return PreparedPurchaseDraft(
            _hash(asdict(request)), request.profile_id, request.account_id, request.external_reference,
            request.supplier_erp_id, 'unapproved', False, request.line,
            hashlib.sha256(asdict(request.line).__repr__().encode()).hexdigest(),
            OFFICIAL_CONTRACT_HASH, self.clock(), hashlib.sha256(wire.encode()).hexdigest())

    def lookup(self, intent):
        return PurchaseDraftLookup(True, intent.request.profile_id, intent.request.account_id,
                                   intent.request.external_reference, ())

    def submit_once(self, intent) -> None:
        ref = intent.request.external_reference
        if ref in self._outcomes:
            return
        wire = self._pending.pop(ref, None)
        if not wire or hashlib.sha256(wire.encode()).hexdigest() != intent.prepared.evidence_hash:
            self._outcomes[ref] = WriteOutcome(False, detail='purchase_inspection_unverified')
            return
        try:
            response = self.client.call(OFFICIAL_WRITE_METHOD, {'purchaseorder': wire})
        except Exception:
            self._outcomes[ref] = WriteOutcome(False, detail='purchase_readback_not_found')
            return
        vchcode = vchcode_of(response)
        if not vchcode:
            self._outcomes[ref] = WriteOutcome(False, detail='purchase_write_rejected')
            return
        number = document_number(ref)
        draft = ErpPurchaseDraft(
            number, vchcode, intent.request.profile_id, intent.request.account_id, ref,
            intent.request.supplier_erp_id, 'unverified', False, intent.request.line,
            self.clock(), intent.prepared.evidence_hash, 'write_response', vchcode)
        self._outcomes[ref] = WriteOutcome(True, draft, 'purchase_write_receipt_pending_human_check')

    def write_outcome(self, intent) -> WriteOutcome | None:
        return self._outcomes.get(intent.request.external_reference)


def attach_purchase_adapter(submission, client):
    """Pin the real adapter only when both flags and the account env match the vault."""
    policy = submission.policy
    if policy.enabled is not True or policy.allow_submit is not True:
        return False
    if not policy.profile_id or policy.profile_id != policy.account_id:
        return False
    if getattr(client, 'allow_purchase_order', False) is not True or not hasattr(client, 'identity'):
        return False
    try:
        identity = client.identity()
    except Exception:
        return False
    if identity.get('profile_id') != policy.profile_id or not identity.get('employee_id'):
        return False
    submission.adapter = PurchaseOrderDraftAdapter(
        client, profile_id=policy.profile_id, account_id=policy.account_id,
        employee_id=identity['employee_id'])
    return True
