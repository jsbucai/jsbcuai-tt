"""Whole-order print coordination shared by queries and print jobs."""
import hashlib
import json
import re
import uuid
from datetime import date

from .fields import order_fields, order_total
from .rules import note_delivery_date, text
from .store import timestamp


PRINT_STATES = frozenset({'unprinted', 'reserved', 'printed', 'uncertain'})


class OrderStateConflict(Exception):
    pass


def ensure_schema(store):
    """Own additive print-state schema without coupling it to ERP migrations."""
    with store.connect(write=True) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS order_print_states(
                profile_id TEXT NOT NULL, order_no TEXT NOT NULL, state TEXT NOT NULL,
                device_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL, PRIMARY KEY(profile_id,order_no));
            CREATE INDEX IF NOT EXISTS order_print_state_status
                ON order_print_states(state,updated_at);
            CREATE TABLE IF NOT EXISTS order_print_audit(
                id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, order_no TEXT NOT NULL,
                from_state TEXT NOT NULL DEFAULT '', to_state TEXT NOT NULL,
                device_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',actor_name TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS order_print_audit_order
                ON order_print_audit(profile_id,order_no,created_at);
        ''')


def order_ref(item):
    return text(item.get('profile_id')), text(item.get('order_no'))


def order_fingerprint(items):
    """Hash only label-affecting source content, independent of API row order."""
    rows = []
    for item in items:
        fields = order_fields(item)
        rows.append({
            'id': text(item.get('id')),
            'line_id': text(item.get('line_id')),
            'fields': fields,
            'total': text(order_total(item)),
        })
    rows.sort(key=lambda row: (row['line_id'], row['id'], row['fields'].get('sku', '')))
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def natural_location_key(value):
    """Comparable key where A2 sorts before A10 and blank locations sort last."""
    value = text(value).casefold()
    if not value:
        return ((2, ''),)
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r'(\d+)', value) if part)


def location_sort_key(value):
    """Stable JSON-friendly representation exposed to clients for diagnostics."""
    if not text(value):
        return '~'
    return ''.join(f'{int(part):020d}' if part.isdigit() else part.casefold()
                   for part in re.split(r'(\d+)', text(value)) if part)


def normalized_order_dates(item):
    def iso_day(value):
        raw = text(value)[:10]
        try:
            return date.fromisoformat(raw).isoformat() if raw else ''
        except ValueError:
            return ''

    order_date = iso_day(item.get('order_date') or item.get('document_date'))
    delivery_date = iso_day(item.get('delivery_date'))
    if not delivery_date:
        try:
            delivery_date = note_delivery_date(item.get('summary') or item.get('system_note')) or ''
        except ValueError:
            delivery_date = ''
    return order_date, delivery_date


def _all_order_groups(store, wanted=None):
    groups = {}
    restricted = wanted is not None
    wanted = set(wanted or ())
    for item in store.rows('orders'):
        ref = order_ref(item)
        if not restricted or ref in wanted:
            groups.setdefault(ref, []).append(item)
    return groups


def _audit(db, ref, before, after, device_id='', job_id='', reason='', actor=None):
    db.execute('''INSERT INTO order_print_audit
        (id,profile_id,order_no,from_state,to_state,device_id,job_id,reason,created_at,actor_id,actor_name)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (uuid.uuid4().hex, ref[0], ref[1], before, after, text(device_id), text(job_id),
         text(reason), timestamp(), text((actor or {}).get('id')),
         text((actor or {}).get('display_name') or (actor or {}).get('username'))))


