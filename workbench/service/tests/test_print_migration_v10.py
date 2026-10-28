"""Upgrade a populated pre-reprint database without inventing past operators."""
import json
import sqlite3

from test_api_migration import store
from core.store import Store
from tools.release_data_audit import audit


def test_v9_upgrade_preserves_print_facts_and_audits_original_columns(store):
    with store.connect(write=True) as db:
        db.execute('DROP TABLE print_job_previews')
        db.execute('DROP TABLE order_print_reset_requests')
        db.execute('ALTER TABLE order_print_audit DROP COLUMN actor_id')
        db.execute('ALTER TABLE order_print_audit DROP COLUMN actor_name')
        db.execute("""INSERT INTO order_print_audit VALUES
            ('historic','P','ORDER-1','reserved','printed','device-old','job-old','submitted','2026-10-07')""")
        db.execute("""INSERT INTO print_jobs(id,status,frozen,metadata,prepare_idempotency_key)
            VALUES ('job-old','submitted','{}','{}','historic-prepare')""")
        db.execute('PRAGMA user_version=9')
    before = audit(store.path)
    reopened = Store(store.runtime)
    backups = list((store.runtime / 'migration-backups').glob('before-v10-*.sqlite3'))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
        assert 'actor_id' not in {row[1] for row in db.execute('PRAGMA table_info(order_print_audit)')}
        assert db.execute("SELECT to_state FROM order_print_audit WHERE id='historic'").fetchone()[0] == 'printed'
    after = audit(reopened.path, before)
    assert all(after['tables'][name] == value for name, value in before['tables'].items())
    with reopened.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 11
        row = db.execute("SELECT actor_id,actor_name FROM order_print_audit WHERE id='historic'").fetchone()
        assert tuple(row) == ('', '')
        db.execute('SELECT * FROM print_job_previews')
        db.execute('SELECT * FROM order_print_reset_requests')
    Store(reopened.runtime)
    assert len(list((store.runtime / 'migration-backups').glob('before-v10-*.sqlite3'))) == 1
    # Audit detects an actual historical state rewrite even after a schema change.
    with reopened.connect(write=True) as db:
        db.execute("UPDATE order_print_audit SET to_state='unprinted' WHERE id='historic'")
    changed = audit(reopened.path, before)
    assert changed['tables']['order_print_audit'] != before['tables']['order_print_audit']


def test_fresh_v10_print_schema_and_active_job_audit(store):
    with store.connect(write=True) as db:
        for index, status in enumerate(('prepared', 'claimed', 'submitted', 'unknown')):
            db.execute('INSERT INTO print_jobs(id,status,frozen,metadata) VALUES (?,?,?,?)',
                       (str(index), status, '{}', json.dumps({'test': True})))
    result = audit(store.path)
    assert result['schema'] == 11 and result['pending']['print_jobs'] == 2
    assert result['tables']['print_job_previews']['count'] == 0
    assert 'actor_id' in result['columns']['order_print_audit']
