"""Fixed-scope application API. No staff identity, exports, or ERP operations."""
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
import time
from datetime import datetime, timezone
from flask import Blueprint, g, jsonify, request
from .procurement_projection import read_cards, needs_procurement, ProjectionUnavailable

CONTRACT = 'dot-ul-readonly/2'
PREFIX = '/api/dot-readonly/v1/'
TOKEN_PREFIX = 'dot_ro_v1_'
PROFILE = '27056512834386351'
WAREHOUSE = '29871293290508390'
SCOPE = {'profile_id': PROFILE, 'warehouse_id': WAREHOUSE, 'warehouse_code': '08390'}
FIELDS = {
    'orders': ('id','order_no','document_date','sku','product_name','quantity','unit_name','status'),
    'inventory': ('id','sku','product_name','spec','location','stock_quantity','available_stock',
                  'unit_name','stock_observed_at','product_id','sku_id','base_unit'),
    'purchases': ('id','document_no','document_date','posted_at','document_type','sku','product_name',
                  'quantity','unit_name','quantity_basis'),
}
SEARCH = {'orders':('order_no','sku','product_name'),
          'inventory':('sku','product_name','spec','location'),
          'purchases':('document_no','sku','product_name')}
PIN_FILES = {'app.py','run.py','core/auth.py','core/store.py','core/api_source.py',
             'core/queries.py','core/sql_queries.py','core/dot_readonly.py',
             'core/customer_orders.py','core/daily_work.py','core/task_board.py','core/procurement_projection.py'}
ALLOWED_TABLES = {'records','settings','sync_batches'}
PROCUREMENT_COLUMNS = {
 'daily_work_settings': {'key','value'},
 'daily_work_tasks': {'id','profile_id','warehouse_id','order_no','product_id','sku','payload','role','condition_state'},
 'logistics_calculations': {'rowid','ROWID','profile','data_version','payload'},
 'customer_order_group_aliases': {'legacy_group_id','customer_name','group_id','profile_id','warehouse_id'},
 'json_each': {'value','json','root'},
}
ACTIONS = (*FIELDS, 'sources', 'procurement')

class Denied(Exception):
    def __init__(self, code, status=503):
        self.code,self.status=code,status

