"""Durable Qimen order lifecycle and shipped-but-unposted shadow demand.

The broad Qimen query is an active-work list.  A document can disappear from
that list when it reaches ERP ``process_state=500`` even though ERP physical
stock has not been posted yet.  The last published business snapshot is kept
as shadow demand until an exact query proves ``process_state=600``.

This module stages observations only.  :func:`persist` is called from the same
SQLite transaction that publishes the query snapshot, so a failed update never
advances lifecycle evidence on its own.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import date, datetime

from .erp_api import ErpUnavailable
from .store import dumps
from . import delivery_confirmations


QIMEN = 'qimen.taobao.erp.order.sync'
START = '1900-01-01 00:00:00'
END = '2100-01-01 00:00:00'
SHIPPED_UNPOSTED_NAME = '已完成订单需结存'
PENDING_REVIEW_NAME = '订单状态待核对'
STATES = frozenset({'active', 'candidate', 'shipped_unposted',
                    'release_pending', 'archived'})
LIFECYCLE_FIELDS = frozenset({
    'lifecycle_state', 'process_state', 'lifecycle_confirmations',
    'lifecycle_error', 'lifecycle_pending_review', 'lifecycle_print_blocked',
    'lifecycle_write_ready', 'lifecycle_freight_code', 'shadow_demand',
    'lifecycle_order_date_fallback', 'lifecycle_release_pending',
})


class LifecycleQueryError(ValueError):
    pass


class OrderNotFound(LifecycleQueryError):
    """Successful exact all-status query explicitly returned zero records."""


def _text(value):
    return '' if value is None else str(value).strip()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _expanded(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            return {}
    return {}


def order_no(raw):
    head = raw.get('trade_order') if isinstance(raw, dict) else None
    return _text(head.get('trade_id')) if isinstance(head, dict) else ''


def process_state(raw):
    head = raw.get('trade_order') if isinstance(raw, dict) else None
    if not isinstance(head, dict):
        return ''
    value = _expanded(head.get('extendProps')).get('process_state')
    return _text(value)


def _clean_business(value):
    if isinstance(value, dict):
        return {key: _clean_business(item) for key, item in value.items()
                if key not in LIFECYCLE_FIELDS}
    if isinstance(value, list):
        return [_clean_business(item) for item in value]
    return copy.deepcopy(value)


def ensure_schema(store):
    """Own an additive schema independently of the main user_version."""
    delivery_confirmations.ensure_schema(store)
    with store.connect(write=True) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS order_lifecycle_state(
                profile_id TEXT NOT NULL,
                order_no TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                process_state TEXT NOT NULL DEFAULT '',
                confirmation_count INTEGER NOT NULL DEFAULT 0,
                first_seen_500_at TEXT NOT NULL DEFAULT '',
                last_confirmed_at TEXT NOT NULL DEFAULT '',
                ended_at TEXT NOT NULL DEFAULT '',
                last_observed_at TEXT NOT NULL DEFAULT '',
                last_business_snapshot TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,order_no)
            );
            CREATE INDEX IF NOT EXISTS order_lifecycle_status
                ON order_lifecycle_state(profile_id,lifecycle_state,updated_at);
            CREATE TABLE IF NOT EXISTS order_lifecycle_audit(
                id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                order_no TEXT NOT NULL,
                from_state TEXT NOT NULL DEFAULT '',
                to_state TEXT NOT NULL,
                process_state TEXT NOT NULL DEFAULT '',
                observation TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS order_lifecycle_audit_order
                ON order_lifecycle_audit(profile_id,order_no,created_at);
            CREATE TABLE IF NOT EXISTS erp_order_delivery_cache(
                profile_id TEXT NOT NULL,
                order_no TEXT NOT NULL,
                delivery_date TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,order_no)
            );
            CREATE INDEX IF NOT EXISTS erp_order_delivery_date
                ON erp_order_delivery_cache(profile_id,delivery_date,order_no);
        ''')


def _empty(profile, number, observed_at):
    return {
        'profile_id': str(profile), 'order_no': number,
        'lifecycle_state': 'active', 'process_state': '',
        'confirmation_count': 0, 'first_seen_500_at': '',
        'last_confirmed_at': '', 'ended_at': '',
        'last_observed_at': '', 'business_snapshot': {},
        'content_hash': '', 'last_error': '', 'updated_at': observed_at,
    }


def _active(old, state, observed_at):
    row = copy.deepcopy(old)
    row.update(lifecycle_state='active', process_state=state,
               confirmation_count=0, ended_at='', last_observed_at=observed_at,
               last_error='', updated_at=observed_at)
    return row


def _state_500(old, observed_at):
    row = copy.deepcopy(old)
    # A single 600 observation is held as ``release_pending``.  If the next
    # complete read puts the order back in bill_status=6, restore the already
    # confirmed 500 state immediately instead of making the operator wait for
    # two new 500 observations.
    if (row.get('lifecycle_state') == 'release_pending' and
            int(row.get('confirmation_count') or 0) >= 2):
        row.update(lifecycle_state='shipped_unposted', process_state='500',
                   last_confirmed_at=observed_at, ended_at='',
                   last_observed_at=observed_at, last_error='', updated_at=observed_at)
        return row
    consecutive = (row.get('process_state') == '500' and
                   row.get('lifecycle_state') in {'candidate', 'shipped_unposted'})
    count = int(row.get('confirmation_count') or 0) + 1 if consecutive else 1
    row.update(lifecycle_state='shipped_unposted' if count >= 2 else 'candidate',
               process_state='500', confirmation_count=count,
               first_seen_500_at=row.get('first_seen_500_at') or observed_at,
               last_confirmed_at=observed_at, ended_at='',
               last_observed_at=observed_at, last_error='', updated_at=observed_at)
    return row


def _state_600(old, observed_at, force=False):
    row = copy.deepcopy(old)
    confirmed = force or (row.get('process_state') == '600' and
                          row.get('lifecycle_state') == 'release_pending')
    row.update(lifecycle_state='archived' if confirmed else 'release_pending',
               process_state='600', ended_at=observed_at if confirmed else '',
               last_confirmed_at=observed_at, last_observed_at=observed_at,
               last_error='', updated_at=observed_at)
    return row


def _absent(old, observed_at):
    # Removed/re-entered documents are not unposted stock reservations. Keep
    # their history, but do not label an absence as ERP posting (state 600).
    row = copy.deepcopy(old)
    row.update(lifecycle_state='archived', process_state='absent',
               ended_at=observed_at, last_confirmed_at=observed_at,
               last_observed_at=observed_at, updated_at=observed_at,
               last_error='absent_from_complete_erp_sets')
    return row


def _delivery_day(value):
    value = _text(value)
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed.isoformat()
    except ValueError:
        raise ValueError('订单交期必须使用 YYYY-MM-DD') from None


def _persist_delivery_dates(db, profile, values, source, observed_at,
                            replace=True):
    """Persist verified dates inside the caller's SQLite transaction."""
    profile = _text(profile)
    source = _text(source)
    if not profile or not source:
        raise ValueError('交期缓存缺少账套或来源')
    installed = 0
    for number, raw_day in sorted(values.items()):
        number, day = _text(number), _delivery_day(raw_day)
        if not number:
            raise ValueError('交期缓存缺少完整订单号')
        prior = db.execute('''SELECT delivery_date FROM erp_order_delivery_cache
            WHERE profile_id=? AND order_no=?''', (profile, number)).fetchone()
        if prior and prior[0] != day and not replace:
            raise ValueError(f'订单 {number} 已有不同的已验证交期，拒绝覆盖')
        if prior and prior[0] == day:
            continue
        db.execute('''INSERT INTO erp_order_delivery_cache(
                profile_id,order_no,delivery_date,source,updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(profile_id,order_no) DO UPDATE SET
                delivery_date=excluded.delivery_date,
                source=excluded.source,
                updated_at=excluded.updated_at''',
            (profile, number, day, source, observed_at))
        installed += 1
    return installed


