"""Only fake adapters: no ERP endpoints, browser writes or payment calls."""
from dataclasses import asdict, replace
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from core.daily_work import DailyWork
from core.sales_assistant import SalesAssistant, _digest
from core.sales_submission import (SalesSubmission,SubmissionPolicy,AdapterCapabilities,
    PreparedSalesOrder,ErpSalesOrder,SalesOrderLookup,SubmissionBlocked,_hash,_restore)
from core.sales_pricing import quote_price
from core.store import dumps
from test_daily_work import store,user,enable_assignment

NOW=datetime(2026,10,7,1,tzinfo=timezone.utc).timestamp()


class FakeAdapter:
    def __init__(self):
        self.capabilities=AdapterCapabilities('P','account','test-only contract','a'*64,
            True,True,True,True,True,True,True)
        self.inspections=0;self.lookups=0;self.writes=0;self.orders=[]
        self.prepared_changes={};self.order_changes={};self.lookup_changes={}
        self.inspect_callback=None;self.submit_mode='success';self.lookup_error=False

    def inspect(self,request):
        self.inspections+=1
        if self.inspect_callback:self.inspect_callback()
        evidence=tuple((line.client_line_id,'b'*64) for line in request.lines)
        total=sum((Decimal(line.untaxed_line_amount) if line.untaxed_line_amount else
                   Decimal(line.quantity)*Decimal(line.unit_price) for line in request.lines),Decimal(0))
        return replace(PreparedSalesOrder(_hash(asdict(request)),request.profile_id,request.account_id,
            request.external_reference,request.customer_name,'customer-1',1,'c'*64,
            request.sales_employee_id,request.operator_employee_id,request.employee_mapping_hash,'d'*64,
            request.audit_status,True,request.lines,evidence,evidence,evidence,str(total),'a'*64,NOW,'e'*64),
            **self.prepared_changes)

    def lookup(self,intent):
        self.lookups+=1
        if self.lookup_error:raise TimeoutError('never log credential-like adapter errors')
        return replace(SalesOrderLookup(True,'P','account',intent.request.external_reference,tuple(self.orders)),
            **self.lookup_changes)

    def submit_once(self,intent):
        self.writes+=1
        # The adapter verifies the intent was committed and the existing draft
        # editor is locked before it performs its one irreversible fake action.
        with self.store.connect() as db:
            assert db.execute('SELECT count(*) FROM sales_submission_intents').fetchone()[0]==1
            assert db.execute('SELECT status FROM sales_assistant_sessions').fetchone()[0]=='submitting'
        if self.submit_mode=='crash_before_write':raise SystemExit('power loss')
        if self.submit_mode not in {'timeout_no_order'}:
            req=intent.request;prepared=intent.prepared
            self.orders=[replace(ErpSalesOrder('ERP-1',req.profile_id,req.account_id,req.external_reference,
                req.customer_name,prepared.customer_erp_id,req.sales_employee_id,req.operator_employee_id,
                req.customer_order_no,req.delivery_date,req.note,req.audit_status,req.lines,
                prepared.order_total,NOW,'f'*64),**self.order_changes)]
        if self.submit_mode=='crash_after_write':raise SystemExit('power loss')
        if self.submit_mode.startswith('timeout'):raise TimeoutError('unknown outcome')