class DotReadOnly:
    def __init__(self, store, config):
        self.store=store
        self.root=Path(__file__).resolve().parents[1]
        self.policy=Path(config.get('DOT_READONLY_POLICY') or store.runtime/'secrets/dot-ro-policy.json')
        self.manifest=Path(config.get('DOT_READONLY_MANIFEST') or self.root/'dot-readonly-manifest.json')
        self.slot=threading.BoundedSemaphore(1)

    def boundary(self):
        """Run before staff authentication; a service token never becomes a staff session."""
        authorization=request.headers.get('Authorization','')
        token=authorization[7:] if authorization.startswith('Bearer ') else ''
        if not request.path.startswith('/api/dot-readonly') and not token.startswith(TOKEN_PREFIX):
            return None
        if request.path not in {PREFIX+k for k in ACTIONS} or request.method!='GET':
            return jsonify(ok=False,error='action_not_allowed'),403
        try:
            policy=json.loads(self.policy.read_text())
            if (policy.get('enabled') is not True or policy.get('scope')!=SCOPE
                    or not isinstance(policy.get('token_sha256'),str) or len(policy['token_sha256'])!=64):
                raise Denied('service_disabled',403)
            if (not token.startswith(TOKEN_PREFIX) or len(token)>160 or
                    not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),policy['token_sha256'])):
                raise Denied('authentication_required',401)
        except Denied as e:
            return jsonify(ok=False,error=e.code),e.status
        except Exception:
            return jsonify(ok=False,error='service_disabled'),403
        g.dot_service=True
        return None

    def compatible(self, db):
        try:
            manifest=json.loads(self.manifest.read_text())
            pins=manifest['sha256']
            if manifest['contract']!=CONTRACT or set(pins)!=PIN_FILES:
                raise Denied('upgrade_review_required')
            for name,expected in pins.items():
                if hashlib.sha256((self.root/name).read_bytes()).hexdigest()!=expected:
                    raise Denied('upgrade_review_required')
            if db.execute('PRAGMA user_version').fetchone()[0]!=11:
                raise Denied('schema_incompatible')
        except Denied:
            raise
        except Exception:
            raise Denied('upgrade_review_required') from None

    @staticmethod
    def params(kind):
        if len(request.query_string)>4096:
            raise Denied('query_not_allowed',400)
        args=request.args
        allowed={'page','page_size','q','expected_data_version'} if kind in FIELDS else set()
        if set(args)-allowed or any(len(args.getlist(k))!=1 for k in args):
            raise Denied('query_not_allowed',400)
        try:
            page=int(args.get('page','1'));size=int(args.get('page_size','50'))
        except ValueError:
            raise Denied('invalid_pagination',400) from None
        q=args.get('q','');version=args.get('expected_data_version','')
        if not 1<=page<=100000 or not 1<=size<=100 or len(q)>100 or len(version)>128:
            raise Denied('invalid_query',400)
        if page>1 and not version:
            raise Denied('version_required',409)
        return page,size,q,version

    def metadata(self, db):
        settings={r['key']:json.loads(r['value']) for r in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('data_version','data_source_updated_at',"
            "'data_published_at','data_query_scope','data_quantity_basis')")}
        version=settings.get('data_version')
        if not isinstance(version,str) or not 1<=len(version)<=128:
            raise Denied('publication_unavailable')
        publication=db.execute("SELECT profile,status,source_updated_at,data_version,published_at FROM sync_batches "
            "WHERE published_at IS NOT NULL ORDER BY published_at DESC,rowid DESC LIMIT 1").fetchone()
        if not publication or (publication['profile'],publication['status'],publication['data_version'])!=(PROFILE,'published',version):
            raise Denied('publication_scope_mismatch')
        if settings.get('data_source_updated_at')!=publication['source_updated_at'] or settings.get('data_published_at')!=publication['published_at']:
            raise Denied('publication_metadata_mismatch')
        counts={}
        for kind in FIELDS:
            invalid=db.execute("SELECT count(*) FROM records WHERE kind=? AND "
                "(NOT json_valid(payload) OR coalesce(cast(json_extract(payload,'$.profile_id') AS TEXT),'')<>? "
                "OR coalesce(cast(json_extract(payload,'$.warehouse_id') AS TEXT),'')<>?)",
                (kind,PROFILE,WAREHOUSE)).fetchone()[0]
            if invalid:raise Denied('scope_mismatch')
            counts[kind]=db.execute('SELECT count(*) FROM records WHERE kind=?',(kind,)).fetchone()[0]
        missing=db.execute("SELECT count(*) FROM records WHERE kind='inventory' AND ("
            "coalesce(json_extract(payload,'$.product_id'),'')='' "
            "OR coalesce(json_extract(payload,'$.base_unit'),'')='')").fetchone()[0]
        if missing:raise Denied('inventory_identity_missing')
        updated=publication['source_updated_at'];parsed=datetime.fromisoformat(updated.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise Denied('source_time_invalid')
        now=datetime.now(timezone.utc);age=(now-parsed).total_seconds()
        if age < -300:raise Denied('source_time_invalid')
        coverage=settings.get('data_query_scope')
        if not isinstance(coverage,dict):raise Denied('coverage_unavailable')
        start,end=coverage.get('purchase_history_start'),coverage.get('purchase_history_end')
        if not (isinstance(start,str) and isinstance(end,str) and 10<=len(start)<=35 and 10<=len(end)<=35 and start<=end):
            raise Denied('purchase_coverage_unavailable')
        quantity_basis=settings.get('data_quantity_basis')
        if quantity_basis is not None and (not isinstance(quantity_basis,str) or len(quantity_basis)>128):
            raise Denied('quantity_basis_invalid')
        return {'ok':True,'contract':CONTRACT,'scope':SCOPE,'data_version':version,
            'source_updated_at':updated,'published_at':publication['published_at'],
            'generated_at':now.isoformat(timespec='seconds'),'stale':age>86400,
            'source_age_seconds':max(0,int(age)),'counts':counts,'quantity_basis':quantity_basis,
            'purchase_coverage':{'start':start,'end':end,'date_basis':'posted_at',
                'quantity_basis':'posted_movement','pending_purchase_batches':False},
            'source':'UL 已发布数据；采购为已过账流水，不代表采购未到货；读取不触发同步'}

    def query(self, kind):
        page,size,q,expected=self.params(kind)
        if not self.slot.acquire(blocking=False):raise Denied('busy')
        try:
            with self.store.connect() as db:
                self.compatible(db)
                db.execute('PRAGMA query_only=ON')
                db.execute("SELECT value FROM json_each('[]')").fetchall()
                def authorizer(action,table,column,database,trigger):
                    if action==sqlite3.SQLITE_READ:
                        return sqlite3.SQLITE_OK if (table in ALLOWED_TABLES or kind=='procurement' and
                            column in PROCUREMENT_COLUMNS.get(table,set())) else sqlite3.SQLITE_DENY
                    if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_TRANSACTION):
                        return sqlite3.SQLITE_OK
                    return sqlite3.SQLITE_DENY
                db.set_authorizer(authorizer)
                deadline=time.monotonic()+3
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),5000)
                try:
                    meta=self.metadata(db)
                    if expected and expected!=meta['data_version']:raise Denied('data_version_changed',409)
                    if kind=='sources':return meta
                    if kind=='procurement':return self.procurement(db,meta)
                    where="kind=? AND cast(json_extract(payload,'$.profile_id') AS TEXT)=? AND cast(json_extract(payload,'$.warehouse_id') AS TEXT)=?"
                    values=[kind,PROFILE,WAREHOUSE]
                    if q:
                        where+=' AND ('+' OR '.join("instr(lower(coalesce(cast(json_extract(payload,'$."+f+"') AS TEXT),'')),lower(?))>0" for f in SEARCH[kind])+')'
                        values.extend([q]*len(SEARCH[kind]))
                    total=db.execute('SELECT count(*) FROM records WHERE '+where,values).fetchone()[0]
                    columns=','.join("json_extract(payload,'$."+f+"') AS "+f+",json_type(payload,'$."+f+"') AS type_"+f for f in FIELDS[kind])
                    rows=db.execute('SELECT '+columns+' FROM records WHERE '+where+' ORDER BY id LIMIT ? OFFSET ?',
                                    [*values,size,(page-1)*size]).fetchall()
                    items=[]
                    for row in rows:
                        item={}
                        for f in FIELDS[kind]:
                            value=row[f]
                            if row['type_'+f] not in (None,'null','text','integer','real','true','false') or (isinstance(value,str) and len(value)>2048):
                                raise Denied('field_incompatible')
                            item[f]=value
                        if any(item.get(k) is None or str(item[k])=='' for k in ('id','product_name')):
                            raise Denied('record_identity_missing')
                        items.append(item)
                    return {**meta,'kind':kind,'items':items,'total':total,'page':page,'page_size':size,'returned':len(items)}
                finally:
                    db.set_progress_handler(None,0);db.set_authorizer(None)
        finally:self.slot.release()

    def procurement(self, db, meta):
        settings=dict(db.execute("SELECT key,value FROM daily_work_settings WHERE key IN "
            "('task_version','projection_data_version','projection_source_updated_at')"))
        task_version=settings.get('task_version')
        task_version=int(task_version) if isinstance(task_version,str) and task_version.isdigit() else None
        current=bool(meta['data_version']==settings.get('projection_data_version') and
                     meta['source_updated_at']==settings.get('projection_source_updated_at'))
        reason='task_projection_unavailable' if task_version is None else 'task_projection_changed' if not current else ''
        total=None
        if not reason:
            try:
                cards=read_cards(db,PROFILE,WAREHOUSE,meta['data_version'])
                total=len([card for card in cards if needs_procurement(card)])
            except ProjectionUnavailable as e:
                reason=str(e)
            except (ValueError,TypeError,KeyError):
                reason='projection_invalid'
        return {key:meta[key] for key in ('ok','contract','scope','data_version','source_updated_at','generated_at','stale')} | {
            'metric':'procurement_items','total':total,'task_version':task_version,
            'task_data_version':settings.get('projection_data_version',''),
            'tasks_current':current,'valid':not bool(reason),'unavailable_reason':reason}

    def routes(self):
        bp=Blueprint('dot_readonly',__name__)
        def endpoint(kind):
            if not getattr(g,'dot_service',False):return jsonify(ok=False,error='authentication_required'),401
            try:return jsonify(self.query(kind))
            except Denied as e:return jsonify(ok=False,error=e.code,message='只读请求未返回数据，请检查范围、版本或接口状态。'),e.status
            except Exception:return jsonify(ok=False,error='contract_incompatible',message='只读契约不兼容，已停止返回。'),503
        for kind in ACTIONS:
            bp.add_url_rule(PREFIX+kind,kind,lambda kind=kind:endpoint(kind),methods=['GET'])
        return bp
