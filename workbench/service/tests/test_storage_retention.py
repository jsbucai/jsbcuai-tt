import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import uuid
import pytest
from test_api_migration import store
from core.update_jobs import UpdateJobs
from core.storage_retention import archive_calculations
from core.maintenance_archive import write_archive, verify_archive
from core.storage_capacity import capacity

NOW=datetime(2026,10,12,3,30)


def seed(store):
    UpdateJobs(store,None)
    with store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version','\"v-current\"')")
        for i in range(16):
            identity=f'j{i}'
            version='v-current' if i==2 else f'v{i}'
            stamp=(NOW-timedelta(days=3)+timedelta(minutes=i)).isoformat()
            db.execute('INSERT INTO logistics_calculations VALUES (?,?,?,?,?)',
                       (identity,'P',version,json.dumps({'plans':['x'*1000],'scope_groups':{'a':[['p','w','s']]}}),stamp))
            db.execute('INSERT INTO update_jobs VALUES (?,?,?,?,?,?,?)',
                       (identity,'sync','succeeded','complete','{"policy":"keep","changes":[{"tradeid":"O1"}]}',stamp,stamp))
            db.execute('INSERT INTO sync_batches VALUES (?,?,?,?,?,?,?,?,?)',
                       (identity,'P','published',stamp,version,'{}','{}',stamp,stamp))
            db.execute('INSERT INTO sync_issues VALUES (?,?,?)',(identity,'O1','{"reason":"old"}'))
        db.execute("UPDATE update_jobs SET status='unknown' WHERE id='j1'")
        db.execute("INSERT INTO freight_attempts VALUES ('a','j3','P','O1','{}','unknown','x','x')")
        db.execute("INSERT INTO freight_attempt_scopes VALUES ('a','{\"job_id\":\"j4\",\"group_id\":\"a\"}')")
        db.execute("INSERT INTO update_projection_retries VALUES ('j5',1,'x')")
        db.execute("CREATE TABLE auth_sessions(token_hash TEXT,expires REAL)")
        db.executemany('INSERT INTO auth_sessions VALUES (?,?)',[('expired',0),('valid',NOW.timestamp()+86400)])


def test_retention_protects_current_recent_unknown_and_exact_scope(store,tmp_path):
    seed(store)
    with store.connect() as db:
        jobs=[tuple(r) for r in db.execute('SELECT * FROM update_jobs')]
        attempts=[tuple(r) for r in db.execute('SELECT * FROM freight_attempts')]
    report=archive_calculations(str(store.path),tmp_path,b'k'*32,NOW)
    assert report['archived_jobs']==5  # j0,j6,j7,j8,j9
    with store.connect() as db:
        archived={r[0] for r in db.execute("SELECT job_id FROM logistics_calculations WHERE json_extract(payload,'$.archived')=1")}
        assert archived=={'j0','j6','j7','j8','j9'}
        assert jobs==[tuple(r) for r in db.execute('SELECT * FROM update_jobs')]
        assert attempts==[tuple(r) for r in db.execute('SELECT * FROM freight_attempts')]
        assert db.execute('SELECT count(*) FROM sync_issues').fetchone()[0]==11
        assert [r[0] for r in db.execute('SELECT token_hash FROM auth_sessions')]==['valid']
    assert archive_calculations(str(store.path),tmp_path,b'k'*32,NOW)['archived_jobs']==0
    for p in tmp_path.glob('*.ulcold'): assert verify_archive(p,b'k'*32)['bytes']>1000


def test_archive_failure_never_prunes_database(store,tmp_path,monkeypatch):
    seed(store)
    def fail(*a,**kw): raise OSError('NAS failed')
    monkeypatch.setattr('core.storage_retention.write_archive',fail)
    with pytest.raises(OSError): archive_calculations(str(store.path),tmp_path,b'k'*32,NOW)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM logistics_calculations WHERE json_extract(payload,'$.archived')=1").fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM sync_issues').fetchone()[0]==16


def test_active_source_start_during_archive_prevents_pruning(store,tmp_path,monkeypatch):
    seed(store)
    original=write_archive
    def race(*a,**kw):
        result=original(*a,**kw)
        with store.connect(write=True) as db: db.execute("UPDATE update_jobs SET status='running' WHERE id='j15'")
        return result
    monkeypatch.setattr('core.storage_retention.write_archive',race)
    result=archive_calculations(str(store.path),tmp_path,b'k'*32,NOW)
    assert result['skipped_busy'] and result['archived_jobs']==0
    assert not list(tmp_path.glob('*.ulcold'))


def test_identical_versions_do_not_retain_every_historical_copy(store,tmp_path):
    seed(store)
    with store.connect(write=True) as db:
        db.execute("UPDATE logistics_calculations SET data_version='v-current'")
    result=archive_calculations(str(store.path),tmp_path,b'k'*32,NOW)
    assert result['archived_jobs']==6  # only newest current plus other explicit pins