def state_revision(row, ref):
    value = {key: (row[key] if row else '') for key in
             ('state', 'device_id', 'job_id', 'fingerprint', 'reason', 'updated_at')}
    if not row:
        value['state'] = 'unprinted'
    value.update(profile_id=ref[0], order_no=ref[1])
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def print_state_details(db, refs):
    """Server print facts; old records without an actor stay explicitly blank."""
    refs = set(refs)
    rows, audit, jobs = {}, {}, {}
    ordered = sorted(refs)
    for start in range(0, len(ordered), 400):
        part = ordered[start:start+400]
        values = ','.join('(?,?)' for _ in part)
        args = [value for ref in part for value in ref]
        for r in db.execute(f"WITH wanted(profile_id,order_no) AS (VALUES {values}) SELECT s.* FROM wanted w JOIN order_print_states s USING(profile_id,order_no)", args):
            rows[(r['profile_id'],r['order_no'])] = dict(r)
        for r in db.execute(f"""WITH wanted(profile_id,order_no) AS (VALUES {values})
            SELECT a.* FROM wanted w JOIN order_print_audit a ON a.rowid=(
                SELECT x.rowid FROM order_print_audit x WHERE x.profile_id=w.profile_id AND x.order_no=w.order_no
                AND x.to_state IN ('printed','uncertain') AND x.reason IN ('submitted','transport_unknown')
                ORDER BY x.created_at DESC,x.rowid DESC LIMIT 1)""", args):
            audit[(r['profile_id'],r['order_no'])] = dict(r)
    job_ids = sorted({r['job_id'] for r in rows.values() if r['job_id']})
    for start in range(0,len(job_ids),400):
        part=job_ids[start:start+400]
        jobs.update((r['id'],r) for r in db.execute('SELECT id,status,metadata FROM print_jobs WHERE id IN ('+','.join('?' for _ in part)+')',part))
    result = {}
    for ref in refs:
        row = rows.get(ref)
        actor_id, actor_name, printed_at = '', '', ''
        lock_actor_id, lock_actor_name = '', ''
        if ref in audit:
            fact = audit[ref]
            actor_id, actor_name, printed_at = fact.get('actor_id', ''), fact.get('actor_name', ''), fact['created_at']
        if row and row['job_id']:
            job = jobs.get(row['job_id'])
            if job:
                meta = json.loads(job['metadata'])
                # A reservation's owner identifies the active lock; completed
                # legacy jobs are never assigned a guessed employee.
                if row['state'] == 'reserved':
                    lock_actor_id, lock_actor_name = text(meta.get('actor_id')), text(meta.get('actor_name'))
                else:
                    actor_id = text(meta.get('actor_id')) or actor_id
                    actor_name = text(meta.get('actor_name')) or actor_name
                if row['state'] in {'printed', 'uncertain'}:
                    printed_at = printed_at or text(meta.get('updated_at'))
        result[ref] = {**(row or {}), 'state': row['state'] if row else 'unprinted',
                       'print_state': row['state'] if row else 'unprinted',
                       'state_revision': state_revision(row, ref),
                       'label_fingerprint': row['fingerprint'] if row else '',
                       'latest_print_actor_id': actor_id, 'latest_print_actor_name': actor_name,
                       'latest_print_at': printed_at, 'current_print_actor_id': lock_actor_id,
                       'current_print_actor_name': lock_actor_name,
                       'print_job_revoked': bool(row and row['job_id'] in jobs and jobs[row['job_id']]['status']=='revoked')}
    return result


def _lock_message(db, ref):
    fact = print_state_details(db, {ref})[ref]
    who = fact['current_print_actor_name'] or text(fact.get('device_id')) or '另一台设备'
    return f'订单 {ref[1]} 正由 {who} 打印，不能重复占用'


def _state_rows(store, refs):
    if not refs:
        return {}
    with store.connect() as db:
        rows = db.execute('SELECT * FROM order_print_states').fetchall()
    return {(row['profile_id'], row['order_no']): dict(row) for row in rows if
            (row['profile_id'], row['order_no']) in refs}


def invalidate_changed_print_state(db, profile_id, order_no, current_fingerprint):
    """Invalidate stale completed/unknown output inside the caller's transaction.

    An active reservation belongs to its frozen job and is deliberately left
    intact; claim-time validation will cancel it before any bytes are sent.
    """
    ref = (text(profile_id), text(order_no))
    row = db.execute('''SELECT * FROM order_print_states
        WHERE profile_id=? AND order_no=?''', ref).fetchone()
    if (not row or row['state'] not in {'printed', 'uncertain'} or
            not current_fingerprint or row['fingerprint'] == current_fingerprint):
        return False
    now = timestamp()
    db.execute('''UPDATE order_print_states SET state='unprinted',device_id='',job_id='',
        fingerprint=?,reason='content_changed',updated_at=?
        WHERE profile_id=? AND order_no=?''',
        (current_fingerprint, now, ref[0], ref[1]))
    _audit(db, ref, row['state'], 'unprinted', reason='content_changed')
    return True


