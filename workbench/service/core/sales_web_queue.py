"""Frozen, opt-in sales-assistant handoff to the shared browser queue.

This module performs no ERP I/O.  It validates the confirmed draft using the
same pure import projection that the browser worker will use, then inserts the
operation in the caller's SQLite transaction so the session lock and queue
entry cannot diverge.  The worker owns XLS creation, the single upload
attempt, and a complete website import confirmation. No ERP readback follows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import re
import uuid

from .sales_web_import import WebImportConfig, _values
from .store import dumps
from .unit_policy import CONFIRMED_BASE_UNIT, USER_UNIT_EVIDENCE, VALID_UNIT_EVIDENCE
from .web_operations import WebOperations, now


POLICY_SETTING = 'sales_web_import_policy'
SCHEMA = 'sales_web_import_v2'
HASH = re.compile(r'[0-9a-f]{64}\Z')
RETAIL_CUSTOMER = '备用零售客户'


class SalesWebQueueBlocked(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _required(value, field, *, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\n' in value or '\r' in value:
        raise SalesWebQueueBlocked('sales_web_configuration_incomplete', f'销售网页配置缺少有效的{field}')
    return value.strip()


def _policy(db):
    row = db.execute('SELECT value FROM settings WHERE key=?', (POLICY_SETTING,)).fetchone()
    if not row:
        return {}
    try:
        policy = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return policy if isinstance(policy, dict) else {}


def policy_available(db, profile_id):
    policy = _policy(db)
    return policy.get('enabled') is True and policy.get('profile_id') == profile_id


def order_number(profile_id, session_id):
    """A stable 20-character ERP full number, unique per random session ID."""
    return 'UL' + sha256((profile_id + '\x00' + session_id).encode()).hexdigest()[:18].upper()


def line_document_numbers(profile_id, session_id, count):
    """Deterministic, independently reserved ERP numbers for frozen line order."""
    if type(count) is not int or not 1 <= count <= 500:
        raise SalesWebQueueBlocked('sales_line_count_invalid', '销售明细须为 1 至 500 行')
    stem = 'UL' + sha256((profile_id + '\x00' + session_id + '\x00line-document-v2').encode()).hexdigest()[:15].upper()
    return tuple(stem + f'{ordinal:03d}' for ordinal in range(1, count + 1))


def _session_snapshot(row):
    return {key: row[key] for key in ('id', 'profile_id', 'customer_name', 'version', 'status',
                                      'confirmed_version', 'confirmed_hash', 'confirmed_payload', 'draft')}


def _customer_binding(policy, customer, profile_id, account_id):
    bindings = policy.get('customer_bindings')
    binding = bindings.get(customer) if isinstance(bindings, dict) else None
    if not isinstance(binding, dict):
        raise SalesWebQueueBlocked('sales_web_customer_unverified', '客户缺少 ERP 精确身份核验证据')
    if (binding.get('customer_name') != customer or
            binding.get('profile_id') != profile_id or binding.get('account_id') != account_id or
            type(binding.get('match_count')) is not int or binding['match_count'] != 1 or
            binding.get('source') != 'erp_customer_exact_lookup'):
        raise SalesWebQueueBlocked('sales_web_customer_unverified', '客户 ERP 账号或唯一匹配证据不符')
    try: _required(binding.get('erp_customer_id'), 'ERP客户身份', limit=128)
    except SalesWebQueueBlocked:
        raise SalesWebQueueBlocked('sales_web_customer_unverified', '客户 ERP 身份缺失') from None
    source_hash = binding.get('source_hash')
    if not isinstance(source_hash, str) or not HASH.fullmatch(source_hash):
        raise SalesWebQueueBlocked('sales_web_customer_unverified', '客户 ERP 查询证据校验值无效')
    try:
        observed = datetime.fromisoformat(binding['observed_at'])
        if observed.tzinfo is None:
            raise ValueError('timezone missing')
    except (KeyError, TypeError, ValueError):
        raise SalesWebQueueBlocked('sales_web_customer_unverified', '客户 ERP 查询时间无效') from None
    age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
    if age < -timedelta(minutes=5):
        raise SalesWebQueueBlocked('sales_web_customer_unverified', '客户 ERP 绑定时间异常')
    return {key: binding[key] for key in ('customer_name', 'erp_customer_id', 'profile_id',
        'account_id', 'match_count', 'source', 'source_hash', 'observed_at')}


def prepare_sales_web_payload(db, row, owner_id):
    policy = _policy(db)
    if policy.get('enabled') is not True:
        raise SalesWebQueueBlocked('sales_submission_unavailable', '销售网页提交尚未启用，确认稿已保留')
    profile_id = row['profile_id']
    if policy.get('profile_id') != profile_id:
        raise SalesWebQueueBlocked('sales_web_scope_mismatch', '网页提交账套与确认稿不一致')
    account_id = _required(policy.get('account_id'), 'ERP账号身份', limit=128)
    if not HASH.fullmatch(account_id):
        raise SalesWebQueueBlocked('sales_web_configuration_incomplete', 'ERP账号身份校验值须为 SHA256')
    department = _required(policy.get('department_name'), '部门名称')
    maker = _required(policy.get('maker_name'), '制单人')
    warehouse_id = _required(policy.get('warehouse_id'), '仓库身份', limit=128)
    warehouse_name = _required(policy.get('warehouse_name'), '仓库名称')
    template_hash = policy.get('template_sha256')
    if not isinstance(template_hash, str) or not HASH.fullmatch(template_hash):
        raise SalesWebQueueBlocked('sales_web_configuration_incomplete', '原始 XLS 模板校验值尚未配置')
    handlers = policy.get('handler_by_owner')
    if not isinstance(handlers, dict) or owner_id not in handlers:
        raise SalesWebQueueBlocked('sales_web_handler_unmapped', '固定销售尚未映射到 ERP 经手人')
    handler = _required(handlers[owner_id], 'ERP经手人')
    binding = _customer_binding(policy, row['customer_name'], profile_id, account_id)
    if row['customer_name'] == RETAIL_CUSTOMER and policy.get('retail_test_session_id') != row['id']:
        raise SalesWebQueueBlocked('sales_retail_test_not_approved', '备用零售客户仅限已批准的指定测试会话')
    frozen = _session_snapshot(row)
    try:
        confirmed = json.loads(row['confirmed_payload'])
    except (TypeError, ValueError):
        raise SalesWebQueueBlocked('sales_confirmed_payload_invalid', '确认稿冻结内容无效') from None
    if not isinstance(confirmed, dict):
        raise SalesWebQueueBlocked('sales_confirmed_payload_invalid', '确认稿冻结内容无效')
    current=db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
    try: current_version=json.loads(current[0]) if current else ''
    except (TypeError,ValueError): current_version=''
    if not confirmed.get('data_version') or confirmed.get('data_version') != current_version:
        raise SalesWebQueueBlocked('sales_catalog_changed', '商品数据版本已变化，请重新确认草稿')
    date = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    documents = line_document_numbers(profile_id, row['id'], len(confirmed.get('draft', {}).get('lines', ())))
    number = documents[0]
    config = WebImportConfig(Path('/unused/original.xls'), Path('/unused/order.xls'), number,
                             date, profile_id, confirmed['data_version'], owner_id,
                             department, maker, handler, warehouse_id, warehouse_name,
                             'line_document', documents)
    try:
        values, version, confirmed_hash, summary, lines = _values(frozen, config)
    except (TypeError, ValueError, KeyError) as error:
        raise SalesWebQueueBlocked('sales_web_draft_not_importable', f'确认稿尚不能精确导入：{str(error)[:160]}') from None
    if version != row['version'] or confirmed_hash != row['confirmed_hash']:
        raise SalesWebQueueBlocked('sales_web_draft_not_importable', '确认版本与冻结稿不一致')
    draft = confirmed['draft']
    if any(line.get('unit_evidence') not in VALID_UNIT_EVIDENCE or line.get('unit') != CONFIRMED_BASE_UNIT
           for line in draft['lines']):
        raise SalesWebQueueBlocked('sales_web_unit_unverified', '确认稿计量单位来源待核对')
    import_config = {
        'order_date': date.isoformat(), 'profile_id': profile_id, 'account_id': account_id,
        'data_version': confirmed['data_version'], 'owner_id': owner_id,
        'department_name': department, 'maker_name': maker, 'handler_name': handler,
        'warehouse_id': warehouse_id, 'warehouse_name': warehouse_name,
        'template_sha256': template_hash,
    }
    payload = {
        'schema': SCHEMA, 'session_id': row['id'], 'order_number': number,
        'document_mode': 'line_document', 'document_numbers': list(documents),
        'confirmed_version': version, 'confirmed_hash': confirmed_hash,
        'profile_id': profile_id, 'account_id': account_id,
        'customer_name': row['customer_name'], 'customer_binding': binding,
        'fixed_sales_owner_id': owner_id,
        'unit_acceptance': {'base_unit_id': '1', 'unit_name': CONFIRMED_BASE_UNIT,
                            'source': USER_UNIT_EVIDENCE},
        'frozen_session': frozen, 'import_config': import_config,
        'summary': summary, 'line_count': len(lines),
        'line_documents': [{'line_id': line.line_id, 'order_number': line.order_number,
                            'summary': line.summary, 'delivery_date': line.delivery_date}
                           for line in lines],
        'line_unit_evidence': [line.get('unit_evidence') for line in draft['lines']],
    }
    return number, payload


def enqueue_sales_web_operation(db, *, actor_id, profile_id, number, key, payload):
    """Insert into WebOperations tables using the caller's write transaction."""
    fingerprint = sha256(dumps({'kind': 'sales_order_import', 'role': 'sales',
        'profile_id': profile_id, 'target_id': number, 'payload': payload}).encode()).hexdigest()
    if db.execute('SELECT 1 FROM web_operations WHERE actor_id=? AND idempotency_key=?',
                  (actor_id, key)).fetchone():
        raise SalesWebQueueBlocked('idempotency_conflict', '操作标识已用于另一项网页操作')
    if db.execute('''SELECT 1 FROM web_operations WHERE kind='sales_order_import'
        AND profile_id=? AND target_id=?''', (profile_id, number)).fetchone():
        raise SalesWebQueueBlocked('sales_order_already_queued', '此 ERP 单号已进入执行队列，禁止重复提交')
    # Reserve every frozen line number inside the session/queue transaction.
    # Existing v1 intents keep their already occupied single target unchanged.
    db.execute('''CREATE TABLE IF NOT EXISTS sales_web_document_numbers
        (profile_id TEXT NOT NULL, order_number TEXT NOT NULL, session_id TEXT NOT NULL,
         PRIMARY KEY(profile_id,order_number))''')
    for document in payload.get('document_numbers', [number]):
        claimed = db.execute('''SELECT session_id FROM sales_web_document_numbers
            WHERE profile_id=? AND order_number=?''', (profile_id, document)).fetchone()
        if claimed:
            raise SalesWebQueueBlocked('sales_order_number_claimed', 'ERP 明细单号已被占用，禁止重复提交')
        db.execute('INSERT INTO sales_web_document_numbers VALUES (?,?,?)',
                   (profile_id, document, payload['session_id']))
    stamp = now()
    operation_id = uuid.uuid4().hex
    db.execute('''INSERT INTO web_operations VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (operation_id, 'sales_order_import', actor_id, 'sales', profile_id, number,
         key, fingerprint, 'queued', '排队', '等待浏览器执行槽', 0, 4, '{}',
         stamp, '', stamp, '', ''))
    db.execute('INSERT INTO web_operation_payloads VALUES (?,?)', (operation_id, dumps(payload)))
    WebOperations._event(db, operation_id, 'queued', '排队', '等待浏览器执行槽')
    return operation_id
