"""Durable update queue; automatic jobs use read-only modes and keep history."""
import json
import threading
import uuid
import inspect
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .store import dumps, timestamp
from .logistics import calculate
from .api_source import SourceIncomplete, SourceChanged, validate_snapshot, publish
from .work_schedule import workday_slot, business_open
from .business_rules import calculate_snapshot
from .classification_pipeline import calculate_query_snapshot, allocation_scope
from .print_jobs import JobConflict
from .freight_write_policy import FreightWritePolicy
from .heavy_lane import exclusive_heavy_work
from .freight_reconciliation import (stamp, fingerprints, recheck_reason,
    recheck_priority, RECHECK_BATCH, RECHECK_AFTER)


def _freight_observed_changes(observation, cached, target):
    """Only a newer actual ERP observation can invalidate a success receipt.

    Old cached WMS data must never turn a successful write into an endless
    retry. Do not use Qimen's frequently empty logistics or WMS's unsupported
    company field as negative evidence.
    """
    written = stamp(cached['updated_at'])
    if written is None:
        return False
    if not isinstance(observation, dict):
        return False
    at, number = stamp(observation.get('observed_at')), observation.get('freightnumber')
    if not at or at <= written:
        return False
    if isinstance(number, str) and number != target['freightnumber']:
        return True
    # Missing/unsupported category fields are not evidence of erasure. When a
    # source actually supplies the category, same-number drift is meaningful.
    return any(isinstance(observation.get(key), str) and observation[key].strip()
        and observation[key] != target.get(key) for key in ('freightname', 'freightcode'))


