import base64
import io
import json
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app
from core.importers import Importer
from core.store import guarded_runtime, dumps
from core.print_jobs import data_version
from core.mobile_cpcl import order_cpcl
from core.fields import order_fields


def sheet(path, rows, title='导出数据'):
    book = Workbook()
    book.active.title = title
    for row in rows: book.active.append(row)
    book.save(path)
    book.close()


@pytest.fixture
def app():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='test-', dir=ROOT / 'runtime') as temp:
        runtime = Path(temp)
        snapshots = runtime / 'snapshots'
        snapshots.mkdir()
        orders = [['订单编号','发货识别号','买家账号','商品名称','商品编号','数量','系统备注','处理状态','填充1','填充2','物流公司','型号']]
        sales = [['订单编号','商品编号']]
        for index, company in enumerate(['LS库存件','LD库存件','LM库存件','DQ库存件','QT库存件','来货件','需采购']):
            note = f'PO{index}___#JOB1___#1___#图纸___#六角 螺栓___#DIN933__M12*40___#8.8___#白锌___#12.5___#LS0001'
            orders.append([f'PO{index}',f'SHIP{index}','客户甲','螺栓','LS0001',12.5,note,'待发货','','',company,'10.9级达克罗' if index == 0 else ''])
            sales.append([f'PO{index}','LS0001'])
        orders.append(['OMISSING','','客户乙','测试','MISSING',1,'','待发货','','','来货件'])
        sales.append(['OMISSING','MISSING'])
        orders.append(['EXCLUDE','','','测试','LS0001',1000,'','待发货','','','LS库存件'])
        sheet(snapshots/'orders.xls', orders, '导出记录')
        sheet(snapshots/'sales.xls', sales)
        sheet(snapshots/'inventory.xls', [['商品编号','商品名称','库存数量','可销售库存','规格','型号','材质','货位','标准号'],['LS0001','螺栓',100,0,'M12*40','8.8','钢','A01','DIN933']])
        yield create_app({'TESTING': True}, runtime_dir=runtime)


def test_orders_stock_companies_zero_missing_and_version(app):
    c = app.test_client()
    all_rows = c.get('/api/v2/orders?q=LS0001').get_json()
    assert all_rows['count'] == 7 and all_rows['total_quantity'] == 87.5
    assert all_rows['data_version'] and all_rows['inventory']['available_stock'] == '0'
    row = all_rows['items'][0]
    assert row['inventory']['found'] is True and row['specification'] == 'DIN933  M12*40'
    stock = c.get('/api/v2/orders?q=LS0001&scope=stock').get_json()
    assert stock['count'] == 5
    assert c.get('/api/v2/orders?q=LS0001&scope=stock&logistics_company=DQ库存件').get_json()['count'] == 1
    assert c.get('/api/v2/orders?q=LS0001&scope=all&logistics_company=需采购').get_json()['count'] == 1
    assert c.get('/api/v2/orders?q=LS0001&scope=all&logistics_company=来货件').get_json()['count'] == 1
    assert c.get('/api/v2/orders?scope=all&logistics_company=需采购').get_json()['count'] == 1
    assert c.get('/api/v2/orders?q=LS0001&scope=stock&logistics_company=需采购').status_code == 400
    assert c.get('/api/v2/orders?q=LS0001&logistics_company=unknown').status_code == 400
    missing = c.get('/api/v2/orders?q=MISSING').get_json()['items'][0]['inventory']
    assert missing['found'] is False and missing['available_stock'] == ''
    assert c.get('/api/v2/health').get_json()['capabilities']['physical_print'] is False


def test_order_model_comes_from_order_column_not_inventory_or_note_grade(app):
    result = app.test_client().get('/api/v2/orders?q=LS0001').get_json()
    source_model, blank_model = result['items'][:2]
    assert source_model['model'] == '10.9级达克罗'
    assert source_model['inventory']['model'] == '8.8'
    assert order_fields(source_model)['grade'] == '8.8'
    assert blank_model['model'] == ''  # Do not manufacture an order model from a fallback.
    assert blank_model['inventory']['model'] == '8.8'


