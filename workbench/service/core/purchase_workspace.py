"""Shared, role-scoped procurement workspace; no browser or automatic payment."""
from collections import Counter, defaultdict
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
import hmac
import json
import re
import uuid
import logging

from flask import Blueprint, g, jsonify, request, send_from_directory, send_file
from .daily_work import _is_admin, _roles
from .store import dumps, timestamp
from .purchase_notes import (REWORK_CARD, card_name_for_demand, default_purchase_note,
    demand_on_rework_card, item_is_rework)
from .purchase_orders import next_auto_order_no, next_platform_line_no, shanghai_now
from .purchase_workspace_data import (ensure, refresh_index, source_token, setting, decimal,
    number, normalized, natural, text)

BASE = '/api/v2/purchase-workspace'
COOKIE = 'ul_purchase_session'


class PurchaseConflict(ValueError):
    def __init__(self, message, code='purchase_conflict', status=409):
        super().__init__(message)
        self.code, self.status = code, status


def customer_board(items, only_names=None):
    """Customers who still have open demand on a product ERP still shows short.

    ``items`` / ``demand`` stay the ERP shortage view. ``pending_*`` drops as soon
    as a local order covers the shortage, before the next ERP refresh.
    """
    only = set(only_names) if only_names is not None else None
    board = {}
    for item in items:
        if decimal(item['shortage']) <= 0:
            continue
        ordered = sum((decimal(a['quantity']) for a in item.get('allocations') or []
                       if a.get('state') in {'ordered', 'awaiting_audit'}), Decimal(0))
        pending = decimal(item['shortage']) - ordered > 0
        allocations = item.get('allocations') or []
        flags = {
            'inquired': any(a.get('progress') == 'inquired' for a in allocations),
            'ordered': any(a.get('state') in {'ordered', 'awaiting_audit'} and not (a.get('details') or {}).get('arrived_at')
                           for a in allocations),
            'arrived': any((a.get('details') or {}).get('arrived_at') for a in allocations),
        }
        seen = set()
        for line in item.get('demands') or []:
            name = card_name_for_demand(line, item)
            if not name or (only is not None and name not in only):
                continue
            slot = board.setdefault(name, {
                'name': name, 'kind': 'rework' if name == REWORK_CARD else 'customer',
                'items': 0, 'demand': Decimal(0), 'unassigned_items': 0,
                'pending_items': 0, 'pending_pieces': Decimal(0), 'inquired_items': 0,
                'ordered_items': 0, 'arrived_items': 0, 'earliest_due': ''})
            slot['demand'] += decimal(line.get('quantity') or 0)
            if pending:
                slot['pending_pieces'] += decimal(line.get('quantity') or 0)
            due = text(line.get('delivery_date'))
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', due) and (not slot['earliest_due'] or due < slot['earliest_due']):
                slot['earliest_due'] = due
            if name in seen:
                continue
            seen.add(name)
            slot['items'] += 1
            if decimal(item['unassigned']) > 0:
                slot['unassigned_items'] += 1
            if pending:
                slot['pending_items'] += 1
            if flags['inquired']:
                slot['inquired_items'] += 1
            if flags['ordered']:
                slot['ordered_items'] += 1
            if flags['arrived']:
                slot['arrived_items'] += 1
    rows = [{**slot, 'demand': number(slot['demand']), 'pending_pieces': number(slot['pending_pieces'])}
            for slot in board.values()]
    rows.sort(key=lambda row: (row['name'] != REWORK_CARD, -row['pending_items'], -row['items'], natural(row['name'])))
    return rows


def _generation(db):
    row = db.execute("SELECT value FROM purchase_workspace_meta WHERE key='purchase_index_generation'").fetchone()
    return row[0] if row else '0'


def read_customer_cache(db, token):
    row = db.execute("SELECT value FROM purchase_workspace_meta WHERE key='customer_board_cache'").fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if data.get('source_token') != token or data.get('generation') != _generation(db):
        return None
    return data


def write_customer_cache(db, token, rows, last_recomputed):
    payload = {'source_token': token, 'generation': _generation(db), 'rows': rows,
               'last_recomputed': last_recomputed}
    db.execute("INSERT OR REPLACE INTO purchase_workspace_meta VALUES ('customer_board_cache', ?)", (dumps(payload),))


def merge_customer_rows(previous, fresh, names):
    names = set(names)
    rows = [row for row in previous if row['name'] not in names]
    rows.extend(fresh)
    rows.sort(key=lambda row: (row.get('name') != REWORK_CARD, -row['pending_items'], -row['items'], natural(row['name'])))
    return rows


def annotate_allocation(row):
    details = row['details'] if isinstance(row['details'], dict) else {}
    state = row['state']
    if state == 'draft' and details.get('inquired_at'):
        progress = 'inquired'
    elif state in {'ordered', 'awaiting_audit'} and details.get('arrived_at'):
        progress = 'arrived'
    else:
        progress = state
    amount = decimal(details['amount']) if details.get('amount') not in (None, '') else Decimal(0)
    settled = decimal(details['settled_amount']) if details.get('settled_amount') not in (None, '') else Decimal(0)
    if settled < 0:
        settled = Decimal(0)
    if amount > 0 and settled > amount:
        settled = amount
    row['progress'] = progress
    row['settled_amount'] = number(settled) if details.get('settled_amount') not in (None, '') else ''
    row['unsettled_amount'] = number(amount - settled) if amount > 0 else ''
    row['partial_arrival'] = bool(details.get('arrived_at') and decimal(details.get('arrived_quantity') or 0) < decimal(row['quantity']))
    return row


def item_matches_card(item, customer):
    customer = text(customer)
    if not customer:
        return True
    if customer == REWORK_CARD:
        return any(demand_on_rework_card(line, item) for line in item.get('demands') or [])
    return any(text(line.get('customer_name')) == customer and not demand_on_rework_card(line, item)
               for line in item.get('demands') or [])


def customer_need(item, customer):
    if customer == REWORK_CARD:
        lines = [line for line in item.get('demands') or [] if demand_on_rework_card(line, item)]
    else:
        lines = [line for line in item.get('demands') or []
                 if text(line.get('customer_name')) == customer and not demand_on_rework_card(line, item)]
    dues = sorted(text(line.get('delivery_date')) for line in lines
                  if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text(line.get('delivery_date'))))
    orders = []
    for line in lines:
        order_no = text(line.get('order_no'))
        if order_no and order_no not in orders:
            orders.append(order_no)
    quantity = sum((decimal(line.get('quantity') or 0) for line in lines), Decimal(0))
    return {'customer_demand': number(quantity), 'customer_due': dues[0] if dues else '',
            'customer_orders': orders[:8], 'customer_line_count': len(lines),
            'sale_prices': sale_price_summary(lines)}


