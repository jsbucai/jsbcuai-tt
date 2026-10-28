"""One-time, administrator-confirmed correction. Never invoked by ERP sync."""
import hashlib
import json
import uuid

from .store import dumps, timestamp
from .rules import text
from .order_print_state import order_ref, order_fingerprint, print_state_details, _audit
from .print_jobs import JobConflict, _database_data_version, _lifecycle_blocks_printing


REASON = '用户确认实际未打印'


def _admin(actor):
    if not actor or actor.get('is_admin') is not True or not text(actor.get('id')):
        raise JobConflict('仅管理员可以执行打印状态纠正', 'admin_required')


def _manifest(db, scope='unfulfilled', order_refs=None):
    if scope not in {'unfulfilled', 'stock_packing_processing', 'specified_orders'}:
        raise ValueError('未知打印纠正范围')
    selected = set()
    if scope == 'specified_orders':
        if not isinstance(order_refs, list) or not 1 <= len(order_refs) <= 100:
            raise ValueError('请指定 1 至 100 张订单及账套')
        for ref in order_refs:
            if not isinstance(ref, dict) or not text(ref.get('profile_id')) or not text(ref.get('order_no')):
                raise ValueError('指定订单必须包含账套和完整单号')
            selected.add((text(ref['profile_id']), text(ref['order_no'])))
    groups = {}
    for record in db.execute("SELECT payload FROM records WHERE kind='orders'"):
        row = json.loads(record['payload'])
        groups.setdefault(order_ref(row), []).append(row)
    lifecycle = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_lifecycle_state'").fetchone():
        lifecycle = {(r['profile_id'], r['order_no']): dict(r)
                     for r in db.execute('SELECT * FROM order_lifecycle_state')}
    packing_tasks = {}
    if scope == 'stock_packing_processing':
        for task in db.execute("SELECT * FROM daily_work_tasks WHERE role='stock_packing' AND condition_state='active' AND status<>'business_completed'"):
            ref = (task['profile_id'], task['order_no'])
            packing_tasks.setdefault(ref, []).append({'id': task['id'], 'version': task['version']})
    refs_to_check = set(packing_tasks) if scope == 'stock_packing_processing' else set(groups)
    if scope == 'specified_orders':
        if not selected <= set(groups):
            raise JobConflict('指定订单已不在当前未交清单，请重新核对', 'print_correction_conflict')
        refs_to_check = selected
    facts = print_state_details(db, refs_to_check)
    orders, excluded = [], []
    for ref in sorted(refs_to_check):
        rows = groups.get(ref, [])
        fact = facts[ref]
        content_review = fact['state'] == 'unprinted' and fact.get('reason') == 'content_changed'
        if fact['state'] not in {'printed', 'reserved', 'uncertain'} and not content_review:
            continue
        # This explicitly selected maintenance scope corrects physical print
        # facts even when the ERP source is pending/missing. It never clears
        # lifecycle blocks or reopens a confirmed completed order.
        completed = any(text(r.get('lifecycle_state')) in {'shipped_unposted', 'archived'}
                        for r in [*rows, lifecycle.get(ref, {})])
        blocked = (completed if scope == 'stock_packing_processing' else
                   any(r.get('shadow_demand') or _lifecycle_blocks_printing(r) for r in rows)
                   or _lifecycle_blocks_printing(lifecycle.get(ref, {})))
        if blocked:
            excluded.append({'profile_id': ref[0], 'order_no': ref[1], 'reason': '已出库或生命周期待核对'})
            continue
        orders.append({'profile_id': ref[0], 'order_no': ref[1], 'state': fact['state'],
                       'state_revision': fact['state_revision'], 'job_id': text(fact.get('job_id')),
                       'fingerprint': order_fingerprint(rows) if rows else text(fact.get('fingerprint')),
                       'line_count': len(rows)})
        if scope == 'stock_packing_processing':
            orders[-1]['tasks'] = sorted(packing_tasks[ref], key=lambda t: t['id'])
    refs = {(o['profile_id'], o['order_no']) for o in orders}
    jobs = []
    for row in db.execute("SELECT * FROM print_jobs WHERE status != 'revoked'"):
        meta = json.loads(row['metadata'])
        job_refs = {(text(o.get('profile_id')), text(o.get('order_no'))) for o in meta.get('orders', [])}
        if job_refs & refs:
            # A frozen transport cannot be resumed partly. Revoke its send
            # authority, while retaining every out-of-scope order fact verbatim.
            # Missing/shipped orders in yesterday's batch must not prevent the
            # explicitly requested correction of today's unfulfilled orders.
            jobs.append({'id': row['id'], 'status': row['status'],
                         'preserved_order_refs': [dict(profile_id=p, order_no=n)
                                                  for p, n in sorted(job_refs - refs)],
                         'metadata_hash': hashlib.sha256(row['metadata'].encode()).hexdigest()})
    return {**({'scope': scope} if scope != 'unfulfilled' else {}),
            **({'order_refs': [dict(profile_id=p, order_no=n) for p,n in sorted(selected)]}
               if scope == 'specified_orders' else {}),
            'data_version': _database_data_version(db), 'orders': orders,
            'jobs': sorted(jobs, key=lambda r: r['id']), 'excluded': excluded,
            'order_count': len(orders), 'line_count': sum(o['line_count'] for o in orders),
            'reason': REASON}