@pytest.fixture
def submission(store):
    daily=DailyWork(store,auto_project=False);enable_assignment(daily)
    with store.connect(write=True) as db:
        product={'profile_id':'P','warehouse_id':'08390','product_id':'p1','sku_id':'s1',
            'base_unit':'1','unit_name':'','sku':'LS1000','product_name':'螺栓',
            'spec':'M20*80','model':'8.8级特殊热浸锌'}
        db.execute("INSERT INTO records(kind,id,sku,payload) VALUES ('inventory','stock','LS1000',?)",(dumps(product),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)",(dumps('v1'),))
    assistant=SalesAssistant(store);actor=user('sales',['sales'])
    session=assistant.create(actor,{'profile_id':'P','customer_name':'客户甲','idempotency_key':'create'})['session']
    session=assistant.edit(actor,session['id'],{'version':session['version'],'idempotency_key':'edit',
        'customer_order_no':'客户-PO','delivery_date':'2099-10-10','note':'已确认',
        'lines':[{'product_code':'LS1000','quantity':'3','unit_price':'1.25','unit':'支','warehouse':'08390'}]})['session']
    session=assistant.confirm(actor,session['id'],{'version':session['version'],'idempotency_key':'confirm'})['session']
    # Current UI confirmation has no tax fields. This simulates a future
    # explicitly reviewed draft contract rather than inferring them from WMS.
    with store.connect(write=True) as db:
        row=db.execute('SELECT * FROM sales_assistant_sessions').fetchone()
        frozen=json.loads(row['confirmed_payload']);frozen['draft'].update(tax_basis='tax_inclusive',tax_rate='13')
        db.execute('UPDATE sales_assistant_sessions SET draft=?,confirmed_payload=?,confirmed_hash=?',
            (dumps(frozen['draft']),dumps(frozen),_digest(frozen)))
    adapter=FakeAdapter();adapter.store=store
    policy=SubmissionPolicy(True,True,'P','account')
    engine=SalesSubmission(store,adapter,policy,clock=lambda:NOW)
    with store.connect(write=True) as db:
        for who,employee in (('sales','ERP-sales'),('admin','ERP-admin')):
            db.execute('INSERT INTO sales_submission_staff_mappings VALUES (?,?,?,?,?,?,?,?)',
                ('P',who,employee,who,'approved','1'*64,'2026-10-07T01:00:00+00:00','account'))
    return engine,adapter,assistant,actor,session


def body(session,key='submit'):return {'version':session['version'],'idempotency_key':key}


def session_state(engine):
    with engine.store.connect() as db:return db.execute('SELECT status FROM sales_assistant_sessions').fetchone()[0]


def change_frozen_draft(engine,change):
    with engine.store.connect(write=True) as db:
        frozen=json.loads(db.execute('SELECT confirmed_payload FROM sales_assistant_sessions').fetchone()[0])
        change(frozen['draft'])
        db.execute('UPDATE sales_assistant_sessions SET draft=?,confirmed_payload=?,confirmed_hash=?',
            (dumps(frozen['draft']),dumps(frozen),_digest(frozen)))


def test_default_disabled_keeps_confirmation_and_never_calls_adapter(submission):
    engine,adapter,_,actor,session=submission
    engine.policy=SubmissionPolicy()
    result=engine.submit(actor,session['id'],body(session))
    assert result['status']=='blocked' and result['detail']=='sales_submission_disabled'
    assert adapter.inspections==adapter.lookups==adapter.writes==0
    assert session_state(engine)=='confirmed'


def test_single_durable_intent_readback_and_idempotent_request(submission):
    engine,adapter,assistant,actor,session=submission
    result=engine.submit(actor,session['id'],body(session))
    assert result['status']=='submitted' and result['erp_order_no']=='ERP-1'
    assert adapter.writes==1 and session_state(engine)=='submitted'
    again=engine.submit(actor,session['id'],body(session))
    assert again=={**result,'replayed':True} and adapter.writes==1
    assert engine.submit(actor,session['id'],body(session,'different-key'))['status']=='submitted'
    with pytest.raises(SubmissionBlocked) as error:
        engine.submit(actor,session['id'],{**body(session),'version':session['version']+1})
    assert error.value.code=='sales_submission_idempotency_conflict'
    with engine.store.connect() as db:
        assert db.execute('SELECT count(*) FROM sales_submission_intents').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM records WHERE kind="orders"').fetchone()[0]==0
    # The original existing draft API rejects editing the submitted draft.
    with pytest.raises(Exception):assistant.edit(actor,session['id'],{'version':session['version'],'idempotency_key':'locked','lines':[]})


@pytest.mark.parametrize('mode,status',[('timeout_after_write','submitted'),('timeout_no_order','submission_unknown')])
def test_timeout_uses_unique_readback_and_different_keys_never_resubmit(submission,mode,status):
    engine,adapter,_,actor,session=submission;adapter.submit_mode=mode
    result=engine.submit(actor,session['id'],body(session))
    assert result['status']==status and adapter.writes==1
    resumed=SalesSubmission(engine.store,adapter,replace(engine.policy,enabled=False,allow_submit=False),clock=lambda:NOW)
    assert resumed.submit(actor,session['id'],body(session,'new-key'))['status']==status
    assert adapter.writes==1


@pytest.mark.parametrize('mode,status',[('crash_before_write','submission_unknown'),('crash_after_write','submitted')])
def test_power_loss_even_before_write_leaves_intent_that_can_only_be_looked_up(submission,mode,status):
    engine,adapter,_,actor,session=submission;adapter.submit_mode=mode
    with pytest.raises(SystemExit):engine.submit(actor,session['id'],body(session))
    assert session_state(engine)=='submitting'
    restarted=SalesSubmission(engine.store,adapter,engine.policy,clock=lambda:NOW)
    assert restarted.submit(actor,session['id'],body(session))['status']==status
    assert adapter.writes==1 and adapter.inspections==1


@pytest.mark.parametrize('capability',['write_contract_verified','readback_contract_verified','complete_unique_lookup',
    'durable_document_binding','durable_line_binding','creates_unapproved_order','account_verified'])
def test_unaccepted_contracts_or_audit_semantics_block_before_inspection(submission,capability):
    engine,adapter,_,actor,session=submission
    adapter.capabilities=replace(adapter.capabilities,**{capability:False})
    assert engine.submit(actor,session['id'],body(session))['status']=='blocked'
    assert adapter.inspections==adapter.writes==0 and session_state(engine)=='confirmed'


def test_fixed_owner_and_actual_operator_require_distinct_approved_erp_mappings(submission):
    engine,adapter,_,actor,session=submission
    with engine.store.connect(write=True) as db:db.execute("DELETE FROM sales_submission_staff_mappings WHERE staff_id='admin'")
    result=engine.submit(user('admin',['sales'],True),session['id'],body(session))
    assert result['detail']=='sales_employee_mapping_unverified' and adapter.writes==0
    with engine.store.connect(write=True) as db:db.execute("UPDATE staff_users SET roles='[\"sales\"]' WHERE id='buyer'")
    with pytest.raises(SubmissionBlocked) as error:engine.submit(user('buyer',['sales']),session['id'],body(session))
    assert error.value.code=='sales_customer_owner_conflict'
    with engine.store.connect(write=True) as db:db.execute("UPDATE sales_customer_owners SET owner_id='buyer'")
    with pytest.raises(SubmissionBlocked):engine.submit(actor,session['id'],body(session))
    result=engine.submit(user('admin',['sales'],True),session['id'],body(session,'changed-owner'))
    assert result['detail']=='sales_confirmation_scope_changed' and adapter.writes==0


@pytest.mark.parametrize('change,code',[
    ('hold','restoration_hold'),('source','sales_source_changed'),('catalog','sales_catalog_changed'),
    ('unit','sales_unit_evidence_changed'),('tax','sales_tax_price_unverified'),('tamper','sales_confirmed_payload_changed')])
def test_local_evidence_must_still_match_confirmed_version(submission,change,code):
    engine,adapter,_,actor,session=submission
    with engine.store.connect(write=True) as db:
        if change=='hold':db.execute("INSERT OR REPLACE INTO settings VALUES ('restoration_hold','true')")
        elif change=='source':db.execute("UPDATE settings SET value='\"v2\"' WHERE key='data_version'")
        elif change in {'catalog','unit'}:
            row=json.loads(db.execute('SELECT payload FROM records WHERE id="stock"').fetchone()[0])
            row['sku_id' if change=='catalog' else 'unit_name']='different' if change=='catalog' else '箱'
            db.execute('UPDATE records SET payload=? WHERE id="stock"',(dumps(row),))
        elif change=='tamper':db.execute("UPDATE sales_assistant_sessions SET confirmed_hash=?",('2'*64,))
        else:
            row=db.execute('SELECT confirmed_payload FROM sales_assistant_sessions').fetchone();frozen=json.loads(row[0])
            frozen['draft'].pop('tax_basis');frozen['draft'].pop('tax_rate')
            for line in frozen['draft']['lines']:
                line.pop('tax_basis',None);line.pop('tax_rate',None)
            db.execute('UPDATE sales_assistant_sessions SET draft=?,confirmed_payload=?,confirmed_hash=?',
                (dumps(frozen['draft']),dumps(frozen),_digest(frozen)))
    result=engine.submit(actor,session['id'],body(session))
    assert result['status']=='blocked' and result['detail']==code and adapter.writes==0


def test_mid_inspection_source_change_revalidation_preserves_input_and_never_records_intent(submission):
    engine,adapter,_,actor,session=submission
    def change_source():
        with engine.store.connect(write=True) as db:db.execute("UPDATE settings SET value='\"v2\"' WHERE key='data_version'")
    adapter.inspect_callback=change_source
    result=engine.submit(actor,session['id'],body(session))
    assert result['detail']=='sales_source_changed' and adapter.writes==0
    with engine.store.connect() as db:assert db.execute('SELECT count(*) FROM sales_submission_intents').fetchone()[0]==0
    assert session_state(engine)=='confirmed'


@pytest.mark.parametrize('field',['base_unit_name','unit'])
def test_changed_alternate_unit_name_requires_reconfirmation(submission,field):
    engine,adapter,_,actor,session=submission
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute('SELECT payload FROM records WHERE id="stock"').fetchone()[0]);product[field]='箱'
        db.execute('UPDATE records SET payload=? WHERE id="stock"',(dumps(product),))
    assert engine.submit(actor,session['id'],body(session))['detail']=='sales_unit_evidence_changed'
    assert adapter.writes==0


