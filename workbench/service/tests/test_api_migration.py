import copy
import json
import sys
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import create_app
from core.store import Store
from core.logistics import calculate
from core.update_jobs import UpdateJobs
from core.api_source import publish, SourceIncomplete
from core.erp_api import ErpClient, ErpUnavailable
from core.wsgjp_gateway import ApiError


@pytest.fixture
def store():
    (ROOT/'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='api-test-') as path:
        yield Store(path)


def line(q='8', **extra):
    return dict(profile_id='1',warehouse_id='2',product_id='3',sku_id='4',base_unit='件',
                product_code='LS0001',line_id='1',open_qty=q,receipt_evidence_complete=True,
                expedite_evidence_complete=True,allocated_receipts=[],expedite_required=False,**extra)


def order(number='A#1',q='8', **extra):
    return dict(order_no=number,order_date='2026-09-01',delivery_date='2026-10-01',lines=[line(q)],**extra)


def inventory(physical='10',saleable='0'):
    return [{**{k:v for k,v in line().items() if k in ('profile_id','warehouse_id','product_id','sku_id','base_unit')},
             'physical_qty':physical,'saleable_qty':saleable}]


def snapshot(orders=None, physical='20',saleable='0'):
    orders=orders or [order()]
    return {'complete':True,'profile_id':'1','source_updated_at':'2026-09-25T12:00:00+08:00',
        'orders':orders,'inventory':inventory(physical,saleable),
        'records':{'orders':[{'id':o['order_no']+':'+l['line_id'],'line_id':l['line_id'],'order_no':o['order_no'],
          'sku':l['product_code'],'quantity':l['open_qty'],'product_name':'测试商品','status':'待发货',
          'summary':'PO#PROJECT#1#DRAWING#螺栓#M10#8.8#锌#'+l['open_qty']+'#'+l['product_code']}
          for o in orders for l in o['lines']],
          'inventory':[{'id':'stock-1','sku':'LS0001','stock_quantity':physical,'available_stock':saleable}], 'purchases':[]}}


def test_fifo_large_order_consumes_even_when_still_short():
    result=calculate([order('B#1','2'),order('A#1','20')],inventory('10','-10'))
    assert [x['freightname'] for x in result['plans']]==['需采购','需采购']
    allocations={x['order_no']:x for x in result['allocations']}
    assert allocations['A#1']['physical']=='10' and allocations['A#1']['transit']=='2'
    assert allocations['B#1']['physical']=='0' and allocations['B#1']['transit']=='0'
    assert result['plans'][0]['freightnumber']=='@10/01#B#1'


def test_negative_net_reduces_effective_physical_as_other_draft_reservation():
    result=calculate([order(q='2')],inventory('10','7.99'))
    assert result['plans'] and not result['conflicts']
    allocation=result['allocations'][0]
    assert allocation['physical']=='2' and allocation['transit']=='0'
    assert allocation['inferred_other_reserved']=='0.01'
    assert allocation['effective_physical']=='9.99'


def test_missing_receipt_evidence_blocks_but_consumes_stock():
    a=order('A','8');a['lines'][0]['receipt_evidence_complete']=False
    result=calculate([a,order('B','8')],inventory('8','-8'))
    assert result['conflicts'][0]['order_no']=='A'
    assert result['plans'][0]['freightname']=='需采购'


@pytest.mark.parametrize('age,status',[(7,'来货件'),(8,'异常')])
def test_natural_day_boundary(age,status):
    o=order(q='8');o['lines'][0]['allocated_receipts']=[{'allocation_id':'R1','qty':'8','received_date':f'2026-09-{25-age:02}'}]
    result=calculate([o],inventory('8','0'),today=date(2026,9,25))
    assert result['plans'][0]['freightname']==status


def test_warehouse_isolation_and_multiline_priority():
    a=order(q='4');b=line('2');b['line_id']='2';b['warehouse_id']='9';a['lines'].append(b)
    stocks=inventory('4','0')+[dict(inventory('0','-2')[0],warehouse_id='9')]
    result=calculate([a],stocks)
    assert result['plans'][0]['freightname']=='需采购'
    assert sum(float(x['physical']) for x in result['allocations'])==4


def test_mixed_stock_prefix_is_local_packing_category():
    a=order(q='4');b=line('2');b.update(line_id='2',product_id='9',product_code='LD0002');a['lines'].append(b)
    stocks=inventory('4','0')+[dict(inventory('2','0')[0],product_id='9')]
    result=calculate([a],stocks)
    assert not result['conflicts'] and result['plans'][0]['freightname'] == '库存件组合'
    assert result['plans'][0]['write_enabled'] is False


def test_delivery_date_not_inferred_from_today():
    a=order(q='4');del a['delivery_date']
    result=calculate([a],inventory('4','0'))
    assert not result['conflicts'] and result['plans'][0]['freightnumber'] == ''
    assert result['plans'][0]['write_enabled'] is False


def test_auth_public_boundary_logout_and_no_windows_import(store):
    app=create_app({'SOURCE_MODE':'api','PUBLIC_MODE':True,'ADMIN_PASSWORD_HASH':generate_password_hash('test-password'),
                    'TESTING':True},runtime_dir=store.runtime)
    client=app.test_client()
    assert client.get('/livez').status_code==200
    assert client.get('/app').status_code==200
    assert client.get('/app/download').status_code in {200,404}
    assert client.get('/app/private').status_code==401
    assert client.get('/api/v2/health',environ_base={'REMOTE_ADDR':'8.8.8.8'}).status_code==401
    assert client.get('/api/v2/orders',headers={'X-Forwarded-For':'127.0.0.1'}).status_code==401
    token=client.post('/api/v2/auth/login',json={'password':'test-password'}).json['token']
    headers={'Authorization':'Bearer '+token}
    assert client.get('/api/v2/health',headers=headers).json['capabilities']['inventory_batch_upload'] is False
    assert client.post('/api/v2/adjustment-batches',json={},headers=headers).status_code==409
    assert client.post('/api/v2/auth/logout',json={},headers=headers).status_code==200
    assert client.get('/api/v2/orders',headers=headers).status_code==401
    with store.connect() as db:assert token not in str([tuple(x) for x in db.execute('SELECT * FROM auth_sessions')])


def test_login_rate_limit(store):
    app=create_app({'SOURCE_MODE':'api','PUBLIC_MODE':True,'ADMIN_PASSWORD_HASH':generate_password_hash('test'),'TESTING':True},runtime_dir=store.runtime)
    c=app.test_client()
    for _ in range(5): assert c.post('/api/v2/auth/login',json={'password':'bad'}).status_code==401
    assert c.post('/api/v2/auth/login',json={'password':'test'}).status_code==429


def test_success_cache_partial_failures_and_unknowns(store):
    data=snapshot([order('A'),order('B'),order('C')],physical='24',saleable='0')
    calls=[]
    def sender(target):
        calls.append(target['tradeid'])
        if target['tradeid']=='C': raise TimeoutError()
        return target['tradeid']=='A'
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:copy.deepcopy(data)),sender)
    first=str(uuid.uuid4());jobs.enqueue(first,'update');jobs.process_one()
    assert jobs.get(first)['status']=='unknown'
    with store.connect() as db: assert [r[0] for r in db.execute('SELECT tradeid FROM freight_cache')]==['A']
    calls.clear()
    second=str(uuid.uuid4());jobs.enqueue(second,'update');jobs.process_one()
    assert calls==['B']  # A succeeded; C remains unknown and cannot be blindly retried.


