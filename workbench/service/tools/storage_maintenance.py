"""Nightly bounded maintenance; no business records or live attachments deleted."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.maintenance_archive import file_chunks, write_archive
from core.storage_capacity import capacity
from core.storage_retention import archive_calculations, busy


def verified_backup(folder, key, scratch, now):
    candidates = sorted((p for p in folder.glob('*.ulbackup') if p.is_file() and not p.is_symlink()),
                        key=lambda p:p.stat().st_mtime, reverse=True)
    if not candidates or now.timestamp()-candidates[0].stat().st_mtime > 48*3600:
        raise ValueError('没有48小时内NAS备份，保留全部原数据')
    source = candidates[0]
    before = source.stat()
    with tempfile.TemporaryDirectory(prefix='.retention-check-', dir=scratch) as d:
        target = Path(d)/'archive.zip'
        with source.open('rb') as src, target.open('wb') as out:
            prefix=src.read(21)
            if not prefix.startswith(b'ULBACKUP1'): raise ValueError('备份格式无效')
            length=before.st_size-21-16
            if length < 0: raise ValueError('备份截断')
            src.seek(-16,os.SEEK_END)
            decryptor=Cipher(algorithms.AES(key),modes.GCM(prefix[9:],src.read(16))).decryptor()
            decryptor.authenticate_additional_data(b'ULBACKUP1');src.seek(21)
            while length:
                chunk=src.read(min(1024**2,length))
                if not chunk: raise ValueError('备份截断')
                length-=len(chunk);out.write(decryptor.update(chunk))
            out.write(decryptor.finalize())
        with zipfile.ZipFile(target) as z:
            if 'workbench.sqlite3' not in z.namelist() or 'manifest.json' not in z.namelist() or z.testzip():
                raise ValueError('NAS备份内容校验失败')
            manifest=json.loads(z.read('manifest.json'))
            if manifest.get('version') not in (1,2): raise ValueError('备份版本不支持')
    after=source.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('校验期间备份变化')
    return source


def backup_survivors(files, now):
    """30 daily + 8 weekly + 12 monthly; newest 10 named release checkpoints."""
    ordered=sorted(files,key=lambda p:p.stat().st_mtime,reverse=True)
    keep=set(ordered[:3]);daily=set();weekly=set();monthly=set();special=0
    for p in ordered:
        stamp=datetime.fromtimestamp(p.stat().st_mtime, now.tzinfo)
        age=(now-stamp).total_seconds()/86400
        if not re.fullmatch(r'ul-\d{8}T\d{6}Z-\d+\.ulbackup',p.name):
            special+=1
            if special<=10 or age<14: keep.add(p)
            continue
        day=stamp.date();week=stamp.isocalendar()[:2];month=(stamp.year,stamp.month)
        if age<30 and day not in daily: keep.add(p);daily.add(day)
        if age<56 and week not in weekly: keep.add(p);weekly.add(week)
        if age<366 and month not in monthly: keep.add(p);monthly.add(month)
    return keep


def move_old_database_copies(runtime, destination, key, now):
    moved=[]
    for name, retain in (('migration-backups',2),('backups',0)):
        folder=runtime/name
        if not folder.is_dir() or folder.is_symlink(): continue
        files=sorted((p for p in folder.glob('*.sqlite3') if p.is_file() and not p.is_symlink()),
                     key=lambda p:p.stat().st_mtime,reverse=True)
        for p in files[retain:]:
            if now.timestamp()-p.stat().st_mtime < 86400: continue
            wal=p.with_name(p.name+'-wal')
            if wal.exists() and (wal.is_symlink() or wal.stat().st_size): continue
            before=p.stat()
            # Only frozen backup directories are considered, never the live DB.
            with closing(sqlite3.connect('file:'+str(p)+'?mode=ro&immutable=1',uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': continue
            archive=destination/('database-'+uuid.uuid4().hex+'.ulcold')
            receipt=write_archive(archive,file_chunks(p),key)
            after=p.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                raise ValueError('备份副本发生变化，源文件保留')
            record={**receipt,'original':str(p.relative_to(runtime)), 'archived_at':now.isoformat()}
            receipt_path=archive.with_suffix('.json')
            with receipt_path.open('x') as f:
                json.dump(record,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
            p.unlink()
            for suffix in ('-shm','-wal'):
                side=p.with_name(p.name+suffix)
                if side.is_file() and not side.is_symlink() and (suffix=='-shm' or side.stat().st_size==0): side.unlink()
            moved.append(record)
    return moved


def trim_image_cache(runtime, db, now, budget=512*1024**2):
    folder=runtime/'product-images'
    if not folder.is_dir() or folder.is_symlink(): return {'removed':0,'bytes':0}
    referenced={r[0] for r in db.execute('SELECT cache_path FROM product_images WHERE cache_path<>\'\'')}
    files=[p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and re.fullmatch(r'[0-9a-f]{32}\.bin',p.name)]
    total=sum(p.stat().st_size for p in files);removed=freed=0
    for p in sorted(files,key=lambda p:p.stat().st_atime):
        s=p.stat()
        orphan=p.name not in referenced and now.timestamp()-s.st_mtime>86400
        cold=total>budget and now.timestamp()-max(s.st_atime,s.st_mtime)>7*86400
        if orphan or cold:
            p.unlink();removed+=1;freed+=s.st_size;total-=s.st_size
    return {'removed':removed,'bytes':freed,'remaining_bytes':total,'target_bytes':budget}


def run(runtime, nas, apply=False):
    runtime=Path(runtime).resolve();nas=Path(nas).resolve()
    if not os.path.ismount(nas): raise ValueError('NAS未挂载，停止归档和清理')
    root=nas/'开发项目/UL随行'
    if not root.is_dir() or root.is_symlink(): raise ValueError('NAS项目目录无效')
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    db=sqlite3.connect('file:'+str(runtime/'workbench.sqlite3')+'?mode=ro',uri=True)
    with db:
        if busy(db): return {'status':'deferred_busy','checked_at':now.isoformat()}
    backup_dir=root/'N150备份'
    files=[p for p in backup_dir.glob('*.ulbackup') if p.is_file() and not p.is_symlink()]
    keep=backup_survivors(files,now)
    report={'status':'preview','checked_at':now.isoformat(),'before':capacity(runtime),
            'backup_files':len(files),'backup_keep':len(keep),'backup_retire':len(files)-len(keep)}
    if not apply: db.close();return report
    key=(runtime/'secrets/backup.key').read_bytes()
    if len(key)!=32: raise ValueError('备份密钥无效')
    verified=verified_backup(backup_dir,key,runtime,now)
    report['verified_backup']=verified.name
    cold=root/'冷归档';calc=cold/'同步计算';static=cold/'旧数据库副本'
    for directory in (cold,calc,static):
        if directory.is_symlink(): raise ValueError('归档目录不得为链接')
        directory.mkdir(mode=0o700,exist_ok=True)
    report['database_copies']=move_old_database_copies(runtime,static,key,now)
    # Use naive Shanghai time to match existing DB created_at values.
    report['calculations']=archive_calculations(str(runtime/'workbench.sqlite3'),calc,key,now.replace(tzinfo=None))
    if report['calculations']['skipped_busy']:
        report['status']='partial_busy'
    else:
        report['status']='completed'
        report['images']=trim_image_cache(runtime,db,now)
        retired=[]
        for p in files:
            if p not in keep and p!=verified:
                retired.append({'name':p.name,'bytes':p.stat().st_size});p.unlink()
        report['retired_backups']=retired
        # Only rebuildable calculation archives expire; original business DB
        # archives and all live business/financial/print ledgers are retained.
        expired=[]
        for p in calc.glob('calc-*.ulcold'):
            if p.is_file() and not p.is_symlink() and now.timestamp()-p.stat().st_mtime>90*86400:
                expired.append(p.name);p.unlink()
        report['expired_calculation_archives']=expired
    db.close()
    report['after']=capacity(runtime)
    target=runtime/'storage-maintenance.json';temporary=target.with_suffix('.tmp')
    with temporary.open('w') as f:
        json.dump(report,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temporary,target)
    # Keep one report per date for 90 days; avoid unbounded cron output files.
    history=cold/'维护记录';history.mkdir(mode=0o700,exist_ok=True)
    path=history/(now.date().isoformat()+'.json');path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for old in history.glob('*.json'):
        if old.is_file() and not old.is_symlink() and now.timestamp()-old.stat().st_mtime>90*86400: old.unlink()
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--nas',type=Path,required=True)
    p.add_argument('--apply',action='store_true');args=p.parse_args()
    with (args.runtime/'.storage-maintenance.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: print(json.dumps({'status':'already_running'}));sys.exit(0)
        result=run(args.runtime,args.nas,args.apply)
    print(json.dumps(result,ensure_ascii=False))
