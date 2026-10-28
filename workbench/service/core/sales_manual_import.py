"""Frozen ERP import files and human-reported progress; never calls ERP."""
from datetime import date
from hashlib import sha256
from pathlib import Path
import json
import re
import uuid

from . import sales_api_jobs
from .daily_work import _setting
from .sales_web_import import _number, _render, _validate_template, _verify
from .store import dumps, timestamp

TEMPLATE = Path(__file__).resolve().parents[1] / 'assets/templates/sales-order-import.xls'
TEMPLATE_SHA = 'a24e8a1f34d781b8719840def86bf02452ecc53f8bfe681e5a629c48c30fa2e2'


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sales_manual_imports(
        id TEXT PRIMARY KEY,session_id TEXT NOT NULL,profile_id TEXT NOT NULL,
        confirmed_hash TEXT NOT NULL,model_json TEXT NOT NULL,content BLOB NOT NULL,
        sha256 TEXT NOT NULL,filename TEXT NOT NULL,status TEXT NOT NULL,
        actor_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS sales_manual_active_session
        ON sales_manual_imports(session_id) WHERE status!='cancelled' ''')
    db.execute('CREATE INDEX IF NOT EXISTS sales_manual_profile ON sales_manual_imports(profile_id,status)')


def active(db, identity):
    return db.execute("SELECT id FROM sales_manual_imports WHERE session_id=? AND status!='cancelled'",
                      (identity,)).fetchone() is not None


def projection(db, identity):
    row = db.execute('''SELECT id,status,filename,sha256,model_json,created_at,updated_at
        FROM sales_manual_imports WHERE session_id=? ORDER BY rowid DESC LIMIT 1''', (identity,)).fetchone()
    if not row:
        return None
    model = json.loads(row['model_json'])
    return {**{k:row[k] for k in ('id','status','filename','sha256','created_at','updated_at')},
            'document_count':len(model['documents']),
            'document_numbers':[d['number'] for d in model['documents']],
            'completion_basis':'user_report' if row['status']=='reported_imported' else 'file_generated',
            'erp_readback_performed':False}


def template():
    if not TEMPLATE.is_file() or sha256(TEMPLATE.read_bytes()).hexdigest() != TEMPLATE_SHA:
        raise ValueError('原始 ERP 导入模板缺失或已变化，请联系管理员')
    return _validate_template(TEMPLATE)


def workbook(model):
    """Keep the vendor BIFF8 format and all 27 columns, with typed values."""
    source = template()
    values = []
    for doc in model['documents']:
        if len(doc['summary'].encode('utf-16-le')) // 2 > 180:
            raise ValueError('摘要超过 ERP 导入的 180 字符限制，请核对后缩短；不会自动截断')
        values.append({'录单日期':date.fromisoformat(model['order_date']),
            '单据编号':doc['number'],'单据类型':'销售订单','往来单位名称':model['customer_name'],
            '部门名称':model['department_name'],'摘要':doc['summary'],'附加说明':'',
            '制单人名称':model['maker_name'],'经手人名称':model['handler_name'],
            '仓库名称':model['warehouse_name'],'明细仓库名称':model['warehouse_name'],
            '交货日期':date.fromisoformat(doc['delivery_date']),
            '商品编号':doc['product_code'],'商品名称':doc['product_name'],'单位':'支',
            '数量':float(_number(doc['quantity'],'数量',positive=True)),
            '折前单价':float(_number(doc['unit_price'],'未税单价')),
            '折前金额':float(_number(doc['amount'],'未税金额')),
            '税率(%)':13,'明细备注':doc['summary']})
    raw = _render(source, values)
    _verify(source, raw, values)
    return raw


def _model(engine, db, row, user):
    from .sales_assistant import SalesAssistantError
    engine._codex_confirmation_guard(db,row)
    old = json.loads(row['draft'])
    current = engine._draft(db,row,old,actor_id=user['id'],identity_change_code='sales_catalog_changed')
    if old.get('issues') or current['issues']:
        raise SalesAssistantError('草稿还有待核对项，请先核对并确认','sales_draft_needs_review',422)
    for before,after in zip(old['lines'],current['lines']):
        if any(before.get(k)!=after.get(k) for k in
               ('resolved_identity','catalog_summary','erp_unit_names','unit_evidence')):
            raise SalesAssistantError('商品资料已变化，请核对后重新确认','sales_catalog_changed')
    model = sales_api_jobs.normalized(db,row)
    model.pop('customer_delivery',None)
    model.pop('delivery_write_status',None)
    policy = _setting(db,'sales_web_import_policy',{})
    model['department_name'] = policy.get('department_name') or '销售部'
    return model


def prepare(engine,user,identity,body):
    from .sales_assistant import _key,_digest
    key = _key(body)
    fingerprint = _digest({'manual_import':identity,'body':body})
    with engine.store.connect() as db:
        row = engine._session(db,user,identity,write=True)
        replay = engine._replay(db,user['id'],key,fingerprint)
        if replay:
            return replay
        engine._write_guard(db,row,body)
        model = _model(engine,db,row,user)
        sales_api_jobs._reserve_guard(db,row['profile_id'],[d['number'] for d in model['documents']])
    # Generate and verify outside SQLite's writer lock.
    content = workbook(model)
    assert len(content) <= 2*1024*1024
    with engine.store.connect(write=True) as db:
        row = engine._session(db,user,identity,write=True)
        replay = engine._replay(db,user['id'],key,fingerprint)
        if replay:
            return replay
        engine._write_guard(db,row,body)
        if _model(engine,db,row,user) != model:
            raise ValueError('确认内容已变化，请刷新后核对，原输入仍保留')
        sales_api_jobs._reserve_guard(db,row['profile_id'],[d['number'] for d in model['documents']])
        export_id = uuid.uuid4().hex
        po = model['documents'][0]['number'].rsplit('#',1)[0]
        safe_po = re.sub(r'[^\w.-]+','-',po)[:60].strip('-') or '订单'
        filename = '销售订单导入-'+safe_po+'-'+export_id[:8]+'.xls'
        now = timestamp()
        db.execute('INSERT INTO sales_manual_imports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (export_id,identity,row['profile_id'],row['confirmed_hash'],dumps(model),content,
             sha256(content).hexdigest(),filename,'prepared',user['id'],now,now))
        return engine._finish(db,user,identity,'prepare_manual_import',key,fingerprint)


def report(engine,user,identity,body,*,cancel=False):
    from .sales_assistant import SalesAssistantError,_key,_digest,_version
    key = _key(body)
    action = 'cancel_manual_import' if cancel else 'report_manual_imported'
    fingerprint = _digest({'action':action,'id':identity,'body':body})
    with engine.store.connect(write=True) as db:
        row = engine._session(db,user,identity,write=True)
        replay = engine._replay(db,user['id'],key,fingerprint)
        if replay:
            return replay
        if _setting(db,'restoration_hold',False):
            raise SalesAssistantError('恢复后请先核对业务记录','restoration_hold')
        item = db.execute("SELECT * FROM sales_manual_imports WHERE id=? AND session_id=?",
                          (body.get('export_id'),identity)).fetchone()
        if row['version']!=_version(body) or not item or item['confirmed_hash']!=row['confirmed_hash']:
            raise SalesAssistantError('导入文件或确认版本已变化，请刷新核对','sales_draft_version_changed')
        if item['status']!='prepared' or body.get('confirmed') is not True:
            raise ValueError('请确认本次人工导入的实际结果')
        db.execute('UPDATE sales_manual_imports SET status=?,updated_at=? WHERE id=?',
            ('cancelled' if cancel else 'reported_imported',timestamp(),item['id']))
        return engine._finish(db,user,identity,action,key,fingerprint)
