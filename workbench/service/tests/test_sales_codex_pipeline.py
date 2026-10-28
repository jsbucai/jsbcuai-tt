from datetime import datetime,timedelta,timezone
import copy
import json
import uuid

import pytest

from core.sales_assistant import SalesAssistantError
from core.sales_codex_pipeline import CodexJobError,validate_output
from core.store import dumps
from test_sales_assistant import engine,create,edit_body
from test_daily_work import user,store


def enable(engine):
    with engine.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('sales_codex_policy',?)",(dumps({'enabled':True}),))


def request(engine,session=None,*,key='ai-1',text='请录单：螺栓 M20*80，3支，含税1.13元，交期2026-10-15，订单PO-AI'):
    enable(engine);session=session or create(engine)
    return engine.request_codex(user('sales',['sales']),session['id'],
        {'version':session['version'],'data_version':'v1','idempotency_key':key,'text':text})


def output(source_id,**updates):
    row={'source_ids':[source_id],'source_quote':'螺栓 M20*80，3支，含税1.13元',
         'product_code':'LS1000','original_name':'螺栓','original_spec':'M20*80',
         'original_material':'',
         'standard':'','grade':'8.8','surface':'热浸锌','customer_part_no':'','quantity':'3',
         'unit_price':'1.13','unit':'支','delivery_date':'2026-10-15','project_no':'',
         'line_no':'','drawing_no':''}
    data={'status':'ready_for_review','assistant_text':'草稿已整理，请核对。','questions':[],
          'new_product_suggestions':[],'draft':{'customer_order_no':'PO-AI','delivery_date':'2026-10-15',
          'note':'','tax_basis':'tax_inclusive','tax_rate':13,'lines':[row]},
          'source_coverage':[{'source_id':source_id,'status':'complete','line_count':1,'detail':'完整文字已整理'}]}
    return {**data,**updates}


def finish(engine,session=None):
    result=request(engine,session);token=uuid.uuid4().hex
    snapshot=engine.codex.claim(token)
    sid=snapshot['messages'][-1]['id']
    engine.codex.finish(snapshot['job_id'],token,output=output(sid),source_ids=[sid])
    return engine.detail(user('sales',['sales']),result['session']['id'])['session'],snapshot


def test_request_freezes_sources_without_rule_parsing_or_erp(engine):
    result=request(engine);session=result['session']
    assert session['version']==2 and session['draft']['lines']==[]
    assert session['codex_enabled'] is True and session['codex_job']['status']=='queued'
    assert session['codex_job']['base_version']==session['version']
    assert not session['codex_job']['can_adopt'] and not session['submission_operation_id']
    assert session['messages'][-1]['role']=='user'


def test_repeat_request_returns_same_job_and_never_appends_message_twice(engine):
    original=create(engine);one=request(engine,original)
    two=request(engine,original)
    assert two['replayed'] and two['job_id']==one['job_id']
    with engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM sales_codex_jobs').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM sales_assistant_messages').fetchone()[0]==1


def test_an_active_job_rolls_back_second_message_and_version(engine):
    session=request(engine)['session']
    with pytest.raises(SalesAssistantError) as error:request(engine,session,key='ai-2',text='另一条资料')
    assert error.value.code=='sales_codex_job_active'
    current=engine.detail(user('sales',['sales']),session['id'])['session']
    assert current['version']==session['version'] and len(current['messages'])==1


def test_model_result_is_proposal_and_cannot_confirm_old_draft(engine):
    session,snapshot=finish(engine)
    assert session['draft']['lines']==[] and session['codex_job']['can_adopt']
    assert session['codex_job']['preview']['lines'][0]['product_code']=='LS1000'
    with pytest.raises(SalesAssistantError) as error:
        engine.confirm(user('sales',['sales']),session['id'],{'version':session['version'],'idempotency_key':'c'})
    assert error.value.code=='sales_codex_adoption_required'


def test_adoption_is_versioned_bulk_replacement_and_not_confirmation(engine):
    session,snapshot=finish(engine);job=session['codex_job']
    body={'version':session['version'],'data_version':'v1','result_hash':job['result_hash'],'idempotency_key':'adopt'}
    adopted=engine.adopt_codex(user('sales',['sales']),session['id'],job['id'],body)['session']
    assert adopted['version']==3 and adopted['status']=='draft'
    assert adopted['codex_job']['status']=='adopted' and adopted['codex_job']['adopted_version']==3
    assert not adopted['codex_job']['can_adopt'] and not adopted['submission_operation_id']
    assert len(adopted['draft']['lines'])==1
    assert adopted['draft']['lines'][0]['untaxed_unit_price']=='1.000'
    replay=engine.adopt_codex(user('sales',['sales']),session['id'],job['id'],body)
    assert replay['replayed'] and replay['session']['version']==3


def test_edit_or_sync_during_analysis_makes_result_stale(engine):
    result=request(engine);token=uuid.uuid4().hex;snapshot=engine.codex.claim(token)
    engine.edit(user('sales',['sales']),result['session']['id'],edit_body(result['session']))
    source=snapshot['messages'][-1]['id']
    done=engine.codex.finish(snapshot['job_id'],token,output=output(source),source_ids=[source])
    assert done['status']=='stale'
    viewed=engine.detail(user('sales',['sales']),snapshot['session_id'])['session']
    assert viewed['codex_job']['status']=='stale' and not viewed['codex_job']['can_adopt']
    assert viewed['draft']['lines'][0]['unit_price']=='1.25'


