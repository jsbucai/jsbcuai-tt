"""Administrator-triggered, read-only, one-month-at-a-time history jobs.

There is deliberately no timer or automatic enqueue path. The worker only
calls the Qimen read method through OrderHistoryArchive; every job freezes a
single already-closed Shanghai month. Interrupted reads can be retried after
restart because the archive publishes only complete, replay-verified windows.
"""

import json
import threading
import uuid
from datetime import datetime, timedelta

from .order_history_archive import (HistoryIncomplete, SHANGHAI,
                                    _month_start, _next_month)
from .print_jobs import JobConflict
from .store import dumps, timestamp
from .heavy_lane import exclusive_heavy_work


class OrderHistoryJobs:
    def __init__(self, store, archive, client):
        self.store, self.archive, self.client = store, archive, client
        self.wake, self.stop_event = threading.Event(), threading.Event()
        self.thread = self.lock_file = None
        with store.connect(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS order_history_jobs(
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    month TEXT NOT NULL,
                    rescan INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    result TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS order_history_one_running
                    ON order_history_jobs((1)) WHERE status='running';
            ''')

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ('id', 'profile_id', 'month', 'status',
                                         'stage', 'attempts', 'created_at',
                                         'updated_at')} | {
            'rescan': bool(row['rescan']),
            'result': json.loads(row['result']),
        }

    def enqueue(self, identity, profile, month, *, rescan=False):
        try:
            identity = str(uuid.UUID(identity))
        except (TypeError, ValueError, AttributeError):
            raise ValueError('history_job_id_must_be_uuid') from None
        profile = str(profile or '').strip()
        if not profile.isascii() or not profile.isdigit():
            raise ValueError('profile_id_must_be_numeric')
        first = _month_start(month)
        if _next_month(first) > datetime.now(SHANGHAI).replace(tzinfo=None):
            raise ValueError('history_month_must_be_closed')
        if not isinstance(rescan, bool):
            raise ValueError('rescan_must_be_boolean')
        with self.store.connect(write=True) as db:
            existing = db.execute('SELECT * FROM order_history_jobs WHERE id=?',
                                  (identity,)).fetchone()
            if existing:
                if (existing['profile_id'], existing['month'], bool(existing['rescan'])) != (
                        profile, month, rescan):
                    raise JobConflict('任务编号已用于不同历史月份或查询范围',
                                      'history_job_id_conflict')
                return self._public(existing)
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='update_jobs'").fetchone()
                    and db.execute("SELECT 1 FROM update_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()):
                raise JobConflict('主数据更新正在排队或执行，历史月份查询暂不启动',
                                  'update_busy')
            active = db.execute("SELECT id FROM order_history_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if active:
                raise JobConflict('已有历史月份查询正在排队或执行，请等待完成', 'history_busy')
            if not rescan:
                completed = db.execute('''SELECT 1 FROM order_history_coverage
                    WHERE profile_id=? AND mode='month' AND window_start=?
                    AND window_end=? LIMIT 1''',
                    (profile, first.strftime('%Y-%m-%d 00:00:00'),
                     (_next_month(first) - timedelta(seconds=1))
                     .strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
                if completed:
                    raise JobConflict('该月份已完整归档；如需复核请显式指定 rescan=true',
                                      'history_month_complete')
            now = timestamp()
            db.execute('''INSERT INTO order_history_jobs(
                id,profile_id,month,rescan,status,stage,result,attempts,created_at,updated_at)
                VALUES (?,?,?,?,'queued','queued',?,0,?,?)''',
                (identity, profile, month, int(rescan), dumps({}), now, now))
        self.wake.set()
        return self.get(identity)

    def get(self, identity):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM order_history_jobs WHERE id=?',
                             (identity,)).fetchone()
        if row is None:
            raise KeyError('历史归档任务不存在')
        return self._public(row)

    def history(self, limit=50, offset=0):
        try:
            limit, offset = int(limit), int(offset)
        except (ValueError, TypeError):
            raise ValueError('invalid_history_job_pagination') from None
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ValueError('invalid_history_job_pagination')
        with self.store.connect() as db:
            rows = db.execute('''SELECT * FROM order_history_jobs
                ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?''',
                (limit, offset)).fetchall()
        return [self._public(row) for row in rows]

    def recover(self):
        with self.store.connect(write=True) as db:
            db.execute("""UPDATE order_history_jobs SET status='queued',stage='recovered',
                updated_at=? WHERE status='running'""", (timestamp(),))

    @exclusive_heavy_work
    def process_one(self):
        with self.store.connect(write=True) as db:
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='update_jobs'").fetchone()
                    and db.execute("SELECT 1 FROM update_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()):
                return False
            if db.execute("SELECT 1 FROM order_history_jobs WHERE status='running'").fetchone():
                return False
            row = db.execute("""SELECT * FROM order_history_jobs WHERE status='queued'
                ORDER BY rowid LIMIT 1""").fetchone()
            if row is None:
                return False
            db.execute("""UPDATE order_history_jobs SET status='running',stage='reading',
                attempts=attempts+1,updated_at=? WHERE id=?""", (timestamp(), row['id']))
        try:
            account = getattr(self.client, 'account', None)
            if not callable(account):
                raise HistoryIncomplete('source_account_unavailable')
            if str(account()) != row['profile_id']:
                raise HistoryIncomplete('authorized_profile_mismatch')
            result = self.archive.backfill_month(self.client, row['profile_id'],
                                                 row['month'])
        except HistoryIncomplete as error:
            self._finish(row['id'], 'blocked', 'source_incomplete', {
                'message': str(error), 'review_required': True,
                'archive_unchanged_for_failed_month': True})
        except Exception:
            self._finish(row['id'], 'failed', 'error', {
                'message': '历史月份读取未完成；请核对授权和来源接口后重新提交',
                'review_required': True,
                'archive_unchanged_for_failed_month': True})
        else:
            self._finish(row['id'], 'succeeded', 'complete', {
                'coverage': result, 'review_required': True,
                'review_reasons': ['customer_identity_unverified',
                                   'reported_quantity_basis_unverified',
                                   'lifetime_coverage_incomplete']})
        return True

    def _finish(self, identity, status, stage, result):
        with self.store.connect(write=True) as db:
            db.execute('''UPDATE order_history_jobs SET status=?,stage=?,result=?,
                updated_at=? WHERE id=?''',
                (status, stage, dumps(result), timestamp(), identity))

    def start(self):
        import fcntl
        if self.thread and self.thread.is_alive():
            return False
        handle = (self.store.runtime / '.order-history-worker.lock').open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.lock_file = handle
        try:
            self.recover()
        except BaseException:
            handle.close()
            self.lock_file = None
            raise

        def loop():
            try:
                while not self.stop_event.is_set():
                    try:
                        if not self.process_one():
                            self.wake.wait(1)
                            self.wake.clear()
                    except Exception:
                        self.stop_event.wait(1)
            finally:
                handle.close()

        self.thread = threading.Thread(target=loop, name='manual-order-history', daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(25)