def annotate_print_states(store, items):
    """Attach the effective whole-order state to order rows.

    Printed/uncertain states are invalidated when current label content changes.
    Active reservations remain attached to their frozen job until it reports a
    result, preserving the existing one-shot delivery contract.
    """
    refs = {order_ref(item) for item in items if text(item.get('order_no'))}
    groups = _all_order_groups(store, refs)
    fingerprints = {ref: order_fingerprint(rows) for ref, rows in groups.items()}
    eligible_refs = printable_order_refs(
        [item for group in groups.values() for item in group])
    states = _state_rows(store, refs)
    changed = []
    for ref, state in states.items():
        current = fingerprints.get(ref, '')
        if (state['state'] in {'printed', 'uncertain'} and current and
                state['fingerprint'] != current and ref in eligible_refs):
            changed.append((ref, state, current))
    if changed:
        with store.connect(write=True) as db:
            for ref, state, current in changed:
                updated = timestamp()
                cursor = db.execute('''UPDATE order_print_states SET state='unprinted',device_id='',job_id='',
                    fingerprint=?,reason='content_changed',updated_at=?
                    WHERE profile_id=? AND order_no=? AND state=? AND fingerprint=?''',
                    (current, updated, ref[0], ref[1], state['state'], state['fingerprint']))
                if cursor.rowcount:
                    _audit(db, ref, state['state'], 'unprinted', reason='content_changed')
                    state.update(state='unprinted', device_id='', job_id='', fingerprint=current,
                                 reason='content_changed', updated_at=updated)
    with store.connect() as db:
        details = print_state_details(db, refs)
    output = []
    for item in items:
        row = dict(item)
        ref = order_ref(row)
        state = states.get(ref)
        current = fingerprints.get(ref, '')
        row.update({key: value for key, value in details.get(ref, {}).items()
                    if key in {'state_revision', 'latest_print_actor_id', 'latest_print_actor_name', 'latest_print_at'}})
        row['print_state'] = state['state'] if state else 'unprinted'
        row['print_state_updated_at'] = state['updated_at'] if state else ''
        row['print_device_id'] = state['device_id'] if state else ''
        row['print_state_reason'] = state['reason'] if state else ''
        row['print_content_changed'] = bool(state and state['reason'] == 'content_changed')
        row['label_fingerprint'] = current
        row['print_queue_eligible'] = ref in eligible_refs
        output.append(row)
    return output


def _truthy(value):
    return value is True or value == 1 or text(value).lower() in {'1', 'true', 'yes', 'on'}


def _line_print_eligible(item):
    """Whether a current order line may participate in packaging work."""
    lifecycle = text(item.get('lifecycle_state'))
    process_state = text(item.get('process_state'))
    return not (
        item.get('printable') is False or _truthy(item.get('print_blocked')) or
        _truthy(item.get('lifecycle_print_blocked')) or _truthy(item.get('shadow_demand')) or
        lifecycle in {'candidate', 'shipped_unposted', 'archived'} or
        process_state in {'500', '600'} or
        text(item.get('logistics_company')) == '已完成订单需结存'
    )


def printable_order_refs(items):
    """Return whole orders whose every visible line is eligible to print."""
    eligibility = {}
    for item in items:
        ref = order_ref(item)
        if ref[1]:
            line_eligible = (bool(item.get('print_queue_eligible'))
                             if 'print_queue_eligible' in item else _line_print_eligible(item))
            eligibility[ref] = eligibility.get(ref, True) and line_eligible
    return {ref for ref, eligible in eligibility.items() if eligible}


def filter_print_state(items, state):
    """Filter packaging queues while retaining completed-print history.

    Lifecycle-blocked rows remain available through the default ``all`` view.
    A prior ``printed`` state remains a historical fact even if the order later
    becomes non-printable; active work queues exclude all other blocked rows.
    """
    if state == 'all':
        return list(items)
    eligible = printable_order_refs(items)
    return [item for item in items if item.get('print_state', 'unprinted') == state and
            (state == 'printed' or order_ref(item) in eligible)]


def print_state_counts(items):
    eligible = printable_order_refs(items)
    by_order = {}
    for item in items:
        ref = order_ref(item)
        if ref[1]:
            by_order[ref] = item.get('print_state', 'unprinted')
    counts = {state: 0 for state in ('unprinted', 'reserved', 'printed', 'uncertain')}
    for ref, state in by_order.items():
        state = state if state in counts else 'unprinted'
        if ref in eligible or state == 'printed':
            counts[state] += 1
    return {'all': sum(counts.values()), **counts}


