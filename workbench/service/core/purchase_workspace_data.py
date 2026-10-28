"""Rebuildable purchase index. ERP saleqty is the only shortage authority.

Draft decisions and purchases are stored elsewhere and never overwritten by a
source refresh. No ERP request, print state or logistics write occurs here.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import unicodedata

from .daily_work import _in_unfulfilled_scope
from .store import dumps, timestamp

WAREHOUSE = '29871293290508390'
IDENTITY = ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')


def text(v):
    return '' if v is None else str(v).strip()


def decimal(v):
    if isinstance(v, bool):
        raise ValueError('数量或金额不能是布尔值')
    try:
        result = Decimal(str(v))
    except (InvalidOperation, TypeError):
        raise ValueError('数量或金额格式无效') from None
    if not result.is_finite() or abs(result) > Decimal('1e15'):
        raise ValueError('数量或金额超出范围')
    return result


def number(v):
    return format(decimal(v).normalize(), 'f')


def normalized(v):
    return unicodedata.normalize('NFKC', text(v)).casefold()


def natural(v):
    return tuple((0, Decimal(p)) if re.fullmatch(r'\d+(?:\.\d+)?', p) else (1, p)
                 for p in re.split(r'(\d+(?:\.\d+)?)', normalized(v)) if p)


def identity(row):
    parts = [text(row.get(k)) for k in IDENTITY]
    if not all(parts[i] for i in (0, 1, 2, 4)):
        raise ValueError('采购商品缺少账套、仓库、商品或单位身份')
    return dumps(parts)


def item_id(row):
    return sha256(identity(row).encode()).hexdigest()[:32]


def supplier_key(profile, erp_id):
    return sha256(dumps([profile, erp_id]).encode()).hexdigest()[:32]


def setting(db, key, default=''):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return row[0]


def source_token(db):
    return dumps([setting(db, 'data_version'), setting(db, 'data_source_updated_at')])


def classification(row):
    name = text(row.get('product_name'))
    if '螺母' in name:
        category = '螺母'
    elif any(s in name for s in ('垫圈', '垫片', '弹垫', '平垫')):
        category = '垫圈'
    elif any(s in name for s in ('挡圈', '卡簧')):
        category = '挡圈'
    elif any(s in name for s in ('螺栓', '螺钉', '螺丝', '紧定')):
        category = '螺栓／螺钉'
    elif any(s in name for s in ('螺柱', '牙条', '螺杆')):
        category = '螺柱／牙条'
    elif '销' in name:
        category = '销'
    elif '键' in name:
        category = '键'
    else:
        category = '其他／待分类'
    standard = re.search(r'(?:GB\s*/?\s*T?|DIN|ISO|ANSI|ASME|JIS)\s*[-/]?\s*\d+(?:\.\d+)?', name, re.I)
    kind = '非标件' if '非标' in name else '标准件' if standard else '待分类'
    return {'category': category, 'kind': kind,
            'standard': text(row.get('standard_no')) or (standard.group(0) if standard else '')}


def ensure(db):
    statements = [
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_index(
            id TEXT PRIMARY KEY,identity TEXT NOT NULL UNIQUE,profile_id TEXT NOT NULL,
            product_code TEXT NOT NULL,shortage TEXT NOT NULL,payload TEXT NOT NULL,
            source_token TEXT NOT NULL,active INTEGER NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS purchase_workspace_active ON purchase_workspace_index(active,profile_id)',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_suppliers(
            id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,erp_id TEXT NOT NULL,name TEXT NOT NULL,
            active INTEGER NOT NULL,history_count INTEGER NOT NULL,UNIQUE(profile_id,erp_id))''',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_history(
            id TEXT PRIMARY KEY,item_id TEXT NOT NULL,supplier_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,payload TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS purchase_workspace_history_item ON purchase_workspace_history(item_id,occurred_at DESC)',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_decisions(
            item_id TEXT PRIMARY KEY,version INTEGER NOT NULL DEFAULT 1,origin TEXT NOT NULL,
            fixed_supplier TEXT NOT NULL DEFAULT '',review_ack INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_allocations(
            id TEXT PRIMARY KEY,item_id TEXT NOT NULL,supplier_id TEXT NOT NULL,quantity TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'draft',origin TEXT NOT NULL,details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS purchase_workspace_allocation_item ON purchase_workspace_allocations(item_id,state)',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_actions(
            actor_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
            result TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(actor_id,idempotency_key))''',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,actor_id TEXT NOT NULL,
            action TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_refresh(
            id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,requested_at TEXT NOT NULL,status TEXT NOT NULL,
            job_id TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_quotes(
            id TEXT PRIMARY KEY,item_id TEXT NOT NULL,supplier_id TEXT NOT NULL,
            payload TEXT NOT NULL,actor_id TEXT NOT NULL,created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS purchase_workspace_imports(
            id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,kind TEXT NOT NULL,source_hash TEXT NOT NULL,
            filename TEXT NOT NULL,rows_json TEXT NOT NULL,created_at TEXT NOT NULL)''',
    ]
    for sql in statements:
        db.execute(sql)


def load_sale_facts(db):
    """Verified sale prices published with the current order snapshot. No ERP call."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='wms_sale_price_facts'").fetchone():
        return {}
    version = text(setting(db, 'data_version', ''))
    sql = '''SELECT order_no, line_id, quantity, inclusive_amount, unit, tax_basis
             FROM wms_sale_price_facts'''
    params = ()
    if version:
        sql += ' WHERE data_version=?'
        params = (version,)
    facts = {}
    for row in db.execute(sql, params):
        facts[(text(row['order_no']), text(row['line_id']))] = dict(row)
    return facts


