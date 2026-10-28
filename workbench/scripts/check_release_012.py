"""Local release evidence only: never invokes the ERP sender or a printer."""
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'service'))
from core.erp_adjustments import TEMPLATE_PATH, write_adjustment_import_file
import xlrd

TARGET = ROOT / 'service/runtime/maintenance/2026-09-10-v0.1.2'
TARGET.mkdir(parents=True, exist_ok=True)
database = ROOT / 'service/runtime/data/workbench.sqlite3'
protected = [Path(r'E:\UL\订单扫码系统\app.py'), Path(r'E:\UL\订单扫码系统\data\inventory_adjustment_jobs.json'),
             Path(r'E:\UL\ERP自动化\订单处理_1.xls'),
             Path(r'E:\UL\ERP自动化\每日更新数据\销售订单统计列表数据.xls'),
             Path(r'E:\UL\ERP自动化\每日更新数据\库存状况数据.xls')]

def snapshot(name):
    with sqlite3.connect(database) as db:
        ledger = db.execute('SELECT id,status,frozen,metadata,idempotency_key,claim_token_hash FROM print_jobs ORDER BY id').fetchall()
        batches = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='adjustment_batches'").fetchone()
        states = db.execute('SELECT status,COUNT(*) FROM adjustment_batches GROUP BY status').fetchall() if batches else []
        if name == 'before':
            if any(status in ('claimed', 'unknown') for _, status, *_ in ledger):
                raise RuntimeError('Unresolved printing needs review before service reload')
            if any(status in ('running', 'queued') for status, _ in states):
                raise RuntimeError('Pending stock uploads prevent service reload')
            with sqlite3.connect(TARGET / 'workbench-before.sqlite3') as backup:
                db.backup(backup)
    record = {'protected': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
              'print_ledger_hash': hashlib.sha256(json.dumps(ledger, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
              'print_statuses': {status: sum(row[1] == status for row in ledger) for _, status, *_ in ledger},
              'adjustment_states': states}
    (TARGET / f'{name}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    if name == 'before':
        shutil.copyfile(ROOT / 'releases/current.json', TARGET / 'installation-before.json')
    else:
        before = json.loads((TARGET / 'before.json').read_text(encoding='utf-8'))
        assert before['print_ledger_hash'] == record['print_ledger_hash'], 'Print ledger changed'
        assert before['protected'] == record['protected'], 'Protected production/source files changed'
    print(json.dumps({'snapshot': name, 'protected_files': len(protected), 'print_statuses': record['print_statuses'],
                      'adjustment_states': states}, ensure_ascii=False))

def template_check():
    # These files are deliberately outside the uploader batch directories.
    rows = [{'sku': 'PREVIEW_ONLY_001', 'quantity': 2.5, 'note': '本地模板检查，不得导入 ERP'},
            {'sku': 'PREVIEW_ONLY_002', 'quantity': 1.0, 'note': 'DIN933  M12*40'}]
    for kind in ('loss', 'overflow'):
        path = TARGET / f'NEVER_UPLOAD-template-{kind}.xls'
        write_adjustment_import_file(TEMPLATE_PATH, path, kind, rows, 'PREVIEW-DO-NOT-UPLOAD')
        book = xlrd.open_workbook(str(path))
        try:
            sheet = book.sheet_by_index(0)
            headers = {str(sheet.cell_value(0, col)).strip(): col for col in range(sheet.ncols)}
            assert sheet.cell_value(1, headers['商品编号']) == 'PREVIEW_ONLY_001'
            assert sheet.cell_value(2, headers['商品编号']) == 'PREVIEW_ONLY_002'
            assert sheet.cell_value(1, headers['数量']) == 2.5
            assert sheet.cell_value(2, headers['明细备注']) == 'DIN933  M12*40'
            assert sheet.cell_value(1, headers['单据类型']) == ('报损单' if kind == 'loss' else '报溢单')
        finally:
            book.release_resources()
    print('Two local template copies verified; ERP was not opened or uploaded.')

if __name__ == '__main__':
    if sys.argv[1:] == ['templates']:
        template_check()
    elif sys.argv[1:] in (['before'], ['after']):
        snapshot(sys.argv[1])
    else:
        raise SystemExit('Expected before, after or templates')
