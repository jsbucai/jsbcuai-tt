"""One-way snapshots of approved business files into this service's own runtime."""
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .store import ROOT, guarded_runtime
from .import_safety import import_lock

SOURCES = {
    'orders': (r'E:\UL\ERP自动化\订单处理_1.xls', 'orders.xls'),
    'sales': (r'E:\UL\ERP自动化\每日更新数据\销售订单统计列表数据.xls', 'sales.xls'),
    'inventory': (r'E:\UL\ERP自动化\每日更新数据\库存状况数据.xls', 'inventory.xls'),
}


def capture(runtime=None):
    runtime = guarded_runtime(runtime or ROOT / 'runtime' / 'data')
    destination = runtime / 'snapshots'
    if not destination.resolve().is_relative_to(runtime):
        raise ValueError('快照目录必须留在本服务数据目录内')
    destination.mkdir(exist_ok=True)
    with import_lock(runtime), tempfile.TemporaryDirectory(prefix='capture-', dir=runtime) as directory:
        staged = Path(directory)
        records = {}
        for key, (original, filename) in SOURCES.items():
            source, copied = Path(original), staged / filename
            for attempt in range(3):
                before = source.stat()
                shutil.copyfile(source, copied)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                    break
                if attempt == 2:
                    raise RuntimeError(f'源文件正在更新，本次未发布：{source.name}')
                time.sleep(.2)
            records[key] = {'filename': filename, 'source': str(source), 'sha256': hashlib.sha256(copied.read_bytes()).hexdigest(), 'size': after.st_size, 'source_mtime': datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat()}
        import xlrd
        workbook = None
        needs_recovery = False
        try:
            workbook = xlrd.open_workbook(str(staged / 'sales.xls'), on_demand=True)
            needs_recovery = not workbook.nsheets or not workbook.sheet_by_index(0).nrows
        except xlrd.XLRDError:
            needs_recovery = True
        finally:
            if workbook is not None:
                workbook.release_resources()
        if needs_recovery:
            from .normalize_snapshot import normalize
            normalize(('sales',), snapshot_dir=staged)
        manifest = {'mode': 'android-service-source-snapshot', 'created_at': datetime.now(timezone.utc).isoformat(), 'files': records}
        (staged / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        generations = destination / 'generations'
        if not generations.resolve().is_relative_to(destination):
            raise ValueError('快照版本目录不能指向服务目录外')
        generations.mkdir(exist_ok=True)
        generation = generations / uuid.uuid4().hex
        os.replace(staged, generation)
        pointer = destination / ('current-' + uuid.uuid4().hex + '.tmp')
        with pointer.open('w', encoding='utf-8') as file:
            json.dump({'generation': generation.relative_to(destination).as_posix(), 'created_at': manifest['created_at']}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(pointer, destination / 'current.json')
        return manifest
