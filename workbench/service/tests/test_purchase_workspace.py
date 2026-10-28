import json
import uuid
import pytest
from core.store import dumps
from core.purchase_workspace import PurchaseWorkspace, PurchaseConflict
from core.purchase_workspace_data import WAREHOUSE, refresh_index, item_id, supplier_key
from test_daily_work import store, user

ACTOR=user('buyer',['purchase'])

def inventory(code='A',available='-20',physical='3'):
    return dict(id=code,profile_id='P',warehouse_id=WAREHOUSE,product_id=code+'p',sku_id=code+'s',base_unit='1',
        sku=code,product_name='GB/T 5783 六角螺栓',spec='M8*20',model='8.8',material='钢',stock_quantity=physical,available_stock=available)

def seed(store,items,receipts=(),version='1',orders=()):
    with store.connect(write=True) as db:
        db.execute('DELETE FROM records')
        for kind,rows in [('inventory',items),('purchases',receipts),('orders',orders)]:
            for i,r in enumerate(rows):
                db.execute('INSERT INTO records(kind,id,payload,sku) VALUES (?,?,?,?)',(kind,str(i),dumps(r),r['sku']))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)",(dumps(version),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_source_updated_at','\"2026-10-21T13:15:00+08:00\"')")

def receipt(product, supplier, date, **kw):
    return {**product,'quantity':'10','amount':'25','document_type':'进货入库单','party_id':supplier,'party':supplier,
        'source_bill_id':supplier+date,'source_line_id':product['sku'],'posted_at':date,**kw}

def body(engine,action,**kw):
    listing=engine.listing(ACTOR,{})
    item=listing['items'][0]
    return {'action':action,'source_token':listing['source_token'],'items':[{'id':item['id'],'version':item['version']}],
        'idempotency_key':str(uuid.uuid4()),**kw}

def test_shortage_is_negative_saleqty_not_double_deducted(store):
    p=inventory();seed(store,[p,inventory('B','0'),inventory('C','12')])
    engine=PurchaseWorkspace(store);r=engine.listing(ACTOR,{})
    assert r['total']==1
    x=r['items'][0]
    assert x['shortage']=='20' and x['physical']=='3' and x['review_reasons']
    assert x['inferred_transit']=='0' and x['inferred_other_reserved']=='23'
    with pytest.raises(PurchaseConflict):engine.listing(user('packer',['stock_packing']),{})

def test_latest_valid_supplier_manual_return_and_refresh(store):
    p=inventory();history=[receipt(p,'S1','2026-10-01'),receipt(p,'S2','2026-10-02'),receipt(p,'S3','2026-10-03',redword=True)]
    seed(store,[p],history);e=PurchaseWorkspace(store)
    x=e.listing(ACTOR,{})['items'][0];assert x['allocations'][0]['supplier_name']=='S2'
    b=body(e,'return');e.mutate(ACTOR,b);assert e.mutate(ACTOR,b)=={'changed':1,'action':'return'}
    seed(store,[inventory(available='-30')],history,'2');refresh_index(store)
    x=e.listing(ACTOR,{})['items'][0];assert x['unassigned']=='30' and x['decision']=='manual_return'
    assert not x['allocations']
    with pytest.raises(PurchaseConflict):e.mutate(ACTOR,{**b,'idempotency_key':str(uuid.uuid4())})
    e.mutate(ACTOR,body(e,'suggest'));assert e.listing(ACTOR,{})['items'][0]['arranged']=='30'

def test_same_date_different_supplier_no_guess(store):
    p=inventory();seed(store,[p],[receipt(p,'S1','2026-10-01'),receipt(p,'S2','2026-10-01')])
    e=PurchaseWorkspace(store);assert not e.listing(ACTOR,{})['items'][0]['allocations']

