"""Read candidates without changing source, quote, draft or customer scope."""
from contextlib import contextmanager
import json
import sqlite3

import pytest

from core.sales_assistant import SalesAssistant, SalesAssistantError
from core.sales_history_matching import import_history_snapshot
from core.store import dumps
from test_daily_work import store, user
from test_sales_assistant import engine, create
from test_sales_history_matching import historical, snapshot


def prepare(engine,tmp_path):
    source,digest=snapshot(tmp_path,[historical(code='LS1000')])
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute("SELECT payload FROM records WHERE id='record-1'").fetchone()[0])
        product.update(product_name='六角头螺栓',spec='M6*30',model='DIN 933',
                       warehouse_name='江苏优螺',grade='8.8级',surface='发黑')
        db.execute("UPDATE records SET payload=? WHERE id='record-1'",(dumps(product),))
        import_history_snapshot(db,source,'P',digest)


def with_part(engine,actor,key='part'):
    session=create(engine,actor,key+'-create')
    return engine.message(actor,session['id'],{'version':session['version'],
        'idempotency_key':key,'text':'客户料号=PART-1,3,1.13'})['session']


def test_candidate_projection_is_readonly_and_keeps_unmatched_quote(engine,tmp_path):
    prepare(engine,tmp_path);actor=user('sales',['sales']);session=with_part(engine,actor)
    line=session['draft']['lines'][0]
    assert line['product_code']=='' and line['quantity']=='3' and line['unit_price']=='1.13'
    assert line['match_candidates'][0]['product_code']=='LS1000'
    assert line['match_candidates'][0]['available'] is True
    assert line['match_status']['covered_to']=='2026-07-01'
    with engine.store.connect() as db:
        before=dict(db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(session['id'],)).fetchone())
    again=engine.detail(actor,session['id'])['session']
    with engine.store.connect() as db:
        after=dict(db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(session['id'],)).fetchone())
    assert before==after and again['version']==session['version']
    stored=json.loads(after['draft'])['lines'][0]
    assert 'match_candidates' not in stored and 'match_status' not in stored
    assert stored['product_code']=='' and stored['source_ref']['customer_part_no']=='PART-1'


def test_candidate_choice_validates_current_code_preserves_price_and_server_source(engine,tmp_path):
    prepare(engine,tmp_path);actor=user('sales',['sales']);session=with_part(engine,actor)
    line=session['draft']['lines'][0];original=line['source_ref']
    body={**session['draft'],'version':session['version'],'idempotency_key':'choose-history',
          'customer_order_no':'CURRENT-PO','delivery_date':'2026-10-15',
          'lines':[{**line,'product_code':'LS1000','replace_product_code':True,
                    'source_ref':{**original,'customer_part_no':'FORGED'},
                    'match_candidates':[{'product_code':'NOT-IN-CATALOG','available':True}]}]}
    result=engine.edit(actor,session['id'],body)['session']
    chosen=result['draft']['lines'][0]
    assert chosen['id']==line['id'] and chosen['source_ref']['id']==original['id']
    assert chosen['source_ref']['customer_part_no']=='PART-1'
    assert chosen['quantity']=='3' and chosen['unit_price']=='1.13'
    assert chosen['resolved_identity']['sku_id']=='sku-1' and chosen['unit']=='支'
    with engine.store.connect() as db:
        persisted=json.loads(db.execute('SELECT draft FROM sales_assistant_sessions WHERE id=?',
                                      (session['id'],)).fetchone()[0])['lines'][0]
    assert 'match_candidates' not in persisted
    assert 'NOT-IN-CATALOG' not in dumps(result)


def test_candidates_never_expand_employee_scope_and_list_does_not_search(engine,tmp_path,monkeypatch):
    prepare(engine,tmp_path);actor=user('sales',['sales']);session=with_part(engine,actor)
    with engine.store.connect(write=True) as db:
        db.execute('INSERT INTO staff_users VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('other-sales','other-sales','销售乙','',dumps(['sales']),0,1,3,'',''))
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append(args)
        raise AssertionError('A list or unauthorized detail must not search historical data')
    monkeypatch.setattr('core.sales_assistant.search_candidates',forbidden)
    listed=engine.list(actor)
    assert listed['total']==1
    with pytest.raises(SalesAssistantError) as denied:
        engine.detail(user('other-sales',['sales']),session['id'])
    assert denied.value.http_status==403 and not calls


def test_missing_reference_does_not_block_explicit_current_code(engine,monkeypatch):
    actor=user('sales',['sales']);session=create(engine,actor)
    def forbidden(*args,**kwargs):
        raise AssertionError('An explicit unique current SKU does not need history')
    monkeypatch.setattr('core.sales_assistant.search_candidates',forbidden)
    result=engine.message(actor,session['id'],{'version':1,'idempotency_key':'explicit',
                          'text':'LS1000,2,1.13'})['session']
    assert result['draft']['lines'][0]['resolved_identity']['sku_id']=='sku-1'
    assert 'match_status' not in result['draft']['lines'][0]


def test_unreadable_reference_retains_input_and_does_not_mutate_draft(engine):
    actor=user('sales',['sales'])
    class Broken:
        @contextmanager
        def connect(self):
            raise ValueError('reference_replaced')
            yield
    engine.history_reference=Broken()
    session=with_part(engine,actor)
    line=session['draft']['lines'][0]
    assert line['match_status']['status']=='needs_review' and line['match_candidates']==[]
    assert line['source_ref']['customer_part_no']=='PART-1' and line['quantity']=='3'
    assert line['unit_price']=='1.13' and line['product_code']==''


def test_independent_reference_uses_current_business_catalog_once_per_view(engine,tmp_path):
    prepare(engine,tmp_path);actor=user('sales',['sales'])
    with engine.store.connect() as db:
        history=sqlite3.connect(':memory:');db.backup(history)
    history.execute('DELETE FROM records');history.commit()
    class Reference:
        opened=0
        @contextmanager
        def connect(self):
            self.opened+=1
            yield history
    reference=Reference();engine.history_reference=reference
    session=create(engine,actor)
    result=engine.message(actor,session['id'],{'version':1,'idempotency_key':'two-source-lines',
        'text':'客户料号=PART-1,3,1.13\n客户料号=PART-1,7,2.26'})['session']
    assert reference.opened==1
    assert len({line['id'] for line in result['draft']['lines']})==2
    assert [line['quantity'] for line in result['draft']['lines']]==['3','7']
    assert all(line['match_candidates'][0]['available'] for line in result['draft']['lines'])
    history.close()