def preview(store, actor, scope='unfulfilled', order_refs=None):
    _admin(actor)
    with store.connect(write=True) as db:
        value = _manifest(db, scope, order_refs)
        digest = hashlib.sha256(dumps(value).encode()).hexdigest()
        identity = uuid.uuid4().hex
        db.execute('INSERT INTO print_state_corrections VALUES (?,?,?,?,NULL,?,?)',
                   (identity, actor['id'], dumps(value), digest, '', timestamp()))
        return {**value, 'correction_id': identity, 'preview_hash': digest}


def apply(store, actor, payload):
    _admin(actor)
    key = text(payload.get('idempotency_key'))
    if not 16 <= len(key) <= 128 or not payload.get('preview_hash'):
        raise ValueError('打印纠正需提供预览版本和幂等标识')
    with store.connect(write=True) as db:
        row = db.execute('SELECT * FROM print_state_corrections WHERE id=?',
                         (text(payload.get('correction_id')),)).fetchone()
        if not row or row['actor_id'] != actor['id'] or row['preview_hash'] != payload['preview_hash']:
            raise JobConflict('纠正预览不存在、属于其他管理员或版本已变化', 'print_correction_conflict')
        if row['result_json']:
            if row['idempotency_key'] != key:
                raise JobConflict('本批纠正已执行，请查看原结果', 'print_correction_conflict')
            return {**json.loads(row['result_json']), 'replayed': True}
        if db.execute('SELECT 1 FROM print_state_corrections WHERE idempotency_key=?', (key,)).fetchone():
            raise JobConflict('该操作标识已用于另一批纠正', 'print_correction_conflict')
        hold = db.execute("SELECT value FROM settings WHERE key='printing_maintenance'").fetchone()
        if not hold or hold[0] != 'true' or payload.get('devices_stopped') is not True:
            raise JobConflict('请先暂停打印并确认现场手机已退出旧打印流程', 'printing_maintenance_required')
        original_manifest = json.loads(row['payload_json'])
        current = _manifest(db, original_manifest.get('scope', 'unfulfilled'), original_manifest.get('order_refs'))
        if hashlib.sha256(dumps(current).encode()).hexdigest() != row['preview_hash']:
            raise JobConflict('未交清单或打印状态已变化，请重新生成纠正预览', 'print_correction_conflict')
        now = timestamp()
        for job in current['jobs']:
            original = db.execute('SELECT metadata FROM print_jobs WHERE id=?', (job['id'],)).fetchone()
            meta = json.loads(original[0])
            # Preserve all transport evidence, including original status. Do
            # not manufacture zero bytes or a "cancelled before sending" fact.
            meta.update(revoked_from_status=job['status'], revoked_by=actor['id'],
                        revoked_at=now, correction_id=row['id'], status='revoked',
                        correction_preserved_order_refs=job['preserved_order_refs'],
                        message=REASON + '，旧打印任务已撤销，不能继续发送')
            db.execute("UPDATE print_jobs SET status='revoked',metadata=?,claim_token_hash=NULL WHERE id=?",
                       (dumps(meta), job['id']))
        for order in current['orders']:
            ref = (order['profile_id'], order['order_no'])
            db.execute("""UPDATE order_print_states SET state='unprinted',device_id='',job_id='',
                fingerprint=?,reason=?,updated_at=? WHERE profile_id=? AND order_no=?""",
                (order['fingerprint'], 'correction:' + row['id'] + ':' + REASON, now, *ref))
            _audit(db, ref, order['state'], 'unprinted', job_id=order['job_id'],
                   reason='correction:' + row['id'] + ':' + REASON, actor=actor)
        # Invalidate selection/preview caches in the same transaction. Task
        # reconciliation itself can be retried without redoing the correction.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_work_settings'").fetchone():
            db.execute("UPDATE daily_work_settings SET value=CAST(value AS INTEGER)+1 WHERE key='task_version'")
        result = {'correction_id': row['id'], 'order_count': current['order_count'],
                  'line_count': current['line_count'], 'revoked_job_count': len(current['jobs']),
                  'already_unprinted_review_count': sum(o['state'] == 'unprinted' for o in current['orders']),
                  'completed_at': now, 'reason': REASON, 'replayed': False}
        db.execute('UPDATE print_state_corrections SET idempotency_key=?,result_json=? WHERE id=?',
                   (key, dumps(result), row['id']))
        return result