def test_inventory_batch_http_contract_is_idempotent_device_scoped_and_async(app):
    client = app.test_client()
    payload = {'id': str(uuid.uuid4()), 'client_id': str(uuid.uuid4()), 'type': 'loss',
               'items': [{'sku': 'LS0001', 'name': '螺栓', 'quantity': '2.5', 'note': '测试不上传'}]}
    created = client.post('/api/v2/adjustment-batches', json=payload)
    assert created.status_code == 200
    batch = created.get_json()['batch']
    assert batch['status'] == 'queued' and batch['items'][0]['quantity'] == '2.5'
    assert app.extensions['service']['adjustments'].thread is None  # No real worker/ERP in tests.
    assert client.post('/api/v2/adjustment-batches', json=payload).get_json()['batch'] == batch
    assert client.get('/api/v2/adjustment-batches', query_string={'client_id': payload['client_id']}).get_json()['batches'] == [batch]
    path = f"/api/v2/adjustment-batches/{payload['id']}"
    assert client.get(path, query_string={'client_id': payload['client_id']}).get_json()['batch'] == batch
    assert client.get(path, query_string={'client_id': str(uuid.uuid4())}).status_code == 404
    assert client.post(path + '/retry', json={'client_id': payload['client_id']}).status_code == 409
    assert client.post('/api/v2/adjustment-batches', json={**payload, 'type': 'overflow'}).status_code == 409
    assert client.post('/api/v2/adjustment-batches', json=payload, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert client.post('/api/v2/adjustment-batches', json=payload, environ_overrides={'REMOTE_ADDR': '8.8.8.8'}).status_code == 403
    with app.extensions['service']['store'].connect() as db:
        assert db.execute('SELECT COUNT(*) FROM adjustment_batches').fetchone()[0] == 1


def test_inventory_sku_is_exact_while_general_keyword_remains_broad(app):
    store = app.extensions['service']['store']
    with store.connect(write=True) as db:
        for sku in ('LS00010', 'XLS0001'):
            db.execute('INSERT INTO records(kind,id,payload,sku,keywords) VALUES (?,?,?,?,?)',
                ('inventory', sku, dumps({'id': sku, 'sku': sku, 'product_name': '螺栓', 'stock_quantity': '1'}), sku, sku.lower()))
    client = app.test_client()
    exact = client.get('/api/v2/inventory', query_string={'sku': ' ls0001 '}).get_json()
    assert exact['total'] == 1 and exact['items'][0]['sku'] == 'LS0001'
    assert client.get('/api/v2/inventory?q=LS0001').get_json()['total'] == 3


def test_confirmation_endpoint_only_changes_the_queried_batch_after_explicit_review(app):
    client = app.test_client()
    value = {'id': str(uuid.uuid4()), 'client_id': str(uuid.uuid4()), 'type': 'overflow', 'items': [{'sku': 'LS0001', 'quantity': '1'}]}
    client.post('/api/v2/adjustment-batches', json=value)
    path = '/api/v2/adjustment-batches/' + value['id'] + '/confirm'
    assert client.post(path, json={'client_id': value['client_id']}).status_code == 400
    assert client.post(path, json={'client_id': value['client_id'], 'confirmed': True}).status_code == 409
    with app.extensions['service']['store'].connect(write=True) as db:
        db.execute("UPDATE adjustment_batches SET status='unknown' WHERE id=?", (value['id'],))
    done = client.post(path, json={'client_id': value['client_id'], 'confirmed': True}).get_json()['batch']
    assert done['status'] == 'succeeded' and done['completion_source'] == 'user_verified'
    assert client.post(path, json={'client_id': value['client_id'], 'confirmed': True}).get_json()['batch'] == done


@pytest.fixture
def combined_inventory(app):
    store = app.extensions['service']['store']
    rows = [['商品编号','商品名称','库存数量','可销售库存','规格','型号','材质','货位','标准号','状态'],
            ['A100','六角螺栓',100,0,'M12*40','8.8级发黑','40Cr','A01','DIN933','启用'],
            ['B100','六角螺栓',20,7,'M12*50','8.8级发黑','40Cr','B01','DIN933','启用'],
            ['A102','六角螺母',30,3,'M12','8.8级发黑','40Cr','A02','DIN934','启用'],
            ['A103','六角螺栓',40,4,'M16*40','8.8级发黑','40Cr','A03','DIN933','启用'],
            ['A104','六角螺栓',50,5,'M12*40','10.9级发黑','40Cr','A04','DIN933','启用'],
            ['A105','六角螺栓',60,6,'M12*40','8.8级发黑','304','A05','DIN933','启用']]
    sheet(store.snapshots / 'inventory.xls', rows)
    app.extensions['service']['importer'].run('inventory')
    return app, {'name': ' 六角 螺栓 ', 'spec': 'm12', 'model': '8.8', 'material': '40cr'}


def test_inventory_and_filters_paginate_aggregate_and_print_same_full_range(combined_inventory):
    app, filters = combined_inventory
    client = app.test_client()
    first = client.get('/api/v2/inventory', query_string={**filters, 'page_size':1}).get_json()
    second = client.get('/api/v2/inventory', query_string={**filters, 'page':2, 'page_size':1}).get_json()
    assert first['total'] == second['total'] == 2
    assert first['total_stock'] == second['total_stock'] == 120
    assert first['total_available'] == second['total_available'] == 7
    assert [first['items'][0]['sku'], second['items'][0]['sku']] == ['A100', 'B100']
    requested = {**filters, 'page':2, 'page_size':1}
    body = {'kind':'inventory', 'all':True, 'query':requested, 'expected_data_version':first['data_version']}
    printed = client.post('/api/v2/print/jobs', json=body).get_json()
    assert printed['labels'] == 2
    assert [page['fields']['sku'] for page in printed['pages']] == ['A100','B100']
    assert all('EG ' not in base64.b64decode(page).decode('gb18030') for page in printed['cpcl_pages'])
    # q is an additional constraint, not a replacement for the other four filters.
    narrowed = client.get('/api/v2/inventory', query_string={**filters, 'q':'B100'}).get_json()
    assert narrowed['total'] == 1 and narrowed['items'][0]['sku'] == 'B100'
    narrow_print = client.post('/api/v2/print/jobs', json={**body, 'query':{**filters, 'q':'B100'}}).get_json()
    assert narrow_print['labels'] == 1 and narrow_print['pages'][0]['fields']['sku'] == 'B100'
    conflicting = client.get('/api/v2/inventory', query_string={**filters, 'q':'A104'}).get_json()
    assert conflicting['total'] == 0
    assert client.post('/api/v2/print/jobs', json={**body, 'query':{**filters, 'q':'A104'}}).status_code == 400
    available = client.post('/api/v2/print/jobs', json={**body, 'query':{**filters, 'only_available':True}}).get_json()
    assert available['labels'] == 1 and available['pages'][0]['fields']['sku'] == 'B100'
    selected = client.post('/api/v2/print/jobs', json={'kind':'inventory', 'item_ids':[second['items'][0]['id']], 'expected_data_version':first['data_version']}).get_json()
    assert selected['labels'] == 1 and selected['pages'][0]['fields']['sku'] == 'B100'


def test_all_inventory_rejects_unbounded_queries_and_keeps_order_guard(combined_inventory):
    app, filters = combined_inventory
    client, store = app.test_client(), app.extensions['service']['store']
    body = {'kind':'inventory', 'all':True, 'expected_data_version':data_version(store)}
    for query in ({}, {'q':'', 'name':' ', 'spec':'\t', 'model':'', 'material':''},
                  {'q':'___'}, {'page':2, 'page_size':1}, {'unknown':'yes'},
                  {'only_available':False}, {'only_available':'false'}):
        response = client.post('/api/v2/print/jobs', json={**body, 'query':query})
        assert response.status_code == 400 and '当前查询条件' in response.get_json()['message']
    # Structural fields only are a new inventory capability, not a broader order range.
    assert client.post('/api/v2/print/jobs', json={**body, 'kind':'order', 'query':filters}).status_code == 400
    source = store.rows('inventory')[0]
    app.extensions['service']['importer'].save_records('inventory', [{**source, 'id':f'bulk-{n}', 'sku':f'BULK{n}'} for n in range(101)])
    over = client.post('/api/v2/print/jobs', json={**body, 'query':filters})
    assert over.status_code == 400 and '100' in over.get_json()['message']


def test_prepare_fast_split_selected_scope_and_data_version(app):
    c = app.test_client()
    result = c.get('/api/v2/orders?q=LS0001').get_json()
    body = {'kind':'order','item_ids':[result['items'][0]['id']], 'expected_data_version':result['data_version'], 'scope':'stock', 'logistics_company':'LS库存件','max_per_package':5}
    prepared = c.post('/api/v2/print/jobs',json=body).get_json()
    assert prepared['status']=='prepared' and prepared['labels']==3 and prepared['data_version']==result['data_version']
    assert prepared['cpcl_bytes'] < 9000 and prepared['mobile_render_mode']=='text'
    assert [page['packageInfo']['packageQuantity'] for page in prepared['pages']]==[5,5,2.5]
    page=base64.b64decode(prepared['cpcl_pages'][0]).decode('gb18030')
    assert 'EG ' not in page and 'VTEXT 55 0 174 604 DIN933  M12*40' in page and 'MA,SHIP0' in page
    assert c.post('/api/v2/print/jobs',json={**body,'expected_data_version':'old'}).status_code==409
    assert c.post('/api/v2/print/jobs',json={**body,'expected_data_version':''}).status_code==400
    assert c.post('/api/v2/print/jobs',json={**body,'logistics_company':'DQ库存件'}).status_code==400
    assert c.post('/api/v2/print/jobs',json={**body,'item_ids':[result['items'][0]['id']]*2}).status_code==400


def test_all_current_scope_inventory_and_function_codes(app):
    c=app.test_client()
    version=c.get('/api/v2/health').get_json()['data_version']
    result=c.post('/api/v2/print/jobs',json={'kind':'order','all':True,'query':{'q':'LS0001','scope':'stock','logistics_company':'LM库存件'},'expected_data_version':version}).get_json()
    assert result['labels']==1 and result['pages'][0]['fields']['qrValue']=='SHIP2'
    nonstock=c.post('/api/v2/print/jobs',json={'kind':'order','all':True,'query':{'q':'LS0001','scope':'all','logistics_company':'需采购'},'expected_data_version':version}).get_json()
    assert nonstock['labels']==1 and nonstock['pages'][0]['fields']['qrValue']=='SHIP6'
    inv=c.get('/api/v2/inventory?q=LS0001').get_json()
    job=c.post('/api/v2/print/jobs',json={'kind':'inventory','item_ids':[inv['items'][0]['id']],'expected_data_version':version}).get_json()
    assert job['cpcl_bytes']<1500 and 'MA,LS0001' in base64.b64decode(job['cpcl_pages'][0]).decode('gb18030')
    code=c.post('/api/v2/print/jobs',json={'kind':'function_codes'}).get_json()
    raw=base64.b64decode(code['cpcl_pages'][0])
    assert b'MA,PALL01' in raw and b'MA,PBOX01' in raw and len(raw)<1000


def test_claim_is_atomic_and_results_never_send_hardware(app,monkeypatch):
    import socket,subprocess
    def forbidden(*a,**k): raise AssertionError('hardware/process operation attempted')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    c=app.test_client()
    job=c.post('/api/v2/print/jobs',json={'kind':'function_codes'}).get_json()
    route='/api/v2/print/jobs/'+job['job_id']
    body={'user_initiated':True,'idempotency_key':'one-native-action-1234','transport':'native'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs=list(pool.map(lambda _: app.test_client().post(route+'/claim',json=body).get_json(),range(2)))
    first=next(item for item in outputs if not item['replayed'])
    replay=next(item for item in outputs if item['replayed'])
    assert first['claim_token'] and 'cpcl_pages' not in replay and 'claim_token' not in replay
    report={'claim_token':first['claim_token'],'status':'unknown','message':'中途断开','bytes_sent':180}
    assert c.post(route+'/result',json=report).get_json()['status']=='unknown'
    assert c.post(route+'/result',json=report).get_json()['replayed'] is True
    assert c.post(route+'/claim',json={**body,'idempotency_key':'another-user-action-1234'}).status_code==409
    assert c.post(route+'/submit',json=body).status_code==404
    assert c.post('/api/v2/erp/submit',json={}).status_code==404


def test_failed_refresh_is_atomic_and_runtime_cannot_point_to_other_services(app,monkeypatch):
    importer=app.extensions['service']['importer']
    store=app.extensions['service']['store']
    before=store.rows('orders')
    version=data_version(store)
    original=Importer.save_records
    def fail(self,kind,rows):
        if kind=='inventory': raise ValueError('intentional late failure')
        if kind=='orders': rows[0]['quantity']='9999'
        return original(self,kind,rows)
    monkeypatch.setattr(Importer,'save_records',fail)
    assert app.test_client().post('/api/v2/data/refresh',json={}).status_code==422
    assert store.rows('orders')==before and data_version(store)==version
    for path in ['E:/UL/订单扫码系统/data','E:/临时文件/工作台/workbench-next/runtime']:
        with pytest.raises(ValueError): guarded_runtime(path)


def test_all_over_limit_is_rejected_instead_of_truncated(app):
    store=app.extensions['service']['store']
    source=store.rows('orders')[0]
    rows=[{**source,'id':f'extra-{index}','order_no':f'EXP{index}'} for index in range(101)]
    app.extensions['service']['importer'].save_records('orders',rows)
    response=app.test_client().post('/api/v2/print/jobs',json={'kind':'order','all':True,'query':{'q':'LS0001','scope':'stock'},'expected_data_version':data_version(store)})
    assert response.status_code==400 and '100' in response.get_json()['message']


def test_monitor_skips_unchanged_and_keeps_data_on_failure(app,monkeypatch):
    monitor=app.extensions['service']['monitor']
    importer=app.extensions['service']['importer']
    calls=[]
    monkeypatch.setattr(monitor,'changed',lambda:False)
    monkeypatch.setattr(importer,'run',lambda **kwargs:calls.append(kwargs))
    monitor.check_once()
    assert not calls and monitor.status()['checked_at']
    monkeypatch.setattr(monitor,'changed',lambda:True)
    monitor.check_once()
    assert len(calls)==1 and monitor.status()['last_success_at']
    def fail(**kwargs): raise OSError('source is busy')
    monkeypatch.setattr(importer,'run',fail)
    monitor.check_once()
    assert 'busy' in monitor.status()['last_error'] and not monitor.status()['refreshing']
    assert app.test_client().get('/api/v2/orders?q=LS0001').get_json()['count']==7
    assert app.test_client().get('/api/v2/health',environ_overrides={'REMOTE_ADDR':'8.8.8.8'}).status_code==403


def test_monitor_retries_snapshot_not_yet_published_to_database(app,monkeypatch):
    from datetime import datetime,timezone
    import core.source_monitor as module
    store=app.extensions['service']['store']
    source=store.snapshots/'orders.xls'
    stat=source.stat()
    modified=datetime.fromtimestamp(stat.st_mtime,timezone.utc).isoformat()
    (store.snapshots/'manifest.json').write_text(json.dumps({'files':{'orders':{'filename':'orders.xls','source_mtime':modified,'size':stat.st_size,'sha256':'newly-copied'}}}),encoding='utf-8')
    monkeypatch.setattr(module,'SOURCES',{'orders':(str(source),'orders.xls')})
    assert app.extensions['service']['monitor'].changed() is True
