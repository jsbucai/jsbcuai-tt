import json
import pytest
from core.store import dumps
from core.sales_workspace import BASE
from core import sales_customer_directory as directory
from core.customer_orders import CustomerOrders
from core.daily_work import DailyWorkConflict
from core.sales_assistant import SalesAssistantError
from test_sales_workspace import app,login,confirmed,binding
from test_sales_assistant import engine,stable_order_date,create,edit_body
from test_daily_work import store,user
from test_sales_customer_directory import client as erp_client,row as customer_row
from test_customer_orders import context,seed,row,allocation,admin


def directory_setup(app):
    services=app.extensions['service'];store=services['store']
    with store.connect(write=True) as db:
        db.execute("UPDATE staff_users SET display_name='李瑄' WHERE id='sales'")
    directory.refresh(store,erp_client([customer_row('新客户','2'),customer_row()]),'P')
    browser=app.test_client();headers=login(browser)
    return services,browser,headers


def test_assign_new_directory_customer_then_start_shared_session(app):
    services,browser,headers=directory_setup(app)
    body={'profile_id':'P','customer_name':'新客户','erp_customer_id':'2','owner_id':'sales','owner_version':0,'idempotency_key':'assign'}
    reply=browser.post(BASE+'/customers/owner',json=body,headers=headers)
    assert reply.status_code==200,reply.json
    assert reply.json['owner']['owner_id']=='sales'
    repeat=browser.post(BASE+'/customers/owner',json=body,headers=headers)
    assert repeat.json['replayed']
    assert browser.post(BASE+'/customers/owner',json={**body,'idempotency_key':'different'},headers=headers).status_code==409
    choices=browser.get(BASE+'/customers?profile_id=P&query=新客户').json
    assert choices['items'][0]['can_start'] and not choices['items'][0]['can_assign_owner']
    started=browser.post('/api/v2/sales-assistant/sessions',headers=headers,json={
        'profile_id':'P','customer_name':'新客户','erp_customer_id':'2','handler_name':'李瑄','idempotency_key':'create-new'})
    assert started.status_code==200 and started.json['session']['fixed_sales_owner_id']=='sales'
    with services['store'].connect() as db:
        assert db.execute("SELECT count(*) FROM sales_customer_owner_events WHERE customer_name='新客户'").fetchone()[0]==1


def test_owner_rejects_identity_staff_and_stale_version(app):
    services,browser,headers=directory_setup(app)
    body={'profile_id':'P','customer_name':'新客户','erp_customer_id':'2','owner_id':'sales','owner_version':0,'idempotency_key':'assign'}
    for field,value in [('erp_customer_id','1'),('owner_version',9),('owner_id','buyer')]:
        result=browser.post(BASE+'/customers/owner',json={**body,field:value},headers=headers)
        assert result.status_code in (400,409)
    with services['store'].connect() as db:
        assert not db.execute("SELECT 1 FROM sales_customer_owners WHERE customer_name='新客户'").fetchone()
    assert browser.post(BASE+'/customers/owner',json=body).status_code==403


def test_assign_updates_current_tasks_without_rewriting_human_facts(context):
    store,daily=context;item=row('PO#1','LS1000',customer='新客户')
    seed(store,[item],[allocation(item,1)]);daily.refresh()
    directory.refresh(store,erp_client([customer_row('新客户','2')]),'P')
    with store.connect(write=True) as db:
        db.execute("UPDATE staff_users SET display_name='李瑄' WHERE id='seller'")
        before=[tuple(r) for r in db.execute("SELECT id,status,next_follow_up_date FROM daily_work_tasks WHERE role='sales'")]
        owner=db.execute("SELECT version FROM sales_customer_owners WHERE customer_name='新客户'").fetchone()
    result=CustomerOrders(store,daily).set_customer_owner(admin(),'P','新客户','seller','','',
        owner[0] if owner else 0,'assign','录单网页确认客户固定销售',_directory_id='2',_only_unassigned=True)
    assert result['owner']['owner_id']=='seller'
    with store.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT id,status,next_follow_up_date FROM daily_work_tasks WHERE role='sales'")]==before
        assert all(r[0]=='seller' for r in db.execute("SELECT assignee_id FROM daily_work_tasks WHERE role='sales'"))