def sale_price_summary(lines):
    """Reference prices for the lines on this customer or 改制 card. Shortage is untouched."""
    priced = []
    for line in lines:
        price = text(line.get('sale_unit_price'))
        row = {'order_no': text(line.get('order_no')), 'line_id': text(line.get('line_id')),
               'order_date': text(line.get('order_date')), 'quantity': text(line.get('quantity')),
               'unit_price': price, 'tax_basis': text(line.get('sale_tax_basis')),
               'unit': text(line.get('sale_unit')) or '支'}
        if price:
            priced.append(row)
    if not priced:
        return {'status': 'missing', 'lines': [], 'priced_count': 0, 'line_count': len(lines), 'missing_count': len(lines)}
    priced.sort(key=lambda row: (row['order_date'], row['order_no'], row['line_id']), reverse=True)
    values = [decimal(row['unit_price']) for row in priced]
    bases = sorted({row['tax_basis'] or '税价待核对' for row in priced})
    units = sorted({row['unit'] or '支' for row in priced})
    return {'status': 'ok', 'lines': priced, 'priced_count': len(priced), 'line_count': len(lines),
            'missing_count': len(lines) - len(priced), 'min': number(min(values)), 'max': number(max(values)),
            'latest': priced[0]['unit_price'], 'latest_order_no': priced[0]['order_no'],
            'tax_basis': bases[0] if len(bases) == 1 else '多口径',
            'unit': units[0] if len(units) == 1 else ''}


def history_preview(db, idents):
    preview = defaultdict(list)
    if not idents:
        return preview
    marks = ','.join('?' * len(idents))
    for row in db.execute(
            f'''SELECT item_id, occurred_at, payload FROM purchase_workspace_history
                WHERE item_id IN ({marks}) ORDER BY occurred_at DESC, id''', idents):
        bucket = preview[row['item_id']]
        if len(bucket) >= 3:
            continue
        payload = json.loads(row['payload'])
        bucket.append({'supplier_name': text(payload.get('supplier_name')),
                       'occurred_at': text(row['occurred_at'] or payload.get('posted_at') or payload.get('document_date')),
                       'quantity': text(payload.get('quantity')), 'unit_price': text(payload.get('unit_price')),
                       'tax_basis': text(payload.get('tax_basis'))})
    return preview


def authorize(user):
    if not isinstance(user, dict) or not (_is_admin(user) or 'purchase' in _roles(user)):
        raise PurchaseConflict('需要采购岗位或管理员权限', 'purchase_role_required', 403)


def is_request(path):
    return path.startswith(BASE + '/')


def csrf(token):
    return sha256(('ul-purchase-csrf:' + token).encode()).hexdigest()


def same_origin(app):
    return request.headers.get('Origin', '').rstrip('/') == app.config.get(
        'PURCHASE_WEB_ORIGIN', app.config.get('SALES_WEB_ORIGIN', 'https://ul.yingyinghome.com:8443')).rstrip('/')


