"""Maintenance backup/evidence; no ERP requests, no inventory or print mutations."""
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'service/runtime/maintenance/2026-09-10-v0.1.3'
DATABASE = ROOT / 'service/runtime/data/workbench.sqlite3'
PROTECTED = [Path(r'E:\UL\订单扫码系统\app.py'), Path(r'E:\UL\订单扫码系统\data\inventory_adjustment_jobs.json'),
             Path(r'E:\UL\ERP自动化\订单处理_1.xls'), Path(r'E:\UL\ERP自动化\每日更新数据\销售订单统计列表数据.xls'),
             Path(r'E:\UL\ERP自动化\每日更新数据\库存状况数据.xls')]

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def capture(stage):
    TARGET.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE) as db:
        printing = db.execute('SELECT id,status,frozen,metadata,idempotency_key,claim_token_hash FROM print_jobs ORDER BY id').fetchall()
        batches = db.execute('SELECT id,client_id,payload,status,message,document_no,retryable,created_at,updated_at FROM adjustment_batches ORDER BY id').fetchall()
        if stage == 'before':
            if any(row[1] == 'claimed' for row in printing) or any(row[3] in ('queued', 'running') for row in batches):
                raise RuntimeError('Wait for active printing/inventory uploads before reloading service')
            with sqlite3.connect(TARGET / 'workbench-before.sqlite3') as backup:
                db.backup(backup)
            shutil.copyfile(ROOT / 'releases/current.json', TARGET / 'installation-before.json')
    result = {'protected': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in PROTECTED},
              'printing_hash': digest(printing), 'adjustment_hash': digest(batches),
              'print_statuses': {s: sum(row[1] == s for row in printing) for s in {row[1] for row in printing}},
              'adjustment_statuses': {s: sum(row[3] == s for row in batches) for s in {row[3] for row in batches}}}
    (TARGET / f'{stage}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    if stage == 'after':
        before = json.loads((TARGET / 'before.json').read_text('utf-8'))
        for key in ('protected', 'printing_hash', 'adjustment_hash'):
            if result[key] != before[key]:
                raise RuntimeError(f'{key} changed during maintenance; inspect concurrent business activity')
    print(json.dumps({'stage': stage, 'printing': result['print_statuses'], 'adjustments': result['adjustment_statuses']}, ensure_ascii=False))

if __name__ == '__main__':
    if sys.argv[1:] not in (['before'], ['after']):
        raise SystemExit('Expected before or after')
    capture(sys.argv[1])
