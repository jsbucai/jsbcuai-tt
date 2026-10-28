"""Install a checked additive release; never restores or replaces the database."""
from pathlib import Path
import hashlib,json,os,re,shutil,subprocess,sys,time,urllib.request

source=Path(sys.argv[1]).resolve();root=Path('/opt/ul-workbench')
manifest=json.loads((source/'manifest.json').read_text())
def run(args,**kw):return subprocess.run(args,check=True,**kw)
def output(args):return subprocess.check_output(args,text=True).strip()
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
assert os.geteuid()==0
assert output(['docker','inspect','--format','{{.Config.Image}}','ul-workbench-workbench-1'])==manifest['base_image']
for rel,expected in manifest['originals'].items():
    if digest(root/rel)!=expected:raise RuntimeError('Live source drift: '+rel)
for rel,expected in manifest['files'].items():
    if digest(source/rel)!=expected:raise RuntimeError('Package hash mismatch: '+rel)
assert output(['systemctl','show','ul-backup.service','-p','Result','--value'])=='success'
run(['docker','build','--network=none','-t',manifest['image'],str(source)])
run(['docker','run','--rm','--network=none','--entrypoint','python',manifest['image'],'-c',
     'import app; import core.sales_workspace; import core.sales_api_jobs; import tools.sales_api_worker'])
timers=['ul-sales-codex.timer','ul-sales-new-product.timer','ul-sales-api.timer','ul-sales-customers.timer']
active=[timer for timer in timers if subprocess.run(['systemctl','is-active','--quiet',timer]).returncode==0]
installed=[timer for timer in timers if output(['systemctl','show',timer,'-p','LoadState','--value'])=='loaded']
if installed:run(['systemctl','stop',*installed])
try:
    for service in ['ul-sales-codex.service','ul-sales-new-product.service','ul-sales-api.service','ul-sales-customers.service']:
        if subprocess.run(['systemctl','is-active','--quiet',service]).returncode==0:raise RuntimeError('Worker still active: '+service)
    backup=root/'deploy-backups'/('sales-workspace-'+time.strftime('%Y%m%dT%H%M%S'))
    backup.mkdir(parents=True,mode=0o700)
    for rel in [*manifest['files'],'compose.yaml']:
        target=root/rel
        if target.exists():
            destination=backup/rel;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,destination)
    for rel in manifest['files']:
        destination=root/rel;destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_name(destination.name+'.sales-new')
        shutil.copyfile(source/rel,temporary);temporary.chmod(0o644);os.replace(temporary,destination)
    for unit in ['ul-sales-api.service','ul-sales-api.timer','ul-sales-customers.service','ul-sales-customers.timer']:
        shutil.copyfile(source/'deploy'/unit,Path('/etc/systemd/system')/unit)
    override=Path('/etc/systemd/system/ul-sales-codex.service.d');override.mkdir(exist_ok=True)
    shutil.copyfile(source/'deploy/sales-codex-app-server.conf',override/'workspace.conf')
    compose=root/'compose.yaml';text=compose.read_text()
    assert 'image: '+manifest['base_image'] in text
    compose.write_text(text.replace('image: '+manifest['base_image'],'image: '+manifest['image'],1))
    run(['docker','compose','up','-d','--no-deps','--no-build','workbench'],cwd=root)
    for attempt in range(40):
        try:
            with urllib.request.urlopen('http://192.168.100.31:5191/livez',timeout=2) as response:
                assert response.status==200
            break
        except Exception:time.sleep(1)
    else:raise RuntimeError('New service did not become healthy; original program backup retained')
    # Configuration is additive and grounded in the user-verified sample.
    sys.path.insert(0,str(root/'service'))
    from core.store import Store,dumps
    from tools.sales_codex_runner import _rules
    from tools.sales_codex_worker import _database_identity,_ServiceIdentityStore
    _,base_hash=_rules(root/'deploy/skills/ul-sales-order')
    runtime=root/'service/runtime/data';info=(runtime/'workbench.sqlite3').stat()
    with _database_identity(info.st_uid,info.st_gid):store=Store(runtime)
    with _ServiceIdentityStore(store).connect(write=True) as db:
        for key,value in {
          'sales_workspace_owner_id':'admin','sales_rules_base_hash':base_hash,
          'sales_api_policy':{'enabled':True,'contract_accepted':True,'profile_id':'27056512834386351',
             'owner_user_id':'admin','sample_number':'UT02EE58ACAD4D57CE76','sample_vchcode':'2861532290583552305',
             'accepted_at':'2026-10-20','acceptance':'用户核对全部正确，创建时未审核','browser_enabled':False}
        }.items():db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,dumps(value)))
        from core.sales_customer_directory import ensure as ensure_customers
        ensure_customers(db)
        db.execute("INSERT OR IGNORE INTO sales_erp_customer_sync(profile_id) VALUES ('27056512834386351')")
        db.execute("UPDATE sales_erp_customer_sync SET requested=1 WHERE profile_id='27056512834386351'")
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','--now','ul-sales-api.timer','ul-sales-customers.timer'])
    print(json.dumps({'image':manifest['image'],'program_backup':str(backup),'base_rules_sha256':base_hash,
                      'api_enabled':True,'browser_enabled':False},ensure_ascii=False))
finally:
    if active:run(['systemctl','start',*active])