def test_authenticated_archive_detects_tampering_and_does_not_overwrite(tmp_path):
    path=tmp_path/'a.ulcold';key=b'k'*32
    receipt=write_archive(path,[b'abc'*1000000],key)
    assert verify_archive(path,key)['sha256']==receipt['sha256']
    with pytest.raises(ValueError): write_archive(path,[b'other'],key)
    assert verify_archive(path,key)['bytes']==3000000
    raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
    with pytest.raises(Exception): verify_archive(path,key)


def test_capacity_gate_keeps_automatic_job_and_frozen_request(store,monkeypatch):
    source=SimpleNamespace(collect=lambda:pytest.fail('must not fetch'))
    jobs=UpdateJobs(store,source)
    job=jobs.enqueue_automatic('refresh')
    monkeypatch.setattr('core.storage_capacity.capacity',lambda p:{'status':'critical','free_bytes':0})
    assert not jobs.process_one()
    current=jobs.get(job['id'])
    assert current['status']=='queued' and current['stage']=='waiting_capacity'
    assert 'next_retry_at' in current['result'] and current['result']['automatic']


def test_capacity_reserves_database_backup_space(tmp_path,monkeypatch):
    (tmp_path/'workbench.sqlite3').write_bytes(b'x'*10)
    monkeypatch.setattr('core.storage_capacity.shutil.disk_usage',lambda p:SimpleNamespace(total=10*1024**3,free=500*1024**2))
    assert capacity(tmp_path)['status']=='critical'


def maintenance_module():
    import importlib.util
    path=Path(__file__).resolve().parents[1]/'tools/storage_maintenance.py'
    spec=importlib.util.spec_from_file_location('storage_maintenance_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_backup_rotation_keeps_recovery_points_per_day_week_month(tmp_path):
    mod=maintenance_module();files=[]
    for day in range(400):
        for copy in range(3):
            when=NOW-timedelta(days=day,minutes=copy)
            p=tmp_path/f'ul-{when:%Y%m%dT%H%M%SZ}-{day*3+copy}.ulbackup';p.write_bytes(b'x')
            os.utime(p,(when.timestamp(),when.timestamp()));files.append(p)
    keep=mod.backup_survivors(files,NOW)
    assert files[0] in keep and 30<=len(keep)<=55
    assert not any((NOW.timestamp()-p.stat().st_mtime)>367*86400 for p in keep)


def test_old_database_archives_keep_two_newest_and_nonempty_wal(tmp_path):
    mod=maintenance_module();runtime=tmp_path/'runtime';folder=runtime/'migration-backups';folder.mkdir(parents=True)
    destination=tmp_path/'cold';destination.mkdir()
    for i in range(5):
        p=folder/f'before-v{i}.sqlite3'
        with sqlite3.connect(p) as c:c.execute('CREATE TABLE facts(id INTEGER)')
        os.utime(p,((NOW-timedelta(days=10-i)).timestamp(),)*2)
    (folder/'before-v1.sqlite3-wal').write_bytes(b'preserve')
    result=mod.move_old_database_copies(runtime,destination,b'k'*32,NOW)
    assert len(result)==2
    assert {p.name for p in folder.glob('*.sqlite3')}=={'before-v1.sqlite3','before-v3.sqlite3','before-v4.sqlite3'}
    for p in destination.glob('*.ulcold'):assert verify_archive(p,b'k'*32)['bytes']>0


def test_missing_nas_fails_before_mutation(tmp_path,monkeypatch):
    mod=maintenance_module();monkeypatch.setattr(mod.os.path,'ismount',lambda p:False)
    with pytest.raises(ValueError,match='NAS'):mod.run(tmp_path,tmp_path/'not-mounted',True)


def test_cold_archive_restores_exact_bytes_and_refuses_overwrite(tmp_path):
    from core.maintenance_archive import restore_archive
    raw=b'original protected data'*50000
    source=tmp_path/'test.ulcold';target=tmp_path/'restored.sqlite3'
    write_archive(source,[raw],b'k'*32)
    restore_archive(source,target,b'k'*32)
    assert target.read_bytes()==raw
    with pytest.raises(FileExistsError): restore_archive(source,target,b'k'*32)
    assert target.read_bytes()==raw


def test_host_worker_lane_defers_api_without_claim_or_duplicate_job(store):
    import fcntl
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:pytest.fail('lane was busy')))
    job=jobs.enqueue(str(uuid.uuid4()),'refresh')
    with (store.runtime/'.web-browser-lane.lock').open('a+') as host:
        fcntl.flock(host,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert jobs.process_one() is False
        assert jobs.retry_projection() is False
    assert jobs.get(job['id'])['status']=='queued'