def test_user_confirmed_piece_override_flows_through_fake_adapter_only(submission):
    engine,adapter,assistant,actor,session=submission
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute('SELECT payload FROM records WHERE id="stock"').fetchone()[0])
        product['unit_name']='箱'
        db.execute('UPDATE records SET payload=? WHERE id="stock"',(dumps(product),))
    reviewed=assistant.edit(actor,session['id'],{'version':session['version'],'idempotency_key':'review-box',
        'customer_order_no':'客户-PO','delivery_date':'2099-10-10','note':'已确认',
        'lines':[{'product_code':'LS1000','quantity':'3','unit_price':'1.25','unit':'支','warehouse':'08390'}]})['session']
    assert not reviewed['draft']['issues']
    line=reviewed['draft']['lines'][0]
    assert line['unit']=='支' and line['erp_unit_names']['unit_name']=='箱'
    confirmed=assistant.confirm(actor,session['id'],{'version':reviewed['version'],
        'idempotency_key':'confirm-box'})['session']
    with engine.store.connect(write=True) as db:
        row=db.execute('SELECT confirmed_payload FROM sales_assistant_sessions').fetchone()
        frozen=json.loads(row[0]);frozen['draft'].update(tax_basis='tax_inclusive',tax_rate='13')
        db.execute('UPDATE sales_assistant_sessions SET draft=?,confirmed_payload=?,confirmed_hash=?',
            (dumps(frozen['draft']),dumps(frozen),_digest(frozen)))
    result=engine.submit(actor,session['id'],body(confirmed,'fake-box'))
    assert result['status']=='submitted' and adapter.writes==1
    assert adapter.orders[0].lines[0].unit=='支'


