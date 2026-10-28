"""Regression tests for bounded, durable ERP logistics rollouts."""
import copy
import uuid
from types import SimpleNamespace

import pytest

from app import create_app
from core.freight_write_policy import FreightWritePolicy
from core.print_jobs import JobConflict
from core.update_jobs import UpdateJobs
from test_api_migration import store, order
from test_query_publication import query_snapshot


def run(jobs, mode='sync'):
    identity = str(uuid.uuid4())
    jobs.enqueue(identity, mode)
    assert jobs.process_one()
    return jobs.get(identity)


def policy(limit='0', allowlist='', scope=''):
    return FreightWritePolicy.from_config(limit, allowlist, scope)


def test_policy_requires_a_server_side_bound_and_selects_deterministically():
    changes = [{'tradeid': value} for value in ('A', 'B', 'C', 'D')]
    selected, deferred, detail = policy().select(changes)
    assert selected == [] and deferred == changes
    assert detail == {'reason': 'write_policy_missing', 'allowlist_deferred': 0,
                      'limit_deferred': 4}

    selected, deferred, detail = policy('2').select(changes)
    assert [row['tradeid'] for row in selected] == ['A', 'B']
    assert [row['tradeid'] for row in deferred] == ['C', 'D']
    assert detail['limit_deferred'] == 2

    selected, deferred, detail = policy('3', 'D,B,X').select(changes)
    assert [row['tradeid'] for row in selected] == ['B', 'D']
    assert [row['tradeid'] for row in deferred] == ['A', 'C']
    assert detail['allowlist_deferred'] == 2


def test_bounded_sync_writes_only_selected_and_next_job_continues_from_cache(store):
    data = query_snapshot([order('A'), order('B'), order('C')])
    calls = []
    jobs = UpdateJobs(store, SimpleNamespace(collect=lambda: copy.deepcopy(data)),
                      lambda target: calls.append(target['tradeid']) or True,
                      write_policy=policy('2'))

    first = run(jobs)
    assert first['status'] == 'partial'
    assert calls == ['A', 'B']
    assert first['result']['selected_orders'] == ['A', 'B']
    assert first['result']['deferred_orders'] == ['C']
    assert first['result']['selected'] == 2 and first['result']['deferred'] == 1
    assert first['result']['unchanged'] == 0

    second = run(jobs)
    assert second['status'] == 'succeeded'
    assert calls == ['A', 'B', 'C']
    assert second['result']['selected_orders'] == ['C']
    assert second['result']['deferred_orders'] == []
    assert second['result']['unchanged'] == 2


def test_explicit_all_scope_selects_and_writes_every_difference_in_one_job(store):
    orders = [order(f'ORDER#{number}') for number in range(1, 251)]
    data = query_snapshot(orders)
    calls = []
    jobs = UpdateJobs(store, SimpleNamespace(collect=lambda: copy.deepcopy(data)),
                      lambda target: calls.append(target['tradeid']) or True,
                      write_policy=policy(scope='all'))

    result = run(jobs)

    assert result['status'] == 'succeeded'
    assert calls == sorted(item['order_no'] for item in orders)
    assert result['result']['selected'] == 250
    assert result['result']['deferred'] == 0
    assert result['result']['deferred_orders'] == []
    assert result['result']['write_policy']['scope'] == 'all'
    assert result['result']['write_policy_result'] == {
        'reason': 'all', 'allowlist_deferred': 0, 'limit_deferred': 0}


def test_explicit_all_scope_has_no_arbitrary_candidate_cap():
    changes = [{'tradeid': f'ORDER#{number}'} for number in range(1, 1502)]
    selected, deferred, detail = policy(scope='all').select(changes)
    assert selected == changes
    assert deferred == []
    assert detail == {
        'reason': 'all', 'allowlist_deferred': 0, 'limit_deferred': 0}


def test_allowlist_and_limit_are_both_enforced(store):
    data = query_snapshot([order('A'), order('B'), order('C')])
    calls = []
    jobs = UpdateJobs(store, SimpleNamespace(collect=lambda: data),
                      lambda target: calls.append(target['tradeid']) or True,
                      write_policy=policy('1', 'B,C'))
    result = run(jobs)
    assert calls == ['B']
    assert result['result']['selected_orders'] == ['B']
    assert result['result']['deferred_orders'] == ['A', 'C']
    assert result['result']['write_policy_result'] == {
        'reason': 'bounded', 'allowlist_deferred': 1, 'limit_deferred': 1}


