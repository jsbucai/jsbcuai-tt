"""Workbook caching must not weaken publication validation or reply isolation."""
import json
import os

import pytest

from core.purchase_unfinished import PurchaseSnapshotError
from test_purchase_unfinished import publish, SNAPSHOT_ID
from test_staff_auth import public_app


def engine_for(public_app,tmp_path):
    engine=public_app.extensions['service']['purchase_unfinished']
    engine.root=tmp_path
    return engine,{'id':'admin','is_admin':True}


def test_all_reads_parse_once_but_supplier_replies_remain_live(public_app,tmp_path,monkeypatch):
    publish(tmp_path);engine,actor=engine_for(public_app,tmp_path)
    import core.purchase_unfinished as module
    calls=[];original=module._parse_xlsx
    def counted(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'_parse_xlsx',counted)
    rows=engine.list(actor);line=rows['items'][0]
    engine.status(actor);engine.suppliers(actor);engine.reminder_preview(actor,'S01')
    engine.promise(actor,line['line_key'],{'snapshot_id':rows['snapshot_id'],'version':0,
        'idempotency_key':'reply','promised_date':'2026-10-12','note':'供应商回复'})
    current=engine.list(actor)['items'][0]
    assert len(calls)==1 and current['promise_note']=='供应商回复' and current['promise_version']==1
    current['product_name']='不得污染缓存'
    assert engine.list(actor)['items'][0]['product_name']!='不得污染缓存'


def test_same_size_and_restored_mtime_tampering_never_reuses_valid_parse(public_app,tmp_path):
    path=publish(tmp_path);engine,actor=engine_for(public_app,tmp_path)
    engine.status(actor);saved=engine._parsed_snapshot
    stamp=path.stat();raw=bytearray(path.read_bytes());raw[10]^=1
    path.write_bytes(raw);os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert engine.status(actor)['status']=='invalid'
    with pytest.raises(PurchaseSnapshotError) as conflict:engine.list(actor)
    assert conflict.value.code=='snapshot_hash_or_size_mismatch'
    assert engine._parsed_snapshot is saved


def test_changed_manifest_requires_full_parse_and_failure_preserves_last_cache(public_app,tmp_path):
    publish(tmp_path);engine,actor=engine_for(public_app,tmp_path)
    engine.status(actor);saved=engine._parsed_snapshot
    manifest_path=tmp_path/'snapshots'/SNAPSHOT_ID/'manifest.json'
    manifest=json.loads(manifest_path.read_text());manifest['row_count']=1
    manifest_path.write_text(json.dumps(manifest))
    assert engine.status(actor)['status']=='invalid' and engine._parsed_snapshot is saved
    next_id='20261006T010000Z-0123456789ab-01234567'
    publish(tmp_path,snapshot_id=next_id)
    assert engine.status(actor)['snapshot_id']==next_id
    assert engine._parsed_snapshot is not saved


def test_warm_cache_rejects_symlink_and_file_change_during_parse(public_app,tmp_path,monkeypatch):
    path=publish(tmp_path);engine,actor=engine_for(public_app,tmp_path)
    engine.status(actor);saved=engine._parsed_snapshot
    target=tmp_path/'copy.xlsx';target.write_bytes(path.read_bytes())
    path.unlink();path.symlink_to(target)
    assert engine.status(actor)['status']=='invalid' and engine._parsed_snapshot is saved
    path.unlink();path.write_bytes(target.read_bytes())
    import core.purchase_unfinished as module
    original=module._parse_xlsx
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        path.write_bytes(path.read_bytes()+b'incomplete')
        return result
    monkeypatch.setattr(module,'_parse_xlsx',changed)
    with pytest.raises(PurchaseSnapshotError) as conflict:engine.list(actor)
    assert conflict.value.code=='snapshot_file_changed' and engine._parsed_snapshot is saved
