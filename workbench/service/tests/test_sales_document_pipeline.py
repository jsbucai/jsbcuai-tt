from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid

import pytest

from core import sales_document_pipeline as pipeline_module
from core.sales_assistant import ensure_schema
from core.sales_document_extract import DocumentExtraction, DocumentLine, DocumentPage
from core.sales_document_pipeline import (SalesDocumentPipeline, DocumentPipelineError,
    document_projection, enqueue_document)
from core.sales_document_pipeline import enqueue_session_documents
from core.store import dumps
from test_daily_work import store
from test_sales_document_extract import _pdf, _fake_tesseract


@pytest.fixture
def pipeline(store):
    ensure_schema(store)
    with store.connect(write=True) as db:
        db.execute('''INSERT INTO sales_assistant_sessions
            (id,profile_id,customer_name,created_by,version,status,draft,created_at,updated_at)
            VALUES ('session-1','P','客户甲','sales',1,'collecting','{}','','')''')
        db.execute('''INSERT INTO sales_assistant_sessions
            (id,profile_id,customer_name,created_by,version,status,draft,created_at,updated_at)
            VALUES ('session-2','P','客户乙','sales',1,'collecting','{}','','')''')
    return SalesDocumentPipeline(store)


def attach(pipeline, raw, mime='application/pdf', *, session='session-1'):
    identity=uuid.uuid4().hex
    blob_name=uuid.uuid4().hex+'.bin'
    blobs=pipeline.store.runtime/'sales-assistant'/'blobs'
    blobs.mkdir(parents=True,exist_ok=True)
    (blobs/blob_name).write_bytes(raw)
    digest=sha256(raw).hexdigest()
    with pipeline.store.connect(write=True) as db:
        db.execute('INSERT INTO sales_assistant_attachments VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (identity,session,'订单.pdf',mime,digest,len(raw),blob_name,'queued','sales',''))
        job=enqueue_document(db,identity)
    return identity,blob_name,digest,job


def extraction(identity,digest,text='LS1000,3,1.13', *, unready=()):
    return DocumentExtraction('no_text' if unready else 'needs_review','application/pdf',
        digest,identity,2 if unready else 1,
        (DocumentPage(1,'needs_review','pdf_text_layer',1),),
        (DocumentLine(1,1,text,'pdf_text_layer',digest,identity),),
        tuple(unready),'pdf_pages_without_text' if unready else '')


def test_upload_enqueue_atomic_idempotent_and_does_not_read_or_extract(pipeline,monkeypatch):
    monkeypatch.setattr(pipeline_module,'_bounded_extract',lambda *args:pytest.fail('upload extracted'))
    identity,blob,digest,job=attach(pipeline,b'%PDF-1.7\npending')
    assert job['status']=='queued' and not job['candidate_count']
    with pipeline.store.connect(write=True) as db:
        again=enqueue_document(db,identity)
        assert again['id']==job['id']
        db.execute('UPDATE sales_assistant_attachments SET sha256=? WHERE id=?',('0'*64,identity))
        with pytest.raises(DocumentPipelineError,match='原附件身份已改变'):
            enqueue_document(db,identity)
    with pipeline.store.connect() as db:
        assert pipeline.result(db,'session-1',identity)['projection'] is None
        assert db.execute('SELECT version FROM sales_assistant_sessions WHERE id=?',
                          ('session-1',)).fetchone()['version']==1


def test_rollback_upload_also_rolls_back_queue(pipeline):
    with pytest.raises(RuntimeError):
        with pipeline.store.connect(write=True) as db:
            identity=uuid.uuid4().hex
            db.execute('INSERT INTO sales_assistant_attachments VALUES (?,?,?,?,?,?,?,?,?,?)',
                (identity,'session-1','订单.pdf','application/pdf','0'*64,10,
                 uuid.uuid4().hex+'.bin','queued','sales',''))
            enqueue_document(db,identity)
            raise RuntimeError('upload rollback')
    with pipeline.store.connect() as db:
        assert pipeline.status(db,'session-1')==[]


def test_legacy_documents_can_be_metadata_backfilled_per_session_without_reprocessing(pipeline):
    identity,blob,digest,job=attach(pipeline,b'%PDF-1.7\nlegacy')
    with pipeline.store.connect(write=True) as db:
        db.execute('DELETE FROM sales_document_jobs WHERE attachment_id=?',(identity,))
        first=enqueue_session_documents(db,'session-1')
        again=enqueue_session_documents(db,'session-1')
        assert len(first)==1 and first[0]['id']==again[0]['id']
        assert first[0]['status']=='queued'
        assert enqueue_session_documents(db,'session-2')==[]


def test_one_heavy_job_at_a_time_restart_reclaims_and_stale_worker_cannot_finish(pipeline):
    identity,blob,digest,job=attach(pipeline,b'%PDF-1.7\npending')
    attach(pipeline,b'%PDF-1.7\nother')
    claim=pipeline.claim('a'*32,now=100)
    assert claim['attempts']==1 and claim['id']==job['id']
    assert pipeline.claim('b'*32,now=150) is None
    reclaimed=pipeline.claim('b'*32,now=191)
    assert reclaimed['id']==job['id'] and reclaimed['attempts']==2
    with pytest.raises(DocumentPipelineError) as error:
        pipeline.finish(job['id'],'a'*32,extraction(identity,digest),now=192)
    assert error.value.code=='document_worker_lease_lost'
    ready=pipeline.finish(job['id'],'b'*32,extraction(identity,digest),now=192)
    assert ready['status']=='ready' and ready['candidate_count']==1
    assert pipeline.claim('c'*32,now=193)['id']!=job['id']


def test_source_rows_stable_across_attempts_same_sku_stays_distinct_and_unknown_kept():
    identity='a'*32;digest='b'*64
    value=extraction(identity,digest)
    value=replace(value,lines=(value.lines[0],
        DocumentLine(1,2,'LS1000,2,1.13','pdf_text_layer',digest,identity),
        DocumentLine(2,1,'客户采购单','pdf_text_layer',digest,identity)))
    first=document_projection(value);second=document_projection(value)
    assert first==second
    assert [line['quantity'] for line in first['lines']]==['3','2','']
    assert len({line['source_ref']['id'] for line in first['lines']})==3
    assert first['lines'][0]['source_ref']['match_basis']=='explicit_code'
    assert first['lines'][2]['source_ref']['match_basis']=='manual_code_required'
    assert first['source_rows'][2]['original_text']=='客户采购单'
    assert first['source_rows'][2]['page']==2
    assert all(row['blocking'] and row['exclude_allowed'] for row in first['source_rows'])


def test_missing_page_coverage_not_excludable_and_limit_never_partially_projects():
    value=extraction('a'*32,'b'*64,unready=(2,))
    projection=document_projection(value)
    blocker=projection['source_rows'][-1]
    assert blocker['scope']=='page_coverage' and blocker['page']==2 and blocker['row']==0
    assert blocker['blocking'] and blocker['exclude_allowed'] is False
    assert len(projection['lines'])==1
    overflow=replace(value,lines=tuple(DocumentLine(1,index,'LS1000,1,1.13',
        'pdf_text_layer','b'*64,'a'*32) for index in range(1,502)))
    limited=document_projection(overflow)
    assert limited['lines']==[] and len(limited['source_rows'])==1
    assert limited['source_rows'][0]['reason']=='document_candidate_limit'
    assert not limited['source_rows'][0]['exclude_allowed']


def test_result_access_scoped_and_status_does_not_fetch_large_text(pipeline):
    identity,blob,digest,job=attach(pipeline,b'%PDF-1.7\npending')
    pipeline.claim('a'*32,now=100)
    pipeline.finish(job['id'],'a'*32,extraction(identity,digest),now=101)
    with pipeline.store.connect() as db:
        queries=[]
        db.set_trace_callback(queries.append)
        status=pipeline.status(db,'session-1')
        assert status[0]['status']=='ready' and status[0]['line_count']==1
        assert 'SELECT *' not in queries[0] and ',result,' not in queries[0]
        assert pipeline.status(db,'session-2',identity)==[]
        with pytest.raises(KeyError): pipeline.result(db,'session-2',identity)
        result=pipeline.result(db,'session-1',identity)
        assert result['projection']['lines'][0]['product_code']=='LS1000'
        assert result['result_hash']==sha256(dumps({'extraction':result['extraction'],
            'projection':result['projection']}).encode()).hexdigest()


def test_adoption_same_transaction_version_checked_and_never_adds_rows_twice(pipeline):
    identity,blob,digest,job=attach(pipeline,b'%PDF-1.7\npending')
    pipeline.claim('a'*32,now=100)
    ready=pipeline.finish(job['id'],'a'*32,extraction(identity,digest),now=101)
    with pipeline.store.connect(write=True) as db:
        with pytest.raises(DocumentPipelineError) as error:
            pipeline.mark_adopted(db,'session-1',identity,ready['result_hash'],2)
        assert error.value.code=='document_adoption_version_mismatch'
        with pytest.raises(DocumentPipelineError):
            pipeline.mark_adopted(db,'session-1',identity,'wrong',1)
        db.execute("UPDATE sales_assistant_sessions SET version=2 WHERE id='session-1'")
        adopted=pipeline.mark_adopted(db,'session-1',identity,ready['result_hash'],2)
        assert adopted['status']=='adopted' and adopted['adopted_version']==2
        again=pipeline.mark_adopted(db,'session-1',identity,ready['result_hash'],3)
        assert again['adopted_version']==2
    with pipeline.store.connect() as db:
        assert json.loads(db.execute('SELECT draft FROM sales_assistant_sessions WHERE id=?',
                                    ('session-1',)).fetchone()['draft'])=={}


def test_extraction_no_sqlite_write_lock_and_integrity_failure_preserves_blocker(pipeline,monkeypatch):
    identity,blob,digest,job=attach(pipeline,b'%PDF-1.7\npending')
    def check_free_writer(raw,mime,source,config):
        other=sqlite3.connect(pipeline.store.path,timeout=0)
        other.execute('BEGIN IMMEDIATE');other.rollback();other.close()
        return extraction(source,sha256(raw).hexdigest())
    monkeypatch.setattr(pipeline_module,'_bounded_extract',check_free_writer)
    result=pipeline.run_one('a'*32)
    assert result['status']=='ready'
    bad_id,bad_blob,bad_digest,bad_job=attach(pipeline,b'%PDF-1.7\nsecond')
    (pipeline.store.runtime/'sales-assistant'/'blobs'/bad_blob).write_bytes(b'changed')
    failed=pipeline.run_one('b'*32)
    assert failed['status']=='failed' and failed['error_code']=='document_blob_integrity_failed'
    with pipeline.store.connect() as db:
        blocker=pipeline.result(db,'session-1',bad_id)['projection']['source_rows'][0]
        assert blocker['blocking'] and blocker['exclude_allowed'] is False


def test_real_pdf_child_keeps_both_pages_without_mutating_session(pipeline):
    raw=_pdf(b'BT /F1 12 Tf 10 180 Td (LS1000,1,1.13) Tj ET',
             b'BT /F1 12 Tf 10 180 Td (LS1000,2,1.13) Tj ET')
    identity,blob,digest,job=attach(pipeline,raw)
    result=pipeline.run_one('a'*32)
    assert result['status']=='ready' and result['page_count']==2 and result['candidate_count']==2
    with pipeline.store.connect() as db:
        projected=pipeline.result(db,'session-1',identity)['projection']
        assert [row['page'] for row in projected['source_rows']]==[1,2]
        assert [line['quantity'] for line in projected['lines']]==['1','2']
        assert db.execute("SELECT version FROM sales_assistant_sessions WHERE id='session-1'").fetchone()[0]==1


def test_real_pdf_blank_page_and_unconfigured_image_never_succeed(pipeline):
    identity,blob,digest,job=attach(pipeline,_pdf(
        b'BT /F1 12 Tf 10 180 Td (LS1000,1,1.13) Tj ET',None))
    result=pipeline.run_one('a'*32)
    assert result['status']=='failed' and result['unready_pages']==[2]
    identity,blob,digest,job=attach(pipeline,b'\x89PNG\r\n\x1a\n' + b'fake-image','image/png')
    image=pipeline.run_one('b'*32)
    assert image['status']=='failed' and image['error_code']=='ocr_not_configured'
    assert image['unready_pages']==[1]


def test_bound_child_timeout_kills_process_group_and_does_not_return_partial_text(monkeypatch):
    class FakeProcess:
        pid=12345;returncode=None
        calls=0
        def communicate(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1: raise subprocess.TimeoutExpired('extract',30)
            return b'{"partial":true}',b''
    process=FakeProcess();killed=[]
    def popen(args,**kwargs):
        assert isinstance(args,list) and kwargs['start_new_session'] and 'shell' not in kwargs
        return process
    monkeypatch.setattr(pipeline_module.subprocess,'Popen',popen)
    monkeypatch.setattr(pipeline_module.os,'killpg',lambda pid,sig:killed.append((pid,sig)))
    with pytest.raises(DocumentPipelineError) as error:
        pipeline_module._bounded_extract(b'%PDF-1.7','application/pdf','source-1')
    assert error.value.code=='document_extract_timeout' and len(killed)==1