def import_delivery_dates(store, profile, values, source='verified_historical_import',
                          observed_at=None, replace=False):
    """Install a reviewed order->delivery-date mapping for historical shadows.

    This is intentionally a separate, explicit operation.  It never derives a
    delivery date from the order date, and refuses conflicting replacements by
    default.
    """
    if not isinstance(values, dict):
        raise ValueError('交期导入必须是订单号到日期的映射')
    ensure_schema(store)
    stamp = _text(observed_at) or datetime.now().astimezone().isoformat(timespec='seconds')
    with store.connect(write=True) as db:
        installed = _persist_delivery_dates(
            db, profile, values, source, stamp, replace=replace)
    return {'profile_id': _text(profile), 'validated': len(values),
            'installed': installed, 'source': _text(source)}


def _query_exact(client, profile, number):
    response = client.call(QIMEN, {
        'pageNo': 1, 'pageSize': 100,
        'extendProps': {'profile_id': int(profile), 'query_time_type': 2,
            # Broad status 7 would contain the entire posted history.  With an
            # exact trade_id it is safe and necessary to observe a 500 -> 600
            # transition without enumerating that history.
            'start_time': START, 'end_time': END,
            'bill_status': '1,2,3,4,5,6,7',
            'trade_id': number},
    })
    if not isinstance(response, dict) or str(response.get('code')) != '0':
        raise LifecycleQueryError('奇门精确查询未返回明确成功')
    items = response.get('trade_orders')
    if isinstance(items, dict):
        items = items.get('trade_orders')
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise LifecycleQueryError('奇门精确查询集合结构不完整')
    totals = [response[key] for key in ('TotalResults', 'totalResults') if key in response]
    if not items and totals and all(not isinstance(v, bool) and str(v) == '0' for v in totals):
        raise OrderNotFound('完整ERP清单及全状态精确查询均已无此订单')
    matches = [item for item in items if order_no(item) == number]
    unique = {_canonical(item): item for item in matches}
    if len(unique) != 1:
        raise LifecycleQueryError('奇门精确查询未唯一命中订单')
    raw = next(iter(unique.values()))
    if process_state(raw) not in {'400', '500', '600'}:
        raise LifecycleQueryError('奇门订单处理状态尚未验证')
    return raw


