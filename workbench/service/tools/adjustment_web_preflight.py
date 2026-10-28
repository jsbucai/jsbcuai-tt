"""Read-only login, original XLS validation and exact loss/overflow type selection.
Never clicks upload/confirm; the placeholder workbook is only staged locally.
"""
import fcntl,json,os,sys,uuid
from datetime import date
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.adjustment_web_import import workbook,AdjustmentBrowser
from core.sales_web_transport import PlaywrightUploadSelectors
from tools.sales_web_operation_worker import _private_config,_host_scope,_web_identity
from tools.erp_human_challenge import wait_login

config=_private_config(Path(sys.argv[1]));host=_host_scope(config)
sys.path.insert(0,config['collector_source_path'])
from purchase_export_collector.collector import _login,_read_credentials,_account_scope
from playwright.sync_api import sync_playwright
runtime=Path('/opt/ul-workbench/service/runtime/data')
with (runtime/'.web-browser-lane.lock').open('a+') as lock:
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('heavy_lane_busy')
    credentials=_read_credentials(Path(config['credentials_path']))
    assert (credentials['company'],credentials['username'])==(host.tenant_name,host.operator_name)
    root=Path(config['adjustment_browser_profile']).parent
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    batch={'id':str(uuid.uuid4()),'created_at':date.today().isoformat(),'items':[{'sku':'UL-PREFLIGHT-NOT-UPLOADED','quantity':'1','note':'不上传的本地预检'}]}
    binding={'maker_name':host.maker_name,'warehouse_name':host.warehouse_name}
    results=[]
    with sync_playwright() as pw:
        context=pw.chromium.launch_persistent_context(config['adjustment_browser_profile'],headless=False,
            executable_path=config['chromium_path'],viewport={'width':1920,'height':930})
        try:
            page=context.pages[0];page.set_default_timeout(15000)
            page=_login(page,credentials,wait_for_human=True,
                human_handler=lambda p:wait_login(p,batch['id'],lambda value:print(value,flush=True)))
            assert _account_scope(credentials['company'],page.url)==host.account_id
            browser=AdjustmentBrowser(page,PlaywrightUploadSelectors(),identity=lambda:_web_identity(page,host))
            assert browser.account_identity()==(host.profile_id,host.account_id)
            for kind in ('loss','overflow'):
                batch['type']=kind
                path=root/('preflight-'+kind+'.xls');path.write_bytes(workbook(Path(config['template_path']),batch,'UL-PREFLIGHT',binding));os.chmod(path,0o600)
                browser.stage_adjustment(path,kind)
                results.append({'kind':kind,'type_value':browser._one(browser.selectors.document_type_selector).input_value(),'staged':True,'upload_clicked':False})
                browser._close_import_dialogs()
            print(json.dumps({'login_verified':True,'operator':host.operator_name,'warehouse':host.warehouse_name,'results':results},ensure_ascii=False),flush=True)
        finally:context.close()
