"""Consistent SQLite backup, encrypted before NAS copy; offline restore with write hold."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC=b'ULBACKUP1'
BLOB_NAME=re.compile(r'[0-9a-f]{32}\.bin\Z')
CHUNK=1024*1024


def _attachment_rows(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_assistant_attachments'").fetchone():
        return []
    return db.execute('SELECT blob_name,sha256,size FROM sales_assistant_attachments ORDER BY blob_name').fetchall()


def _checked_blob(path, expected_hash, expected_size):
    if path.is_symlink() or not path.is_file():
        raise ValueError('录单附件缺失或不是普通文件')
    raw=path.read_bytes()
    if len(raw)!=expected_size or hashlib.sha256(raw).hexdigest()!=expected_hash:
        raise ValueError('录单附件与数据库不一致')
    return raw


def backup(runtime, output, keyfile):
    runtime=Path(runtime).resolve();output=Path(output);key=Path(keyfile).read_bytes()
    if len(key)!=32: raise ValueError('备份密钥必须为32字节')
    if output.exists(): raise ValueError('不覆盖已有备份')
    # The hardened service has a small tmpfs; a real SQLite copy can exceed it.
    # Stage beside the source database on the VM's durable local filesystem and
    # remove the plaintext copy before returning.
    with tempfile.TemporaryDirectory(prefix='.backup-stage-', dir=runtime) as directory:
        path=Path(directory)/'workbench.sqlite3';zip_path=Path(directory)/'archive.zip'
        with sqlite3.connect(f'file:{runtime / "workbench.sqlite3"}?mode=ro',uri=True) as source, sqlite3.connect(path) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('数据库校验失败')
        with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as snapshot:
            attachment_rows=_attachment_rows(snapshot)
        blob_dir=runtime/'sales-assistant'/'blobs'
        if attachment_rows and (blob_dir.is_symlink() or not blob_dir.resolve().is_relative_to(runtime)):
            raise ValueError('录单附件目录无效')
        with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(path,'workbench.sqlite3')
            for name in ('erp.json','admin-password.hash'):
                secret=runtime/'secrets'/name
                if secret.is_file() and not secret.is_symlink():archive.write(secret,'secrets/'+name)
            for blob_name,digest,size in attachment_rows:
                if not BLOB_NAME.fullmatch(blob_name):
                    raise ValueError('录单附件名称无效')
                raw=_checked_blob(blob_dir/blob_name,digest,size)
                archive.writestr('sales-assistant/blobs/'+blob_name,raw)
            archive.writestr('manifest.json',json.dumps({
                'version':2,'restoration_requires_write_hold':True,
                'sales_attachment_count':len(attachment_rows)}))
        nonce=os.urandom(12)
        encryptor=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(MAGIC)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as target, zip_path.open('rb') as source:
                target.write(MAGIC+nonce)
                while chunk:=source.read(CHUNK):target.write(encryptor.update(chunk))
                target.write(encryptor.finalize()+encryptor.tag)
                target.flush();os.fsync(target.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise


def restore(backup_file, destination, keyfile):
    destination=Path(destination)
    if destination.exists():raise ValueError('只恢复到全新目录；先停服并保留原运行目录')
    key=Path(keyfile).read_bytes()
    if len(key)!=32:raise ValueError('备份密钥必须为32字节')
    with tempfile.TemporaryDirectory(prefix='.restore-check-',dir=destination.parent) as check_dir:
        zip_path=Path(check_dir)/'archive.zip'
        with Path(backup_file).open('rb') as source:
            prefix=source.read(len(MAGIC)+12)
            if len(prefix)!=len(MAGIC)+12 or not prefix.startswith(MAGIC):
                raise ValueError('备份格式无效')
            length=Path(backup_file).stat().st_size-len(prefix)-16
            if length<0:raise ValueError('备份格式无效')
            source.seek(-16,os.SEEK_END);tag=source.read(16)
            source.seek(len(prefix))
            decryptor=Cipher(algorithms.AES(key),modes.GCM(prefix[len(MAGIC):],tag)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            with zip_path.open('wb') as target:
                while length:
                    chunk=source.read(min(CHUNK,length))
                    if not chunk:raise ValueError('备份内容截断')
                    length-=len(chunk);target.write(decryptor.update(chunk))
                target.write(decryptor.finalize())
        with zipfile.ZipFile(zip_path) as archive:
            allowed={'workbench.sqlite3','manifest.json','secrets/erp.json','secrets/admin-password.hash'}
            names=set(archive.namelist())
            if len(names)!=len(archive.namelist()) or any(
                name not in allowed and not (
                    name.startswith('sales-assistant/blobs/') and
                    BLOB_NAME.fullmatch(name.removeprefix('sales-assistant/blobs/')))
                for name in names):
                raise ValueError('备份含非预期文件')
            if 'workbench.sqlite3' not in names or 'manifest.json' not in names:
                raise ValueError('备份缺少必要文件')
            manifest=json.loads(archive.read('manifest.json'))
            if manifest.get('version') not in (1,2):raise ValueError('备份版本无效')
            check_db=Path(check_dir)/'workbench.sqlite3'
            check_db.write_bytes(archive.read('workbench.sqlite3'))
            with sqlite3.connect(f'file:{check_db}?mode=ro',uri=True) as snapshot:
                if snapshot.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise ValueError('恢复数据库不完整')
                attachment_rows=_attachment_rows(snapshot)
            expected={'sales-assistant/blobs/'+row[0] for row in attachment_rows}
            if names-allowed!=expected or manifest.get('sales_attachment_count',0)!=len(attachment_rows):
                raise ValueError('录单附件清单与数据库不一致')
            for blob_name,digest,size in attachment_rows:
                if not BLOB_NAME.fullmatch(blob_name):raise ValueError('录单附件名称无效')
                content=archive.read('sales-assistant/blobs/'+blob_name)
                if len(content)!=size or hashlib.sha256(content).hexdigest()!=digest:
                    raise ValueError('备份中的录单附件校验失败')
            destination.mkdir(mode=0o700)
            for name in sorted(names):
                path=destination/name;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as f:f.write(archive.read(name))
    with sqlite3.connect(destination/'workbench.sqlite3') as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('恢复数据库不完整')
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.execute("INSERT OR REPLACE INTO settings VALUES ('restoration_hold','true')")
        if 'auth_sessions' in tables:db.execute('DELETE FROM auth_sessions')
        if 'update_jobs' in tables:db.execute("UPDATE update_jobs SET status='unknown',stage='restored' WHERE status IN ('queued','running')")
        if 'freight_attempts' in tables:db.execute("UPDATE freight_attempts SET status='unknown' WHERE status='sending'")
        if 'adjustment_batches' in tables:db.execute("UPDATE adjustment_batches SET status='unknown',retryable=0 WHERE status IN ('queued','running')")
        if 'print_jobs' in tables:db.execute("UPDATE print_jobs SET status='unknown' WHERE status IN ('prepared','claimed')")
        if 'order_print_states' in tables:
            # A reservation may already have produced paper before the backup or
            # restore boundary.  Keep it out of the unprinted queue until the
            # operator explicitly checks it.
            restored_at=datetime.now().isoformat(timespec='seconds')
            reservations=db.execute("""SELECT profile_id,order_no,device_id,job_id
                FROM order_print_states WHERE state='reserved'""").fetchall()
            if 'order_print_audit' in tables:
                db.executemany("""INSERT INTO order_print_audit
                    (id,profile_id,order_no,from_state,to_state,device_id,job_id,reason,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",[
                    (uuid.uuid4().hex,row[0],row[1],'reserved','uncertain',row[2],row[3],
                     'restored_during_print',restored_at) for row in reservations])
            db.execute("""UPDATE order_print_states
                SET state='uncertain',reason='restored_during_print',updated_at=?
                WHERE state='reserved'""",(restored_at,))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['backup','restore'])
    parser.add_argument('--runtime',type=Path,required=True);parser.add_argument('--file',type=Path,required=True)
    parser.add_argument('--key-file',type=Path,required=True);args=parser.parse_args()
    if args.operation=='backup':backup(args.runtime,args.file,args.key_file)
    else:restore(args.file,args.runtime,args.key_file)
    print('完成；恢复后保持业务写入暂停，需核对备份之后的操作记录。')
