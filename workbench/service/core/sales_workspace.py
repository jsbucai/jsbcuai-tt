"""Same-origin, owner-only desktop workspace over the existing sales engine."""
from hashlib import sha256
import hmac
import json
from pathlib import Path
import time
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from flask import Blueprint, Response, g, jsonify, request, send_file, send_from_directory
from .daily_work import _setting
from .store import dumps,timestamp
from .sales_assistant import SalesAssistantError,_key,_digest,_version,_has_sales_execution_reference
from .daily_work import DailyWorkConflict
from . import sales_api_jobs
from . import sales_manual_import
from io import BytesIO
from . import sales_customer_directory as directory

COOKIE='ul_sales_session'
BASE='/api/v2/sales-workspace'

def cookie_token():
    return request.cookies.get(COOKIE,'')

def csrf(token):
    return sha256(('ul-sales-csrf:'+token).encode()).hexdigest()

def is_sales_request(path):
    return path.startswith('/api/v2/sales-assistant/') or path.startswith(BASE+'/')

def same_origin(app):
    origin=request.headers.get('Origin','')
    configured=app.config.get('SALES_WEB_ORIGIN','https://ul.yingyinghome.com:8443')
    return origin.rstrip('/')==configured.rstrip('/')

def owner_id(db):
    return _setting(db,'sales_workspace_owner_id','admin')


