"""Durable, administrator-confirmed new product jobs, separate from AI proposals."""
from datetime import datetime,timedelta,timezone
import json
import re
import uuid

from .store import dumps,timestamp
from .daily_work import _setting
from .sales_ownership import fixed_sales_owner
from .sales_codex_pipeline import digest,CodexJobError
from .sales_dependencies import catalog_fingerprint,job_catalog_current


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sales_new_product_plans(
        id TEXT PRIMARY KEY,session_id TEXT NOT NULL,job_id TEXT NOT NULL,
        suggestion_id TEXT NOT NULL,source_result_hash TEXT NOT NULL,
        base_version INTEGER NOT NULL,data_version TEXT NOT NULL,owner_id TEXT NOT NULL,
        requested_by TEXT NOT NULL,confirmed_by TEXT NOT NULL DEFAULT '',
        input_json TEXT NOT NULL,plan_json TEXT NOT NULL DEFAULT '{}',plan_hash TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,worker_token TEXT NOT NULL DEFAULT '',lease_until TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(session_id,job_id,suggestion_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS sales_new_product_queue ON sales_new_product_plans(status,created_at)')


def suggestion_id(job,index):
    return digest({'job_id':job['id'],'result_hash':job['result_hash'],'index':index})[:32]


def enabled(db):
    config=_setting(db,'sales_new_product_policy',{})
    return isinstance(config,dict) and config.get('enabled') is True


def current(db,plan,session):
    return bool(session and session['version']==plan['base_version']
        and job_catalog_current(db,plan,session)
        and fixed_sales_owner(db,session['profile_id'],session['customer_name'])==plan['owner_id']
        and not session['submission_operation_id']
        and session['status'] not in {'submitting','submitted','submission_unknown','submission_blocked'})


def projection(db,session,*,is_admin=False):
    values=[]
    for row in db.execute('SELECT * FROM sales_new_product_plans WHERE session_id=? ORDER BY rowid',
                          (session['id'],)):
        plan=json.loads(row['plan_json']);result=json.loads(row['result_json'])
        status=row['status'];valid=current(db,row,session)
        if not valid and status in {'preparing','ready_for_confirmation','queued'}:status='stale'
        value={key:row[key] for key in ('id','job_id','suggestion_id','source_result_hash','plan_hash','error_code')}
        value.update(status=status,can_confirm=(is_admin and enabled(db) and valid and status=='ready_for_confirmation'),
                     can_prepare=valid and status in {'blocked','failed'},created_code=result.get('created_code',''))
        source=json.loads(row['input_json'])['suggestion']
        value.update({field:plan.get(field,source.get(field,'')) for field in
                      ('product_name','spec','model','code','base_unit','classification_path')})
        value['issues']=plan.get('issues',[])
        value['reference_choices']=plan.get('reference_choices',[])
        values.append(value)
    return values


def request_prepare(db,session,job,selected,actor,reference_code=''):
    proposals=json.loads(job['result_json']).get('new_product_suggestions',[])
    matches=[item for i,item in enumerate(proposals) if suggestion_id(job,i)==selected]
    if len(matches)!=1:raise CodexJobError('sales_new_product_suggestion_changed','新商品建议已变化，请重新核对')
    previous=db.execute('SELECT * FROM sales_new_product_plans WHERE session_id=? AND job_id=? AND suggestion_id=?',
                        (session['id'],job['id'],selected)).fetchone()
    if previous and previous['status'] not in {'blocked','failed'}:
        raise CodexJobError('sales_new_product_plan_exists','该建议已有新建计划，请查看原计划进度')
    identity=previous['id'] if previous else uuid.uuid4().hex;now=timestamp()
    snapshot={'id':identity,'session_id':session['id'],'job_id':job['id'],'suggestion_id':selected,
              'source_result_hash':job['result_hash'],'suggestion':matches[0],
              'profile_id':session['profile_id'],'customer_name':session['customer_name'],
              'owner_id':fixed_sales_owner(db,session['profile_id'],session['customer_name']),
              'base_version':session['version'],'data_version':_setting(db,'data_version',''),
              'reference_code':reference_code,'catalog_fingerprint':catalog_fingerprint(db,session['profile_id'])}
    if previous:
        db.execute("""UPDATE sales_new_product_plans SET input_json=?,plan_json='{}',plan_hash='',
                   status='preparing',worker_token='',lease_until='',error_code='',updated_at=? WHERE id=?""",
                   (dumps(snapshot),now,identity))
    else:
        db.execute('''INSERT INTO sales_new_product_plans
            (id,session_id,job_id,suggestion_id,source_result_hash,base_version,data_version,owner_id,
             requested_by,input_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'preparing',?,?)''',
            (identity,session['id'],job['id'],selected,job['result_hash'],session['version'],snapshot['data_version'],
             snapshot['owner_id'],actor,dumps(snapshot),now,now))
    return identity


def confirm(db,session,plan,actor,plan_hash):
    if not enabled(db):raise CodexJobError('sales_new_product_unavailable','新建编码执行端尚未就绪，方案已保留')
    if plan['status']!='ready_for_confirmation' or not current(db,plan,session):
        raise CodexJobError('sales_new_product_not_ready','计划已变化或已有创建记录，请核对原计划')
    if not re.fullmatch(r'[0-9a-f]{64}',str(plan_hash)) or plan_hash!=plan['plan_hash']:
        raise CodexJobError('sales_new_product_plan_changed','新建编码方案已变化，请重新核对')
    db.execute("UPDATE sales_new_product_plans SET status='queued',confirmed_by=?,updated_at=? WHERE id=?",
               (actor,timestamp(),plan['id']))


class SalesNewProductJobs:
    def __init__(self,store):
        self.store=store
        with store.connect(write=True) as db:ensure_schema(db)

    def claim(self,worker_token,*,now=None,allow_create=True):
        if not isinstance(worker_token,str) or not re.fullmatch(r'[0-9a-f]{32}',worker_token):raise ValueError('invalid token')
        now=now or datetime.now(timezone.utc)
        with self.store.connect(write=True) as db:
            if _setting(db,'restoration_hold',False):return None
            # An interrupted creation may already exist in ERP: it is never
            # returned to the queue. Interrupted preparation is read-only.
            db.execute("""UPDATE sales_new_product_plans SET status=CASE WHEN status='creating' THEN 'unknown' ELSE 'failed' END,
                error_code='sales_new_product_interrupted',worker_token='',lease_until='',updated_at=?
                WHERE worker_token!='' AND lease_until<=?""",(timestamp(),now.isoformat()))
            if db.execute("SELECT 1 FROM sales_new_product_plans WHERE worker_token!='' LIMIT 1").fetchone():return None
            candidates="('preparing','queued')" if allow_create else "('preparing')"
            row=db.execute(f'SELECT * FROM sales_new_product_plans WHERE status IN {candidates} ORDER BY created_at,id LIMIT 1').fetchone()
            if not row:return None
            session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(row['session_id'],)).fetchone()
            if not current(db,row,session):
                db.execute("UPDATE sales_new_product_plans SET status='stale',updated_at=? WHERE id=?",(timestamp(),row['id']))
                return None
            if row['status']=='queued':
                person=db.execute('SELECT active,is_admin FROM staff_users WHERE id=?',(row['confirmed_by'],)).fetchone()
                if not enabled(db) or not person or not person['active'] or not person['is_admin']:return None
            phase='creating' if row['status']=='queued' else 'preparing'
            db.execute('UPDATE sales_new_product_plans SET status=?,worker_token=?,lease_until=?,updated_at=? WHERE id=?',
                       (phase,worker_token,(now+timedelta(minutes=10)).isoformat(),timestamp(),row['id']))
            value=json.loads(row['input_json']);value.update(status=phase,plan=json.loads(row['plan_json']),
                                                           plan_hash=row['plan_hash'],confirmed_by=row['confirmed_by'])
            return value

    @staticmethod
    def _claimed(db,identity,token,expected):
        row=db.execute('SELECT * FROM sales_new_product_plans WHERE id=?',(identity,)).fetchone()
        if not row or row['worker_token']!=token or row['status']!=expected:
            raise CodexJobError('sales_new_product_claim_changed','新建编码任务已变化')
        return row

    def finish_prepare(self,identity,token,plan):
        if not isinstance(plan,dict) or plan.get('status') not in {'ready','blocked'} or len(dumps(plan).encode())>200000:
            raise CodexJobError('sales_new_product_invalid_plan','新建编码方案格式无效')
        with self.store.connect(write=True) as db:
            row=self._claimed(db,identity,token,'preparing')
            session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(row['session_id'],)).fetchone()
            status='ready_for_confirmation' if plan['status']=='ready' else 'blocked'
            if not current(db,row,session):status='stale'
            if status=='ready_for_confirmation':
                if any(json.loads(other['plan_json']).get('code')==plan.get('code') for other in
                       db.execute("SELECT plan_json FROM sales_new_product_plans WHERE id!=? AND status IN ('ready_for_confirmation','queued','creating','unknown','created')",(identity,))):
                    plan={**plan,'status':'blocked','issues':[*plan.get('issues',[]),
                          {'code':'sales_new_product_code_reserved','message':'该编号已被其他新建计划预留，请重新整理'}]}
                    status='blocked'
            frozen={**plan,'source_result_hash':row['source_result_hash'],'suggestion_id':row['suggestion_id'],
                    'session_id':row['session_id'],'base_version':row['base_version'],'data_version':row['data_version']}
            db.execute('''UPDATE sales_new_product_plans SET plan_json=?,plan_hash=?,status=?,
                worker_token='',lease_until='',updated_at=? WHERE id=?''',
                (dumps(frozen),digest(frozen),status,timestamp(),identity))
            return {'id':identity,'status':status}

    def finish_creation(self,identity,token,*,status,result=None,error_code=''):
        if status not in {'created','unknown','blocked'}:raise ValueError('invalid creation result')
        result=result or {}
        with self.store.connect(write=True) as db:
            row=self._claimed(db,identity,token,'creating');plan=json.loads(row['plan_json'])
            if status=='created':
                product=result.get('verified_inventory_row')
                snapshot=json.loads(row['input_json'])
                if (not isinstance(product,dict) or product.get('profile_id')!=snapshot['profile_id']
                    or product.get('sku')!=plan.get('code') or result.get('created_code')!=plan.get('code')
                    or not product.get('product_id') or not product.get('sku_id') or product.get('base_unit')!='1'
                    or product.get('unit_name')!='支' or not product.get('id') or not product.get('warehouse_id')):
                    raise CodexJobError('sales_new_product_identity_unverified','新编码或基本单位尚未得到唯一核验')
                existing=db.execute("SELECT payload FROM records WHERE kind='inventory' AND sku=?",(product['sku'],)).fetchall()
                if any(json.loads(item['payload']).get('product_id')!=product['product_id']
                       or json.loads(item['payload']).get('sku_id')!=product['sku_id'] for item in existing):
                    raise CodexJobError('sales_new_product_identity_conflict','本地编码身份冲突，请核对新商品')
                if not existing:
                    db.execute("INSERT INTO records(kind,id,payload,sku,keywords) VALUES ('inventory',?,?,?,?)",
                               (product['id'],dumps(product),product['sku'],' '.join(str(product.get(k,'')) for k in ('sku','product_name','spec','model'))))
                # Only the new verified catalog row is merged; stock, tasks and
                # the existing directory are never replaced by a create result.
                db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)",(dumps('new-product-'+uuid.uuid4().hex),))
                # This creation resolves this proposal's own missing product;
                # unrelated catalogue additions no longer stale every order.
                db.execute("UPDATE sales_codex_jobs SET status='stale',step='stale',updated_at=? WHERE id=? AND status IN ('needs_clarification','ready_for_review')",
                           (timestamp(),row['job_id']))
            db.execute('''UPDATE sales_new_product_plans SET status=?,result_json=?,error_code=?,
                worker_token='',lease_until='',updated_at=? WHERE id=?''',
                (status,dumps(result),error_code[:100],timestamp(),identity))
            db.execute('INSERT INTO sales_assistant_events VALUES (?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,row['session_id'],row['confirmed_by'],'new_product_'+status,row['base_version'],
                 dumps({'plan_id':identity,'code':plan.get('code',''),'result':result}),timestamp()))
            return {'id':identity,'status':status}

    def authorize_submission(self,identity,token):
        """Final local check after network preflight, immediately before POST."""
        with self.store.connect() as db:
            row=self._claimed(db,identity,token,'creating')
            session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(row['session_id'],)).fetchone()
            person=db.execute('SELECT active,is_admin FROM staff_users WHERE id=?',(row['confirmed_by'],)).fetchone()
            if (not enabled(db) or _setting(db,'restoration_hold',False) or not current(db,row,session)
                or not person or not person['active'] or not person['is_admin']):
                raise CodexJobError('sales_new_product_permission_changed','方案、客户归属或管理员权限已变化，未创建商品')
            return True

    def fail(self,identity,token,code):
        with self.store.connect(write=True) as db:
            row=db.execute('SELECT * FROM sales_new_product_plans WHERE id=?',(identity,)).fetchone()
            if not row or row['worker_token']!=token:raise CodexJobError('sales_new_product_claim_changed','任务已变化')
            status='unknown' if row['status']=='creating' else 'failed'
            db.execute("UPDATE sales_new_product_plans SET status=?,error_code=?,worker_token='',lease_until='',updated_at=? WHERE id=?",
                       (status,code[:100],timestamp(),identity))