def test_split_atomic_conflict_and_purchase_duplicate(store):
    p=inventory();q=inventory('B');seed(store,[p,q],[receipt(p,'S','2026-10-01'),receipt(q,'S','2026-10-01')])
    e=PurchaseWorkspace(store);listing=e.listing(ACTOR,{})
    b=body(e,'return');b['items']=[{'id':x['id'],'version':x['version']} for x in listing['items']];b['items'][1]['version']+=1
    with pytest.raises(PurchaseConflict):e.mutate(ACTOR,b)
    assert all(x['arranged']=='20' for x in e.listing(ACTOR,{})['items'])
    e.mutate(ACTOR,body(e,'review_ack',note='确认该商品需要采购'))
    x=e.listing(ACTOR,{})['items'][0]
    b=body(e,'record_purchase',allocation_id=x['allocations'][0]['id'],quantity='20',amount='50',tax_basis='含税',
        platform_order_no='PO1',platform_line_no='1',pack_count='2',pack_size='10')
    e.mutate(ACTOR,b);assert e.mutate(ACTOR,b)['changed']==1
    assert e.detail(ACTOR,x['id'])['allocations'][0]['state']=='ordered'
    # Loss of source keeps purchased evidence and reservation visible.
    seed(store,[],version='2');refresh_index(store)
    kept=e.listing(ACTOR,{'view':'ordered'})['items'][0]
    assert kept['source_missing'] and kept['arranged']=='20'

def test_pack_mismatch_not_saved(store):
    p=inventory();seed(store,[p],[receipt(p,'S','2026-10-01')]);e=PurchaseWorkspace(store)
    e.mutate(ACTOR,body(e,'review_ack',note='人工核对需求'))
    x=e.listing(ACTOR,{})['items'][0]
    with pytest.raises(ValueError):e.mutate(ACTOR,body(e,'record_purchase',allocation_id=x['allocations'][0]['id'],quantity='20',amount='50',tax_basis='含税',
        platform_order_no='PO1',platform_line_no='1',pack_count='3',pack_size='10'))
    assert e.detail(ACTOR,x['id'])['allocations'][0]['state']=='draft'

def test_purchase_cookie_role_and_csrf(store):
    from app import create_app
    from types import SimpleNamespace
    from werkzeug.security import generate_password_hash
    with store.connect(write=True) as db:db.execute("UPDATE staff_users SET password_hash=?",(generate_password_hash('test-only'),))
    app=create_app({'TESTING':True,'AUTO_IMPORT':False,'SOURCE_MODE':'api','PUBLIC_MODE':True,
        'ERP_CLIENT':SimpleNamespace(last_auth_error=None),'API_SOURCE':object(),'ADMIN_PASSWORD_HASH':generate_password_hash('test-only'),'PURCHASE_WEB_ORIGIN':'http://localhost'},runtime_dir=store.runtime)
    c=app.test_client();base='/api/v2/purchase-workspace'
    assert c.get(base+'/items').status_code==401
    assert c.post(base+'/login',json={'username':'packer','password':'test-only'},headers={'Origin':'http://localhost'}).status_code==403
    r=c.post(base+'/login',json={'username':'buyer','password':'test-only'},headers={'Origin':'http://localhost'});assert r.status_code==200
    assert c.get(base+'/items').status_code==200 and c.get('/api/v2/tasks').status_code==401
    assert c.post(base+'/refresh',json={'idempotency_key':str(uuid.uuid4())}).status_code==403
    h={'Origin':'http://localhost','X-CSRF-Token':r.json['csrf']}
    assert c.post(base+'/refresh',json={'idempotency_key':str(uuid.uuid4())},headers=h).status_code==202
    assert c.post(base+'/refresh',json={'idempotency_key':str(uuid.uuid4())},headers={**h,'Origin':'https://evil.invalid'}).status_code==403


def test_refresh_waits_for_old_read_and_only_uses_refresh(store):
    from core.update_jobs import UpdateJobs
    e=PurchaseWorkspace(store);u=UpdateJobs(store,object());older=u.enqueue(str(uuid.uuid4()),'refresh')
    e.request_refresh(ACTOR,{'idempotency_key':str(uuid.uuid4())});e.tick_refresh(u)
    with store.connect() as db:assert e.refresh_status(db)['status']=='queued'
    with store.connect(write=True) as db:db.execute("UPDATE update_jobs SET status='succeeded' WHERE id=?",(older['id'],))
    e.tick_refresh(u)
    with store.connect() as db:
        r=e.refresh_status(db);assert r['job_id']!=older['id'] and r['status']=='running'
        assert db.execute('SELECT mode FROM update_jobs WHERE id=?',(r['job_id'],)).fetchone()[0]=='refresh'


