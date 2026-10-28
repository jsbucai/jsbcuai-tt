"""Success-cache expiry, source replacement and bounded recovery regressions."""
import copy
from datetime import datetime,timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pytest
from core.update_jobs import UpdateJobs, _freight_observed_changes
from core.freight_reconciliation import fingerprints, recheck_reason, RECHECK_AFTER, observation
from core.freight_write_policy import FreightWritePolicy
from test_api_migration import store,order
from test_query_publication import query_snapshot,run


def setup(store,names=('A',)):
    data=query_snapshot([order(n) for n in names]);calls=[]
    jobs=UpdateJobs(store,SimpleNamespace(collect=lambda:copy.deepcopy(data)),lambda p:calls.append(p.copy()) or True)
    run(jobs);calls.clear()
    return data,calls,jobs


def expire(store):
    with store.connect(write=True) as db:
        db.execute("UPDATE freight_cache SET updated_at='2000-01-01T00:00:00'")
        db.execute("UPDATE freight_reconciliation SET confirmed_at='2000-01-01T00:00:00',last_attempt_at='2000-01-01T00:00:00'")


def test_same_number_category_change_is_not_ignored():
    target={'freightnumber':'@10/20#AD044-D#10','freightname':'需采购','freightcode':'xcg'}
    observed=dict(target,observed_at='2026-10-23T10:00:00+08:00',freightname='待到货')
    cache={'updated_at':'2026-10-02T18:17:36'}
    assert _freight_observed_changes(observed,cache,target)
    observed.update(freightname=None,freightcode=None)
    assert not _freight_observed_changes(observed,cache,target)
    observed.update(freightname='待到货',observed_at='2026-10-01T10:00:00+08:00')
    assert not _freight_observed_changes(observed,cache,target)


def test_wms_supported_category_is_compared_but_null_is_not_erasure():
    assert observation({'freightcompany':'待到货'})['freightcode']=='ddh'
    for value in (None,'','2858435901271711505',{'name':'待到货'}):
        assert 'freightname' not in observation({'freightcompany':value})


def test_legacy_cache_is_rechecked_once_and_survives_restart(store):
    data,calls,jobs=setup(store)
    with store.connect(write=True) as db:db.execute('DELETE FROM freight_reconciliation')
    result=run(jobs)['result']
    assert result['freight_recheck_selected']==['A'] and len(calls)==1
    jobs=UpdateJobs(store,jobs.source,jobs.sender)
    assert run(jobs)['result']['selected']==0 and len(calls)==1


def test_unobservable_category_receipt_expires_without_inventing_erp_drift(store):
    data,calls,jobs=setup(store)
    data['logistics_observations']={'A':{'observed_at':'2000-01-01T00:00:00'}}
    expire(store)
    result=run(jobs)['result']
    assert result['freight_recheck_selected']==['A']
    assert result['erp_changed_logistics_orders']==[] and len(calls)==1
    assert run(jobs)['result']['selected']==0


@pytest.mark.parametrize('kind',['document_id','line_id','quantity'])
def test_replaced_or_edited_order_not_skipped_even_when_target_same(store,kind):
    data,calls,jobs=setup(store)
    if kind=='document_id':data['logistics_observations']={'A':{'document_id':'new-document'}}
    elif kind=='line_id':
        data['orders'][0]['lines'][0]['line_id']='new-line'
        data['records']['orders'][0]['line_id']='new-line'
    else:data['orders'][0]['lines'][0]['open_qty']='7'
    result=run(jobs)['result']
    assert result['source_changed_logistics_orders']==['A'] and len(calls)==1
    assert run(jobs)['result']['selected']==0


def test_reads_and_our_own_freight_write_never_change_source_fingerprint():
    data=query_snapshot([order('A')]);before=fingerprints(data)
    data['source_updated_at']='2099-01-01T10:00:00+08:00'
    data['logistics_observations']={'A':{'observed_at':data['source_updated_at'],'freightnumber':'updated','freightname':'需采购'}}
    assert fingerprints(data)==before


def test_recovery_is_bounded_oldest_first_and_failed_row_cannot_starve(store,monkeypatch):
    monkeypatch.setattr('core.update_jobs.RECHECK_BATCH',1)
    data,calls,jobs=setup(store,('A','B','C'));expire(store)
    jobs.sender=lambda p:calls.append(p.copy()) or p['tradeid']!='A'
    first=run(jobs)['result'];assert first['selected_orders']==['A'] and first['failed']==1 and first['freight_recheck_deferred']==2
    assert run(jobs)['result']['selected_orders']==['B']
    assert run(jobs)['result']['selected_orders']==['C']
    with store.connect() as db:
        assert db.execute("SELECT updated_at FROM freight_cache WHERE tradeid='A'").fetchone()[0]=='2000-01-01T00:00:00'


def test_new_business_change_precedes_renewal_and_permission_applies_before_cap(store,monkeypatch):
    monkeypatch.setattr('core.update_jobs.RECHECK_BATCH',1)
    data,calls,jobs=setup(store,('A','B','C'));expire(store)
    updated=query_snapshot(data['orders']+[order('NEW')]);data.clear();data.update(updated)
    jobs.write_policy=FreightWritePolicy(max_per_job=1,allow_all=True)
    assert run(jobs)['result']['selected_orders']==['NEW']
    jobs.write_policy=FreightWritePolicy(allowlist=frozenset({'C'}))
    assert run(jobs)['result']['selected_orders']==['C']


def test_unknown_outcome_stays_held_and_preview_never_renews(store):
    data,calls,jobs=setup(store);expire(store)
    assert run(jobs,'refresh')['result']['succeeded']==0 and not calls
    jobs.sender=lambda p:calls.append(p.copy()) or None
    assert run(jobs)['result']['unknown']==1
    jobs.sender=lambda p:calls.append(p.copy()) or True
    assert run(jobs)['result']['selected']==0 and len(calls)==1


def test_lease_boundary_timezone_and_future_clock():
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    old=(now-RECHECK_AFTER).isoformat();cache={'updated_at':old};proof={'confirmed_at':old,'fingerprint':'f'}
    assert recheck_reason(cache,proof,'f',now)=='lease_expired'
    old=(now-RECHECK_AFTER+timedelta(seconds=1)).isoformat();cache['updated_at']=old;proof['confirmed_at']=old
    assert recheck_reason(cache,proof,'f',now)==''
    future=(now+timedelta(hours=1)).isoformat();cache['updated_at']=future;proof['confirmed_at']=future
    assert recheck_reason(cache,proof,'f',now)=='lease_expired'
