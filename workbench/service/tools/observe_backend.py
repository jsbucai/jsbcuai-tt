"""Finite, read-only resource sampling; does not start app services or ERP calls."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path('/opt/ul-workbench')
DATA = ROOT / 'service/runtime/data'
END = datetime(2026, 10, 16, tzinfo=ZoneInfo('Asia/Shanghai'))


def command(args):
    return subprocess.check_output(args, text=True, timeout=5).strip()


def sample():
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if now >= END:
        return {'status': 'observation_complete'}
    disk = shutil.disk_usage(DATA)
    mem = {line.split(':')[0]: int(line.split()[1]) * 1024
           for line in Path('/proc/meminfo').read_text().splitlines()}
    result = {'checked_at': now.isoformat(), 'load': os.getloadavg(),
              'memory_available_bytes': mem['MemAvailable'],
              'memory_total_bytes': mem['MemTotal'], 'disk_free_bytes': disk.free,
              'disk_total_bytes': disk.total,
              'files': {name: (DATA / name).stat().st_size if (DATA / name).exists() else 0
                        for name in ('workbench.sqlite3', 'workbench.sqlite3-wal')}}
    try:
        # Restrict Docker output to operational fields; never collect env/secrets.
        container = json.loads(command(['docker', 'inspect', '--format',
            '{{json .State}}', 'ul-workbench-workbench-1']))
        result['container'] = {key: container.get(key) for key in
            ('Status', 'Running', 'OOMKilled', 'Restarting', 'ExitCode', 'StartedAt')}
        result['restart_count'] = int(command(['docker', 'inspect', '--format',
            '{{.RestartCount}}', 'ul-workbench-workbench-1']))
        pid = container.get('Pid', 0)
        if pid:
            relative = Path(f'/proc/{pid}/cgroup').read_text().strip().split('::', 1)[1]
            cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
            result['cgroup'] = {name: (cgroup / name).read_text().strip()
                               for name in ('memory.current', 'memory.peak', 'memory.events',
                                            'memory.stat', 'cpu.stat', 'memory.pressure')
                               if (cgroup / name).is_file()}
    except Exception as error:
        result['container_error'] = type(error).__name__
    started = time.monotonic()
    try:
        with urllib.request.urlopen('http://192.168.100.31:5191/livez', timeout=4) as response:
            result['health_ok'] = response.status == 200 and bool(json.load(response).get('ok'))
    except Exception as error:
        result.update(health_ok=False, health_error=type(error).__name__)
    result['health_ms'] = round((time.monotonic() - started) * 1000, 2)
    try:
        with sqlite3.connect(f'file:{DATA}/workbench.sqlite3?mode=ro', uri=True, timeout=2) as db:
            result['source'] = {key: json.loads(value) for key, value in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('data_version','data_source_updated_at')")}
            result['jobs'] = [dict(zip(('id', 'mode', 'status', 'stage', 'created_at', 'updated_at'), row))
                              for row in db.execute('SELECT id,mode,status,stage,created_at,updated_at '
                                                    'FROM update_jobs ORDER BY rowid DESC LIMIT 3')]
            result['queued_updates'] = db.execute("SELECT count(*) FROM update_jobs WHERE status='queued'").fetchone()[0]
            result['running_updates'] = db.execute("SELECT count(*) FROM update_jobs WHERE status='running'").fetchone()[0]
    except Exception as error:
        result['database_error'] = type(error).__name__
    report = DATA / 'observation-20261012'
    report.mkdir(mode=0o700, exist_ok=True)
    path = report / (now.strftime('%Y-%m-%d') + '.jsonl')
    if path.exists() and path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('observation file unexpectedly large')
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n')
    return {'status': 'sampled', 'checked_at': result['checked_at'],
            'health_ok': result['health_ok'], 'health_ms': result['health_ms'],
            'disk_free_bytes': disk.free, 'memory_available_bytes': mem['MemAvailable']}


if __name__ == '__main__':
    print(json.dumps(sample(), ensure_ascii=False))