def test_quote_preview_exact_match_and_atomic_apply(store):
    from core.purchase_workspace_files import quote_template,preview_quotes,apply_quotes
    from openpyxl import load_workbook
    from io import BytesIO
    p=inventory();seed(store,[p],[receipt(p,'S','2026-10-01')]);e=PurchaseWorkspace(store)
    w=load_workbook(quote_template());s=w.active;s.append(['A',2.5,'含税','',100,100,0,'样本']);s.append(['UNKNOWN',8,'含税'])
    out=BytesIO();w.save(out)
    preview=preview_quotes(e,ACTOR,out.getvalue(),'quote.xlsx',supplier_key('P','S'))
    assert not preview['rows'][0]['issues'] and preview['rows'][1]['issues']
    with pytest.raises(ValueError):apply_quotes(e,ACTOR,{'import_id':preview['id'],'rows':[2,3],'idempotency_key':str(uuid.uuid4())})
    b={'import_id':preview['id'],'rows':[2],'idempotency_key':str(uuid.uuid4())};apply_quotes(e,ACTOR,b);apply_quotes(e,ACTOR,b)
    quotes=e.detail(ACTOR,item_id(p))['quotes'];assert len(quotes)==1 and quotes[0]['payload']['unit_price']=='2.5'


def test_pdf_frozen_and_does_not_change_draft(store):
    from core.purchase_workspace_files import export_document
    from types import SimpleNamespace
    p=inventory();seed(store,[p],[receipt(p,'S','2026-10-01')]);e=PurchaseWorkspace(store)
    b=body(e,'unused',kind='inquiry',supplier_id=supplier_key('P','S'));b.pop('action')
    result=export_document(e,ACTOR,b,SimpleNamespace());again=export_document(e,ACTOR,b,SimpleNamespace());assert result['id']==again['id']
    assert (store.runtime/'purchase-documents'/(result['id']+'.pdf')).read_bytes().startswith(b'%PDF')
    assert e.listing(ACTOR,{})['items'][0]['allocations'][0]['state']=='draft'


def test_company_template_requires_confirmed_quote_and_freezes_prices(store):
    from core.purchase_workspace_files import export_document
    from types import SimpleNamespace
    from pypdf import PdfReader
    p=inventory();seed(store,[p],[receipt(p,'S','2026-10-01')]);e=PurchaseWorkspace(store)
    e.mutate(ACTOR,body(e,'review_ack',note='已核对需求'))
    b=body(e,'unused',kind='purchase',supplier_id=supplier_key('P','S'),allocation_ids=[e.detail(ACTOR,item_id(p))['allocations'][0]['id']]);b.pop('action')
    with pytest.raises(ValueError,match='采纳报价'):export_document(e,ACTOR,b,SimpleNamespace())
    e.mutate(ACTOR,body(e,'quote',supplier_id=supplier_key('P','S'),unit_price='2',tax_basis='未税',note='内部客户不应输出'))
    x=e.detail(ACTOR,item_id(p));q=x['quotes'][0]
    e.mutate(ACTOR,body(e,'adopt_quote',quote_id=q['id'],allocation_id=x['allocations'][0]['id']))
    b=body(e,'unused',kind='purchase',supplier_id=supplier_key('P','S'),allocation_ids=[e.detail(ACTOR,item_id(p))['allocations'][0]['id']]);b.pop('action')
    result=export_document(e,ACTOR,b,SimpleNamespace())
    pdf=PdfReader(store.runtime/'purchase-documents'/(result['id']+'.pdf'))
    content=''.join(p.extract_text() for p in pdf.pages)
    assert len(pdf.pages)==2 and '45.20' in content and 'TERMS AND CONDITIONS' in content
    assert '内部客户不应输出' not in content and 'LS3700' not in content and 'JHD-20261021-007' not in content
    with store.connect() as db:
        snapshot=json.loads(db.execute('SELECT snapshot FROM purchase_workspace_documents WHERE id=?',(result['id'],)).fetchone()[0])
    assert snapshot['tax_total']=='45.20' and snapshot['lines'][0]['tax_unit_price']=='2.26'
    assert e.detail(ACTOR,item_id(p))['allocations'][0]['state']=='draft'


