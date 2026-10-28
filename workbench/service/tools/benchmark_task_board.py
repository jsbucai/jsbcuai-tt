"""Benchmark read-only task-list queries on an isolated database copy.

The source application initializes additive schema on startup, so never point
this tool at the live database. It prints timings and counts, never task data.
"""
import argparse
import json
import statistics
import time
from pathlib import Path

from werkzeug.security import generate_password_hash

from app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('isolated_runtime', type=Path)
    args = parser.parse_args()
    runtime = args.isolated_runtime.resolve()
    if not (runtime / 'workbench.sqlite3').is_file():
        raise SystemExit('isolated database not found')
    app = create_app({'SOURCE_MODE': 'api', 'PUBLIC_MODE': True,
        'AUTO_IMPORT': False, 'ADMIN_PASSWORD_HASH': generate_password_hash('benchmark-only'),
        'ERP_CLIENT': object(), 'RUNTIME_DIR': runtime})
    services = app.extensions['service']
    with services['store'].connect() as db:
        row = db.execute('SELECT id,roles,is_admin,active FROM staff_users WHERE is_admin=1 AND active=1 LIMIT 1').fetchone()
        if not row:
            raise SystemExit('no active administrator in isolated copy')
        user = {'id': row['id'], 'roles': json.loads(row['roles']),
                'is_admin': bool(row['is_admin']), 'active': bool(row['active'])}
    for role in ('sales', 'stock_packing'):
        board = services['task_board']
        first = board.list(user, role, view='role', page=1)
        durations = []
        for _ in range(30):
            started = time.perf_counter()
            board.list(user, role, view='role', page=1)
            durations.append((time.perf_counter() - started) * 1000)
        durations.sort()
        print(json.dumps({'role': role, 'total': first['total'],
            'first_page': len(first['items']), 'median_ms': round(statistics.median(durations), 2),
            'p95_ms': round(durations[28], 2), 'max_ms': round(durations[-1], 2)}))


if __name__ == '__main__':
    main()
