from datetime import datetime
from types import SimpleNamespace
import pytest
from core import sales_customer_directory as directory
from core.sales_assistant import SalesAssistantError
from core.sales_workspace import BASE
from test_sales_workspace import app,login
from test_sales_assistant import engine,stable_order_date
from test_daily_work import store,user

def client(rows,profile='P'):
    def pages(method,params,collection,identity,**kw):
        assert method=='erp.btype.list' and collection=='btypes'
        return rows
    return SimpleNamespace(account=lambda:profile,pages=pages)

def row(name='客户甲',identity='1',**kw):
    return {'id':identity,'fullname':name,'usercode':'A-'+identity,'namepy':'KH','deleted':False,'isstop':False,**kw}

def test_full_directory_paginated_and_unassigned_visible(engine):
    rows=[row()]+[row('新客户'+str(i),str(i+2)) for i in range(65)]+[row('已停用','90',isstop=True)]
    assert directory.refresh(engine.store,client(rows),'P')['total']==67
    with engine.store.connect() as db:
        first=directory.choices(db,'P');second=directory.choices(db,'P',page=2)
        assert first['total']==66 and len(first['items'])==50 and len(second['items'])==16
        assert first['has_more'] and not second['has_more']
        assert directory.choices(db,'P','a-1')['items'][0]['customer_name']=='客户甲'
        assert directory.choices(db,'P','kh')['total']==66
        assert directory.choices(db,'P','客户甲')['items'][0]['can_start']
        assert not directory.choices(db,'P','新客户1')['items'][0]['can_start']
        assert db.execute('SELECT count(*) FROM sales_customer_owners').fetchone()[0]==1

@pytest.mark.parametrize('rows,profile', [([], 'P'),([row(),row()],'P'),([row()], 'OTHER'),([row(fullname='')],'P')])
def test_invalid_refresh_retains_previous_snapshot(engine,rows,profile):
    directory.refresh(engine.store,client([row()]),'P')
    with pytest.raises(Exception):directory.refresh(engine.store,client(rows,profile),'P')
    with engine.store.connect() as db:
        result=directory.choices(db,'P')
        assert result['total']==1 and result['sync']['last_success'] and result['sync']['error']

def test_same_name_identities_not_merged_or_allowed_to_start(engine):
    directory.refresh(engine.store,client([row(),row(identity='2')]),'P')
    with engine.store.connect() as db:
        result=directory.choices(db,'P');assert result['total']==2
        assert not any(r['can_start'] for r in result['items'])
    with pytest.raises(SalesAssistantError,match='同名'):
        engine.create(user('admin',[],True),{'profile_id':'P','customer_name':'客户甲',
            'erp_customer_id':'1','idempotency_key':'duplicate'})

def test_snapshot_replace_and_bound_identity_create(engine):
    directory.refresh(engine.store,client([row(),row('旧客户','2')]),'P')
    directory.refresh(engine.store,client([row()]),'P')
    with engine.store.connect() as db:assert directory.choices(db,'P')['total']==1
    actor=user('admin',[],True)
    with pytest.raises(SalesAssistantError):engine.create(actor,{'profile_id':'P','customer_name':'客户甲','erp_customer_id':'2','idempotency_key':'old'})
    result=engine.create(actor,{'profile_id':'P','customer_name':'客户甲','erp_customer_id':'1','handler_name':'李瑄','idempotency_key':'valid'})
    assert result['session']['fixed_sales_owner_id']=='sales'
    with engine.store.connect() as db:
        assert db.execute('SELECT erp_customer_id FROM sales_assistant_sessions WHERE id=?',(result['session']['id'],)).fetchone()[0]=='1'

def test_owner_endpoint_and_refresh_queue(app):
    store=app.extensions['service']['store'];directory.refresh(store,client([row()]),'P')
    browser=app.test_client();assert browser.get(BASE+'/customers?profile_id=P').status_code==401
    headers=login(browser)
    assert browser.get(BASE+'/customers?profile_id=P&query=A-1').json['total']==1
    assert browser.get(BASE+'/customers?profile_id=P&page=0').status_code==400
    with store.connect(write=True) as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('sales_api_policy','{"profile_id":"P"}'))
    assert browser.post(BASE+'/customers/refresh',json={'profile_id':'P'},headers=headers).status_code==200
    with store.connect() as db:
        assert directory.needed(db,'P',datetime(2026,10,20,23,0))

def test_working_hours_refresh_and_failure_backoff(engine):
    with engine.store.connect(write=True) as db:
        directory.ensure(db)
        assert not directory.needed(db,'P',datetime(2026,10,20,23,0))
        assert directory.needed(db,'P',datetime(2026,10,20,7,30))
        db.execute("INSERT INTO sales_erp_customer_sync(profile_id,last_attempt,error) VALUES ('P','2026-10-20T09:00:00','failed')")
        assert not directory.needed(db,'P',datetime(2026,10,20,9,14))
        assert directory.needed(db,'P',datetime(2026,10,20,9,15))