def test_catalog_update_after_model_finish_prevents_adoption_preserving_result(engine):
    session,_=finish(engine);job=session['codex_job']
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'",(dumps('v2'),))
        db.execute("UPDATE records SET payload=json_set(payload,'$.spec','M20*90') WHERE kind='inventory'")
    with pytest.raises(SalesAssistantError) as error:
        engine.adopt_codex(user('sales',['sales']),session['id'],job['id'],
            {'version':session['version'],'data_version':'v2','result_hash':job['result_hash'],'idempotency_key':'a'})
    assert error.value.code=='sales_codex_not_ready'
    assert engine.detail(user('sales',['sales']),session['id'])['session']['codex_job']['preview']


def test_only_one_worker_claim_and_expired_lease_does_not_repeat_inference(engine):
    request(engine);token=uuid.uuid4().hex;now=datetime.now(timezone.utc)
    snapshot=engine.codex.claim(token,now=now)
    assert engine.codex.claim(uuid.uuid4().hex,now=now) is None
    assert engine.codex.claim(uuid.uuid4().hex,now=now+timedelta(minutes=26)) is None
    with engine.store.connect() as db:
        assert db.execute('SELECT status,error_code FROM sales_codex_jobs WHERE id=?',
                          (snapshot['job_id'],)).fetchone()['error_code']=='sales_codex_interrupted'


def test_disabled_codex_and_wrong_role_do_not_touch_draft(engine):
    session=create(engine)
    with pytest.raises(SalesAssistantError) as error:
        engine.request_codex(user('sales',['sales']),session['id'],
            {'version':1,'data_version':'v1','text':'a','idempotency_key':'ai'})
    assert error.value.code=='sales_codex_unavailable'
    enable(engine)
    with pytest.raises(SalesAssistantError) as wrong:
        engine.request_codex(user('pack',['stock_pack']),session['id'],
            {'version':1,'data_version':'v1','text':'a','idempotency_key':'other'})
    assert wrong.value.http_status==403


@pytest.mark.parametrize('mutate',[
    lambda v:v.update(confirmed=True),
    lambda v:v['draft']['lines'][0].update(source_ids=['other-customer']),
    lambda v:v.update(source_coverage=[]),
    lambda v:v['draft'].update(tax_rate=0),
    lambda v:v['draft']['lines'][0].update(source_ids=[{}]),
])
def test_untrusted_model_output_cannot_invent_sources_or_permission(mutate):
    value=output('s');mutate(value)
    with pytest.raises(CodexJobError):validate_output(value,['s'])


def test_missing_known_excel_row_blocks_ready_status():
    value=output('sheet:1');value['source_coverage'][0]['source_id']='attachment'
    checked=validate_output(value,['attachment'],['sheet:1','sheet:2'])
    assert checked['status']=='needs_clarification'
    assert checked['issues'][0]['code']=='sales_codex_source_rows_missing'


def test_clarification_and_new_product_suggestions_cannot_become_adoptable():
    value=output('s',questions=['请补充单价'])
    assert validate_output(value,['s'])['status']=='needs_clarification'
    value=output('s');value['draft']['lines'][0]['unit_price']=None
    checked=validate_output(value,['s'])
    assert checked['draft']['lines'][0]['unit_price']=='' and checked['status']=='needs_clarification'


def test_repeated_sku_keeps_distinct_source_and_lines(engine):
    result=request(engine);token=uuid.uuid4().hex;frozen=engine.codex.claim(token)
    source=frozen['messages'][0]['id'];value=output(source)
    value['draft']['lines'].append({**copy.deepcopy(value['draft']['lines'][0]),'quantity':'4'})
    value['source_coverage'][0]['line_count']=2
    engine.codex.finish(frozen['job_id'],token,output=value,source_ids=[source])
    session=engine.detail(user('sales',['sales']),result['session']['id'])['session'];job=session['codex_job']
    adopted=engine.adopt_codex(user('sales',['sales']),session['id'],job['id'],
        {'version':session['version'],'data_version':'v1','result_hash':job['result_hash'],'idempotency_key':'adopt'})['session']
    rows=adopted['draft']['lines']
    assert [r['quantity'] for r in rows]==['3','4'] and len({r['id'] for r in rows})==2


def test_stock_refresh_does_not_invalidate_finished_analysis(engine):
    session,_=finish(engine);job=session['codex_job']
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE settings SET value=? WHERE key='data_version'",(dumps('stock-v2'),))
        db.execute("UPDATE records SET payload=json_set(payload,'$.quantity','99') WHERE kind='inventory'")
    result=engine.adopt_codex(user('sales',['sales']),session['id'],job['id'],
        {'version':session['version'],'data_version':'stock-v2','result_hash':job['result_hash'],'idempotency_key':'after-stock'})
    assert result['session']['codex_job']['status']=='adopted'
