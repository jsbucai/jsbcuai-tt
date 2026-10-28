"""Wait for the API update queue to drain before starting a browser trial."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
import time


DEFAULT_DATABASE = Path('/opt/ul-workbench/service/runtime/data/workbench.sqlite3')


def api_updates_idle(database: Path) -> bool:
    """Read the live SQLite queue without initializing or changing the API store."""
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        return db.execute(
            "SELECT 1 FROM update_jobs WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone() is None


def wait_for_api_idle(database: Path, max_wait_sec: float, poll_sec: float,
                      *, clock=time.monotonic, sleep=time.sleep) -> bool:
    deadline = clock() + max_wait_sec
    while True:
        if api_updates_idle(database):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(poll_sec, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--max-wait-sec', type=float, default=1800)
    parser.add_argument('--poll-sec', type=float, default=5)
    args = parser.parse_args()
    if args.max_wait_sec < 0 or args.poll_sec <= 0:
        parser.error('等待时间与轮询间隔必须有效')
    try:
        if wait_for_api_idle(args.database, args.max_wait_sec, args.poll_sec):
            return 0
    except (OSError, sqlite3.Error):
        print('API 更新状态读取失败，本轮采购试验未启动浏览器', file=sys.stderr)
        return 78
    print('API 更新仍在排队或运行，本轮采购试验等待超时且未启动浏览器', file=sys.stderr)
    return 75


if __name__ == '__main__':
    raise SystemExit(main())