def reserve_orders(db, orders, device_id, job_id, actor=None, expected=None):
    """Atomically reserve complete orders; caller owns the write transaction."""
    now = timestamp()
    for value in orders:
        ref = (text(value.get('profile_id')), text(value.get('order_no')))
        fingerprint = text(value.get('fingerprint'))
        row = db.execute('SELECT * FROM order_print_states WHERE profile_id=? AND order_no=?', ref).fetchone()
        if expected is not None:
            if row and row['state'] == 'reserved':
                raise OrderStateConflict(_lock_message(db, ref))
            fact = expected.get(ref)
            if not fact or fact['state_revision'] != state_revision(row, ref):
                raise OrderStateConflict(f'订单 {ref[1]} 的打印状态已变化，请重新预览确认')
            value['prior_print_state'] = dict(row) if row else None
        if row and row['state'] != 'unprinted' and expected is None:
            # A changed completed label is a new print obligation. Reservations
            # cannot be superseded because another device may already be printing.
            if row['state'] in {'printed', 'uncertain'} and row['fingerprint'] != fingerprint:
                _audit(db, ref, row['state'], 'unprinted', reason='content_changed')
            else:
                if row['state'] == 'reserved':
                    raise OrderStateConflict(_lock_message(db, ref))
                label = {'reserved': '打印中', 'printed': '已打印', 'uncertain': '待核对'}.get(row['state'], row['state'])
                raise OrderStateConflict(f'订单 {ref[1]} 当前为{label}，请刷新或先恢复为未打印')
        before = 'unprinted' if row and row['state'] in {'printed', 'uncertain'} and row['fingerprint'] != fingerprint else row['state'] if row else 'unprinted'
        db.execute('''INSERT INTO order_print_states
            (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(profile_id,order_no) DO UPDATE SET state=excluded.state,
            device_id=excluded.device_id,job_id=excluded.job_id,fingerprint=excluded.fingerprint,
            reason=excluded.reason,updated_at=excluded.updated_at''',
            (ref[0], ref[1], 'reserved', device_id, job_id, fingerprint, '', now))
        _audit(db, ref, before, 'reserved', device_id, job_id, 'prepare', actor)


def finish_orders(db, orders, device_id, job_id, result_status, actor=None):
    target = {'submitted': 'printed', 'unknown': 'uncertain',
              'cancelled': 'unprinted', 'failed': 'unprinted'}[result_status]
    reason = {'submitted': 'submitted', 'unknown': 'transport_unknown',
              'cancelled': 'cancelled', 'failed': 'failed'}[result_status]
    now = timestamp()
    for value in orders:
        ref = (text(value.get('profile_id')), text(value.get('order_no')))
        row = db.execute('SELECT * FROM order_print_states WHERE profile_id=? AND order_no=?', ref).fetchone()
        if not row or row['state'] != 'reserved' or row['job_id'] != job_id:
            raise OrderStateConflict(f'订单 {ref[1]} 的打印占用已变化，请人工核对')
        prior = value.get('prior_print_state')
        restore = result_status in {'cancelled', 'failed'} and isinstance(prior, dict)
        next_state = prior['state'] if restore else target
        next_device = prior['device_id'] if restore else device_id if target != 'unprinted' else ''
        next_job = prior['job_id'] if restore else job_id if target != 'unprinted' else ''
        next_reason = prior['reason'] if restore else reason
        next_fingerprint = prior['fingerprint'] if restore else row['fingerprint']
        db.execute("""UPDATE order_print_states SET state=?,device_id=?,job_id=?,reason=?,
            fingerprint=?,updated_at=? WHERE profile_id=? AND order_no=?""",
            (next_state, next_device, next_job, next_reason, next_fingerprint,
             now, ref[0], ref[1]))
        _audit(db, ref, 'reserved', next_state, device_id, job_id,
               'reprint_' + reason + '_restored' if restore else reason, actor)


def current_orders(store, refs):
    groups = _all_order_groups(store, refs)
    return groups, [{'profile_id': ref[0], 'order_no': ref[1],
                     'fingerprint': order_fingerprint(rows)}
                    for ref, rows in sorted(groups.items())]


