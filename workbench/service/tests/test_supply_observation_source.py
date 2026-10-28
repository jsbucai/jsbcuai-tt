"""Non-atomic ERP sources converge only the changed supply scopes."""
import copy
import sys
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.erp_api import ErpUnavailable
from core.erp_source import LiveSource, RECEIPT_START
import core.erp_source as module
from test_erp_source import Client

ZONE = ZoneInfo('Asia/Shanghai')
STARTED = datetime(2026, 10, 14, 10, 0, 0, tzinfo=ZONE)


class Clock(datetime):
    current = STARTED

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


def receipt(qty=5, second=2, bill_id='501', *, details=None, kind='进货入库单'):
    lines = details or [{'dlyorder': '1', 'ktypeid': '20', 'ptypeid': '30',
                        'skuid': '31', 'usercode': 'LS01', 'pfullname': '螺栓', 'qty': qty}]
    return {'vchcode': bill_id, 'number': 'IN' + bill_id, 'vchtypename': kind,
            'date': '2026-10-14',
            'overtime': (STARTED + timedelta(seconds=second)).strftime('%Y-%m-%d %H:%M:%S'),
            'details': {'details': lines}}


class ChangingClient(Client):
    def __init__(self, scenario):
        super().__init__()
        self.scenario = scenario
        self.stock_reads = 0
        self.tail_reads = 0
        self.full_reads = 0
        self.stock_qty = 10
        self.stock_saleable = 5

    def call(self, method, params):
        if method == 'erp.goodsstocks.list':
            self.stock_reads += 1
            if self.stock_reads > 1 and self.scenario == 'stock_failure':
                raise ErpUnavailable('test subset offline')
            if self.stock_reads > 1:
                assert params['ptypeids'] == [30]
                self.stock_qty = 15
                self.stock_saleable = 10
                Clock.current = STARTED + timedelta(seconds=10)
            else:
                Clock.current = STARTED + timedelta(seconds=5)
                if self.scenario == 'stock_first':
                    self.stock_qty = 15
                    self.stock_saleable = 10
            return super().call(method, params)
        if method == 'erp.bill.billcenterlist':
            self.calls.append((method, copy.deepcopy(params)))
            is_tail = params['overbegin'] != RECEIPT_START
            if is_tail:
                self.tail_reads += 1
                if self.scenario == 'tail_failure':
                    raise ErpUnavailable('test tail offline')
            else:
                self.full_reads += 1
            if self.scenario == 'none':
                bills = []
            elif self.scenario == 'late':
                bills = [receipt(second=6)]
                if not is_tail:
                    Clock.current = STARTED + timedelta(seconds=7)
            elif self.scenario == 'tail_backdate':
                bills = [receipt(second=-120 if is_tail else 2)]
            elif self.scenario == 'tail_missing':
                bills = [] if is_tail else [receipt()]
            elif self.scenario == 'continuous':
                bills = [receipt(qty=5 + self.tail_reads)]
            elif self.scenario == 'duplicate_identity':
                detail = receipt()['details']['details'][0]
                bills = [receipt(details=[detail, copy.deepcopy(detail)])] if is_tail else [receipt()]
            elif self.scenario == 'detail_removed':
                detail = receipt()['details']['details'][0]
                lines = [dict(detail, qty=6)]
                if not is_tail:
                    lines.append(dict(detail, dlyorder='2', qty=4))
                bills = [receipt(details=lines)]
            elif self.scenario == 'red_tail':
                bills = [receipt()]
                if is_tail:
                    bills.append(receipt(-5, bill_id='502'))
            elif self.scenario == 'return_duplicate':
                detail = receipt()['details']['details'][0]
                bills = [receipt(details=[dict(detail, qty=6), dict(detail, dlyorder='2', qty=4)]),
                         receipt(-2, bill_id='502', kind='进货退货单')]
            else:
                bills = [receipt()]
            return {'details': copy.deepcopy([bill for bill in bills
                if params['overbegin'] <= bill['overtime'] <= params['overend']])}
        return super().call(method, params)


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    Clock.current = STARTED
    monkeypatch.setattr(module, 'datetime', Clock)


def collect(scenario):
    client = ChangingClient(scenario)
    return client, LiveSource(client, warehouse_id='20').collect()


def stock(facts):
    return next(row for row in facts['inventory'] if row['product_id'] == '30')


def test_stable_source_only_adds_one_short_tail_and_no_full_stock_retry():
    client, facts = collect('none')
    assert (client.stock_reads, client.full_reads, client.tail_reads) == (1, 1, 1)
    assert facts['receipt_events_complete'] is True
    assert facts['receipt_covered_through'] == '2026-10-14T10:00:05+08:00'
    assert stock(facts)['stock_observed_at'] == '2026-10-14T10:00:05+08:00'
    tail = [params for method, params in client.calls
            if method == 'erp.bill.billcenterlist'][-1]
    assert tail['overbegin'] == '2026-10-14 09:59:59'


@pytest.mark.parametrize('scenario', ['stock_first', 'stock_old'])
def test_receipt_during_collect_is_included_and_its_stock_is_reread(scenario):
    client, facts = collect(scenario)
    assert (client.full_reads, client.tail_reads, client.stock_reads) == (1, 1, 2)
    assert stock(facts)['physical_qty'] == '15'
    assert facts['receipt_events'][0]['quantity'] == '5'
    assert facts['receipt_events'][0]['posted_at'] == '2026-10-14T10:00:02+08:00'
    assert stock(facts)['stock_observed_at'] == '2026-10-14T10:00:10+08:00'
    assert facts['receipt_covered_through'] == '2026-10-14T10:00:10+08:00'
    assert not facts['calculation_issues']


