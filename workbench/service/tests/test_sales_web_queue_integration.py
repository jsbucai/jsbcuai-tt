"""Sales submit only freezes a reviewed draft into the shared browser lane."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from core.sales_assistant import SalesAssistantError
from core.sales_web_queue import POLICY_SETTING
from core.sales_web_import import WebImportConfig, _values
from core.sales_web_transport import WebImportOutcome
from core.store import dumps
from core.unit_policy import WEB_BLANK_UNIT_BASIS, WEB_MIXED_UNIT_BASIS
from test_daily_work import store, user
from test_sales_assistant import engine, create, edit_body as _fixed_edit_body


ACCOUNT_ID='c'*64


def edit_body(session,key='edit',**extra):
    # Web-import preflight uses the actual import date; October fixture dates
    # must not expire as the calendar advances.
    body=_fixed_edit_body(session,key,**extra)
    due=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=5)).isoformat()
    body['delivery_date']=due
    for line in body['lines']:line['delivery_date']=due
    return body


def _policy(engine, *, enabled=True, **changes):
    binding={'customer_name':'客户甲','erp_customer_id':'erp-customer-1',
             'profile_id':'P','account_id':ACCOUNT_ID,'match_count':1,
             'source':'erp_customer_exact_lookup','source_hash':'b'*64,
             'observed_at':datetime.now(timezone.utc).isoformat()}
    value={
        'enabled':enabled,'profile_id':'P','account_id':ACCOUNT_ID,
        'department_name':'销售部','maker_name':'罗健树',
        'handler_by_owner':{'sales':'李瑄'},
        'warehouse_id':'08390','warehouse_name':'江苏优螺',
        'template_sha256':'a'*64,'customer_bindings':{'客户甲':binding},
    }
    value.update(changes)
    with engine.store.connect(write=True) as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(POLICY_SETTING,dumps(value)))
    return value


def _confirmed(engine, *, lines=1, split_dates=False, customer='客户甲'):
    actor=user('sales',['sales'])
    if customer!='客户甲':
        with engine.store.connect(write=True) as db:
            db.execute('''INSERT INTO sales_customer_owners
                (profile_id,customer_name,owner_id,status,source,updated_at)
                VALUES (?,?,?,?,?,?)''',('P',customer,'sales','assigned','customer_confirmed',''))
        session=engine.create(actor,{'profile_id':'P','customer_name':customer,
                                     'idempotency_key':'create-special'})['session']
    else:
        session=create(engine,actor)
    due=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=5)).isoformat()
    later=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=6)).isoformat()
    request=edit_body(session)
    request['delivery_date']=due
    request['lines']=[{**request['lines'][0], 'quantity':str(index+2),
                       'delivery_date':later if split_dates and index else due} for index in range(lines)]
    edited=engine.edit(actor,session['id'],request)['session']
    confirmed=engine.confirm(actor,session['id'],
        {'version':edited['version'],'idempotency_key':'confirm'})['session']
    return actor,confirmed


def _outcome(operation_id,number,*,imported=True,table=True,rows=1):
    documents=tuple(number[:-3]+f'{index:03d}' for index in range(1,rows+1))
    return WebImportOutcome(str(uuid.UUID(operation_id)),number,
        'imported' if imported else 'unknown',
        'web_import_confirmed' if imported else 'web_import_unconfirmed',
        True,True,False,'','web_import_confirmation' if imported else '',False,
        table,'d'*64,rows if imported else 0,rows,documents,rows)


def test_default_closed_preserves_confirmed_draft_and_does_not_enqueue(engine):
    actor,confirmed=_confirmed(engine)
    with pytest.raises(SalesAssistantError) as error:
        engine.submit(actor,confirmed['id'],
            {'version':confirmed['version'],'idempotency_key':'submit-closed'})
    assert error.value.code=='sales_submission_unavailable' and error.value.http_status==503
    assert engine.detail(actor,confirmed['id'])['session']['status']=='confirmed'
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==0


def test_submit_atomically_freezes_two_repeated_sku_lines_and_replays(engine):
    actor,confirmed=_confirmed(engine,lines=2)
    _policy(engine)
    body={'version':confirmed['version'],'idempotency_key':'submit-two-lines'}
    result=engine.submit(actor,confirmed['id'],body)
    operation_id=result['operation_id']; number=result['erp_order_no']
    assert len(number)==20 and number.startswith('UL')
    assert result['session']['status']=='submitting'
    assert result['session']['submission_operation_id']==operation_id
    assert result['session']['submit_available'] is False
    assert engine.submit(actor,confirmed['id'],body)=={**result,'replayed':True}
    with engine.store.connect() as db:
        operations=db.execute("SELECT * FROM web_operations WHERE kind='sales_order_import'").fetchall()
        assert len(operations)==1 and operations[0]['target_id']==number
        assert operations[0]['status']=='queued'
        payload=json.loads(db.execute('SELECT payload_json FROM web_operation_payloads').fetchone()[0])
        assert payload['schema']=='sales_web_import_v2'
        assert payload['document_mode']=='line_document'
        assert len(payload['document_numbers'])==2 and len(set(payload['document_numbers']))==2
        assert all(len(value)==20 for value in payload['document_numbers'])
        assert payload['confirmed_version']==confirmed['version']
        assert payload['confirmed_hash']==confirmed['confirmed_hash']
        assert payload['line_count']==2
        assert [line['product_code'] for line in json.loads(payload['frozen_session']['draft'])['lines']]==['LS1000','LS1000']
        assert payload['import_config']['maker_name']=='罗健树'
        assert payload['import_config']['handler_name']=='李瑄'
        assert payload['import_config']['warehouse_name']=='江苏优螺'
        assert payload['customer_binding']['erp_customer_id']=='erp-customer-1'
    with pytest.raises(SalesAssistantError) as error:
        engine.edit(actor,confirmed['id'],edit_body(confirmed,key='late-edit'))
    assert error.value.code=='sales_draft_locked'


@pytest.mark.parametrize('changes,code', [
    ({'customer_bindings':{}},'sales_web_customer_unverified'),
    ({'handler_by_owner':{}},'sales_web_handler_unmapped'),
    ({'template_sha256':''},'sales_web_configuration_incomplete'),
    ({'account_id':'not-a-sha256'},'sales_web_configuration_incomplete'),
    ({'warehouse_id':'other'},'sales_web_draft_not_importable'),
])
def test_incomplete_or_mismatched_policy_never_enqueues(engine,changes,code):
    actor,confirmed=_confirmed(engine)
    _policy(engine,**changes)
    with pytest.raises(SalesAssistantError) as error:
        engine.submit(actor,confirmed['id'],{'version':confirmed['version'],'idempotency_key':'submit-check'})
    assert error.value.code==code
    assert engine.detail(actor,confirmed['id'])['session']['status']=='confirmed'
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==0


def test_old_fixed_customer_binding_can_enqueue_for_fresh_worker_verification(engine):
    actor,confirmed=_confirmed(engine)
    policy=_policy(engine)
    stale={**policy['customer_bindings']['客户甲'],
           'observed_at':'2020-01-01T00:00:00+00:00'}
    _policy(engine,customer_bindings={'客户甲':stale})
    result=engine.submit(actor,confirmed['id'],
        {'version':confirmed['version'],'idempotency_key':'submit-old-binding'})
    assert result['session']['status']=='submitting'
    with engine.store.connect() as db:
        payload=json.loads(db.execute('SELECT payload_json FROM web_operation_payloads').fetchone()[0])
        assert payload['customer_binding']['observed_at']==stale['observed_at']


@pytest.mark.parametrize('observed_at', ['2020-01-01T00:00:00', 'invalid',
    (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()])
def test_invalid_or_future_customer_binding_timestamp_never_enqueues(engine,observed_at):
    actor,confirmed=_confirmed(engine)
    policy=_policy(engine)
    binding={**policy['customer_bindings']['客户甲'],'observed_at':observed_at}
    _policy(engine,customer_bindings={'客户甲':binding})
    with pytest.raises(SalesAssistantError) as error:
        engine.submit(actor,confirmed['id'],{'version':confirmed['version'],'idempotency_key':'submit-stale'})
    assert error.value.code=='sales_web_customer_unverified'


def test_changed_catalog_never_enqueues(engine):
    actor,confirmed=_confirmed(engine)
    _policy(engine)
    with engine.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)",(dumps('v2'),))
    with pytest.raises(SalesAssistantError) as error:
        engine.submit(actor,confirmed['id'],{'version':confirmed['version'],'idempotency_key':'submit-catalog'})
    assert error.value.code=='sales_catalog_changed'
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==0


def test_distinct_line_delivery_dates_are_preserved_in_frozen_import_table(engine):
    actor=user('sales',['sales'])
    session=create(engine,actor)
    due=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=5)).isoformat()
    later=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=6)).isoformat()
    request=edit_body(session)
    request['delivery_date']=due
    request['lines']=[{**request['lines'][0],'delivery_date':day}
                      for day in (due,later)]
    draft=engine.edit(actor,session['id'],request)['session']
    assert draft['draft']['issues']==[]
    confirmed=engine.confirm(actor,session['id'],
        {'version':draft['version'],'idempotency_key':'confirm-dates'})['session']
    assert confirmed['status']=='confirmed'
    _policy(engine)
    queued=engine.submit(actor,session['id'],
        {'version':confirmed['version'],'idempotency_key':'submit-dates'})
    with engine.store.connect() as db:
        payload=json.loads(db.execute('SELECT payload_json FROM web_operation_payloads WHERE operation_id=?',
                                      (queued['operation_id'],)).fetchone()[0])
        config=WebImportConfig(Path('/tmp/not-used'),Path('/tmp/not-used'),payload['order_number'],
            datetime.fromisoformat(payload['import_config']['order_date']).date(),
            'P',payload['import_config']['data_version'],
            'sales','销售部','罗健树','李瑄','08390','江苏优螺')
        values,*_= _values(payload['frozen_session'],config)
        assert [row['交货日期'].isoformat() for row in values]==[due,later]
        assert values[0]['摘要'].endswith('#交期='+due)
        assert values[1]['明细备注']!=values[0]['明细备注']


@pytest.mark.parametrize('missing,expected', [
    ('po','customer_order_no_missing'),
    ('delivery','delivery_missing'),
])
def test_missing_po_or_delivery_is_saved_as_issue_before_confirmation(
        engine,missing,expected):
    actor=user('sales',['sales']);session=create(engine,actor)
    request=edit_body(session)
    future=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=5)).isoformat()
    request['delivery_date']=future
    request['lines'][0]['delivery_date']=future
    if missing=='po': request['customer_order_no']=''
    else:
        request['delivery_date']=''
        request['lines'][0]['delivery_date']=''
    draft=engine.edit(actor,session['id'],request)['session']
    assert expected in {issue['code'] for issue in draft['draft']['issues']}
    with pytest.raises(SalesAssistantError) as error:
        engine.confirm(actor,session['id'],{'version':draft['version'],'idempotency_key':'confirm-incomplete'})
    assert error.value.code=='sales_draft_needs_review'
    assert engine.detail(actor,session['id'])['session']['draft']==draft['draft']


def test_delivery_before_today_is_issue_before_confirmation(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    past=(datetime.now(ZoneInfo('Asia/Shanghai')).date()-timedelta(days=1)).isoformat()
    request=edit_body(session)
    request['delivery_date']=past
    request['lines'][0]['delivery_date']=past
    draft=engine.edit(actor,session['id'],request)['session']
    assert 'delivery_before_order_date' in {issue['code'] for issue in draft['draft']['issues']}
    with pytest.raises(SalesAssistantError) as error:
        engine.confirm(actor,session['id'],{'version':draft['version'],'idempotency_key':'confirm-past'})
    assert error.value.code=='sales_draft_needs_review'


def test_summary_separator_and_mixed_warehouses_are_issues_before_confirmation(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute("SELECT payload FROM records WHERE id='record-1'").fetchone()[0])
        product.update(sku='LS2000',sku_id='sku-2',warehouse_id='other')
        db.execute("INSERT INTO records(kind,id,sku,payload) VALUES ('inventory','record-2','LS2000',?)",
                   (dumps(product),))
    request=edit_body(session)
    request['customer_order_no']='PO#123'
    request['lines'].append({**request['lines'][0], 'product_code':'LS2000',
                             'warehouse':'other'})
    draft=engine.edit(actor,session['id'],request)['session']
    assert {'customer_order_no_invalid','warehouse_split_required'} <= {
        issue['code'] for issue in draft['draft']['issues']}
    with pytest.raises(SalesAssistantError) as error:
        engine.confirm(actor,session['id'],{'version':draft['version'],'idempotency_key':'confirm-split'})
    assert error.value.code=='sales_draft_needs_review'


def test_non_dimension_catalog_spec_cannot_guess_size_from_model(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute("SELECT payload FROM records WHERE id='record-1'").fetchone()[0])
        product.update(spec='GB/T5783',model='M20*80')
        db.execute("UPDATE records SET payload=? WHERE id='record-1'",(dumps(product),))
    draft=engine.edit(actor,session['id'],edit_body(session))['session']
    assert 'summary_size_unverified' in {issue['code'] for issue in draft['draft']['issues']}
    line=draft['draft']['lines'][0]
    assert line['spec']=='GB/T5783' and line['model']=='M20*80'
    assert line['summary_dimension']=='' and line['summary_dimension_source']==''
    with pytest.raises(SalesAssistantError) as error:
        engine.confirm(actor,session['id'],{'version':draft['version'],'idempotency_key':'confirm-no-size'})
    assert error.value.code=='sales_draft_needs_review'


def test_human_confirmed_nonstandard_size_is_frozen_and_reused_without_client_spoof(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute("SELECT payload FROM records WHERE id='record-1'").fetchone()[0])
        product.update(spec='美制非标',model='混合说明')
        db.execute("UPDATE records SET payload=? WHERE id='record-1'",(dumps(product),))
    body=edit_body(session)
    body['lines'][0].update(summary_dimension='UNC 1/4-20 × 2',
        summary_metadata_confirmed=True,summary_metadata_actor='spoof',
        summary_metadata_at='2099-01-01T00:00:00')
    edited=engine.edit(actor,session['id'],body)['session']
    line=edited['draft']['lines'][0]
    assert not edited['draft']['issues']
    assert line['summary_dimension']=='UNC 1/4-20 × 2'
    assert line['summary_dimension_source']=='human_confirmed'
    assert line['summary_metadata_actor']=='sales'
    assert line['summary_metadata_at']!='2099-01-01T00:00:00'
    confirmed=engine.confirm(actor,session['id'],
        {'version':edited['version'],'idempotency_key':'confirm-human-dimension'})['session']
    with engine.store.connect() as db:
        frozen=dict(db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',
                               (session['id'],)).fetchone())
    values,_,_,summary,lines=_values(frozen,WebImportConfig(
        Path('/unused/original.xls'),Path('/unused/order.xls'),
        'UL261007123456ABCDEF',datetime.now(ZoneInfo('Asia/Shanghai')).date(),
        'P','v1','sales','销售部','罗健树','李瑄','08390','江苏优螺'))
    assert summary.split('#')[5].strip()=='UNC 1/4-20 × 2'
    old_client=edit_body(confirmed,key='old-client-human-size')
    old_client['lines'][0]['quantity']='4'
    retained=engine.edit(actor,session['id'],old_client)['session']
    assert retained['draft']['lines'][0]['summary_dimension']=='UNC 1/4-20 × 2'
    assert retained['draft']['lines'][0]['summary_metadata_actor']=='sales'
    assert not retained['draft']['issues']
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute("SELECT payload FROM records WHERE id='record-1'").fetchone()[0])
        product['spec']='美制非标新版'
        db.execute("UPDATE records SET payload=? WHERE id='record-1'",(dumps(product),))
    stale=engine.edit(actor,session['id'],edit_body(retained,key='catalog-changed'))['session']
    assert 'summary_catalog_changed' in {issue['code'] for issue in stale['draft']['issues']}
    rereview=edit_body(stale,key='human-rereview')
    rereview['lines'][0].update(summary_dimension='UNC 1/4-20 × 2',
                                 summary_metadata_confirmed=True)
    fixed=engine.edit(actor,session['id'],rereview)['session']
    assert not fixed['draft']['issues']
    assert fixed['draft']['lines'][0]['catalog_summary']['spec']=='美制非标新版'


def test_human_size_override_keeps_catalog_conflict_and_invalid_input_for_review(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    invalid=edit_body(session)
    invalid['lines'][0].update(summary_dimension='M20#90',summary_metadata_confirmed=True)
    pending=engine.edit(actor,session['id'],invalid)['session']
    line=pending['draft']['lines'][0]
    assert line['summary_dimension']=='M20#90'
    assert line['summary_dimension_source']=='human_pending'
    assert 'summary_size_invalid' in {issue['code'] for issue in pending['draft']['issues']}
    reviewed=edit_body(pending,key='human-size-override')
    reviewed['lines'][0].update(summary_dimension='M20X90',summary_metadata_confirmed=True)
    edited=engine.edit(actor,session['id'],reviewed)['session']
    line=edited['draft']['lines'][0]
    assert not edited['draft']['issues']
    assert line['summary_dimension_source']=='human_confirmed'
    assert line['summary_dimension_catalog_conflict'] is True
    assert line['catalog_summary']['spec']=='M20*80'


def test_retail_customer_cannot_be_used_without_named_test_session_approval(engine):
    actor,confirmed=_confirmed(engine,customer='备用零售客户')
    binding={'customer_name':'备用零售客户','erp_customer_id':'retail-erp-id',
             'profile_id':'P','account_id':ACCOUNT_ID,'match_count':1,
             'source':'erp_customer_exact_lookup','source_hash':'b'*64,
             'observed_at':datetime.now(timezone.utc).isoformat()}
    _policy(engine,customer_bindings={'备用零售客户':binding})
    with pytest.raises(SalesAssistantError) as error:
        engine.submit(actor,confirmed['id'],{'version':confirmed['version'],'idempotency_key':'retail-not-approved'})
    assert error.value.code=='sales_retail_test_not_approved'
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==0


def test_submit_and_enqueue_roll_back_together_when_result_record_fails(engine,monkeypatch):
    actor,confirmed=_confirmed(engine)
    _policy(engine)
    def fail(*args,**kwargs): raise RuntimeError('injected failure')
    monkeypatch.setattr(engine,'_finish',fail)
    with pytest.raises(RuntimeError):
        engine.submit(actor,confirmed['id'],{'version':confirmed['version'],'idempotency_key':'submit-rollback'})
    with engine.store.connect() as db:
        assert db.execute("SELECT status,submission_operation_id FROM sales_assistant_sessions WHERE id=?",
                          (confirmed['id'],)).fetchone()[:]==('confirmed','')
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==0


def test_unknown_result_only_recovers_complete_local_receipt_without_new_upload(engine):
    actor,confirmed=_confirmed(engine)
    _policy(engine)
    submitted=engine.submit(actor,confirmed['id'],
        {'version':confirmed['version'],'idempotency_key':'submit-unknown'})
    operation_id=submitted['operation_id'];number=submitted['erp_order_no']
    token='worker-token-for-sales'
    assert engine.web_operations.claim_next(token)['id']==operation_id
    outcome=_outcome(operation_id,number,imported=False)
    result=engine.finish_web_import(operation_id,token,outcome=outcome)
    assert result['status']=='uncertain'
    current=engine.detail(actor,confirmed['id'])['session']
    assert current['status']=='submission_unknown' and current['erp_order_no']==''
    assert current['submission_state']=='uncertain'
    assert engine.web_operations.claim_next('another-worker-token') is None
    with pytest.raises(SalesAssistantError):
        engine.reconcile_web_import(operation_id,_outcome(operation_id,number,table=False))
    closed=engine.reconcile_web_import(operation_id,_outcome(operation_id,number))
    assert closed['status']=='succeeded'
    assert closed['result']['completion_basis']=='web_import_confirmation'
    assert closed['result']['unit_verified'] is False
    assert closed['result']['erp_readback_performed'] is False
    current=engine.detail(actor,confirmed['id'])['session']
    assert current['status']=='submitted' and current['erp_order_no']==number
    assert current['submission_state']=='succeeded'
    assert current['submission_completion_basis']=='web_import_confirmation'
    assert current['erp_readback_performed'] is False


def test_worker_success_requires_validated_table_bound_to_complete_web_receipt(engine):
    actor,confirmed=_confirmed(engine)
    _policy(engine)
    submitted=engine.submit(actor,confirmed['id'],
        {'version':confirmed['version'],'idempotency_key':'submit-unit'})
    operation_id=submitted['operation_id'];number=submitted['erp_order_no']
    token='worker-token-for-sales'
    engine.web_operations.claim_next(token)
    result=engine.finish_web_import(operation_id,token,outcome=_outcome(operation_id,number,table=False))
    assert result['status']=='uncertain' and result['result']['code']=='web_import_receipt_incomplete'
    assert engine.detail(actor,confirmed['id'])['session']['status']=='submission_unknown'


def test_complete_web_receipt_finishes_repeated_sku_lines_once_without_unit_readback(engine):
    actor,confirmed=_confirmed(engine,lines=2)
    _policy(engine)
    submitted=engine.submit(actor,confirmed['id'],
        {'version':confirmed['version'],'idempotency_key':'mixed-unit-submit'})
    operation_id=submitted['operation_id'];number=submitted['erp_order_no']
    token='worker-token-for-mixed-sales'
    engine.web_operations.claim_next(token)
    evidence=_outcome(operation_id,number,rows=2)
    finished=engine.finish_web_import(operation_id,token,outcome=evidence)
    assert finished['status']=='succeeded'
    assert finished['result']['unit_verified'] is False
    assert finished['result']['verified'] is False
    assert finished['result']['table_verified'] is True
    assert finished['result']['web_import_confirmed'] is True
    assert finished['result']['file_sha256']=='d'*64
    assert finished['result']['imported_rows']==2
    assert engine.detail(actor,confirmed['id'])['session']['status']=='submitted'
    with pytest.raises(SalesAssistantError) as error:
        engine.finish_web_import(operation_id,token,outcome=evidence)
    assert error.value.code=='sales_web_operation_not_owned'
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==1


@pytest.mark.parametrize('changes',[
    {'imported_rows':1}, {'expected_rows':1}, {'imported_rows':True},
    {'file_sha256':'bad'}, {'table_verified':False}, {'erp_readback_performed':True},
    {'unit_verified':True}, {'progress_observed':False}, {'attempted':False},
    {'intent_id':str(uuid.uuid4())}, {'completion_basis':'erp_readback'},
    {'status':'verified','code':'verified'},
])
def test_incomplete_or_legacy_success_never_completes_current_file_only_operation(engine,changes):
    actor,confirmed=_confirmed(engine,lines=2)
    _policy(engine)
    submitted=engine.submit(actor,confirmed['id'],
        {'version':confirmed['version'],'idempotency_key':'receipt-binding'})
    operation_id=submitted['operation_id'];number=submitted['erp_order_no']
    token='worker-token-receipt-binding'
    engine.web_operations.claim_next(token)
    outcome=replace(_outcome(operation_id,number,rows=2),**changes)
    result=engine.finish_web_import(operation_id,token,outcome=outcome)
    assert result['status']=='uncertain'
    assert result['result']['completion_basis']==''
    current=engine.detail(actor,confirmed['id'])['session']
    assert current['status']=='submission_unknown'
    assert current['erp_readback_performed'] is False
    assert engine.web_operations.claim_next('another-worker-token') is None


def test_wrong_full_number_cannot_mark_session_submitted(engine):
    actor,confirmed=_confirmed(engine)
    _policy(engine)
    submitted=engine.submit(actor,confirmed['id'],
        {'version':confirmed['version'],'idempotency_key':'submit-wrong-number'})
    operation_id=submitted['operation_id'];number=submitted['erp_order_no']
    token='worker-token-for-sales'
    engine.web_operations.claim_next(token)
    result=engine.finish_web_import(operation_id,token,
        outcome=_outcome(operation_id,number+'X'))
    assert result['status']=='uncertain'
    assert engine.detail(actor,confirmed['id'])['session']['erp_order_no']==''


def test_worker_preflight_block_keeps_original_operation_without_second_queue(engine):
    actor,confirmed=_confirmed(engine)
    _policy(engine)
    body={'version':confirmed['version'],'idempotency_key':'submit-blocked'}
    submitted=engine.submit(actor,confirmed['id'],body)
    token='worker-token-for-sales'
    engine.web_operations.claim_next(token)
    result=engine.finish_web_import(submitted['operation_id'],token,
                                    blocked_code='preflight_incomplete')
    assert result['status']=='blocked'
    assert engine.detail(actor,confirmed['id'])['session']['status']=='submission_blocked'
    assert engine.submit(actor,confirmed['id'],body)['replayed']
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM web_operations WHERE kind='sales_order_import'").fetchone()[0]==1


def test_catalog_summary_and_confirmed_manual_fields_survive_legacy_android_edit(engine):
    actor=user('sales',['sales'])
    with engine.store.connect(write=True) as db:
        record=json.loads(db.execute("SELECT payload FROM records WHERE id='record-1'").fetchone()[0])
        record.update(spec='M20*80',model='8.8级特殊热浸锌',
                      drawing_no='未经人工确认的图号')
        db.execute("UPDATE records SET payload=? WHERE id='record-1'",(dumps(record),))
    session=create(engine,actor)
    body=edit_body(session)
    due=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=5)).isoformat()
    body['delivery_date']=due;body['lines'][0]['delivery_date']=due
    body['lines'][0].update(project_no='项目甲',drawing_no='DW-1',
                            grade='8.8',surface='特殊热浸锌',
                            summary_metadata_confirmed=True)
    edited=engine.edit(actor,session['id'],body)['session']
    line=edited['draft']['lines'][0]
    assert line['spec']=='M20*80' and line['model']=='8.8级特殊热浸锌'
    assert line['summary_dimension']=='M20X80'
    assert line['summary_dimension_source']=='catalog_spec_shape'
    assert line['grade']=='8.8' and line['surface']=='特殊热浸锌'
    assert line['project_no']=='项目甲' and line['drawing_no']=='DW-1'
    assert line['summary_metadata_confirmed_by']=='sales'
    assert line['manual_summary']=={'project_no':'项目甲','drawing_no':'DW-1',
                                    'grade':'8.8','surface':'特殊热浸锌'}
    first_line_id=line['id']
    confirmed=engine.confirm(actor,session['id'],
        {'version':edited['version'],'idempotency_key':'confirm-metadata'})['session']
    with engine.store.connect() as db:
        frozen=dict(db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',(session['id'],)).fetchone())
    values,_,_,summary,lines=_values(frozen,WebImportConfig(
        Path('/unused/original.xls'),Path('/unused/order.xls'),
        'UL261007123456ABCDEF',datetime.now(ZoneInfo('Asia/Shanghai')).date(),
        'P','v1','sales','销售部','罗健树','李瑄','08390','江苏优螺'))
    assert [part.strip() for part in summary.split('#')[:10]]==[
        'PO-123','项目甲','1','DW-1','螺栓','M20X80','8.8级','特殊热浸锌','3','LS1000']
    assert values[0]['摘要']==summary
    assert [part.strip() for part in lines[0].detail_note.split('#')[:2]]==['PO-123','项目甲']
    old_client=edit_body(confirmed,key='old-client-edit')
    old_client['lines'][0]['quantity']='7'
    changed=engine.edit(actor,session['id'],old_client)['session']
    retained=changed['draft']['lines'][0]
    assert retained['id']==first_line_id
    assert retained['manual_summary']=={'project_no':'项目甲','drawing_no':'DW-1',
                                        'grade':'8.8','surface':'特殊热浸锌'}
    assert retained['spec']=='M20*80' and retained['model']=='8.8级特殊热浸锌'
    assert retained['summary_dimension']=='M20X80'
    assert retained['surface']=='特殊热浸锌'
    assert retained['quantity']=='7'
    assert not changed['draft']['issues']


def test_unconfirmed_manual_summary_is_review_issue(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    body=edit_body(session)
    body['lines'][0]['drawing_no']='DW-unreviewed'
    edited=engine.edit(actor,session['id'],body)['session']
    assert 'summary_metadata_unconfirmed' in {issue['code'] for issue in edited['draft']['issues']}
    assert edited['draft']['lines'][0]['drawing_no']==''
    with pytest.raises(SalesAssistantError) as error:
        engine.confirm(actor,session['id'],{'version':edited['version'],'idempotency_key':'bad-metadata-confirm'})
    assert error.value.code=='sales_draft_needs_review'


def test_duplicate_sku_manual_summary_needs_line_ids_on_edit(engine):
    actor=user('sales',['sales']);session=create(engine,actor)
    body=edit_body(session)
    body['lines']=[{**body['lines'][0],'project_no':f'项目{index}',
                    'summary_metadata_confirmed':True} for index in (1,2)]
    edited=engine.edit(actor,session['id'],body)['session']
    assert len({line['id'] for line in edited['draft']['lines']})==2
    old_client=edit_body(edited,key='legacy-ambiguous')
    old_client['lines']=[dict(body['lines'][0]),dict(body['lines'][1])]
    for line in old_client['lines']:
        line.pop('project_no');line.pop('summary_metadata_confirmed')
    with pytest.raises(SalesAssistantError) as error:
        engine.edit(actor,session['id'],old_client)
    assert error.value.code=='sales_line_identity_ambiguous'
    with_ids=edit_body(edited,key='identified-edit')
    with_ids['lines']=[{**old_client['lines'][index],'id':old_line['id']}
                       for index,old_line in enumerate(edited['draft']['lines'])]
    changed=engine.edit(actor,session['id'],with_ids)['session']
    assert [line['project_no'] for line in changed['draft']['lines']]==['项目1','项目2']
