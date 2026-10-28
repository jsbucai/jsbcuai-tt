"""Runs owner-requested language samples on the existing serialized heavy lane."""
import json,os,re,subprocess,signal,uuid
from pathlib import Path
from hashlib import sha256
from core.store import dumps,timestamp
from core.daily_work import _setting
from core.sales_rule_versions import ensure
from tools import sales_codex_runner as runner


def run_one(store,config):
    from tools.sales_codex_worker import _identity,_directory,_write,_secure_bytes,SERVICE
    with store.connect(write=True) as db:
        ensure(db)
        # The lane lock guarantees an earlier testing process has ended.
        db.execute("UPDATE sales_rule_versions SET status='failed',evaluation=?,updated_at=? WHERE status='testing'",
            (dumps({'message':'测试中断，请手动重新运行；未发布规则'}),timestamp()))
        row=db.execute("SELECT * FROM sales_rule_versions WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:return {'claimed':False}
        row=dict(row)
        current=_setting(db,'sales_rules_active_id','')
        if current!=row['base_id']:
            db.execute("UPDATE sales_rule_versions SET status='failed',evaluation=?,updated_at=? WHERE id=?",(dumps({'message':'基础版本已变化'}),timestamp(),row['id']))
            return {'claimed':True,'status':'failed'}
        baseline=db.execute('SELECT content FROM sales_rule_versions WHERE id=?',(current,)).fetchone()
        codes=list(dict.fromkeys(re.findall(r'(?<![A-Z0-9])[A-Z]{1,5}[0-9]{2,}[A-Z0-9-]*',row['problem'].upper())))[:30]
        catalog=[]
        for code in codes:
            for item in db.execute("SELECT payload FROM records WHERE kind='inventory' AND sku=?",(code,)):
                product=json.loads(item['payload']);catalog.append({k:product.get(k) for k in ('sku','product_name','spec','model','unit_name')})
        db.execute("UPDATE sales_rule_versions SET status='testing',updated_at=? WHERE id=?",(timestamp(),row['id']))
    process=None
    try:
        uid,gid,account=_identity(config)
        root=_directory(config.jobs_root/('rules-'+uuid.uuid4().hex),uid,gid)
        base,base_hash=runner._rules(config.rules_root)
        packet={'base_rules':base,'problem':row['problem'],'catalog':catalog,
                'supplements':[baseline[0] if baseline else '',row['content']]}
        _write(root/'input.json',dumps(packet).encode(),uid,gid)
        args=['/usr/bin/python3',str(SERVICE/'tools'/'sales_rule_eval_runner.py'),'--job-dir',str(root),'--codex-binary',str(config.codex_binary)]
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
        if account:
            args=['/usr/sbin/runuser','-u',account.pw_name,'--',*args]
            env.update(HOME=account.pw_dir,USER=account.pw_name,LOGNAME=account.pw_name)
        else:env.update(HOME=os.environ['HOME'])
        process=subprocess.Popen(args,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        if process.wait(timeout=320)!=0:raise ValueError('rule_test_failed')
        results=json.loads(_secure_bytes(root/'result.json',owner=uid))['comparisons']
        expected=json.loads(row['expected']);passed=results[1]['result']==expected
        report={'scope':'语言理解与字段识别样本；不写 ERP、不更改当前订单',
                'base_hash':base_hash,'candidate_hash':row['content_hash'],
                'baseline':results[0]['result'],'candidate':results[1]['result'],
                'expected':expected,'passed':passed}
        status='passed' if passed else 'failed'
    except Exception:
        status='failed';report={'message':'未取得完整样本结果，原规则继续使用；可以手动重新测试'}
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
    with store.connect(write=True) as db:
        db.execute('UPDATE sales_rule_versions SET status=?,evaluation=?,updated_at=? WHERE id=? AND status=?',
                   (status,dumps(report),timestamp(),row['id'],'testing'))
    return {'claimed':True,'kind':'rule_evaluation','status':status}