def test_repeat_target_no_write_and_preview_not_success_cache(store):
    data=snapshot(physical='8')
    calls=[];jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:data),lambda p:calls.append(p) or True)
    preview=str(uuid.uuid4());jobs.enqueue(preview,'preview');jobs.process_one()
    assert not calls
    for _ in range(2):
        identity=str(uuid.uuid4());jobs.enqueue(identity,'update');jobs.process_one()
    assert len(calls)==1


def test_recovery_does_not_replay_sending(store):
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:snapshot(physical='8')),lambda p:True)
    identity=str(uuid.uuid4());jobs.enqueue(identity,'update')
    with store.connect(write=True) as db:
        db.execute("UPDATE update_jobs SET status='running' WHERE id=?",(identity,))
        db.execute("INSERT INTO freight_attempts VALUES ('x',?,'1','A#1','{}','sending','now','now')",(identity,))
    jobs.recover()
    assert jobs.get(identity)['status']=='unknown'
    with store.connect() as db: assert db.execute('SELECT status FROM freight_attempts').fetchone()[0]=='unknown'
    assert jobs.process_one() is False


def test_source_failure_preserves_last_snapshot(store):
    data=snapshot(physical='8');publish(store,data,calculate(data['orders'],data['inventory']))
    before=store.rows('orders')
    def missing():raise SourceIncomplete('缺少交期')
    jobs=UpdateJobs(store,SimpleNamespace(collect=missing))
    identity=str(uuid.uuid4());jobs.enqueue(identity);jobs.process_one()
    assert jobs.get(identity)['status']=='blocked' and store.rows('orders')==before


def test_task_idempotency_and_single_active_job(store):
    from core.print_jobs import JobConflict
    jobs=UpdateJobs(store,None)
    identity=str(uuid.uuid4())
    assert jobs.enqueue(identity)==jobs.enqueue(identity)
    assert jobs.enqueue(str(uuid.uuid4()))['id']==identity


def test_source_publication_atomic_on_late_invalid_row(store):
    data=snapshot(physical='8');result=calculate(data['orders'],data['inventory']);publish(store,data,result)
    before=store.rows('orders');data['records']['inventory'].append({'sku':'BAD'})
    with pytest.raises(SourceIncomplete):publish(store,data,result)
    assert store.rows('orders')==before


