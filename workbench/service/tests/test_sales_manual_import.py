from io import BytesIO
import json
import pytest
import xlrd

from core.sales_workspace import BASE
from core.sales_web_import import EXPECTED_HEADERS,EXPECTED_SHEETS
from core import sales_api_jobs as sales
from test_sales_workspace import app,login,confirmed
from test_sales_assistant import engine,stable_order_date
from test_daily_work import store,user


def prepare(client,headers,s,key='manual'):
    return client.post(BASE+'/sessions/'+s['id']+'/manual-import',headers=headers,
                       json={'version':s['version'],'idempotency_key':key})


def file_url(s):
    return BASE+'/sessions/'+s['id']+'/manual-import/'+s['manual_import']['id']+'/file'


def test_real_template_filled_from_same_model_and_repeat_download(app,engine):
    actor,s=confirmed(engine)
    client=app.test_client();headers=login(client)
    first=prepare(client,headers,s)
    assert first.status_code==200,first.json
    ready=first.json['session'];item=ready['manual_import']
    assert ready['status']=='confirmed' and item['status']=='prepared'
    listed=client.get(BASE+'/sessions').json['items']
    assert next(x for x in listed if x['id']==s['id'])['manual_import_status']=='prepared'
    assert item['completion_basis']=='file_generated' and not item['erp_readback_performed']
    assert prepare(client,headers,s).json['replayed']
    response=client.get(file_url(ready))
    assert response.status_code==200 and response.data==client.get(file_url(ready)).data
    assert '.xls' in response.headers['Content-Disposition']
    book=xlrd.open_workbook(file_contents=response.data,formatting_info=True)
    assert tuple(book.sheet_names())==EXPECTED_SHEETS
    sheet=book.sheet_by_index(0)
    assert tuple(sheet.row_values(0))==EXPECTED_HEADERS and sheet.nrows==3
    get=lambda row,name:sheet.cell_value(row,EXPECTED_HEADERS.index(name))
    with engine.store.connect() as db:
        row=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(s['id'],)).fetchone()
        model=sales.normalized(db,row)
        assert db.execute('SELECT count(*) FROM sales_api_batches').fetchone()[0]==0
    for index,doc in enumerate(model['documents'],1):
        assert get(index,'单据编号')==doc['number']
        assert '#' in get(index,'单据编号')
        assert get(index,'商品编号')=='LS1000'
        assert get(index,'数量')==float(doc['quantity'])
        assert get(index,'折前单价')==float(doc['unit_price'])
        assert get(index,'折前金额')==float(doc['amount'])
        assert get(index,'税率(%)')==13 and get(index,'单位')=='支'
        assert get(index,'附加说明')==''
        assert get(index,'摘要')==get(index,'明细备注')==doc['summary']
        assert doc['summary'].endswith('#交期=2026-12-20')
        assert get(index,'制单人名称')=='罗健树' and get(index,'经手人名称')=='李瑄'
        assert get(index,'仓库名称')==get(index,'明细仓库名称')=='江苏优螺'
    assert get(1,'单据编号')!=get(2,'单据编号')
    # Unrelated refresh does not replace the frozen file.
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE settings SET value='\"later-global-refresh\"' WHERE key='data_version'")
    assert client.get(file_url(ready)).data==response.data


def test_manual_reservation_blocks_api_and_cross_session_duplicate(app,engine):
    actor,s=confirmed(engine)
    client=app.test_client();headers=login(client)
    ready=prepare(client,headers,s).json['session']
    api=client.post('/api/v2/sales-assistant/sessions/'+s['id']+'/submit',headers=headers,
                    json={'version':s['version'],'channel':'api','idempotency_key':'api-after-manual'})
    assert api.status_code==409 and api.json['error']=='sales_manual_import_pending'
    with engine.store.connect() as db:
        with pytest.raises(sales.SalesAPIError,match='手动导入'):
            sales._reserve_guard(db,s['profile_id'],ready['manual_import']['document_numbers'])
    assert prepare(client,headers,s,key='other-key').status_code==409


def test_cancel_only_unimported_file_then_original_api_is_available(app,engine):
    actor,s=confirmed(engine)
    client=app.test_client();headers=login(client)
    ready=prepare(client,headers,s).json['session'];item=ready['manual_import']
    body={'version':s['version'],'export_id':item['id'],'confirmed':True,'idempotency_key':'cancel'}
    result=client.post(BASE+'/sessions/'+s['id']+'/manual-import/cancel',headers=headers,json=body)
    assert result.status_code==200 and result.json['session']['manual_import']['status']=='cancelled'
    assert client.post(BASE+'/sessions/'+s['id']+'/manual-import/cancel',headers=headers,json=body).json['replayed']
    assert client.get(file_url(ready)).status_code==400
    sent=engine.submit(actor,s['id'],{'version':s['version'],'channel':'api','idempotency_key':'now-api'})
    assert sent['session']['status']=='submitting'


def test_completion_is_user_report_and_cannot_be_cancelled_or_requeued(app,engine):
    actor,s=confirmed(engine)
    client=app.test_client();headers=login(client)
    ready=prepare(client,headers,s).json['session'];item=ready['manual_import']
    body={'version':s['version'],'export_id':item['id'],'confirmed':True,'idempotency_key':'complete'}
    result=client.post(BASE+'/sessions/'+s['id']+'/manual-import/complete',headers=headers,json=body)
    assert result.status_code==200
    manual=result.json['session']['manual_import']
    assert manual['status']=='reported_imported' and manual['completion_basis']=='user_report'
    assert not manual['erp_readback_performed'] and result.json['session']['status']=='confirmed'
    body['idempotency_key']='late-cancel'
    assert client.post(BASE+'/sessions/'+s['id']+'/manual-import/cancel',headers=headers,json=body).status_code==400
    with pytest.raises(Exception,match='手动导入'):
        engine.submit(actor,s['id'],{'version':s['version'],'channel':'api','idempotency_key':'duplicate-api'})


def test_versions_permissions_and_existing_execution_remain_protected(app,engine):
    actor,s=confirmed(engine)
    client=app.test_client();headers=login(client)
    assert app.test_client().get(BASE+'/import-template').status_code==401
    assert client.post(BASE+'/sessions/'+s['id']+'/manual-import',json={'version':s['version']}).status_code==403
    wrong={**s,'version':s['version']-1}
    assert prepare(client,headers,wrong).status_code==409
    with engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM sales_manual_imports').fetchone()[0]==0
    engine.submit(actor,s['id'],{'version':s['version'],'channel':'api','idempotency_key':'api-first'})
    assert prepare(client,headers,s).status_code==409


def test_manual_export_does_not_need_api_or_browser_enablement(app,engine):
    actor,s=confirmed(engine)
    with engine.store.connect(write=True) as db:
        db.execute("DELETE FROM settings WHERE key IN ('sales_api_policy','sales_web_import_policy')")
    client=app.test_client();headers=login(client)
    result=prepare(client,headers,s)
    assert result.status_code==200,result.json
    blank=client.get(BASE+'/import-template')
    assert blank.status_code==200
    book=xlrd.open_workbook(file_contents=blank.data)
    assert book.sheet_by_index(0).nrows==1