def test_only_nonstandard_drawings_attached_and_missing_blocks(store,tmp_path):
    from core.purchase_workspace_files import export_document
    from types import SimpleNamespace
    from PIL import Image
    from pypdf import PdfReader
    p=inventory();p['has_image']=True
    seed(store,[p],[receipt(p,'S','2026-10-01')]);e=PurchaseWorkspace(store)
    b=body(e,'unused',kind='inquiry',supplier_id=supplier_key('P','S'));b.pop('action')
    # Standard items do not invoke drawing retrieval, even when they have a picture.
    assert export_document(e,ACTOR,b,SimpleNamespace())['images']==0
    e.mutate(ACTOR,body(e,'classify',category='螺栓',kind='非标件'))
    b=body(e,'unused',kind='inquiry',supplier_id=supplier_key('P','S'));b.pop('action')
    with pytest.raises(ValueError,match='图纸'):export_document(e,ACTOR,b,SimpleNamespace())
    image=tmp_path/'drawing.png';Image.new('RGB',(100,100),'white').save(image)
    images=SimpleNamespace(list_for_product=lambda _: {'items':[{'id':'1'},{'id':'2'}]},content=lambda _:SimpleNamespace(path=image))
    result=export_document(e,ACTOR,b,images)
    assert result['images']==2
    assert len(PdfReader(store.runtime/'purchase-documents'/(result['id']+'.pdf')).pages)==3


def test_manual_erp_confirmation_requires_refresh_exact_quantity_and_consent(store):
    p=inventory();h=[receipt(p,'S','2026-10-01')];seed(store,[p],h);e=PurchaseWorkspace(store)
    e.mutate(ACTOR,body(e,'review_ack',note='已核对需求'))
    a=e.listing(ACTOR,{})['items'][0]['allocations'][0]
    e.mutate(ACTOR,body(e,'record_purchase',allocation_id=a['id'],quantity='20',amount='50',tax_basis='含税',platform_order_no='P1',platform_line_no='1'))
    args=dict(allocation_id=a['id'],erp_order_no='ERP1',erp_line_no='1',erp_quantity='20',erp_counted_confirmed=True)
    with pytest.raises(PurchaseConflict,match='刷新'):e.mutate(ACTOR,body(e,'confirm_erp_counted',**args))
    seed(store,[inventory(available='-10')],h,version='2');refresh_index(store)
    x=e.listing(ACTOR,{})['items'][0]
    assert x['arranged']=='20' # A new snapshot alone is never proof of auditing.
    with pytest.raises(ValueError,match='确认'):e.mutate(ACTOR,body(e,'confirm_erp_counted',**{**args,'erp_counted_confirmed':False}))
    with pytest.raises(ValueError,match='数量'):e.mutate(ACTOR,body(e,'confirm_erp_counted',**{**args,'erp_quantity':'19'}))
    b=body(e,'confirm_erp_counted',**args);e.mutate(ACTOR,b);e.mutate(ACTOR,b)
    x=e.listing(ACTOR,{})['items'][0]
    assert x['arranged']=='0' and x['unassigned']=='10'
    assert x['allocations'][0]['details']['erp_counted_method']=='human_verified'


def test_formal_pdf_exports_only_explicit_allocations(store):
    from core.purchase_workspace_files import export_document
    from types import SimpleNamespace
    p=inventory();seed(store,[p],[receipt(p,'S','2026-10-01')]);e=PurchaseWorkspace(store)
    e.mutate(ACTOR,body(e,'review_ack',note='已核对需求'))
    a=e.detail(ACTOR,item_id(p))['allocations'][0]
    e.mutate(ACTOR,body(e,'split',allocation_id=a['id'],quantity='10',supplier_id=supplier_key('P','S')))
    e.mutate(ACTOR,body(e,'quote',supplier_id=supplier_key('P','S'),unit_price='2',tax_basis='含税'))
    q=e.detail(ACTOR,item_id(p))['quotes'][0]
    e.mutate(ACTOR,body(e,'adopt_quote',allocation_id=a['id'],quote_id=q['id']))
    b=body(e,'unused',kind='purchase',supplier_id=supplier_key('P','S'),allocation_ids=[a['id']]);b.pop('action')
    result=export_document(e,ACTOR,b,SimpleNamespace())
    with store.connect() as db:s=json.loads(db.execute('SELECT snapshot FROM purchase_workspace_documents WHERE id=?',(result['id'],)).fetchone()[0])
    assert len(s['lines'])==1 and s['lines'][0]['quantity']=='10' and s['tax_total']=='20.00'
