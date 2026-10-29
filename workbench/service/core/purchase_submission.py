"""Opt-in ERP purchase-order draft submission. Default off. No live adapter.

Official method ``erp.purchaseorder.add`` (提交进货订单,
http://d7safe.mygjp.com.cn:8020/web/#/erpapi/317) has no audit/draft field and
returns only ``vchcode``. The erpapi menu has no purchase-order read method.
``PurchaseOrderDraftAdapter`` may write once when both env flags and the
account pin match; success means the write receipt arrived, and a person must
check 进货订单管理. Env flags alone still do not open a network write.

``inspect`` / ``lookup`` are read-only. ``submit_once`` is one write with no
internal retry. An intent is committed before that call. Any existing intent
may only be looked up afterwards: a missing draft never authorizes another
submission. This module never audits, pays, or marks a line ``erp_counted``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from time import time
from typing import Protocol

from .purchase_workspace_data import setting, source_token, text
from .store import dumps, timestamp

OFFICIAL_WRITE_METHOD = 'erp.purchaseorder.add'
OFFICIAL_WRITE_REFERENCE = 'http://d7safe.mygjp.com.cn:8020/web/#/erpapi/317'
ENV_ENABLED = 'UL_PURCHASE_ERP_SUBMIT'
ENV_ALLOW = 'UL_PURCHASE_ERP_ALLOW_SUBMIT'

MESSAGES = {
    'purchase_submission_disabled': '采购 ERP 草稿提交未开放。当前只保存本地成交记录，不会写 ERP。',
    'purchase_adapter_unavailable': '尚未接入已验收的采购订单适配器，未向 ERP 发送。官方 erp.purchaseorder.add 还没有可回查的写入合同。',
    'purchase_write_contract_unverified': '采购订单写入合同尚未验收，未向 ERP 发送。',
    'purchase_readback_contract_unverified': '采购订单回查合同尚未验收，未向 ERP 发送。',
    'purchase_erp_account_unverified': 'ERP 账套尚未核对，未向 ERP 发送。',
    'purchase_payment_or_audit_forbidden': '采购草稿不能付款或审核。未向 ERP 发送。',
    'purchase_draft_confirmation_required': '请确认只创建未审核采购订单草稿，不付款、不审核。',
    'purchase_allocation_not_ordered': '请先记录已下单，再写 ERP 草稿。未下单和已计入的记录不能提交。',
    'purchase_version_changed': '其他人已调整这件商品，请刷新后核对；未向 ERP 发送。',
    'purchase_source_changed': '库存数据已更新，请刷新后核对；未向 ERP 发送。',
    'purchase_duplicate_line': '该供应商的同一平台订单明细已有 ERP 草稿记录，不会再写一行。',
    'purchase_submission_idempotency_conflict': '原操作标识不能用于另一批内容。',
    'purchase_submission_busy': '这一笔采购草稿正在处理，请稍后回查，不要重复提交。',
    'purchase_readback_not_found': '已记下提交意图，但还没有查到对应的 ERP 草稿。请回查，不要重复提交。',
    'purchase_write_receipt_pending_human_check': '已写入进货订单。官方接口只返回单据ID，没有审核字段，也没有按单号回查。请到管家婆进货订单管理核对未审核、未付款、未入库。核对前不要重复提交。',
    'purchase_write_rejected': '管家婆没有返回有效单据ID。请按预定单据编号在进货订单管理核对是否已经生成，不要重复提交。',
    'purchase_readback_incomplete': 'ERP 回查不完整，不能确认单据。请回查，不要重复提交。',
    'purchase_readback_ambiguous': '回查到多张单据，不能确认是哪一张。请人工核对，不要重复提交。',
    'purchase_readback_mismatch': '回查结果与冻结的采购行不一致。请人工核对，不要重复提交。',
    'purchase_readback_not_draft': '回查到的单据不是未审核、未付款草稿。不会记为已计入，也不会再提交。',
    'purchase_inspection_unverified': '提交前核对未通过，未向 ERP 发送。',
    'purchase_inspection_stale': '提交前核对已过期，未向 ERP 发送。请重新发起。',
    'purchase_inspection_unavailable': '提交前核对没有完成，未向 ERP 发送。',
    'purchase_role_forbidden': '需要采购岗位或管理员权限。',
    'purchase_request_invalid': '请提供完整的草稿提交内容。',
    'purchase_restoration_hold': '备份恢复核对期间不能写 ERP。',
    'purchase_product_unavailable': '商品来源缺失或停用，不能写 ERP 草稿。',
    'purchase_supplier_unverified': '供应商 ERP 身份未核对，未向 ERP 发送。',
    'purchase_submission_intent_missing': '提交台账缺少意图，已停止。请人工核对，不要重复提交。',
    'purchase_submission_intent_corrupt': '提交台账已损坏，已停止。请人工核对，不要重复提交。',
    'purchase_recovery_account_changed': 'ERP 账套与当初提交时不一致，已停止。请人工核对，不要重复提交。',
    'purchase_number_invalid': '数量或金额无效，未向 ERP 发送。',
}


class PurchaseSubmissionBlocked(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        self.message = MESSAGES.get(code, '采购草稿提交已停止，未重复写入 ERP。')
        super().__init__(self.message)


@dataclass(frozen=True)
class PurchaseSubmissionPolicy:
    enabled: bool = False
    allow_submit: bool = False
    profile_id: str = ''
    account_id: str = ''
    audit_status: str = 'unapproved'
    max_inspection_age_seconds: int = 300


@dataclass(frozen=True)
class PurchaseAdapterCapabilities:
    profile_id: str = ''
    account_id: str = ''
    official_contract_reference: str = ''
    official_contract_hash: str = ''
    account_verified: bool = False
    write_contract_verified: bool = False
    readback_contract_verified: bool = False
    complete_unique_lookup: bool = False
    durable_document_binding: bool = False
    durable_line_binding: bool = False
    creates_unapproved_draft: bool = False
    allows_payment: bool = False
    allows_audit: bool = False
    acceptance_basis: str = 'erp_readback'


@dataclass(frozen=True)
class PurchaseDraftLine:
    client_line_id: str
    allocation_id: str
    platform_order_no: str
    platform_line_no: str
    supplier_id: str
    supplier_erp_id: str
    product_code: str
    product_identity: str
    quantity: str
    unit_price: str
    amount: str
    tax_basis: str
    note: str
    delivery_date: str = ''


@dataclass(frozen=True)
class PurchaseDraftRequest:
    attempt_id: str
    allocation_id: str
    item_id: str
    confirmed_version: int
    external_reference: str
    profile_id: str
    account_id: str
    actor_id: str
    supplier_id: str
    supplier_erp_id: str
    supplier_name: str
    audit_status: str
    line: PurchaseDraftLine


@dataclass(frozen=True)
class PreparedPurchaseDraft:
    request_hash: str
    profile_id: str
    account_id: str
    external_reference: str
    supplier_erp_id: str
    audit_status: str
    audit_status_verified: bool
    line: PurchaseDraftLine
    line_evidence_hash: str
    contract_hash: str
    observed_at: float
    evidence_hash: str


@dataclass(frozen=True)
class PurchaseDraftIntent:
    id: str
    request: PurchaseDraftRequest
    prepared: PreparedPurchaseDraft


@dataclass(frozen=True)
class ErpPurchaseDraft:
    erp_order_no: str
    erp_line_no: str
    profile_id: str
    account_id: str
    external_reference: str
    supplier_erp_id: str
    audit_status: str
    paid: bool
    line: PurchaseDraftLine
    observed_at: float
    evidence_hash: str
    evidence_basis: str = 'erp_readback'
    vchcode: str = ''


@dataclass(frozen=True)
class PurchaseDraftLookup:
    complete: bool
    profile_id: str
    account_id: str
    external_reference: str
    drafts: tuple[ErpPurchaseDraft, ...] = ()


class PurchaseDraftAdapter(Protocol):
    capabilities: PurchaseAdapterCapabilities

    def inspect(self, request: PurchaseDraftRequest) -> PreparedPurchaseDraft: ...

    def lookup(self, intent: PurchaseDraftIntent) -> PurchaseDraftLookup: ...

    def submit_once(self, intent: PurchaseDraftIntent) -> None: ...


def policy_from_environ(environ=None):
    """Both flags must be the exact string ``1``. Anything else stays off."""
    env = os.environ if environ is None else environ
    enabled = env.get(ENV_ENABLED) == '1' and env.get(ENV_ALLOW) == '1'
    return PurchaseSubmissionPolicy(
        enabled=enabled, allow_submit=enabled,
        profile_id=text(env.get('UL_PURCHASE_ERP_PROFILE_ID')),
        account_id=text(env.get('UL_PURCHASE_ERP_ACCOUNT_ID')))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _number(value, positive=False):
    if value is None or isinstance(value, bool) or len(str(value)) > 80:
        raise PurchaseSubmissionBlocked('purchase_number_invalid', 400)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PurchaseSubmissionBlocked('purchase_number_invalid', 400) from None
    if not result.is_finite() or result < 0 or positive and not result or result.adjusted() > 15 or result.as_tuple().exponent < -12:
        raise PurchaseSubmissionBlocked('purchase_number_invalid', 400)
    return result


def _number_text(value, positive=False):
    return format(_number(value, positive).normalize(), 'f')


# Twelve places is the finest ``_number`` accepts. amount/qty often repeats past that.
_PRICE_STEP = Decimal('1e-12')


def saved_deal(quantity, amount, unit_price):
    """Use the saved quantity and amount. Rebuild a unit price the checker allows.

    A line total such as 60.97 for 22 支 divides to 27 decimal places. That
    price is already on the allocation; the confirm dialog does not send it
    again. Rejecting it blocked every write of an uneven deal.
    """
    qty = _number(quantity, True)
    money = _number(amount)
    price = None
    if unit_price not in (None, ''):
        try:
            price = _number(unit_price)
        except PurchaseSubmissionBlocked:
            price = None
    if price is None or abs(price * qty - money) > Decimal('0.01'):
        price = (money / qty).quantize(_PRICE_STEP, rounding=ROUND_HALF_UP)
    if price.as_tuple().exponent < -12 or abs(price * qty - money) > Decimal('0.01'):
        raise PurchaseSubmissionBlocked('purchase_number_invalid', 400)
    return _number_text(qty, True), _number_text(price), _number_text(money)


def _restore(value):
    request = dict(value['request'])
    request['line'] = PurchaseDraftLine(**request['line'])
    prepared = dict(value['prepared'])
    prepared['line'] = PurchaseDraftLine(**prepared['line'])
    return PurchaseDraftIntent(value['id'], PurchaseDraftRequest(**request), PreparedPurchaseDraft(**prepared))


class PurchaseSubmission:
    def __init__(self, store, adapter: PurchaseDraftAdapter | None = None, policy=None, *, clock=time):
        self.store, self.adapter, self.clock = store, adapter, clock
        self.policy = policy if policy is not None else policy_from_environ()
        with store.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS purchase_submission_attempts(
                id TEXT PRIMARY KEY, allocation_id TEXT NOT NULL UNIQUE, item_id TEXT NOT NULL,
                confirmed_version INTEGER NOT NULL, external_reference TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                erp_order_no TEXT NOT NULL DEFAULT '', erp_line_no TEXT NOT NULL DEFAULT '',
                receipt TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS purchase_submission_intents(
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS purchase_submission_operations(
                actor_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                attempt_id TEXT NOT NULL, PRIMARY KEY(actor_id, idempotency_key))''')
            db.execute('''CREATE TABLE IF NOT EXISTS purchase_submission_events(
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                action TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS purchase_submission_bindings(
                profile_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
                platform_order_no TEXT NOT NULL, platform_line_no TEXT NOT NULL,
                attempt_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                PRIMARY KEY(profile_id, supplier_id, platform_order_no, platform_line_no))''')

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec='seconds')

    def _writing_allowed(self):
        return self.policy.enabled is True and self.policy.allow_submit is True

    def ready(self):
        if not self._writing_allowed():
            return False
        try:
            self._capabilities(writing=True)
        except PurchaseSubmissionBlocked:
            return False
        return True

    def _fresh(self, stamp):
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not 0 <= self.clock() - stamp <= self.policy.max_inspection_age_seconds:
            raise PurchaseSubmissionBlocked('purchase_inspection_stale')

    @contextmanager
    def _lock(self):
        root = self.store.runtime / 'purchase-submission-locks'
        root.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(root / 'purchase-erp-draft.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PurchaseSubmissionBlocked('purchase_submission_busy') from None
            yield
        finally:
            os.close(fd)

    def _authorize(self, user):
        from .purchase_workspace import authorize
        authorize(user)

    def _parse(self, body):
        if not isinstance(body, dict):
            raise PurchaseSubmissionBlocked('purchase_request_invalid', 400)
        key = text(body.get('idempotency_key'))
        allocation_id = text(body.get('allocation_id'))
        item_id = text(body.get('item_id'))
        version = body.get('version')
        token = text(body.get('source_token'))
        if not 8 <= len(key) <= 128 or not allocation_id or not item_id or not token:
            raise PurchaseSubmissionBlocked('purchase_request_invalid', 400)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise PurchaseSubmissionBlocked('purchase_request_invalid', 400)
        fingerprint = _hash({
            'allocation_id': allocation_id, 'item_id': item_id, 'version': version,
            'source_token': token, 'draft_confirmed': body.get('draft_confirmed') is True})
        return key, allocation_id, item_id, version, token, fingerprint

    def _capabilities(self, writing=False):
        if writing and not self._writing_allowed():
            raise PurchaseSubmissionBlocked('purchase_submission_disabled')
        cap = getattr(self.adapter, 'capabilities', None)
        if not isinstance(cap, PurchaseAdapterCapabilities):
            raise PurchaseSubmissionBlocked('purchase_adapter_unavailable')
        if cap.allows_payment is not False or cap.allows_audit is not False:
            raise PurchaseSubmissionBlocked('purchase_payment_or_audit_forbidden')
        if (not self.policy.profile_id or not self.policy.account_id or cap.profile_id != self.policy.profile_id
                or cap.account_id != self.policy.account_id or cap.account_verified is not True):
            raise PurchaseSubmissionBlocked('purchase_erp_account_unverified')
        readback = (bool(cap.official_contract_reference) and _valid_hash(cap.official_contract_hash)
                    and cap.readback_contract_verified is True and cap.complete_unique_lookup is True
                    and cap.durable_document_binding is True and cap.durable_line_binding is True)
        receipt = (cap.acceptance_basis == 'write_receipt_human_check' and bool(cap.official_contract_reference)
                   and _valid_hash(cap.official_contract_hash) and cap.write_contract_verified is True
                   and cap.readback_contract_verified is False and cap.complete_unique_lookup is False
                   and cap.creates_unapproved_draft is False and cap.durable_document_binding is False
                   and cap.durable_line_binding is False)
        if not readback and not receipt:
            raise PurchaseSubmissionBlocked('purchase_readback_contract_unverified')
        if writing and readback and (cap.write_contract_verified is not True or cap.creates_unapproved_draft is not True
                                     or self.policy.audit_status != 'unapproved'):
            raise PurchaseSubmissionBlocked('purchase_write_contract_unverified')
        if writing and receipt and self.policy.audit_status != 'unapproved':
            raise PurchaseSubmissionBlocked('purchase_write_contract_unverified')
        return cap

    def _context(self, db, item_id, allocation_id, version, token):
        if source_token(db) != token:
            raise PurchaseSubmissionBlocked('purchase_source_changed')
        if setting(db, 'restoration_hold', False) not in (False, 0, '', '0', 'false', None):
            raise PurchaseSubmissionBlocked('purchase_restoration_hold')
        row = db.execute('SELECT * FROM purchase_workspace_index WHERE id=?', (item_id,)).fetchone()
        decision = db.execute('SELECT * FROM purchase_workspace_decisions WHERE item_id=?', (item_id,)).fetchone()
        alloc = db.execute('SELECT * FROM purchase_workspace_allocations WHERE id=? AND item_id=?', (allocation_id, item_id)).fetchone()
        if not row or not decision or not alloc or alloc['state'] not in {'ordered', 'awaiting_audit'}:
            raise PurchaseSubmissionBlocked('purchase_allocation_not_ordered')
        if decision['version'] != version:
            raise PurchaseSubmissionBlocked('purchase_version_changed')
        item = json.loads(row['payload'])
        reasons = item.get('review_reasons') or []
        if item.get('source_missing') or not text(item.get('sku_id')) or any('停用' in text(reason) for reason in reasons):
            raise PurchaseSubmissionBlocked('purchase_product_unavailable')
        if text(item.get('profile_id')) != row['profile_id']:
            raise PurchaseSubmissionBlocked('purchase_product_unavailable')
        supplier = db.execute(
            'SELECT * FROM purchase_workspace_suppliers WHERE id=? AND profile_id=? AND active=1',
            (alloc['supplier_id'], row['profile_id'])).fetchone()
        if not supplier or not text(supplier['erp_id']):
            raise PurchaseSubmissionBlocked('purchase_supplier_unverified')
        details = json.loads(alloc['details'] or '{}')
        order_no, line_no = text(details.get('platform_order_no'))[:60], text(details.get('platform_line_no'))[:60]
        tax = details.get('tax_basis')
        if not order_no or not line_no or tax not in {'含税', '未税'}:
            raise PurchaseSubmissionBlocked('purchase_allocation_not_ordered')
        identity = '|'.join(text(item.get(key)) for key in ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit'))
        if not all(identity.split('|')) or not text(item.get('sku')):
            raise PurchaseSubmissionBlocked('purchase_product_unavailable')
        quantity, price, money = saved_deal(alloc['quantity'], details.get('amount'), details.get('unit_price'))
        line = PurchaseDraftLine(
            line_no, allocation_id, order_no, line_no, alloc['supplier_id'], text(supplier['erp_id']),
            text(item.get('sku')), identity, quantity, price, money, tax,
            text(details.get('note'))[:500], text(details.get('delivery_date'))[:10])
        return row, supplier, line

    def _request(self, attempt, actor, supplier, line, version, item_id):
        if line.allocation_id != attempt['allocation_id']:
            raise PurchaseSubmissionBlocked('purchase_inspection_unverified')
        return PurchaseDraftRequest(
            attempt['id'], attempt['allocation_id'], item_id, version, attempt['external_reference'],
            self.policy.profile_id, self.policy.account_id, actor, line.supplier_id, line.supplier_erp_id,
            text(supplier['name']), 'unapproved', line)

    def _prepared(self, request, prepared, cap):
        if not isinstance(prepared, PreparedPurchaseDraft):
            raise PurchaseSubmissionBlocked('purchase_inspection_unverified')
        self._fresh(prepared.observed_at)
        if prepared.request_hash != _hash(asdict(request)):
            raise PurchaseSubmissionBlocked('purchase_inspection_unverified')
        expected = (request.profile_id, request.account_id, request.external_reference, request.supplier_erp_id, request.audit_status)
        if (prepared.profile_id, prepared.account_id, prepared.external_reference, prepared.supplier_erp_id, prepared.audit_status) != expected:
            raise PurchaseSubmissionBlocked('purchase_inspection_unverified')
        verified = prepared.audit_status_verified is True
        if cap.acceptance_basis == 'write_receipt_human_check':
            verified = prepared.audit_status_verified is False
        if prepared.audit_status != 'unapproved' or not verified or prepared.contract_hash != cap.official_contract_hash:
            raise PurchaseSubmissionBlocked('purchase_inspection_unverified')
        if not _valid_hash(prepared.line_evidence_hash) or not _valid_hash(prepared.evidence_hash) or prepared.line != request.line:
            raise PurchaseSubmissionBlocked('purchase_inspection_unverified')

    def _lookup(self, intent):
        cap = self._capabilities(writing=False)
        if (intent.request.profile_id, intent.request.account_id) != (cap.profile_id, cap.account_id):
            raise PurchaseSubmissionBlocked('purchase_recovery_account_changed')
        if cap.acceptance_basis == 'write_receipt_human_check':
            return None
        result = self.adapter.lookup(intent)
        if (not isinstance(result, PurchaseDraftLookup) or result.complete is not True
                or (result.profile_id, result.account_id, result.external_reference) != (cap.profile_id, cap.account_id, intent.request.external_reference)):
            raise PurchaseSubmissionBlocked('purchase_readback_incomplete')
        if len(result.drafts) > 1:
            raise PurchaseSubmissionBlocked('purchase_readback_ambiguous')
        if not result.drafts:
            return None
        draft = result.drafts[0]
        if not isinstance(draft, ErpPurchaseDraft) or draft.evidence_basis != 'erp_readback':
            raise PurchaseSubmissionBlocked('purchase_readback_incomplete')
        self._fresh(draft.observed_at)
        if draft.paid is not False or draft.audit_status != 'unapproved':
            raise PurchaseSubmissionBlocked('purchase_readback_not_draft')
        if not text(draft.erp_order_no) or not text(draft.erp_line_no) or not _valid_hash(draft.evidence_hash):
            raise PurchaseSubmissionBlocked('purchase_readback_mismatch')
        request = intent.request
        if (draft.profile_id, draft.account_id, draft.external_reference, draft.supplier_erp_id) != (
                request.profile_id, request.account_id, request.external_reference, request.supplier_erp_id):
            raise PurchaseSubmissionBlocked('purchase_readback_mismatch')
        line, original = draft.line, request.line
        if not isinstance(line, PurchaseDraftLine):
            raise PurchaseSubmissionBlocked('purchase_readback_mismatch')
        same = (line.client_line_id, line.allocation_id, line.platform_order_no, line.platform_line_no,
                line.supplier_id, line.supplier_erp_id, line.product_code, line.product_identity, line.tax_basis, line.note)
        expect = (original.client_line_id, original.allocation_id, original.platform_order_no, original.platform_line_no,
                  original.supplier_id, original.supplier_erp_id, original.product_code, original.product_identity, original.tax_basis, original.note)
        if same != expect or _number(line.quantity, True) != _number(original.quantity, True) or _number(line.unit_price) != _number(original.unit_price) or _number(line.amount) != _number(original.amount):
            raise PurchaseSubmissionBlocked('purchase_readback_mismatch')
        return draft

    def _event(self, db, attempt, actor, action, payload):
        db.execute('INSERT INTO purchase_submission_events VALUES (?,?,?,?,?,?)',
                   (uuid.uuid4().hex, attempt['id'], actor, action, dumps(payload), self._now()))

    def _set(self, db, attempt, actor, status, detail='', receipt=None):
        latest = db.execute('SELECT status FROM purchase_submission_attempts WHERE id=?', (attempt['id'],)).fetchone()
        if latest and latest['status'] in {'submitted', 'awaiting_human_check'}:
            return
        db.execute('''UPDATE purchase_submission_attempts SET status=?,detail=?,updated_at=?,
            erp_order_no=CASE WHEN ? IS NULL THEN erp_order_no ELSE ? END,
            erp_line_no=CASE WHEN ? IS NULL THEN erp_line_no ELSE ? END,
            receipt=CASE WHEN ? IS NULL THEN receipt ELSE ? END WHERE id=?''',
                   (status, detail, self._now(),
                    'yes' if receipt else None, receipt.erp_order_no if receipt else '',
                    'yes' if receipt else None, receipt.erp_line_no if receipt else '',
                    'yes' if receipt else None, dumps(asdict(receipt)) if receipt else '',
                    attempt['id']))
        self._event(db, attempt, actor, status, {'code': detail, 'erp_order_no': receipt.erp_order_no if receipt else ''})

    def _bind(self, db, request, attempt_id):
        line = request.line
        row = db.execute('''SELECT attempt_id FROM purchase_submission_bindings
            WHERE profile_id=? AND supplier_id=? AND platform_order_no=? AND platform_line_no=?''',
            (request.profile_id, line.supplier_id, line.platform_order_no, line.platform_line_no)).fetchone()
        if row:
            if row['attempt_id'] != attempt_id:
                raise PurchaseSubmissionBlocked('purchase_duplicate_line')
            return
        try:
            db.execute('INSERT INTO purchase_submission_bindings VALUES (?,?,?,?,?,?)',
                       (request.profile_id, line.supplier_id, line.platform_order_no, line.platform_line_no, attempt_id, self._now()))
        except sqlite3.IntegrityError:
            raise PurchaseSubmissionBlocked('purchase_duplicate_line') from None

    def _apply(self, db, attempt, actor, status, detail, receipt):
        alloc = db.execute('SELECT * FROM purchase_workspace_allocations WHERE id=?', (attempt['allocation_id'],)).fetchone()
        if not alloc or alloc['state'] in {'cancelled', 'erp_counted', 'draft'}:
            return
        details = json.loads(alloc['details'] or '{}')
        if status == 'submitted' and receipt is not None:
            updated = {
                'erp_draft_no': receipt.erp_order_no, 'erp_draft_line_no': receipt.erp_line_no,
                'erp_draft_status': 'unapproved', 'erp_submit_status': 'submitted', 'erp_submit_detail': '',
                'erp_submit_external_reference': attempt['external_reference'], 'erp_submit_attempt_id': attempt['id']}
        elif status == 'awaiting_human_check' and receipt is not None:
            updated = {
                'erp_draft_no': receipt.erp_order_no, 'erp_draft_line_no': receipt.erp_line_no,
                'erp_draft_status': 'pending_human_check', 'erp_submit_status': 'awaiting_human_check',
                'erp_submit_detail': 'purchase_write_receipt_pending_human_check',
                'erp_submit_external_reference': attempt['external_reference'], 'erp_submit_attempt_id': attempt['id']}
        elif status == 'submission_unknown':
            updated = {
                'erp_submit_status': 'submission_unknown', 'erp_submit_detail': detail,
                'erp_submit_external_reference': attempt['external_reference'], 'erp_submit_attempt_id': attempt['id']}
        else:
            return
        if all(details.get(key) == value for key, value in updated.items()):
            return
        details.update(updated)
        now = timestamp()
        db.execute('UPDATE purchase_workspace_allocations SET details=?,updated_at=? WHERE id=?', (dumps(details), now, alloc['id']))
        db.execute('UPDATE purchase_workspace_decisions SET version=version+1,updated_at=? WHERE item_id=?', (now, alloc['item_id']))
        db.execute('INSERT INTO purchase_workspace_events(item_id,actor_id,action,details,created_at) VALUES (?,?,?,?,?)',
                   (alloc['item_id'], actor, 'submit_erp_draft', dumps({
                       'target': {'allocation_id': alloc['id']},
                       'body': {'note': updated.get('erp_draft_no') or detail}}), now))

    def _result(self, attempt_id, replayed=False):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM purchase_submission_attempts WHERE id=?', (attempt_id,)).fetchone()
        if row['status'] == 'submitted' and row['erp_order_no']:
            message = f"已回查到未审核采购草稿 {row['erp_order_no']} / {row['erp_line_no']}。未付款、未审核，本地预留未释放。"
        elif row['status'] == 'awaiting_human_check' and row['erp_order_no']:
            message = (f"已写入进货订单 {row['erp_order_no']}（单据ID {row['erp_line_no']}）。"
                       '官方接口没有审核字段，也没有按单号回查。请到管家婆进货订单管理核对此单未审核、未付款、未入库。'
                       '核对前不要重复提交。')
        elif row['status'] == 'submission_unknown':
            message = MESSAGES.get(row['detail'], MESSAGES['purchase_readback_not_found'])
            cap = getattr(self.adapter, 'capabilities', None)
            if getattr(cap, 'acceptance_basis', '') == 'write_receipt_human_check':
                from .purchase_erp_adapter import document_number
                try:
                    number = document_number(row['external_reference'])
                except ValueError:
                    number = ''
                if number:
                    message += f' 预定单据编号 {number}。请按这个编号在进货订单管理核对是否已经生成，不要重复提交。'
        elif row['status'] == 'blocked':
            message = MESSAGES.get(row['detail'], '未向 ERP 发送。')
        elif row['status'] == 'intent_recorded':
            message = '已记下提交意图，正在回查，请不要重复提交。'
        else:
            message = '采购草稿提交没有完成。'
        return {'attempt_id': row['id'], 'allocation_id': row['allocation_id'], 'item_id': row['item_id'],
                'status': row['status'], 'detail': row['detail'], 'message': message,
                'erp_order_no': row['erp_order_no'], 'erp_line_no': row['erp_line_no'],
                'external_reference': row['external_reference'], 'replayed': replayed, 'erp_counted': False}

    def _load_intent(self, attempt):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM purchase_submission_intents WHERE attempt_id=?', (attempt['id'],)).fetchone()
        if not row:
            raise PurchaseSubmissionBlocked('purchase_submission_intent_missing')
        if _hash(json.loads(row['payload'])) != row['payload_hash']:
            raise PurchaseSubmissionBlocked('purchase_submission_intent_corrupt')
        intent = _restore(json.loads(row['payload']))
        if (intent.request.attempt_id, intent.request.allocation_id, intent.request.external_reference) != (
                attempt['id'], attempt['allocation_id'], attempt['external_reference']):
            raise PurchaseSubmissionBlocked('purchase_submission_intent_corrupt')
        return intent

    def _recover(self, attempt, actor):
        intent = self._load_intent(attempt)
        try:
            cap = self._capabilities(writing=False)
            if cap.acceptance_basis == 'write_receipt_human_check':
                outcome = self.adapter.write_outcome(intent)
                if outcome is not None and outcome.accepted and outcome.draft is not None:
                    receipt, status, detail = outcome.draft, 'awaiting_human_check', outcome.detail
                elif outcome is not None and outcome.detail:
                    receipt, status, detail = None, 'submission_unknown', outcome.detail
                else:
                    receipt, status, detail = None, 'submission_unknown', 'purchase_readback_not_found'
            else:
                receipt = self._lookup(intent)
                status, detail = ('submitted', '') if receipt else ('submission_unknown', 'purchase_readback_not_found')
        except PurchaseSubmissionBlocked as error:
            receipt, status, detail = None, 'submission_unknown', error.code
        except Exception:
            receipt, status, detail = None, 'submission_unknown', 'purchase_readback_incomplete'
        with self.store.connect(write=True) as db:
            self._set(db, attempt, actor, status, detail, receipt)
            self._apply(db, attempt, actor, status, detail, receipt)
        return self._result(attempt['id'])

    def submit(self, user, body):
        """Create or recover one allocation attempt. At most one ERP write.

        The flag-off path returns before any ledger row or adapter call.
        After an intent exists, later calls only look the draft up.
        """
        self._authorize(user)
        key, allocation_id, item_id, version, token, fingerprint = self._parse(body)
        actor = text(user.get('id') if isinstance(user, dict) else '')
        replayed = False
        with self._lock():
            with self.store.connect(write=True) as db:
                operation = db.execute(
                    'SELECT * FROM purchase_submission_operations WHERE actor_id=? AND idempotency_key=?',
                    (actor, key)).fetchone()
                if operation and operation['request_hash'] != fingerprint:
                    raise PurchaseSubmissionBlocked('purchase_submission_idempotency_conflict')
                if operation:
                    attempt = dict(db.execute('SELECT * FROM purchase_submission_attempts WHERE id=?', (operation['attempt_id'],)).fetchone())
                    replayed = True
                else:
                    existing = db.execute('SELECT * FROM purchase_submission_attempts WHERE allocation_id=?', (allocation_id,)).fetchone()
                    if existing is None:
                        if not self._writing_allowed():
                            raise PurchaseSubmissionBlocked('purchase_submission_disabled')
                        if body.get('draft_confirmed') is not True:
                            raise PurchaseSubmissionBlocked('purchase_draft_confirmation_required', 400)
                        self._context(db, item_id, allocation_id, version, token)
                        if not self.policy.profile_id:
                            raise PurchaseSubmissionBlocked('purchase_erp_account_unverified')
                        identity = uuid.uuid4().hex
                        now = self._now()
                        db.execute('''INSERT INTO purchase_submission_attempts
                            (id,allocation_id,item_id,confirmed_version,external_reference,status,detail,erp_order_no,erp_line_no,receipt,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                                   (identity, allocation_id, item_id, version, 'ULP-' + identity, 'preflight', '', '', '', '', now, now))
                        attempt = dict(db.execute('SELECT * FROM purchase_submission_attempts WHERE id=?', (identity,)).fetchone())
                    else:
                        attempt = dict(existing)
                    db.execute('INSERT INTO purchase_submission_operations VALUES (?,?,?,?)', (actor, key, fingerprint, attempt['id']))
                intent = db.execute('SELECT 1 FROM purchase_submission_intents WHERE attempt_id=?', (attempt['id'],)).fetchone()
            if attempt['status'] in {'submitted', 'awaiting_human_check'}:
                return self._result(attempt['id'], replayed)
            if intent:
                self._recover(attempt, actor)
                return self._result(attempt['id'], replayed)
            if attempt['status'] == 'blocked' and attempt['detail'] == 'purchase_duplicate_line':
                return self._result(attempt['id'], replayed)
            if not self._writing_allowed():
                raise PurchaseSubmissionBlocked('purchase_submission_disabled')
            try:
                cap = self._capabilities(writing=True)
                with self.store.connect() as db:
                    row, supplier, line = self._context(db, item_id, allocation_id, version, token)
                    if row['profile_id'] != self.policy.profile_id:
                        raise PurchaseSubmissionBlocked('purchase_erp_account_unverified')
                    request = self._request(attempt, actor, supplier, line, version, item_id)
                prepared = self.adapter.inspect(request)
                self._prepared(request, prepared, cap)
                planned = PurchaseDraftIntent(uuid.uuid4().hex, request, prepared)
                existing = self._lookup(planned)
                if existing:
                    with self.store.connect(write=True) as db:
                        self._bind(db, request, attempt['id'])
                        self._set(db, attempt, actor, 'submitted', '', existing)
                        self._apply(db, attempt, actor, 'submitted', '', existing)
                    return self._result(attempt['id'], replayed)
                with self.store.connect(write=True) as db:
                    fresh_row, fresh_supplier, fresh_line = self._context(db, item_id, allocation_id, version, token)
                    if fresh_row['profile_id'] != request.profile_id or self._request(attempt, actor, fresh_supplier, fresh_line, version, item_id) != request:
                        raise PurchaseSubmissionBlocked('purchase_version_changed')
                    self._capabilities(writing=True)
                    self._fresh(prepared.observed_at)
                    self._bind(db, request, attempt['id'])
                    value = asdict(planned)
                    db.execute('INSERT INTO purchase_submission_intents VALUES (?,?,?,?,?)',
                               (planned.id, attempt['id'], dumps(value), _hash(value), self._now()))
                    self._set(db, attempt, actor, 'intent_recorded')
            except PurchaseSubmissionBlocked as error:
                with self.store.connect(write=True) as db:
                    self._set(db, attempt, actor, 'blocked', error.code)
                return self._result(attempt['id'], replayed)
            except Exception:
                with self.store.connect(write=True) as db:
                    self._set(db, attempt, actor, 'blocked', 'purchase_inspection_unavailable')
                return self._result(attempt['id'], replayed)
            try:
                self.adapter.submit_once(planned)
            except Exception:
                pass
            self._recover(attempt, actor)
            return self._result(attempt['id'], replayed)
