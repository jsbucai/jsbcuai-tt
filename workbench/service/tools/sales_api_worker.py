"""Single-lane sales API worker. Restart recovery never retransmits a document."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.store import Store,dumps,timestamp
from core.erp_api import ErpClient
from core.sales_api_jobs import ensure_schema,policy,wire_document,decimal_json
from core.sales_api_transport import preflight,submit_once
from core.sales_ownership import fixed_sales_owner
from core.daily_work import _setting


def run_one(store,client,*,prepare=preflight,send=submit_once):
    with store.connect(write=True) as db:
        ensure_schema(db)
        # A prior sender may have reached ERP before exiting. Never reclaim it.
        for old in db.execute("SELECT * FROM sales_api_batches WHERE status='sending'").fetchall():
            unfinished=db.execute("SELECT 1 FROM sales_api_documents WHERE batch_id=? AND status!='accepted' LIMIT 1",(old['id'],)).fetchone()
            if not unfinished:
                db.execute("UPDATE sales_api_batches SET status='accepted',updated_at=? WHERE id=?",(timestamp(),old['id']))
                db.execute("UPDATE sales_assistant_sessions SET status='submitted',submitted_at=?,updated_at=? WHERE id=?",(timestamp(),timestamp(),old['session_id']))
                continue
            db.execute("UPDATE sales_api_documents SET status='unknown',receipt_json=?,updated_at=? WHERE batch_id=? AND status='sending'",
                (dumps({'message':'执行中断，需核对原单；未自动重传'}),timestamp(),old['id']))
            db.execute("UPDATE sales_api_batches SET status='unknown',updated_at=? WHERE id=?",(timestamp(),old['id']))
            db.execute("UPDATE sales_api_documents SET status='held',receipt_json=?,updated_at=? WHERE batch_id=? AND status='queued'",
                (dumps({'message':'上一明细结果未定，后续明细未发送'}),timestamp(),old['id']))
            db.execute("UPDATE sales_assistant_sessions SET status='submission_unknown',updated_at=? WHERE id=?",(timestamp(),old['session_id']))
        config=policy(db)
        if config.get('enabled') is not True or config.get('contract_accepted') is not True or _setting(db,'restoration_hold',False):return {'claimed':False}
        batch=db.execute("SELECT * FROM sales_api_batches WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not batch:return {'claimed':False}
        batch=dict(batch);model=json.loads(batch['model_json'])
    try:binding=prepare(client,model)
    except Exception as error:
        finish(store,batch,'blocked',{'code':getattr(error,'code','sales_api_preflight_failed'),'message':str(error) if hasattr(error,'code') else 'API 提交前核对未通过，尚未发送'})
        return {'claimed':True,'status':'blocked'}
    for doc in model['documents']:
        try:wire=wire_document(model,doc,binding)
        except Exception:
            finish(store,batch,'blocked',{'message':'当前商品身份与确认稿不一致，已暂停'});break
        with store.connect(write=True) as db:
            config=policy(db);person=db.execute('SELECT active FROM staff_users WHERE id=?',(batch['actor_id'],)).fetchone()
            session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(batch['session_id'],)).fetchone()
            if (not person or not person['active'] or config.get('enabled') is not True or config.get('contract_accepted') is not True
                or config.get('profile_id')!=model['profile_id'] or batch['actor_id']!=config.get('owner_user_id','admin')
                or _setting(db,'restoration_hold',False) or not session or session['confirmed_hash']!=batch['confirmed_hash']
                or fixed_sales_owner(db,model['profile_id'],model['customer_name'])!=model['fixed_owner_id']):
                db.execute("UPDATE sales_api_batches SET status='blocked',updated_at=? WHERE id=?",(timestamp(),batch['id']))
                db.execute("UPDATE sales_assistant_sessions SET status='submission_blocked',updated_at=? WHERE id=?",(timestamp(),batch['session_id']))
                break
            changed=db.execute("UPDATE sales_api_documents SET status='sending',payload_json=?,updated_at=? WHERE batch_id=? AND line_id=? AND status='queued'",
                (decimal_json(wire),timestamp(),batch['id'],doc['line_id']))
            if changed.rowcount!=1:break
            db.execute("UPDATE sales_api_batches SET status='sending',updated_at=? WHERE id=?",(timestamp(),batch['id']))
        try:receipt=send(client,wire)
        except BaseException:
            # Leave committed sending intent for recovery on the next start.
            raise
        status=receipt.get('status') if receipt.get('status') in {'accepted','rejected','unknown'} else 'unknown'
        with store.connect(write=True) as db:
            db.execute('UPDATE sales_api_documents SET status=?,receipt_json=?,updated_at=? WHERE batch_id=? AND line_id=?',
                (status,dumps(receipt),timestamp(),batch['id'],doc['line_id']))
        if status!='accepted':
            finish(store,batch,'blocked' if status=='rejected' else 'unknown',receipt);break
    else:finish(store,batch,'accepted',{})
    return {'claimed':True}


def finish(store,batch,status,receipt):
    with store.connect(write=True) as db:
        now=timestamp()
        db.execute('UPDATE sales_api_batches SET status=?,updated_at=? WHERE id=?',(status,now,batch['id']))
        if status!='accepted':
            db.execute("UPDATE sales_api_documents SET status='held',receipt_json=?,updated_at=? WHERE batch_id=? AND status='queued'",(dumps(receipt),now,batch['id']))
        state={'accepted':'submitted','unknown':'submission_unknown','blocked':'submission_blocked'}[status]
        db.execute('UPDATE sales_assistant_sessions SET status=?,submitted_at=?,updated_at=? WHERE id=?',
            (state,now if status=='accepted' else '',now,batch['session_id']))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',required=True);p.add_argument('--vault',required=True);args=p.parse_args()
    from tools.sales_codex_worker import _database_identity,_ServiceIdentityStore
    info=(Path(args.runtime)/'workbench.sqlite3').stat()
    with _database_identity(info.st_uid,info.st_gid):store=Store(args.runtime)
    print(dumps(run_one(_ServiceIdentityStore(store),ErpClient(args.vault))))
