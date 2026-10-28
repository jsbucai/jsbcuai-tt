#!/usr/bin/env python3
"""Prepare/import only a new isolated sales-history reference database.

This command cannot open a production, fixture or ordinary service runtime as
its destination. It never writes ERP, snapshots, credentials or app settings.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.sales_history_matching import ensure_matching_schema, import_history_snapshot


MARKER = '.isolated-sales-history.json'
DATABASE = 'sales-matching.sqlite3'
PURPOSE = 'isolated-sales-history-validation'


def validate_runtime(path, *, preparing=False):
    requested = Path(path).absolute()
    resolved = requested.resolve()
    if requested != resolved or any(parent.is_symlink() for parent in (requested,*requested.parents)):
        raise ValueError('isolated_runtime_symlink_rejected')
    if not resolved.name.startswith('sales-history-isolated-'):
        raise ValueError('isolated_runtime_name_required')
    forbidden = ('/opt/ul-workbench','/var/lib/ul-', '/app/service/runtime')
    if any(str(resolved).startswith(prefix) for prefix in forbidden) or any(
            token in part.lower() for part in resolved.parts for token in ('production','emulator','fixture')):
        raise ValueError('production_or_fixture_runtime_rejected')
    if preparing:
        if resolved.exists():
            raise ValueError('isolated_runtime_must_be_new')
        if not resolved.parent.is_dir():
            raise ValueError('isolated_runtime_parent_missing')
        return resolved
    marker = resolved / MARKER
    if not marker.is_file() or marker.is_symlink():
        raise ValueError('isolated_runtime_marker_required')
    value = json.loads(marker.read_text(encoding='utf-8'))
    if value.get('purpose') != PURPOSE or value.get('runtime') != str(resolved):
        raise ValueError('isolated_runtime_marker_mismatch')
    database = resolved / DATABASE
    if database.is_symlink() or not database.is_file():
        raise ValueError('isolated_database_missing_or_symlink')
    return resolved


def prepare_runtime(path):
    runtime = validate_runtime(path,preparing=True)
    runtime.mkdir(mode=0o700)
    marker = {'purpose':PURPOSE,'runtime':str(runtime),
              'created_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}
    fd = os.open(runtime / MARKER,os.O_CREAT | os.O_EXCL | os.O_WRONLY,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:
        json.dump(marker,handle,ensure_ascii=False)
    fd = os.open(runtime / DATABASE,os.O_CREAT | os.O_EXCL | os.O_WRONLY,0o600)
    os.close(fd)
    with sqlite3.connect(runtime / DATABASE) as db:
        ensure_matching_schema(db)
    return {'status':'prepared','runtime':str(runtime),'database':DATABASE,'isolated':True}


def import_to_isolated_runtime(runtime,source_db,profile_id,expected_sha256):
    runtime = validate_runtime(runtime)
    with sqlite3.connect(runtime / DATABASE) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(not name.startswith('sales_matching_') for name in tables):
            raise ValueError('isolated_database_contains_other_tables')
        result = import_history_snapshot(db,source_db,profile_id,expected_sha256)
    return {**result,'isolated':True,'runtime':str(runtime)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated-runtime',required=True)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--source-db')
    parser.add_argument('--profile-id')
    parser.add_argument('--expected-sha256')
    args=parser.parse_args(argv)
    if args.prepare:
        if any((args.source_db,args.profile_id,args.expected_sha256)):
            parser.error('--prepare does not accept source/import arguments')
        result=prepare_runtime(args.isolated_runtime)
    else:
        if not all((args.source_db,args.profile_id,args.expected_sha256)):
            parser.error('import requires --source-db, --profile-id, --expected-sha256')
        result=import_to_isolated_runtime(args.isolated_runtime,args.source_db,args.profile_id,args.expected_sha256)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