class UpdateJobs:
    def __init__(self, store, source, sender=None, write_policy=None, after_publish=None):
        self.store, self.source, self.sender = store, source, sender
        self.after_publish = after_publish
        # Production always injects an explicit server policy.  The fallback
        # retains compatibility for isolated unit tests with a fake sender.
        self.write_policy = write_policy if write_policy is not None else (
            FreightWritePolicy.unbounded_for_injected_sender() if sender is not None
            else FreightWritePolicy.deny())
        self.wake, self.stop_event = threading.Event(), threading.Event()
        self.thread = self.lock_file = None
        with store.connect(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS update_jobs(id TEXT PRIMARY KEY, mode TEXT, status TEXT,
                    stage TEXT, result TEXT, created_at TEXT, updated_at TEXT);
                DROP INDEX IF EXISTS update_one_active;
                CREATE UNIQUE INDEX IF NOT EXISTS update_one_running ON update_jobs((1))
                    WHERE status='running';
                CREATE TABLE IF NOT EXISTS update_projection_retries(
                    job_id TEXT PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS update_schedule_triggers(
                    slot TEXT PRIMARY KEY,job_id TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS freight_cache(profile TEXT, tradeid TEXT, target TEXT,
                    version TEXT, updated_at TEXT, PRIMARY KEY(profile,tradeid));
                CREATE TABLE IF NOT EXISTS freight_attempts(id TEXT PRIMARY KEY,job_id TEXT,profile TEXT,
                    tradeid TEXT,target TEXT,status TEXT,created_at TEXT,updated_at TEXT);
                CREATE TABLE IF NOT EXISTS freight_reconciliation(
                    profile TEXT, tradeid TEXT, fingerprint TEXT, confirmed_at TEXT,
                    last_attempt_at TEXT, pending_fingerprint TEXT, pending_attempt_id TEXT,
                    PRIMARY KEY(profile,tradeid));
                CREATE TABLE IF NOT EXISTS logistics_calculations(job_id TEXT PRIMARY KEY,profile TEXT,
                    data_version TEXT,payload TEXT,created_at TEXT);
            ''')

    def enqueue(self, identity, mode='sync', request=None):
        try: identity = str(uuid.UUID(identity))
        except (ValueError, TypeError, AttributeError): raise ValueError('更新任务必须携带 UUID') from None
        if mode not in {'preview','update','sync','refresh','reconcile','inventory_subset'}:
            raise ValueError('更新模式无效')
        if mode == 'inventory_subset':
            if not isinstance(request, dict):
                raise ValueError('库存子集刷新缺少商品身份')
            frozen_request = {
                'product_ids': sorted({_value for item in request.get('product_ids', [])
                    if (_value := str(item or '').strip())}),
                'identities': sorted(request.get('identities', []),
                    key=lambda item:json.dumps(item,ensure_ascii=False,sort_keys=True)
                    if isinstance(item,dict) else str(item)),
            }
            if not frozen_request['product_ids'] and not frozen_request['identities']:
                raise ValueError('库存子集刷新缺少商品身份')
        else:
            if request not in (None, {}):
                raise ValueError('当前更新模式不接受库存子集')
            frozen_request = None
        if mode == 'update' and self.sender is None:
            raise JobConflict('物流写入尚未完成无发货副作用验收，当前仅可计算预览', 'capability_unavailable')
        if mode == 'update' and not self.write_policy.configured:
            raise JobConflict('物流写入未显式配置有限范围或全量范围，拒绝启动写入',
                              'write_policy_required')
        with self.store.connect(write=True) as db:
            if mode == 'update' and db.execute("SELECT 1 FROM settings WHERE key='restoration_hold' AND value='true'").fetchone():
                raise JobConflict('备份恢复后需核对期间写入记录，当前仅允许计算预览','restoration_hold')
            row = db.execute('SELECT * FROM update_jobs WHERE id=?',(identity,)).fetchone()
            if row:
                if row['mode'] != mode: raise JobConflict('任务标识已用于不同更新模式')
                if mode == 'inventory_subset' and json.loads(row['result']).get('request') != frozen_request:
                    raise JobConflict('任务标识已用于不同库存子集')
                return self.result(row)
            active = db.execute("SELECT * FROM update_jobs WHERE status IN ('queued','running')").fetchone()
            if active:
                if mode == 'inventory_subset':
                    active_request=json.loads(active['result']).get('request')
                    if active['mode']=='inventory_subset' and active_request==frozen_request:
                        return self.result(active)
                    raise JobConflict('其他更新任务正在运行，本次库存刷新未加入该任务',
                                      'update_busy')
                if active['mode'] != mode:
                    raise JobConflict('其他更新任务正在运行或排队，请稍后重试本次更新',
                                      'update_busy')
                return self.result(active)
            now = timestamp()
            initial = {'request': frozen_request} if frozen_request is not None else {}
            if mode in {'sync', 'update'}:
                # Freeze the boundary into the durable task.  Changing .env or
                # restarting the service cannot widen an already queued job.
                initial['write_policy'] = self.write_policy.freeze()
            db.execute('INSERT INTO update_jobs VALUES (?,?,\'queued\',\'queued\',?,?,?)',
                       (identity,mode,dumps(initial),now,now))
        self.wake.set()
        return self.get(identity)

    @staticmethod
    def automatic_slot(now=None):
        """Most recent of the three daily Shanghai-time refresh slots."""
        zone = ZoneInfo('Asia/Shanghai')
        now = now or datetime.now(zone)
        if now.tzinfo is None:
            raise ValueError('自动更新时刻必须包含时区')
        now = now.astimezone(zone)
        for hour, minute in ((16, 0), (11, 30), (7, 30)):
            slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot <= now:
                return slot.isoformat()
        return (now - timedelta(days=1)).replace(
            hour=16, minute=0, second=0, microsecond=0).isoformat()

    @staticmethod
    def monthly_reconcile_slot(now=None):
        """Most recent first-of-month 03:40 Shanghai-time reconciliation slot."""
        zone = ZoneInfo('Asia/Shanghai')
        now = now or datetime.now(zone)
        if now.tzinfo is None:
            raise ValueError('自动更新时刻必须包含时区')
        now = now.astimezone(zone)
        slot = now.replace(day=1, hour=3, minute=40, second=0, microsecond=0)
        if slot > now:
            previous_month = now.replace(day=1) - timedelta(days=1)
            slot = previous_month.replace(day=1, hour=3, minute=40,
                second=0, microsecond=0)
        return slot.isoformat()

    def enqueue_automatic(self, mode, slot=None):
        """Queue one read-only job per scheduled slot without deleting prior runs."""
        if mode not in {'refresh', 'reconcile'}:
            raise ValueError('自动更新模式无效')
        slot_for_mode = self.automatic_slot if mode == 'refresh' else self.monthly_reconcile_slot
        scheduled_for = slot_for_mode() if slot is None else slot_for_mode(
            datetime.fromisoformat(slot))
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL,
            'ul-workbench:auto:' + mode + ':' + scheduled_for))
        with self.store.connect(write=True) as db:
            row = db.execute('SELECT * FROM update_jobs WHERE id=?', (identity,)).fetchone()
            if row:
                if row['mode'] != mode:
                    raise JobConflict('自动更新时段已用于不同模式')
                return self._hydrate(row)
            now = timestamp()
            db.execute('INSERT INTO update_jobs VALUES (?,?,?,?,?,?,?)',
                (identity,mode,'queued','queued',dumps({
                    'automatic':True,'scheduled_for':scheduled_for}),now,now))
        self.wake.set()
        return self.get(identity)

    def enqueue_purchase_dawn(self, enabled=None, now=None):
        """One full reconcile at 03:10 Asia/Shanghai. Disabled unless explicitly enabled."""
        import os
        if enabled is None:
            enabled = os.environ.get('UL_PURCHASE_DAWN_FULL') == '1'
        if not enabled:
            return None
        scheduled = self.dawn_full_slot(now).isoformat()
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'ul-workbench:purchase-dawn:' + scheduled))
        with self.store.connect(write=True) as db:
            row = db.execute('SELECT * FROM update_jobs WHERE id=?', (identity,)).fetchone()
            if row:
                if row['mode'] != 'reconcile':
                    raise JobConflict('凌晨全量时段已用于不同模式')
                return self._hydrate(row)
            stamp = timestamp()
            db.execute('INSERT INTO update_jobs VALUES (?,?,?,?,?,?,?)',
                       (identity, 'reconcile', 'queued', 'queued', dumps({
                           'automatic': True, 'schedule': 'purchase-dawn', 'scheduled_for': scheduled,
                           'full_reconcile': True}), stamp, stamp))
        self.wake.set()
        return self.get(identity)

    @staticmethod
    def dawn_full_slot(now=None):
        zone = ZoneInfo('Asia/Shanghai')
        now = now or datetime.now(zone)
        if now.tzinfo is None:
            raise ValueError('自动更新时刻必须包含时区')
        now = now.astimezone(zone)
        slot = now.replace(hour=3, minute=10, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(days=1)
        return slot

    def enqueue_workday(self, now=None):
        """One active daytime refresh; retain every trigger without a backlog.

        07:30 preloads read-only. Daytime sync freezes the same already-enabled
        server write policy used by manual updates. Off-hours never catch up.
        """
        slot = workday_slot(now)
        if slot is None:
            return None
        mode = 'sync' if slot.hour >= 8 and self.write_policy.configured else 'refresh'
        scheduled = slot.isoformat()
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'ul-workbench:workday:' + scheduled))
        with self.store.connect(write=True) as db:
            old = db.execute('SELECT job_id FROM update_schedule_triggers WHERE slot=?', (scheduled,)).fetchone()
            if old:
                identity = old[0]
            else:
                # Do not extend a manually frozen write scope, or let a read
                # job stand in for requested logistics work.
                active = db.execute("SELECT * FROM update_jobs WHERE mode=? AND status IN ('queued','running') ORDER BY rowid LIMIT 1", (mode,)).fetchone()
                policy = self.write_policy.freeze() if mode == 'sync' else FreightWritePolicy.deny().freeze()
                reuse = active and (mode != 'sync' or json.loads(active['result']).get('write_policy') == policy)
                if reuse:
                    identity = active['id']
                else:
                    stamp = timestamp()
                    db.execute('INSERT INTO update_jobs VALUES (?,?,?,?,?,?,?)',
                        (identity, mode, 'queued', 'queued', dumps({'automatic': True,
                         'schedule': 'workday', 'scheduled_for': scheduled,
                         'write_policy': policy}), stamp, stamp))
                db.execute('INSERT INTO update_schedule_triggers VALUES (?,?,?)', (scheduled, identity, timestamp()))
        self.wake.set()
        return self.get(identity)

    @staticmethod
    def result(row):
        result = json.loads(row['result'])
        result.setdefault('attempts',[])
        return {k:row[k] for k in ('id','mode','status','stage','created_at','updated_at')} | {'result':result} | {
            k:result.get(k) for k in ('progress','data_version','source_updated_at','published_at')}


    def get(self, identity):
        with self.store.connect() as db: row = db.execute('SELECT * FROM update_jobs WHERE id=?',(identity,)).fetchone()
        if row is None: raise KeyError('更新任务不存在')
        return self._hydrate(row)

    def latest(self):
        with self.store.connect() as db: row = db.execute('SELECT * FROM update_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
        return self._hydrate(row) if row else None

    def history(self, limit=50, offset=0):
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM update_jobs ORDER BY rowid DESC LIMIT ? OFFSET ?',
                (limit,offset)).fetchall()
        return [self._hydrate(row) for row in rows]

    def _hydrate(self, row):
        job=self.result(row)
        job['result']['attempts']=self.attempts(row['id'],include_pending=True)
        return job

    @property
    def write_enabled(self):
        return self.sender is not None and self.write_policy.configured

    def status(self):
        from .storage_capacity import capacity
        return {'worker_running':bool(self.thread and self.thread.is_alive()), 'latest':self.latest(),
                'storage':capacity(self.store.runtime),
                'logistics_transport_enabled':self.sender is not None,
                'logistics_write_enabled':self.write_enabled,
                'logistics_write_policy':self.write_policy.freeze()}

    def mark(self, identity, status, stage, result=None):
        with self.store.connect(write=True) as db:
            if result is None:
                db.execute('UPDATE update_jobs SET status=?,stage=?,updated_at=? WHERE id=?',(status,stage,timestamp(),identity))
            else:
                db.execute('UPDATE update_jobs SET status=?,stage=?,result=?,updated_at=? WHERE id=?',(status,stage,dumps(result),timestamp(),identity))

    def recover(self):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE freight_attempts SET status='unknown',updated_at=? WHERE status='sending'",(timestamp(),))
            for row in db.execute("SELECT id FROM update_jobs WHERE status='running'").fetchall():
                # Once any request began, preserve its exact record; never replay a job.
                sent = db.execute('SELECT 1 FROM freight_attempts WHERE job_id=?',(row['id'],)).fetchone()
                db.execute('UPDATE update_jobs SET status=?,stage=?,updated_at=? WHERE id=?',
                    ('unknown' if sent else 'queued','interrupted' if sent else 'queued',timestamp(),row['id']))

    def attempts(self, identity, include_pending=False):
        with self.store.connect() as db:
            profile=db.execute('SELECT profile FROM logistics_calculations WHERE job_id=?',(identity,)).fetchone()
            if include_pending and profile:
                rows=db.execute("SELECT * FROM freight_attempts WHERE job_id=? OR (profile=? AND status IN ('sending','unknown')) ORDER BY rowid",(identity,profile[0])).fetchall()
            else:
                rows = db.execute('SELECT * FROM freight_attempts WHERE job_id=? ORDER BY rowid',(identity,)).fetchall()
        output=[]
        for row in rows:
            target=json.loads(row['target'])
            output.append({'id':row['id'],'tradeid':row['tradeid'],'status':row['status'],
                'freight_company':target.get('freightname',''),'freight_number':target.get('freightnumber',''),
                'message':'请核对 ERP 实际物流值后处理' if row['status']=='unknown' else ''})
        return output

    def resolve_attempt(self, identity, resolution, note, actor='admin'):
        if resolution not in {'applied','not_applied'}:
            raise ValueError('核对结果必须为已生效或未生效')
        if not isinstance(note,str) or not note.strip() or len(note)>1000:
            raise ValueError('请填写 1 至 1000 字的人工核对说明')
        with self.store.connect(write=True) as db:
            row=db.execute('SELECT * FROM freight_attempts WHERE id=?',(identity,)).fetchone()
            if row is None: raise KeyError('物流请求不存在')
            owner=db.execute('SELECT status FROM update_jobs WHERE id=?',(row['job_id'],)).fetchone()
            if owner and owner['status'] in {'queued','running'}:
                raise JobConflict('当前更新任务仍在执行，请结束后再确认待核对请求','update_busy')
            if row['status']!='unknown':
                audit=db.execute('SELECT resolution FROM freight_resolution_audit WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1',(identity,)).fetchone()
                if not audit or audit[0]!=resolution:
                    raise JobConflict('只有待核对请求可以确认，且不能改写已有核对结论','attempt_not_unknown')
            else:
                target=json.loads(row['target'])
                if resolution=='applied' and any(not target.get(k) for k in ('tradeid','freightname','freightcode','freightnumber')):
                    raise JobConflict('历史请求未保存完整目标，不能确认成功缓存','target_incomplete')
                status='succeeded' if resolution=='applied' else 'not_applied'
                db.execute('UPDATE freight_attempts SET status=?,updated_at=? WHERE id=?',(status,timestamp(),identity))
                db.execute('INSERT INTO freight_resolution_audit VALUES (?,?,?,?,?,?)',
                    (uuid.uuid4().hex,identity,resolution,note.strip(),actor,timestamp()))
                if resolution=='applied':
                    calculation=db.execute('SELECT data_version FROM logistics_calculations WHERE job_id=?',(row['job_id'],)).fetchone()
                    confirmed_at=timestamp()
                    db.execute('INSERT OR REPLACE INTO freight_cache VALUES (?,?,?,?,?)',
                        (row['profile'],row['tradeid'],dumps(target),calculation[0] if calculation else '',confirmed_at))
                    db.execute('''UPDATE freight_reconciliation SET fingerprint=pending_fingerprint,
                        confirmed_at=? WHERE profile=? AND tradeid=? AND pending_attempt_id=?''',
                        (confirmed_at,row['profile'],row['tradeid'],identity))
        self._refresh_resolved_job(row['job_id'])
        return next(item for item in self.attempts(row['job_id']) if item['id']==identity)

    def _refresh_resolved_job(self, identity):
        job=self.get(identity); summary=job['result']; attempts=self.attempts(identity)
        summary['attempts']=attempts
        summary['succeeded']=sum(a['status']=='succeeded' for a in attempts)
        summary['failed']=sum(a['status'] in {'failed','not_applied'} for a in attempts)
        summary['unknown']=sum(a['status'] in {'sending','unknown'} for a in attempts)
        summary['retired']=sum(a['status']=='superseded' for a in attempts)
        summary['success']=summary['succeeded']
        summary['unknown_orders']=[a['tradeid'] for a in attempts if a['status'] in {'sending','unknown'}]
        sent_orders={a['tradeid'] for a in attempts}
        summary['deferred_orders']=[p['tradeid'] for p in summary.get('changes',[]) if p['tradeid'] not in sent_orders]
        summary['deferred']=len(summary['deferred_orders'])
        status='unknown' if summary['unknown'] else 'partial' if (
            summary['failed'] or summary.get('blocked_orders') or
            summary.get('write_blocks') or summary['deferred']) else 'succeeded'
        summary['logistics_status']='unknown' if summary['unknown'] else 'partial' if status=='partial' else 'succeeded'
        self.mark(identity,status,'complete',summary)

    def _retire_closed_attempts(self, snapshot):
        """Release old scope holds only after a committed ERP lifecycle exit.

        Never retry an uncertain write, mark it successful, or delete its
        original target. Incomplete reads and active/shadow orders stay held.
        """
        if not isinstance(snapshot.get('order_lifecycle'), dict):
            return []
        current = {str(row['order_no']) for row in snapshot['orders']}
        retired, jobs = [], set()
        with self.store.connect(write=True) as db:
            # Retire the success shortcut as well, including orders that never
            # had a failed request. Recreating the same order number in ERP
            # must trigger a fresh logistics write for the new document.
            cached = db.execute('''SELECT f.tradeid FROM freight_cache f
                JOIN order_lifecycle_state l ON l.profile_id=f.profile AND l.order_no=f.tradeid
                WHERE f.profile=? AND l.lifecycle_state='archived' AND l.ended_at<>''
                  AND ((l.process_state='600' AND l.last_error='') OR
                       (l.process_state='absent' AND l.last_error='absent_from_complete_erp_sets'))''',
                (str(snapshot['profile_id']),)).fetchall()
            for row in cached:
                if row['tradeid'] not in current:
                    db.execute('DELETE FROM freight_cache WHERE profile=? AND tradeid=?',
                               (str(snapshot['profile_id']),row['tradeid']))
                    db.execute('DELETE FROM freight_reconciliation WHERE profile=? AND tradeid=?',
                               (str(snapshot['profile_id']),row['tradeid']))
            rows = db.execute('''SELECT a.*,l.process_state,l.ended_at,l.last_error
                FROM freight_attempts a JOIN order_lifecycle_state l
                  ON l.profile_id=a.profile AND l.order_no=a.tradeid
                WHERE a.profile=? AND a.status='unknown'
                  AND l.lifecycle_state='archived' AND l.ended_at<>'' ''',
                (str(snapshot['profile_id']),)).fetchall()
            for row in rows:
                closed = (row['process_state']=='600' and not row['last_error']) or (
                    row['process_state']=='absent' and
                    row['last_error']=='absent_from_complete_erp_sets')
                if not closed or row['tradeid'] in current:
                    continue
                db.execute("UPDATE freight_attempts SET status='superseded',updated_at=? WHERE id=? AND status='unknown'",
                           (timestamp(),row['id']))
                db.execute('INSERT INTO freight_resolution_audit VALUES (?,?,?,?,?,?)',
                    (uuid.uuid4().hex,row['id'],'closed_order',
                     dumps({'previous_status':'unknown','process_state':row['process_state'],
                            'ended_at':row['ended_at'],'reason':'ERP已结束或移除旧单，释放历史物流锁定；未重发、未确认旧请求成功'}),
                     'system:erp_lifecycle',timestamp()))
                # A later re-entry of this number must not inherit an old
                # successful-write shortcut from the removed ERP document.
                db.execute('DELETE FROM freight_cache WHERE profile=? AND tradeid=?',
                           (row['profile'],row['tradeid']))
                db.execute('DELETE FROM freight_reconciliation WHERE profile=? AND tradeid=?',
                           (row['profile'],row['tradeid']))
                retired.append(row['tradeid']);jobs.add(row['job_id'])
        for identity in jobs:
            self._refresh_resolved_job(identity)
        return retired

    def _progress(self, identity, stage, summary, completed=0, total=0):
        summary['progress']={'completed':completed,'total':total}
        summary['success']=summary.get('succeeded',0)
        summary['skipped']=summary.get('unchanged',0)
        self.mark(identity,'running',stage,summary)

    @exclusive_heavy_work
    def process_one(self):
        with self.store.connect(write=True) as db:
            # Browser imports and API collection share one heavy-work lane.
            # The browser claimant performs the reciprocal check in this same
            # SQLite writer transaction, so simultaneous starts cannot race.
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='web_operations'").fetchone()
                    and db.execute("SELECT 1 FROM web_operations WHERE status='running' LIMIT 1").fetchone()):
                return False
            # A manually requested historical month may page through a large
            # Qimen result. Do not interleave it with the main ERP refresh;
            # both workers claim their running state under this SQLite writer
            # lock, so the other worker sees the winner before starting.
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='order_history_jobs'").fetchone()
                    and db.execute("SELECT 1 FROM order_history_jobs WHERE status='running' LIMIT 1").fetchone()):
                return False
            if db.execute("SELECT 1 FROM update_jobs WHERE status='running'").fetchone():
                return False
            candidates = db.execute("SELECT * FROM update_jobs WHERE status='queued' ORDER BY rowid").fetchall()
            row = next((r for r in candidates if
                json.loads(r['result']).get('next_retry_at', '') <= timestamp() and
                (json.loads(r['result']).get('schedule') != 'workday' or
                 workday_slot() is not None and (r['mode'] != 'sync' or business_open()))), None)
            if not row: return False
            job = self.result(row)
            if job['result'].get('automatic'):
                from .storage_capacity import capacity
                space = capacity(self.store.runtime)
                if space['status'] == 'critical':
                    pending = {**job['result'], 'message':'服务器存储空间不足，自动更新已等待；保留当前数据',
                        'storage':space, 'next_retry_at':(datetime.now()+timedelta(minutes=10)).isoformat(timespec='seconds')}
                    db.execute("UPDATE update_jobs SET stage='waiting_capacity',result=?,updated_at=? WHERE id=?",
                               (dumps(pending),timestamp(),job['id']))
                    return False
            db.execute("UPDATE update_jobs SET status='running',stage='reading',updated_at=? WHERE id=?",(timestamp(),job['id']))
        summary={'published':False,'query_status':'reading','logistics_status':'pending','succeeded':0,'failed':0,'unknown':0,
                 'unchanged':0,'attempts':[],'blocked_orders':[],'progress':{'completed':0,'total':0}}
        if job['result'].get('automatic'):
            summary.update(automatic=True,scheduled_for=job['result']['scheduled_for'])
        for field in ('schedule', 'source_retry_count'):
            if field in job['result']:
                summary[field] = job['result'][field]
        frozen_policy = FreightWritePolicy.thaw(job['result'].get('write_policy'))
        summary['write_policy'] = frozen_policy.freeze()
        if job['mode'] == 'inventory_subset':
            # The frozen input is part of the durable idempotency record and
            # must survive success, failure and process restart.
            summary['request'] = job['result'].get('request')
        try:
            with self.store.connect() as db:
                hold=bool(db.execute("SELECT 1 FROM settings WHERE key='restoration_hold' AND value='true'").fetchone())
            if hold and job['mode']=='update':
                raise SourceIncomplete('恢复后业务写入保持暂停')
            def report(progress):
                # Source adapters report counts only; no vendor payload in App status.
                summary['source']=str(progress.get('source',''))[:40]
                current=max(0,int(progress.get('current',progress.get('completed',0))))
                total=max(current,int(progress.get('total',0)))
                if progress.get('retry_attempt'):
                    summary['source_retry'] = {k: progress[k] for k in ('source', 'retry_attempt', 'retry_reason')}
                self._progress(job['id'],'reading',summary,current,total)
            if job['mode'] == 'inventory_subset':
                refresh = getattr(self.source, 'refresh_inventory_subset', None)
                if not callable(refresh):
                    raise SourceIncomplete('当前 ERP 数据源不支持库存子集刷新')
                request = job['result'].get('request') or {}
                result = refresh(product_ids=request.get('product_ids'),
                    identities=request.get('identities'), progress=report)
                summary.update(result, published=True, query_status='published',
                    logistics_status='not_requested', counts={'inventory': result['updated']},
                    record_counts={'inventory': result['updated']},
                    progress={'completed': result['updated'], 'total': result['updated']})
                self.mark(job['id'],'succeeded','complete',summary)
                return True
            collector=self.source.collect
            parameters=inspect.signature(collector).parameters
            options={}
            if 'progress' in parameters:
                options['progress']=report
            if 'full_reconcile' in parameters:
                options['full_reconcile']=job['mode']=='reconcile'
            facts=collector(**options)
            snapshot = validate_snapshot(facts)
            counts={k:len(v) for k,v in snapshot['records'].items()}
            summary.update(source_updated_at=snapshot['source_updated_at'],counts=counts,record_counts=counts,query_status='validating')
            summary['query_scope']=snapshot.get('query_scope',{})
            summary['quantity_basis']=snapshot.get('quantity_basis',summary['query_scope'].get('quantity_basis',''))
            summary['source_counts']=snapshot.get('source_counts',{})
            with self.store.connect(write=True) as db:
                db.execute('INSERT OR REPLACE INTO sync_batches VALUES (?,?,?,?,?,?,?,?,?)',
                    (job['id'],str(snapshot['profile_id']),'staged',snapshot['source_updated_at'],'',dumps(counts),dumps(snapshot),timestamp(),None))
                previous=db.execute("SELECT value FROM settings WHERE key='receipt_ledger_v1'").fetchone()
            previous_state=json.loads(previous[0]) if previous else None
            self._progress(job['id'],'calculating',summary)
            if snapshot.get('query_complete') is True:
                result=calculate_query_snapshot(snapshot,previous_state)
            elif snapshot.get('rules_contract') == 'v2':
                if previous_state is None:
                    if snapshot.get('receipt_state_bootstrap_verified') is True and isinstance(snapshot.get('receipt_initial_state'), dict):
                        previous_state=snapshot['receipt_initial_state']
                    else:
                        raise SourceIncomplete('首次 API 分类须完成旧入库账本身份映射及初始化核对，不能重置已消耗批次')
                result=calculate_snapshot(snapshot,previous_state)
            else:
                result=calculate(snapshot['orders'],snapshot['inventory'])
            with self.store.connect(write=True) as db:
                db.execute('INSERT OR REPLACE INTO logistics_calculations VALUES (?,?,?,?,?)',
                    (job['id'],str(snapshot['profile_id']),'',dumps(result),timestamp()))
            if result['conflicts'] and snapshot.get('query_complete') is not True:
                self.mark(job['id'],'blocked','conflict',result)
                return True
            self._progress(job['id'],'publishing',summary)
            version=publish(self.store,snapshot,result,job['id'])
            with self.store.connect(write=True) as db:
                db.execute('UPDATE logistics_calculations SET data_version=? WHERE job_id=?',(version,job['id']))
                published_at=db.execute('SELECT published_at FROM sync_batches WHERE job_id=?',(job['id'],)).fetchone()[0]
            summary.update(data_version=version,published_at=published_at,published=True,query_status='published',
                           blocked_orders=result.get('blocked_orders',[]),
                           write_blocks=result.get('write_blocks',[]))
            if self.after_publish is not None:
                try:
                    task_result = self.after_publish(
                        data_version=version,
                        source_updated_at=snapshot['source_updated_at'])
                    summary['daily_work_status'] = 'updated'
                    if isinstance(task_result, dict):
                        summary['daily_work_counts'] = task_result.get('counts', {})
                except Exception:
                    # Query publication and ERP logistics are independent of the
                    # local task projection.  A task refresh can be retried from
                    # its own endpoint without hiding the newly published data.
                    summary['daily_work_status'] = 'failed'
                    summary['daily_work_message'] = '每日任务刷新失败，请稍后重试；订单数据已经发布'
                    with self.store.connect(write=True) as db:
                        db.execute('INSERT OR REPLACE INTO update_projection_retries VALUES (?,?,?)',
                            (job['id'],0,(datetime.now()+timedelta(minutes=5)).isoformat(timespec='seconds')))
            summary['retired_unknown_orders']=self._retire_closed_attempts(snapshot)
            profile=str(snapshot['profile_id']); changes=[]; unknown=[]; invalidated=[]
            renewals=[]; source_changed=[]; source_fingerprints=fingerprints(snapshot)
            checked_at=datetime.now(ZoneInfo('Asia/Shanghai'))
            observations=snapshot.get('logistics_observations', {})
            writable_plans=[target for target in result['plans']
                if target.get('write_enabled') is not False and
                all(target.get(name) for name in
                    ('tradeid','freightname','freightcode','freightnumber'))]
            scope_sets={gid:{tuple(k) for k in keys} for gid,keys in result.get('scope_groups',{}).items()}
            def target_scope(number):
                gid=result.get('order_scope_ids',{}).get(number)
                return scope_sets[gid] if gid in scope_sets else {tuple(k) for k in allocation_scope(result,number)}
            with self.store.connect() as db:
                proofs={r['tradeid']:r for r in db.execute(
                    'SELECT * FROM freight_reconciliation WHERE profile=?',(profile,))}
                caches={r['tradeid']:r for r in db.execute(
                    'SELECT * FROM freight_cache WHERE profile=?',(profile,))}
                uncertain=db.execute("SELECT a.tradeid,s.inventory_keys FROM freight_attempts a LEFT JOIN freight_attempt_scopes s ON s.attempt_id=a.id WHERE a.profile=? AND a.status IN ('sending','unknown')",(profile,)).fetchall()
                unknown_keys=set(); unknown_scope=False
                old_calculations={}
                for old in uncertain:
                    if old['inventory_keys'] is None: unknown_scope=True
                    else:
                        old_keys=json.loads(old['inventory_keys'])
                        if isinstance(old_keys,dict):
                            old_job=old_keys.get('job_id')
                            if old_job not in old_calculations:
                                saved=db.execute('SELECT payload FROM logistics_calculations WHERE job_id=?',(old_job,)).fetchone()
                                old_calculations[old_job]=json.loads(saved[0]) if saved else {}
                            old_keys=old_calculations[old_job].get('scope_groups',{}).get(old_keys.get('group_id'),[])
                        if not old_keys: unknown_scope=True
                        unknown_keys.update(tuple(k) for k in old_keys)
                    # An edited order may now join a different allocation group.
                    unknown_keys.update(target_scope(old['tradeid']))
                uncertain_orders={u['tradeid'] for u in uncertain}
                held_groups={gid for gid,keys in scope_sets.items() if unknown_scope or unknown_keys & keys}
                for target in sorted(writable_plans,key=lambda p:p['tradeid']):
                    row=caches.get(target['tradeid'])
                    fields={k:target[k] for k in ('tradeid','freightname','freightcode','freightnumber')}
                    affected=target['tradeid'] in uncertain_orders
                    if snapshot.get('query_complete') is True:
                        affected=affected or unknown_scope or result.get('order_scope_ids',{}).get(target['tradeid']) in held_groups
                    if affected: unknown.append(target['tradeid'])
                    elif row and json.loads(row['target'])==fields:
                        if _freight_observed_changes(observations.get(target['tradeid']), row, fields):
                            changes.append(fields)
                            invalidated.append(target['tradeid'])
                        else:
                            proof=proofs.get(target['tradeid'])
                            reason=recheck_reason(row,proof,source_fingerprints.get(target['tradeid'],''),checked_at)
                            if reason=='source_changed':
                                changes.append(fields);source_changed.append(target['tradeid'])
                            elif reason:
                                renewals.append((recheck_priority(row,proof),target['tradeid'],fields,reason))
                    else: changes.append(fields)
            renewals.sort(key=lambda item:(item[0],item[1]))
            renewal_fields=[item[2] for item in renewals]
            # Apply the existing permission/batch policy BEFORE the renewal cap.
            # Otherwise ineligible old rows could starve an allowed newer row.
            selected, deferred, policy_result = frozen_policy.select(changes+renewal_fields)
            renewal_ids={item[1] for item in renewals}
            renewal_selected=[p for p in selected if p['tradeid'] in renewal_ids]
            postponed=renewal_selected[RECHECK_BATCH:]
            postponed_ids={p['tradeid'] for p in postponed}
            selected=[p for p in selected if p['tradeid'] not in postponed_ids]
            deferred+=postponed
            summary.update(freight_recheck_due=len(renewals),
                freight_recheck_selected=[p['tradeid'] for p in renewal_selected[:RECHECK_BATCH]],
                freight_recheck_deferred=len(renewals)-min(len(renewal_selected),RECHECK_BATCH),
                freight_recheck_after_hours=RECHECK_AFTER.total_seconds()/3600,
                source_changed_logistics_orders=source_changed)
            changes+=renewal_fields
            summary.update(changes=changes, erp_changed_logistics_orders=invalidated,
                unchanged=len(writable_plans)-len(changes)-len(unknown),
                unknown_orders=unknown,unknown=len(unknown),
                write_blocked_orders=[item.get('order_no','')
                    for item in result.get('write_blocks',[])])
            summary.update(selected=len(selected),
                selected_orders=[target['tradeid'] for target in selected],
                deferred=len(deferred),
                deferred_orders=[target['tradeid'] for target in deferred],
                write_policy_result=policy_result)
            write_requested=job['mode'] in {'update','sync'}
            policy_blocked=write_requested and self.sender is not None and not frozen_policy.configured
            can_write=write_requested and self.sender is not None and frozen_policy.configured and not hold
            if not can_write:
                summary['logistics_status']='restoration_hold' if hold else \
                    'preview' if job['mode'] in {'preview','refresh','reconcile'} else 'unavailable'
                if policy_blocked:
                    summary['logistics_status']='policy_blocked'
            else:
                summary['logistics_status']='writing'
                self._progress(job['id'],'writing',summary,0,len(selected))
                newly_uncertain_groups=set()
                blocked_writes=0
                completed_writes=0
                for target in selected:
                    target_group=result.get('order_scope_ids',{}).get(target['tradeid'])
                    if snapshot.get('query_complete') is True and target_group in newly_uncertain_groups:
                        summary['unknown_orders'].append(target['tradeid'])
                        summary['unknown']+=1
                        blocked_writes+=1
                        handled=completed_writes+blocked_writes
                        if handled % 10 == 0 or handled == len(selected):
                            self._progress(job['id'],'writing',summary,handled,len(selected))
                        continue
                    attempt=uuid.uuid4().hex; now=timestamp()
                    with self.store.connect(write=True) as db:
                        db.execute("INSERT INTO freight_attempts VALUES (?,?,?,?,?,'sending',?,?)",
                            (attempt,job['id'],profile,target['tradeid'],dumps(target),now,now))
                        scope_record={'job_id':job['id'],'group_id':target_group} if target_group is not None else allocation_scope(result,target['tradeid'])
                        db.execute('INSERT INTO freight_attempt_scopes VALUES (?,?)',(attempt,dumps(scope_record)))
                        db.execute('''INSERT INTO freight_reconciliation
                            (profile,tradeid,fingerprint,confirmed_at,last_attempt_at,pending_fingerprint,pending_attempt_id)
                            VALUES (?,?,'','',?,?,?) ON CONFLICT(profile,tradeid) DO UPDATE
                            SET last_attempt_at=excluded.last_attempt_at,
                            pending_fingerprint=excluded.pending_fingerprint,
                            pending_attempt_id=excluded.pending_attempt_id''',
                            (profile,target['tradeid'],now,source_fingerprints.get(target['tradeid'],''),attempt))
                    try:
                        success=self.sender(target)
                        outcome='succeeded' if success is True else 'failed' if success is False else 'unknown'
                    except Exception:
                        outcome='unknown'
                    with self.store.connect(write=True) as db:
                        db.execute('UPDATE freight_attempts SET status=?,updated_at=? WHERE id=?',(outcome,timestamp(),attempt))
                        if outcome=='succeeded':
                            confirmed_at=timestamp()
                            db.execute('INSERT OR REPLACE INTO freight_cache VALUES (?,?,?,?,?)',
                                (profile,target['tradeid'],dumps(target),version,confirmed_at))
                            db.execute('''UPDATE freight_reconciliation SET fingerprint=?,confirmed_at=?
                                WHERE profile=? AND tradeid=?''',
                                (source_fingerprints.get(target['tradeid'],''),confirmed_at,profile,target['tradeid']))
                    summary[outcome]+=1
                    if outcome=='unknown':
                        summary['unknown_orders'].append(target['tradeid'])
                        newly_uncertain_groups.add(target_group)
                    completed_writes+=1
                    handled=completed_writes+blocked_writes
                    # A full manual update can contain thousands of ERP calls.
                    # Each attempt is already durable in freight_attempts, so
                    # rebuilding and serializing the growing list after every
                    # request only adds quadratic database work.  Refresh the
                    # compact progress summary in small batches; hydrate live
                    # attempt details from their own table for API readers.
                    if handled % 10 == 0 or handled == len(selected):
                        self._progress(job['id'],'writing',summary,handled,len(selected))
                summary['attempts']=self.attempts(job['id'])
            state='unknown' if summary['unknown'] else 'partial' if (
                summary['failed'] or summary['blocked_orders'] or
                summary.get('write_blocks') or policy_blocked or
                (can_write and summary['deferred'])) else 'succeeded'
            if can_write: summary['logistics_status']=state
            summary['success']=summary['succeeded']; summary['skipped']=summary['unchanged']
            summary['progress']={'completed':len(selected) if can_write else 0,'total':len(selected)}
            self.mark(job['id'],state,'complete',summary)
        except SourceChanged as exc:
            # This exception can only arise from read adapters, before any
            # publication or ERP write. Keep the job and its frozen boundary.
            if summary['published'] or self.attempts(job['id']):
                raise
            count = int(summary.get('source_retry_count', 0)) + 1
            delay = min(300, 30 * 2 ** min(count - 1, 4))
            summary.update(message='ERP 数据正在变化，稍后自动续读；继续显示上次完整数据',
                source_retry_count=count, source_retry_reason=str(exc), query_status='retrying',
                next_retry_at=(datetime.now()+timedelta(seconds=delay)).isoformat(timespec='seconds'))
            self.mark(job['id'], 'queued', 'waiting_source', summary)
        except SourceIncomplete as exc:
            summary.update(message=str(exc),query_status='published' if summary['published'] else 'failed')
            self.mark(job['id'],'partial' if summary['published'] else 'blocked','source_incomplete',summary)
        except Exception:
            summary.update(message='更新未完成，请检查服务端数据合同或授权；已有写入请以逐笔记录为准',query_status='published' if summary['published'] else 'failed')
            with self.store.connect(write=True) as db:
                db.execute("UPDATE freight_attempts SET status='unknown',updated_at=? WHERE job_id=? AND status='sending'",(timestamp(),job['id']))
            summary['attempts']=self.attempts(job['id'])
            has_unknown=any(attempt['status']=='unknown' for attempt in summary['attempts'])
            self.mark(job['id'],'unknown' if has_unknown else 'partial' if summary['published'] else 'failed','error',summary)
        return True

    @exclusive_heavy_work
    def retry_projection(self):
        """Repair a published local task projection without refetching or ERP writes."""
        if self.after_publish is None:
            return False
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM update_jobs WHERE status IN ('queued','running')").fetchone():
                return False
            row = db.execute('''SELECT j.*,r.attempts FROM update_projection_retries r
                JOIN update_jobs j ON j.id=r.job_id WHERE r.next_retry_at<=?
                ORDER BY r.next_retry_at LIMIT 1''',(timestamp(),)).fetchone()
        if row is None:
            return False
        result=json.loads(row['result'])
        if not result.get('published') or result.get('daily_work_status')!='failed':
            with self.store.connect(write=True) as db:
                db.execute('DELETE FROM update_projection_retries WHERE job_id=?',(row['id'],))
            return True
        with self.store.connect() as db:
            current=db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
        current_version=json.loads(current[0]) if current else ''
        if current_version!=result.get('data_version'):
            # A newer published generation supersedes this projection.  Do not
            # re-project old tasks over newer data; its own retry follows next.
            result['daily_work_status']='superseded'
            result['daily_work_message']='较新的订单数据已发布，旧任务刷新不再重试'
            with self.store.connect(write=True) as db:
                db.execute('UPDATE update_jobs SET result=?,updated_at=? WHERE id=?',
                    (dumps(result),timestamp(),row['id']))
                db.execute('DELETE FROM update_projection_retries WHERE job_id=?',(row['id'],))
            return True
        try:
            projected=self.after_publish(data_version=result['data_version'],
                source_updated_at=result['source_updated_at'])
        except Exception:
            with self.store.connect(write=True) as db:
                db.execute('UPDATE update_projection_retries SET attempts=?,next_retry_at=? WHERE job_id=?',
                    (row['attempts']+1,(datetime.now()+timedelta(minutes=min(60,5*2**min(4,row['attempts']))))
                     .isoformat(timespec='seconds'),row['id']))
            return True
        result['daily_work_status']='updated'
        result.pop('daily_work_message',None)
        if isinstance(projected,dict):
            result['daily_work_counts']=projected.get('counts',{})
        with self.store.connect(write=True) as db:
            db.execute('UPDATE update_jobs SET result=?,updated_at=? WHERE id=?',
                (dumps(result),timestamp(),row['id']))
            db.execute('DELETE FROM update_projection_retries WHERE job_id=?',(row['id'],))
        return True

    def start(self):
        import fcntl
        if self.thread and self.thread.is_alive(): return False
        handle = (self.store.runtime / '.api-worker.lock').open('a+')
        try: fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close(); return False
        self.lock_file = handle
        self.recover()
        def loop():
            try:
                while not self.stop_event.is_set():
                    try:
                        if not self.process_one() and not self.retry_projection():
                            self.wake.wait(1); self.wake.clear()
                    except Exception:
                        self.stop_event.wait(1)
            finally:
                handle.close()
        self.thread = threading.Thread(target=loop,name='manual-api-updates',daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set(); self.wake.set()
        if self.thread: self.thread.join(25)
