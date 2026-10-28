import json
import hashlib
import io
import os
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backup import MAGIC, backup, restore
from core.store import Store
from core.update_jobs import UpdateJobs
from core.print_jobs import JobConflict
from core.order_print_state import ensure_schema as ensure_order_print_schema


def test_encrypted_backup_restore_keeps_cache_and_blocks_replay():
    (ROOT/'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
        root=Path(temp);store=Store(root/'live');jobs=UpdateJobs(store,None,lambda p:True)
        ensure_order_print_schema(store)
        identity=str(uuid.uuid4());jobs.enqueue(identity,'update')
        with store.connect(write=True) as db:
            db.execute("INSERT INTO freight_cache VALUES ('1','A#1','{}','version','today')")
            db.execute("""INSERT INTO inventory_movements(
                event_id,profile_id,warehouse_id,product_id,sku_id,base_unit,
                source_bill_id,source_line_id,document_no,document_type_code,
                document_type,business_date,posted_at,raw_quantity,signed_quantity,
                source_hash,sync_token,updated_at)
                VALUES ('event','1','08390','p','s','1','bill','line','NO','34',
                '进货入库单','2026-09-29','2026-09-29T12:00:00+08:00','2','2',
                'hash','sync','today')""")
            db.execute("""INSERT INTO inventory_snapshots VALUES
                ('1','08390','p','s','1','2','1','today','version','reconciled',
                 '2026-08-07','2026-09-29','')""")
            db.execute("""INSERT INTO inventory_sync_watermarks VALUES
                ('1','08390','billcenter','today','2026-08-07','2026-09-29','today',
                 'succeeded','{}','today')""")
            db.execute("""INSERT INTO product_images(
                image_id,profile_id,product_id,source_url,last_seen_at)
                VALUES ('image','1','p','https://erp.qiniu.wsgjp.com/a.png','today')""")
            db.execute("CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY,expires REAL)")
            db.execute("INSERT INTO auth_sessions VALUES ('hash',9999999999)")
            db.execute("INSERT INTO packing_product_owners VALUES ('group','stock_packing','packer','today')")
            db.execute("INSERT INTO daily_work_selections VALUES ('selection','packer','stock_packing','version',1,'{}','today')")
            db.execute("INSERT INTO daily_work_batches VALUES ('request','packer','request-hash','{\"ok\":true}','today')")
            db.execute("INSERT INTO task_board_followups VALUES ('item','wait','采购','2026-10-06','已确认','today')")
            db.execute("""INSERT INTO order_print_states
                (profile_id,order_no,state,device_id,job_id,fingerprint,reason,updated_at)
                VALUES ('1','ORDER-A','reserved','phone-a','print-a','hash','','today')""")
        key=root/'backup.key';key.write_bytes(os.urandom(32))
        archive=root/'backup.ul';backup(store.runtime,archive,key)
        assert b'SQLite format' not in archive.read_bytes()
        assert not list(store.runtime.glob('.backup-stage-*'))
        restore(archive,root/'restored',key)
        restored=Store(root/'restored');new=UpdateJobs(restored,None,lambda p:True)
        assert new.get(identity)['status']=='unknown'
        with restored.connect() as db:
            assert db.execute('SELECT count(*) FROM freight_cache').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM inventory_movements').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM inventory_snapshots').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM inventory_sync_watermarks').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM product_images').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0]==0
            assert db.execute('SELECT assignee_id FROM packing_product_owners').fetchone()[0]=='packer'
            assert db.execute('SELECT id FROM daily_work_selections').fetchone()[0]=='selection'
            assert json.loads(db.execute('SELECT result FROM daily_work_batches').fetchone()[0])=={'ok':True}
            assert db.execute('SELECT promised_date FROM task_board_followups').fetchone()[0]=='2026-10-06'
            restored_print=db.execute("SELECT state,reason,updated_at FROM order_print_states WHERE order_no='ORDER-A'").fetchone()
            assert tuple(restored_print[:2])==('uncertain','restored_during_print')
            assert 'T' in restored_print['updated_at']
            audit=db.execute("""SELECT from_state,to_state,device_id,job_id,reason,created_at
                FROM order_print_audit WHERE order_no='ORDER-A'""").fetchone()
            assert tuple(audit[:5])==('reserved','uncertain','phone-a','print-a','restored_during_print')
            assert 'T' in audit['created_at']
        with pytest.raises(JobConflict):new.enqueue(str(uuid.uuid4()),'update')
        assert new.process_one() is False
        bad=root/'bad.key';bad.write_bytes(os.urandom(32))
        with pytest.raises(Exception):restore(archive,root/'wrong',bad)
        assert not (root/'wrong').exists()


def test_backup_restores_sales_attachments_and_rejects_missing_blob():
    (ROOT/'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
        root=Path(temp);store=Store(root/'live')
        content=b'order attachment sample'
        name=uuid.uuid4().hex+'.bin'
        blobs=store.runtime/'sales-assistant'/'blobs'
        blobs.mkdir(parents=True)
        (blobs/name).write_bytes(content)
        with store.connect(write=True) as db:
            db.execute('CREATE TABLE sales_assistant_attachments(blob_name TEXT,sha256 TEXT,size INTEGER)')
            db.execute('INSERT INTO sales_assistant_attachments VALUES (?,?,?)',
                       (name,hashlib.sha256(content).hexdigest(),len(content)))
        key=root/'backup.key';key.write_bytes(os.urandom(32))
        archive=root/'backup.ul';backup(store.runtime,archive,key)
        restore(archive,root/'restored',key)
        assert (root/'restored'/'sales-assistant'/'blobs'/name).read_bytes()==content
        (blobs/name).unlink()
        with pytest.raises(ValueError,match='录单附件'):
            backup(store.runtime,root/'incomplete.ul',key)
        assert not (root/'incomplete.ul').exists()


def test_streaming_restore_accepts_existing_v1_backup_format():
    (ROOT/'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
        root=Path(temp);store=Store(root/'live')
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(store.path,'workbench.sqlite3')
            archive.writestr('manifest.json',json.dumps({'version':1,'restoration_requires_write_hold':True}))
        key=root/'backup.key';key.write_bytes(os.urandom(32))
        nonce=os.urandom(12)
        old=root/'old.ul';old.write_bytes(MAGIC+nonce+AESGCM(key.read_bytes()).encrypt(nonce,payload.getvalue(),MAGIC))
        restore(old,root/'restored',key)
        with sqlite3.connect(root/'restored'/'workbench.sqlite3') as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert db.execute("SELECT value FROM settings WHERE key='restoration_hold'").fetchone()[0]=='true'
