"""Install reviewed host-only entry point after a separate consistent NAS backup.

Run as root with release archive extracted in the supplied source directory.
Does not restart the business container or change ERP/browser policy settings.
"""
import hashlib,json,os,shutil,sqlite3,sys
from datetime import datetime,timezone
from pathlib import Path

os.umask(0o077)
source=Path(sys.argv[1]);root=Path('/opt/ul-workbench')
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup=Path('/var/backups/ul-weixin-sales')/stamp;backup.mkdir(parents=True,mode=0o700)
files={
 'service/core/weixin_sales_bridge.py':root/'service/core/weixin_sales_bridge.py',
 'service/tools/weixin_sales_worker.py':root/'service/tools/weixin_sales_worker.py',
 'service/tools/weixin_sales_media.py':root/'service/tools/weixin_sales_media.py',
 'service/tools/weixin_sales_spool.py':root/'service/tools/weixin_sales_spool.py',
 'service/tools/weixin_guardian.py':Path('/opt/ul-guardian/weixin_guardian.py'),
 'deploy/ul-weixin-sales.service':Path('/etc/systemd/system/ul-weixin-sales.service'),
 'deploy/ul-weixin-notify.service':Path('/etc/systemd/system/ul-weixin-notify.service'),
 'deploy/ul-guardian.service':Path('/etc/systemd/system/ul-guardian.service'),
}
records=[]
for relative,target in files.items():
    old=backup/relative
    if target.exists():old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,old)
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.wx-new')
    shutil.copyfile(source/relative,temporary);temporary.chmod(0o644);os.replace(temporary,target)
    records.append({'path':str(target),'previous':str(old) if old.exists() else None,
                    'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
shutil.copyfile(source/'service/tools/weixin_sales_spool.py','/opt/ul-guardian/weixin_sales_spool.py')
os.chmod('/opt/ul-guardian/weixin_sales_spool.py',0o644)
(backup/'manifest.json').write_text(json.dumps(records,indent=2))
configroot=Path('/etc/ul-weixin-sales');configroot.mkdir(mode=0o700,exist_ok=True)
config=configroot/'config.json'
account=json.loads(Path('/etc/ul-weixin-notify/account.json').read_text())
binding=hashlib.sha256(json.dumps([account['ilink_user_id'],account['ilink_bot_id']],
    ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
if config.exists():
    old=json.loads(config.read_text())
    if old.get('binding')!=binding:raise SystemExit('existing owner binding mismatch; no rebind performed')
else:
    config.write_text(json.dumps({'enabled':True,'actor_id':'admin','profile_id':'27056512834386351',
        'binding':binding,'runtime':'/opt/ul-workbench/service/runtime/data','spool':'/var/lib/ul-weixin-sales'},indent=2))
    config.chmod(0o600)
Path('/var/lib/ul-weixin-sales').mkdir(mode=0o700,exist_ok=True)
print(json.dumps({'installed_files':len(records)+1,'program_backup':str(backup),'configuration_private':True}))
