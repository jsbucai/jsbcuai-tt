import copy
from datetime import datetime,timedelta,timezone
import json
import uuid

import pytest

from test_sales_assistant import engine,create
from test_daily_work import store,user
from test_sales_codex_pipeline import request,output
from core.sales_assistant import SalesAssistantError
from core.sales_codex_pipeline import CodexJobError
from core.store import dumps


def suggestion_session(engine):
    response=request(engine);token=uuid.uuid4().hex;frozen=engine.codex.claim(token)
    source=frozen['messages'][0]['id'];result=output(source)
    result['draft']['lines'][0]['product_code']=''
    result['new_product_suggestions']=[{'source_ids':[source],'source_quote':'新规格M20*85',
       'product_name':'螺栓','spec':'M20*85','standard':'','grade':'8.8','surface':'热浸锌',
       'material':'',
       'base_unit':'支','reason':'当前目录没有匹配规格，请管理员核对'}]
    engine.codex.finish(frozen['job_id'],token,output=result,source_ids=[source])
    return engine.detail(user('sales',['sales']),response['session']['id'])['session']


def prepare(engine,session=None,key='prepare'):
    session=session or suggestion_session(engine);job=session['codex_job']
    return engine.prepare_new_product(user('sales',['sales']),session['id'],
       {'version':session['version'],'data_version':session['data_version'],'job_id':job['id'],
        'result_hash':job['result_hash'],'suggestion_id':job['new_product_suggestions'][0]['id'],
        'reference_code':'LS1000','idempotency_key':key})


def ready(engine):
    response=prepare(engine);token=uuid.uuid4().hex;snapshot=engine.new_products.claim(token)
    plan={'status':'ready','product_name':'螺栓','spec':'M20*85','model':'8.8 热浸锌','code':'LS1001',
          'base_unit':'支','classification_path':'标准件/六角头','issues':[],
          'payload':{'ptypes':[{'FullName':'螺栓','Name':'螺栓','UserCode':'LS1001','Unit':'支'}]}}
    engine.new_products.finish_prepare(snapshot['id'],token,plan)
    with engine.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('sales_new_product_policy',?)",(dumps({'enabled':True}),))
    session=engine.detail(user('admin',[],admin=True),response['session']['id'])['session']
    return session,session['new_product_plans'][0]


def confirm(engine,session,plan,actor='admin',key='confirm'):
    return engine.confirm_new_product(user(actor,['sales'],admin=actor=='admin'),session['id'],plan['id'],
       {'version':session['version'],'data_version':session['data_version'],'plan_hash':plan['plan_hash'],
        'idempotency_key':key})


def test_prepare_is_durable_readonly_and_does_not_stale_other_suggestions(engine):
    session=suggestion_session(engine);result=prepare(engine,session)
    assert result['session']['version']==session['version']
    assert result['session']['codex_job']['status']=='needs_clarification'
    assert result['session']['new_product_plans'][0]['status']=='preparing'
    assert result['session']['draft']['lines']==[]
    assert not result['session']['codex_job']['new_product_suggestions'][0]['can_prepare']
    replay=prepare(engine,session)
    assert replay['replayed'] and replay['plan_id']==result['plan_id']


def test_client_cannot_confirm_new_code_even_claiming_admin(engine):
    session,plan=ready(engine)
    assert plan['can_confirm']
    viewed=engine.detail(user('sales',['sales']),session['id'])['session']['new_product_plans'][0]
    assert not viewed['can_confirm']
    with pytest.raises(SalesAssistantError) as error:confirm(engine,session,plan,'sales')
    assert error.value.http_status==403


def test_confirm_freezes_original_plan_and_idempotency(engine):
    session,plan=ready(engine);one=confirm(engine,session,plan);two=confirm(engine,session,plan)
    assert two['replayed'] and one['plan_id']==two['plan_id']
    assert one['session']['new_product_plans'][0]['status']=='queued'
    token=uuid.uuid4().hex;claimed=engine.new_products.claim(token)
    assert claimed['status']=='creating' and claimed['confirmed_by']=='admin'
    assert claimed['plan']['code']=='LS1001' and engine.new_products.authorize_submission(claimed['id'],token)
    assert engine.new_products.claim(uuid.uuid4().hex) is None


