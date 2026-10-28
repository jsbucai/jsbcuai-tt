"""Opt-in sales submission core; no live adapter, HTTP route or scheduler.

The adapter's inspect/lookup methods are read-only. submit_once performs one
write, without internal retries. An intent is committed before that call. Any
existing intent, including one left by power loss *before* the call, permits
only lookup thereafter: a missing order never authorizes another submission.

Official write/readback contracts and durable document/line reference fields
must be independently accepted before enabling this module. A model-generated
draft, successful HTTP response, customer PO or matching total is not proof of
an ERP order. This module never audits, ships, settles or pays an order.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from time import time
from typing import Protocol

from .unit_policy import CONFIRMED_BASE_UNIT, unit_evidence
from .sales_ownership import fixed_sales_owner
from .sales_pricing import VAT_PERCENT, quote_price
from .store import dumps

IDENTITY=('profile_id','warehouse_id','product_id','sku_id','base_unit')


@dataclass(frozen=True)
class SubmissionPolicy:
    enabled: bool = False
    allow_submit: bool = False
    profile_id: str = ''
    account_id: str = ''
    audit_status: str = 'unapproved'
    max_inspection_age_seconds: int = 300


@dataclass(frozen=True)
class AdapterCapabilities:
    profile_id: str = ''
    account_id: str = ''
    official_contract_reference: str = ''
    official_contract_hash: str = ''
    account_verified: bool = False
    write_contract_verified: bool = False
    readback_contract_verified: bool = False
    complete_unique_lookup: bool = False
    durable_document_binding: bool = False
    durable_line_binding: bool = False
    creates_unapproved_order: bool = False


@dataclass(frozen=True)
class SalesLine:
    client_line_id: str
    product_code: str
    product_identity: str
    quantity: str
    unit: str
    unit_evidence: str
    unit_price: str
    tax_basis: str
    tax_rate: str
    delivery_date: str
    quoted_unit_price: str = ''
    untaxed_line_amount: str = ''


@dataclass(frozen=True)
class SubmissionRequest:
    attempt_id: str
    session_id: str
    confirmed_version: int
    confirmed_hash: str
    external_reference: str
    profile_id: str
    account_id: str
    data_version: str
    customer_name: str
    fixed_sales_owner_id: str
    actor_id: str
    sales_employee_id: str
    operator_employee_id: str
    employee_mapping_hash: str
    customer_order_no: str
    delivery_date: str
    note: str
    audit_status: str
    lines: tuple[SalesLine, ...]


@dataclass(frozen=True)
class PreparedSalesOrder:
    request_hash: str
    profile_id: str
    account_id: str
    external_reference: str
    customer_name: str
    customer_erp_id: str
    customer_match_count: int
    customer_evidence_hash: str
    sales_employee_id: str
    operator_employee_id: str
    employee_mapping_hash: str
    employee_evidence_hash: str
    audit_status: str
    audit_status_verified: bool
    lines: tuple[SalesLine, ...]
    line_evidence: tuple[tuple[str, str], ...]
    unit_evidence: tuple[tuple[str, str], ...]
    tax_price_evidence: tuple[tuple[str, str], ...]
    order_total: str
    contract_hash: str
    observed_at: float
    evidence_hash: str


@dataclass(frozen=True)
class SubmissionIntent:
    id: str
    request: SubmissionRequest
    prepared: PreparedSalesOrder


@dataclass(frozen=True)
class ErpSalesOrder:
    erp_order_no: str
    profile_id: str
    account_id: str
    external_reference: str
    customer_name: str
    customer_erp_id: str
    sales_employee_id: str
    operator_employee_id: str
    customer_order_no: str
    delivery_date: str
    note: str
    audit_status: str
    lines: tuple[SalesLine, ...]
    order_total: str
    observed_at: float
    evidence_hash: str


@dataclass(frozen=True)
class SalesOrderLookup:
    complete: bool
    profile_id: str
    account_id: str
    external_reference: str
    orders: tuple[ErpSalesOrder, ...] = ()


class SalesSubmissionAdapter(Protocol):
    capabilities: AdapterCapabilities

    def inspect(self, request: SubmissionRequest) -> PreparedSalesOrder: ...

    def lookup(self, intent: SubmissionIntent) -> SalesOrderLookup: ...

    def submit_once(self, intent: SubmissionIntent) -> None: ...


class SubmissionBlocked(Exception):
    def __init__(self, code):
        super().__init__(code); self.code=code


def _text(value): return '' if value is None else str(value).strip()


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _valid_hash(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def _number(value, positive=False):
    if value is None or isinstance(value,bool) or len(str(value))>80: raise SubmissionBlocked('sales_number_invalid')
    try: result=Decimal(str(value))
    except (InvalidOperation,ValueError): raise SubmissionBlocked('sales_number_invalid') from None
    if not result.is_finite() or result<0 or positive and not result or result.adjusted()>15 or result.as_tuple().exponent < -12:
        raise SubmissionBlocked('sales_number_invalid')
    return result


def _number_text(value, positive=False): return format(_number(value,positive).normalize(),'f')


def _setting(db,key,default=None):
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    if not row: return default
    try: return json.loads(row[0])
    except (TypeError,ValueError): return row[0]


def _restore(value):
    request=dict(value['request']);request['lines']=tuple(SalesLine(**line) for line in request['lines'])
    prepared=dict(value['prepared']);prepared['lines']=tuple(SalesLine(**line) for line in prepared['lines'])
    for key in ('line_evidence','unit_evidence','tax_price_evidence'):
        prepared[key]=tuple(tuple(pair) for pair in prepared[key])
    return SubmissionIntent(value['id'],SubmissionRequest(**request),PreparedSalesOrder(**prepared))


class SalesSubmission:
    def __init__(self,store,adapter: SalesSubmissionAdapter | None=None,policy=None,*,clock=time):
        self.store,self.adapter,self.policy,self.clock=store,adapter,policy or SubmissionPolicy(),clock
        with store.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS sales_submission_staff_mappings(
                profile_id TEXT NOT NULL,staff_id TEXT NOT NULL,erp_employee_id TEXT NOT NULL,
                erp_employee_name TEXT NOT NULL,status TEXT NOT NULL,evidence_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,account_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(profile_id,staff_id))''')
            columns={row['name'] for row in db.execute('PRAGMA table_info(sales_submission_staff_mappings)')}
            if 'account_id' not in columns:
                # Existing approvals remain unbound rather than acquiring an
                # implicit new account. They need explicit approval again.
                db.execute("ALTER TABLE sales_submission_staff_mappings ADD COLUMN account_id TEXT NOT NULL DEFAULT ''")
            db.execute('''CREATE TABLE IF NOT EXISTS sales_submission_attempts(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,confirmed_version INTEGER NOT NULL,
                confirmed_hash TEXT NOT NULL,external_reference TEXT NOT NULL UNIQUE,
                frozen TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',
                erp_order_no TEXT NOT NULL DEFAULT '',receipt TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                UNIQUE(session_id,confirmed_version))''')
            db.execute('''CREATE TABLE IF NOT EXISTS sales_submission_intents(
                id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL,created_at TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS sales_submission_operations(
                actor_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,
                attempt_id TEXT NOT NULL,PRIMARY KEY(actor_id,idempotency_key))''')
            db.execute('''CREATE TABLE IF NOT EXISTS sales_submission_events(
                id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL,actor_id TEXT NOT NULL,
                action TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)''')

    def _now(self): return datetime.fromtimestamp(self.clock(),timezone.utc).isoformat(timespec='seconds')

    def _fresh(self,stamp):
        if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp) or not 0<=self.clock()-stamp<=self.policy.max_inspection_age_seconds:
            raise SubmissionBlocked('sales_inspection_stale')

    @contextmanager
    def _lock(self,session_id):
        root=self.store.runtime/'sales-submission-locks';root.mkdir(mode=0o700,exist_ok=True)
        handles=[]
        try:
            for key in sorted({'session:'+session_id,'account:'+self.policy.profile_id+':'+self.policy.account_id}):
                fd=os.open(root/(_hash(key)+'.lock'),os.O_CREAT|os.O_RDWR,0o600);handles.append(fd)
                try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: raise SubmissionBlocked('sales_submission_busy') from None
            yield
        finally:
            for fd in reversed(handles): os.close(fd)

    @staticmethod
    def _scope(db,user,session_id):
        actor=_text(user.get('id')) if isinstance(user,dict) else ''
        staff=db.execute('SELECT * FROM staff_users WHERE id=?',(actor,)).fetchone()
        if not staff or not staff['active'] or not user.get('active',True) or not staff['is_admin'] and 'sales' not in json.loads(staff['roles']):
            raise SubmissionBlocked('sales_role_forbidden')
        row=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(session_id,)).fetchone()
        if not row: raise SubmissionBlocked('sales_session_not_found')
        owner=fixed_sales_owner(db,row['profile_id'],row['customer_name'])
        if not owner: raise SubmissionBlocked('sales_customer_owner_review')
        if not staff['is_admin'] and owner!=actor: raise SubmissionBlocked('sales_customer_owner_conflict')
        return dict(row),actor,owner

    @staticmethod
    def _mapping(db,profile,staff_id,account_id):
        row=db.execute('SELECT * FROM sales_submission_staff_mappings WHERE profile_id=? AND staff_id=?',(profile,staff_id)).fetchone()
        if not row or row['account_id']!=account_id or row['status']!='approved' or not _text(row['erp_employee_id']) or not _valid_hash(row['evidence_hash']):
            raise SubmissionBlocked('sales_employee_mapping_unverified')
        return dict(row)

    def _capabilities(self,writing=False):
        if writing and (self.policy.enabled is not True or self.policy.allow_submit is not True):
            raise SubmissionBlocked('sales_submission_disabled')
        cap=getattr(self.adapter,'capabilities',None)
        if not isinstance(cap,AdapterCapabilities): raise SubmissionBlocked('sales_adapter_unavailable')
        if not self.policy.profile_id or not self.policy.account_id or cap.profile_id!=self.policy.profile_id or cap.account_id!=self.policy.account_id or cap.account_verified is not True:
            raise SubmissionBlocked('sales_erp_account_unverified')
        if not cap.official_contract_reference or not _valid_hash(cap.official_contract_hash) or cap.readback_contract_verified is not True or cap.complete_unique_lookup is not True or cap.durable_document_binding is not True or cap.durable_line_binding is not True:
            raise SubmissionBlocked('sales_readback_contract_unverified')
        if writing and (cap.write_contract_verified is not True or cap.creates_unapproved_order is not True or self.policy.audit_status!='unapproved'):
            raise SubmissionBlocked('sales_write_contract_unverified')
        return cap

    def _request(self,db,user,row,attempt):
        if _setting(db,'restoration_hold',False) not in (False,0,'','0','false',None): raise SubmissionBlocked('restoration_hold')
        current,actor,owner=self._scope(db,user,row['id'])
        payload=json.loads(current['confirmed_payload']);draft=payload.get('draft',{})
        if current['status']!='confirmed' or current['confirmed_version']!=current['version'] or current['version']!=attempt['confirmed_version']:
            raise SubmissionBlocked('sales_confirmation_required')
        if not _valid_hash(current['confirmed_hash']) or hashlib.sha256(dumps(payload).encode()).hexdigest()!=current['confirmed_hash'] or current['confirmed_hash']!=attempt['confirmed_hash'] or _hash(payload)!=_hash(json.loads(attempt['frozen'])) or draft!=json.loads(current['draft']):
            raise SubmissionBlocked('sales_confirmed_payload_changed')
        if payload.get('profile_id')!=self.policy.profile_id or payload.get('customer_name')!=current['customer_name'] or payload.get('fixed_sales_owner_id')!=owner or payload.get('version')!=current['version']:
            raise SubmissionBlocked('sales_confirmation_scope_changed')
        version=_setting(db,'data_version','')
        if not version or payload.get('data_version')!=version: raise SubmissionBlocked('sales_source_changed')
        mappings=[self._mapping(db,current['profile_id'],who,self.policy.account_id) for who in sorted({actor,owner})]
        by_staff={mapping['staff_id']:mapping for mapping in mappings}
        mapping_hash=_hash(mappings)
        if draft.get('issues') or not isinstance(draft.get('lines'),list) or not 1<=len(draft['lines'])<=500:
            raise SubmissionBlocked('sales_draft_needs_review')
        lines=[]
        for index,line in enumerate(draft['lines'],1):
            ident=line.get('resolved_identity')
            if line.get('issues') or not isinstance(ident,dict) or set(ident)!=set(IDENTITY) or any(not isinstance(ident[key],str) or not ident[key] for key in IDENTITY) or ident['profile_id']!=current['profile_id'] or ident['base_unit']!='1':
                raise SubmissionBlocked('sales_product_identity_unverified')
            products=[]
            for record in db.execute("SELECT payload FROM records WHERE kind='inventory' AND sku=?",(line.get('product_code'),)):
                product=json.loads(record[0])
                if all(_text(product.get(key))==ident[key] for key in IDENTITY): products.append(product)
            if len(products)!=1: raise SubmissionBlocked('sales_catalog_changed')
            raw_names={key:_text(products[0].get(key)) for key in ('unit_name','base_unit_name','unit')}
            evidence=unit_evidence(raw_names.values())
            if evidence is None or line.get('unit')!=CONFIRMED_BASE_UNIT or line.get('warehouse') not in {ident['warehouse_id'],_text(products[0].get('warehouse_name'))}:
                raise SubmissionBlocked('sales_unit_unverified')
            if line.get('unit_evidence')!=evidence or line.get('erp_unit_names')!=raw_names:
                raise SubmissionBlocked('sales_unit_evidence_changed')
            basis=line.get('tax_basis',draft.get('tax_basis'));rate=line.get('tax_rate',draft.get('tax_rate'))
            if basis not in {'tax_inclusive','tax_exclusive'} or rate is None:
                raise SubmissionBlocked('sales_tax_price_unverified')
            tax_rate=_number_text(rate)
            if tax_rate != VAT_PERCENT:
                raise SubmissionBlocked('sales_tax_price_unverified')
            quote=_number_text(line.get('unit_price'))
            try: untaxed,amount=quote_price(line.get('quantity'),quote,basis)
            except (ValueError,InvalidOperation): raise SubmissionBlocked('sales_tax_price_unverified') from None
            if (_number_text(line.get('quoted_unit_price'))!=quote or
                    _number_text(line.get('untaxed_unit_price'))!=_number_text(untaxed) or
                    _number_text(line.get('untaxed_line_amount'))!=_number_text(amount)):
                raise SubmissionBlocked('sales_tax_price_unverified')
            lines.append(SalesLine(str(index),line['product_code'],'|'.join(ident[key] for key in IDENTITY),
                _number_text(line.get('quantity'),True),CONFIRMED_BASE_UNIT,evidence,
                untaxed,basis,tax_rate,_text(line.get('delivery_date')),quote,amount))
        return SubmissionRequest(attempt['id'],current['id'],current['confirmed_version'],current['confirmed_hash'],
            attempt['external_reference'],current['profile_id'],self.policy.account_id,version,current['customer_name'],
            owner,actor,by_staff[owner]['erp_employee_id'],by_staff[actor]['erp_employee_id'],mapping_hash,
            _text(draft.get('customer_order_no')),_text(draft.get('delivery_date')),_text(draft.get('note')),
            self.policy.audit_status,tuple(lines))

    def _prepared(self,request,prepared,cap):
        if not isinstance(prepared,PreparedSalesOrder): raise SubmissionBlocked('sales_inspection_unverified')
        self._fresh(prepared.observed_at)
        if prepared.request_hash!=_hash(asdict(request)) or (prepared.profile_id,prepared.account_id,prepared.external_reference,prepared.customer_name,prepared.sales_employee_id,prepared.operator_employee_id,prepared.employee_mapping_hash,prepared.audit_status)!=(request.profile_id,request.account_id,request.external_reference,request.customer_name,request.sales_employee_id,request.operator_employee_id,request.employee_mapping_hash,request.audit_status):
            raise SubmissionBlocked('sales_inspection_scope_changed')
        if prepared.customer_match_count!=1 or isinstance(prepared.customer_match_count,bool) or not _text(prepared.customer_erp_id) or prepared.audit_status_verified is not True or prepared.contract_hash!=cap.official_contract_hash or not all(_valid_hash(value) for value in (prepared.customer_evidence_hash,prepared.employee_evidence_hash,prepared.evidence_hash)):
            raise SubmissionBlocked('sales_inspection_unverified')
        if prepared.lines!=request.lines: raise SubmissionBlocked('sales_inspection_lines_changed')
        expected={line.client_line_id for line in request.lines}
        for proof in (prepared.line_evidence,prepared.unit_evidence,prepared.tax_price_evidence):
            if len(proof)!=len(expected) or {pair[0] for pair in proof}!=expected or any(len(pair)!=2 or not _valid_hash(pair[1]) for pair in proof):
                raise SubmissionBlocked('sales_line_evidence_unverified')
        total=sum((_number(line.untaxed_line_amount) if line.untaxed_line_amount else
                   _number(line.quantity,True)*_number(line.unit_price) for line in request.lines),Decimal(0))
        if _number(prepared.order_total)!=total: raise SubmissionBlocked('sales_total_unverified')

    def _lookup(self,intent):
        cap=self._capabilities()
        if (intent.request.profile_id,intent.request.account_id)!=(cap.profile_id,cap.account_id):
            raise SubmissionBlocked('sales_recovery_account_changed')
        result=self.adapter.lookup(intent)
        if not isinstance(result,SalesOrderLookup) or result.complete is not True or (result.profile_id,result.account_id,result.external_reference)!=(cap.profile_id,cap.account_id,intent.request.external_reference):
            raise SubmissionBlocked('sales_readback_incomplete')
        if len(result.orders)>1: raise SubmissionBlocked('sales_readback_ambiguous')
        if not result.orders: return None
        order=result.orders[0];request=intent.request;prepared=intent.prepared
        if not isinstance(order,ErpSalesOrder): raise SubmissionBlocked('sales_readback_unverified')
        self._fresh(order.observed_at)
        if not _text(order.erp_order_no) or not _valid_hash(order.evidence_hash) or (order.profile_id,order.account_id,order.external_reference,order.customer_name,order.customer_erp_id,order.sales_employee_id,order.operator_employee_id,order.customer_order_no,order.delivery_date,order.note,order.audit_status)!=(request.profile_id,request.account_id,request.external_reference,request.customer_name,prepared.customer_erp_id,request.sales_employee_id,request.operator_employee_id,request.customer_order_no,request.delivery_date,request.note,request.audit_status):
            raise SubmissionBlocked('sales_readback_mismatch')
        if len(order.lines)!=len(request.lines) or len({line.client_line_id for line in order.lines})!=len(order.lines):
            raise SubmissionBlocked('sales_readback_lines_changed')
        expected={line.client_line_id:line for line in request.lines}
        for line in order.lines:
            original=expected.get(line.client_line_id)
            if not original or (line.product_code,line.product_identity,line.unit,line.unit_evidence,line.tax_basis,line.delivery_date)!=(original.product_code,original.product_identity,original.unit,original.unit_evidence,original.tax_basis,original.delivery_date) or _number(line.quantity,True)!=_number(original.quantity,True) or _number(line.unit_price)!=_number(original.unit_price) or _number(line.tax_rate)!=_number(original.tax_rate) or line.quoted_unit_price!=original.quoted_unit_price or line.untaxed_line_amount!=original.untaxed_line_amount:
                raise SubmissionBlocked('sales_readback_lines_changed')
        if _number(order.order_total)!=_number(prepared.order_total): raise SubmissionBlocked('sales_readback_total_changed')
        return order

    def _event(self,db,attempt,actor,action,payload):
        db.execute('INSERT INTO sales_submission_events VALUES (?,?,?,?,?,?)',
            (uuid.uuid4().hex,attempt['id'],actor,action,dumps(payload),self._now()))

    def _set(self,db,attempt,actor,status,detail='',receipt=None):
        latest=db.execute('SELECT status FROM sales_submission_attempts WHERE id=?',(attempt['id'],)).fetchone()
        if latest and latest['status']=='submitted':
            # A later stale/incomplete lookup cannot revoke a verified receipt
            # or replace its immutable ERP document binding.
            return
        db.execute('''UPDATE sales_submission_attempts SET status=?,detail=?,updated_at=?,
            erp_order_no=CASE WHEN ? IS NULL THEN erp_order_no ELSE ? END,
            receipt=CASE WHEN ? IS NULL THEN receipt ELSE ? END WHERE id=?''',
            (status,detail,self._now(),'yes' if receipt else None,receipt.erp_order_no if receipt else '',
             'yes' if receipt else None,dumps(asdict(receipt)) if receipt else '',attempt['id']))
        if status in {'intent_recorded','submission_unknown','submitted'}:
            session_status='submitting' if status=='intent_recorded' else status
            db.execute('UPDATE sales_assistant_sessions SET status=?,updated_at=? WHERE id=?',
                (session_status,self._now(),attempt['session_id']))
        self._event(db,attempt,actor,status,{'code':detail,'erp_order_no':receipt.erp_order_no if receipt else ''})

    def _result(self,attempt_id,replayed=False):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM sales_submission_attempts WHERE id=?',(attempt_id,)).fetchone()
            return {'attempt_id':row['id'],'session_id':row['session_id'],'confirmed_version':row['confirmed_version'],
                'status':row['status'],'detail':row['detail'],'erp_order_no':row['erp_order_no'],
                'external_reference':row['external_reference'],'replayed':replayed}

    def _recover(self,attempt,actor):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM sales_submission_intents WHERE attempt_id=?',(attempt['id'],)).fetchone()
        if not row: raise SubmissionBlocked('sales_submission_intent_missing')
        if _hash(json.loads(row['payload']))!=row['payload_hash']:
            raise SubmissionBlocked('sales_submission_intent_corrupt')
        intent=_restore(json.loads(row['payload']))
        if (intent.request.attempt_id,intent.request.session_id,intent.request.confirmed_version,
            intent.request.confirmed_hash,intent.request.external_reference)!=(attempt['id'],attempt['session_id'],
            attempt['confirmed_version'],attempt['confirmed_hash'],attempt['external_reference']):
            raise SubmissionBlocked('sales_submission_intent_corrupt')
        try:
            receipt=self._lookup(intent)
            status,detail=('submitted','') if receipt else ('submission_unknown','sales_readback_not_found')
        except SubmissionBlocked as error: receipt=None;status,detail='submission_unknown',error.code
        except Exception: receipt=None;status,detail='submission_unknown','sales_readback_unavailable'
        with self.store.connect(write=True) as db:self._set(db,attempt,actor,status,detail,receipt)
        return self._result(attempt['id'])

    def submit(self,user,session_id,body):
        """Create or recover one frozen-version attempt; at most one write call.

        Blocking validation returns a persisted `blocked` attempt. Authorization,
        input and request-key conflicts raise SubmissionBlocked. Changing keys,
        restarting or toggling policy cannot re-execute an existing intent.
        """
        if not isinstance(body,dict): raise SubmissionBlocked('sales_submission_request_invalid')
        key=_text(body.get('idempotency_key'));version=body.get('version')
        if not key or len(key)>128 or isinstance(version,bool) or not isinstance(version,int) or version<1:
            raise SubmissionBlocked('sales_submission_request_invalid')
        fingerprint=_hash({'session_id':session_id,'body':body});replayed=False
        with self._lock(session_id):
            with self.store.connect(write=True) as db:
                session,actor,owner=self._scope(db,user,session_id)
                operation=db.execute('SELECT * FROM sales_submission_operations WHERE actor_id=? AND idempotency_key=?',(actor,key)).fetchone()
                if operation and operation['request_hash']!=fingerprint: raise SubmissionBlocked('sales_submission_idempotency_conflict')
                if operation:
                    attempt=dict(db.execute('SELECT * FROM sales_submission_attempts WHERE id=?',(operation['attempt_id'],)).fetchone());replayed=True
                else:
                    if session['version']!=version: raise SubmissionBlocked('sales_draft_version_changed')
                    attempt=db.execute('SELECT * FROM sales_submission_attempts WHERE session_id=? AND confirmed_version=?',(session_id,version)).fetchone()
                    if not attempt:
                        if session['status']!='confirmed' or session['confirmed_version']!=version: raise SubmissionBlocked('sales_confirmation_required')
                        previous=db.execute('''SELECT 1 FROM sales_submission_intents i JOIN sales_submission_attempts a ON a.id=i.attempt_id WHERE a.session_id=?''',(session_id,)).fetchone()
                        if previous: raise SubmissionBlocked('sales_draft_locked')
                        identity=uuid.uuid4().hex;now=self._now()
                        db.execute('INSERT INTO sales_submission_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (identity,session_id,version,session['confirmed_hash'],'ULS-'+identity,session['confirmed_payload'],'preflight','','','',now,now))
                        attempt=db.execute('SELECT * FROM sales_submission_attempts WHERE id=?',(identity,)).fetchone()
                    attempt=dict(attempt)
                    db.execute('INSERT INTO sales_submission_operations VALUES (?,?,?,?)',(actor,key,fingerprint,attempt['id']))
                intent=db.execute('SELECT 1 FROM sales_submission_intents WHERE attempt_id=?',(attempt['id'],)).fetchone()
                if attempt['status']=='submitted': return self._result(attempt['id'],replayed=True)
            if intent:
                self._recover(attempt,actor);return self._result(attempt['id'],replayed)
            try:
                cap=self._capabilities(writing=True)
                with self.store.connect() as db:request=self._request(db,user,session,attempt)
                prepared=self.adapter.inspect(request);self._prepared(request,prepared,cap)
                planned=SubmissionIntent(uuid.uuid4().hex,request,prepared)
                existing=self._lookup(planned)
                if existing:
                    # The binding was already found before writing. Preserve it
                    # locally; never issue another order to "fix" a readback.
                    with self.store.connect(write=True) as db:self._set(db,attempt,actor,'submitted',receipt=existing)
                    return self._result(attempt['id'],replayed)
                with self.store.connect(write=True) as db:
                    if self._request(db,user,session,attempt)!=request: raise SubmissionBlocked('sales_preflight_changed')
                    self._capabilities(writing=True);self._fresh(prepared.observed_at)
                    value=asdict(planned)
                    db.execute('INSERT INTO sales_submission_intents VALUES (?,?,?,?,?)',
                        (planned.id,attempt['id'],dumps(value),_hash(value),self._now()))
                    self._set(db,attempt,actor,'intent_recorded')
            except SubmissionBlocked as error:
                with self.store.connect(write=True) as db:self._set(db,attempt,actor,'blocked',error.code)
                return self._result(attempt['id'],replayed)
            except Exception:
                with self.store.connect(write=True) as db:self._set(db,attempt,actor,'blocked','sales_inspection_unavailable')
                return self._result(attempt['id'],replayed)
            try: self.adapter.submit_once(planned)
            except Exception: pass  # Durable intent controls recovery, not HTTP success.
            self._recover(attempt,actor)
            return self._result(attempt['id'],replayed)

    def recover(self,user,attempt_id):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM sales_submission_attempts WHERE id=?',(attempt_id,)).fetchone()
            if not row: raise SubmissionBlocked('sales_submission_attempt_not_found')
            session_id=row['session_id']
        with self._lock(session_id):
            # Another recovery may have finished between the lookup above and
            # lock acquisition. Re-read both status and current permissions.
            with self.store.connect() as db:
                attempt=dict(db.execute('SELECT * FROM sales_submission_attempts WHERE id=?',(attempt_id,)).fetchone())
                _,actor,_=self._scope(db,user,session_id)
            if attempt['status']=='submitted': return self._result(attempt_id,replayed=True)
            return self._recover(attempt,actor)