def test_receipt_after_full_cutoff_uses_tail_and_only_changed_product_reread():
    client, facts = collect('late')
    assert (client.full_reads, client.tail_reads, client.stock_reads) == (1, 2, 2)
    assert stock(facts)['physical_qty'] == '15'
    assert facts['receipt_events'][0]['quantity'] == '5'
    assert len(facts['records']['purchases']) == 1
    assert not facts['calculation_issues']


def test_continuously_changing_scope_is_blocked_with_bounded_calls():
    client, facts = collect('continuous')
    assert (client.full_reads, client.tail_reads, client.stock_reads) == (1, 2, 3)
    issues = [row for row in facts['calculation_issues']
              if row['code'] == 'supply_observation_unstable']
    assert [row['inventory_key'] for row in issues] == [['10', '20', '30', '31', '1']]
    assert facts['receipt_events_complete'] is True
    assert facts['receipt_events'][0]['quantity'] == '7'
    assert facts['source_counts']['supply_unstable_products'] == 1


def test_subset_stock_failure_does_not_replace_stock_with_zero_or_block_other_skus():
    client, facts = collect('stock_failure')
    assert stock(facts)['physical_qty'] == '10'
    issues = [row for row in facts['calculation_issues']
              if row['code'] == 'supply_observation_unstable']
    assert [row['inventory_key'] for row in issues] == [['10', '20', '30', '31', '1']]
    assert facts['receipt_events_complete'] is True
    assert client.stock_reads == 2


def test_unknown_tail_failure_explicitly_marks_receipts_incomplete():
    client, facts = collect('tail_failure')
    assert facts['query_complete'] is True
    assert facts['receipt_events_complete'] is False
    assert any(row['code'] == 'receipt_tail_unverified' and row['scope'] == 'global'
               for row in facts['calculation_issues'])
    assert len(facts['receipt_events']) == 1
    assert client.tail_reads == 1


def test_tail_replaces_duplicate_details_and_signed_returns_without_double_counting():
    client, facts = collect('return_duplicate')
    assert client.full_reads == client.tail_reads == 1
    assert len(facts['records']['purchases']) == 3
    assert {row['quantity'] for row in facts['receipt_events']} == {'10', '-2'}
    assert {row['type'] for row in facts['receipt_events']} == {'进货入库单', '进货退货单'}


def test_changed_bill_removes_old_detail_before_reaggregation():
    client, facts = collect('detail_removed')
    assert (client.full_reads, client.tail_reads, client.stock_reads) == (1, 2, 3)
    assert len(facts['records']['purchases']) == 1
    assert facts['receipt_events'][0]['quantity'] == '6'
    assert not facts['calculation_issues']


def test_red_receipt_in_tail_is_preserved_once_with_signed_quantity():
    client, facts = collect('red_tail')
    assert (client.full_reads, client.tail_reads, client.stock_reads) == (1, 2, 3)
    assert len(facts['records']['purchases']) == 2
    assert {row['quantity'] for row in facts['receipt_events']} == {'5', '-5'}
    assert {row['type'] for row in facts['receipt_events']} == {'进货入库单'}
    assert not facts['calculation_issues']


def test_duplicate_tail_line_identity_cannot_silently_overwrite_a_detail():
    client, facts = collect('duplicate_identity')
    assert facts['receipt_events_complete'] is False
    assert facts['receipt_events'][0]['quantity'] == '5'
    assert len(facts['records']['purchases']) == 1
    assert any(row['code'] == 'receipt_tail_unverified' for row in facts['calculation_issues'])


@pytest.mark.parametrize('scenario', ['tail_backdate', 'tail_missing'])
def test_bill_missing_from_short_tail_is_preserved_and_only_its_scope_is_blocked(scenario):
    client, facts = collect(scenario)
    assert (client.full_reads, client.tail_reads, client.stock_reads) == (1, 2, 2)
    assert facts['receipt_events_complete'] is True
    assert len(facts['records']['purchases']) == 1
    assert facts['receipt_events'][0]['quantity'] == '5'
    assert facts['receipt_events'][0]['posted_at'] == '2026-10-14T10:00:02+08:00'
    issues = [row for row in facts['calculation_issues']
              if row['code'] == 'supply_observation_unstable']
    assert [row['inventory_key'] for row in issues] == [['10', '20', '30', '31', '1']]


def test_missing_tail_bill_blocks_all_skus_of_its_product():
    client = ChangingClient('tail_backdate')
    original = client.call

    def call(method, params):
        result = original(method, params)
        if method == 'erp.ptype.list':
            result['ptypes'][0]['skus'].append({'skuid': '35', 'usercode': 'LS01-V2'})
        return result

    client.call = call
    facts = LiveSource(client, warehouse_id='20').collect()
    issues = [row for row in facts['calculation_issues']
              if row['code'] == 'supply_observation_unstable']
    assert {tuple(row['inventory_key']) for row in issues} == {
        ('10', '20', '30', '31', '1'), ('10', '20', '30', '35', '1')}
    assert len(facts['records']['purchases']) == 1
    assert facts['receipt_events'][0]['quantity'] == '5'