def test_changed_plan_hash_and_catalog_prevent_creation(engine):
    session,plan=ready(engine);changed={**plan,'plan_hash':'0'*64}
    with pytest.raises(SalesAssistantError) as error:confirm(engine,session,changed)
    assert error.value.code=='sales_new_product_plan_changed'
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE records SET payload=json_set(payload,'$.spec','M20*99') WHERE kind='inventory'")
    with pytest.raises(SalesAssistantError):confirm(engine,session,plan)
    assert engine.new_products.claim(uuid.uuid4().hex) is None


@pytest.mark.parametrize('mutation',['owner','admin','data','hold'])
def test_permission_is_rechecked_after_network_preflight(engine,mutation):
    session,plan=ready(engine);confirm(engine,session,plan);token=uuid.uuid4().hex;claimed=engine.new_products.claim(token)
    with engine.store.connect(write=True) as db:
        if mutation=='owner':db.execute("UPDATE sales_customer_owners SET owner_id='buyer'")
        elif mutation=='admin':db.execute("UPDATE staff_users SET is_admin=0 WHERE id='admin'")
        elif mutation=='data':db.execute("UPDATE records SET payload=json_set(payload,'$.spec','M20*99') WHERE kind='inventory'")
        else:db.execute("INSERT OR REPLACE INTO settings VALUES ('restoration_hold','true')")
    with pytest.raises(CodexJobError):engine.new_products.authorize_submission(claimed['id'],token)


def test_interrupted_create_never_returns_to_queue(engine):
    session,plan=ready(engine);confirm(engine,session,plan);now=datetime.now(timezone.utc)
    token=uuid.uuid4().hex;claimed=engine.new_products.claim(token,now=now)
    assert engine.new_products.claim(uuid.uuid4().hex,now=now+timedelta(minutes=11)) is None
    current=engine.detail(user('admin',[],admin=True),session['id'])['session']['new_product_plans'][0]
    assert current['status']=='unknown' and not current['can_confirm'] and not current['can_prepare']
    with pytest.raises(SalesAssistantError):confirm(engine,session,current,key='again')
    with pytest.raises(SalesAssistantError):prepare(engine,session,key='again-prepare')


def test_success_merges_only_verified_new_catalog_row_and_stales_original_ai(engine):
    session,plan=ready(engine);confirm(engine,session,plan);token=uuid.uuid4().hex;claimed=engine.new_products.claim(token)
    with engine.store.connect() as db:
        before=[tuple(r) for r in db.execute("SELECT * FROM records WHERE sku='LS1000'")]
    product={'id':'new-stock','profile_id':'P','warehouse_id':'08390','warehouse':'江苏优螺',
             'product_id':'new-p','sku_id':'new-s','sku':'LS1001','base_unit':'1','unit_name':'支',
             'product_name':'螺栓','spec':'M20*85','model':'8.8 热浸锌','stock_quantity':'0'}
    engine.new_products.finish_creation(claimed['id'],token,status='created',
       result={'created_code':'LS1001','verified_inventory_row':product})
    with engine.store.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM records WHERE sku='LS1000'")]==before
        assert db.execute("SELECT count(*) FROM records WHERE sku='LS1001'").fetchone()[0]==1
    current=engine.detail(user('admin',[],admin=True),session['id'])['session']
    assert current['new_product_plans'][0]['status']=='created'
    assert current['codex_job']['status']=='stale' and not current['submission_operation_id']


def test_unverified_new_code_cannot_be_published(engine):
    session,plan=ready(engine);confirm(engine,session,plan);token=uuid.uuid4().hex;claimed=engine.new_products.claim(token)
    with pytest.raises(CodexJobError):
        engine.new_products.finish_creation(claimed['id'],token,status='created',result={'created_code':'LS1001'})
    engine.new_products.fail(claimed['id'],token,'identity_unverified')
    assert engine.detail(user('admin',[],admin=True),session['id'])['session']['new_product_plans'][0]['status']=='unknown'


def test_stock_refresh_does_not_cancel_new_product_plan(engine):
    session,plan=ready(engine)
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE records SET payload=json_set(payload,'$.stock_quantity','99') WHERE kind='inventory'")
        db.execute("UPDATE settings SET value=? WHERE key='data_version'",(dumps('stock-refresh'),))
    current=engine.detail(user('admin',[],True),session['id'])['session']
    assert current['new_product_plans'][0]['can_confirm']
    result=confirm(engine,current,plan)
    assert result['session']['new_product_plans'][0]['status']=='queued'
