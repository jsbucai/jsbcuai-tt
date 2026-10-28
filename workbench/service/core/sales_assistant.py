"""Durable sales conversations and reviewed drafts with an opt-in web queue.

Attachments and customer messages are untrusted source material. Only explicit
product codes and quantities form lines; ambiguous identities remain issues.
Confirmation freezes one version locally and never claims an ERP order exists.
Submitting can only enqueue a frozen version; the isolated browser worker must
validate the import table and receive a complete website import confirmation.
ERP readback is not part of this submission workflow.
"""
from __future__ import annotations

from datetime import date, datetime
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from zipfile import ZipFile, BadZipFile
from zoneinfo import ZoneInfo

from flask import Blueprint, g, jsonify, request, send_file

from .daily_work import DailyWorkConflict, _text, _setting, _flag
from .sales_ownership import fixed_sales_owner
from .sales_pricing import VAT_PERCENT, is_fixed_vat_rate, quote_basis, quote_price
from .store import dumps, timestamp
from .unit_policy import CONFIRMED_BASE_UNIT, unit_evidence
from .sales_web_queue import (SalesWebQueueBlocked, enqueue_sales_web_operation,
                              prepare_sales_web_payload)
from .sales_web_import import catalog_spec_dimension, build_source_summary
from . import sales_api_jobs
from . import sales_manual_import
from .sales_source_parser import parse_text, parse_excel
from .sales_document_pipeline import (SalesDocumentPipeline, DocumentPipelineError,
                                     ensure_document_schema, enqueue_document, DOCUMENT_TYPES)
from .sales_source_review import inspect_source_match, make_source_confirmation, source_confirmation_valid
from .sales_history_matching import search_candidates
from .sales_codex_pipeline import (SalesCodexPipeline, CodexJobError, enabled as codex_enabled,
                                   latest_job as codex_latest, freeze_input as codex_freeze,
                                   enqueue as codex_enqueue, source_manifest as codex_sources)
from .sales_skill_matching import catalog_attributes
from .sales_new_product_jobs import (SalesNewProductJobs,projection as new_product_projection,
                                    request_prepare as new_product_prepare,
                                    confirm as new_product_confirm,suggestion_id as new_product_suggestion_id)
from .web_operations import WebOperations, now as web_now
from .sales_web_transport import WebImportOutcome, WebImportBlocked, validate_sales_header_limits

MAX_ATTACHMENT = 10 * 1024 * 1024
MAX_LINES = 500
IDENTITY = ('profile_id','warehouse_id','product_id','sku_id','base_unit')


class SalesAssistantError(DailyWorkConflict):
    def __init__(self, message, code, status=409):
        super().__init__(message, code)
        self.http_status = status


def _digest(value):
    return sha256(dumps(value).encode()).hexdigest()


def _decimal_text(value, positive=False):
    if isinstance(value,bool): raise ValueError()
    if len(str(value))>80: raise ValueError()
    number=Decimal(str(value))
    if not number.is_finite() or number < 0 or positive and number == 0 or number.adjusted()>15 or number.as_tuple().exponent < -12:
        raise ValueError()
    return format(number.normalize(),'f')


def _day(value):
    if not value: return ''
    if isinstance(value,datetime): return value.date().isoformat()
    if isinstance(value,date): return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _key(body):
    if not isinstance(body,dict): raise ValueError('请求必须为 JSON 对象')
    key=_text(body.get('idempotency_key'))
    if not key or len(key)>128: raise ValueError('请提供操作标识')
    return key


def _version(body):
    value=body.get('version')
    if isinstance(value,bool): raise ValueError('请提供当前会话版本')
    try:
        version=int(value)
    except (ValueError,TypeError): raise ValueError('请提供当前会话版本') from None
    if version<1 or str(value)!=str(version): raise ValueError('请提供当前会话版本')
    return version


def _migrate_legacy_line_ids(db):
    """Give unsubmitted legacy rows server-owned IDs without guessing a match.

    Each legacy physical row has its own deterministic identity, even when
    several rows carry the same SKU. Submitted/frozen execution facts remain
    untouched. An unsubmitted confirmation is archived and invalidated with
    a new version, so no request can execute a silently altered confirmation.
    """
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    submitted_sessions=set()
    if 'sales_submission_attempts' in tables:
        submitted_sessions.update(row[0] for row in db.execute(
            'SELECT session_id FROM sales_submission_attempts'))
    if 'web_operation_payloads' in tables:
        for payload_row in db.execute('SELECT payload_json FROM web_operation_payloads'):
            try: payload=json.loads(payload_row[0])
            except (TypeError,ValueError): continue
            if not isinstance(payload,dict): continue
            identity=_text(payload.get('session_id'))
            if identity: submitted_sessions.add(identity)
            frozen=payload.get('frozen_session')
            if isinstance(frozen,dict) and _text(frozen.get('id')):
                submitted_sessions.add(_text(frozen['id']))
    changed=0
    for row in db.execute("""SELECT * FROM sales_assistant_sessions
            WHERE status IN ('collecting','draft','confirmed')
            AND submission_operation_id='' AND erp_order_no='' AND submitted_at=''""").fetchall():
        if row['id'] in submitted_sessions: continue
        try: draft=json.loads(row['draft'])
        except (TypeError,ValueError): continue
        if not isinstance(draft,dict): continue
        lines=draft.get('lines')
        if (not isinstance(lines,list) or not lines or len(lines)>MAX_LINES
                or any(not isinstance(line,dict) for line in lines)): continue
        valid_ids=[line['id'] for line in lines
                   if isinstance(line.get('id'),str) and line['id'].strip()]
        if len(valid_ids)==len(lines) or len(set(valid_ids))!=len(valid_ids): continue
        source_ids=[line['source_ref'].get('id') for line in lines
                    if isinstance(line.get('source_ref'),dict) and line['source_ref'].get('id')]
        # Duplicated source identity is a review conflict, not an invitation
        # to decide which of two rows was meant by a source report.
        if len(source_ids)!=len(set(source_ids)): continue
        original_frozen={key:row[key] for key in
                         ('version','status','draft','confirmed_version','confirmed_hash','confirmed_payload')}
        used=set(valid_ids); assigned=[]; source_links={}
        for ordinal,line in enumerate(lines,1):
            if not isinstance(line.get('id'),str) or not line['id'].strip():
                base=f"ul-sales-legacy-line-v1:{row['id']}:{row['version']}:{ordinal}"
                attempt=0
                while True:
                    identity=uuid.uuid5(uuid.NAMESPACE_URL,base+f':{attempt}').hex
                    if identity not in used: break
                    attempt+=1
                line['id']=identity;used.add(identity)
                assigned.append({'ordinal':ordinal,'id':identity})
            ref=line.get('source_ref')
            if isinstance(ref,dict) and ref.get('id'):
                source_links[ref['id']]=line['id']
        reports=draft.get('source_rows',[])
        if isinstance(reports,list):
            for report in reports:
                if isinstance(report,dict) and report.get('id') in source_links:
                    report['line_id']=source_links[report['id']]
        problems=draft.get('issues',[])
        if not isinstance(problems,list): problems=[]
        draft['issues']=[*problems,{'code':'legacy_line_identity_review',
            'message':'旧草稿明细已补齐稳定行号，请打开编辑核对并保存后重新确认'}]
        moment=timestamp(); version=row['version']+1
        db.execute("""UPDATE sales_assistant_sessions SET draft=?,version=?,status='draft',
            confirmed_version=0,confirmed_hash='',confirmed_payload='{}',updated_at=? WHERE id=?""",
            (dumps(draft),version,moment,row['id']))
        db.execute('INSERT INTO sales_assistant_events VALUES (?,?,?,?,?,?,?)',
            (uuid.uuid4().hex,row['id'],'system:legacy_line_identity','migrate_line_identity',version,
             dumps({'assigned_lines':assigned,'original_frozen':original_frozen,
                    'reason':'补齐服务端行身份；未提交确认须重新核对'}),moment))
        changed+=1
    return changed


def _has_sales_execution_reference(db, session_id):
    """A durable intent protects a session even if an old status is stale."""
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'sales_manual_imports' in tables and sales_manual_import.active(db,session_id): return True
    if 'sales_api_batches' in tables and db.execute(
            'SELECT 1 FROM sales_api_batches WHERE session_id=? LIMIT 1',(session_id,)).fetchone(): return True
    if 'sales_submission_attempts' in tables and db.execute(
            'SELECT 1 FROM sales_submission_attempts WHERE session_id=? LIMIT 1',
            (session_id,)).fetchone(): return True
    if 'web_operation_payloads' in tables and db.execute('''SELECT 1 FROM web_operation_payloads
        WHERE CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.session_id') END=?
        OR CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.frozen_session.id') END=?
        LIMIT 1''',(session_id,session_id)).fetchone(): return True
    return False