def test_global_transport_without_policy_refuses_every_erp_send(store):
    data = query_snapshot([order('A'), order('B')])
    calls = []
    jobs = UpdateJobs(store, SimpleNamespace(collect=lambda: data),
                      lambda target: calls.append(target),
                      write_policy=policy())
    result = run(jobs)
    assert calls == []
    assert result['status'] == 'partial'
    assert result['result']['logistics_status'] == 'policy_blocked'
    assert result['result']['selected'] == 0
    assert result['result']['deferred_orders'] == ['A', 'B']
    assert result['result']['unchanged'] == 0
    with pytest.raises(JobConflict) as error:
        jobs.enqueue(str(uuid.uuid4()), 'update')
    assert error.value.code == 'write_policy_required'


def test_queued_job_keeps_frozen_policy_across_config_change(store):
    data = query_snapshot([order('A'), order('B')])
    calls = []
    jobs = UpdateJobs(store, SimpleNamespace(collect=lambda: data),
                      lambda target: calls.append(target['tradeid']) or True,
                      write_policy=policy('1', 'A'))
    identity = str(uuid.uuid4())
    queued = jobs.enqueue(identity, 'sync')
    assert queued['result']['write_policy']['allowlist'] == ['A']
    jobs.write_policy = policy('100')
    assert jobs.process_one()
    assert calls == ['A']
    assert jobs.get(identity)['result']['deferred_orders'] == ['B']


def test_queued_job_keeps_explicit_all_scope_across_config_change(store):
    data = query_snapshot([order('A'), order('B')])
    calls = []
    jobs = UpdateJobs(store, SimpleNamespace(collect=lambda: data),
                      lambda target: calls.append(target['tradeid']) or True,
                      write_policy=policy(scope='all'))
    identity = str(uuid.uuid4())
    queued = jobs.enqueue(identity, 'sync')
    assert queued['result']['write_policy']['scope'] == 'all'
    jobs.write_policy = policy('1')
    assert jobs.process_one()
    assert calls == ['A', 'B']


def test_app_global_switch_alone_does_not_advertise_writes(store):
    erp = SimpleNamespace(update_logistics=lambda target: True, last_auth_error=None)
    app = create_app({'TESTING': True, 'SOURCE_MODE': 'api',
                      'LOGISTICS_WRITE_ENABLED': True,
                      'ERP_CLIENT': erp,
                      'API_SOURCE': SimpleNamespace(collect=lambda: query_snapshot())},
                     runtime_dir=store.runtime)
    health = app.test_client().get('/api/v2/health').get_json()
    assert health['capabilities']['logistics_write'] is False
    assert health['updates']['logistics_transport_enabled'] is True
    assert health['updates']['logistics_write_enabled'] is False


def test_app_advertises_writes_only_with_transport_and_explicit_all_scope(store):
    erp = SimpleNamespace(update_logistics=lambda target: True, last_auth_error=None)
    app = create_app({'TESTING': True, 'SOURCE_MODE': 'api',
                      'LOGISTICS_WRITE_ENABLED': True,
                      'LOGISTICS_WRITE_SCOPE': 'all',
                      'ERP_CLIENT': erp,
                      'API_SOURCE': SimpleNamespace(collect=lambda: query_snapshot())},
                     runtime_dir=store.runtime)
    health = app.test_client().get('/api/v2/health').get_json()
    assert health['capabilities']['logistics_write'] is True
    assert health['updates']['logistics_write_enabled'] is True
    assert health['updates']['logistics_write_policy']['scope'] == 'all'


@pytest.mark.parametrize('value', ['-1', '1001', 'many'])
def test_invalid_write_limit_fails_closed(value):
    with pytest.raises(ValueError):
        FreightWritePolicy.from_config(value, '')


@pytest.mark.parametrize('scope', ['true', 'unbounded', '*'])
def test_invalid_write_scope_fails_closed(scope):
    with pytest.raises(ValueError):
        FreightWritePolicy.from_config('0', '', scope)


@pytest.mark.parametrize('limit,allowlist', [('1', ''), ('0', 'A'), ('5', 'A,B')])
def test_explicit_all_scope_rejects_conflicting_finite_boundaries(limit, allowlist):
    with pytest.raises(ValueError):
        FreightWritePolicy.from_config(limit, allowlist, 'all')


def test_damaged_frozen_full_policy_fails_closed():
    thawed = FreightWritePolicy.thaw({
        'scope': 'all', 'write_all': True, 'max_per_job': 100,
        'allowlist': [], 'allow_all': True,
    })
    assert thawed.configured is False
    assert thawed.scope == 'disabled'