def sale_reference(order, fact):
    """Unit price for one unfulfilled sales line. Missing fact means no sale price."""
    if fact:
        quantity = decimal(fact.get('quantity') or 0)
        amount = decimal(fact.get('inclusive_amount') or 0)
        if quantity > 0 and amount >= 0:
            basis = text(fact.get('tax_basis'))
            return {
                'sale_unit_price': number(amount / quantity),
                'sale_amount': number(amount),
                'sale_tax_basis': '含税' if basis == 'tax_inclusive' else '未税' if basis == 'tax_exclusive' else '税价待核对',
                'sale_unit': text(fact.get('unit')) or '支',
                'sale_price_source': 'wms_inclusive_amount',
            }
    explicit = text(order.get('sale_unit_price'))
    if explicit:
        return {
            'sale_unit_price': number(explicit),
            'sale_amount': text(order.get('sale_amount')),
            'sale_tax_basis': text(order.get('sale_tax_basis')) or '税价待核对',
            'sale_unit': text(order.get('sale_unit')) or text(order.get('unit_name')) or '支',
            'sale_price_source': 'order_snapshot',
        }
    return {}


def refresh_index(store, force=False):
    """Build outside the write lock, then atomically swap this published version."""
    with store.read_snapshot(), store.connect() as db:
        token = source_token(db)
        saved = db.execute("SELECT value FROM purchase_workspace_meta WHERE key='source_token'").fetchone()
        if saved and saved[0] == token and not force:
            return {'changed': False}
        rows = {kind: [json.loads(r[0]) for r in db.execute('SELECT payload FROM records WHERE kind=?', (kind,))]
                for kind in ('inventory', 'orders', 'purchases')}
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='sales_erp_customers'").fetchone()
        directory = [dict(r) for r in db.execute('SELECT * FROM sales_erp_customers')] if table else []
        coverage = setting(db, 'data_query_scope', {})
        sale_facts = load_sale_facts(db)
    now = timestamp()
    suppliers = {}
    for r in directory:
        k = supplier_key(r['profile_id'], r['erp_id'])
        suppliers[k] = (k, r['profile_id'], r['erp_id'], r['customer_name'], r['active'], 0)
    histories = defaultdict(list)
    seen_history = set()
    for r in rows['purchases']:
        if (r.get('document_type') != '进货入库单' or r.get('redold') or r.get('redword') or
                not r.get('party_id') or r.get('party_id') == 'internal' or r.get('warehouse_id') != WAREHOUSE):
            continue
        qty = decimal(r.get('quantity', 0))
        if qty <= 0:
            continue
        ident = item_id(r)
        sid = supplier_key(r['profile_id'], r['party_id'])
        if sid not in suppliers:
            suppliers[sid] = (sid, r['profile_id'], r['party_id'], text(r.get('party')), 1, 0)
        s = suppliers[sid]
        suppliers[sid] = (*s[:5], s[5] + 1)
        evidence = {k: r.get(k, '') for k in ('document_no', 'source_bill_id', 'source_line_id', 'document_date', 'posted_at')}
        if not evidence['source_bill_id'] or not evidence['source_line_id']:
            continue
        hid = sha256(dumps([r['profile_id'], evidence['source_bill_id'], evidence['source_line_id']]).encode()).hexdigest()
        if hid in seen_history:
            raise ValueError('历史进货明细重复，采购索引未发布')
        seen_history.add(hid)
        amount = decimal(r.get('amount', 0))
        evidence.update(supplier_id=sid, supplier_name=s[3], quantity=number(qty), amount=number(amount),
                        unit_price=number(amount / qty) if amount > 0 else '', tax_basis='历史金额口径待核对')
        histories[ident].append((hid, ident, sid, text(r.get('posted_at') or r.get('document_date')), dumps(evidence)))
    demands = defaultdict(list)
    shadow = defaultdict(lambda: Decimal(0))
    for r in rows['orders']:
        if r.get('warehouse_id') != WAREHOUSE:
            continue
        ident = item_id(r)
        qty = decimal(r.get('quantity', 0))
        if _in_unfulfilled_scope(r):
            demand = {k: r.get(k, '') for k in
                ('order_no', 'line_id', 'customer_name', 'customer_source', 'quantity', 'delivery_date', 'order_date')}
            blob = ''.join(text(r.get(k)) for k in ('summary', 'note', 'system_note', 'product_name', 'spec'))
            demand['summary'] = text(r.get('summary'))[:200]
            demand['rework'] = '1' if '改制' in blob else ''
            demand.update(sale_reference(r, sale_facts.get((text(demand.get('order_no')), text(demand.get('line_id'))))))
            demands[ident].append(demand)
        elif r.get('shadow_demand'):
            shadow[ident] += qty
    prepared = []
    seen = set()
    for r in rows['inventory']:
        if r.get('warehouse_id') != WAREHOUSE:
            continue
        ident = item_id(r)
        if ident in seen:
            raise ValueError('库存商品身份重复，采购索引未发布')
        seen.add(ident)
        physical, available = decimal(r['stock_quantity']), decimal(r['available_stock'])
        shortage = max(-available, Decimal(0))
        ds = demands[ident]
        current = sum((decimal(d['quantity']) for d in ds), Decimal(0))
        net = available - physical + current + shadow[ident]
        history = sorted(histories[ident], key=lambda x: (x[3], x[0]), reverse=True)
        valid = [h for h in history if suppliers[h[2]][4]]
        newest = valid[0] if valid else None
        tied = newest and len({h[2] for h in valid if h[3] == newest[3]}) > 1
        supplier = newest[2] if newest and not tied else ''
        due = sorted(text(d['delivery_date']) for d in ds if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text(d['delivery_date'])))
        review = []
        if not text(r.get('sku_id')):
            review.append('商品 SKU 身份未建立，不能下单')
        if not ds:
            review.append('没有可对应的未交客户订单，需求待核对')
        if physical < 0:
            review.append('实物库存为负，请核对库存账')
        if r.get('is_deleted') or r.get('is_stopped') or r.get('status') == '停用':
            review.append('商品已停用或删除')
        payload = {k: r.get(k, '') for k in (*IDENTITY, 'sku', 'product_name', 'spec', 'model', 'material',
                    'location', 'has_image', 'stock_observed_at')}
        payload.update(classification(r))
        payload.update(id=ident, unit='支' if r.get('base_unit') == '1' else text(r.get('unit_name')),
            shortage=number(shortage), physical=number(physical), available=number(available),
            demand=number(current), shadow_reserved=number(shadow[ident]),
            inferred_transit=number(max(net, Decimal(0))), inferred_other_reserved=number(max(-net, Decimal(0))),
            customer_count=len({d['customer_name'] for d in ds}), order_line_count=len(ds),
            customers=sorted({text(d['customer_name']) for d in ds if d['customer_name']}),
            due_date=due[0] if due else '', demands=ds, review_reasons=review,
            suggested_supplier=supplier, suggestion=json.loads(newest[4]) if newest and not tied else None,
            history_count=len(history), source_token=token)
        prepared.append((ident, identity(r), r['profile_id'], r.get('sku', ''), number(shortage), dumps(payload), token, int(shortage > 0)))
    with store.connect(write=True) as db:
        if source_token(db) != token:
            return {'changed': False, 'retry': True}
        # A vanished source never deletes the operator's bought/draft record.
        for old in db.execute("SELECT * FROM purchase_workspace_index WHERE id IN (SELECT item_id FROM purchase_workspace_allocations WHERE state!='cancelled')"):
            if old['id'] in seen:
                continue
            p = json.loads(old['payload'])
            p.update(source_missing=True, shortage='0', source_token=token,
                     review_reasons=['库存来源暂时缺失，请核对；原采购记录保留'])
            prepared.append((old['id'], old['identity'], old['profile_id'], old['product_code'], '0', dumps(p), token, 0))
        db.execute('DELETE FROM purchase_workspace_index')
        db.executemany('INSERT INTO purchase_workspace_index VALUES (?,?,?,?,?,?,?,?)', prepared)
        db.execute('DELETE FROM purchase_workspace_history')
        db.executemany('INSERT INTO purchase_workspace_history VALUES (?,?,?,?,?)', [h for hs in histories.values() for h in hs])
        db.execute('UPDATE purchase_workspace_suppliers SET active=0,history_count=0')
        db.executemany('''INSERT INTO purchase_workspace_suppliers VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,active=excluded.active,history_count=excluded.history_count''', list(suppliers.values()))
        for row in prepared:
            ident = row[0]
            old = db.execute('SELECT * FROM purchase_workspace_decisions WHERE item_id=?', (ident,)).fetchone()
            if old or not row[7]:
                continue
            payload = json.loads(row[5])
            db.execute('INSERT INTO purchase_workspace_decisions(item_id,origin,updated_at) VALUES (?,?,?)', (ident, 'system', now))
            if payload['suggested_supplier']:
                db.execute('''INSERT INTO purchase_workspace_allocations
                    VALUES (?,?,?,?,?,?,?,?,?)''', (sha256(('initial:'+ident).encode()).hexdigest()[:32], ident,
                    payload['suggested_supplier'], row[4], 'draft', 'system', dumps({'basis': payload['suggestion']}), now, now))
        for k, v in [('source_token', token), ('indexed_at', now), ('history_coverage', dumps(coverage))]:
            db.execute('INSERT OR REPLACE INTO purchase_workspace_meta VALUES (?,?)', (k, v))
        previous = db.execute("SELECT value FROM purchase_workspace_meta WHERE key='purchase_index_generation'").fetchone()
        generation = str((int(previous[0]) if previous and str(previous[0]).isdigit() else 0) + 1)
        db.execute("INSERT OR REPLACE INTO purchase_workspace_meta VALUES ('purchase_index_generation', ?)", (generation,))
        db.execute("DELETE FROM purchase_workspace_meta WHERE key='customer_board_cache'")
        db.execute("DELETE FROM purchase_workspace_meta WHERE key='index_error'")
    return {'changed': True, 'items': len(prepared), 'shortage_items': sum(r[7] for r in prepared)}