def test_new_erp_named_provenance_requires_draft_reconfirmation(submission):
    engine,adapter,_,actor,session=submission
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute('SELECT payload FROM records WHERE id="stock"').fetchone()[0]);product['base_unit_name']='支'
        db.execute('UPDATE records SET payload=? WHERE id="stock"',(dumps(product),))
    assert engine.submit(actor,session['id'],body(session))['detail']=='sales_unit_evidence_changed'
    assert adapter.writes==0


def test_approved_mapping_cannot_be_reused_after_account_switch(submission):
    engine,adapter,_,actor,session=submission
    engine.policy=replace(engine.policy,account_id='other-account')
    adapter.capabilities=replace(adapter.capabilities,account_id='other-account')
    assert engine.submit(actor,session['id'],body(session))['detail']=='sales_employee_mapping_unverified'
    assert adapter.writes==0


def test_legacy_mapping_schema_never_inherits_the_current_account(store):
    with store.connect(write=True) as db:
        db.execute('''CREATE TABLE sales_submission_staff_mappings(profile_id TEXT,staff_id TEXT,
            erp_employee_id TEXT,erp_employee_name TEXT,status TEXT,evidence_hash TEXT,updated_at TEXT,
            PRIMARY KEY(profile_id,staff_id))''')
        db.execute('INSERT INTO sales_submission_staff_mappings VALUES (?,?,?,?,?,?,?)',
            ('P','sales','employee','旧映射','approved','a'*64,'old'))
    engine=SalesSubmission(store)
    restarted=SalesSubmission(store)
    with store.connect() as db:
        row=dict(db.execute('SELECT * FROM sales_submission_staff_mappings').fetchone())
        assert row['account_id']=='' and row['erp_employee_id']=='employee'
        with pytest.raises(SubmissionBlocked):restarted._mapping(db,'P','sales','new-account')


