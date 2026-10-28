import json
import pytest

from core import sales_api_jobs as api
from core import sales_customer_directory as directory
from core.store import dumps
from test_sales_assistant import engine, stable_order_date
from test_daily_work import store
from test_sales_workspace import confirmed, binding
from test_sales_customer_directory import client, row
from tools.sales_api_worker import run_one


def test_source_po_and_nonconsecutive_line_numbers_not_display_indices():
    draft={'customer_order_no':'POQ001019','lines':[
        {'source_summary':{'customer_line_no':'10'}},
        {'source_summary':{'customer_line_no':'30'}}]}
    assert api.planned_numbers(draft)==['POQ001019#10','POQ001019#30']
    draft['lines'].reverse()
    assert api.planned_numbers(draft)==['POQ001019#30','POQ001019#10']
    assert api.planned_numbers({'customer_order_no':'PO-1','lines':[{},{}]})==['PO-1#1','PO-1#2']


@pytest.mark.parametrize('lines',[
    [{'source_summary':{'customer_line_no':'10'}},{'source_summary':{'customer_line_no':'10'}}],
    [{'source_summary':{'customer_line_no':'1\n2'}}],
    [{'source_summary':{'customer_line_no':'1#2'}}],
])
def test_ambiguous_source_rows_not_silently_renumbered(lines):
    with pytest.raises(api.SalesAPIError):api.planned_numbers({'customer_order_no':'PO-1','lines':lines})


def test_real_wire_and_export_use_po_number_and_prevent_second_business_attempt(engine):
    actor,s=confirmed(engine)
    with engine.store.connect() as db:
        saved=db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(s['id'],)).fetchone()
        model=api.normalized(db,saved)
    assert [d['number'] for d in model['documents']]==['PO-123#1','PO-123#2']
    assert api.wire_document(model,model['documents'][0],binding(None,model))['number']=='PO-123#1'
    engine.submit(actor,s['id'],{'version':s['version'],'channel':'api','idempotency_key':'send'})
    with engine.store.connect() as db:
        with pytest.raises(api.SalesAPIError,match='已有提交记录'):
            api._reserve_guard(db,'P',['po-123#1'])
    assert engine.detail(actor,s['id'])['session']['api_submission']['delivery_write_status']=='not_verified'


def test_legacy_accepted_number_receipt_and_frozen_payload_not_rewritten(engine):
    actor,s=confirmed(engine,lines=1)
    engine.submit(actor,s['id'],{'version':s['version'],'channel':'api','idempotency_key':'send'})
    with engine.store.connect(write=True) as db:
        batch=db.execute('SELECT * FROM sales_api_batches').fetchone()
        old=json.loads(batch['model_json']);old['schema']='sales_api_v1'
        old['documents'][0]['number']='UL_LEGACY_001'
        db.execute('UPDATE sales_api_batches SET model_json=?',(dumps(old),))
        db.execute("UPDATE sales_api_documents SET number='UL_LEGACY_001'")
        frozen=dumps(old)
    sent=[]
    run_one(engine.store,None,prepare=binding,
            send=lambda client,doc:sent.append(doc) or {'status':'accepted','vchcode':'123'})
    assert sent[0]['number']=='UL_LEGACY_001'
    with engine.store.connect() as db:
        assert db.execute('SELECT model_json FROM sales_api_batches').fetchone()[0]==frozen
        with pytest.raises(api.SalesAPIError,match='UL_LEGACY_001'):
            api._reserve_guard(db,'P',['PO-123#1'])
        assert api.projection(db,s['id'])['documents'][0]['number']=='UL_LEGACY_001'


def delivery_row(**extra):
    return row(deliveryinfoid='88',deliveryinfo={
        'id':'88','btypeid':'1','receiverpeople':'收件人',
        'receivercellphone':'12345','receiveraddress':'客户甲',**extra},bankaccount='must-not-cache')


def test_delivery_cache_is_exact_scoped_and_keeps_company_name_address(engine):
    directory.refresh(engine.store,client([delivery_row()]),'P')
    with engine.store.connect() as db:
        value=directory.delivery(db,'P','1','客户甲')
        assert value['status']=='available' and value['info']['receiveraddress']=='客户甲'
        assert 'must-not-cache' not in dumps(value)
        assert value['observed_at'] and value['source']=='erp.btype.list'
        assert directory.delivery(db,'P','other','客户甲')['status']=='identity_review'
        assert directory.delivery(db,'OTHER','1','客户甲')['status']=='identity_review'
    with pytest.raises(ValueError):directory.refresh(engine.store,client([]),'P')
    with engine.store.connect() as db:
        assert directory.delivery(db,'P','1','客户甲')==value


def test_delivery_mismatch_and_removed_default_never_use_fallback(engine):
    directory.refresh(engine.store,client([delivery_row(btypeid='other')]),'P')
    with engine.store.connect() as db:
        assert directory.delivery(db,'P','1','客户甲')['status']=='conflict'
    directory.refresh(engine.store,client([row(area='不可猜测的公司地址',person='不可猜测的联系人')]),'P')
    with engine.store.connect() as db:
        value=directory.delivery(db,'P','1','客户甲')
        assert value['status']=='missing' and value['info']=={}