def test_api_write_allowlist_and_pagination():
    api=ErpClient('/not/a/vault')
    with pytest.raises(ErpUnavailable):api.call('beefun.deliver.updatefreightinfo',{})
    responses=iter([{'orders':[{'id':'1'},{'id':'2'}],'totalresults':3}, {'orders':[{'id':'2'}],'totalresults':3}])
    api.call=lambda m,p:next(responses)
    with pytest.raises(ErpUnavailable):api.pages('anything',{},'orders',lambda x:x['id'],size=2)


def test_token_refresh_seconds_atomic_and_no_secret_error(tmp_path,monkeypatch):
    from core import erp_api
    vault=tmp_path/'erp.json';now=time.time()
    record={'credentials':{'appkey':'1'*32,'appsecret':'a'*32,'signkey':'key'},
            'token':{'appkey':'1'*32,'profileid':'42','auth_token':'old','refresh_token':'refresh','expires_at':now}}
    vault.write_text(json.dumps(record));vault.chmod(0o600)
    monkeypatch.setattr(erp_api.Client,'refresh',lambda *a:{'appkey':'1'*32,'profileid':'42','auth_token':'new','expires_in':7200})
    api=ErpClient(vault);assert api.account()=='42'
    stored=json.loads(vault.read_text())
    assert 7190<stored['token']['expires_at']-now<7210
    assert stored['token']['refresh_token']=='refresh'
    assert vault.stat().st_mode & 0o777==0o600


def test_read_retries_once_after_vendor_reports_rotated_authorization(tmp_path,monkeypatch):
    from core import erp_api
    vault=tmp_path/'erp.json';now=time.time()
    record={'credentials':{'appkey':'1'*32,'appsecret':'a'*32,'signkey':'key'},
            'token':{'appkey':'1'*32,'profileid':'42','auth_token':'old','refresh_token':'refresh',
                     'expires_at':now+7200}}
    vault.write_text(json.dumps(record));vault.chmod(0o600)
    monkeypatch.setattr(erp_api.Client,'refresh',lambda *a:{'appkey':'1'*32,'profileid':'42',
        'auth_token':'new','refresh_token':'new-refresh','expires_in':7200})
    calls=[]
    def transport(path,payload):
        calls.append(payload['token'])
        if len(calls)==1: raise ApiError('account',15)
        return {'stocks':[],'totalresults':0}
    result=ErpClient(vault,transport=transport).call('erp.stock.list',{})
    assert result['stocks']==[] and calls==['old','new']
    assert json.loads(vault.read_text())['token']['auth_token']=='new'


def test_worker_single_owner_and_durable_completion(store):
    source=SimpleNamespace(collect=lambda:snapshot(physical='8'))
    jobs=UpdateJobs(store,source);other=UpdateJobs(store,source)
    identity=str(uuid.uuid4());jobs.enqueue(identity)
    try:
        assert jobs.start() is True
        assert other.start() is False
        deadline=time.monotonic()+5
        while jobs.get(identity)['status'] in {'queued','running'} and time.monotonic()<deadline:
            time.sleep(.02)
        assert jobs.get(identity)['status']=='succeeded'
        with store.connect() as db:
            row=db.execute('SELECT payload,data_version FROM logistics_calculations WHERE job_id=?',(identity,)).fetchone()
            assert json.loads(row[0])['allocations'] and row[1]
    finally:jobs.stop()


def test_unknown_target_is_not_hidden_by_older_matching_cache(store):
    data=snapshot(physical='8');calls=[]
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:data),lambda p:calls.append(p) or True)
    identity=str(uuid.uuid4());jobs.enqueue(identity,'update');jobs.process_one()
    with store.connect(write=True) as db:
        db.execute("INSERT INTO freight_attempts VALUES ('uncertain',?,'1','A#1','{}','unknown','now','now')",(identity,))
    calls.clear();second=str(uuid.uuid4());jobs.enqueue(second,'update');jobs.process_one()
    assert jobs.get(second)['status']=='unknown' and calls==[]


def test_missing_key_or_explicit_evidence_never_becomes_default():
    a=order();a['lines'][0]['warehouse_id']=None
    with pytest.raises(ValueError):calculate([a],inventory('8','0'))
    a=order();del a['lines'][0]['expedite_required']
    assert calculate([a],inventory('8','0'))['conflicts']


def test_duplicate_display_mapping_and_timezone_rejected():
    from core.api_source import validate_snapshot
    data=snapshot(physical='8')
    data['records']['orders'].append(dict(data['records']['orders'][0],id='another'))
    with pytest.raises(SourceIncomplete):validate_snapshot(data)
    data=snapshot(physical='8');data['source_updated_at']='2026-09-25T12:00:00'
    with pytest.raises(SourceIncomplete):validate_snapshot(data)