def install_workspace(app,engine,sessions):
    bp=Blueprint('sales_workspace',__name__)
    store=engine.store
    static=Path(__file__).resolve().parents[1]/'static'/'sales'
    streams=BoundedSemaphore(2) # Keep request threads available for App and ERP queries.
    with store.connect(write=True) as db:
        directory.ensure(db)
        db.execute('''CREATE TABLE IF NOT EXISTS sales_workspace_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,
            kind TEXT NOT NULL,ref TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS sales_workspace_event_session ON sales_workspace_events(session_id,seq)')
        db.execute('''CREATE TRIGGER IF NOT EXISTS workspace_event_retention AFTER INSERT ON sales_workspace_events
            WHEN NEW.seq % 500 = 0 BEGIN DELETE FROM sales_workspace_events WHERE seq<=NEW.seq-20000; END''')
        for table,kind,state in [('sales_codex_jobs','codex','status||\':\'||NEW.step'),
                                  ('sales_assistant_sessions','draft','status'),
                                  ('sales_api_batches','submission','status'),
                                  ('sales_manual_imports','manual_import','status'),
                                  ('sales_new_product_plans','new_product','status')]:
            ref='NEW.id';session='NEW.id' if kind=='draft' else 'NEW.session_id'
            for action in ('INSERT','UPDATE'):
                when='' if action=='INSERT' else ('WHEN OLD.status!=NEW.status OR OLD.step!=NEW.step' if kind=='codex' else
                    'WHEN OLD.status!=NEW.status OR OLD.version!=NEW.version' if kind=='draft' else 'WHEN OLD.status!=NEW.status')
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS workspace_{kind}_{action.lower()}
                    AFTER {action} ON {table} {when} BEGIN
                    INSERT INTO sales_workspace_events(session_id,kind,ref,state,created_at)
                    VALUES ({session},'{kind}',{ref},NEW.{state},strftime('%Y-%m-%dT%H:%M:%S','now')); END''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS workspace_api_document_update AFTER UPDATE ON sales_api_documents
            WHEN OLD.status!=NEW.status BEGIN
            INSERT INTO sales_workspace_events(session_id,kind,ref,state,created_at)
            SELECT session_id,'document',NEW.id,NEW.status,strftime('%Y-%m-%dT%H:%M:%S','now')
            FROM sales_api_batches WHERE id=NEW.batch_id; END''')
    @bp.before_request
    def owner():
        if request.path in {'/sales','/sales/',BASE+'/login'} or request.path.startswith('/sales/assets/'):return
        with store.connect() as db:
            if g.current_user['id']!=owner_id(db):return jsonify(ok=False,error='owner_only',message='首版仅本人使用'),403
    @bp.errorhandler(ValueError)
    @bp.errorhandler(DailyWorkConflict)
    def bad(error):
        return jsonify(ok=False,error=getattr(error,'code','invalid_request'),message=str(error)),409 if hasattr(error,'code') else 400
    @bp.get('/sales')
    @bp.get('/sales/')
    def index():return send_from_directory(static,'index.html')
    @bp.get('/sales/assets/<path:filename>')
    def assets(filename):return send_from_directory(static/'assets',filename)
    @bp.post(BASE+'/login')
    def login():
        if not same_origin(app):return jsonify(ok=False,error='origin_blocked',message='请从录单网页登录'),403
        body=request.get_json(silent=True) or {}
        token,error=sessions.login(body.get('password'),request.remote_addr,body.get('username','admin'))
        if not token:return jsonify(ok=False,error=error,message='账号或密码错误，请核对；连续错误请稍后再试'),401
        user=sessions.current(token)
        with store.connect() as db:allowed=user['id']==owner_id(db)
        if not allowed:
            sessions.revoke(token);return jsonify(ok=False,error='owner_only',message='首版仅本人使用'),403
        response=jsonify(ok=True,user=user,csrf=csrf(token))
        response.set_cookie(COOKIE,token,httponly=True,secure=not app.config.get('TESTING',False),
                            samesite='Strict',max_age=7*86400,path='/')
        return response
    @bp.post(BASE+'/logout')
    def logout():
        sessions.revoke(cookie_token());response=jsonify(ok=True);response.delete_cookie(COOKIE,path='/');return response
    @bp.get(BASE+'/bootstrap')
    def bootstrap():
        with store.connect() as db:
            profiles=[r[0] for r in db.execute("SELECT DISTINCT profile_id FROM sales_customer_owners ORDER BY profile_id")]
            config=sales_api_jobs.policy(db)
            sales_people=[dict(r) for r in db.execute('''SELECT id,display_name FROM staff_users
                WHERE active=1 AND display_name IN ('李瑄','刘锋')
                AND EXISTS(SELECT 1 FROM json_each(roles) WHERE value='sales') ORDER BY display_name''')]
        return jsonify(ok=True,user=g.current_user,csrf=csrf(cookie_token()),profiles=profiles,
                       sales_people=sales_people,
                       handlers=['李瑄','刘锋'],api_enabled=config.get('enabled') is True and config.get('contract_accepted') is True,
                       mode='owner',browser_enabled=False)
    @bp.get(BASE+'/sessions')
    def listing():
        query=request.args.get('q','')[:200];page=max(1,int(request.args.get('page',1)))
        with store.connect() as db:
            rows=db.execute('''SELECT id,customer_name,status,version,updated_at,archived_at,archive_kind,
                (SELECT status FROM sales_manual_imports m WHERE m.session_id=s.id AND m.status!='cancelled' LIMIT 1) manual_import_status
                FROM sales_assistant_sessions s
                WHERE (archived_at!='')=? AND (customer_name LIKE ? OR json_extract(draft,'$.customer_order_no') LIKE ?)
                ORDER BY updated_at DESC,id LIMIT 30 OFFSET ?''',(int(request.args.get('archived')=='1'),'%'+query+'%','%'+query+'%',(page-1)*30)).fetchall()
        return jsonify(ok=True,items=[dict(row) for row in rows],page=page)
    @bp.get(BASE+'/customers')
    def customers():
        with store.connect() as db:
            result=directory.choices(db,request.args.get('profile_id',''),
                request.args.get('query',''),int(request.args.get('page','1')))
        return jsonify(ok=True,**result)
    @bp.post(BASE+'/customers/refresh')
    def refresh_customers():
        profile=str((request.get_json(silent=True) or {}).get('profile_id',''))
        with store.connect(write=True) as db:
            if profile!=sales_api_jobs.policy(db).get('profile_id'):
                raise ValueError('请选择当前 ERP 账套')
            db.execute('INSERT OR IGNORE INTO sales_erp_customer_sync(profile_id) VALUES (?)',(profile,))
            db.execute('UPDATE sales_erp_customer_sync SET requested=1 WHERE profile_id=?',(profile,))
        return jsonify(ok=True,message='已请求从 ERP 更新客户名录')
    @bp.post(BASE+'/customers/owner')
    def assign_customer_owner():
        body=request.get_json(silent=True) or {};key=_key(body)
        if not body.get('owner_id') or not body.get('erp_customer_id'):
            raise ValueError('请选择客户和固定销售')
        result=app.extensions['service']['customer_orders'].set_customer_owner(g.current_user,
            str(body.get('profile_id','')),str(body.get('customer_name','')),str(body['owner_id']),'','',
            body.get('owner_version'),key,'录单网页确认客户固定销售',
            _directory_id=str(body['erp_customer_id']),_only_unassigned=True)
        return jsonify(ok=True,**result)
    @bp.post(BASE+'/sessions/<identity>/archive')
    @bp.post(BASE+'/sessions/<identity>/restore')
    def archive_session(identity):
        body=request.get_json(silent=True) or {};key=_key(body)
        restore=request.path.endswith('/restore');action='restore_session' if restore else 'archive_session'
        fingerprint=_digest({'action':action,'id':identity,'body':body})
        with store.connect(write=True) as db:
            row=engine._session(db,g.current_user,identity)
            replay=engine._replay(db,g.current_user['id'],key,fingerprint)
            if replay:return jsonify(ok=True,**replay)
            if row['version']!=_version(body):
                raise SalesAssistantError('对话已变化，请刷新后再操作','sales_draft_version_changed')
            if _setting(db,'restoration_hold',False):
                raise SalesAssistantError('恢复后请先核对业务记录','restoration_hold')
            if bool(row['archived_at'])==restore:
                if not restore:
                    busy_api=db.execute("SELECT 1 FROM sales_api_batches WHERE session_id=? AND status IN ('queued','sending')",(identity,)).fetchone()
                    busy_product=db.execute("SELECT 1 FROM sales_new_product_plans WHERE session_id=? AND status IN ('queued','creating','preparing')",(identity,)).fetchone()
                    if busy_api or busy_product or row['status']=='submitting':
                        raise SalesAssistantError('正在执行录单或新建商品，请等待结果后再移除','sales_session_busy')
                    # Invalidate a queued/running model job in the same transaction.
                    db.execute("UPDATE sales_codex_jobs SET status='cancelled',step='cancelled',worker_token='',lease_until='',updated_at=? WHERE session_id=? AND status IN ('queued','running')",(timestamp(),identity))
                kind='' if restore else 'archived' if (_has_sales_execution_reference(db,identity) or
                    row['status'] in {'submitted','submission_unknown','submission_blocked'} or row['erp_order_no'] or row['submitted_at']) else 'deleted'
                db.execute('''UPDATE sales_assistant_sessions SET archived_at=?,archived_by=?,archive_kind=?,
                    version=version+1,updated_at=? WHERE id=?''',
                    ('' if restore else timestamp(),'' if restore else g.current_user['id'],kind,timestamp(),identity))
            return jsonify(ok=True,**engine._finish(db,g.current_user,identity,action,key,fingerprint))
    @bp.post(BASE+'/sessions/<identity>/stop')
    def stop(identity):
        body=request.get_json(silent=True);key=_key(body);fingerprint=_digest({'stop':identity,'body':body})
        with store.connect(write=True) as db:
            row=engine._session(db,g.current_user,identity,write=True)
            previous=engine._replay(db,g.current_user['id'],key,fingerprint)
            if previous:return jsonify(ok=True,**previous)
            engine._write_guard(db,row,body)
            db.execute("UPDATE sales_codex_jobs SET status='cancelled',step='cancelled',worker_token='',lease_until='',updated_at=? WHERE session_id=? AND status IN ('queued','running')",(timestamp(),identity))
            return jsonify(ok=True,**engine._finish(db,g.current_user,identity,'stop_codex',key,fingerprint))
    @bp.get(BASE+'/sessions/<identity>/export')
    def export(identity):
        with store.connect() as db:
            row=engine._session(db,g.current_user,identity)
            batch=db.execute('SELECT model_json FROM sales_api_batches WHERE session_id=?',(identity,)).fetchone()
            model=json.loads(batch[0]) if batch else sales_api_jobs.normalized(db,row)
        return send_file(sales_api_jobs.workbook(model),download_name='录单确认-'+identity[:8]+'.xlsx',as_attachment=True)
    @bp.get(BASE+'/import-template')
    def import_template():
        sales_manual_import.template()
        return send_file(sales_manual_import.TEMPLATE,download_name='销售订单空白导入模板.xls',as_attachment=True)
    @bp.post(BASE+'/sessions/<identity>/manual-import')
    def prepare_manual_import(identity):
        return jsonify(ok=True,**sales_manual_import.prepare(engine,g.current_user,identity,request.get_json(silent=True)))
    @bp.post(BASE+'/sessions/<identity>/manual-import/complete')
    def complete_manual_import(identity):
        return jsonify(ok=True,**sales_manual_import.report(engine,g.current_user,identity,request.get_json(silent=True)))
    @bp.post(BASE+'/sessions/<identity>/manual-import/cancel')
    def cancel_manual_import(identity):
        return jsonify(ok=True,**sales_manual_import.report(engine,g.current_user,identity,request.get_json(silent=True),cancel=True))
    @bp.get(BASE+'/sessions/<identity>/manual-import/<export_id>/file')
    def download_manual_import(identity,export_id):
        with store.connect() as db:
            row=engine._session(db,g.current_user,identity)
            item=db.execute('SELECT * FROM sales_manual_imports WHERE id=? AND session_id=?',(export_id,identity)).fetchone()
            if not item or item['status']=='cancelled' or item['confirmed_hash']!=row['confirmed_hash']:
                raise ValueError('这份导入表格已失效，请查看当前确认稿')
            model=json.loads(item['model_json'])
            from .sales_ownership import fixed_sales_owner
            if fixed_sales_owner(db,row['profile_id'],row['customer_name'])!=model['fixed_owner_id']:
                raise ValueError('客户归属已变化，请先核对')
            content=bytes(item['content'])
            if sha256(content).hexdigest()!=item['sha256']: raise ValueError('导入文件校验失败，请联系管理员')
            filename=item['filename']
        return send_file(BytesIO(content),download_name=filename,as_attachment=True,
                         mimetype='application/vnd.ms-excel',max_age=0)
    @bp.get(BASE+'/sessions/<identity>/events')
    def events(identity):
        with store.connect() as db:engine._session(db,g.current_user,identity)
        cursor=max(0,int(request.headers.get('Last-Event-ID') or request.args.get('after',0)))
        user_id=g.current_user['id'];token=cookie_token()
        if not streams.acquire(blocking=False):
            return Response('retry: 10000\n\n',mimetype='text/event-stream')
        def generate():
            nonlocal cursor
            # Bound connection lifetime and release SQLite between checks.
            for _ in range(12):
                current=sessions.current(token)
                if not current or current['id']!=user_id:return
                with store.connect() as db:
                    if owner_id(db)!=user_id:return
                    rows=db.execute('SELECT * FROM sales_workspace_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 100',(identity,cursor)).fetchall()
                for row in rows:
                    cursor=row['seq'];yield 'id: '+str(cursor)+'\nevent: progress\ndata: '+dumps(dict(row))+'\n\n'
                yield ': keepalive\n\n';time.sleep(1)
        response=Response(generate(),mimetype='text/event-stream',headers={'X-Accel-Buffering':'no','Cache-Control':'no-store'})
        response.call_on_close(streams.release)
        return response
    from .sales_rule_versions import register
    register(bp,store,BASE)
    app.register_blueprint(bp)
