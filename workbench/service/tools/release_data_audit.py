"""Read-only release comparison of protected UL workflow facts.

Prints counts and hashes only; no customer, credential or order data leaves
the database. Run once before and once after a backend program upgrade.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

PROTECTED = (
    'records', 'daily_work_tasks', 'daily_work_events',
    'order_print_states', 'order_print_audit',
    'customer_order_followups', 'customer_order_followup_events',
    'sales_customer_owners', 'purchase_delivery_events',
    'sales_assistant_sessions','sales_assistant_messages','sales_assistant_attachments',
    'sales_assistant_events','sales_assistant_operations','web_operations',
    'sales_codex_jobs','sales_new_product_plans',
    'print_jobs','print_job_previews','order_print_reset_requests','print_state_corrections',
)
PENDING = {
    'print_jobs': "status IN ('queued','running','prepared','claimed')",
    'update_jobs': "status IN ('queued','running')",
    'adjustment_batches': "status IN ('queued','running')",
}


def audit(path, baseline=None):
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True, timeout=30)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {'integrity': db.execute('PRAGMA integrity_check').fetchone()[0],
                  'schema': db.execute('PRAGMA user_version').fetchone()[0],
                  'tables': {}, 'columns': {}, 'pending': {}, 'settings': {}}
        for name in PROTECTED:
            if name not in tables:
                continue
            digest = hashlib.sha256()
            count = 0
            available = [row[1] for row in db.execute(f'PRAGMA table_info({name})')]
            columns = (baseline or {}).get('columns', {}).get(name, available)
            if not columns or len(set(columns)) != len(columns) or not set(columns) <= set(available):
                raise ValueError(f'原始字段缺失或审计字段无效: {name}')
            result['columns'][name] = columns
            projection = ','.join('"' + field.replace('"', '""') + '"' for field in columns)
            for row in db.execute(f'SELECT {projection} FROM {name} ORDER BY rowid'):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                                         default=str).encode('utf-8'))
                digest.update(b'\n')
                count += 1
            result['tables'][name] = {'count': count, 'sha256': digest.hexdigest()}
        for name, condition in PENDING.items():
            if name in tables:
                result['pending'][name] = db.execute(
                    f'SELECT count(*) FROM {name} WHERE {condition}').fetchone()[0]
        if 'settings' in tables:
            result['settings'] = {key: value for key, value in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('data_version','data_source_updated_at')")}
        return result
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--baseline', type=Path, help='Compare the original columns across additive migrations')
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    print(json.dumps(audit(args.database, baseline), ensure_ascii=False, indent=2, sort_keys=True))
