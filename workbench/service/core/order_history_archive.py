"""Isolated, read-only Qimen sales-history collector with durable local archive.

Qimen ``bill_status=6,7`` returns shipped and posted documents. This module
never infers a count from the customer-PO suffix or from a product code. A
source order line is identified only by profile, *complete* trade ID and the
source line's ``recid``/``oid``. If one line is observed after several partial
shipments, it remains one archived line; changed observations are retained as
versions, not counted as additional ordered lines.

Fetch and full replay happen before a single SQLite write transaction. Failed
pages, unstable pagination or ambiguous line identities leave the last good
archive and coverage rows untouched. This feature owns its additive schema;
it does not alter Store.user_version or the active-order source.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo


QIMEN = 'qimen.taobao.erp.order.sync'
SOURCE = 'qimen'
SHIPPED_STATUSES = '6,7'
QUERY_TIME_TYPE = 1  # Official Qimen meaning: shipment time.
SHANGHAI = ZoneInfo('Asia/Shanghai')


class HistoryIncomplete(ValueError):
    """A source window cannot be published as complete."""


def _text(value):
    return '' if value is None else str(value).strip()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), default=str)


def _hash(value):
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _collection(value, name):
    if isinstance(value, dict):
        value = value.get(name)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HistoryIncomplete('invalid_' + name + '_collection')
    return value


def _extension(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            return {}
    return {}


def _local_datetime(value, label):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(label + '_must_be_timezone_aware')
    return value.astimezone(SHANGHAI).replace(tzinfo=None)


def _query_stamp(value):
    return value.strftime('%Y-%m-%d %H:%M:%S')


def _quantity(value):
    if value is None or value == '':
        return ''
    if isinstance(value, bool):
        raise HistoryIncomplete('invalid_reported_quantity')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HistoryIncomplete('invalid_reported_quantity') from None
    if not number.is_finite():
        raise HistoryIncomplete('invalid_reported_quantity')
    return format(number.normalize(), 'f')


def _source_line_id(line):
    for field in ('recid', 'oid'):
        candidate = _text(line.get(field))
        if candidate and not re.fullmatch(r'0+(?:\.0+)?', candidate):
            return candidate
    raise HistoryIncomplete('stable_source_line_id_missing')


def _document(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('trade_order'), dict):
        raise HistoryIncomplete('order_header_missing')
    head = raw['trade_order']
    number = _text(head.get('trade_id'))
    if not number:
        raise HistoryIncomplete('complete_trade_id_missing')
    state = _text(_extension(head.get('extendProps')).get('process_state'))
    if state not in {'500', '600'}:
        raise HistoryIncomplete('unexpected_shipped_process_state')
    lines = _collection(raw.get('order_info'), 'order_info')
    if not lines:
        raise HistoryIncomplete('order_lines_missing')
    projected_lines = {}
    for line in lines:
        line_id = _source_line_id(line)
        projected = {
            'source_line_id': line_id,
            'product_code': _text(line.get('spec_code') or line.get('goods_code')),
            'product_name': _text(line.get('goods_name')),
            'unit_name': _text(line.get('goods_unit')),
            'reported_quantity': _quantity(line.get('sell_count')),
        }
        prior = projected_lines.get(line_id)
        if prior is not None and prior != projected:
            raise HistoryIncomplete('same_line_conflicting_inside_document')
        projected_lines[line_id] = projected
    document = {
        'order_no': number,
        'process_state': state,
        'trade_status': _text(head.get('trade_status')),
        'order_time': _text(head.get('trade_time') or head.get('pay_time')),
        # Qimen history alone does not establish the customer/company name.
        'customer_name': '',
        'customer_source': '',
        'source_hash': _hash(raw),
        'lines': [projected_lines[key] for key in sorted(projected_lines)],
    }
    return document


def _window_id(profile, mode, start, end):
    return _hash([profile, SOURCE, SHIPPED_STATUSES, QUERY_TIME_TYPE,
                  mode, start, end])[:32]


def _whole_month(start, end):
    first = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
    last = datetime.strptime(end, '%Y-%m-%d %H:%M:%S')
    if first.day != 1 or first.time() != datetime.min.time():
        return False
    following = (first.replace(year=first.year + 1, month=1) if first.month == 12
                 else first.replace(month=first.month + 1))
    return last == following - timedelta(seconds=1)


def _month_start(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value):
        raise ValueError('invalid_month')
    return datetime.strptime(value + '-01', '%Y-%m-%d')


def _next_month(first):
    return (first.replace(year=first.year + 1, month=1) if first.month == 12
            else first.replace(month=first.month + 1))


def ensure_schema(store):
    """Install additive tables without touching the service-wide migration."""
    with store.connect(write=True) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS order_history_orders(
                profile_id TEXT NOT NULL,
                source TEXT NOT NULL,
                order_no TEXT NOT NULL,
                process_state TEXT NOT NULL,
                trade_status TEXT NOT NULL DEFAULT '',
                order_time TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL DEFAULT '',
                customer_source TEXT NOT NULL DEFAULT '',
                source_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,source,order_no)
            );
            CREATE TABLE IF NOT EXISTS order_history_lines(
                profile_id TEXT NOT NULL,
                source TEXT NOT NULL,
                order_no TEXT NOT NULL,
                source_line_id TEXT NOT NULL,
                product_code TEXT NOT NULL DEFAULT '',
                product_name TEXT NOT NULL DEFAULT '',
                unit_name TEXT NOT NULL DEFAULT '',
                reported_quantity TEXT NOT NULL DEFAULT '',
                quantity_basis TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,source,order_no,source_line_id)
            );
            CREATE INDEX IF NOT EXISTS order_history_line_product
                ON order_history_lines(profile_id,product_code,order_no);
            CREATE TABLE IF NOT EXISTS order_history_line_observations(
                profile_id TEXT NOT NULL,
                source TEXT NOT NULL,
                order_no TEXT NOT NULL,
                source_line_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                projection TEXT NOT NULL,
                first_observed_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,source,order_no,source_line_id,content_hash)
            );
            CREATE TABLE IF NOT EXISTS order_history_coverage(
                window_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                query_time_type INTEGER NOT NULL,
                bill_status TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                raw_count INTEGER NOT NULL,
                unique_orders INTEGER NOT NULL,
                unique_lines INTEGER NOT NULL,
                page_count INTEGER NOT NULL,
                digest TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                run_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS order_history_coverage_profile
                ON order_history_coverage(profile_id,window_start,window_end);
            CREATE TABLE IF NOT EXISTS order_history_window_members(
                window_id TEXT NOT NULL,
                order_no TEXT NOT NULL,
                PRIMARY KEY(window_id,order_no)
            );
            CREATE TABLE IF NOT EXISTS order_history_window_line_members(
                window_id TEXT NOT NULL,
                order_no TEXT NOT NULL,
                source_line_id TEXT NOT NULL,
                PRIMARY KEY(window_id,order_no,source_line_id)
            );
        ''')


class OrderHistoryArchive:
    def __init__(self, store, *, page_size=100, page_limit=2000):
        if not 1 <= page_size <= 500 or page_limit < 1:
            raise ValueError('invalid_page_limits')
        self.store = store
        self.page_size = page_size
        self.page_limit = page_limit
        ensure_schema(store)

    def _fetch_once(self, client, profile, start, end):
        raw_count = 0
        expected = None
        pages = []
        unique = {}
        for page_no in range(1, self.page_limit + 1):
            response = client.call(QIMEN, {
                'pageNo': page_no,
                'pageSize': self.page_size,
                'extendProps': {
                    'profile_id': int(profile),
                    'query_time_type': QUERY_TIME_TYPE,
                    'start_time': start,
                    'end_time': end,
                    'bill_status': SHIPPED_STATUSES,
                },
            })
            if not isinstance(response, dict) or str(response.get('code')) != '0':
                raise HistoryIncomplete('qimen_read_not_explicitly_successful')
            items = _collection(response.get('trade_orders'), 'trade_orders')
            total = response.get('TotalResults', response.get('totalResults'))
            if isinstance(total, bool) or not str(total).isdigit():
                raise HistoryIncomplete('page_total_missing')
            total = int(total)
            if expected is not None and total != expected:
                raise HistoryIncomplete('page_total_changed')
            expected = total
            pages.append(_hash(items))
            for raw in items:
                # Validate source-line identity on the first sight of an order.
                # Historical Qimen rows can have no recid/oid at all; a stable
                # page count cannot make those rows safe to count as order
                # lines. Fail before fetching the rest of a large month or
                # replaying it, while the archive transaction is still untouched.
                number = _document(raw)['order_no']
                previous = unique.get(number)
                if previous is not None and _canonical(previous) != _canonical(raw):
                    raise HistoryIncomplete('same_order_conflicting_across_pages')
                unique[number] = raw
            raw_count += len(items)
            if raw_count == expected:
                return unique, {'raw_count': raw_count, 'page_count': len(pages),
                                'page_digests': pages}
            if raw_count > expected or len(items) != self.page_size:
                raise HistoryIncomplete('pagination_not_closed')
        raise HistoryIncomplete('page_limit_exceeded')

    def _stable_window(self, client, profile, start, end):
        first, evidence = self._fetch_once(client, profile, start, end)
        second, repeated = self._fetch_once(client, profile, start, end)
        if evidence != repeated or _hash(first) != _hash(second):
            raise HistoryIncomplete('pagination_changed_during_replay')
        documents = [_document(first[number]) for number in sorted(first)]
        return documents, evidence

    def collect_window(self, client, profile, start, end, *, mode='incremental',
                       observed_at=None):
        """Fetch twice, then atomically upsert one complete time window.

        ``start``/``end`` are local Shanghai wall-clock timestamps formatted
        YYYY-MM-DD HH:MM:SS. Both are inclusive in the vendor request. The
        caller chooses the window; no old data is removed when a re-scan omits
        an order. Coverage membership is replaced for that window only.
        """
        profile = _text(profile)
        if not re.fullmatch(r'[0-9]+', profile) or mode not in {'incremental', 'month'}:
            raise ValueError('invalid_profile_or_mode')
        for value in (start, end):
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value):
                raise ValueError('invalid_window_timestamp')
            datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        if start > end:
            raise ValueError('window_start_after_end')
        if mode == 'month' and not _whole_month(start, end):
            raise ValueError('month_window_must_be_complete')
        observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec='seconds')
        documents, evidence = self._stable_window(client, profile, start, end)
        window_id = _window_id(profile, mode, start, end)
        run_id = _hash([window_id, observed_at, evidence['page_digests']])[:32]
        line_count = sum(len(doc['lines']) for doc in documents)
        digest = _hash(documents)
        with self.store.connect(write=True) as db:
            for doc in documents:
                db.execute('''INSERT INTO order_history_orders(
                    profile_id,source,order_no,process_state,trade_status,order_time,
                    customer_name,customer_source,source_hash,first_seen_at,last_seen_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(profile_id,source,order_no) DO UPDATE SET
                    process_state=excluded.process_state,
                    trade_status=excluded.trade_status,
                    order_time=excluded.order_time,
                    source_hash=excluded.source_hash,
                    last_seen_at=excluded.last_seen_at''',
                    (profile, SOURCE, doc['order_no'], doc['process_state'],
                     doc['trade_status'], doc['order_time'], '', '', doc['source_hash'],
                     observed_at, observed_at))
                for line in doc['lines']:
                    line_hash = _hash(line)
                    db.execute('''INSERT INTO order_history_lines(
                        profile_id,source,order_no,source_line_id,product_code,
                        product_name,unit_name,reported_quantity,quantity_basis,
                        source_hash,first_seen_at,last_seen_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(profile_id,source,order_no,source_line_id) DO UPDATE SET
                        product_code=excluded.product_code,
                        product_name=excluded.product_name,
                        unit_name=excluded.unit_name,
                        reported_quantity=excluded.reported_quantity,
                        source_hash=excluded.source_hash,
                        last_seen_at=excluded.last_seen_at''',
                        (profile, SOURCE, doc['order_no'], line['source_line_id'],
                         line['product_code'], line['product_name'], line['unit_name'],
                         line['reported_quantity'], 'qimen_sell_count_history_unverified',
                         line_hash, observed_at, observed_at))
                    db.execute('''INSERT OR IGNORE INTO order_history_line_observations(
                        profile_id,source,order_no,source_line_id,content_hash,
                        projection,first_observed_at) VALUES (?,?,?,?,?,?,?)''',
                        (profile, SOURCE, doc['order_no'], line['source_line_id'],
                         line_hash, _canonical(line), observed_at))
            db.execute('DELETE FROM order_history_window_members WHERE window_id=?', (window_id,))
            db.executemany('''INSERT INTO order_history_window_members(window_id,order_no)
                VALUES (?,?)''', ((window_id, doc['order_no']) for doc in documents))
            db.execute('DELETE FROM order_history_window_line_members WHERE window_id=?',
                       (window_id,))
            db.executemany('''INSERT INTO order_history_window_line_members(
                window_id,order_no,source_line_id) VALUES (?,?,?)''',
                ((window_id, doc['order_no'], line['source_line_id'])
                 for doc in documents for line in doc['lines']))
            db.execute('''INSERT INTO order_history_coverage(
                window_id,profile_id,mode,query_time_type,bill_status,window_start,
                window_end,raw_count,unique_orders,unique_lines,page_count,digest,
                completed_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(window_id) DO UPDATE SET
                mode=excluded.mode,raw_count=excluded.raw_count,
                unique_orders=excluded.unique_orders,unique_lines=excluded.unique_lines,
                page_count=excluded.page_count,digest=excluded.digest,
                completed_at=excluded.completed_at,run_id=excluded.run_id''',
                (window_id, profile, mode, QUERY_TIME_TYPE, SHIPPED_STATUSES,
                 start, end, evidence['raw_count'], len(documents), line_count,
                 evidence['page_count'], digest, observed_at, run_id))
        return {'profile_id': profile, 'window_id': window_id, 'mode': mode,
                'window_start': start, 'window_end': end,
                'raw_count': evidence['raw_count'], 'unique_orders': len(documents),
                'unique_lines_in_window': line_count, 'page_count': evidence['page_count'],
                'coverage_status': 'complete_for_window', 'digest': digest,
                'completed_at': observed_at}

    def backfill_month(self, client, profile, month, *, now=None):
        """Backfill one fully closed Shanghai calendar month."""
        first = _month_start(month)
        next_month = _next_month(first)
        current = _local_datetime(now or datetime.now(timezone.utc), 'now')
        if next_month > current:
            raise ValueError('month_not_closed_use_incremental')
        return self.collect_window(client, profile, _query_stamp(first),
                                   _query_stamp(next_month - timedelta(seconds=1)),
                                   mode='month')

    def backfill_missing_months(self, client, profile, first_month, *, now=None,
                                max_months=1):
        """Fill missing closed months oldest-first, at most ``max_months`` per call.

        Each month is independently committed only after full stable pagination.
        A later failure leaves earlier completed months durable and the failed
        month missing for the next call.
        """
        first = _month_start(first_month)
        if not re.fullmatch(r'[0-9]+', _text(profile)):
            raise ValueError('invalid_profile_or_mode')
        if isinstance(max_months, bool) or not isinstance(max_months, int) or max_months < 1:
            raise ValueError('invalid_max_months')
        current = _local_datetime(now or datetime.now(timezone.utc), 'now')
        if first > current:
            raise ValueError('first_month_in_future')
        with self.store.connect() as db:
            completed = {row[0][:7] for row in db.execute('''
                SELECT window_start FROM order_history_coverage
                WHERE profile_id=? AND mode='month' ''', (_text(profile),))}
        missing = []
        month = first
        while _next_month(month) <= current:
            key = month.strftime('%Y-%m')
            if key not in completed:
                missing.append(key)
            month = _next_month(month)
        results = []
        for key in missing[:max_months]:
            results.append(self.backfill_month(client, profile, key, now=now))
        return {'profile_id': _text(profile), 'first_month': first_month,
                'completed_this_run': results,
                'remaining_missing_months': missing[max_months:]}

    def sync_incremental(self, client, profile, *, now=None, initial_start=None,
                         overlap_hours=72):
        """Use last complete incremental end, with overlap; require a first start."""
        if not 1 <= overlap_hours <= 24 * 30:
            raise ValueError('invalid_overlap_hours')
        current = _local_datetime(now or datetime.now(timezone.utc), 'now')
        with self.store.connect() as db:
            row = db.execute('''SELECT MAX(window_end) FROM order_history_coverage
                WHERE profile_id=? AND mode='incremental' ''', (_text(profile),)).fetchone()
        previous_end = row[0] if row else None
        if previous_end:
            start = datetime.strptime(previous_end, '%Y-%m-%d %H:%M:%S') - timedelta(hours=overlap_hours)
        elif initial_start is not None:
            start = _local_datetime(initial_start, 'initial_start')
        else:
            raise ValueError('initial_start_required')
        if start >= current:
            raise ValueError('incremental_window_not_positive')
        return self.collect_window(client, profile, _query_stamp(start),
                                   _query_stamp(current), mode='incremental')

    def coverage(self, profile, *, year=None):
        """Return completed month windows and gaps; never claim lifetime total."""
        profile = _text(profile)
        if not profile:
            raise ValueError('profile_required')
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute('''SELECT * FROM order_history_coverage
                WHERE profile_id=? ORDER BY window_start,window_end''', (profile,))]
            line_count = db.execute('''SELECT COUNT(*) FROM order_history_lines
                WHERE profile_id=? AND source=? AND EXISTS (
                    SELECT 1 FROM order_history_window_line_members m
                    JOIN order_history_coverage c ON c.window_id=m.window_id
                    WHERE c.profile_id=order_history_lines.profile_id
                      AND m.order_no=order_history_lines.order_no
                      AND m.source_line_id=order_history_lines.source_line_id
                )''', (profile, SOURCE)).fetchone()[0]
            order_count = db.execute('''SELECT COUNT(*) FROM order_history_orders
                WHERE profile_id=? AND source=? AND EXISTS (
                    SELECT 1 FROM order_history_window_members m
                    JOIN order_history_coverage c ON c.window_id=m.window_id
                    WHERE c.profile_id=order_history_orders.profile_id
                      AND m.order_no=order_history_orders.order_no
                )''', (profile, SOURCE)).fetchone()[0]
            retained_orders = db.execute('''SELECT COUNT(*) FROM order_history_orders
                WHERE profile_id=? AND source=?''', (profile, SOURCE)).fetchone()[0]
            retained_lines = db.execute('''SELECT COUNT(*) FROM order_history_lines
                WHERE profile_id=? AND source=?''', (profile, SOURCE)).fetchone()[0]
        result = {'profile_id': profile, 'archived_orders': order_count,
                  'archived_lines': line_count, 'windows': rows,
                  'retained_orders_for_audit': retained_orders,
                  'retained_lines_for_audit': retained_lines,
                  'lifetime_coverage_complete': False,
                  'review_required': True,
                  'review_reasons': ['customer_identity_unverified',
                                     'reported_quantity_basis_unverified',
                                     'lifetime_coverage_incomplete']}
        if year is not None:
            if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
                raise ValueError('invalid_year')
            completed = {row['window_start'][:7] for row in rows if row['mode'] == 'month'
                         and row['window_start'].startswith(str(year) + '-')}
            months = [f'{year}-{month:02d}' for month in range(1, 13)]
            result['completed_months'] = sorted(completed)
            result['missing_months'] = [month for month in months if month not in completed]
        return result

    def query_lines(self, profile, *, product_code=None, order_no=None,
                    limit=50, offset=0):
        """Search archived source lines without treating shipment observations as lines."""
        profile = _text(profile)
        if not re.fullmatch(r'[0-9]+', profile):
            raise ValueError('profile_id_must_be_numeric')
        if isinstance(limit, bool) or isinstance(offset, bool):
            raise ValueError('invalid_history_pagination')
        try:
            limit, offset = int(limit), int(offset)
        except (ValueError, TypeError):
            raise ValueError('invalid_history_pagination') from None
        if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
            raise ValueError('invalid_history_pagination')
        if product_code is not None and (not isinstance(product_code, str) or
                                         not 1 <= len(product_code.strip()) <= 128):
            raise ValueError('invalid_product_code')
        if order_no is not None and (not isinstance(order_no, str) or
                                     not 1 <= len(order_no.strip()) <= 128):
            raise ValueError('invalid_order_no')
        clauses = ['l.profile_id=?', 'l.source=?', '''EXISTS (
            SELECT 1 FROM order_history_window_line_members m
            JOIN order_history_coverage c ON c.window_id=m.window_id
            WHERE c.profile_id=l.profile_id AND m.order_no=l.order_no
              AND m.source_line_id=l.source_line_id)''']
        params = [profile, SOURCE]
        if product_code is not None:
            clauses.append('l.product_code=?')
            params.append(product_code.strip())
        if order_no is not None:
            clauses.append('l.order_no=?')
            params.append(order_no.strip())
        where = ' AND '.join(clauses)
        with self.store.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM order_history_lines l WHERE ' + where,
                               params).fetchone()[0]
            rows = [dict(row) for row in db.execute('''
                SELECT l.profile_id,l.source,l.order_no,l.source_line_id,
                       l.product_code,l.product_name,l.unit_name,
                       l.reported_quantity,l.quantity_basis,l.first_seen_at,
                       l.last_seen_at,o.process_state,o.trade_status,o.order_time,
                       o.customer_name,o.customer_source
                FROM order_history_lines l JOIN order_history_orders o
                ON o.profile_id=l.profile_id AND o.source=l.source
                   AND o.order_no=l.order_no
                WHERE ''' + where + '''
                ORDER BY o.order_time DESC,l.order_no DESC,l.source_line_id
                LIMIT ? OFFSET ?''', (*params, limit, offset))]
        return {'profile_id': profile, 'items': rows, 'total': total,
                'limit': limit, 'offset': offset,
                'coverage_status': 'partial_archive',
                'review_required': True,
                'review_reasons': ['customer_identity_unverified',
                                   'reported_quantity_basis_unverified',
                                   'lifetime_coverage_incomplete']}