def reset_order(store, order_no, profile_id='', device_id='', reason='manual_reset',
                access_checker=None, actor=None, idempotency_key='',
                expected_state_revision=None, expected_label_fingerprint=None):
    order_no, profile_id = text(order_no), text(profile_id)
    if not order_no:
        raise ValueError('缺少订单编号')
    actor_id = text((actor or {}).get('id'))
    guarded = expected_state_revision is not None or expected_label_fingerprint is not None or bool(idempotency_key)
    if guarded and (not 16 <= len(idempotency_key) <= 128 or not expected_state_revision or not expected_label_fingerprint):
        raise ValueError('恢复未打印需提供完整版本和操作标识')
    request_hash = hashlib.sha256(json.dumps([order_no, profile_id, device_id, reason,
        expected_state_revision, expected_label_fingerprint], ensure_ascii=False).encode()).hexdigest()
    now = timestamp()
    with store.connect(write=True) as db:
        if guarded:
            replay = db.execute('SELECT * FROM order_print_reset_requests WHERE actor_id=? AND idempotency_key=?',
                                (actor_id, idempotency_key)).fetchone()
            if replay:
                if replay['request_fingerprint'] != request_hash:
                    raise OrderStateConflict('恢复操作标识已用于不同订单或版本')
                return {**json.loads(replay['result_json']), 'replayed': True}
        groups = {}
        for record in db.execute("SELECT payload FROM records WHERE kind='orders'"):
            try:
                item = json.loads(record['payload'])
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                ref = order_ref(item)
                groups.setdefault(ref, []).append(item)
        state_refs = {(row['profile_id'], row['order_no']) for row in db.execute(
            'SELECT profile_id,order_no FROM order_print_states WHERE order_no=?', (order_no,))}
        candidates = {ref for ref in set(groups) | state_refs
                      if ref[1] == order_no and (not profile_id or ref[0] == profile_id)}
        if not candidates:
            raise KeyError('订单不存在')
        if len(candidates) > 1:
            raise ValueError('订单编号存在于多个账套，请提供 profile_id')
        ref = next(iter(candidates))
        items = groups.get(ref) or [{'profile_id': ref[0], 'order_no': ref[1]}]
        if access_checker is not None:
            access_checker(db, items)
        fingerprint = order_fingerprint(groups[ref]) if ref in groups else ''
        row = db.execute('SELECT * FROM order_print_states WHERE profile_id=? AND order_no=?', ref).fetchone()
        if row and row['state'] == 'reserved':
            raise OrderStateConflict(_lock_message(db, ref))
        if guarded and (state_revision(row, ref) != expected_state_revision or
                        (fingerprint or (row['fingerprint'] if row else '')) != expected_label_fingerprint):
            raise OrderStateConflict('订单打印状态或标签已变化，请刷新后核对')
        if not row or row['state'] == 'unprinted':
            result = {'profile_id': ref[0], 'order_no': ref[1], 'print_state': 'unprinted',
                    'print_state_updated_at': row['updated_at'] if row else '', 'replayed': True,
                      'state_revision': state_revision(row, ref)}
            if guarded:
                db.execute('INSERT INTO order_print_reset_requests VALUES (?,?,?,?,?)',
                           (actor_id, idempotency_key, request_hash, json.dumps(result), now))
            return result
        db.execute('''UPDATE order_print_states SET state='unprinted',device_id='',job_id='',
            fingerprint=?,reason=?,updated_at=? WHERE profile_id=? AND order_no=?''',
            (fingerprint or row['fingerprint'], text(reason) or 'manual_reset', now, ref[0], ref[1]))
        _audit(db, ref, row['state'], 'unprinted', device_id, row['job_id'], text(reason) or 'manual_reset', actor)
        new_row = db.execute('SELECT * FROM order_print_states WHERE profile_id=? AND order_no=?', ref).fetchone()
        result = {'profile_id': ref[0], 'order_no': ref[1], 'print_state': 'unprinted',
                  'print_state_updated_at': now, 'replayed': False,
                  'state_revision': state_revision(new_row, ref)}
        if guarded:
            db.execute('INSERT INTO order_print_reset_requests VALUES (?,?,?,?,?)',
                       (actor_id, idempotency_key, request_hash, json.dumps(result), now))
    return result
