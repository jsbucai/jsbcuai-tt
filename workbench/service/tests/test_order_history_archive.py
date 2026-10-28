"""Offline contract tests for the isolated shipped-order history archive."""

import copy
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.order_history_archive import HistoryIncomplete, OrderHistoryArchive, QIMEN
from core.store import Store


def document(number, *, line_id='10', quantity='5', state=500):
    return {
        'trade_order': {
            'trade_id': number,
            'trade_status': '发货',
            'trade_time': '2026-09-10 11:00:00',
            'extendProps': {'process_state': state},
        },
        'order_info': {'order_info': [{
            'recid': line_id, 'spec_code': 'LS01', 'goods_name': '测试商品',
            'goods_unit': '个', 'sell_count': quantity,
        }]},
    }


class Client:
    """The fake returns a chosen complete dataset for each full query pass."""

    def __init__(self, rows, *, page_size=2, rounds=None, fail_call=None):
        self.rows = copy.deepcopy(rows)
        self.rounds = copy.deepcopy(rounds) if rounds is not None else None
        self.page_size = page_size
        self.fail_call = fail_call
        self.calls = []

    def call(self, method, params):
        assert method == QIMEN
        self.calls.append(copy.deepcopy(params))
        call_index = len(self.calls)
        if self.fail_call == call_index:
            return {'code': '500', 'message': 'source failure'}
        page = params['pageNo']
        assert params['pageSize'] == self.page_size
        assert params['extendProps']['bill_status'] == '6,7'
        assert params['extendProps']['query_time_type'] == 1
        per_pass = max(1, (len(self.rows) + self.page_size - 1) // self.page_size)
        round_index = (call_index - 1) // per_pass
        rows = (self.rounds[min(round_index, len(self.rounds) - 1)]
                if self.rounds is not None else self.rows)
        offset = (page - 1) * self.page_size
        return {'code': '0', 'TotalResults': len(rows),
                'trade_orders': {'trade_orders': copy.deepcopy(rows[offset:offset+self.page_size])}}


@pytest.fixture
def store():
    (ROOT / 'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / 'runtime', prefix='history-') as path:
        yield Store(path)


def counts(store):
    with store.connect() as db:
        return tuple(db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                     for name in ('order_history_orders', 'order_history_lines',
                                  'order_history_line_observations',
                                  'order_history_coverage',
                                  'order_history_window_members'))


def test_closed_month_full_pagination_replay_and_idempotence(store):
    archive = OrderHistoryArchive(store, page_size=2)
    rows = [document('PO#49'), document('PO#50'), document('PO#51')]
    client = Client(rows)
    result = archive.backfill_month(client, '79', '2026-09',
                                    now=datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert result['coverage_status'] == 'complete_for_window'
    assert result['raw_count'] == 3
    assert result['unique_lines_in_window'] == 3
    assert [p['pageNo'] for p in client.calls] == [1, 2, 1, 2]
    assert all(p['extendProps']['profile_id'] == 79 for p in client.calls)
    assert all(p['extendProps']['start_time'] == '2026-09-01 00:00:00' for p in client.calls)
    assert all(p['extendProps']['end_time'] == '2026-09-30 23:59:59' for p in client.calls)
    assert counts(store) == (3, 3, 3, 1, 3)

    replay = archive.backfill_month(Client(rows), '79', '2026-09',
                                    now=datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert replay['window_id'] == result['window_id']
    assert counts(store) == (3, 3, 3, 1, 3)
    status = archive.coverage('79', year=2026)
    assert status['archived_lines'] == 3
    assert status['completed_months'] == ['2026-09']
    assert status['lifetime_coverage_complete'] is False


def test_partial_shipments_count_one_source_line_and_keep_observations(store):
    archive = OrderHistoryArchive(store, page_size=2)
    first = document('QJ0804#49', quantity='5', state=500)
    second = document('QJ0804#49', quantity='2', state=600)
    archive.collect_window(Client([first]), '79',
                           '2026-09-10 00:00:00', '2026-09-10 23:59:59')
    archive.collect_window(Client([second]), '79',
                           '2026-09-11 00:00:00', '2026-09-11 23:59:59')
    assert counts(store) == (1, 1, 2, 2, 2)
    with store.connect() as db:
        order = db.execute('SELECT process_state,customer_name,customer_source '
                           'FROM order_history_orders').fetchone()
        line = db.execute('SELECT source_line_id,reported_quantity,quantity_basis '
                          'FROM order_history_lines').fetchone()
    assert tuple(order) == ('600', '', '')
    assert tuple(line) == ('10', '2', 'qimen_sell_count_history_unverified')


def test_failed_page_and_unstable_replay_leave_last_success_untouched(store):
    archive = OrderHistoryArchive(store, page_size=2)
    initial = document('A')
    archive.collect_window(Client([initial]), '1',
                           '2026-09-01 00:00:00', '2026-09-02 00:00:00')
    baseline = counts(store)

    rows = [document('B'), document('C'), document('D')]
    with pytest.raises(HistoryIncomplete, match='qimen_read_not_explicitly_successful'):
        archive.collect_window(Client(rows, fail_call=2), '1',
                               '2026-09-03 00:00:00', '2026-09-04 00:00:00')
    assert counts(store) == baseline

    with pytest.raises(HistoryIncomplete, match='pagination_changed_during_replay'):
        archive.collect_window(Client([document('B')],
                                      rounds=[[document('B')], [document('C')]]),
                               '1', '2026-09-03 00:00:00', '2026-09-04 00:00:00')
    assert counts(store) == baseline
    assert archive.coverage('1')['archived_orders'] == 1


def test_failed_same_window_rescan_keeps_coverage_and_successful_omission_keeps_archive(store):
    archive = OrderHistoryArchive(store, page_size=2)
    start, end = '2026-09-01 00:00:00', '2026-09-02 00:00:00'
    first = archive.collect_window(Client([document('A'), document('B')]),
                                   '1', start, end)
    baseline = counts(store)
    with pytest.raises(HistoryIncomplete, match='pagination_changed_during_replay'):
        archive.collect_window(Client([document('A')],
                                      rounds=[[document('A')], [document('C')]]),
                               '1', start, end)
    assert counts(store) == baseline
    with store.connect() as db:
        row = db.execute('SELECT digest,unique_orders FROM order_history_coverage '
                         'WHERE window_id=?', (first['window_id'],)).fetchone()
    assert tuple(row) == (first['digest'], 2)

    # A later complete scan may have a smaller window result. Keep historical
    # line identities, but replace that window's exact source membership.
    archive.collect_window(Client([document('A')]), '1', start, end)
    assert counts(store) == (2, 2, 2, 1, 1)
    assert archive.coverage('1')['archived_orders'] == 1
    assert [item['order_no'] for item in archive.query_lines('1')['items']] == ['A']


def test_successful_zero_row_rescan_hides_old_order_but_retains_audit_rows(store):
    archive = OrderHistoryArchive(store, page_size=2)
    start, end = '2026-09-01 00:00:00', '2026-09-30 23:59:59'
    archive.collect_window(Client([document('A')]), '1', start, end, mode='month')
    assert archive.coverage('1')['archived_orders'] == 1
    result = archive.collect_window(Client([]), '1', start, end, mode='month')
    assert result['raw_count'] == 0
    coverage = archive.coverage('1')
    assert coverage['archived_orders'] == 0
    assert coverage['archived_lines'] == 0
    assert coverage['retained_orders_for_audit'] == 1
    assert coverage['retained_lines_for_audit'] == 1
    assert archive.query_lines('1')['total'] == 0
    assert counts(store) == (1, 1, 1, 1, 0)


def test_removed_source_line_does_not_remain_counted_after_rescan(store):
    archive = OrderHistoryArchive(store, page_size=2)
    start, end = '2026-09-01 00:00:00', '2026-09-30 23:59:59'
    original = document('A')
    original['order_info']['order_info'].append({
        'recid': '11', 'spec_code': 'LS02', 'goods_name': '第二商品',
        'sell_count': '3'})
    archive.collect_window(Client([original]), '1', start, end, mode='month')
    archive.collect_window(Client([document('A')]), '1', start, end, mode='month')
    assert archive.coverage('1')['archived_lines'] == 1
    assert archive.coverage('1')['retained_lines_for_audit'] == 2
    assert [item['source_line_id'] for item in archive.query_lines('1')['items']] == ['10']


def test_missing_stable_line_id_rejects_window_without_archive_write(store):
    archive = OrderHistoryArchive(store)
    incomplete = document('A', line_id='')
    client = Client([incomplete], page_size=100)
    with pytest.raises(HistoryIncomplete, match='stable_source_line_id_missing'):
        archive.collect_window(client, '1',
                               '2026-09-01 00:00:00', '2026-09-02 00:00:00')
    # Identity failure is decisive. Do not download a second full replay.
    assert len(client.calls) == 1
    assert counts(store) == (0, 0, 0, 0, 0)


def test_missing_line_identity_on_first_page_stops_large_month_without_coverage(store):
    archive = OrderHistoryArchive(store, page_size=2)
    missing = document('A', line_id='0')
    client = Client([missing, document('B'), document('C')], page_size=2)
    with pytest.raises(HistoryIncomplete, match='stable_source_line_id_missing'):
        archive.backfill_month(client, '1', '2026-09',
                               now=datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert [call['pageNo'] for call in client.calls] == [1]
    assert counts(store) == (0, 0, 0, 0, 0)
    assert archive.coverage('1', year=2026)['completed_months'] == []


def test_valid_oid_is_used_when_recid_is_zero(store):
    archive = OrderHistoryArchive(store)
    row = document('A', line_id='0')
    row['order_info']['order_info'][0]['oid'] = 'source-line-42'
    result = archive.collect_window(Client([row], page_size=100), '1',
                                    '2026-09-01 00:00:00', '2026-09-02 00:00:00')
    assert result['unique_lines_in_window'] == 1
    assert archive.query_lines('1')['items'][0]['source_line_id'] == 'source-line-42'


def test_zero_row_month_is_complete_and_partial_month_cannot_be_marked_complete(store):
    archive = OrderHistoryArchive(store)
    result = archive.backfill_month(Client([], page_size=100), '1', '2026-09',
                                    now=datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert result['raw_count'] == 0
    assert counts(store) == (0, 0, 0, 1, 0)
    assert archive.coverage('1', year=2026)['completed_months'] == ['2026-09']
    with pytest.raises(ValueError, match='month_window_must_be_complete'):
        archive.collect_window(Client([], page_size=100), '1',
                               '2026-08-01 00:00:00', '2026-08-02 00:00:00',
                               mode='month')
    with pytest.raises(ValueError, match='month_not_closed'):
        archive.backfill_month(Client([], page_size=100), '1', '2026-10',
                               now=datetime(2026, 10, 6, tzinfo=timezone.utc))


def test_month_gap_filler_resumes_and_skips_completed_months(store):
    archive = OrderHistoryArchive(store)
    current = datetime(2026, 10, 6, tzinfo=timezone.utc)
    archive.backfill_month(Client([], page_size=100), '1', '2026-08', now=current)
    july = archive.backfill_missing_months(Client([], page_size=100), '1',
                                           '2026-07', now=current, max_months=1)
    assert [item['window_start'][:7] for item in july['completed_this_run']] == ['2026-07']
    assert july['remaining_missing_months'] == ['2026-09']
    september = archive.backfill_missing_months(Client([], page_size=100), '1',
                                                '2026-07', now=current)
    assert [item['window_start'][:7] for item in september['completed_this_run']] == ['2026-09']
    assert september['remaining_missing_months'] == []
    assert counts(store) == (0, 0, 0, 3, 0)


def test_incremental_watermark_overlap_only_advances_on_success(store):
    archive = OrderHistoryArchive(store)
    client = Client([document('A')], page_size=100)
    first_now = datetime(2026, 10, 5, 16, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='initial_start_required'):
        archive.sync_incremental(client, '1', now=first_now)
    first = archive.sync_incremental(client, '1', now=first_now,
                                     initial_start=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert first['window_start'] == '2026-10-01 08:00:00'
    assert first['window_end'] == '2026-10-06 00:00:00'
    second_now = datetime(2026, 10, 6, 16, tzinfo=timezone.utc)
    with pytest.raises(HistoryIncomplete):
        archive.sync_incremental(Client([document('A')], page_size=100, fail_call=1),
                                 '1', now=second_now, overlap_hours=72)
    assert counts(store) == (1, 1, 1, 1, 1)
    later = Client([document('A')], page_size=100)
    second = archive.sync_incremental(later, '1', now=second_now, overlap_hours=72)
    assert second['window_start'] == '2026-10-03 00:00:00'
    assert second['window_end'] == '2026-10-07 00:00:00'
    assert counts(store) == (1, 1, 1, 2, 2)


def test_conflicting_duplicate_trade_id_rejects_unstable_source(store):
    archive = OrderHistoryArchive(store, page_size=2)
    with pytest.raises(HistoryIncomplete, match='same_order_conflicting_across_pages'):
        archive.collect_window(Client([document('A', quantity='5'),
                                       document('B'),
                                       document('A', quantity='4')]),
                               '1', '2026-09-01 00:00:00', '2026-09-02 00:00:00')
    assert counts(store) == (0, 0, 0, 0, 0)
