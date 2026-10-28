"""Removed ERP demand and posted-order locks must not hold new work."""
import copy
import json
from types import SimpleNamespace
import pytest
from test_order_lifecycle import Client, raw, seed_active, persist
from test_api_migration import store, order
from test_query_publication import query_snapshot, run
from core.order_lifecycle import OrderLifecycle, _query_exact, LifecycleQueryError, OrderNotFound
from core.update_jobs import UpdateJobs
from core.update_jobs import _freight_observed_changes


def closed_stage(number='OLD',state='600'):
    return {'profile_id':'1','updates':{number:{'lifecycle_state':'archived','process_state':state,
        'ended_at':'2026-10-23T10:00:00+08:00','last_error':'' if state=='600' else 'absent_from_complete_erp_sets'}},
        'observed_at':'2026-10-23T10:00:00+08:00','shadow_order_ids':[],'release_order_ids':[number]}


def test_removed_order_releases_ghost_demand_and_reentry_uses_live_rows(store):
    life=OrderLifecycle(store);client=Client();seed_active(life,client,store)
    _,stage=life.prepare(client,'1',[],'2026-10-23T10:00:00+08:00')
    orders,rows,stage=life.finalize(stage,[],[])
    assert orders==rows==[] and stage['release_order_ids']==['A']
    assert stage['updates']['A']['process_state']=='absent'
    persist(store,stage)
    source,again=life.prepare(client,'1',[raw('A',400)],'2026-10-23T11:00:00+08:00')
    assert len(source)==1 and not again['release_order_ids']
    assert again['updates']['A']['lifecycle_state']=='active'
    assert again['updates']['A']['last_error']==''


@pytest.mark.parametrize('response',[
    {'code':'0','trade_orders':[]},
    {'code':'0','TotalResults':False,'trade_orders':[]},
    {'code':'0','TotalResults':1,'trade_orders':[]},
    {'code':'0','TotalResults':0,'totalResults':1,'trade_orders':[]},
    {'code':'1','TotalResults':0,'trade_orders':[]},
    {'code':'0','TotalResults':0,'trade_orders':None},
    {'code':'0','TotalResults':0,'trade_orders':[raw('OTHER',400)]},
])
def test_malformed_missing_or_unrelated_result_never_proves_removal(response):
    with pytest.raises(LifecycleQueryError) as error:
        _query_exact(SimpleNamespace(call=lambda *args:response),'1','A')
    assert not isinstance(error.value,OrderNotFound)


def test_successful_zero_can_release_previously_unposted_removed_order(store):
    life=OrderLifecycle(store);client=Client();seed_active(life,client,store)
    _,stage=life.prepare(client,'1',[],'2026-10-23T09:00:00+08:00',shipped_rows=[raw('A',500)])
    _,_,stage=life.finalize(stage,[],[]);persist(store,stage)
    _,removed=life.prepare(client,'1',[],'2026-10-23T10:00:00+08:00')
    assert removed['release_order_ids']==['A'] and not removed['shadow_order_ids']
    _,present=life.prepare(client,'1',[],'2026-10-23T10:00:00+08:00',shipped_rows=[raw('A',500)])
    assert present['shadow_order_ids']==['A'] and not present['release_order_ids']


def test_posted_unknown_no_longer_blocks_new_order_with_same_product(store):
    OrderLifecycle(store);calls=[]
    data=query_snapshot([order('OLD')]);jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:copy.deepcopy(data)),lambda p:None)
    first=run(jobs);attempt=first['result']['attempts'][0]['id'];target=first['result']['attempts'][0]['freight_number']
    data.clear();data.update(query_snapshot([order('NEW')]));data['order_lifecycle']=closed_stage()
    jobs.sender=lambda p:calls.append(p['tradeid']) or True
    done=run(jobs)
    assert calls==['NEW'] and done['result']['unknown']==0
    assert done['result']['retired_unknown_orders']==['OLD']
    with store.connect() as db:
        row=db.execute('SELECT status,target FROM freight_attempts WHERE id=?',(attempt,)).fetchone()
        assert row['status']=='superseded' and json.loads(row['target'])['freightnumber']==target
        assert db.execute('SELECT resolution FROM freight_resolution_audit WHERE attempt_id=?',(attempt,)).fetchone()[0]=='closed_order'
    assert run(jobs)['result']['retired_unknown_orders']==[] and calls==['NEW']


def test_current_reused_number_is_never_retired_from_stale_history(store):
    OrderLifecycle(store)
    data=query_snapshot([order('A')]);jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:data),lambda p:None)
    run(jobs);stage=closed_stage('A');persist(store,stage)
    assert jobs._retire_closed_attempts(dict(data,order_lifecycle=stage))==[]
    with store.connect() as db:assert db.execute('SELECT status FROM freight_attempts').fetchone()[0]=='unknown'


def test_removed_success_cache_cannot_skip_recreated_order(store):
    OrderLifecycle(store);calls=[]
    data=query_snapshot([order('A')]);jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:copy.deepcopy(data)),lambda p:calls.append(p['tradeid']) or True)
    run(jobs);stage=closed_stage('A','absent');persist(store,stage)
    jobs._retire_closed_attempts(dict(data,orders=[],order_lifecycle=stage))
    run(jobs);assert calls==['A','A']


@pytest.mark.parametrize('observed,value,expected',[
    ('2026-10-23T10:01:00+08:00','',True),
    ('2026-10-23T02:01:00Z','old',True),
    ('2026-10-23T10:01:00','wanted',False),
    ('2026-10-23T09:59:00+08:00','',False),
    ('2026-10-23T10:00:00+08:00','',False),
    ('','',False),('invalid','',False),('2026-10-23','',False),
    ('2026-10-23T10:01:00+08:00',None,False),
])
def test_source_read_time_not_cache_reuse_time_controls_repair(observed,value,expected):
    observation={'observed_at':observed,'freightnumber':value}
    assert _freight_observed_changes(observation,{'updated_at':'2026-10-23T10:00:00'},
        {'freightnumber':'wanted'}) is expected


def test_erp_erased_logistics_repair_once_then_old_read_cannot_replay(store):
    calls=[];data=query_snapshot([order('A')])
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:copy.deepcopy(data)),lambda p:calls.append(p) or True)
    run(jobs)
    with store.connect(write=True) as db:
        db.execute("UPDATE freight_cache SET updated_at='2000-01-01T10:00:00'")
    data['logistics_observations']={'A':{'observed_at':'2000-01-02T10:00:00+08:00','freightnumber':''}}
    repaired=run(jobs)
    assert len(calls)==2 and repaired['result']['erp_changed_logistics_orders']==['A']
    assert run(jobs)['result']['erp_changed_logistics_orders']==[] and len(calls)==2


def test_read_timestamp_alone_does_not_change_mobile_query_version(store):
    from core.api_source import publish
    from core.classification_pipeline import calculate_query_snapshot
    data=query_snapshot([order('A')]);calc=calculate_query_snapshot(data)
    data['logistics_observations']={'A':{'observed_at':'2026-10-23T10:00:00+08:00','freightnumber':''}}
    before=publish(store,data,calc)
    data['logistics_observations']['A']['observed_at']='2026-10-23T11:00:00+08:00'
    assert publish(store,data,calc)==before
