"""Install additive host files after backup; backend image switch is separate."""
from pathlib import Path
import json,os,shutil,sys,hashlib
from datetime import datetime,timezone
os.umask(0o077)
source=Path(sys.argv[1]);root=Path('/opt/ul-workbench')
backup=Path('/var/backups/ul-adjustments-20261013')/datetime.now(timezone.utc).strftime('%H%M%S')
backup.mkdir(parents=True,mode=0o700)
files=['service/app.py','service/core/print_jobs.py','service/core/print_ordering.py',
       'service/core/adjustment_web.py','service/core/adjustment_web_import.py',
       'service/tools/adjustment_web_worker.py','service/tools/erp_human_challenge.py',
       'service/tools/weixin_erp_verification.py','service/tools/weixin_guardian.py']
manifest=[]
for relative in files:
 target=root/relative;old=backup/relative
 if target.exists():old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,old)
 tmp=target.with_name(target.name+'.new');shutil.copyfile(source/relative,tmp);tmp.chmod(0o644);os.replace(tmp,target)
 manifest.append({'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
for name in ('ul-adjustment-web.service','ul-weixin-notify.service'):
 target=Path('/etc/systemd/system')/name
 if target.exists():shutil.copy2(target,backup/name)
 shutil.copyfile(source/'deploy'/name,target);target.chmod(0o644)
collector=Path('/opt/ul-adjustment-runner/collector/purchase_export_collector');collector.mkdir(parents=True,exist_ok=True)
for p in (source/'tools/purchase_export_collector').glob('*.py'):
 shutil.copyfile(p,collector/p.name);(collector/p.name).chmod(0o644)
for parent in (collector,collector.parent,collector.parent.parent):parent.chmod(0o755)
configroot=Path('/etc/ul-adjustment-web');configroot.mkdir(mode=0o700,exist_ok=True);os.chown(configroot,10001,10001)
c=json.loads(Path('/etc/ul-sales-web-worker/config.json').read_text())
c['collector_source_path']=str(collector.parent);c['adjustment_browser_profile']='/var/lib/ul-adjustment-runner/browser-profile'
p=configroot/'config.json';p.write_text(json.dumps(c));p.chmod(0o600);os.chown(p,10001,10001)
for name in ('weixin_guardian.py','weixin_erp_verification.py','erp_human_challenge.py'):
 shutil.copyfile(source/'service/tools'/name,Path('/opt/ul-guardian')/name);(Path('/opt/ul-guardian')/name).chmod(0o644)
(backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
# Config is non-secret. Backend records exact actor/identity on each intake.
os.setgid(10001);os.setuid(10001)
sys.path.insert(0,str(root/'service'))
from core.store import Store,dumps
from core.adjustment_web import WebAdjustmentJobs
from core.sql_queries import Queries
store=Store(root/'service/runtime/data');jobs=WebAdjustmentJobs(store,Queries(store))
public={k:c[k] for k in ('profile_id','account_id','warehouse_id','warehouse_name','maker_name')}
public.update(enabled=False,preflight_verified=True,operator_name=c['operator_name'])
with store.connect(write=True) as db:
 db.execute("INSERT OR REPLACE INTO settings VALUES ('adjustment_web_config',?)",(dumps(public),))
print(json.dumps({'program_backup':str(backup),'installed':len(files),'queue_enabled':False}))
