"""Durable Codex draft jobs. Models cannot confirm, create products, or submit ERP.

API calls only freeze source references. The separate host worker prepares input,
runs the owner's restricted Codex, and records proposals. Adoption is a versioned
human action; model output never becomes an execution permission.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from copy import deepcopy
from hashlib import sha256
import json
import re
import uuid

from .store import dumps, timestamp
from .daily_work import _setting
from .sales_ownership import fixed_sales_owner
from .sales_dependencies import catalog_fingerprint, job_catalog_current, product_dependencies, changed_products


MAX_JSON = 2 * 1024 * 1024
MAX_LINES = 500
TERMINAL = {'needs_clarification','ready_for_review','failed','stale','adopted','cancelled'}


class CodexJobError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def digest(value):
    return sha256(dumps(value).encode()).hexdigest()


def ensure_codex_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sales_codex_jobs(
        id TEXT PRIMARY KEY,session_id TEXT NOT NULL,actor_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,base_version INTEGER NOT NULL,data_version TEXT NOT NULL,
        input_hash TEXT NOT NULL,input_json TEXT NOT NULL,status TEXT NOT NULL,
        step TEXT NOT NULL DEFAULT 'queued',worker_token TEXT NOT NULL DEFAULT '',
        lease_until TEXT NOT NULL DEFAULT '',result_hash TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}',source_provenance TEXT NOT NULL DEFAULT '{}',
        error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,adopted_version INTEGER,
        FOREIGN KEY(session_id) REFERENCES sales_assistant_sessions(id))''')
    if 'source_provenance' not in {row[1] for row in db.execute('PRAGMA table_info(sales_codex_jobs)')}:
        db.execute("ALTER TABLE sales_codex_jobs ADD COLUMN source_provenance TEXT NOT NULL DEFAULT '{}'")
    if 'catalog_dependencies' not in {row[1] for row in db.execute('PRAGMA table_info(sales_codex_jobs)')}:
        db.execute("ALTER TABLE sales_codex_jobs ADD COLUMN catalog_dependencies TEXT NOT NULL DEFAULT '{}'")
    from .sales_rule_versions import ensure
    ensure(db)
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS sales_codex_session_active
        ON sales_codex_jobs(session_id) WHERE status IN ('queued','running')''')
    db.execute('''CREATE INDEX IF NOT EXISTS sales_codex_queue
        ON sales_codex_jobs(status,created_at,id)''')


def enabled(db):
    policy = _setting(db,'sales_codex_policy',{})
    return isinstance(policy,dict) and policy.get('enabled') is True


def latest_job(db,session,*,full=True):
    row=db.execute('SELECT * FROM sales_codex_jobs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                   (session['id'],)).fetchone()
    if row is None:
        return None
    result=json.loads(row['result_json'])
    unchanged=(session['version']==row['base_version'] and
               job_catalog_current(db,row,session) and
               fixed_sales_owner(db,session['profile_id'],session['customer_name'])==row['owner_id'])
    status=row['status']
    if not unchanged and status in {'ready_for_review','needs_clarification'}:
        status='stale'
    value={key:row[key] for key in ('id','base_version','result_hash','error_code','step','created_at','updated_at')}
    value['adopted_version']=row['adopted_version']
    value['changed_product_codes']=changed_products(db,row,session)
    value.update(status=status,engine='codex',can_adopt=(status=='ready_for_review'
        and unchanged and enabled(db) and not session['submission_operation_id']
        and session['status'] not in {'submitting','submitted','submission_unknown','submission_blocked'}),
        assistant_text=result.get('assistant_text',''),issues=result.get('issues',[]),
        questions=result.get('questions',[]),new_product_suggestions=result.get('new_product_suggestions',[]))
    if full:
        value['preview']=result.get('draft',{})
    return value


def freeze_input(db,session,actor_id,body):
    """Bind every original attachment and message, without doing OCR in HTTP."""
    attachments=[dict(row) for row in db.execute('''SELECT id,name,mime_type,sha256,size,blob_name
        FROM sales_assistant_attachments WHERE session_id=? ORDER BY rowid''',(session['id'],))]
    selected=body.get('attachment_ids')
    if selected is not None and (not isinstance(selected,list) or any(not isinstance(v,str) for v in selected)
            or len(selected)!=len(set(selected))
            or set(selected)!={row['id'] for row in attachments}):
        raise CodexJobError('sales_codex_sources_incomplete','整理须包含本会话全部原附件，请核对附件范围')
    messages=[dict(row) for row in db.execute('''SELECT id,role,text,attachment_id
        FROM sales_assistant_messages WHERE session_id=? ORDER BY rowid''',(session['id'],))]
    if len(messages)>200:
        raise CodexJobError('sales_codex_source_limit','会话资料过多，请分成新的录单会话')
    draft=json.loads(session['draft'])
    if not attachments and not any(row['role']=='user' and not row['attachment_id']
                                   for row in messages) and not draft.get('lines'):
        raise CodexJobError('sales_codex_sources_missing','请先提供订单文字或附件')
    from .sales_rule_versions import pinned
    value={'rules_version':pinned(db),'schema_version':'ul_sales_codex_job_v1','job_id':uuid.uuid4().hex,
           'session_id':session['id'],'base_version':session['version'],
           'profile_id':session['profile_id'],'customer_name':session['customer_name'],
           'actor_id':actor_id,'owner_id':fixed_sales_owner(db,session['profile_id'],session['customer_name']),
           'current_data_version':_setting(db,'data_version',''),'messages':messages,
           'draft':draft,'attachments':attachments,
           'catalog_dependency_contract':2}
    previous=db.execute("SELECT id,result_json FROM sales_codex_jobs WHERE session_id=? AND result_hash<>'' ORDER BY rowid DESC LIMIT 1",
                        (session['id'],)).fetchone()
    if previous:
        proposal=json.loads(previous['result_json'])
        value['previous_proposal']={'job_id':previous['id'], 'draft':proposal.get('draft',{}),
                                    'questions':proposal.get('questions',[])}
    if len(dumps(value).encode())>MAX_JSON:
        raise CodexJobError('sales_codex_source_limit','订单资料超过单次整理范围，请拆分')
    return value


def enqueue(db,snapshot):
    active=db.execute("SELECT id FROM sales_codex_jobs WHERE session_id=? AND status IN ('queued','running')",
                      (snapshot['session_id'],)).fetchone()
    if active:
        raise CodexJobError('sales_codex_job_active','本会话正在由 Codex 整理，请等待当前结果')
    original=dict(snapshot);original.pop('input_hash',None)
    snapshot={**original,'input_hash':digest(original)}
    now=timestamp()
    db.execute('''INSERT INTO sales_codex_jobs
        (id,session_id,actor_id,owner_id,base_version,data_version,input_hash,input_json,status,
         created_at,updated_at) VALUES (?,?,?,?,?,?,?,?, 'queued',?,?)''',
        (snapshot['job_id'],snapshot['session_id'],snapshot['actor_id'],snapshot['owner_id'],
         snapshot['base_version'],snapshot['current_data_version'],snapshot['input_hash'],
         dumps(snapshot),now,now))
    return snapshot['job_id']


def source_manifest(snapshot):
    sources=[]
    for item in snapshot['messages']:
        if item['role']=='user' and not item['attachment_id']:
            sources.append({'source_id':item['id'],'kind':'text','text':item['text'],
                            'sha256':sha256(item['text'].encode()).hexdigest()})
    for item in snapshot['draft'].get('lines',[]):
        if not item.get('source_ref'):
            raw=dumps(item)
            sources.append({'source_id':'draft:'+item['id'],'kind':'draft','text':raw,
                            'sha256':sha256(raw.encode()).hexdigest()})
    for item in snapshot['attachments']:
        sources.append({'source_id':item['id'],'kind':'attachment','name':item['name'],
                        'mime_type':item['mime_type'],'sha256':item['sha256']})
    return sources


def _bounded_text(value,limit=2000):
    if not isinstance(value,str) or len(value)>limit or '\x00' in value:
        raise CodexJobError('sales_codex_invalid_result','Codex 返回的文字字段无效')
    return value.strip()


def validate_output(value,source_ids,physical_rows=(),allowed_refs=()):
    """Validate provenance and full known spreadsheet coverage, not model confidence."""
    keys={'status','assistant_text','draft','questions','new_product_suggestions','source_coverage'}
    if not isinstance(value,dict) or set(value)!=keys or value.get('status') not in {
            'ready_for_review','needs_clarification'}:
        raise CodexJobError('sales_codex_invalid_result','Codex 返回格式无效，原资料已保留')
    value=deepcopy(value)
    result=dict(value)
    result['assistant_text']=_bounded_text(value['assistant_text'],10000)
    questions=value['questions']
    if not isinstance(questions,list) or len(questions)>100:
        raise CodexJobError('sales_codex_invalid_result','Codex 追问格式无效')
    result['questions']=[_bounded_text(q,1000) for q in questions]
    draft=value['draft']
    if not isinstance(draft,dict) or set(draft)!={'customer_order_no','delivery_date','note',
            'tax_basis','tax_rate','lines'} or draft['tax_rate'] not in (13,'13'):
        raise CodexJobError('sales_codex_invalid_result','订单草稿或税率格式无效')
    for field,limit in (('customer_order_no',200),('delivery_date',10),('note',2000)):
        _bounded_text(draft[field],limit)
    if draft['tax_basis'] not in {'tax_inclusive','tax_exclusive'}:
        raise CodexJobError('sales_codex_invalid_result','订单税价口径无效')
    rows=draft['lines']; suggestions=value['new_product_suggestions']
    if not isinstance(rows,list) or len(rows)>MAX_LINES or not isinstance(suggestions,list) or len(suggestions)>MAX_LINES:
        raise CodexJobError('sales_codex_invalid_result','明细数量超出范围')
    line_keys={'source_ids','source_quote','product_code','original_name','original_spec',
               'original_material','standard','grade','surface','customer_part_no','quantity','unit_price','unit',
               'delivery_date','project_no','line_no','drawing_no'}
    suggestion_keys={'source_ids','source_quote','product_name','spec','standard','grade',
                     'surface','material','base_unit','reason'}
    allowed=set(source_ids)|set(physical_rows)|set(allowed_refs);referenced=set()
    for row,expected in [(v,line_keys) for v in rows]+[(v,suggestion_keys) for v in suggestions]:
        if not isinstance(row,dict) or set(row)!=expected:
            raise CodexJobError('sales_codex_invalid_result','明细字段无效')
        refs=row['source_ids']
        if not isinstance(refs,list) or not refs or len(refs)>100 or any(not isinstance(ref,str) for ref in refs) \
                or len(set(refs))!=len(refs) or any(ref not in allowed for ref in refs):
            raise CodexJobError('sales_codex_source_mismatch','Codex 明细没有对应原资料')
        referenced.update(refs)
        for key in expected-{'source_ids'}:
            if key in {'quantity','unit_price'}:
                raw=row[key]
                if raw is None:row[key]=''
                elif isinstance(raw,(str,int,float)) and not isinstance(raw,bool):
                    row[key]=str(raw)
                    if row[key].strip():
                        try:
                            numeric=Decimal(row[key])
                            if not numeric.is_finite() or numeric<0 or (key=='quantity' and numeric==0):
                                raise InvalidOperation
                        except InvalidOperation:
                            raise CodexJobError('sales_codex_invalid_result','数量或价格不是有效数值') from None
                else:raise CodexJobError('sales_codex_invalid_result','数量或价格字段无效')
            _bounded_text(row[key],1000 if key in {'source_quote','reason'} else 300)
    coverage=value['source_coverage']
    if not isinstance(coverage,list) or len(coverage)!=len(source_ids):
        raise CodexJobError('sales_codex_coverage_incomplete','资料覆盖不完整，不能直接采用')
    seen=set()
    for row in coverage:
        if (not isinstance(row,dict) or set(row)!={'source_id','status','line_count','detail'}
                or not isinstance(row['source_id'],str)
                or row['source_id'] not in source_ids or row['source_id'] in seen
                or row['status'] not in {'complete','needs_review'}
                or type(row['line_count']) is not int or not 0<=row['line_count']<=MAX_LINES):
            raise CodexJobError('sales_codex_coverage_incomplete','资料覆盖结果无效')
        seen.add(row['source_id']);_bounded_text(row['detail'],1000)
    issues=[]
    missing=set(physical_rows)-referenced
    if missing:
        issues.append({'code':'sales_codex_source_rows_missing','message':'原表格还有明细未整理，请补齐后再采用'})
    if any(row['status']!='complete' for row in coverage):
        issues.append({'code':'sales_codex_coverage_incomplete','message':'原资料尚未完整识别，请补充清晰资料'})
    if questions or suggestions or not rows or any(not row['product_code'] or not row['quantity']
                                                 or not row['unit_price'] for row in rows):
        issues.append({'code':'sales_codex_needs_clarification','message':'有待补充资料、商品编码或新商品建议'})
    if issues:
        result['status']='needs_clarification'
    result['issues']=issues
    if len(dumps(result).encode())>MAX_JSON:
        raise CodexJobError('sales_codex_result_limit','整理结果超出范围')
    return result


class SalesCodexPipeline:
    def __init__(self,store):
        self.store=store
        with store.connect(write=True) as db:
            ensure_codex_schema(db)

    def claim(self,worker_token,*,now=None):
        if not isinstance(worker_token,str) or not re.fullmatch(r'[0-9a-f]{32}',worker_token):
            raise ValueError('invalid worker token')
        now=now or datetime.now(timezone.utc)
        with self.store.connect(write=True) as db:
            if not enabled(db) or _setting(db,'restoration_hold',False):
                return None
            db.execute("""UPDATE sales_codex_jobs SET status='failed',step='interrupted',
                error_code='sales_codex_interrupted',worker_token='',updated_at=?
                WHERE status='running' AND lease_until<=?""",(timestamp(),now.isoformat()))
            if db.execute("SELECT 1 FROM sales_codex_jobs WHERE status='running' LIMIT 1").fetchone():
                return None
            row=db.execute("SELECT * FROM sales_codex_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:return None
            session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(row['session_id'],)).fetchone()
            if not self._current(db,row,session):
                db.execute("UPDATE sales_codex_jobs SET status='stale',step='stale',updated_at=? WHERE id=?",(timestamp(),row['id']))
                return None
            lease=(now+timedelta(minutes=10)).isoformat()
            db.execute("UPDATE sales_codex_jobs SET status='running',step='analyze',worker_token=?,lease_until=?,updated_at=? WHERE id=?",
                       (worker_token,lease,timestamp(),row['id']))
            return json.loads(row['input_json'])

    @staticmethod
    def _current(db,job,session):
        return bool(session and session['version']==job['base_version'] and
            job_catalog_current(db,job,session) and
            fixed_sales_owner(db,session['profile_id'],session['customer_name'])==job['owner_id'] and
            not session['submission_operation_id'] and session['status'] not in {
                'submitting','submitted','submission_unknown','submission_blocked'})

    def progress(self,job_id,worker_token,step):
        if step not in {'analyze','match','review'}:raise ValueError('invalid step')
        with self.store.connect(write=True) as db:
            result=db.execute("UPDATE sales_codex_jobs SET step=?,updated_at=? WHERE id=? AND status='running' AND worker_token=?",
                              (step,timestamp(),job_id,worker_token))
            if result.rowcount!=1:raise CodexJobError('sales_codex_claim_changed','整理任务已变化')

    def finish(self,job_id,worker_token,*,output=None,source_ids=(),physical_rows=(),source_provenance=None,error_code='',catalog_dependencies=None):
        result=validate_output(output,source_ids,physical_rows,(source_provenance or {}).keys()) if not error_code else None
        with self.store.connect(write=True) as db:
            row=db.execute('SELECT * FROM sales_codex_jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row['status']!='running' or row['worker_token']!=worker_token:
                raise CodexJobError('sales_codex_claim_changed','整理任务已变化')
            session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(row['session_id'],)).fetchone()
            provenance=source_provenance or {}
            source_hashes={item['source_id']:item['sha256'] for item in source_manifest(json.loads(row['input_json']))}
            if not isinstance(provenance,dict) or any(not isinstance(key,str)
                    or not isinstance(item,dict) or item.get('source_id') not in source_hashes
                    or item.get('sha256')!=source_hashes[item['source_id']]
                    for key,item in provenance.items()):
                raise CodexJobError('sales_codex_source_mismatch','原资料定位校验失败')
            status='failed' if error_code else result['status']
            if not self._current(db,row,session):status='stale'
            dependencies={}
            if result and json.loads(row['input_json']).get('catalog_dependency_contract')==2:
                dependencies=product_dependencies(db,session['profile_id'],
                    [line['product_code'] for line in result['draft']['lines']])
                if catalog_dependencies is not None and dependencies!=catalog_dependencies:
                    status='stale'
                    error_code='sales_codex_relevant_product_changed'
                    dependencies=catalog_dependencies
            data=dumps(result or {})
            db.execute('''UPDATE sales_codex_jobs SET status=?,step=?,result_json=?,result_hash=?,source_provenance=?,
                error_code=?,catalog_dependencies=?,worker_token='',lease_until='',updated_at=? WHERE id=?''',
                (status,status,data,sha256(data.encode()).hexdigest() if result else '',dumps(provenance),
                 error_code[:100],dumps(dependencies),timestamp(),job_id))
            if result:
                text=result['assistant_text']
                if result['questions']:text+='\n'+'\n'.join(result['questions'])
                db.execute('INSERT INTO sales_assistant_messages VALUES (?,?,?,?,?,?)',
                           (uuid.uuid4().hex,row['session_id'],'assistant',text,'',timestamp()))
            return {'id':job_id,'status':status,'external_submit':False}
