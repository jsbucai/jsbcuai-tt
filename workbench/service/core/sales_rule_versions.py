"""Immutable owner-authored rule supplements, tested before explicit activation."""
import json
import uuid
from hashlib import sha256
from flask import g,jsonify,request
from .store import dumps,timestamp
from .daily_work import _setting


def ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sales_rule_versions(
        id TEXT PRIMARY KEY,content TEXT NOT NULL,content_hash TEXT NOT NULL,
        problem TEXT NOT NULL,expected TEXT NOT NULL,base_id TEXT NOT NULL,
        status TEXT NOT NULL,evaluation TEXT NOT NULL DEFAULT '{}',created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS sales_rule_actions(
        actor TEXT NOT NULL,key TEXT NOT NULL,hash TEXT NOT NULL,result TEXT NOT NULL,
        PRIMARY KEY(actor,key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS sales_rule_publications(
        id TEXT PRIMARY KEY,version_id TEXT NOT NULL,previous_id TEXT NOT NULL,
        actor TEXT NOT NULL,created_at TEXT NOT NULL)''')


def pinned(db):
    active=_setting(db,'sales_rules_active_id','')
    row=db.execute('SELECT * FROM sales_rule_versions WHERE id=?',(active,)).fetchone() if active else None
    return {'id':active,'content':row['content'] if row else '',
            'sha256':row['content_hash'] if row else sha256(b'').hexdigest(),
            'base_hash':_setting(db,'sales_rules_base_hash','')}


def register(bp,store,base):
    from .sales_assistant import _key,_digest,SalesAssistantError
    with store.connect(write=True) as db:ensure(db)
    def mutate(action):
        body=request.get_json(silent=True) or {};key=_key(body);fingerprint=_digest({'path':request.path,'body':body})
        with store.connect(write=True) as db:
            old=db.execute('SELECT * FROM sales_rule_actions WHERE actor=? AND key=?',(g.current_user['id'],key)).fetchone()
            if old:
                if old['hash']!=fingerprint:raise SalesAssistantError('操作标识冲突','idempotency_conflict')
                return jsonify(ok=True,**json.loads(old['result']))
            result=action(db,body)
            db.execute('INSERT INTO sales_rule_actions VALUES (?,?,?,?)',(g.current_user['id'],key,fingerprint,dumps(result)))
        return jsonify(ok=True,**result)
    @bp.get(base+'/rules')
    def list_rules():
        with store.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM sales_rule_versions ORDER BY created_at DESC,id DESC LIMIT 50')]
            active=_setting(db,'sales_rules_active_id','')
        for r in rows:r['evaluation']=json.loads(r['evaluation'])
        return jsonify(ok=True,versions=rows,active_id=active)
    @bp.post(base+'/rules')
    def create():
        def action(db,body):
            for field in ('problem','expected','content'):
                if not isinstance(body.get(field),str) or not 1<=len(body[field].strip())<=10000:raise ValueError('请填写问题样本、期望结果与补充说明（各不超过 10000 字）')
            try:expected=json.loads(body['expected'])
            except ValueError:raise ValueError('期望结果请用 JSON，例如 {"product_codes":["LS3073"],"needs_clarification":false}') from None
            if (not isinstance(expected,dict) or set(expected)!={'product_codes','needs_clarification'} or
                not isinstance(expected['product_codes'],list) or not all(isinstance(c,str) and len(c)<=100 for c in expected['product_codes']) or
                type(expected['needs_clarification']) is not bool):raise ValueError('期望结果须包含 product_codes 数组与 needs_clarification 布尔值')
            identity=uuid.uuid4().hex;now=timestamp();content=body['content'].strip()
            active=_setting(db,'sales_rules_active_id','')
            prior=db.execute('SELECT content FROM sales_rule_versions WHERE id=?',(active,)).fetchone()
            if prior:content=prior['content']+'\n\n'+content
            if len(content)>30000:raise ValueError('累计补充规则过长，请先整理现有规则')
            db.execute('''INSERT INTO sales_rule_versions
                (id,content,content_hash,problem,expected,base_id,status,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'draft',?,?,?)''',(identity,content,sha256(content.encode()).hexdigest(),body['problem'],dumps(expected),
                    active,g.current_user['id'],now,now))
            return {'id':identity}
        return mutate(action)
    @bp.post(base+'/rules/<identity>/evaluate')
    def evaluate(identity):
        def action(db,body):
            row=db.execute('SELECT * FROM sales_rule_versions WHERE id=?',(identity,)).fetchone()
            if not row:raise ValueError('候选版本不存在')
            if row['base_id']!=_setting(db,'sales_rules_active_id',''):raise ValueError('基础规则已变化，请新建候选版本重新比较')
            if row['status'] in {'testing','queued'}:return {'id':identity,'status':row['status']}
            db.execute("UPDATE sales_rule_versions SET status='queued',evaluation='{}',updated_at=? WHERE id=?",(timestamp(),identity))
            return {'id':identity,'status':'queued'}
        return mutate(action)
    @bp.post(base+'/rules/<identity>/publish')
    def publish(identity):
        def action(db,body):
            active=_setting(db,'sales_rules_active_id','')
            row=db.execute('SELECT * FROM sales_rule_versions WHERE id=?',(identity,)).fetchone()
            if body.get('expected_active_id')!=active:raise ValueError('当前规则版本已变化，请刷新核对')
            if not row or row['status']!='passed':raise ValueError('此版本还未通过样本测试')
            if row['base_id']!=active:raise ValueError('当前规则已有变化，请基于当前版本重新测试')
            report=json.loads(row['evaluation'])
            if report.get('candidate_hash')!=row['content_hash'] or report.get('base_hash')!=_setting(db,'sales_rules_base_hash',''):
                raise ValueError('测试对应的规则文件已变化，请重新测试')
            db.execute("INSERT OR REPLACE INTO settings VALUES ('sales_rules_active_id',?)",(dumps(identity),))
            db.execute('INSERT INTO sales_rule_publications VALUES (?,?,?,?,?)',(uuid.uuid4().hex,identity,active,g.current_user['id'],timestamp()))
            return {'active_id':identity}
        return mutate(action)
    @bp.post(base+'/rules/rollback')
    def rollback():
        def action(db,body):
            active=_setting(db,'sales_rules_active_id','')
            if not active or body.get('expected_active_id')!=active:raise ValueError('当前规则版本已变化')
            publication=db.execute('SELECT previous_id FROM sales_rule_publications WHERE version_id=? ORDER BY rowid DESC LIMIT 1',(active,)).fetchone()
            if not publication:raise ValueError('没有可恢复的上一版')
            previous=publication['previous_id']
            db.execute("INSERT OR REPLACE INTO settings VALUES ('sales_rules_active_id',?)",(dumps(previous),))
            db.execute('INSERT INTO sales_rule_publications VALUES (?,?,?,?,?)',(uuid.uuid4().hex,previous,active,g.current_user['id'],timestamp()))
            return {'active_id':previous}
        return mutate(action)