def ensure_schema(store):
    with store.connect(write=True) as db:
        for sql in (
            '''CREATE TABLE IF NOT EXISTS sales_assistant_sessions(
                id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,customer_name TEXT NOT NULL,
                created_by TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,
                draft TEXT NOT NULL,confirmed_version INTEGER NOT NULL DEFAULT 0,
                confirmed_hash TEXT NOT NULL DEFAULT '',confirmed_payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
            '''CREATE INDEX IF NOT EXISTS sales_assistant_customer
                ON sales_assistant_sessions(profile_id,customer_name,updated_at)''',
            '''CREATE TABLE IF NOT EXISTS sales_assistant_messages(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,role TEXT NOT NULL,
                text TEXT NOT NULL,attachment_id TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sales_assistant_sessions(id))''',
            '''CREATE INDEX IF NOT EXISTS sales_assistant_message_session
                ON sales_assistant_messages(session_id,created_at)''',
            '''CREATE TABLE IF NOT EXISTS sales_assistant_attachments(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,name TEXT NOT NULL,
                mime_type TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
                blob_name TEXT NOT NULL,status TEXT NOT NULL,created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,FOREIGN KEY(session_id) REFERENCES sales_assistant_sessions(id))''',
            '''CREATE TABLE IF NOT EXISTS sales_assistant_operations(
                actor_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,session_id TEXT NOT NULL,action TEXT NOT NULL,
                result TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(actor_id,idempotency_key))''',
            '''CREATE TABLE IF NOT EXISTS sales_assistant_events(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,actor_id TEXT NOT NULL,
                action TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,
                created_at TEXT NOT NULL,FOREIGN KEY(session_id) REFERENCES sales_assistant_sessions(id))''',
        ): db.execute(sql)
        existing={row['name'] for row in db.execute('PRAGMA table_info(sales_assistant_sessions)')}
        for name in ('submission_operation_id','erp_order_no','submitted_at','erp_customer_id',
                     'archived_at','archived_by','archive_kind'):
            if name not in existing:
                db.execute(f"ALTER TABLE sales_assistant_sessions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        ensure_document_schema(db)
        _migrate_legacy_line_ids(db)


class SalesAssistant:
    def __init__(self,store,history_reference=None):
        self.store=store
        self.history_reference=history_reference
        with store.connect(write=True) as db:
            sales_api_jobs.ensure_schema(db)
            sales_manual_import.ensure_schema(db)
        self.blobs=store.runtime/'sales-assistant'/'blobs'
        if not self.blobs.resolve().is_relative_to(store.runtime.resolve()):
            raise ValueError('录单附件目录不得指向运行目录外')
        self.blobs.mkdir(parents=True,exist_ok=True,mode=0o700)
        ensure_schema(store)
        self.documents=SalesDocumentPipeline(store)
        self.codex=SalesCodexPipeline(store)
        self.new_products=SalesNewProductJobs(store)
        with store.connect(write=True) as db:
            for attachment in db.execute('SELECT id,mime_type FROM sales_assistant_attachments'):
                if attachment['mime_type'] in DOCUMENT_TYPES:
                    enqueue_document(db,attachment['id'])
        self.web_operations=WebOperations(store)

    @staticmethod
    def _actor(db,user):
        actor=_text(user.get('id')) if isinstance(user,dict) else ''
        row=db.execute('SELECT roles,active,is_admin FROM staff_users WHERE id=?',(actor,)).fetchone()
        if not actor or not row or not row['active'] or not user.get('active',True):
            raise SalesAssistantError('请登录在岗销售账号','role_forbidden',403)
        roles=json.loads(row['roles'] or '[]')
        if not row['is_admin'] and 'sales' not in roles:
            raise SalesAssistantError('账号没有销售岗位权限','role_forbidden',403)
        return actor,bool(row['is_admin'])

    def _scope(self,db,user,profile,customer,require_owner=False):
        actor,admin=self._actor(db,user)
        owner=fixed_sales_owner(db,profile,customer)
        if not owner and (require_owner or not admin):
            raise SalesAssistantError('客户固定销售待指定或核对','customer_owner_review')
        if not admin and owner!=actor:
            raise SalesAssistantError('客户未分配给当前销售','task_owner_conflict',403)
        return actor,owner

    def _session(self,db,user,identity,write=False):
        row=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(identity,)).fetchone()
        if not row: raise KeyError('录单会话不存在')
        if write and row['archived_at']:
            raise SalesAssistantError('对话已移除，请先在录单网页恢复','sales_session_archived')
        self._scope(db,user,row['profile_id'],row['customer_name'],require_owner=write)
        return row

    def _project_history(self,db,row,draft):
        eligible=[]
        for line in draft.get('lines',[]):
            if not isinstance(line,dict):
                continue
            source=line.get('source_ref')
            if not isinstance(source,dict) or not source.get('id'):
                continue
            if source.get('match_basis')=='explicit_code' and line.get('resolved_identity'):
                continue
            eligible.append((line,source))
        if not eligible:
            return
        unavailable={'status':'needs_review','candidates':[],
                     'message':'历史参考资料暂不能读取，请核对原件；输入仍保留'}
        def show(line,matched):
            line['match_candidates']=matched.get('candidates',[])
            line['match_status']={key:matched.get(key,'') for key in
                                  ('status','message','covered_from','covered_to')}
        try:
            connection=self.history_reference.connect() if self.history_reference else nullcontext(None)
            with connection as history_db:
                for line,source in eligible:
                    matched=search_candidates(db,row['profile_id'],row['customer_name'],source,
                                              history_db=history_db)
                    show(line,matched)
        except (ValueError,sqlite3.DatabaseError,OSError):
            for line,_ in eligible:
                show(line,unavailable)

    def _view(self,db,row,full=True,user=None):
        owner=fixed_sales_owner(db,row['profile_id'],row['customer_name'])
        person=db.execute('SELECT display_name FROM staff_users WHERE id=?',(owner,)).fetchone()
        shown_draft=json.loads(row['draft'])
        known_sources={entry.get('source_id') for entry in shown_draft.get('source_rows',[])
                       if isinstance(entry,dict)}
        missing=[self._attachment_coverage(attachment,'original_attachment_not_reviewed')
                 for attachment in db.execute('SELECT * FROM sales_assistant_attachments WHERE session_id=?',
                                              (row['id'],))
                 if attachment['id'] not in known_sources]
        if missing:
            shown_draft={**shown_draft,'source_rows':[*shown_draft.get('source_rows',[]),*missing],
                'issues':[*shown_draft.get('issues',[]),{'code':'original_attachment_not_reviewed',
                           'message':'原附件尚未完整核对，请核对资料后重新确认'}]}
        if full:
            # Candidates are a read projection of server-owned source rows.
            # They never change the stored draft, its version or confirmation.
            self._project_history(db,row,shown_draft)
        ready=False
        if full and not missing and row['status']=='confirmed' and row['confirmed_version']==row['version']:
            try:
                prepare_sales_web_payload(db,row,owner)
                ready=True
            except SalesWebQueueBlocked:
                pass
        value={key:row[key] for key in ('id','profile_id','customer_name','version','status',
            'confirmed_version','confirmed_hash','created_at','updated_at','archived_at','archive_kind')}
        operation_id=row['submission_operation_id']
        operation=db.execute('SELECT status,result_json FROM web_operations WHERE id=?',(operation_id,)).fetchone() if operation_id else None
        submission_state=operation['status'] if operation else 'ready' if ready else 'not_connected'
        completion=json.loads(operation['result_json'] or '{}') if operation else {}
        imported=bool(operation and operation['status']=='succeeded' and
                      completion.get('web_import_confirmed') is True)
        value.update(fixed_sales_owner_id=owner,fixed_sales_owner_name=person['display_name'] if person else '',
            data_version=_setting(db,'data_version',''),
            submit_available=ready,
            submission_state=submission_state,submission_operation_id=operation_id,
            submission_completion_basis=completion.get('completion_basis',''),
            erp_readback_performed=completion.get('erp_readback_performed') is True,
            erp_order_numbers=completion.get('document_numbers',[]) if imported else [],
            erp_document_count=completion.get('document_count',0) if imported else 0,
            erp_reference_scope=completion.get('reference_scope','') if imported else '',
            erp_order_no=row['erp_order_no'],draft=shown_draft)
        if full:
            value['messages']=[dict(item) for item in db.execute('''SELECT id,role,text,attachment_id,created_at
                FROM sales_assistant_messages WHERE session_id=? ORDER BY rowid''',(row['id'],))]
            value['attachments']=[dict(item) for item in db.execute('''SELECT id,name,mime_type,sha256,size,status,created_at
                FROM sales_assistant_attachments WHERE session_id=? ORDER BY rowid''',(row['id'],))]
            processing={item['attachment_id']:item for item in self.documents.status(db,row['id'])}
            for attachment in value['attachments']:
                if attachment['id'] in processing:
                    attachment['document_processing']=processing[attachment['id']]
        api=sales_api_jobs.projection(db,row['id'])
        value['api_submission']=api
        value['manual_import']=sales_manual_import.projection(db,row['id'])
        if full:
            from .sales_customer_directory import delivery
            value['customer_delivery']=delivery(db,row['profile_id'],row['erp_customer_id'],row['customer_name'])
            value['planned_erp_numbers']=[]
            value['erp_number_error']=''
            if not api and shown_draft.get('lines'):
                try:value['planned_erp_numbers']=sales_api_jobs.planned_numbers(shown_draft)
                except sales_api_jobs.SalesAPIError as error:value['erp_number_error']=str(error)
        value['api_enabled']=sales_api_jobs.policy(db).get('enabled') is True and sales_api_jobs.policy(db).get('contract_accepted') is True
        if api:
            value.update(submission_state=api['status'],submission_completion_basis='api_receipt',
                erp_readback_performed=False,
                erp_order_numbers=[d['number'] for d in api['documents'] if d['status']=='accepted'])
        value['codex_enabled']=codex_enabled(db)
        value['codex_job']=codex_latest(db,row,full=full)
        person=db.execute('SELECT active,is_admin FROM staff_users WHERE id=?',
                          ((_text((user or {}).get('id'))),)).fetchone()
        value['new_product_plans']=new_product_projection(db,row,is_admin=bool(person and person['active'] and person['is_admin']))
        job=value['codex_job']
        if job:
            previous={p['suggestion_id']:p for p in value['new_product_plans'] if p['job_id']==job['id']}
            permitted=(job['status']=='needs_clarification' and job['base_version']==row['version']
                       and value['codex_enabled'] and not row['submission_operation_id'])
            job['new_product_suggestions']=[{**item,'id':new_product_suggestion_id(job,i),
                'can_prepare':permitted and (new_product_suggestion_id(job,i) not in previous
                    or previous[new_product_suggestion_id(job,i)]['can_prepare'])}
                for i,item in enumerate(job['new_product_suggestions'])]
            if full:
                preview=job.get('preview',{})
                for item in preview.get('lines',[]):
                    item['source_product']={key:item.get(original,'') for key,original in
                        (('product_name','original_name'),('spec','original_spec'),('standard','standard'),
                         ('grade','grade'),('surface','surface'))}
                    item['source_product']['material']=item.get('original_material','')
                    item['source_preview']=item.get('source_quote','')
                    item['customer_line_no']=item.get('line_no','')
                    candidates=[json.loads(record['payload']) for record in db.execute(
                        "SELECT payload FROM records WHERE kind='inventory' AND sku=?",(item.get('product_code',''),))]
                    candidates=[p for p in candidates if p.get('profile_id')==row['profile_id']]
                    if len(candidates)==1:
                        item['catalog_product']=catalog_attributes(candidates[0])
                        item['product_name']=candidates[0].get('product_name','')
                        item['spec']=candidates[0].get('spec','')
        return value

    @staticmethod
    def _write_guard(db,row,body):
        if _flag(_setting(db,'restoration_hold',False)):
            raise SalesAssistantError('恢复后请先核对业务记录','restoration_hold')
        if row['version']!=_version(body):
            raise SalesAssistantError('草稿已变化，保留输入并刷新后核对','sales_draft_version_changed')
        if sales_manual_import.active(db,row['id']):
            raise SalesAssistantError('本单已生成手动导入表格，请先核对人工导入进度；尚未导入可取消后继续',
                                      'sales_manual_import_pending')
        if (row['status'] in {'submitting','submitted','submission_unknown','submission_blocked'} or
                row['submission_operation_id'] or row['erp_order_no'] or row['submitted_at'] or
                _has_sales_execution_reference(db,row['id'])):
            raise SalesAssistantError('订单已提交或结果待核对，不能改写已确认版本','sales_draft_locked')

    @staticmethod
    def _replay(db,actor,key,fingerprint):
        row=db.execute('SELECT request_hash,result FROM sales_assistant_operations WHERE actor_id=? AND idempotency_key=?',(actor,key)).fetchone()
        if not row: return None
        if row['request_hash']!=fingerprint:
            raise SalesAssistantError('操作标识已用于其他请求','idempotency_conflict')
        return {**json.loads(row['result']),'replayed':True}

    def _finish(self,db,user,identity,action,key,fingerprint,extra=None):
        row=self._session(db,user,identity)
        result={'replayed':False,'session':self._view(db,row,user=user),**(extra or {})}
        actor=_text(user['id']);now=timestamp()
        db.execute('INSERT INTO sales_assistant_operations VALUES (?,?,?,?,?,?,?)',
            (actor,key,fingerprint,identity,action,dumps(result),now))
        db.execute('INSERT INTO sales_assistant_events VALUES (?,?,?,?,?,?,?)',
            (uuid.uuid4().hex,identity,actor,action,row['version'],
             dumps({'draft_hash':_digest(json.loads(row['draft'])),'status':row['status']}),now))
        return result

    def create(self,user,body):
        key=_key(body);profile=_text(body.get('profile_id'));customer=_text(body.get('customer_name'))
        if not profile or not customer or len(customer)>200: raise ValueError('请选择账套及固定客户')
        fingerprint=_digest({'action':'create','body':body})
        with self.store.connect(write=True) as db:
            actor,_=self._scope(db,user,profile,customer,require_owner=True)
            replay=self._replay(db,actor,key,fingerprint)
            if replay: return replay
            # The desktop picker binds the real ERP identity. Old clients keep
            # their existing fixed-customer contract and API preflight checks.
            if body.get('erp_customer_id'):
                from .sales_customer_directory import ensure as ensure_directory
                ensure_directory(db)
                matches=db.execute('''SELECT erp_id FROM sales_erp_customers
                    WHERE profile_id=? AND customer_name=? AND active=1''',(profile,customer)).fetchall()
                if len(matches)!=1 or matches[0][0]!=str(body['erp_customer_id']):
                    raise SalesAssistantError('ERP 客户身份已变化或存在同名单位，请刷新核对','customer_identity_changed')
            if _flag(_setting(db,'restoration_hold',False)):
                raise SalesAssistantError('恢复后请先核对业务记录','restoration_hold')
            identity=uuid.uuid4().hex;now=timestamp()
            handler=_text(body.get('handler_name'))
            if handler and handler not in {'李瑄','刘锋'}:raise ValueError('请选择有效经手人')
            draft={'customer_name':customer,'handler_name':handler,'customer_order_no':'','delivery_date':'',
                   'note':'','tax_basis':'tax_inclusive','tax_rate':VAT_PERCENT,
                   'lines':[],'issues':[{'code':'lines_missing','message':'请填写商品明细'}]}
            db.execute('''INSERT INTO sales_assistant_sessions
                (id,profile_id,customer_name,created_by,version,status,draft,created_at,updated_at)
                VALUES (?,?,?,?,1,'collecting',?,?,?)''',
                (identity,profile,customer,actor,dumps(draft),now,now))
            db.execute('UPDATE sales_assistant_sessions SET erp_customer_id=? WHERE id=?',
                       (str(body.get('erp_customer_id') or ''),identity))
            return self._finish(db,user,identity,'create',key,fingerprint)

    def customers(self,user,profile_id,query='',page=1):
        """Offer exact confirmed customer names without inferring ownership."""
        profile_id=_text(profile_id); query=_text(query)
        if not profile_id or len(profile_id)>100 or len(query)>200:
            raise ValueError('请选择账套，客户关键词不得超过200字')
        if isinstance(page,bool) or not isinstance(page,int) or not 1<=page<=100000:
            raise ValueError('页码必须为正整数')
        with self.store.connect() as db:
            actor,admin=self._actor(db,user)
            clause=""" FROM sales_customer_owners o JOIN staff_users u ON u.id=o.owner_id
                WHERE o.profile_id=? AND o.status='assigned'
                AND o.source IN ('customer_confirmed','phone_rule_migrated') AND u.active=1
                AND EXISTS(SELECT 1 FROM json_each(u.roles) WHERE value='sales')
                AND (?=1 OR o.owner_id=?) AND instr(o.customer_name,?)>0"""
            params=(profile_id,int(admin),actor,query)
            total=db.execute('SELECT count(*)'+clause,params).fetchone()[0]
            rows=db.execute("SELECT o.customer_name,o.owner_id,u.display_name AS owner_name"+
                            clause+" ORDER BY o.customer_name LIMIT 50 OFFSET ?",
                            (*params,(page-1)*50)).fetchall()
            return {'items':[dict(row) for row in rows], 'total':total,'page':page,
                    'has_more':page*50<total}

    def list(self,user,page=1):
        if isinstance(page,bool) or not isinstance(page,int) or page<1: raise ValueError('页码必须为正整数')
        with self.store.read_snapshot(),self.store.connect() as db:
            actor,admin=self._actor(db,user)
            rows=db.execute("SELECT * FROM sales_assistant_sessions WHERE archived_at='' ORDER BY updated_at DESC,id DESC").fetchall()
            if not admin: rows=[row for row in rows if fixed_sales_owner(db,row['profile_id'],row['customer_name'])==actor]
            start=(page-1)*30
            return {'items':[self._view(db,row,full=False,user=user) for row in rows[start:start+30]],
                    'total':len(rows),'page':page,'has_more':start+30<len(rows),'submit_available':False}

    def detail(self,user,identity):
        with self.store.read_snapshot(),self.store.connect() as db:
            return {'session':self._view(db,self._session(db,user,identity),user=user)}

    def _draft(self,db,row,body,actor_id='',identity_change_code='sales_line_identity_changed',
               trusted_source_rows=None):
        if 'customer_name' in body and _text(body['customer_name'])!=row['customer_name']:
            raise SalesAssistantError('客户不可在草稿中改写，请为该客户新建会话','customer_scope_immutable')
        raw=body.get('lines')
        if not isinstance(raw,list) or len(raw)>MAX_LINES: raise ValueError('明细必须为最多 500 行的列表')
        note=_text(body.get('note'))
        if len(note)>2000: raise ValueError('备注最多 2000 字')
        po=_text(body.get('customer_order_no'))
        if len(po)>200: raise ValueError('客户订单号最多 200 字')
        issues=[];lines=[]
        if '\r' in note or '\n' in note:
            issues.append({'code':'note_web_import_invalid','message':'网页导入附加说明不能包含换行'})
        try: validate_sales_header_limits(note,'')
        except WebImportBlocked:
            issues.append({'code':'note_web_import_length','message':'附加说明超过网页已验证的 180 字限制或含无效字符，请精简后确认'})
        if raw and not po:
            issues.append({'code':'customer_order_no_missing','message':'请填写客户订单号后确认'})
        elif po and any(char in po for char in '#\r\n'):
            issues.append({'code':'customer_order_no_invalid','message':'客户订单号不能包含 # 或换行'})
        previous_draft=json.loads(row['draft'])
        handler=_text(body.get('handler_name',previous_draft.get('handler_name','')))
        if handler and handler not in {'李瑄','刘锋'}:
            raise ValueError('经手人只能选择李瑄或刘锋')
        previous=previous_draft.get('lines',[])
        previous_reports=previous_draft.get('source_rows',[])
        if not isinstance(previous_reports,list): previous_reports=[]
        previous_reports=list(previous_reports)
        # Older uploads only persisted a transient issue. Rebuild missing
        # coverage evidence from attachment facts; editing is not acceptance.
        covered={entry.get('source_id') for entry in previous_reports if isinstance(entry,dict)}
        covered.update(entry.get('source_id') for entry in (trusted_source_rows or [])
                       if isinstance(entry,dict))
        for attachment in db.execute('SELECT * FROM sales_assistant_attachments WHERE session_id=?',
                                     (row['id'],)):
            if attachment['id'] not in covered:
                previous_reports.append(self._attachment_coverage(attachment,
                    'original_attachment_not_reviewed'))
        previously_excluded={entry.get('id') for entry in previous_reports
                             if isinstance(entry,dict) and entry.get('status')=='excluded_by_user'}
        if not isinstance(previous,list): previous=[]
        previous_by_id={item['id']:item for item in previous if isinstance(item,dict)
                        and isinstance(item.get('id'),str) and item['id']}
        if len(previous_by_id)!=sum(isinstance(item,dict) and bool(_text(item.get('id'))) for item in previous):
            raise SalesAssistantError('原草稿明细行身份重复，请人工核对','sales_line_identity_changed',422)
        # Legacy Android edits omit line IDs.  Ordinal fallback is safe for
        # ordinary lines, but not when duplicate SKUs carry different hidden
        # human-confirmed summary fields.
        if any(not isinstance(item,dict) or not _text(item.get('id'))
               for index,item in enumerate(raw) if index<len(previous)):
            if any(isinstance(old_line,dict) and old_line.get('source_ref') for old_line in previous):
                raise SalesAssistantError('来源明细须使用带行身份的客户端核对',
                                          'sales_line_identity_ambiguous',422)
            by_code={}
            for old_line in previous:
                if isinstance(old_line,dict):
                    by_code.setdefault(_text(old_line.get('product_code')),set()).add(
                        dumps(old_line.get('manual_summary',{})))
            if any(code and len(metadata)>1 for code,metadata in by_code.items()):
                raise SalesAssistantError('重复商品行有不同人工摘要，请使用含行身份的客户端核对',
                                          'sales_line_identity_ambiguous',422)
        try: default_basis=quote_basis(body.get('tax_basis'))
        except ValueError:
            default_basis=''
            issues.append({'code':'tax_basis_invalid','message':'报价须注明含税或未税'})
        if not is_fixed_vat_rate(body.get('tax_rate')):
            issues.append({'code':'tax_rate_invalid','message':'销售税率固定为 13%，请核对报价'})
        try: delivery=_day(body.get('delivery_date'))
        except ValueError:
            delivery=_text(body.get('delivery_date'));issues.append({'code':'delivery_date_invalid','message':'整单交期需为 YYYY-MM-DD'})
        order_day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        seen_line_ids=set()
        for index,item in enumerate(raw,1):
            if not isinstance(item,dict): raise ValueError('明细必须为对象')
            offered_ref=item.get('source_ref')
            if isinstance(offered_ref,dict) and offered_ref.get('id') in previously_excluded:
                raise SalesAssistantError('已排除来源行不得重新关联明细',
                                          'sales_source_row_excluded',422)
            line={key:_text(item.get(key)) for key in ('product_code','quantity','unit_price','unit','warehouse','delivery_date','sku_id')}
            supplied_id=_text(item.get('id'))
            prior=previous_by_id.get(supplied_id) if supplied_id else (
                previous[index-1] if index<=len(previous) and isinstance(previous[index-1],dict)
                and _text(previous[index-1].get('product_code'))==line['product_code'] else None)
            if supplied_id and prior is None and item.get('new_line') is not True:
                raise SalesAssistantError('明细行身份与原草稿不一致，请刷新后核对',
                                          'sales_line_identity_changed',422)
            if supplied_id and prior is not None and item.get('new_line') is True:
                raise SalesAssistantError('已有明细不能作为新行提交，请刷新后核对',
                                          'sales_line_identity_changed',422)
            if not supplied_id and index<=len(previous) and isinstance(previous[index-1],dict) \
                    and _text(previous[index-1].get('product_code'))!=line['product_code']:
                raise SalesAssistantError('旧客户端不能按位置更换商品编码，请使用带行身份的客户端',
                                          'sales_line_identity_changed',422)
            replacing=bool(prior and _text(prior.get('product_code'))!=line['product_code'])
            if replacing and item.get('replace_product_code') is not True:
                raise SalesAssistantError('更换商品编码须明确确认，请刷新后核对',
                                          'sales_line_identity_changed',422)
            line['id']=(_text(prior.get('id')) if prior else '') or uuid.uuid4().hex
            if line['id'] in seen_line_ids:
                raise SalesAssistantError('明细行身份重复，请核对每一行',
                                          'sales_line_identity_changed',422)
            seen_line_ids.add(line['id'])
            source_ref=prior.get('source_ref') if isinstance(prior,dict) else None
            if source_ref is None and trusted_source_rows is not None:
                source_ref=item.get('source_ref')
            if isinstance(source_ref,dict) and source_ref.get('id'):
                line['source_ref']=dict(source_ref)
                if replacing:
                    line['source_ref']['match_basis']='manual_code'
            if replacing:
                # The source and line identities survive, but old product
                # identity and human summary choices do not cross SKUs.
                prior=None
            if isinstance(item.get('identity'),dict): line['identity']={key:_text(item['identity'].get(key)) for key in IDENTITY}
            problems=[]
            def issue(code,message):
                problems.append(code);issues.append({'code':code,'line':index,'message':message})
            try: line['quantity']=_decimal_text(item.get('quantity'),positive=True)
            except (ValueError,InvalidOperation): issue('quantity_invalid','数量须为正数')
            try: line['unit_price']=_decimal_text(item.get('unit_price'))
            except (ValueError,InvalidOperation): issue('unit_price_missing_or_invalid','请确认销售单价，不自动猜价')
            line['quoted_unit_price']=line['unit_price']
            try: line['tax_basis']=quote_basis(item.get('tax_basis',default_basis))
            except ValueError:
                line['tax_basis']=''
                issue('tax_basis_invalid','报价须注明含税或未税')
            line['tax_rate']=VAT_PERCENT
            if not is_fixed_vat_rate(item.get('tax_rate')):
                issue('tax_rate_invalid','销售税率固定为 13%，请核对报价')
            if 'quantity_invalid' not in problems and 'unit_price_missing_or_invalid' not in problems and line['tax_basis']:
                try:
                    line['untaxed_unit_price'],line['untaxed_line_amount']=quote_price(
                        line['quantity'],line['unit_price'],line['tax_basis'])
                except (ValueError,InvalidOperation):
                    issue('line_amount_invalid','未税金额超出范围，请核对数量和单价')
            try: line['delivery_date']=_day(item.get('delivery_date'))
            except ValueError: issue('line_delivery_date_invalid','行交期需为 YYYY-MM-DD')
            effective_delivery=line['delivery_date'] or delivery
            if not effective_delivery:
                issue('delivery_missing','请填写整单交期或本行交期')
            elif 'line_delivery_date_invalid' not in problems and not any(
                    entry['code']=='delivery_date_invalid' for entry in issues):
                if effective_delivery < order_day:
                    issue('delivery_before_order_date','交期早于当前录单日期，请更新交期')
            code=line['product_code']
            candidates=[]
            if code:
                matches=db.execute("SELECT id,payload FROM records WHERE kind='inventory' AND sku=?",(code,)).fetchall()
                for record in matches:
                    product=json.loads(record['payload'])
                    if not isinstance(product,dict) or _text(product.get('profile_id'))!=row['profile_id']: continue
                    identity={key:_text(product.get(key)) for key in IDENTITY}
                    if not all(identity.values()): continue
                    supplied=line.get('identity')
                    if supplied and supplied!=identity: continue
                    if line['sku_id'] and line['sku_id']!=identity['sku_id']: continue
                    if line['warehouse'] and line['warehouse'] not in {identity['warehouse_id'],_text(product.get('warehouse_name'))}: continue
                    candidates.append((record['id'],product,identity))
            if len(candidates)!=1:
                issue('product_identity_review','商品编码或 SKU／仓库／单位身份不唯一，请核对')
            else:
                record_id,product,identity=candidates[0]
                if (_flag(product.get('is_deleted',False)) or _flag(product.get('is_stopped',False))
                    or _text(product.get('status')).lower() in {'停用','删除','已删除','disabled','deleted','stopped'}):
                    issue('product_inactive','商品已停用或删除，请核对当前有效编码')
                if prior and prior.get('resolved_identity') and prior.get('resolved_identity')!=identity:
                    if supplied_id:
                        raise SalesAssistantError('明细商品身份已变化，请重新核对行身份',
                                                  identity_change_code,422)
                    prior=None
                    line['id']=uuid.uuid4().hex
                raw_unit_names={key:_text(product.get(key)) for key in ('unit_name','base_unit_name','unit')}
                named_units={value for value in raw_unit_names.values() if value}
                evidence=unit_evidence(named_units) if identity['base_unit']=='1' else None
                if evidence:
                    unit=CONFIRMED_BASE_UNIT
                    line['unit_evidence']=evidence
                elif identity['base_unit']!='1':
                    unit=''
                    issue('unit_base_review','商品基本单位 ID 不是已确认的 1，请核对')
                else:
                    unit=''
                    issue('unit_name_review','商品计量单位名称待核对')
                if line['unit'] and line['unit'] not in {unit,identity['base_unit']}:
                    issue('unit_mismatch','填写的单位与 ERP 基本单位不符')
                line.update(resolved_identity=identity,record_id=record_id,
                    product_name=_text(product.get('product_name')),erp_unit_names=raw_unit_names)
                derived=catalog_attributes(product)
                catalog={key:_text(derived.get(key)) for key in
                         ('spec','model','grade','surface','standard')}
                if source_ref:
                    review=inspect_source_match(source_ref,
                        {**catalog,'product_name':line['product_name'],'material':derived.get('material','')},identity)
                    confirmation=prior.get('source_match_confirmation') if isinstance(prior,dict) else None
                    if item.get('source_match_confirmed') is True:
                        if item.get('source_match_evidence_hash')!=review['revision']:
                            issue('source_match_evidence_changed','原文或目录已变化，请重新核对商品对应')
                        elif not source_confirmation_valid(confirmation,review):
                            confirmation=make_source_confirmation(review,actor_id,
                                datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'))
                    accepted=source_confirmation_valid(confirmation,review)
                    line['source_review']={'required':review['requires_confirmation'],
                        'reasons':review['reasons'],'confirmed':accepted,
                        'evidence_hash':review['revision'],'original':{
                            'product_name':review['source']['original_name'],
                            'spec':review['source']['original_spec'],
                            'grade':review['source']['original_grade'],
                            'surface':review['source']['original_surface'],
                            'material':review['source'].get('original_material',''),
                            'standard':review['source']['original_standard']},
                        'catalog':review['catalog']}
                    if accepted:
                        line['source_match_confirmation']=confirmation
                    if review['requires_confirmation'] and not accepted:
                        issue('source_product_match_unconfirmed','原文与商品资料有差异，请核对后明确使用此商品编码')
                existing_manual=prior.get('manual_summary',{}) if isinstance(prior,dict) else {}
                if not isinstance(existing_manual,dict): existing_manual={}
                manual=dict(existing_manual)
                fields=('project_no','drawing_no','spec','grade','surface')
                changed_manual=False
                for field in fields:
                    if field not in item: continue
                    supplied=_text(item.get(field))
                    if field in {'spec','grade','surface'} and catalog[field] and supplied not in {'',catalog[field]}:
                        issue('summary_catalog_conflict','人工摘要与商品资料不一致，请核对')
                    elif (field not in {'spec','grade','surface'} or supplied != catalog[field]
                          or (prior is None and supplied and item.get('summary_metadata_confirmed') is True)):
                        if supplied != _text(manual.get(field)):
                            if item.get('summary_metadata_confirmed') is not True:
                                issue('summary_metadata_unconfirmed','项目号、图号等摘要资料须人工确认')
                            else:
                                manual[field]=supplied;changed_manual=True
                line['manual_summary']={key:value for key,value in manual.items() if key in fields and _text(value)}
                line['summary_metadata_confirmed']=bool(line['manual_summary'])
                line['summary_metadata_confirmed_by']=(
                    actor_id if changed_manual else _text(prior.get('summary_metadata_confirmed_by')) if prior else '')
                line['summary_metadata_confirmed_at']=(
                    timestamp() if changed_manual else _text(prior.get('summary_metadata_confirmed_at')) if prior else '')
                line['catalog_summary']=catalog
                line['project_no']=_text(manual.get('project_no'))
                line['drawing_no']=_text(manual.get('drawing_no'))
                line['spec']=_text(manual.get('spec')) or catalog['spec']
                line['model']=catalog['model']
                line['grade']=_text(manual.get('grade')) or catalog['grade']
                line['surface']=_text(manual.get('surface')) or catalog['surface']
                shape=catalog_spec_dimension(catalog['spec'])
                prior_human=isinstance(prior,dict) and prior.get('summary_dimension_source')=='human_confirmed'
                catalog_changed=isinstance(prior,dict) and prior.get('catalog_summary')!=catalog
                # Stored draft fields are replayed during confirmation. A new
                # human override is an explicit input without a claimed source;
                # actor, time and source are always assigned by this server.
                explicit_dimension=('summary_dimension' in item and
                    item.get('summary_dimension_source') not in
                    {'catalog_spec_shape','human_confirmed','human_pending'})
                entered=_text(item.get('summary_dimension')) if explicit_dimension else ''
                if explicit_dimension and (not entered or len(entered)>200 or
                                           any(char in entered for char in '#\r\n')):
                    line['summary_dimension']=entered
                    line['summary_dimension_source']='human_pending'
                    issue('summary_size_invalid','人工尺寸须为1至200字且不能包含 # 或换行')
                elif explicit_dimension and item.get('summary_metadata_confirmed') is True:
                    line['summary_dimension']=entered
                    line['summary_dimension_source']='human_confirmed'
                    line['summary_metadata_actor']=actor_id
                    line['summary_metadata_at']=timestamp()
                elif explicit_dimension:
                    line['summary_dimension']=entered
                    line['summary_dimension_source']='human_pending'
                    issue('summary_metadata_unconfirmed','人工尺寸须明确核对后确认')
                elif prior_human:
                    line['summary_dimension']=_text(prior.get('summary_dimension'))
                    line['summary_dimension_source']='human_confirmed'
                    line['summary_metadata_actor']=_text(prior.get('summary_metadata_actor'))
                    line['summary_metadata_at']=_text(prior.get('summary_metadata_at'))
                    if catalog_changed:
                        issue('summary_catalog_changed','商品资料变化，人工尺寸须重新核对确认')
                elif isinstance(prior,dict) and prior.get('summary_dimension_source')=='human_pending':
                    line['summary_dimension']=_text(prior.get('summary_dimension'))
                    line['summary_dimension_source']='human_pending'
                    issue('summary_metadata_unconfirmed','人工尺寸须明确核对后确认')
                elif shape and line['spec']==catalog['spec']:
                    line['summary_dimension']=shape
                    line['summary_dimension_source']='catalog_spec_shape'
                elif source_ref and _text(source_ref.get('original_spec')):
                    # The adopted customer specification is summary evidence,
                    # even for nuts/washers without a catalog M径×长度 shape.
                    line['summary_dimension']=_text(source_ref.get('original_spec'))
                    line['summary_dimension_source']='source_ref'
                else:
                    line['summary_dimension']=''
                    line['summary_dimension_source']=''
                    issue('summary_size_unverified','ERP 规格未提供可核对的 M径×长度尺寸，请人工核对')
                if line['summary_dimension_source']=='human_confirmed':
                    line['summary_metadata_confirmed']=True
                    if not line.get('summary_metadata_actor') or not line.get('summary_metadata_at'):
                        issue('summary_metadata_unconfirmed','人工尺寸确认记录不完整，请重新核对')
                    else:
                        line['summary_metadata_confirmed_by']=line['summary_metadata_actor']
                        line['summary_metadata_confirmed_at']=line['summary_metadata_at']
                else:
                    line['summary_metadata_actor']=(actor_id if changed_manual else
                        _text(prior.get('summary_metadata_actor')) if prior else '')
                    line['summary_metadata_at']=(timestamp() if changed_manual else
                        _text(prior.get('summary_metadata_at')) if prior else '')
                line['summary_dimension_catalog_conflict']=(
                    line['summary_dimension_source']=='human_confirmed' and bool(shape) and
                    line['summary_dimension']!=shape)
                line['unit']=unit if line['unit'] in {'',identity['base_unit']} else line['unit']
                if line['warehouse'] and line['warehouse'] == _text(product.get('warehouse_name')):
                    line['warehouse_name'] = line['warehouse']
                line['warehouse']=identity['warehouse_id']
                # Do not trust source_summary supplied by a client. source_ref
                # can enter only through the server parser / Codex adoption;
                # persisted match review governs its use with this ERP code.
                line['source_summary']=build_source_summary(line,catalog,source_ref)
                if source_ref:
                    line['source_summary']['source_revision']=review['revision']
                summary_fields=(code,line['product_name'],line['project_no'],line['drawing_no'],
                                line['spec'],line['grade'],line['surface'],
                                *(line['source_summary'][key] for key in
                                  ('project_no','customer_line_no','customer_part_no','name','spec','grade','surface')))
                if not line['product_name'] or any(
                        char in value for value in summary_fields for char in '#\r\n'):
                    issue('summary_field_invalid','原始项目号、料号或摘要含 #／换行，当前十段格式不能无损导入；输入已保留，请核对')
            line['issues']=problems;lines.append(line)
        if not lines: issues.append({'code':'lines_missing','message':'请填写商品明细'})
        warehouses={line['resolved_identity']['warehouse_id'] for line in lines
                    if isinstance(line.get('resolved_identity'),dict)}
        if len(warehouses)>1:
            issues.append({'code':'warehouse_split_required',
                           'message':'明细属于不同仓库，当前网页导入只支持单一仓库；请拆单后确认'})
        source_rows=[dict(entry) for entry in previous_reports if isinstance(entry,dict)]
        existing_reports={entry.get('id'):entry for entry in source_rows}
        actions=body.get('source_row_actions',[])
        if not isinstance(actions,list) or len(actions)>MAX_LINES:
            raise ValueError('来源行操作必须为最多 500 项的列表')
        excluded={}
        for action in actions:
            if (not isinstance(action,dict) or set(action)!={'source_row_id','action','reason'}
                    or action.get('action')!='exclude'):
                raise ValueError('来源行操作只支持明确排除及原因')
            source_id=action.get('source_row_id')
            reason=action.get('reason')
            if (not actor_id or not isinstance(source_id,str) or not 1<=len(source_id)<=200
                    or not isinstance(reason,str) or not 1<=len(reason.strip())<=200
                    or len(reason)>200
                    or any(char in reason for char in '\r\n')):
                raise ValueError('排除来源行须提供 1 至 200 字原因')
            report=existing_reports.get(source_id)
            if (source_id in excluded or report is None or report.get('kind') not in {'text','spreadsheet','document'}
                    or report.get('exclude_allowed') is False
                    or report.get('status')=='excluded_by_user'):
                raise SalesAssistantError('来源行不存在、已排除或重复操作，请刷新后核对',
                                          'sales_source_row_action_invalid',422)
            excluded[source_id]=reason.strip()
        if trusted_source_rows is not None:
            source_rows.extend(dict(entry) for entry in trusted_source_rows)
        reports_by_id={entry.get('id'):entry for entry in source_rows}
        if len(reports_by_id)!=len(source_rows):
            raise SalesAssistantError('来源行身份重复，请人工核对','sales_source_row_duplicate',422)
        lines_by_source={}
        for line in lines:
            source_ref=line.get('source_ref')
            if not source_ref: continue
            source_id=source_ref.get('id')
            if source_id in lines_by_source or source_id not in reports_by_id:
                raise SalesAssistantError('来源行与草稿明细不一致，请人工核对',
                                          'sales_source_row_mismatch',422)
            lines_by_source[source_id]=line
        for report in source_rows:
            source_id=report.get('id')
            if source_id in excluded:
                if source_id in lines_by_source:
                    raise SalesAssistantError('排除来源行时须在同次编辑明确移除对应明细',
                                              'sales_source_row_still_linked',422)
                report.update(status='excluded_by_user',blocking=False,
                              original_reason=report.get('reason',''),
                              original_preview=report.get('preview',''),
                              exclusion_reason=excluded[source_id],
                              excluded_by=actor_id,excluded_at=timestamp())
                continue
            if report.get('status')=='excluded_by_user':
                if source_id in lines_by_source:
                    raise SalesAssistantError('已排除来源行不得重新关联明细',
                                              'sales_source_row_excluded',422)
                continue
            if report.get('status')=='ignored_with_reason': continue
            linked=lines_by_source.get(report.get('id'))
            if linked is None:
                if report.get('line_id'):
                    raise SalesAssistantError('原来源明细不能直接删除，请先核对来源行',
                                              'sales_source_line_removed',422)
                report['status']='needs_review';report['blocking']=True
                issues.append({'code':'source_row_needs_review','source_id':report.get('id'),
                               'message':'来源行未解析为明细，请核对原件'})
                continue
            report['line_id']=linked['id']
            report['blocking']=False
            report['status']='needs_review' if linked['issues'] else 'parsed_for_review'
            if linked.get('source_ref',{}).get('match_basis')=='manual_code':
                report['reason']='manual_code_reviewed'
            if linked.get('source_ref',{}).get('unit_blank'):
                report['unit_note']=('来源单位空白；商品基本单位 1 按用户规则归一为支，请核对'
                                     if linked.get('resolved_identity',{}).get('base_unit')=='1'
                                     else '来源单位空白；商品基本单位尚待核对')
        return {'customer_name':row['customer_name'],'customer_order_no':po,'handler_name':handler,
                'delivery_date':delivery,'note':note,'tax_basis':default_basis,
                'tax_rate':VAT_PERCENT,'lines':lines,'source_rows':source_rows,'issues':issues}

    @staticmethod
    def _replace(db,identity,draft):
        db.execute('''UPDATE sales_assistant_sessions SET draft=?,version=version+1,
            status=?,confirmed_version=0,confirmed_hash='',confirmed_payload='{}',updated_at=? WHERE id=?''',
            (dumps(draft),'draft' if draft['lines'] else 'collecting',timestamp(),identity))

    def edit(self,user,identity,body):
        key=_key(body);fingerprint=_digest({'action':'edit','id':identity,'body':body})
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay: return replay
            self._write_guard(db,row,body)
            # Older clients only round-trip `unit_price`. An untaxed Excel
            # quote must never silently become inclusive when edited there.
            old=json.loads(row['draft'])
            incoming=body.get('lines')
            if any(line.get('tax_basis')=='tax_exclusive' for line in old.get('lines',[])) \
                    and 'tax_basis' not in body and isinstance(incoming,list) \
                    and any(not isinstance(line,dict) or 'tax_basis' not in line for line in incoming):
                raise SalesAssistantError('草稿含未税报价，请使用支持税价口径的版本编辑',
                                          'sales_tax_basis_required')
            self._replace(db,identity,self._draft(db,row,body,actor_id=user['id']))
            return self._finish(db,user,identity,'edit',key,fingerprint)

    def message(self,user,identity,body):
        key=_key(body);text=body.get('text')
        if not isinstance(text,str) or not text.strip() or len(text)>10000:
            raise ValueError('消息需为 1 至 10000 字')
        fingerprint=_digest({'action':'message','id':identity,'body':body})
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay: return replay
            self._write_guard(db,row,body)
            duplicate=db.execute('''SELECT id FROM sales_assistant_messages
                WHERE session_id=? AND role='user' AND text=? AND attachment_id=''
                ORDER BY rowid LIMIT 1''',(identity,text)).fetchone()
            if duplicate:
                return self._finish(db,user,identity,'message',key,fingerprint,
                                    {'duplicate_of':duplicate['id']})
            message_id=uuid.uuid4().hex
            parsed,reports=parse_text(text,message_id,sha256(text.encode()).hexdigest())
            draft=json.loads(row['draft'])
            draft=self._draft(db,row,{**draft,'lines':[*draft['lines'],*parsed]},
                              actor_id=user['id'],trusted_source_rows=reports)
            db.execute('INSERT INTO sales_assistant_messages VALUES (?,?,?,?,?,?)',
                (message_id,identity,'user',text,'',timestamp()))
            self._replace(db,identity,draft)
            return self._finish(db,user,identity,'message',key,fingerprint)

    def request_codex(self,user,identity,body):
        key=_key(body);fingerprint=_digest({'action':'request_codex','id':identity,'body':body})
        text=body.get('text','')
        if not isinstance(text,str) or len(text)>10000 or '\x00' in text:
            raise ValueError('消息最多 10000 字')
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay:return replay
            self._write_guard(db,row,body)
            if not codex_enabled(db):
                raise SalesAssistantError('N150 Codex 录单尚未就绪，原资料已保留','sales_codex_unavailable',503)
            # Current server catalog is frozen below; unrelated stock updates
            # do not invalidate the user's request or discard their message.
            if text.strip():
                db.execute('INSERT INTO sales_assistant_messages VALUES (?,?,?,?,?,?)',
                           (uuid.uuid4().hex,identity,'user',text.strip(),'',timestamp()))
            db.execute('''UPDATE sales_assistant_sessions SET version=version+1,
                status=CASE WHEN status='confirmed' THEN 'draft' ELSE status END,
                confirmed_version=0,confirmed_hash='',confirmed_payload='{}',updated_at=? WHERE id=?''',
                (timestamp(),identity))
            current=self._session(db,user,identity,write=True)
            try:
                job_id=codex_enqueue(db,codex_freeze(db,current,user['id'],body))
            except CodexJobError as error:
                raise SalesAssistantError(str(error),error.code,409) from None
            return self._finish(db,user,identity,'request_codex',key,fingerprint,{'job_id':job_id})

    def prepare_new_product(self,user,identity,body):
        key=_key(body);fingerprint=_digest({'action':'prepare_new_product','id':identity,'body':body})
        reference=body.get('reference_code','')
        if not isinstance(reference,str) or len(reference)>100 or '\x00' in reference:
            raise ValueError('参考商品编码格式无效')
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay:return replay
            self._write_guard(db,row,body)
            job=db.execute('SELECT * FROM sales_codex_jobs WHERE id=? AND session_id=?',
                           (body.get('job_id'),identity)).fetchone()
            if (not job or job['status']!='needs_clarification' or not self.codex._current(db,job,row)
                or body.get('result_hash')!=job['result_hash'] or not codex_enabled(db)):
                raise SalesAssistantError('新商品建议或基础数据已变化，请重新整理','sales_new_product_suggestion_changed')
            try:
                plan_id=new_product_prepare(db,row,job,body.get('suggestion_id'),user['id'],reference.strip().upper())
            except CodexJobError as error:raise SalesAssistantError(str(error),error.code) from None
            return self._finish(db,user,identity,'prepare_new_product',key,fingerprint,{'plan_id':plan_id})

    def confirm_new_product(self,user,identity,plan_id,body):
        key=_key(body);fingerprint=_digest({'action':'confirm_new_product','id':identity,'plan_id':plan_id,'body':body})
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            actor,admin=self._actor(db,user)
            if not admin:raise SalesAssistantError('新建商品编码须由管理员确认','role_forbidden',403)
            replay=self._replay(db,actor,key,fingerprint)
            if replay:return replay
            self._write_guard(db,row,body)
            plan=db.execute('SELECT * FROM sales_new_product_plans WHERE id=? AND session_id=?',(plan_id,identity)).fetchone()
            if not plan:raise KeyError('新建编码计划不存在')
            if not json.loads(plan['input_json']).get('catalog_fingerprint') and body.get('data_version')!=plan['data_version']:
                raise SalesAssistantError('基础数据已变化，请重新核对新建编码方案','sales_new_product_data_changed')
            try:new_product_confirm(db,row,plan,actor,body.get('plan_hash'))
            except CodexJobError as error:raise SalesAssistantError(str(error),error.code) from None
            return self._finish(db,user,identity,'confirm_new_product',key,fingerprint,{'plan_id':plan_id})

    def adopt_codex(self,user,identity,job_id,body):
        key=_key(body);fingerprint=_digest({'action':'adopt_codex','id':identity,'job_id':job_id,'body':body})
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay:return replay
            self._write_guard(db,row,body)
            job=db.execute('SELECT * FROM sales_codex_jobs WHERE id=? AND session_id=?',(job_id,identity)).fetchone()
            if not job:raise KeyError('整理任务不存在')
            if body.get('result_hash')!=job['result_hash'] or not re.fullmatch(r'[0-9a-f]{64}',str(body.get('result_hash',''))):
                raise SalesAssistantError('Codex 整理结果已变化，请刷新核对','sales_codex_result_changed')
            if job['adopted_version'] is not None:
                return self._finish(db,user,identity,'adopt_codex',key,fingerprint,{'already_adopted':True})
            if not codex_enabled(db) or job['status']!='ready_for_review' or not self.codex._current(db,job,row):
                raise SalesAssistantError('整理结果尚有追问或已过期，请补齐后重新整理','sales_codex_not_ready',422)
            # _current verifies actual catalog dependencies, not stock revision.
            proposed=json.loads(job['result_json']);snapshot=json.loads(job['input_json'])
            provenance=json.loads(job['source_provenance'])
            source_hashes={s['source_id']:s['sha256'] for s in codex_sources(snapshot)}
            old=json.loads(row['draft'])
            old_reports=[dict(report) for report in old.get('source_rows',[])]
            reports_by_id={r['id']:r for r in old_reports}
            # Preserve raw evidence and explicit exclusions. Known physical
            # spreadsheet rows were all checked by the worker before ready.
            for report in old_reports:
                if report.get('status') not in {'excluded_by_user','ignored_with_reason'}:
                    report.setdefault('original_reason',report.get('reason',''))
                    report.setdefault('original_status',report.get('status',''))
                    report.update(status='ignored_with_reason',blocking=False,line_id='',
                                  reason='codex_draft_explicitly_adopted',codex_job_id=job_id)
            trusted=[];lines=[]
            for ordinal,item in enumerate(proposed['draft']['lines'],1):
                refs=item['source_ids'];source=refs[0]
                physical=provenance.get(source) or reports_by_id.get(source)
                source_id=physical.get('source_id') if physical else source
                source_hash=physical.get('sha256') if physical else source_hashes.get(source_id)
                if not source_hash:
                    raise SalesAssistantError('整理明细来源无法对应，请重新整理','sales_codex_source_mismatch',422)
                rid=f'codex:{job_id}:{ordinal}'
                ref={'id':rid,'kind':'text','source_id':source_id,'sha256':source_hash,
                     'row':ordinal,'sheet':'Codex整理','preview':item['source_quote'],
                     'original_name':item['original_name'],'original_spec':item['original_spec'],
                     'original_standard':item['standard'],'original_grade':item['grade'],
                     'original_material':item['original_material'],
                     'original_surface':item['surface'],'customer_part_no':item['customer_part_no'],
                     'project_no':item['project_no'],'customer_line_no':item['line_no'],
                     'customer_order_no':proposed['draft']['customer_order_no'],
                     'match_basis':'codex_proposed','codex_job_id':job_id,'original_source_ids':refs,
                     'unit_blank':not item['unit']}
                trusted.append({'id':rid,'kind':'text','source_id':source_id,'sha256':source_hash,
                    'row':ordinal,'sheet':'Codex整理','preview':item['source_quote'],
                    'status':'parsed_for_review','reason':'codex_explicitly_adopted',
                    'blocking':False,'line_id':'','codex_job_id':job_id,'original_source_ids':refs})
                lines.append({'product_code':item['product_code'],'quantity':item['quantity'],
                    'unit_price':item['unit_price'],'unit':item['unit'],'delivery_date':item['delivery_date'],
                    'tax_basis':proposed['draft']['tax_basis'],'tax_rate':13,'source_ref':ref,
                    'project_no':item['project_no'],'drawing_no':item['drawing_no'],
                    'summary_metadata_confirmed':True,'new_line':True})
            mutable=dict(row)
            # Full reviewed replacement, not append: duplicate SKU rows remain
            # distinct, while previous source/draft evidence is archived below.
            mutable['draft']=dumps({**old,'lines':[],'source_rows':old_reports})
            draft=self._draft(db,mutable,{**proposed['draft'],'lines':lines},
                              actor_id=user['id'],trusted_source_rows=trusted)
            # This explicit human action accepts the visible source/code
            # correspondence, including NAS aliases, as a revision-bound
            # fact. It does not confirm or submit the sales order itself.
            reviewed=[]
            for item in draft['lines']:
                item={**item,'source_match_confirmed':True,
                      'source_match_evidence_hash':item.get('source_review',{}).get('evidence_hash','')}
                reviewed.append(item)
            mutable['draft']=dumps(draft)
            draft=self._draft(db,mutable,{**draft,'lines':reviewed},actor_id=user['id'])
            self._replace(db,identity,draft)
            db.execute("UPDATE sales_codex_jobs SET status='adopted',step='adopted',adopted_version=?,updated_at=? WHERE id=?",
                       (row['version']+1,timestamp(),job_id))
            db.execute('INSERT INTO sales_assistant_events VALUES (?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,identity,user['id'],'codex_adoption_original_draft',row['version'],
                 dumps({'job_id':job_id,'result_hash':job['result_hash'],'original_draft':old}),timestamp()))
            return self._finish(db,user,identity,'adopt_codex',key,fingerprint,{'job_id':job_id})

    @staticmethod
    def _mime(name,raw):
        suffix=Path(name).suffix.lower()
        if suffix=='.pdf' and raw.startswith(b'%PDF-'): return 'application/pdf'
        if suffix in {'.jpg','.jpeg'} and raw.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
        if suffix=='.png' and raw.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
        if suffix=='.webp' and raw[:4]==b'RIFF' and raw[8:12]==b'WEBP': return 'image/webp'
        if suffix=='.xls' and raw.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
            return 'application/vnd.ms-excel'
        if suffix=='.xlsx':
            try:
                with ZipFile(BytesIO(raw)) as archive:
                    names=set(archive.namelist())
                    if {'[Content_Types].xml','xl/workbook.xml'}<=names and len(names)<=256 and sum(item.file_size for item in archive.infolist())<=40*1024*1024:
                        return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            except BadZipFile: pass
        raise ValueError('附件仅支持有效的 PNG/JPEG/WebP、PDF、XLS 或 XLSX 文件')

    @staticmethod
    def _excel(raw, customer, attachment_id, digest):
        return parse_excel(raw, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                           attachment_id, digest, customer)

    @staticmethod
    def _legacy_excel(raw, customer, attachment_id, digest):
        return parse_excel(raw, 'application/vnd.ms-excel', attachment_id, digest, customer)

    @staticmethod
    def _attachment_coverage(attachment,reason):
        document=attachment['mime_type'] in DOCUMENT_TYPES
        return {'id':'attachment:'+attachment['id']+':coverage',
                'kind':'document' if document else 'spreadsheet',
                'scope':'attachment_coverage','source_id':attachment['id'],
                'sha256':attachment['sha256'],'sheet':attachment['name'],
                'sheet_index':0,'row':0,'preview':'附件整体尚未核对：'+attachment['name'],
                'status':'needs_review','reason':reason,'blocking':True,'line_id':'',
                'exclude_allowed':not document}

    def attachment(self,user,identity,body,file):
        key=_key(body)
        if not file or not file.filename: raise ValueError('请选择附件')
        name=Path(file.filename.replace('\\','/')).name
        if not name or len(name)>160 or any(ord(char)<32 for char in name): raise ValueError('附件名称无效')
        raw=file.stream.read(MAX_ATTACHMENT+1)
        if not raw or len(raw)>MAX_ATTACHMENT:
            raise SalesAssistantError('附件需小于或等于 10MiB','attachment_size_invalid',413)
        mime=self._mime(name,raw);digest=sha256(raw).hexdigest()
        fingerprint=_digest({'action':'attachment','id':identity,'body':body,'name':name,'mime':mime,'sha256':digest})
        blob=None;owned=False
        try:
            with self.store.connect(write=True) as db:
                row=self._session(db,user,identity,write=True)
                replay=self._replay(db,user['id'],key,fingerprint)
                if replay: return replay
                self._write_guard(db,row,body)
                duplicate=db.execute('''SELECT id FROM sales_assistant_attachments
                    WHERE session_id=? AND sha256=? ORDER BY rowid LIMIT 1''',
                    (identity,digest)).fetchone()
                if duplicate:
                    return self._finish(db,user,identity,'attachment',key,fingerprint,
                                        {'duplicate_of':duplicate['id']})
                if db.execute('SELECT count(*) FROM sales_assistant_attachments WHERE session_id=?',(identity,)).fetchone()[0]>=20:
                    raise ValueError('每个会话最多 20 个附件')
                from .storage_capacity import capacity
                if capacity(self.store.runtime)['status'] == 'critical':
                    raise SalesAssistantError('服务器存储空间不足，已有资料已保留，请稍后上传', 'storage_capacity_low', 503)
                attachment_id=uuid.uuid4().hex;blob_name=uuid.uuid4().hex+'.bin';blob=self.blobs/blob_name
                if self.blobs.is_symlink() or not blob.resolve().is_relative_to(self.store.runtime.resolve()):
                    raise ValueError('附件目录无效')
                fd=os.open(blob,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
                owned=True
                with os.fdopen(fd,'wb') as stream:
                    stream.write(raw);stream.flush();os.fsync(stream.fileno())
                if hasattr(os,'O_DIRECTORY'):
                    directory_fd=os.open(self.blobs,os.O_RDONLY|os.O_DIRECTORY)
                    try: os.fsync(directory_fd)
                    finally: os.close(directory_fd)
                status='needs_processing';draft=json.loads(row['draft'])
                if mime.endswith('spreadsheetml.sheet') or mime=='application/vnd.ms-excel':
                    try:
                        parsed,reports=(self._legacy_excel(raw,row['customer_name'],attachment_id,digest)
                                        if mime=='application/vnd.ms-excel' else
                                        self._excel(raw,row['customer_name'],attachment_id,digest))
                        draft=self._draft(db,row,{**draft,'lines':[*draft['lines'],*parsed]},
                                          actor_id=user['id'],trusted_source_rows=reports)
                        status=('needs_review' if any(entry['blocking']
                                 for entry in draft['source_rows'] if entry['source_id']==attachment_id)
                                else 'parsed_for_review')
                    except Exception as error:
                        status='needs_review'
                        message=str(error)[:200] if isinstance(error,ValueError) else 'Excel 无法自动读取，请人工核对文件及明细'
                        coverage=self._attachment_coverage({'id':attachment_id,'mime_type':mime,
                            'sha256':digest,'name':name},'spreadsheet_parse_failed')
                        coverage['preview']=name+'：'+message
                        draft=self._draft(db,row,draft,actor_id=user['id'],trusted_source_rows=[coverage])
                        draft['issues']=[*draft['issues'],{'code':'spreadsheet_needs_review','message':message}]
                else:
                    coverage=self._attachment_coverage({'id':attachment_id,'mime_type':mime,
                        'sha256':digest,'name':name},'document_waiting_for_adoption')
                    draft=self._draft(db,row,draft,actor_id=user['id'],trusted_source_rows=[coverage])
                db.execute('INSERT INTO sales_assistant_attachments VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (attachment_id,identity,name,mime,digest,len(raw),blob_name,status,user['id'],timestamp()))
                if mime in DOCUMENT_TYPES:
                    enqueue_document(db,attachment_id)
                db.execute('INSERT INTO sales_assistant_messages VALUES (?,?,?,?,?,?)',
                    (uuid.uuid4().hex,identity,'user','上传附件：'+name,attachment_id,timestamp()))
                self._replace(db,identity,draft)
                return self._finish(db,user,identity,'attachment',key,fingerprint)
        except BaseException:
            if owned: blob.unlink(missing_ok=True)
            raise

    def adopt_document(self,user,identity,attachment_id,body):
        key=_key(body);fingerprint=_digest({'action':'adopt_document','id':identity,
                                          'attachment_id':attachment_id,'body':body})
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay: return replay
            self._write_guard(db,row,body)
            result=self.documents.result(db,identity,attachment_id)
            if (not isinstance(body.get('data_version'),str) or not body['data_version']
                    or body['data_version']!=_setting(db,'data_version','')):
                raise SalesAssistantError('基础数据已更新，保留输入并刷新后核对',
                                          'sales_document_data_version_changed',409)
            if (not isinstance(body.get('result_hash'),str)
                    or not re.fullmatch(r'[0-9a-f]{64}',body['result_hash'])
                    or result['result_hash']!=body['result_hash']):
                raise SalesAssistantError('附件提取结果已变化，请刷新后核对',
                                          'sales_document_result_changed',409)
            if result['adopted_version'] is not None:
                return self._finish(db,user,identity,'adopt_document',key,fingerprint,
                                    {'already_adopted':True})
            if result['status']!='ready' or result['unready_pages']:
                raise SalesAssistantError('附件提取覆盖未通过，不能加入草稿；请提供完整可读资料',
                                          'sales_document_not_ready',422)
            projection=result.get('projection')
            if (not isinstance(projection,dict) or not isinstance(projection.get('lines'),list)
                    or not isinstance(projection.get('source_rows'),list)):
                raise SalesAssistantError('附件提取结果暂不可用，请刷新核对','sales_document_not_ready',422)
            old=json.loads(row['draft'])
            # Replacing this exact server-created placeholder is the only way
            # document coverage can be cleared. The client cannot exclude it.
            old['source_rows']=[entry for entry in old.get('source_rows',[])
                               if not (entry.get('source_id')==attachment_id
                                   and entry.get('scope')=='attachment_coverage')]
            mutable=dict(row);mutable['draft']=dumps(old)
            draft=self._draft(db,mutable,{**old,'lines':[*old['lines'],*projection['lines']]},
                              actor_id=user['id'],trusted_source_rows=projection['source_rows'])
            self._replace(db,identity,draft)
            self.documents.mark_adopted(db,identity,attachment_id,body['result_hash'],row['version']+1)
            db.execute("UPDATE sales_assistant_attachments SET status='parsed_for_review' WHERE id=? AND session_id=?",
                       (attachment_id,identity))
            return self._finish(db,user,identity,'adopt_document',key,fingerprint)

    def document_result(self,user,identity,attachment_id):
        with self.store.read_snapshot(),self.store.connect() as db:
            self._session(db,user,identity)
            return {'document':self.documents.result(db,identity,attachment_id)}

    def attachment_file(self,user,identity,attachment_id):
        with self.store.read_snapshot(),self.store.connect() as db:
            self._session(db,user,identity)
            row=db.execute('SELECT * FROM sales_assistant_attachments WHERE id=? AND session_id=?',(attachment_id,identity)).fetchone()
            if not row: raise KeyError('附件不存在')
            path=self.blobs/row['blob_name']
            if not re.fullmatch(r'[0-9a-f]{32}\.bin',row['blob_name']) or path.is_symlink() or not path.resolve().is_relative_to(self.store.runtime.resolve()):
                raise SalesAssistantError('附件存储待核对','attachment_blob_unavailable',503)
            try: raw=path.read_bytes()
            except OSError: raise SalesAssistantError('附件暂不可用','attachment_blob_unavailable',503) from None
            if len(raw)!=row['size'] or sha256(raw).hexdigest()!=row['sha256']:
                raise SalesAssistantError('附件完整性校验失败','attachment_blob_unavailable',503)
            return raw,row['name'],row['mime_type']

    def confirm(self,user,identity,body):
        key=_key(body);fingerprint=_digest({'action':'confirm','id':identity,'body':body})
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay: return replay
            self._write_guard(db,row,body)
            self._codex_confirmation_guard(db,row)
            old=json.loads(row['draft']);draft=self._draft(db,row,old,actor_id=user['id'],
                identity_change_code='sales_catalog_changed')
            if old['issues'] or draft['issues']:
                raise SalesAssistantError('草稿还有待核对项，请补齐明细、单价及身份后确认','sales_draft_needs_review',422)
            # Re-resolve each line, then require the exact previously reviewed
            # identity; a changed catalog cannot silently substitute another SKU.
            if [(line.get('resolved_identity'),line.get('unit_evidence'),line.get('erp_unit_names'),
                 line.get('catalog_summary'))
                    for line in old['lines']] != [(line.get('resolved_identity'),line.get('unit_evidence'),line.get('erp_unit_names'),
                                                   line.get('catalog_summary'))
                    for line in draft['lines']]:
                raise SalesAssistantError('商品身份已变化，请刷新草稿后重新确认','sales_catalog_changed')
            payload={'profile_id':row['profile_id'],'customer_name':row['customer_name'],
                     'fixed_sales_owner_id':fixed_sales_owner(db,row['profile_id'],row['customer_name']),
                     'data_version':_setting(db,'data_version',''),'draft':draft,'version':row['version']+1}
            db.execute('''UPDATE sales_assistant_sessions SET draft=?,status='confirmed',version=version+1,
                confirmed_version=version+1,confirmed_hash=?,confirmed_payload=?,updated_at=? WHERE id=?''',
                (dumps(draft),_digest(payload),dumps(payload),timestamp(),identity))
            return self._finish(db,user,identity,'confirm',key,fingerprint)

    def submit(self,user,identity,body):
        key=_key(body);fingerprint=_digest({'action':'submit','id':identity,'body':body})
        with self.store.connect(write=True) as db:
            row=self._session(db,user,identity,write=True)
            replay=self._replay(db,user['id'],key,fingerprint)
            if replay: return replay
            self._write_guard(db,row,body)
            self._codex_confirmation_guard(db,row)
            if row['status']!='confirmed' or row['confirmed_version']!=row['version']:
                raise SalesAssistantError('请先确认当前草稿版本','sales_confirmation_required')
            frozen_draft=json.loads(row['draft'])
            current_draft=self._draft(db,row,frozen_draft,actor_id=user['id'],
                                      identity_change_code='sales_catalog_changed')
            if frozen_draft.get('issues') or current_draft['issues']:
                raise SalesAssistantError('确认稿的来源或商品资料仍需核对，请编辑后重新确认',
                                          'sales_draft_needs_review',422)
            # A historical confirmation cannot bypass new source coverage
            # checks or silently acknowledge a changed catalog.
            for old_line,new_line in zip(frozen_draft['lines'],current_draft['lines']):
                for field in ('resolved_identity','catalog_summary','erp_unit_names','unit_evidence'):
                    if old_line.get(field)!=new_line.get(field):
                        raise SalesAssistantError('商品资料已变化，请编辑后重新确认',
                                                  'sales_catalog_changed',409)
            owner=fixed_sales_owner(db,row['profile_id'],row['customer_name'])
            try:
                if body.get('channel')=='api':
                    operation_id,number=sales_api_jobs.enqueue(db,row,user['id'])
                else:
                    number,payload=prepare_sales_web_payload(db,row,owner)
                    operation_id=enqueue_sales_web_operation(db,actor_id=user['id'],
                        profile_id=row['profile_id'],number=number,key=key,payload=payload)
            except (SalesWebQueueBlocked,sales_api_jobs.SalesAPIError) as error:
                status=503 if error.code=='sales_submission_unavailable' else 409
                raise SalesAssistantError(str(error),error.code,status) from None
            changed=db.execute("""UPDATE sales_assistant_sessions SET status='submitting',
                submission_operation_id=?,updated_at=? WHERE id=? AND version=? AND status='confirmed'""",
                (operation_id,timestamp(),identity,row['version']))
            if changed.rowcount!=1:
                raise SalesAssistantError('确认稿状态已变化','sales_draft_version_changed')
            return self._finish(db,user,identity,'submit',key,fingerprint,
                {'operation_id':operation_id,'erp_order_no':number})

    @staticmethod
    def _codex_confirmation_guard(db,row):
        job=codex_latest(db,row,full=False)
        if job and job['base_version']==row['version'] and job['status']!='adopted':
            raise SalesAssistantError('请先处理 Codex 追问并采用核对后的草稿',
                                      'sales_codex_adoption_required',422)

    @staticmethod
    def _web_operation_scope(db,operation_id):
        operation=db.execute('SELECT * FROM web_operations WHERE id=?',(operation_id,)).fetchone()
        frozen=db.execute('SELECT payload_json FROM web_operation_payloads WHERE operation_id=?',
                          (operation_id,)).fetchone()
        if not operation or operation['kind']!='sales_order_import' or not frozen:
            raise SalesAssistantError('销售网页操作不存在','sales_web_operation_missing')
        try: payload=json.loads(frozen['payload_json'])
        except (TypeError,ValueError):
            raise SalesAssistantError('销售网页冻结参数损坏','sales_web_operation_corrupt') from None
        session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',
                           (payload.get('session_id'),)).fetchone()
        snapshot=payload.get('frozen_session')
        if (payload.get('schema') not in {'sales_web_import_v1','sales_web_import_v2'} or not session or
                not isinstance(snapshot,dict) or snapshot.get('status')!='confirmed' or
                session['submission_operation_id']!=operation_id or
                operation['profile_id']!=session['profile_id'] or
                operation['target_id']!=payload.get('order_number') or
                payload.get('profile_id')!=session['profile_id'] or
                payload.get('confirmed_version')!=session['confirmed_version'] or
                payload.get('confirmed_hash')!=session['confirmed_hash'] or
                sha256(session['confirmed_payload'].encode()).hexdigest()!=session['confirmed_hash'] or
                snapshot.get('id')!=session['id'] or snapshot.get('profile_id')!=session['profile_id'] or
                snapshot.get('confirmed_payload')!=session['confirmed_payload'] or
                snapshot.get('draft')!=session['draft']):
            raise SalesAssistantError('销售网页操作与冻结确认稿不一致','sales_web_operation_corrupt')
        return operation,payload,session

    @staticmethod
    def _verified_web_outcome(operation,payload,outcome):
        try: intent_id=str(uuid.UUID(operation['id']))
        except (TypeError,ValueError,AttributeError): return False
        documents=payload.get('document_numbers',[])
        document_binding=(payload.get('schema')!='sales_web_import_v2' or
            (payload.get('document_mode')=='line_document' and isinstance(documents,list) and
             len(documents)==payload['line_count'] and len(set(documents))==len(documents) and
             documents and documents[0]==operation['target_id'] and
             isinstance(outcome,WebImportOutcome) and tuple(documents)==outcome.document_numbers and
             type(outcome.document_count) is int and outcome.document_count==len(documents)))
        return (document_binding and isinstance(outcome,WebImportOutcome) and outcome.status=='imported' and
                outcome.code=='web_import_confirmed' and outcome.attempted is True and
                outcome.progress_observed is True and
                outcome.completion_basis=='web_import_confirmation' and
                outcome.erp_readback_performed is False and outcome.unit_verified is False and
                outcome.table_verified is True and
                isinstance(outcome.file_sha256,str) and
                re.fullmatch(r'[a-f0-9]{64}',outcome.file_sha256) is not None and
                type(outcome.imported_rows) is int and type(outcome.expected_rows) is int and
                outcome.imported_rows==outcome.expected_rows==payload['line_count'] and
                outcome.intent_id==intent_id and
                outcome.order_number==operation['target_id'] and
                payload['line_count']==len(json.loads(payload['frozen_session']['draft'])['lines']))

    @staticmethod
    def _record_web_outcome(db,operation,payload,session,status,code,outcome=None):
        stamp=web_now();number=operation['target_id']
        stage={'succeeded':'网页已确认导入','uncertain':'导入结果待核对','blocked':'提交受阻'}[status]
        detail={'succeeded':'表格已校验，网页确认全部明细导入完成；未执行 ERP 回查',
                'uncertain':'网页未确认完整导入；保留原单号和上传记录，不重复上传',
                'blocked':'上传前检查未通过；原操作保留，不自动重试'}[status]
        succeeded=status=='succeeded'
        result={'verified':False,'web_import_confirmed':succeeded,
                'reference':number if succeeded else '',
                'code':code,'confirmed_version':payload['confirmed_version'],
                'confirmed_hash':payload['confirmed_hash'],'line_count':payload['line_count'],
                'unit_verified':False,'unit_basis':'',
                'completion_basis':'web_import_confirmation' if succeeded else '',
                'erp_readback_performed':False,
                'table_verified':outcome.table_verified is True if isinstance(outcome,WebImportOutcome) else False,
                'file_sha256':outcome.file_sha256 if isinstance(outcome,WebImportOutcome) else '',
                'imported_rows':outcome.imported_rows if isinstance(outcome,WebImportOutcome) else 0,
                'expected_rows':payload['line_count']}
        if payload.get('schema')=='sales_web_import_v2':
            result.update(document_mode='line_document',document_numbers=list(payload['document_numbers']),
                          document_count=len(payload['document_numbers']),reference_scope='first_line_document')
        db.execute('''UPDATE web_operations SET status=?,stage=?,detail=?,result_json=?,
            updated_at=?,finished_at=? WHERE id=?''',
            (status,stage,detail,dumps(result),stamp,stamp if status in {'succeeded','blocked'} else '',operation['id']))
        WebOperations._event(db,operation['id'],status,stage,detail)
        session_status={'succeeded':'submitted','uncertain':'submission_unknown',
                        'blocked':'submission_blocked'}[status]
        changed=db.execute('''UPDATE sales_assistant_sessions SET status=?,erp_order_no=?,submitted_at=?,
            updated_at=? WHERE id=?''',
            (session_status,number if status=='succeeded' else '',
             timestamp() if status=='succeeded' else '',timestamp(),session['id']))
        if changed.rowcount!=1:
            raise SalesAssistantError('录单会话终结失败','sales_web_operation_corrupt')
        return result

    def finish_web_import(self,operation_id,worker_token,*,outcome=None,blocked_code=None):
        """Atomically end a worker-owned attempt and lock the sales session.

        Success binds table validation and a complete website receipt to the
        frozen operation. An unknown result never schedules another upload.
        """
        with self.store.connect(write=True) as db:
            operation,payload,session=self._web_operation_scope(db,operation_id)
            if operation['status']!='running' or operation['worker_token']!=worker_token:
                raise SalesAssistantError('网页执行权已变化','sales_web_operation_not_owned')
            if session['status']!='submitting':
                raise SalesAssistantError('录单提交状态已变化','sales_web_operation_corrupt')
            if blocked_code is not None:
                if not isinstance(blocked_code,str) or not blocked_code:
                    raise ValueError('受阻原因无效')
                status,code='blocked',blocked_code
            elif self._verified_web_outcome(operation,payload,outcome):
                status,code='succeeded','web_import_confirmed'
            elif isinstance(outcome,WebImportOutcome):
                status,code='uncertain',outcome.code if outcome.status=='unknown' else 'web_import_receipt_incomplete'
            else:
                raise ValueError('缺少网页执行结果')
            result=self._record_web_outcome(db,operation,payload,session,status,code,outcome)
            return {'status':status,'result':result,'operation_id':operation_id}

    def reconcile_web_import(self,operation_id,outcome):
        """Close a held unknown result from the original durable local receipt.

        The caller only recovers the original receipt, without browser or ERP
        queries. This never requeues an uncertain operation or permits upload.
        """
        with self.store.connect(write=True) as db:
            operation,payload,session=self._web_operation_scope(db,operation_id)
            if operation['status']!='uncertain' or session['status']!='submission_unknown':
                raise SalesAssistantError('当前操作不在导入结果待核对状态','sales_web_reconcile_not_allowed')
            if not self._verified_web_outcome(operation,payload,outcome):
                raise SalesAssistantError('没有与原表格匹配的完整网页导入回执','sales_web_receipt_incomplete')
            result=self._record_web_outcome(db,operation,payload,session,'succeeded','web_import_confirmed',outcome)
            return {'status':'succeeded','result':result,'operation_id':operation_id}


def sales_assistant_routes(engine):
    bp=Blueprint('sales_assistant',__name__)
    base='/api/v2/sales-assistant/sessions'

    @bp.errorhandler(DailyWorkConflict)
    def conflict(error):
        return jsonify(ok=False,error=error.code,message=str(error)),getattr(error,'http_status',409)

    @bp.errorhandler(ValueError)
    def invalid(error): return jsonify(ok=False,error='bad_request',message=str(error)),400

    @bp.errorhandler(KeyError)
    def missing(error): return jsonify(ok=False,error='not_found',message='录单会话或附件不存在'),404

    @bp.get(base)
    def sessions(): return jsonify(ok=True,**engine.list(g.current_user,int(request.args.get('page','1'))))

    @bp.get('/api/v2/sales-assistant/customers')
    def customers(): return jsonify(ok=True,**engine.customers(g.current_user,
        request.args.get('profile_id',''),request.args.get('q',''),int(request.args.get('page','1'))))

    @bp.post(base)
    def create(): return jsonify(ok=True,**engine.create(g.current_user,request.get_json(silent=True)))

    @bp.get(base+'/<identity>')
    def detail(identity): return jsonify(ok=True,**engine.detail(g.current_user,identity))

    @bp.post(base+'/<identity>/messages')
    def message(identity): return jsonify(ok=True,**engine.message(g.current_user,identity,request.get_json(silent=True)))

    @bp.post(base+'/<identity>/codex-jobs')
    def request_codex(identity): return jsonify(ok=True,**engine.request_codex(g.current_user,identity,request.get_json(silent=True)))

    @bp.post(base+'/<identity>/codex-jobs/<job_id>/adopt')
    def adopt_codex(identity,job_id): return jsonify(ok=True,**engine.adopt_codex(g.current_user,identity,job_id,request.get_json(silent=True)))

    @bp.post(base+'/<identity>/new-products/prepare')
    def prepare_new_product(identity): return jsonify(ok=True,**engine.prepare_new_product(g.current_user,identity,request.get_json(silent=True)))

    @bp.post(base+'/<identity>/new-products/<plan_id>/confirm')
    def confirm_new_product(identity,plan_id): return jsonify(ok=True,**engine.confirm_new_product(g.current_user,identity,plan_id,request.get_json(silent=True)))

    @bp.put(base+'/<identity>/draft')
    def edit(identity): return jsonify(ok=True,**engine.edit(g.current_user,identity,request.get_json(silent=True)))

    @bp.post(base+'/<identity>/attachments')
    def attachment(identity):
        return jsonify(ok=True,**engine.attachment(g.current_user,identity,dict(request.form),request.files.get('file')))

    @bp.get(base+'/<identity>/attachments/<attachment_id>')
    def attachment_file(identity,attachment_id):
        raw,name,mime=engine.attachment_file(g.current_user,identity,attachment_id)
        return send_file(BytesIO(raw),mimetype=mime,download_name=name,as_attachment=True)

    @bp.get(base+'/<identity>/attachments/<attachment_id>/processing')
    def document_result(identity,attachment_id):
        return jsonify(ok=True,**engine.document_result(g.current_user,identity,attachment_id))

    @bp.post(base+'/<identity>/attachments/<attachment_id>/adopt')
    def adopt_document(identity,attachment_id):
        return jsonify(ok=True,**engine.adopt_document(g.current_user,identity,attachment_id,
                                                     request.get_json(silent=True)))

    @bp.post(base+'/<identity>/confirm')
    def confirm(identity): return jsonify(ok=True,**engine.confirm(g.current_user,identity,request.get_json(silent=True)))

    @bp.post(base+'/<identity>/submit')
    def submit(identity): return jsonify(ok=True,**engine.submit(g.current_user,identity,request.get_json(silent=True)))

    return bp