class PurchaseWorkspace:
    def __init__(self, store, submission=None):
        self.store = store
        self.stop_event = Event()
        with store.connect(write=True) as db:
            ensure(db)
            from .purchase_workspace_files import schema
            schema(db)
        if submission is None:
            from .purchase_submission import PurchaseSubmission
            submission = PurchaseSubmission(store)
        self.submission = submission
        self.rebuild()

    def erp_submit_status(self):
        """Draft ERP submit stays off unless both env flags and a verified adapter are present."""
        enabled = self.submission._writing_allowed()
        ready = bool(enabled and self.submission.ready())
        cap = getattr(self.submission.adapter, 'capabilities', None)
        readback = bool(getattr(cap, 'readback_contract_verified', False) and getattr(cap, 'complete_unique_lookup', False))
        if ready and not readback:
            message = '进货订单写入已开放。只调用 erp.purchaseorder.add，不付款、不审核。含税成交价先除以 1.13，未税成交价原样作为未税折前单价，税率 13%，单据金额为价税合计。官方没有草稿字段，也没有按单号回查。写入后请到管家婆按单据编号人工核对未审核。'
        elif ready:
            message = '采购订单草稿试写已开放。只创建未审核草稿，不付款、不审核；结果不明时只回查。'
        elif enabled:
            message = '采购 ERP 草稿开关已打开，但写入和回查合同尚未验收，不会向 ERP 发送。'
        else:
            message = '采购 API 合同验收前，实际成交记录可保存，ERP 提交暂未开放'
        return {'erp_submit_enabled': enabled, 'erp_draft_ready': ready, 'erp_readback_available': readback,
                'browser_enabled': False, 'message': message}

    def submit_erp_draft(self, user, body):
        authorize(user)
        return self.submission.submit(user, body)

    def rebuild(self, force=False):
        try:
            return refresh_index(self.store, force=force)
        except (ValueError, KeyError) as error:
            logging.getLogger(__name__).warning('Purchase index retained: %s', error)
            with self.store.connect(write=True) as db:
                db.execute("INSERT OR IGNORE INTO purchase_workspace_meta VALUES ('history_coverage','{}')")
                db.execute("INSERT OR REPLACE INTO purchase_workspace_meta VALUES ('index_error',?)", (str(error),))
            return {'changed': False, 'error': str(error)}

    def _source(self, db):
        token = source_token(db)
        row = db.execute("SELECT value FROM purchase_workspace_meta WHERE key='source_token'").fetchone()
        return token, bool(row and row[0] == token)

    def suppliers(self, db):
        return [dict(r) for r in db.execute('SELECT * FROM purchase_workspace_suppliers WHERE active=1 ORDER BY history_count DESC,name,id')]

    def _items(self, db):
        allocations = defaultdict(list)
        suppliers = {r['id']: dict(r) for r in db.execute('SELECT * FROM purchase_workspace_suppliers')}
        for row in db.execute("SELECT * FROM purchase_workspace_allocations WHERE state!='cancelled' ORDER BY created_at,id"):
            r = dict(row)
            r['details'] = json.loads(r['details'])
            annotate_allocation(r)
            s = suppliers.get(r['supplier_id'], {})
            r['supplier_name'] = s.get('name', '供应商待核对')
            r['supplier_active'] = bool(s.get('active'))
            allocations[r['item_id']].append(r)
        decisions = {r['item_id']: dict(r) for r in db.execute('SELECT * FROM purchase_workspace_decisions')}
        items = []
        for row in db.execute("SELECT * FROM purchase_workspace_index WHERE active=1 OR id IN (SELECT item_id FROM purchase_workspace_allocations WHERE state!='cancelled')"):
            item = json.loads(row['payload'])
            a = allocations[item['id']]
            if not row['active'] and not a:
                continue
            d = decisions.get(item['id'], {})
            item.update(version=d.get('version', 0), decision=d.get('origin', 'system'),
                fixed_supplier=d.get('fixed_supplier', ''), review_ack=d.get('review_ack') == dumps(item['review_reasons']),
                allocations=a)
            for field in ('category', 'kind'):
                if d.get(field):
                    item[field] = d[field]
            arranged = sum((decimal(x['quantity']) for x in a if x['state'] != 'erp_counted'), Decimal(0))
            item.update(arranged=number(arranged), unassigned=number(max(Decimal(0), decimal(item['shortage'])-arranged)),
                overallocated=arranged > decimal(item['shortage']))
            items.append(item)
        return items

    def listing(self, user, args):
        authorize(user)
        page, size = max(1, int(args.get('page', 1))), min(100, max(1, int(args.get('page_size', 50))))
        with self.store.read_snapshot(), self.store.connect() as db:
            token, current = self._source(db)
            items = self._items(db)
            summaries = defaultdict(lambda: {'count': 0, 'quantity': Decimal(0), 'ordered': 0})
            counts = {'pool': 0, 'drafts': 0, 'ordered': 0, 'awaiting_audit': 0, 'review': 0,
                      'inquired': 0, 'arrived': 0}
            for x in items:
                counts['pool'] += decimal(x['shortage']) > 0
                counts['drafts'] += any(a['state'] == 'draft' for a in x['allocations'])
                counts['ordered'] += any(a['state'] != 'draft' for a in x['allocations'])
                counts['awaiting_audit'] += sum(a['state'] == 'awaiting_audit' for a in x['allocations'])
                counts['review'] += bool(x['review_reasons'] or x['overallocated'])
                counts['inquired'] += any(a.get('progress') == 'inquired' for a in x['allocations'])
                counts['arrived'] += any(a.get('progress') == 'arrived' for a in x['allocations'])
                for a in x['allocations']:
                    s = summaries[a['supplier_id']]
                    s['count'] += 1
                    s['quantity'] += decimal(a['quantity'])
                    s['ordered'] += a['state'] != 'draft'
            facets = {k: sorted({text(x[k]) for x in items if x[k]}) for k in ('category', 'kind', 'standard', 'model', 'material')}
            facets['customers'] = sorted({c for x in items for c in x['customers']})
            q, view = normalized(args.get('q')), args.get('view', 'pool')
            matched = []
            for x in items:
                if view == 'pool' and decimal(x['shortage']) <= 0:
                    continue
                if view == 'drafts' and not any(a['state'] == 'draft' for a in x['allocations']):
                    continue
                if view == 'ordered' and not any(a['state'] != 'draft' for a in x['allocations']):
                    continue
                if view == 'arrived' and not any(a.get('progress') == 'arrived' for a in x['allocations']):
                    continue
                if view == 'reconcile':
                    continue
                progress = text(args.get('progress'))
                if progress and not any(a.get('progress') == progress for a in x['allocations']):
                    continue
                if args.get('unassigned') == '1' and decimal(x['unassigned']) <= 0:
                    continue
                if args.get('review') == '1' and not (x['review_reasons'] or x['overallocated']):
                    continue
                if q and q not in normalized(' '.join(text(x.get(k)) for k in ('sku', 'product_name', 'spec', 'model', 'standard')) + ' ' +
                        ' '.join(x['customers']) + ' ' + ' '.join(text(d['order_no']) for d in x['demands'])):
                    continue
                if any(args.get(k) and args[k] != x[k] for k in ('category', 'kind', 'standard', 'model', 'material')):
                    continue
                if args.get('customer') and not item_matches_card(x, args.get('customer')):
                    continue
                if args.get('supplier') and not any(a['supplier_id'] == args['supplier'] for a in x['allocations']):
                    continue
                if args.get('image') in {'yes', 'no'} and bool(x['has_image']) != (args['image'] == 'yes'):
                    continue
                matched.append(x)
            sort = args.get('sort', 'product')
            if sort == 'due':
                matched.sort(key=lambda x: (x['due_date'] or '9999', natural(x['sku'])))
            elif sort == 'shortage':
                matched.sort(key=lambda x: (-decimal(x['shortage']), natural(x['sku'])))
            else:
                matched.sort(key=lambda x: (natural(x['category']), natural(x['standard']), natural(x['model']), natural(x['spec']), natural(x['sku'])))
            total = len(matched)
            customer = text(args.get('customer'))
            page_items = matched[(page-1)*size:page*size]
            preview = history_preview(db, [x['id'] for x in page_items])
            output = []
            cached_board = read_customer_cache(db, token)
            board = cached_board['rows'] if cached_board else customer_board(items)
            for x in page_items:
                slim = {k: v for k, v in x.items() if k not in {'demands', 'customers'}}
                slim['history_preview'] = preview.get(x['id'], [])
                slim['rework'] = '1' if customer == REWORK_CARD or item_is_rework(x, customer) else ''
                if customer:
                    slim.update(customer_need(x, customer))
                output.append(slim)
            payload = {'items': output, 'total': total, 'page': page, 'page_size': size,
                'customer_board': board, 'customer_cache': 'hit' if cached_board else 'miss',
                'source_token': token, 'source_current': current, 'source_updated_at': setting(db, 'data_source_updated_at'),
                'counts': counts, 'facets': facets, 'suppliers': [{**s, **{
                    **summaries[s['id']], 'quantity': number(summaries[s['id']]['quantity'])}} for s in self.suppliers(db)],
                'refresh': self.refresh_status(db),
                'history_coverage': json.loads(db.execute("SELECT value FROM purchase_workspace_meta WHERE key='history_coverage'").fetchone()[0])}
        if not cached_board:
            with self.store.connect(write=True) as db:
                if not read_customer_cache(db, token):
                    write_customer_cache(db, token, board, '*')
        return payload

    def detail(self, user, ident):
        authorize(user)
        with self.store.read_snapshot(), self.store.connect() as db:
            item = next((x for x in self._items(db) if x['id'] == ident), None)
            if not item:
                raise PurchaseConflict('采购商品已不在当前范围，请刷新', 'not_found', 404)
            item['history'] = [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM purchase_workspace_history WHERE item_id=? ORDER BY occurred_at DESC,id LIMIT 100', (ident,))]
            item['quotes'] = [{**dict(r), 'payload': json.loads(r['payload'])} for r in db.execute(
                'SELECT * FROM purchase_workspace_quotes WHERE item_id=? ORDER BY created_at DESC LIMIT 100', (ident,))]
            item['events'] = [{**dict(r), 'details': json.loads(r['details'])} for r in db.execute(
                'SELECT * FROM purchase_workspace_events WHERE item_id=? ORDER BY seq DESC LIMIT 50', (ident,))]
            return item

    def _supplier(self, db, sid, profile):
        s = db.execute('SELECT * FROM purchase_workspace_suppliers WHERE id=? AND profile_id=? AND active=1', (sid, profile)).fetchone()
        if not s:
            raise PurchaseConflict('供应商身份已变化或不属于当前账套', 'supplier_review')
        return s

    def mutate(self, user, body):
        authorize(user)
        if not isinstance(body, dict):
            raise ValueError('请提供操作内容')
        key = text(body.get('idempotency_key'))
        if not 8 <= len(key) <= 100:
            raise ValueError('操作标识无效')
        action = body.get('action')
        if action not in {'assign', 'return', 'split', 'fixed_supplier', 'review_ack', 'classify', 'suggest',
                          'record_purchase', 'quote', 'adopt_quote', 'confirm_erp_counted',
                          'mark_inquired', 'mark_arrived', 'mark_settled'}:
            raise ValueError('不支持的采购操作')
        targets = body.get('items')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
            raise ValueError('每次请选择 1–100 件商品')
        if len({x.get('id') for x in targets if isinstance(x, dict)}) != len(targets):
            raise ValueError('选择中有重复或无效商品')
        digest = sha256(dumps(body).encode()).hexdigest()
        actor = user['id']
        with self.store.connect(write=True) as db:
            old = db.execute('SELECT * FROM purchase_workspace_actions WHERE actor_id=? AND idempotency_key=?', (actor, key)).fetchone()
            if old:
                if old['fingerprint'] != digest:
                    raise PurchaseConflict('原操作标识不能用于另一批内容', 'idempotency_conflict')
                return json.loads(old['result'])
            if setting(db, 'restoration_hold', False):
                raise PurchaseConflict('备份恢复核对期间不能修改采购计划', 'restoration_hold')
            token, current = self._source(db)
            if not current or body.get('source_token') != token:
                raise PurchaseConflict('库存数据已更新，请核对新数量后重试；输入仍保留', 'purchase_source_changed')
            lines, names, shared_order = [], set(), ''
            for target in targets:
                row = db.execute('SELECT * FROM purchase_workspace_index WHERE id=?', (target['id'],)).fetchone()
                decision = db.execute('SELECT * FROM purchase_workspace_decisions WHERE item_id=?', (target['id'],)).fetchone()
                if not row or not decision or decision['version'] != target.get('version'):
                    raise PurchaseConflict('其他人已调整这件商品，请刷新后核对；本批未修改', 'purchase_version_changed')
                if action == 'record_purchase':
                    explicit = text(target.get('platform_order_no') or '') or text(body.get('platform_order_no'))
                    if explicit:
                        target = {**target, 'platform_order_no': explicit}
                    else:
                        if not shared_order:
                            shared_order = next_auto_order_no(db, shanghai_now())
                        target = {**target, 'platform_order_no': shared_order, '_order_auto': True}
                info = self._change(db, user, action, row, decision, target, body)
                if info:
                    lines.append(info)
                item = json.loads(row['payload'])
                for demand in item.get('demands') or []:
                    card = card_name_for_demand(demand, item)
                    if card:
                        names.add(card)
            fresh_items = self._items(db)
            cached = read_customer_cache(db, token)
            if cached and names:
                board = merge_customer_rows(cached['rows'], customer_board(fresh_items, names), names)
                write_customer_cache(db, token, board, sorted(names))
            else:
                board = customer_board(fresh_items)
                write_customer_cache(db, token, board, '*')
            result = {'changed': len(targets), 'action': action, 'customer_board': board}
            if lines:
                result['lines'] = lines
            db.execute('INSERT INTO purchase_workspace_actions VALUES (?,?,?,?,?)', (actor, key, digest, dumps(result), timestamp()))
            return result

    def _change(self, db, user, action, row, decision, target, body):
        ident, now, line_result = row['id'], timestamp(), None
        item = json.loads(row['payload'])
        supplier = text(target.get('supplier_id', body.get('supplier_id')))
        if action in {'assign', 'split', 'fixed_supplier', 'quote'}:
            self._supplier(db, supplier, row['profile_id'])
        allocations = [dict(r) for r in db.execute("SELECT * FROM purchase_workspace_allocations WHERE item_id=? AND state!='cancelled'", (ident,))]
        locked = sum((decimal(a['quantity']) for a in allocations if a['state'] not in {'draft', 'erp_counted'}), Decimal(0))
        if action in {'assign', 'return', 'suggest'}:
            if action == 'suggest':
                supplier = decision['fixed_supplier'] or item['suggested_supplier']
                if supplier:
                    self._supplier(db, supplier, row['profile_id'])
            db.execute("UPDATE purchase_workspace_allocations SET state='cancelled',updated_at=? WHERE item_id=? AND state='draft'", (now, ident))
            available = max(Decimal(0), decimal(row['shortage']) - locked)
            if action != 'return' and available > 0 and supplier:
                self._allocate(db, ident, supplier, available, 'manual' if action == 'assign' else 'system')
            db.execute('UPDATE purchase_workspace_decisions SET origin=? WHERE item_id=?',
                       ('manual_return' if action == 'return' else 'manual' if action == 'assign' else 'system', ident))
        elif action == 'split':
            quantity = decimal(target.get('quantity', body.get('quantity')))
            from_id = text(target.get('allocation_id', body.get('allocation_id')))
            origin = next((a for a in allocations if a['id'] == from_id and a['state'] == 'draft'), None)
            if from_id and not origin:
                raise PurchaseConflict('原分配已改变，不能拆分', 'allocation_changed')
            remaining = decimal(origin['quantity']) if origin else decimal(row['shortage']) - sum(
                (decimal(a['quantity']) for a in allocations if a['state'] != 'erp_counted'), Decimal(0))
            if quantity <= 0 or quantity > remaining:
                raise ValueError('拆分数量须大于零且不能超过剩余数量')
            if origin:
                db.execute('UPDATE purchase_workspace_allocations SET quantity=?,state=?,updated_at=? WHERE id=?',
                    (number(remaining - quantity), 'draft' if quantity < remaining else 'cancelled', now, from_id))
            self._allocate(db, ident, supplier, quantity, 'manual')
            db.execute("UPDATE purchase_workspace_decisions SET origin='manual' WHERE item_id=?", (ident,))
        elif action == 'fixed_supplier':
            db.execute('UPDATE purchase_workspace_decisions SET fixed_supplier=? WHERE item_id=?', (supplier, ident))
        elif action == 'review_ack':
            note = text(target.get('note', body.get('note')))
            if len(note) < 2:
                raise ValueError('请填写需求核对说明')
            db.execute('UPDATE purchase_workspace_decisions SET review_ack=? WHERE item_id=?', (dumps(item['review_reasons']), ident))
        elif action == 'classify':
            category, kind = text(body.get('category')), text(body.get('kind'))
            if not category or len(category) > 50 or kind not in {'标准件', '非标件', '待分类'}:
                raise ValueError('请选择类别及标准／非标')
            db.execute('UPDATE purchase_workspace_decisions SET category=?,kind=? WHERE item_id=?', (category, kind, ident))
        elif action == 'quote':
            body = {**body, **target.get('quote', {})}
            price = decimal(body.get('unit_price'))
            if price < 0 or body.get('tax_basis') not in {'含税', '未税', '待核对'}:
                raise ValueError('请核对报价及税价口径')
            data = {k: text(body.get(k))[:500] for k in ('unit_price', 'tax_basis', 'delivery_date', 'pack_size', 'minimum_quantity', 'shipping', 'note')}
            db.execute('INSERT INTO purchase_workspace_quotes VALUES (?,?,?,?,?,?)',
                       (uuid.uuid4().hex, ident, supplier, dumps(data), user['id'], now))
        elif action == 'adopt_quote':
            q=db.execute('SELECT * FROM purchase_workspace_quotes WHERE id=? AND item_id=?',(body.get('quote_id'),ident)).fetchone()
            if not q:raise ValueError('报价不存在')
            quotation=json.loads(q['payload'])
            if quotation['tax_basis'] not in {'含税','未税'}:raise ValueError('请先确认报价税价口径')
            a=next((a for a in allocations if a['id']==body.get('allocation_id') and a['state']=='draft' and a['supplier_id']==q['supplier_id']),None)
            if not a:raise PurchaseConflict('报价须与当前供应商分配一致，请先分配再采纳')
            self._supplier(db,a['supplier_id'],row['profile_id'])
            from decimal import ROUND_HALF_UP
            details={**json.loads(a['details']),**quotation,'quote_id':q['id'],
                     'amount':number((decimal(a['quantity'])*decimal(quotation['unit_price'])).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))}
            db.execute('UPDATE purchase_workspace_allocations SET details=?,updated_at=? WHERE id=?',(dumps(details),now,a['id']))
        elif action == 'record_purchase':
            def picked(field, default=None):
                if isinstance(target, dict) and field in target and target.get(field) not in (None, ''):
                    return target.get(field)
                if field in body and body.get(field) not in (None, ''):
                    return body.get(field)
                return default
            alloc_id = text(picked('allocation_id') or '')
            drafts = [a for a in allocations if a['state'] == 'draft' and (not alloc_id or a['id'] == alloc_id)]
            a = next((a for a in drafts if a['id'] == alloc_id), None) if alloc_id else (drafts[0] if len(drafts) == 1 else None)
            if not a:
                raise PurchaseConflict('只能记录当前未下单的采购分配', 'allocation_changed')
            self._supplier(db, a['supplier_id'], row['profile_id'])
            if item.get('source_missing') or not item.get('sku_id') or any('停用' in r for r in item['review_reasons']):
                raise PurchaseConflict('商品来源缺失或停用，不能记录新的采购', 'product_unavailable')
            if item['review_reasons'] and decision['review_ack'] != dumps(item['review_reasons']):
                raise PurchaseConflict('请先核对这件商品的需求原因', 'demand_review_required')
            qty, amount = decimal(picked('quantity')), decimal(picked('amount'))
            order_no = text(picked('platform_order_no'))
            order_auto = bool(target.get('_order_auto'))
            if not order_no:
                order_no = next_auto_order_no(db, shanghai_now())
                order_auto = True
            if qty <= 0 or amount < 0 or not order_no:
                raise ValueError('请填写实际支数和成交金额；订单号可留空，由系统生成')
            tax_basis = picked('tax_basis')
            if tax_basis not in {'含税', '未税'}:
                raise ValueError('请明确成交金额的税价口径')
            confirmed = target.get('quantity_difference_confirmed') if isinstance(target, dict) and 'quantity_difference_confirmed' in target else body.get('quantity_difference_confirmed')
            if qty != decimal(a['quantity']) and confirmed is not True:
                raise ValueError('实际数量与计划不同，请确认包装多买或部分购买')
            line_no = ''
            for _attempt in range(8):
                line_no = next_platform_line_no(db, order_no)
                duplicate = db.execute("""SELECT id FROM purchase_workspace_allocations WHERE state!='cancelled'
                    AND supplier_id=? AND json_extract(details,'$.platform_order_no')=?
                    AND json_extract(details,'$.platform_line_no')=?""", (a['supplier_id'], order_no, line_no)).fetchone()
                if not duplicate:
                    break
            else:
                raise PurchaseConflict('明细序号分配冲突，请重试', 'purchase_line_duplicate')
            note = text(target.get('note') if isinstance(target, dict) and 'note' in target else body.get('note'))
            if not note:
                note = default_purchase_note(item, text(body.get('customer') or target.get('customer')), qty)
            details = {k: text(picked(k))[:500] for k in ('tax_basis', 'pack_count', 'pack_size', 'delivery_date')}
            details.update(platform_order_no=order_no[:60], platform_line_no=line_no, note=note[:500],
                           arrival_note=note[:500], order_no_auto=1 if order_auto else 0, ordered_at=now)
            if details['pack_count'] or details['pack_size']:
                if decimal(details['pack_count']) <= 0 or decimal(details['pack_size']) <= 0 or decimal(details['pack_count']) * decimal(details['pack_size']) != qty:
                    raise ValueError('包数 × 每包支数必须等于实际采购支数')
            details.update(amount=number(amount), unit_price=number(amount/qty), tax_basis=tax_basis,
                           recorded_by=user['display_name'] if 'display_name' in user else user['id'],
                           original_plan_quantity=a['quantity'], recorded_source_token=body['source_token'])
            db.execute("UPDATE purchase_workspace_allocations SET quantity=?,state='ordered',details=?,updated_at=? WHERE id=?",
                       (number(qty), dumps(details), now, a['id']))
            line_result = {'item_id': ident, 'allocation_id': a['id'], 'sku': text(item.get('sku')),
                           'platform_order_no': order_no, 'platform_line_no': line_no, 'note': note[:500]}
        elif action == 'confirm_erp_counted':
            a=next((a for a in allocations if a['id']==body.get('allocation_id') and a['state'] in {'ordered','awaiting_audit'}),None)
            if not a:raise PurchaseConflict('请选择尚未确认计入 ERP 的已下单记录')
            d=json.loads(a['details'])
            if not d.get('recorded_source_token') or d['recorded_source_token']==body['source_token']:
                raise PurchaseConflict('请先在 ERP 审核并刷新采购需求，再逐行确认；未释放原预留')
            erp_no=text(body.get('erp_order_no'))[:100];line_no=text(body.get('erp_line_no'))[:100]
            if not erp_no or not line_no or body.get('erp_counted_confirmed') is not True:
                raise ValueError('请填写 ERP 单号、明细序号，并确认该明细已审核且计入库存 API')
            if decimal(body.get('erp_quantity'))!=decimal(a['quantity']):
                raise ValueError('ERP 本行数量与实际采购支数不同，请核对；未释放预留')
            duplicate=db.execute("""SELECT id FROM purchase_workspace_allocations WHERE state='erp_counted'
                AND supplier_id=? AND json_extract(details,'$.erp_order_no')=? AND json_extract(details,'$.erp_line_no')=?""",
                (a['supplier_id'],erp_no,line_no)).fetchone()
            if duplicate:raise PurchaseConflict('该 ERP 明细已经关联另一条采购记录，不能重复释放预留')
            d.update(erp_order_no=erp_no,erp_line_no=line_no,erp_quantity=a['quantity'],
                     erp_counted_method='human_verified',erp_counted_by=user['id'],erp_counted_at=now,
                     erp_counted_source_token=body['source_token'])
            db.execute("UPDATE purchase_workspace_allocations SET state='erp_counted',details=?,updated_at=? WHERE id=?",(dumps(d),now,a['id']))
        elif action == 'mark_inquired':
            chosen = text(target.get('allocation_id') or body.get('allocation_id') or '')
            drafts = [a for a in allocations if a['state'] == 'draft' and (not chosen or a['id'] == chosen)]
            if not drafts:
                raise PurchaseConflict('没有可标为已询价的待下单安排', 'allocation_changed')
            inquiry_note = text(body.get('note') or '')[:500]
            for a in drafts:
                details = json.loads(a['details'] or '{}')
                details.setdefault('inquired_at', now)
                if inquiry_note:
                    details['inquiry_note'] = inquiry_note
                details['inquired_by'] = user['id']
                db.execute('UPDATE purchase_workspace_allocations SET details=?,updated_at=? WHERE id=?', (dumps(details), now, a['id']))
        elif action == 'mark_arrived':
            chosen = text(target.get('allocation_id') or body.get('allocation_id') or '')
            candidates = [a for a in allocations if a['state'] in {'ordered', 'awaiting_audit'} and (not chosen or a['id'] == chosen)]
            if not chosen and len(candidates) != 1:
                raise PurchaseConflict('请指定要登记到货的已下单记录', 'allocation_changed')
            if not candidates:
                raise PurchaseConflict('只能对已下单、尚未计入 ERP 的记录登记到货', 'allocation_changed')
            a = candidates[0]
            arrived_qty = decimal(target.get('arrived_quantity', body.get('arrived_quantity', a['quantity'])))
            if arrived_qty <= 0 or arrived_qty > decimal(a['quantity']):
                raise ValueError('到货支数须大于零且不能超过下单支数')
            arrived_on = text(body.get('arrived_on') or '') or shanghai_now().date().isoformat()
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', arrived_on):
                raise ValueError('请填写到货日期')
            details = json.loads(a['details'] or '{}')
            details.update(arrived_at=now, arrived_on=arrived_on, arrived_quantity=number(arrived_qty), arrived_by=user['id'])
            db.execute('UPDATE purchase_workspace_allocations SET details=?,updated_at=? WHERE id=?', (dumps(details), now, a['id']))
        elif action == 'mark_settled':
            chosen = text(target.get('allocation_id') or body.get('allocation_id') or '')
            candidates = [a for a in allocations if a['state'] != 'draft' and (not chosen or a['id'] == chosen)]
            if not chosen and len(candidates) != 1:
                raise PurchaseConflict('请指定要对账的已下单记录', 'allocation_changed')
            if not candidates:
                raise PurchaseConflict('草稿还没有成交金额，不能对账', 'allocation_changed')
            a = candidates[0]
            details = json.loads(a['details'] or '{}')
            amount = decimal(details.get('amount') or 0)
            settled = decimal(target.get('settled_amount', body.get('settled_amount')))
            if amount <= 0:
                raise ValueError('这笔记账没有成交金额，不能对账')
            if settled < 0 or settled > amount:
                raise ValueError('已结金额不能为负，也不能超过本行成交金额')
            details.update(settled_amount=number(settled), settled_at=now, settle_note=text(body.get('note') or '')[:500])
            db.execute('UPDATE purchase_workspace_allocations SET details=?,updated_at=? WHERE id=?', (dumps(details), now, a['id']))
        db.execute('UPDATE purchase_workspace_decisions SET version=version+1,updated_at=? WHERE item_id=?', (now, ident))
        db.execute('INSERT INTO purchase_workspace_events(item_id,actor_id,action,details,created_at) VALUES (?,?,?,?,?)',
                   (ident, user['id'], action, dumps({'target': {k: v for k, v in target.items() if k != '_order_auto'},
                                                      'body': {k: v for k, v in body.items() if k not in {'items', 'source_token'}}}), now))
        return line_result

    @staticmethod
    def _allocate(db, ident, supplier, quantity, origin):
        now = timestamp()
        db.execute('INSERT INTO purchase_workspace_allocations VALUES (?,?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, ident, supplier, number(quantity), 'draft', origin, '{}', now, now))

    @staticmethod
    def refresh_status(db):
        row = db.execute('SELECT * FROM purchase_workspace_refresh ORDER BY rowid DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def request_refresh(self, user, body):
        authorize(user)
        ident = str(uuid.UUID(text(body.get('idempotency_key'))))
        with self.store.connect(write=True) as db:
            old = db.execute('SELECT * FROM purchase_workspace_refresh WHERE id=?', (ident,)).fetchone()
            if old:
                return dict(old)
            queued = db.execute("SELECT * FROM purchase_workspace_refresh WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if queued:
                # The queued read will begin after all merged requests.
                db.execute('UPDATE purchase_workspace_refresh SET requested_at=? WHERE id=?', (timestamp(), queued['id']))
                return dict(queued)
            now = timestamp()
            scope = 'full' if isinstance(body, dict) and body.get('full') is True else 'incremental'
            db.execute('INSERT INTO purchase_workspace_refresh VALUES (?,?,?,?,?,?,?)',
                       (ident, user['id'], now, 'queued', '', dumps({'scope': scope}), now))
            return dict(db.execute('SELECT * FROM purchase_workspace_refresh WHERE id=?', (ident,)).fetchone())

    def _remember_watermark(self):
        with self.store.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO purchase_workspace_meta VALUES ('erp_refresh_watermark', ?)",
                       (shanghai_now().isoformat(timespec='seconds'),))

    @staticmethod
    def _refresh_scope(item):
        try:
            payload = json.loads(item.get('result') or '{}')
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return payload.get('scope') or 'incremental'

    def reconciliation(self, user, args):
        authorize(user)
        supplier, customer = text(args.get('supplier')), text(args.get('customer'))
        start, end = text(args.get('from')), text(args.get('to'))
        with self.store.read_snapshot(), self.store.connect() as db:
            items = self._items(db)
        lines, by_supplier, by_customer = [], {}, {}
        for item in items:
            cards = []
            for demand in item.get('demands') or []:
                card = card_name_for_demand(demand, item)
                if card and card not in cards:
                    cards.append(card)
            if customer and customer not in cards:
                continue
            shared = len(cards) != 1
            for allocation in item['allocations']:
                if allocation['state'] == 'draft' or (supplier and allocation['supplier_id'] != supplier):
                    continue
                details = allocation.get('details') or {}
                when = text(details.get('ordered_at') or allocation.get('updated_at'))[:10]
                if start and when and when < start:
                    continue
                if end and when and when > end:
                    continue
                amount = decimal(details.get('amount') or 0)
                settled = decimal(details['settled_amount']) if details.get('settled_amount') not in (None, '') else Decimal(0)
                unsettled = amount - settled if amount > 0 else Decimal(0)
                tax = text(details.get('tax_basis')) or '待核对'
                row = {'allocation_id': allocation['id'], 'item_id': item['id'], 'sku': item.get('sku'),
                       'spec': item.get('spec'), 'version': item.get('version'),
                       'supplier_id': allocation['supplier_id'], 'supplier_name': allocation.get('supplier_name'),
                       'customers': cards, 'quantity': allocation['quantity'], 'amount': number(amount),
                       'tax_basis': tax, 'settled_amount': number(settled), 'unsettled_amount': number(unsettled),
                       'state': allocation['state'], 'progress': allocation.get('progress'),
                       'platform_order_no': text(details.get('platform_order_no')),
                       'platform_line_no': text(details.get('platform_line_no')),
                       'ordered_at': text(details.get('ordered_at') or allocation.get('updated_at')),
                       'note': text(details.get('arrival_note') or details.get('note'))}
                lines.append(row)
                slot = by_supplier.setdefault((allocation['supplier_id'], tax), {
                    'supplier_id': allocation['supplier_id'], 'supplier_name': allocation.get('supplier_name'),
                    'tax_basis': tax, 'amount': Decimal(0), 'settled': Decimal(0), 'unsettled': Decimal(0), 'count': 0})
                slot['amount'] += amount
                slot['settled'] += settled
                slot['unsettled'] += unsettled
                slot['count'] += 1
                bucket = cards[0] if not shared and cards else '多家客户共用'
                cust = by_customer.setdefault((bucket, tax), {
                    'name': bucket, 'tax_basis': tax, 'amount': Decimal(0), 'settled': Decimal(0),
                    'unsettled': Decimal(0), 'count': 0})
                cust['amount'] += amount
                cust['settled'] += settled
                cust['unsettled'] += unsettled
                cust['count'] += 1

        def pack(rows):
            for row in rows:
                for key in ('amount', 'settled', 'unsettled'):
                    row[key] = number(row[key])
            return rows
        return {'lines': lines[:500], 'truncated': len(lines) > 500,
                'by_supplier': pack(list(by_supplier.values())),
                'by_customer': pack(list(by_customer.values())),
                'mixed_tax': '含税与未税分开汇总，不混加'}

    def tick_refresh(self, updates):
        with self.store.connect() as db:
            waiting = [dict(r) for r in db.execute("SELECT * FROM purchase_workspace_refresh WHERE status IN ('queued','running') ORDER BY rowid")]
        for item in waiting:
            if item['status'] == 'running':
                job = updates.get(item['job_id'])
                if job['status'] in {'queued', 'running'}:
                    continue
                current = False
                if job['status'] == 'succeeded':
                    result=self.rebuild()
                    with self.store.connect() as db:
                        _,current=self._source(db)
                    if not current and not result.get('error'):
                        continue
                    if current:
                        self._remember_watermark()
                with self.store.connect(write=True) as db:
                    db.execute('UPDATE purchase_workspace_refresh SET status=?,result=?,updated_at=? WHERE id=?',
                               ('succeeded' if job['status'] == 'succeeded' and current else 'failed', dumps({'job_status': job['status']}), timestamp(), item['id']))
            else:
                with self.store.connect() as db:
                    active = db.execute("SELECT 1 FROM update_jobs WHERE status IN ('queued','running')").fetchone()
                if active:
                    continue
                scope = self._refresh_scope(item)
                sync = getattr(getattr(updates, 'source', None), 'refresh_purchase_incremental', None)
                if scope != 'full' and callable(sync):
                    from .purchase_incremental import read_watermark
                    with self.store.connect() as db:
                        watermark = read_watermark(db)
                    if watermark:
                        try:
                            outcome = sync()
                        except Exception as error:
                            logging.getLogger(__name__).exception('Purchase incremental refresh failed')
                            with self.store.connect(write=True) as db:
                                db.execute('UPDATE purchase_workspace_refresh SET status=?,result=?,updated_at=? WHERE id=?',
                                           ('failed', dumps({'mode': 'incremental', 'message': str(error)[:200]}), timestamp(), item['id']))
                            break
                        if outcome.get('applied'):
                            self.rebuild(force=True)
                            self._remember_watermark()
                            summary = {k: outcome.get(k) for k in ('purchase_rows', 'stock_rows')}
                            plan = outcome.get('plan') or {}
                            summary.update(mode='incremental', overlap_hours=plan.get('overlap_hours'),
                                           begin=plan.get('begin'), end=plan.get('end'))
                            with self.store.connect(write=True) as db:
                                db.execute('UPDATE purchase_workspace_refresh SET status=?,result=?,updated_at=? WHERE id=?',
                                           ('succeeded', dumps(summary), timestamp(), item['id']))
                            break
                job_id = str(uuid.uuid4())
                job = updates.enqueue(job_id, 'reconcile' if scope == 'full' else 'refresh')
                # Another worker may have won. Never bind to an older read.
                if job['id'] != job_id:
                    continue
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE purchase_workspace_refresh SET status='running',job_id=?,updated_at=? WHERE id=?", (job_id, timestamp(), item['id']))
                break

    def start(self, updates):
        def loop():
            import time
            checked=0
            while not self.stop_event.wait(3):
                try:
                    self.tick_refresh(updates)
                    if time.monotonic()-checked>60:
                        self.rebuild()
                        checked=time.monotonic()
                except Exception:
                    # The durable request survives a busy source or process restart.
                    logging.getLogger(__name__).exception('Purchase refresh remains pending')
                    continue
        Thread(target=loop, name='purchase-refresh', daemon=True).start()


def install(app, engine, sessions):
    bp = Blueprint('purchase_workspace', __name__)
    static = Path(__file__).resolve().parents[1] / 'static' / 'purchase'

    @bp.errorhandler(PurchaseConflict)
    @bp.errorhandler(ValueError)
    def bad(error):
        return jsonify(ok=False, error=getattr(error, 'code', 'invalid_request'), message=str(error)), getattr(error, 'status', 400)

    @bp.before_request
    def roles():
        if request.path.startswith(BASE + '/') and request.path != BASE + '/login':
            authorize(g.current_user)

    @bp.get('/purchase')
    @bp.get('/purchase/')
    def index():
        return send_from_directory(static, 'index.html')

    @bp.get('/purchase/assets/<path:filename>')
    def assets(filename):
        return send_from_directory(static / 'assets', filename)

    @bp.post(BASE + '/login')
    def login():
        if not same_origin(app):
            raise PurchaseConflict('请从采购工作台登录', 'origin_blocked', 403)
        body = request.get_json(silent=True) or {}
        token, error = sessions.login(body.get('password'), request.remote_addr, body.get('username', 'admin'))
        if not token:
            raise PurchaseConflict('账号或密码错误，请核对后重试', 'login_failed', 401)
        user = sessions.current(token)
        try:
            authorize(user)
        except PurchaseConflict:
            sessions.revoke(token)
            raise
        response = jsonify(ok=True, user=user, csrf=csrf(token))
        response.set_cookie(COOKIE, token, httponly=True, secure=not app.testing, samesite='Strict', max_age=7*86400, path=BASE)
        return response

    @bp.post(BASE + '/logout')
    def logout():
        sessions.revoke(request.cookies.get(COOKIE, ''))
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE, path=BASE)
        return response

    @bp.get(BASE + '/bootstrap')
    def bootstrap():
        return jsonify(ok=True, user=g.current_user, csrf=csrf(request.cookies.get(COOKIE, '')),
                       **engine.erp_submit_status())

    @bp.post(BASE + '/erp-draft')
    def erp_draft():
        from .purchase_submission import PurchaseSubmissionBlocked
        try:
            result = engine.submit_erp_draft(g.current_user, request.get_json(silent=True))
        except PurchaseSubmissionBlocked as error:
            return jsonify(ok=False, error=error.code, message=str(error)), error.status
        return jsonify(ok=True, **result)

    @bp.get(BASE + '/items')
    def items():
        return jsonify(ok=True, **engine.listing(g.current_user, request.args))

    @bp.get(BASE + '/reconciliation')
    def reconciliation():
        return jsonify(ok=True, **engine.reconciliation(g.current_user, request.args))

    @bp.get(BASE + '/items/<ident>')
    def detail(ident):
        return jsonify(ok=True, item=engine.detail(g.current_user, ident))

    @bp.get(BASE + '/items/<ident>/image')
    def image(ident):
        item = engine.detail(g.current_user, ident)
        images = app.extensions['service']['product_images']
        metadata = images.list_for_product(item['product_id'])
        if not metadata['items']:
            raise PurchaseConflict('该商品暂时没有图纸或图片', 'image_missing', 404)
        content = images.content(metadata['items'][0]['id'])
        return send_file(content.path, mimetype=content.mime_type, conditional=True, etag=content.etag)

    @bp.post(BASE + '/actions')
    def actions():
        return jsonify(ok=True, **engine.mutate(g.current_user, request.get_json(silent=True)))

    @bp.post(BASE + '/refresh')
    def refresh():
        return jsonify(ok=True, refresh=engine.request_refresh(g.current_user, request.get_json(silent=True) or {})), 202

    @bp.get(BASE + '/quote-template')
    def template():
        from .purchase_workspace_files import quote_template
        return send_file(quote_template(),as_attachment=True,download_name='供应商报价模板.xlsx',mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    @bp.post(BASE + '/quote-preview')
    def quote_preview():
        from .purchase_workspace_files import preview_quotes
        upload=request.files.get('file')
        if not upload:raise ValueError('请选择报价表')
        return jsonify(ok=True,preview=preview_quotes(engine,g.current_user,upload.read(10*1024*1024+1),upload.filename or '',request.form.get('supplier_id')))

    @bp.post(BASE + '/quote-apply')
    def quote_apply():
        from .purchase_workspace_files import apply_quotes
        return jsonify(ok=True,**apply_quotes(engine,g.current_user,request.get_json(silent=True) or {}))

    @bp.post(BASE + '/documents')
    def document_create():
        from .purchase_workspace_files import export_document
        return jsonify(ok=True,document=export_document(engine,g.current_user,request.get_json(silent=True) or {},app.extensions['service']['product_images']))

    @bp.get(BASE + '/documents/<ident>')
    def document_download(ident):
        with engine.store.connect() as db:
            row=db.execute('SELECT * FROM purchase_workspace_documents WHERE id=?',(ident,)).fetchone()
        if not row:raise PurchaseConflict('文件不存在','not_found',404)
        path=engine.store.runtime/'purchase-documents'/(row['id']+'.pdf')
        if not path.is_file() or sha256(path.read_bytes()).hexdigest()!=row['file_sha256']:
            raise PurchaseConflict('文件完整性核对失败，请重新生成','document_changed')
        return send_file(path,as_attachment=True,download_name=('采购单' if row['kind']=='purchase' else '采购询价单')+'-'+ident[:8]+'.pdf')

    app.register_blueprint(bp)
