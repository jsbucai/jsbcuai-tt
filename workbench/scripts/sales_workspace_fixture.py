"""Local synthetic UI fixture. Never connects to production or ERP."""
from pathlib import Path
import tempfile,sys,argparse
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'service'))
from app import create_app
from core.store import dumps
from werkzeug.security import generate_password_hash
from waitress import serve
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=5199);port=p.parse_args().port
with tempfile.TemporaryDirectory(dir=root/'service'/'runtime',prefix='sales-ui-') as runtime:
 app=create_app({'TESTING':True,'AUTO_IMPORT':False,'SOURCE_MODE':'api','PUBLIC_MODE':True,
  'ADMIN_PASSWORD_HASH':generate_password_hash('demo-local-only'),'ERP_CLIENT':SimpleNamespace(last_auth_error=None),
  'API_SOURCE':object(),'SALES_WEB_ORIGIN':'http://127.0.0.1:'+str(port)},runtime_dir=runtime)
 store=app.extensions['service']['store'];engine=app.extensions['service']['sales_assistant']
 from core.sales_customer_directory import refresh
 examples=[{'id':str(i),'fullname':name,'usercode':'C'+str(i),'namepy':'SHILI'} for i,name in enumerate(
  ['示例设备有限公司','示例机械有限公司']+['未指定示例客户'+str(n) for n in range(64)],1)]
 refresh(store,SimpleNamespace(account=lambda:'P',pages=lambda *a,**k:examples),'P')
 with store.connect(write=True) as db:
  db.execute("INSERT INTO staff_users VALUES ('sales','sales','李瑄',?,'[\"sales\"]',0,1,3,'','')",(generate_password_hash('unusable-demo'),))
  db.execute("INSERT INTO staff_users VALUES ('sales2','sales2','刘锋',?,'[\"sales\"]',0,1,3,'','')",(generate_password_hash('unusable-demo'),))
  for customer in ['示例设备有限公司','示例机械有限公司']:
   db.execute("INSERT INTO sales_customer_owners (profile_id,customer_name,owner_id,status,source,updated_at) VALUES ('P',?,'sales','assigned','customer_confirmed','')",(customer,))
  db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version','\"fixture-v1\"')")
  for i,code in enumerate(['LS1000','LS1001','LS1002']):
   item={'profile_id':'P','warehouse_id':'29871293290508390','product_id':str(500+i),'sku_id':str(600+i),'base_unit':'1','unit_name':'支','sku':code,'product_name':'六角头螺栓','spec':['M6*30','M8*40','M10*60'][i],'model':'8.8级镀锌'}
   db.execute("INSERT INTO records(kind,id,sku,payload) VALUES ('inventory',?,?,?)",('I'+str(i),code,dumps(item)))
 actor={'id':'admin','is_admin':True,'active':True,'roles':[]}
 s=engine.create(actor,{'profile_id':'P','customer_name':'示例设备有限公司','handler_name':'李瑄','idempotency_key':'create'})['session']
 s=engine.edit(actor,s['id'],{'version':s['version'],'idempotency_key':'edit','handler_name':'李瑄','customer_order_no':'PO-20261020','delivery_date':'2026-12-20','tax_basis':'tax_inclusive','tax_rate':13,'lines':[{'product_code':c,'quantity':q,'unit_price':p,'unit':'支'} for c,q,p in [('LS1000','200','0.28'),('LS1001','120','0.65'),('LS1002','80','1.25')]],'note':'分批送货前请联系'} )['session']
 with store.connect(write=True) as db:
  db.execute("INSERT INTO sales_assistant_messages VALUES ('M1',?,'user',?,'','2026-10-20T10:00:00')",(s['id'],'这批螺栓请按表格规格整理，报价均含税，交期 12 月 20 日。'))
  db.execute("INSERT INTO sales_assistant_messages VALUES ('M2',?,'assistant',?,'','2026-10-20T10:00:05')",(s['id'],'已整理 3 行明细。商品编码、数量和报价已列在右侧，请核对交期后确认。'))
 print('Synthetic UI ready at http://127.0.0.1:'+str(port)+'/sales',flush=True)
 serve(app,host='127.0.0.1',port=port,threads=8)