def test_delete_restore_draft_retains_contents_and_blocks_old_clients(app):
    services,browser,headers=directory_setup(app);engine=services['sales_assistant']
    s=create(engine,user('admin',[],True));payload=s['draft']
    body={'version':s['version'],'idempotency_key':'remove'}
    result=browser.post(BASE+'/sessions/'+s['id']+'/archive',json=body,headers=headers)
    assert result.status_code==200,result.json
    removed=result.json['session'];assert removed['archive_kind']=='deleted'
    assert removed['draft']==payload
    assert browser.get(BASE+'/sessions').json['items']==[]
    assert len(browser.get(BASE+'/sessions?archived=1').json['items'])==1
    assert engine.list(user('admin',[],True))['items']==[]
    assert browser.post(BASE+'/sessions/'+s['id']+'/archive',json=body,headers=headers).json['replayed']
    with pytest.raises(SalesAssistantError,match='已移除'):engine.edit(user('admin',[],True),s['id'],edit_body(removed))
    restored=browser.post(BASE+'/sessions/'+s['id']+'/restore',json={'version':removed['version'],'idempotency_key':'restore'},headers=headers)
    assert restored.status_code==200 and not restored.json['session']['archived_at']
    assert len(browser.get(BASE+'/sessions').json['items'])==1
    assert restored.json['session']['draft']==payload


def test_delete_version_conflict_and_same_key_conflict(app):
    services,browser,headers=directory_setup(app);s=create(services['sales_assistant'],user('admin',[],True))
    route=BASE+'/sessions/'+s['id']+'/archive'
    assert browser.post(route,json={'version':s['version']+1,'idempotency_key':'bad'},headers=headers).status_code==409
    assert browser.post(route,json={'version':s['version'],'idempotency_key':'one'},headers=headers).status_code==200
    assert browser.post(route,json={'version':s['version']+1,'idempotency_key':'one'},headers=headers).status_code==409


def test_delete_cancels_codex_and_rejects_late_result(app):
    services,browser,headers=directory_setup(app);engine=services['sales_assistant'];actor=user('admin',[],True)
    with engine.store.connect(write=True) as db:db.execute("INSERT OR REPLACE INTO settings VALUES ('sales_codex_policy','{\"enabled\":true}')")
    s=create(engine,actor)
    s=engine.request_codex(actor,s['id'],{'version':s['version'],'idempotency_key':'codex','text':'LS1000 3支'})['session']
    result=browser.post(BASE+'/sessions/'+s['id']+'/archive',json={'version':s['version'],'idempotency_key':'delete'},headers=headers)
    assert result.status_code==200,result.json
    with engine.store.connect() as db:
        job=db.execute('SELECT * FROM sales_codex_jobs WHERE id=?',(s['codex_job']['id'],)).fetchone()
        session=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(s['id'],)).fetchone()
        assert job['status']=='cancelled' and not engine.codex._current(db,job,session)


def test_inflight_api_cannot_hide_and_completed_receipts_are_retained(app):
    services,browser,headers=directory_setup(app);engine=services['sales_assistant'];actor,s=confirmed(engine)
    engine.submit(actor,s['id'],{'version':s['version'],'channel':'api','idempotency_key':'send'})
    s=engine.detail(actor,s['id'])['session'];route=BASE+'/sessions/'+s['id']+'/archive'
    body={'version':s['version'],'idempotency_key':'hide'}
    assert browser.post(route,json=body,headers=headers).status_code==409
    from tools.sales_api_worker import run_one
    run_one(engine.store,None,prepare=binding,send=lambda *a:{'status':'accepted','vchcode':'test-only-receipt'})
    s=engine.detail(actor,s['id'])['session']
    result=browser.post(route,json={'version':s['version'],'idempotency_key':'hide-done'},headers=headers)
    assert result.status_code==200,result.json
    assert result.json['session']['archive_kind']=='archived'
    assert len(result.json['session']['api_submission']['documents'])==2
    assert all(d['status']=='accepted' for d in result.json['session']['api_submission']['documents'])