def _snapshot_from_records(rows):
    if not rows:
        return {}
    rows = [copy.deepcopy(row) for row in rows]
    first = rows[0]
    lines = []
    for index, row in enumerate(rows, 1):
        lines.append({
            key: row.get(key, '') for key in
            ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')
        } | {
            'line_id': _text(row.get('line_id')) or str(index),
            'product_code': _text(row.get('sku')).upper(),
            'open_qty': _text(row.get('quantity')),
            'reported_qty': _text(row.get('quantity')),
            'delivery_date': _text(row.get('delivery_date')),
        })
    order = {
        'order_no': _text(first.get('order_no')),
        'order_date': _text(first.get('order_date') or first.get('document_date'))[:10],
        'delivery_date': _text(first.get('delivery_date'))[:10],
        'lines': lines,
    }
    return {'order': order, 'records': rows}


class OrderLifecycle:
    def __init__(self, store):
        self.store = store
        ensure_schema(store)

    def _load(self, profile):
        with self.store.connect() as db:
            rows = db.execute(
                'SELECT * FROM order_lifecycle_state WHERE profile_id=?',
                (str(profile),),
            ).fetchall()
        output = {}
        for raw in rows:
            row = dict(raw)
            try:
                snapshot = json.loads(row.pop('last_business_snapshot'))
            except (TypeError, ValueError):
                snapshot = {}
            row['business_snapshot'] = snapshot if isinstance(snapshot, dict) else {}
            output[row['order_no']] = row
        return output

    def _bootstrap(self, profile, observed_at):
        groups = {}
        for row in self.store.rows('orders'):
            if _text(row.get('profile_id')) == str(profile) and _text(row.get('order_no')):
                groups.setdefault(_text(row['order_no']), []).append(row)
        output = {}
        for number, rows in groups.items():
            snapshot = _snapshot_from_records(rows)
            entry = _empty(profile, number, observed_at)
            entry['business_snapshot'] = snapshot
            entry['content_hash'] = hashlib.sha256(_canonical(snapshot).encode()).hexdigest()
            output[number] = entry
        return output

    @staticmethod
    def _freight_mapping(client):
        try:
            response = client.call('erp.freight.list', {})
        except ErpUnavailable:
            return {'code': '', 'available': False,
                    'reason': '物流公司字典暂不可用，不写回 ERP'}
        rows = response.get('freights') if isinstance(response, dict) else None
        if isinstance(rows, dict):
            rows = rows.get('freights')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return {'code': '', 'available': False,
                    'reason': '物流公司字典结构不完整，不写回 ERP'}
        matches = [row for row in rows if _text(row.get('fullname')) == SHIPPED_UNPOSTED_NAME]
        if len(matches) != 1 or not _text(matches[0].get('usercode')):
            return {'code': '', 'available': False,
                    'reason': '物流公司字典缺少唯一的“已完成订单需结存”编码，不写回 ERP'}
        code = _text(matches[0]['usercode'])
        if sum(_text(row.get('usercode')) == code for row in rows) != 1:
            return {'code': '', 'available': False,
                    'reason': '物流公司编码重复，不写回 ERP'}
        return {'code': code, 'available': True, 'reason': ''}

    def _receipt_order_dates(self, profile):
        """Dates already frozen by the verified receipt allocation ledger.

        This is only a bootstrap hint for historical state-500 documents that
        predate the lifecycle table.  Ambiguous dates are deliberately omitted.
        """
        with self.store.connect() as db:
            saved = db.execute(
                "SELECT value FROM settings WHERE key='receipt_ledger_v1'"
            ).fetchone()
        if not saved:
            return {}
        try:
            state = json.loads(saved[0])
        except (TypeError, ValueError):
            return {}
        if not isinstance(state, dict) or str(state.get('profile_id')) != str(profile):
            return {}
        found = {}
        for encoded in state.get('orders', {}):
            try:
                identity = json.loads(encoded)
            except (TypeError, ValueError):
                continue
            if not isinstance(identity, list) or len(identity) != 4:
                continue
            number, value = _text(identity[0]), _text(identity[3])
            if number and value:
                found.setdefault(number, set()).add(value)
        return {number: next(iter(values)) for number, values in found.items()
                if len(values) == 1}

    def _delivery_dates(self, profile):
        with self.store.connect() as db:
            rows = db.execute('''SELECT order_no,delivery_date
                FROM erp_order_delivery_cache WHERE profile_id=?''',
                (str(profile),)).fetchall()
        return {row['order_no']: row['delivery_date'] for row in rows}

    def prepare(self, client, profile, current_rows, observed_at,
                shipped_rows=(), excluded_order_ids=()):
        profile = str(profile)
        excluded = {_text(value) for value in excluded_order_ids if _text(value)}
        prior = self._load(profile)
        if not prior:
            prior = self._bootstrap(profile, observed_at)
        current = {}
        for raw in current_rows:
            number = order_no(raw)
            if not number or number in current:
                raise LifecycleQueryError('生命周期输入订单编号缺失或重复')
            current[number] = raw
        shipped = {}
        for raw in shipped_rows:
            number = order_no(raw)
            if not number or number in shipped:
                raise LifecycleQueryError('需结存集合订单编号缺失或重复')
            if process_state(raw) != '500':
                raise LifecycleQueryError('bill_status=6 返回了非 process_state=500 订单')
            shipped[number] = raw

        # Only rows observed in this update are staged and rewritten.  Archived
        # history remains durable in SQLite but no longer expands every future
        # sync payload or write transaction.
        updates = {}
        source_rows = {}
        shadow_ids = set()
        release_ids = set()
        exact_checked = []
        errors = []

        for number in sorted(excluded & set(prior)):
            updates[number] = _state_600(prior[number], observed_at, force=True)
            updates[number]['process_state'] = prior[number].get('process_state', '')
            updates[number]['last_error'] = 'configured_ignored'
            release_ids.add(number)

        for number, raw in sorted(current.items()):
            if number in excluded:
                continue
            old = copy.deepcopy(prior.get(number) or _empty(profile, number, observed_at))
            state = process_state(raw)
            if number in shipped or state == '500':
                raw = shipped.get(number, raw)
                updates[number] = _state_500(old, observed_at)
                shadow_ids.add(number)
                if not old.get('business_snapshot'):
                    source_rows[number] = raw
            elif state == '600':
                updates[number] = _state_600(old, observed_at)
                if updates[number]['lifecycle_state'] == 'archived':
                    release_ids.add(number)
                else:
                    shadow_ids.add(number)
            else:
                updates[number] = _active(old, state or '400', observed_at)
                source_rows[number] = raw

        # bill_status=6 is a fast, complete set in this account.  It also
        # initializes historical state-500 documents that are no longer in the
        # active-work query.
        for number, raw in sorted(shipped.items()):
            if number in excluded or number in current:
                continue
            old = copy.deepcopy(prior.get(number) or _empty(profile, number, observed_at))
            updates[number] = _state_500(old, observed_at)
            shadow_ids.add(number)
            if not old.get('business_snapshot'):
                source_rows[number] = raw

        monitored = {number for number, row in prior.items()
                     if row.get('lifecycle_state') in {
                         'candidate', 'shipped_unposted', 'release_pending'}}
        vanished = {number for number, row in prior.items()
                    if row.get('lifecycle_state') == 'active' and number not in current}
        # Only documents that disappeared from both complete current sets need
        # one exact query.  In particular, never enumerate process_state=600.
        probes = sorted((monitored | vanished) - set(current) - set(shipped) - excluded)
        for number in probes:
            old = copy.deepcopy(prior[number])
            exact_checked.append(number)
            try:
                raw = _query_exact(client, profile, number)
                state = process_state(raw)
                if state == '400':
                    updates[number] = _active(old, state, observed_at)
                    source_rows[number] = raw
                elif state == '500':
                    updates[number] = _state_500(old, observed_at)
                    shadow_ids.add(number)
                else:
                    updates[number] = _state_600(old, observed_at)
                    if updates[number]['lifecycle_state'] == 'archived':
                        release_ids.add(number)
                    else:
                        shadow_ids.add(number)
            except OrderNotFound:
                # Both complete live sets excluded this order, and the exact
                # query explicitly confirms absence. Old snapshots must not
                # continue consuming stock or hold newly entered orders.
                updates[number] = _absent(old, observed_at)
                release_ids.add(number)
            except (ErpUnavailable, LifecycleQueryError) as exc:
                row = copy.deepcopy(old)
                row.update(last_error=str(exc), updated_at=observed_at)
                updates[number] = row
                if row.get('business_snapshot'):
                    shadow_ids.add(number)
                else:
                    errors.append({'order_no': number, 'code': 'lifecycle_snapshot_missing',
                                   'reason': '消失订单状态无法核对且缺少上次业务快照'})

        confirmed = [number for number in shadow_ids
                     if updates[number].get('lifecycle_state') == 'shipped_unposted']
        freight = self._freight_mapping(client) if confirmed else {
            'code': '', 'available': False,
            'reason': '状态尚未连续确认，本次不写回 ERP',
        }
        stage = {
            'profile_id': profile, 'observed_at': observed_at,
            'updates': updates, 'shadow_order_ids': sorted(shadow_ids),
            'release_order_ids': sorted(release_ids),
            'exact_checked_order_ids': exact_checked,
            'freight': freight, 'errors': errors,
            'order_date_hints': self._receipt_order_dates(profile),
            'delivery_date_hints': self._delivery_dates(profile),
            'delivery_confirmations': delivery_confirmations.load(self.store, profile),
            'delivery_date_updates': {},
            'shipped_set_count': len(shipped),
        }
        return list(source_rows.values()), stage

    @staticmethod
    def finalize(stage, orders, records):
        """Add persisted shadow demand and freeze current business snapshots."""
        orders_by_number = {_text(order.get('order_no')): copy.deepcopy(order) for order in orders}
        records_by_number = {}
        for row in records:
            records_by_number.setdefault(_text(row.get('order_no')), []).append(copy.deepcopy(row))
        updates = stage['updates']

        # Freeze every freshly standardized document before adding lifecycle
        # presentation fields.  This becomes the source of any future shadow.
        for number, order in orders_by_number.items():
            rows = records_by_number.get(number, [])
            snapshot = {'order': _clean_business(order),
                        'records': _clean_business(rows)}
            entry = updates[number]
            entry['business_snapshot'] = snapshot
            entry['content_hash'] = hashlib.sha256(_canonical(snapshot).encode()).hexdigest()

        for number in stage['shadow_order_ids']:
            if number not in orders_by_number:
                snapshot = updates[number].get('business_snapshot') or {}
                order = snapshot.get('order')
                rows = snapshot.get('records')
                if not isinstance(order, dict) or not isinstance(rows, list) or not rows:
                    stage['errors'].append({'order_no': number,
                        'code': 'lifecycle_snapshot_missing',
                        'reason': '需结存订单缺少可供 FIFO 占用的上次业务快照'})
                    continue
                orders_by_number[number] = copy.deepcopy(order)
                records_by_number[number] = copy.deepcopy(rows)

        if stage['errors']:
            raise LifecycleQueryError(stage['errors'][0]['reason'])

        for number, entry in updates.items():
            if number not in orders_by_number:
                continue
            is_shadow = number in stage['shadow_order_ids']
            state = entry['lifecycle_state']
            delivery_date = _text(orders_by_number[number].get('delivery_date'))
            pending_review = bool(entry.get('last_error')) and is_shadow
            release_pending = state == 'release_pending' and is_shadow
            if (state == 'active' and not is_shadow and
                    not orders_by_number[number].get('delivery_date_confirmation_id') and
                    orders_by_number[number].get('delivery_date')):
                stage['delivery_date_updates'][number] = _delivery_day(
                    orders_by_number[number]['delivery_date'])
            attributes = {
                'lifecycle_state': state,
                'process_state': entry.get('process_state', ''),
                'lifecycle_confirmations': int(entry.get('confirmation_count') or 0),
                'lifecycle_error': entry.get('last_error', ''),
                'lifecycle_pending_review': pending_review or release_pending,
                'lifecycle_release_pending': release_pending,
                'lifecycle_order_date_fallback': bool(
                    orders_by_number[number].get('allocation_order_date') and
                    not orders_by_number[number].get('order_date')),
                'lifecycle_print_blocked': is_shadow,
                'shadow_demand': is_shadow,
                'lifecycle_freight_code': stage['freight']['code'] if
                    state == 'shipped_unposted' else '',
                'lifecycle_write_ready': bool(state == 'shipped_unposted' and
                    stage['freight']['available'] and not pending_review and
                    orders_by_number[number].get('order_date')),
            }
            orders_by_number[number].update(attributes)
            for row in records_by_number.get(number, []):
                row.update(attributes)
                # WMS summaries normally let query-time parsing recover this
                # field. Historical state-500 rows can have only a reviewed
                # delivery-date cache, so publish the verified date directly
                # for App display, filtering and sorting.
                if delivery_date:
                    row['delivery_date'] = delivery_date
                if is_shadow:
                    row.update(printable=False, print_blocked=True,
                        print_block_reason=('订单状态待核对，暂停包装打印'
                            if pending_review or release_pending else
                            '已出库未结存订单不参与包装打印'))
                    if pending_review or release_pending:
                        row['status'] = '订单状态待核对'
                    elif state == 'candidate':
                        row['status'] = '已出库未结存（待连续确认）'
                    else:
                        row['status'] = SHIPPED_UNPOSTED_NAME

        final_orders = [orders_by_number[number] for number in sorted(orders_by_number)]
        final_records = [row for number in sorted(records_by_number)
                         for row in sorted(records_by_number[number],
                                           key=lambda item: (_text(item.get('line_id')),
                                                             _text(item.get('id'))))]
        stage['order_date_fallback_ids'] = sorted(
            number for number, order in orders_by_number.items()
            if number in stage['shadow_order_ids'] and
            order.get('allocation_order_date') and not order.get('order_date'))
        return final_orders, final_records, stage

    @staticmethod
    def persist(db, stage):
        profile = str(stage['profile_id'])
        for number, row in sorted(stage['updates'].items()):
            if row.get('lifecycle_state') not in STATES:
                raise ValueError('订单生命周期状态无效')
            previous = db.execute('''SELECT lifecycle_state,process_state,last_error
                FROM order_lifecycle_state WHERE profile_id=? AND order_no=?''',
                (profile, number)).fetchone()
            before = previous['lifecycle_state'] if previous else ''
            changed = (previous is None or before != row['lifecycle_state'] or
                       previous['process_state'] != row.get('process_state', '') or
                       previous['last_error'] != row.get('last_error', ''))
            snapshot = row.get('business_snapshot') if isinstance(row.get('business_snapshot'), dict) else {}
            db.execute('''INSERT INTO order_lifecycle_state(
                    profile_id,order_no,lifecycle_state,process_state,confirmation_count,
                    first_seen_500_at,last_confirmed_at,ended_at,last_observed_at,
                    last_business_snapshot,content_hash,last_error,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id,order_no) DO UPDATE SET
                    lifecycle_state=excluded.lifecycle_state,
                    process_state=excluded.process_state,
                    confirmation_count=excluded.confirmation_count,
                    first_seen_500_at=excluded.first_seen_500_at,
                    last_confirmed_at=excluded.last_confirmed_at,
                    ended_at=excluded.ended_at,
                    last_observed_at=excluded.last_observed_at,
                    last_business_snapshot=excluded.last_business_snapshot,
                    content_hash=excluded.content_hash,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at''',
                (profile, number, row['lifecycle_state'], row.get('process_state', ''),
                 int(row.get('confirmation_count') or 0), row.get('first_seen_500_at', ''),
                 row.get('last_confirmed_at', ''), row.get('ended_at', ''),
                 row.get('last_observed_at', ''), dumps(snapshot), row.get('content_hash', ''),
                 row.get('last_error', ''), row.get('updated_at') or stage['observed_at']))
            if changed:
                observation = row.get('last_error') or ('process_state=' + row.get('process_state', ''))
                db.execute('INSERT INTO order_lifecycle_audit VALUES (?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, profile, number, before, row['lifecycle_state'],
                     row.get('process_state', ''), observation, stage['observed_at']))
        _persist_delivery_dates(db, profile,
            stage.get('delivery_date_updates', {}), 'active_wms_summary',
            stage['observed_at'], replace=True)