def test_legacy_confirmed_warehouse_name_remains_bound_to_exact_identity(submission):
    engine,adapter,_,actor,session=submission
    with engine.store.connect(write=True) as db:
        product=json.loads(db.execute('SELECT payload FROM records WHERE id="stock"').fetchone()[0]);product['warehouse_name']='主仓'
        db.execute('UPDATE records SET payload=? WHERE id="stock"',(dumps(product),))
    change_frozen_draft(engine,lambda draft:draft['lines'][0].update(warehouse='主仓'))
    result=engine.submit(actor,session['id'],body(session))
    assert result['status']=='submitted' and adapter.writes==1
    assert adapter.orders[0].lines[0].product_identity=='P|08390|p1|s1|1'


@pytest.mark.parametrize('merge_or_swap',[None,'merge','swap'])
def test_same_product_repeated_lines_need_distinct_durable_line_bindings(submission,merge_or_swap):
    engine,adapter,_,actor,session=submission
    def duplicate(draft):
        unit, amount = quote_price('1','3','tax_inclusive')
        draft['lines'].append({**draft['lines'][0], 'quantity':'1', 'unit_price':'3',
            'quoted_unit_price':'3', 'untaxed_unit_price':unit, 'untaxed_line_amount':amount})
    change_frozen_draft(engine,duplicate)
    original_submit=adapter.submit_once
    def submit_and_modify(intent):
        original_submit(intent)
        order=adapter.orders[0]
        if merge_or_swap=='merge':adapter.orders=[replace(order,lines=(order.lines[0],))]
        elif merge_or_swap=='swap':
            # Same product, same total: switching prices between client line
            # identities is still a wrong confirmed draft.
            one,two=order.lines
            adapter.orders=[replace(order,lines=(replace(one,unit_price=two.unit_price),replace(two,unit_price=one.unit_price)))]
    adapter.submit_once=submit_and_modify
    result=engine.submit(actor,session['id'],body(session))
    assert result['status']==('submitted' if merge_or_swap is None else 'submission_unknown')
    assert adapter.writes==1
    assert [line.client_line_id for line in adapter.orders[0].lines]==(['1'] if merge_or_swap=='merge' else ['1','2'])


def test_recovery_cannot_switch_to_another_account_after_intent(submission):
    engine,adapter,_,actor,session=submission;adapter.submit_mode='timeout_no_order'
    result=engine.submit(actor,session['id'],body(session));before=adapter.lookups
    engine.policy=replace(engine.policy,account_id='other')
    adapter.capabilities=replace(adapter.capabilities,account_id='other')
    assert engine.recover(actor,result['attempt_id'])['detail']=='sales_recovery_account_changed'
    assert adapter.writes==1 and adapter.lookups==before


