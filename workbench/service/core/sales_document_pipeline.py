"""Durable, read-only extraction queue; adoption is a separate sales action.

Uploading never executes an extractor. The single document worker releases its
SQLite transaction before reading a blob or spawning bounded extraction. An
extraction result is evidence to review, never an automatically confirmed SKU.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from .sales_document_extract import (DocumentExtraction, DocumentLine,
    DocumentPage, TesseractConfig, extract_document, IMAGE_TYPES)
from .sales_source_parser import parse_text
from .store import dumps


DOCUMENT_TYPES = IMAGE_TYPES | {'application/pdf'}
MAX_PROJECTION_LINES = 500
WORKER_WALL_SECONDS = 30
LEASE_SECONDS = 90
MAX_RESULT_BYTES = 3 * 1024 * 1024
STATUS_COLUMNS = ('id,attachment_id,session_id,status,error_code,candidate_count,'
    'source_row_count,line_count,page_count,attempts,created_at,updated_at,'
    'adopted_version,result_hash,unready_pages')


class DocumentPipelineError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def ensure_document_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sales_document_jobs(
        id TEXT PRIMARY KEY, attachment_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL, mime_type TEXT NOT NULL, sha256 TEXT NOT NULL,
        size INTEGER NOT NULL, blob_name TEXT NOT NULL, created_by TEXT NOT NULL,
        status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
        result_hash TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
        candidate_count INTEGER NOT NULL DEFAULT 0, source_row_count INTEGER NOT NULL DEFAULT 0,
        line_count INTEGER NOT NULL DEFAULT 0,
        page_count INTEGER NOT NULL DEFAULT 0, unready_pages TEXT NOT NULL DEFAULT '[]',
        worker_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        adopted_version INTEGER,
        FOREIGN KEY(attachment_id) REFERENCES sales_assistant_attachments(id),
        FOREIGN KEY(session_id) REFERENCES sales_assistant_sessions(id))''')
    db.execute('''CREATE INDEX IF NOT EXISTS sales_document_queue
        ON sales_document_jobs(status,created_at,id)''')
    db.execute('''CREATE INDEX IF NOT EXISTS sales_document_session
        ON sales_document_jobs(session_id,created_at,id)''')
    if 'line_count' not in {row['name'] for row in db.execute('PRAGMA table_info(sales_document_jobs)')}:
        db.execute('ALTER TABLE sales_document_jobs ADD COLUMN line_count INTEGER NOT NULL DEFAULT 0')


def _status(row):
    value = {key: row[key] for key in ('id', 'attachment_id', 'session_id',
        'status', 'error_code', 'candidate_count', 'source_row_count', 'line_count', 'page_count',
        'attempts', 'created_at', 'updated_at', 'adopted_version', 'result_hash')}
    value['unready_pages'] = json.loads(row['unready_pages'])
    value['requires_human_review'] = True
    value['status'] = ('adopted' if row['adopted_version'] is not None else
                       'ready' if row['status']=='ready_for_review' else
                       'failed' if row['status']=='not_ready' else row['status'])
    value['message'] = {'queued':'等待文档提取','running':'正在提取文档，尚未改动草稿',
        'ready':'已提取候选文字，请核对后采用','failed':'文档提取覆盖未通过，原件已保留',
        'adopted':'候选已加入草稿，仍须逐行核对'}[value['status']]
    return value


def enqueue_document(db, attachment_id: str) -> dict:
    """Call after attachment INSERT in the same upload transaction."""
    row = db.execute('SELECT * FROM sales_assistant_attachments WHERE id=?',
                     (attachment_id,)).fetchone()
    if row is None or row['mime_type'] not in DOCUMENT_TYPES:
        raise DocumentPipelineError('document_attachment_invalid', '附件不是可提取的图片或 PDF')
    if (not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) or
            not re.fullmatch(r'[0-9a-f]{32}\.bin', row['blob_name']) or
            not 0 < row['size'] <= 10 * 1024 * 1024):
        raise DocumentPipelineError('document_attachment_invalid', '附件身份无效')
    existing = db.execute('SELECT * FROM sales_document_jobs WHERE attachment_id=?',
                          (attachment_id,)).fetchone()
    if existing:
        if any(existing[key] != row[key] for key in
               ('session_id', 'mime_type', 'sha256', 'size', 'blob_name', 'created_by')):
            raise DocumentPipelineError('document_attachment_changed', '原附件身份已改变')
        return _status(existing)
    now = time.time()
    identity = uuid.uuid4().hex
    db.execute('''INSERT INTO sales_document_jobs
        (id,attachment_id,session_id,mime_type,sha256,size,blob_name,created_by,
         status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'queued',?,?)''',
        (identity,attachment_id,row['session_id'],row['mime_type'],row['sha256'],
         row['size'],row['blob_name'],row['created_by'],now,now))
    return _status(db.execute('SELECT * FROM sales_document_jobs WHERE id=?',
                             (identity,)).fetchone())


def enqueue_session_documents(db, session_id: str) -> list[dict]:
    """Backfill old attachments during an already-authorized write transaction.

    This only queues metadata. Caller must also restore a persistent coverage
    row in old drafts, and must not call this from a read-only detail request.
    """
    rows = db.execute('''SELECT id FROM sales_assistant_attachments
        WHERE session_id=? AND mime_type IN (?,?,?,?) ORDER BY rowid''',
        (session_id,'application/pdf','image/png','image/jpeg','image/webp')).fetchall()
    if len(rows)>20:
        raise DocumentPipelineError('document_attachment_limit','旧会话附件超限，请核对')
    return [enqueue_document(db,row['id']) for row in rows]


def document_projection(extraction: DocumentExtraction) -> dict:
    """Map exact extracted physical lines to editable candidates, no SKU lookup.

    A missing text page gets its own non-excludable coverage row. Every actual
    line stays linked to page, line, full original text and extraction method.
    """
    source_id, digest = extraction.source_id, extraction.source_sha256
    lines, reports = [], []
    if len(extraction.lines) <= MAX_PROJECTION_LINES:
        for entry in extraction.lines:
            identity = f'document:{source_id}:{digest[:16]}:{entry.page_number}:{entry.line_number}'
            parsed, _ = parse_text(entry.text, identity, digest)
            item = parsed[0] if parsed else {'product_code':'','quantity':'','unit_price':''}
            explicit = item.get('source_ref',{}).get('match_basis') == 'explicit_code'
            report = {'id':identity,'kind':'document','scope':'extracted_line',
                'source_id':source_id,'sha256':digest,'sheet':f'第{entry.page_number}页',
                'sheet_index':entry.page_number,'page':entry.page_number,
                'row':entry.line_number,'line':entry.line_number,'method':entry.method,'original_text':entry.text,
                'preview':entry.text[:240],'status':'needs_review','blocking':True,
                'reason':'document_line_requires_review','line_id':'', 'exclude_allowed':True}
            item['source_ref'] = {key:report[key] for key in
                ('id','kind','source_id','sha256','sheet','page','row','line','method','preview','original_text')}
            item['source_ref'].update(original_name='',original_spec='',unit_blank=True,
                match_basis='explicit_code' if explicit else 'manual_code_required')
            lines.append(item)
            reports.append(report)
    unavailable = list(extraction.unready_pages)
    if len(extraction.lines) > MAX_PROJECTION_LINES:
        unavailable = [0]
    elif extraction.status != 'needs_review' and not unavailable:
        unavailable = [0]
    for page in unavailable:
        reports.append({'id':f'document:{source_id}:{digest[:16]}:{page}:0',
            'kind':'document','scope':'page_coverage','source_id':source_id,'sha256':digest,
            'sheet':f'第{page}页' if page else '全文覆盖','sheet_index':page,'page':page,'row':0,'line':0,
            'preview':'该页未获得可核对文字' if page else '附件提取覆盖未通过',
            'status':'needs_review','reason': 'document_candidate_limit'
                if len(extraction.lines)>MAX_PROJECTION_LINES else
                extraction.error_code or 'document_page_without_text',
            'blocking':True,'line_id':'','exclude_allowed':False})
    return {'lines':lines,'source_rows':reports,'requires_human_review':True}


def _extraction_from_json(value):
    return DocumentExtraction(value['status'],value['media_type'],value['source_sha256'],
        value['source_id'],value['page_count'],
        tuple(DocumentPage(**page) for page in value['pages']),
        tuple(DocumentLine(**line) for line in value['lines']),
        tuple(value['unready_pages']),value.get('error_code',''),True)


def _failure(row, code):
    return DocumentExtraction('invalid',row['mime_type'],row['sha256'],row['attachment_id'],
                              0,(),(),(0,),code)


def _bounded_extract(raw, mime_type, source_id, tesseract=None):
    config = None if tesseract is None else {'command':str(tesseract.command),
        'languages':list(tesseract.languages),'timeout_seconds':tesseract.timeout_seconds}
    arguments = [sys.executable,'-m','core.sales_document_pipeline','--extract-child',
                 mime_type,source_id,dumps(config)]
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH','')
    process = subprocess.Popen(arguments,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,env=env,start_new_session=True)
    try:
        stdout, _stderr = process.communicate(raw,timeout=WORKER_WALL_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise DocumentPipelineError('document_extract_timeout','文档提取超时') from None
    if process.returncode or len(stdout)>MAX_RESULT_BYTES:
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise DocumentPipelineError('document_extract_failed','文档提取未得到完整结果')
    try:
        result = _extraction_from_json(json.loads(stdout))
    except (ValueError,KeyError,TypeError):
        raise DocumentPipelineError('document_extract_invalid_result','文档提取结果无效') from None
    if (result.source_id != source_id or result.source_sha256 != sha256(raw).hexdigest()
            or result.media_type != mime_type):
        raise DocumentPipelineError('document_extract_identity_mismatch','文档提取身份不一致')
    return result


class SalesDocumentPipeline:
    def __init__(self, store):
        self.store = store
        with store.connect(write=True) as db:
            ensure_document_schema(db)

    @staticmethod
    def status(db, session_id, attachment_id=None):
        query = f'SELECT {STATUS_COLUMNS} FROM sales_document_jobs WHERE session_id=?'
        params = [session_id]
        if attachment_id is not None:
            query += ' AND attachment_id=?'
            params.append(attachment_id)
        query += ' ORDER BY created_at,id'
        return [_status(row) for row in db.execute(query,params)]

    @staticmethod
    def result(db, session_id, attachment_id):
        row = db.execute('SELECT * FROM sales_document_jobs WHERE session_id=? AND attachment_id=?',
                         (session_id,attachment_id)).fetchone()
        if row is None:
            raise KeyError('附件提取任务不存在')
        value = _status(row)
        if row['status'] in {'queued','running'}:
            value.update(extraction=None,projection=None)
        else:
            value.update(json.loads(row['result']))
        return value

    def claim(self, worker_token, now=None):
        if not isinstance(worker_token,str) or not 16 <= len(worker_token) <= 128:
            raise ValueError('文档 worker token 无效')
        now = time.time() if now is None else now
        with self.store.connect(write=True) as db:
            # Only one heavy extraction is active for this database. A stale
            # token cannot finish a lease reclaimed after process interruption.
            if db.execute("SELECT 1 FROM sales_document_jobs WHERE status='running' AND lease_until>?",
                          (now,)).fetchone():
                return None
            row = db.execute("""SELECT * FROM sales_document_jobs
                WHERE status='queued' OR (status='running' AND lease_until<=?)
                ORDER BY created_at,id LIMIT 1""",(now,)).fetchone()
            if row is None:
                return None
            db.execute("""UPDATE sales_document_jobs SET status='running',worker_token=?,
                lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?""",
                (worker_token,now+LEASE_SECONDS,now,row['id']))
            return dict(db.execute('SELECT * FROM sales_document_jobs WHERE id=?',(row['id'],)).fetchone())

    def finish(self, job_id, worker_token, extraction, now=None):
        now = time.time() if now is None else now
        with self.store.connect(write=True) as db:
            row = db.execute('SELECT * FROM sales_document_jobs WHERE id=?',(job_id,)).fetchone()
            if (row is None or row['status']!='running' or row['worker_token']!=worker_token
                    or row['lease_until'] < now):
                raise DocumentPipelineError('document_worker_lease_lost','提取任务租约已失效')
            if (extraction.source_id!=row['attachment_id'] or extraction.source_sha256!=row['sha256']
                    or extraction.media_type!=row['mime_type']):
                raise DocumentPipelineError('document_extract_identity_mismatch','文档提取身份不一致')
            projection = document_projection(extraction)
            payload = {'extraction':asdict(extraction),'projection':projection}
            encoded = dumps(payload)
            if len(encoded.encode())>MAX_RESULT_BYTES:
                extraction = _failure(row,'document_result_limit')
                projection = document_projection(extraction)
                payload = {'extraction':asdict(extraction),'projection':projection}
                encoded = dumps(payload)
            status = 'ready_for_review' if extraction.status=='needs_review' and not any(
                report['scope']=='page_coverage' for report in projection['source_rows']) else 'not_ready'
            result_hash = sha256(encoded.encode()).hexdigest()
            db.execute('''UPDATE sales_document_jobs SET status=?,result=?,result_hash=?,
                error_code=?,candidate_count=?,source_row_count=?,line_count=?,page_count=?,unready_pages=?,
                worker_token='',lease_until=0,updated_at=? WHERE id=?''',
                (status,encoded,result_hash,extraction.error_code or ('document_candidate_limit'
                    if len(extraction.lines)>MAX_PROJECTION_LINES else ''),len(projection['lines']),
                 len(projection['source_rows']),len(extraction.lines),extraction.page_count,
                 dumps(extraction.unready_pages),now,job_id))
            return _status(db.execute('SELECT * FROM sales_document_jobs WHERE id=?',(job_id,)).fetchone())

    def run_one(self, worker_token, tesseract=None):
        row = self.claim(worker_token)
        if row is None:
            return None
        try:
            blob_root = self.store.runtime/'sales-assistant'/'blobs'
            path = blob_root/row['blob_name']
            if (not re.fullmatch(r'[0-9a-f]{32}\.bin',row['blob_name']) or blob_root.is_symlink()
                    or path.is_symlink() or not path.resolve().is_relative_to(self.store.runtime.resolve())):
                raise DocumentPipelineError('document_blob_unavailable','附件存储路径无效')
            fd = os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd,'rb') as stream:
                raw = stream.read(10*1024*1024+1)
            if len(raw)!=row['size'] or sha256(raw).hexdigest()!=row['sha256']:
                raise DocumentPipelineError('document_blob_integrity_failed','附件完整性校验失败')
            extraction = _bounded_extract(raw,row['mime_type'],row['attachment_id'],tesseract)
        except (OSError,DocumentPipelineError) as error:
            extraction = _failure(row,getattr(error,'code','document_blob_unavailable'))
        return self.finish(row['id'],worker_token,extraction)

    @staticmethod
    def mark_adopted(db, session_id, attachment_id, result_hash, adopted_version):
        """Caller checks permission/version and updates draft in this transaction.

        Never use this as a confirmation: retained page blockers still prevent
        freezing. It merely records which reviewed extraction was incorporated.
        """
        if isinstance(adopted_version,bool) or not isinstance(adopted_version,int) or adopted_version<1:
            raise ValueError('采用草稿版本无效')
        row = db.execute('SELECT * FROM sales_document_jobs WHERE session_id=? AND attachment_id=?',
                         (session_id,attachment_id)).fetchone()
        if row is None or row['status'] not in {'ready_for_review','not_ready'}:
            raise DocumentPipelineError('document_result_not_ready','附件尚未完成提取')
        if not result_hash or result_hash!=row['result_hash']:
            raise DocumentPipelineError('document_result_changed','附件提取版本已改变')
        if row['adopted_version'] is None:
            current = db.execute('SELECT version FROM sales_assistant_sessions WHERE id=?',
                                 (session_id,)).fetchone()
            if current is None or current['version']!=adopted_version:
                raise DocumentPipelineError('document_adoption_version_mismatch','草稿版本未原子更新')
            db.execute('UPDATE sales_document_jobs SET adopted_version=?,updated_at=? WHERE id=?',
                       (adopted_version,time.time(),row['id']))
        return _status(db.execute('SELECT * FROM sales_document_jobs WHERE id=?',(row['id'],)).fetchone())


def _extract_child():
    # Linux host worker gets hard memory/CPU limits. macOS RLIMIT_AS is not
    # reliable; wall time and source/output bounds apply on both platforms.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(20,20))
        if sys.platform.startswith('linux'):
            resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    except (ImportError,ValueError,OSError):
        pass
    _flag,mime_type,source_id,encoded = sys.argv[1:]
    value = json.loads(encoded)
    config = None if value is None else TesseractConfig(Path(value['command']),
        tuple(value['languages']),value['timeout_seconds'])
    raw = sys.stdin.buffer.read(10*1024*1024+1)
    result = extract_document(raw,mime_type,source_id=source_id,tesseract=config)
    encoded_result = dumps(asdict(result)).encode()
    if len(encoded_result)>MAX_RESULT_BYTES:
        raise SystemExit(2)
    sys.stdout.buffer.write(encoded_result)


if __name__=='__main__':
    if len(sys.argv)==5 and sys.argv[1]=='--extract-child':
        _extract_child()
    else:
        raise SystemExit('This module is an internal document extraction child, not an ERP runner.')
