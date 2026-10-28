"""Archive only rebuildable sync detail. Business and idempotency facts stay live."""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from .maintenance_archive import write_archive


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def protected_jobs(db, now):
    present = tables(db)
    protected = set()
    # Never discard data needed by an in-flight/review operation or projection.
    if 'update_jobs' in present:
        protected.update(r[0] for r in db.execute("SELECT id FROM update_jobs WHERE status IN ('queued','running','unknown')"))
    if 'freight_attempts' in present:
        protected.update(r[0] for r in db.execute("SELECT job_id FROM freight_attempts WHERE status IN ('sending','unknown')"))
    if 'update_projection_retries' in present:
        protected.update(r[0] for r in db.execute('SELECT job_id FROM update_projection_retries'))
    versions = set()
    row = db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
    if row:
        versions.add(json.loads(row[0]))
    if 'daily_work_settings' in present:
        row = db.execute("SELECT value FROM daily_work_settings WHERE key='projection_data_version'").fetchone()
        if row: versions.add(row[0])
    rows = db.execute('SELECT job_id,profile,data_version,created_at FROM logistics_calculations ORDER BY rowid DESC')
    latest, seen_versions = {}, set()
    cutoff = (now - timedelta(hours=2)).isoformat(timespec='seconds')
    for row in rows:
        latest[row['profile']] = latest.get(row['profile'], 0) + 1
        current_version = row['data_version'] in versions and row['data_version'] not in seen_versions
        seen_versions.add(row['data_version'])
        if latest[row['profile']] <= 6 or current_version or row['created_at'] >= cutoff:
            protected.add(row['job_id'])
    # Historic uncertain scope references may point at a different job.
    if {'freight_attempt_scopes','freight_attempts'} <= present:
        for row in db.execute("SELECT s.inventory_keys FROM freight_attempt_scopes s JOIN freight_attempts a ON a.id=s.attempt_id WHERE a.status IN ('sending','unknown')"):
            scope = json.loads(row[0])
            if isinstance(scope, dict) and scope.get('job_id'): protected.add(scope['job_id'])
    return protected


def busy(db):
    present = tables(db)
    for table in ('update_jobs', 'web_operations', 'order_history_jobs'):
        if table in present and db.execute(f"SELECT 1 FROM {table} WHERE status='running' LIMIT 1").fetchone():
            return True
    return False


def archive_calculations(database, destination, key, now=None, max_jobs=100):
    """Archive outside writer lock, revalidate inside it, then shrink detail only."""
    now = now or datetime.now()
    destination = Path(destination)
    if not destination.is_dir() or destination.is_symlink():
        raise ValueError('归档目录不可用，跳过清理')
    db = sqlite3.connect(database, timeout=2)
    db.row_factory = sqlite3.Row
    result = {'archived_jobs': 0, 'released_payload_bytes': 0, 'skipped_busy': False}
    try:
        if busy(db): return {**result, 'skipped_busy': True}
        protected = protected_jobs(db, now)
        ids = [r[0] for r in db.execute("SELECT job_id FROM logistics_calculations WHERE coalesce(json_extract(payload,'$.archived'),0)<>1 ORDER BY rowid") if r[0] not in protected]
        for identity in ids[:max_jobs]:
            row = db.execute('SELECT * FROM logistics_calculations WHERE job_id=?', (identity,)).fetchone()
            if not row or json.loads(row['payload']).get('archived'): continue
            job = db.execute('SELECT * FROM update_jobs WHERE id=?', (identity,)).fetchone()
            issues = [dict(r) for r in db.execute('SELECT * FROM sync_issues WHERE job_id=? ORDER BY rowid', (identity,))]
            batch = db.execute('SELECT * FROM sync_batches WHERE job_id=?', (identity,)).fetchone()
            body = {'calculation': dict(row), 'update_job': dict(job) if job else None,
                    'issues': issues, 'batch': dict(batch) if batch else None}
            archive = destination / ('calc-' + uuid.uuid4().hex + '.ulcold')
            receipt = write_archive(archive, [compact_json(body).encode()], key)
            db.execute('BEGIN IMMEDIATE')
            try:
                current = db.execute('SELECT payload FROM logistics_calculations WHERE job_id=?', (identity,)).fetchone()
                if busy(db) or identity in protected_jobs(db, now) or not current or current[0] != row['payload']:
                    db.rollback(); archive.unlink(); result['skipped_busy'] = True; break
                if job and tuple(db.execute('SELECT * FROM update_jobs WHERE id=?', (identity,)).fetchone()) != tuple(job):
                    db.rollback(); archive.unlink(); break
                marker = {'archived': True, 'archived_at': now.isoformat(), **receipt}
                db.execute('UPDATE logistics_calculations SET payload=? WHERE job_id=?', (compact_json(marker), identity))
                db.execute('DELETE FROM sync_issues WHERE job_id=?', (identity,))
                if batch:
                    db.execute("UPDATE sync_batches SET snapshot='{}' WHERE job_id=?", (identity,))
                # Retain complete job results: historic resolve/replay uses them.
                db.commit()
            except BaseException:
                db.rollback(); raise
            result['archived_jobs'] += 1
            result['released_payload_bytes'] += len(row['payload'].encode()) + sum(len(r['payload'].encode()) for r in issues)
        with db:
            if 'auth_sessions' in tables(db):
                result['expired_sessions'] = db.execute('DELETE FROM auth_sessions WHERE expires<?', (now.timestamp(),)).rowcount
        db.execute('PRAGMA wal_checkpoint(PASSIVE)')
        result['reusable_database_bytes'] = db.execute('PRAGMA freelist_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
        return result
    finally:
        db.close()
