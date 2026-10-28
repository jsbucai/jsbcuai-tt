"""Cheap live disk headroom; never scan tables on request paths."""
import shutil
from pathlib import Path


def capacity(runtime):
    runtime = Path(runtime)
    disk = shutil.disk_usage(runtime)
    database = runtime / 'workbench.sqlite3'
    size = database.stat().st_size if database.is_file() else 0
    reserve = max(1024**3, size * 2 + 256 * 1024**2)
    return {'free_bytes': disk.free, 'total_bytes': disk.total,
            'used_percent': round((disk.total - disk.free) * 100 / disk.total, 1),
            'database_bytes': size, 'reserve_bytes': reserve,
            'status': 'critical' if disk.free < reserve else 'warning' if disk.free < disk.total * .2 else 'ok'}