def test_corrupt_intent_cannot_change_its_frozen_attempt_binding(submission):
    engine,adapter,_,actor,session=submission;adapter.submit_mode='timeout_no_order'
    result=engine.submit(actor,session['id'],body(session));before=adapter.lookups
    with engine.store.connect(write=True) as db:
        payload=json.loads(db.execute('SELECT payload FROM sales_submission_intents').fetchone()[0])
        payload['request']['external_reference']='other-order'
        db.execute('UPDATE sales_submission_intents SET payload=?,payload_hash=?',(dumps(payload),_hash(payload)))
    with pytest.raises(SubmissionBlocked) as error:engine.recover(actor,result['attempt_id'])
    assert error.value.code=='sales_submission_intent_corrupt' and adapter.lookups==before and adapter.writes==1


def test_recovery_rechecks_terminal_state_after_lock_and_cannot_downgrade_receipt(submission):
    engine,adapter,_,actor,session=submission;adapter.submit_mode='timeout_no_order'
    result=engine.submit(actor,session['id'],body(session))
    with engine.store.connect() as db:
        intent=_restore(json.loads(db.execute('SELECT payload FROM sales_submission_intents').fetchone()[0]))
    req=intent.request;prepared=intent.prepared
    # An order may become visible after an earlier complete lookup found none.
    adapter.orders=[ErpSalesOrder('ERP-1',req.profile_id,req.account_id,req.external_reference,
        req.customer_name,prepared.customer_erp_id,req.sales_employee_id,req.operator_employee_id,
        req.customer_order_no,req.delivery_date,req.note,req.audit_status,req.lines,
        prepared.order_total,NOW,'f'*64)]
    competing=SalesSubmission(engine.store,adapter,engine.policy,clock=lambda:NOW)
    @contextmanager
    def another_recovery_finishes_before_lock(session_id):
        assert competing.recover(actor,result['attempt_id'])['status']=='submitted'
        adapter.lookup_error=True
        yield
    engine._lock=another_recovery_finishes_before_lock
    after=engine.recover(actor,result['attempt_id'])
    assert after['status']=='submitted' and after['erp_order_no']=='ERP-1' and adapter.writes==1
    with engine.store.connect(write=True) as db:
        stale=dict(db.execute('SELECT * FROM sales_submission_attempts').fetchone())
        engine._set(db,stale,actor['id'],'submission_unknown','stale worker failure')
    assert engine._result(result['attempt_id'])['status']=='submitted'


@pytest.mark.parametrize('changes',[
    {'customer_match_count':2},{'audit_status':'approved'},{'audit_status_verified':False},
    {'tax_price_evidence':()},{'employee_mapping_hash':'bad'},{'order_total':'4'},
    {'observed_at':NOW-301},{'contract_hash':'b'*64},
])
def test_preflight_unique_customer_employee_unit_tax_price_and_audit_proofs(submission,changes):
    engine,adapter,_,actor,session=submission;adapter.prepared_changes=changes
    assert engine.submit(actor,session['id'],body(session))['status']=='blocked' and adapter.writes==0


@pytest.mark.parametrize('changes',[
    {'customer_erp_id':'wrong'},{'audit_status':'approved'},{'sales_employee_id':'other'},
    {'external_reference':'other'},{'order_total':'99'},{'lines':()},{'observed_at':NOW-301},
])
def test_readback_mismatch_does_not_claim_success_or_unlock_or_resubmit(submission,changes):
    engine,adapter,_,actor,session=submission;adapter.order_changes=changes
    result=engine.submit(actor,session['id'],body(session))
    assert result['status']=='submission_unknown' and result['erp_order_no']=='' and adapter.writes==1
    assert session_state(engine)=='submission_unknown'
    assert engine.submit(actor,session['id'],body(session,'another'))['status']=='submission_unknown'
    assert adapter.writes==1


def test_readback_ambiguous_or_unavailable_stays_unknown_without_write_retry(submission):
    engine,adapter,_,actor,session=submission;adapter.submit_mode='timeout_no_order'
    result=engine.submit(actor,session['id'],body(session));adapter.lookup_error=True
    assert engine.recover(actor,result['attempt_id'])['detail']=='sales_readback_unavailable'
    adapter.lookup_error=False;adapter.lookup_changes={'orders':(None,None)}
    assert engine.recover(actor,result['attempt_id'])['detail']=='sales_readback_ambiguous'
    assert adapter.writes==1