def release_receipt_orders(state, order_nos):
    """Release provenance after confirmed posting or verified ERP removal."""
    if state is None or not order_nos:
        return state, []
    output = copy.deepcopy(state)
    wanted = {_text(value) for value in order_nos if _text(value)}
    released = []
    for encoded in list(output.get('orders', {})):
        try:
            identity = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        if isinstance(identity, list) and identity and _text(identity[0]) in wanted:
            output['orders'].pop(encoded, None)
            released.append(encoded)
    return output, released


def lifecycle_plan(order, freight):
    """Build the local lifecycle classification; code is dictionary-derived only."""
    number = _text(order.get('order_no'))
    if order.get('lifecycle_pending_review'):
        return {
            'tradeid': number, 'freightname': PENDING_REVIEW_NAME,
            'freightcode': '', 'freightnumber': '',
            'rule_version': '5-api-3-lifecycle', 'write_enabled': False,
        }
    try:
        shipping = date.fromisoformat(_text(order.get('delivery_date')))
        freightnumber = f'@{shipping:%m/%d}#{number}'
    except ValueError:
        freightnumber = ''
    ready = bool(order.get('lifecycle_write_ready') and freight.get('available') and
                 freight.get('code') and freightnumber)
    return {
        'tradeid': number, 'freightname': SHIPPED_UNPOSTED_NAME,
        'freightcode': freight['code'] if ready else '',
        'freightnumber': freightnumber, 'rule_version': '5-api-3-lifecycle',
        'write_enabled': ready,
    }
