"""Read-only ERP query snapshot with explicit business-data evidence.

Qimen supplies the unshipped document index and remaining unshipped quantity.
WMS details supply the order summary and a second quantity source when present.
The quantity meaning was confirmed by the business owner on 2026-09-29.
"""
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .api_source import SourceIncomplete, SourceChanged
from .source_retry import retry_source_read, stable_page
from .erp_api import ErpUnavailable
from .logistics import decimal, inventory_supply
from .receipt_identity import receipt_event_id
from .rules import note_delivery_date, parse_note, parse_summary, text
from .product_images import image_metadata
from .product_catalog import current_products
from .purchase_price_bounds import match_wms_lines, sale_price_fact
from .delivery_confirmations import resolve as confirmed_delivery
from .freight_reconciliation import observation as freight_observation

QIMEN = 'qimen.taobao.erp.order.sync'
START = '1900-01-01 00:00:00'
END = '2100-01-01 00:00:00'
BUSINESS_WAREHOUSE_ID = '29871293290508390'
RECEIPT_START = '2026-08-07 00:00:00'
INVENTORY_BATCH_SIZE = 500
RECEIPT_PAGE_SIZE = 500
SUPPLY_STABILITY_ROUNDS = 2


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def stable_id(*parts):
    return hashlib.sha256(canonical(parts).encode()).hexdigest()[:24]


def number(value):
    return format(decimal(value).normalize(), 'f')


def collection(value, name):
    if isinstance(value, dict):
        value = value.get(name)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceIncomplete('ERP 数据集合结构不完整：' + name)
    return value


def expanded(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            pass
    return {}


def api_day(value):
    value = text(value)
    for pattern in ('%Y/%m/%d %H:%M:%S', '%Y-%m-%d %H:%M:%S',
                    '%Y/%m/%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return ''


def source_time(value):
    """Normalize a real ERP observation time; never invent missing timestamps."""
    raw = text(value).replace('/', '-').replace(' ', 'T', 1)
    if not raw or 'T' not in raw:
        return ''
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        return parsed.astimezone(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    except ValueError:
        return ''


def receipt_events_from_rows(rows):
    groups = {}
    for row in rows:
        event_id = receipt_event_id(row['profile_id'], row['document_no'],
            row['document_type'], row['warehouse_id'], row['product_id'],
            row['sku_id'], row['base_unit'], row['document_date'])
        event = groups.setdefault(event_id, {
            'event_id': event_id, 'document_no': row['document_no'],
            'posted_date': row['document_date'], 'posted_at': '', 'type': row['document_type'],
            'supplier_id': row.get('party_id',''), 'supplier_name': row.get('party',''),
            **{key: row[key] for key in ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')},
            'quantity': Decimal(0),
        })
        event['quantity'] += decimal(row['quantity'])
        posted = source_time(row.get('posted_at'))
        if posted:
            event['posted_at'] = max(event['posted_at'], posted)
    return [{**event, 'quantity': number(event['quantity'])}
            for event in sorted(groups.values(), key=lambda item: item['event_id'])]


def variant_id(row):
    first, second = text(row.get('id')), text(row.get('skuid'))
    if first and second and first != second:
        raise SourceIncomplete('商品 SKU 身份字段冲突')
    return first or second


class LiveSource:
    def __init__(self, client, warehouse_id=BUSINESS_WAREHOUSE_ID, reference_cache=None,
                 wms_cache=None, inventory_ledger=None, order_lifecycle=None,
                 ignored_order_ids=None):
        self.client = client
        self.warehouse_id = text(warehouse_id)
        self.reference_cache = reference_cache
        self.wms_cache = wms_cache
        self.inventory_ledger = inventory_ledger
        self.order_lifecycle = order_lifecycle
        self.ignored_order_ids = frozenset(
            value for item in (ignored_order_ids or ()) if (value := text(item))
        )
        self.progress = lambda value: None

    def _references(self, profile, force=False):
        cached = self.reference_cache.get_entry('catalog-v1', profile) if self.reference_cache and not force else None
        if cached is not None:
            previous = collection(cached['payload'].get('products'), 'products')
            # Keep the complete validated catalog in the cache.  The ERP method
            # returns only changed products for timetype=1, so an incremental
            # page must never replace the full product list or image index.
            begin = (cached['fetched_at'] - timedelta(hours=72)).strftime('%Y-%m-%d %H:%M:%S')
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            changes = self._pages('erp.ptype.list', {
                'begintime':begin, 'endtime':now.strftime('%Y-%m-%d %H:%M:%S'),
                'timetype':1, 'isall':True}, 'ptypes',
                lambda row:text(row.get('ptypeid')), 'products_incremental', 200)
            products_by_id = {text(row.get('ptypeid')):row for row in previous}
            if len(products_by_id) != len(previous) or not all(products_by_id):
                raise SourceIncomplete('商品目录缓存身份不完整')
            for product in changes:
                key=text(product.get('ptypeid'))
                # Retain tombstones in the raw cache for historical identity /
                # code-allocation checks; they are never current inventory.
                products_by_id[key]=product
            products=list(products_by_id.values())
            warehouses=self._pages('erp.stock.list', {}, 'stocks',
                lambda row:text(row.get('id')), 'warehouses')
            self.reference_cache.put('catalog-v1', profile,
                {'warehouses':warehouses,'products':products},now=now)
            self.progress({'source':'products_merged','current':len(changes),'total':len(products)})
            return warehouses, current_products(products), True
        warehouses = self._pages('erp.stock.list', {}, 'stocks',
            lambda row: text(row.get('id')), 'warehouses')
        products = self._pages('erp.ptype.list', {'begintime': START, 'endtime': END,
            'timetype': 1, 'isall': True}, 'ptypes',
            lambda row: text(row.get('ptypeid')), 'products', 200)
        if self.reference_cache:
            self.reference_cache.put('catalog-v1', profile,
                {'warehouses': warehouses, 'products': products})
        # isall=True also returns deleted ERP records. Apply the same projection
        # on full and incremental reads, including old tombstones in the cache.
        return warehouses, current_products(products), False

    @retry_source_read
    def _pages(self, method, params, name, identity, source, size=100):
        rows, seen, expected = [], set(), None
        for page in range(1, 2001):
            result = self.client.call(method, dict(params, pageno=page, pagesize=size))
            items = collection(result.get(name), name)
            total = result.get('totalresults')
            if isinstance(total, bool) or not str(total).isdigit():
                raise SourceIncomplete(source + '缺少分页总数')
            if expected is not None and expected != int(total):
                raise SourceChanged(source + '分页期间数量变化，正在重新读取')
            expected = int(total)
            for row in items:
                key = identity(row)
                if not key:
                    raise SourceIncomplete(source + '存在缺失身份，未发布')
                if key in seen:
                    raise SourceChanged(source + '存在重复身份，重新读取后核对')
                seen.add(key)
                rows.append(row)
            self.progress({'source': source, 'current': len(rows), 'total': expected})
            if len(rows) == expected:
                return rows
            if len(rows) > expected or len(items) != size:
                raise SourceChanged(source + '分页数量未闭合，正在重新读取')
        raise SourceIncomplete(source + '超过分页上限，未发布')

    @retry_source_read
    def _orders(self, profile, bill_status='1,2,3,4,5', source='orders'):
        """Count raw pages; only byte-equivalent duplicate documents may coalesce."""
        seen, raw_count, expected, first_page = {}, 0, None, None
        for page in range(1, 2001):
            result = self.client.call(QIMEN, {
                'pageNo': page, 'pageSize': 100,
                'extendProps': {'profile_id': int(profile), 'query_time_type': 2,
                    'start_time': START, 'end_time': END, 'bill_status': bill_status}})
            if str(result.get('code')) != '0':
                raise SourceIncomplete('订单查询未返回明确成功')
            items = collection(result.get('trade_orders'), 'trade_orders')
            total = result.get('TotalResults', result.get('totalResults'))
            if isinstance(total, bool) or not str(total).isdigit():
                raise SourceIncomplete('订单查询缺少分页总数')
            if expected is not None and expected != int(total):
                raise SourceChanged('订单分页期间数量变化，正在重新读取')
            expected = int(total)
            if page == 1:
                first_page = stable_page(items)
            for row in items:
                head = row.get('trade_order')
                if not isinstance(head, dict) or not text(head.get('trade_id')):
                    raise SourceIncomplete('订单缺少稳定编号')
                key = text(head['trade_id'])
                if key in seen and canonical(seen[key]) != canonical(row):
                    raise SourceChanged('同一订单出现不同内容，重新读取后核对')
                seen[key] = row
            raw_count += len(items)
            self.progress({'source': source, 'current': raw_count, 'total': expected})
            if raw_count == expected:
                if bill_status == '6':
                    # This complete set controls whether FIFO shadow demand is
                    # created or released.  Re-read its first page and count to
                    # reject the common shifting-offset race before publishing.
                    check = self.client.call(QIMEN, {
                        'pageNo': 1, 'pageSize': 100,
                        'extendProps': {'profile_id': int(profile),
                            'query_time_type': 2, 'start_time': START,
                            'end_time': END, 'bill_status': bill_status}})
                    if str(check.get('code')) != '0':
                        raise SourceIncomplete('需结存订单稳定性复核未返回明确成功')
                    checked = collection(check.get('trade_orders'), 'trade_orders')
                    checked_total = check.get('TotalResults', check.get('totalResults'))
                    if (isinstance(checked_total, bool) or
                            not str(checked_total).isdigit() or
                            int(checked_total) != expected or
                            stable_page(checked) != first_page):
                        raise SourceChanged('需结存订单分页期间发生变化，正在重新读取')
                return sorted(seen.values(), key=lambda r: text(r['trade_order']['trade_id']))
            if raw_count > expected or len(items) != 100:
                raise SourceChanged('订单分页未闭合，正在重新读取')
        raise SourceIncomplete('订单超过分页上限，未发布')

    def _wms_details(self, profile, orders, started, force_full=False):
        """Read the complete WMS range, then join locally by trade ID.

        The vendor's exact-query ``numbers`` field means WMS document number,
        not the Qimen trade ID. Passing trade IDs returned only a subset in the
        live account, so it cannot establish complete summary coverage.
        """
        requested = {text(row['trade_order']['trade_id']) for row in orders}
        metadata = self.wms_cache.metadata(profile) if self.wms_cache else None
        # Expensive full-year reads are explicit reconciliation jobs. Ordinary
        # manual and automatic refreshes always use the durable watermark.
        full = force_full or metadata is None or metadata.get('full_year') != started.year
        if full:
            begin = f'{started.year}-01-01 00:00:00'
            timetype = 0
        else:
            try:
                last_sync = datetime.fromisoformat(metadata['last_sync'])
                if last_sync.tzinfo is None:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise SourceIncomplete('WMS 缓存水位无效，未发布快照') from None
            begin = (last_sync - timedelta(days=2)).strftime('%Y-%m-%d 00:00:00')
            timetype = 1
        end = started.strftime('%Y-%m-%d 23:59:59')
        try:
            rows = self._pages('erp.ewms.stockoutbill.list', {
                'begintime': begin, 'endtime': end, 'timetype': timetype,
            }, 'orders', lambda row: text(row.get('vchcode')), 'order_details', 200)
        except ErpUnavailable:
            return {}, [{'scope': 'global', 'code': 'summary_details_unavailable',
                'reason': '订单详情接口暂不可用；摘要和标签等待补齐'}], {
                    'wms_cache_mode': 'failed', 'wms_rows_read': 0, 'wms_cached_matches': 0}
        if self.wms_cache:
            try:
                self.wms_cache.commit(profile, requested, rows, started, full=full)
            except ValueError as error:
                raise SourceIncomplete(str(error)) from None
            matches = self.wms_cache.get(profile, requested)
        else:
            matches = {}
            for row in rows:
                trade_id = text(row.get('tradeid'))
                if trade_id not in requested:
                    continue
                if trade_id in matches:
                    raise SourceIncomplete('WMS 存在重复订单号，无法唯一确定摘要')
                matches[trade_id] = dict(row, _observed_at=started.isoformat(timespec='seconds'))
        missing = sorted(requested - set(matches))
        for offset in range(0, len(missing), 100):
            selected = set(missing[offset:offset + 100])
            try:
                response = self.client.call('erp.ewms.stockoutbill.query', {
                    'numbers': sorted(selected), 'status': 2})
            except ErpUnavailable:
                break
            for row in collection(response.get('orders'), 'orders'):
                trade_id = text(row.get('tradeid'))
                if trade_id in selected and trade_id not in matches:
                    matches[trade_id] = dict(row, _observed_at=started.isoformat(timespec='seconds'))
        issues = [{'scope': 'order', 'order_no': trade_id,
            'code': 'summary_detail_missing',
            'reason': 'WMS 未找到该未完成订单，摘要与明细待核对'}
            for trade_id in sorted(requested - set(matches))]
        return matches, issues, {'wms_cache_mode': 'full' if full else 'incremental',
            'wms_rows_read': len(rows), 'wms_cached_matches': len(matches)}

    def _order_dates(self, profile, orders, started):
        requested = {text(row['trade_order']['trade_id']): row for row in orders}
        if self.wms_cache is None:
            values = {tradeid: api_day(row['trade_order'].get('trade_time') or
                row['trade_order'].get('pay_time')) for tradeid, row in requested.items()}
            return values, [], {'order_dates_cached': 0, 'order_dates_probed': 0}
        values = self.wms_cache.order_dates(profile, requested)
        missing = set(requested) - set(values)
        # Only probe orders already proven to exist in the current-year WMS
        # cache. Very old Qimen rows can make the WMS endpoint reject the date
        # request; they must not prevent current orders from being resolved.
        cached_wms = self.wms_cache.get(profile, missing)
        candidates = {}
        for tradeid in missing:
            paid = api_day(requested[tradeid]['trade_order'].get('pay_time'))
            if not paid:
                continue
            paid_date = datetime.strptime(paid, '%Y-%m-%d').date()
            if tradeid not in cached_wms or paid_date.year != started.year:
                continue
            # WMS creation date matched the legacy order date in every audited
            # mismatch. Payment can lag order creation, so probe one week back.
            candidates[tradeid] = {paid_date - timedelta(days=offset) for offset in range(8)}
        hits = {tradeid:set() for tradeid in missing}
        unavailable = False
        for day in sorted({value for dates in candidates.values() for value in dates}):
            try:
                rows = self._pages('erp.ewms.stockoutbill.list', {
                    'begintime': day.isoformat() + ' 00:00:00',
                    'endtime': day.isoformat() + ' 23:59:59',
                    'timetype': 0,
                }, 'orders', lambda row: text(row.get('vchcode')), 'order_dates', 200)
            except ErpUnavailable:
                unavailable = True
                break
            for row in rows:
                tradeid = text(row.get('tradeid'))
                if tradeid in hits:
                    hits[tradeid].add(day.isoformat())
        resolved = {tradeid:next(iter(days)) for tradeid,days in hits.items() if len(days)==1}
        try:
            self.wms_cache.put_order_dates(profile, resolved, 'wms_creation_window', started)
        except ValueError as error:
            raise SourceIncomplete(str(error)) from None
        values.update(resolved)
        issues = []
        for tradeid in sorted(set(requested) - set(values)):
            reason = 'WMS 创建日期接口暂不可用' if unavailable else \
                'WMS 创建日期窗口无法唯一确定订货日期'
            issues.append({'scope':'order', 'order_no':tradeid,
                'code':'order_date_unverified', 'reason':reason})
        return values, issues, {'order_dates_cached': len(values) - len(resolved),
            'order_dates_probed': len(resolved)}

    @retry_source_read
    def _purchases(self, profile, started, product_map, warehouse_id, *, begin=RECEIPT_START):
        # Assembly documents contain both output and material lines. A page
        # that was safe for purchases alone can exceed the gateway's byte
        # limit. Restart from page one at a smaller size, never splice offsets
        # from different page sizes or lift the bounded response limit.
        size = RECEIPT_PAGE_SIZE
        while True:
            try:
                return self._purchase_history(profile, started, product_map,
                    warehouse_id, begin=begin, page_size=size)
            except ErpUnavailable as error:
                if error.category != 'response_too_large' or size == 1:
                    raise
                size = max(1, size // 2)
                self.progress({'source':'purchase_history_page_resize', 'current':0,
                    'total':0, 'page_size':size})

    def _purchase_history(self, profile, started, product_map, warehouse_id, *, begin, page_size):
        """Posted receipt/return history and App display rows.

        The bootstrap ledger starts on 2026-08-07. Re-reading from that stable
        boundary lets the durable ledger detect late red bills and corrections.
        API quantities are already signed for receipt and return documents.
        """
        params = {'overbegin': begin, 'overend': started.strftime('%Y-%m-%d %H:%M:%S'),
                  'vchtype': [34, 6, 143, 16], 'showredbill': True, 'hascustom': True,
                  'pagesize': page_size}
        rows, seen, first, event_groups, exchange_seen = [], set(), None, {}, False
        internal_voids, internal_reds = {}, {}
        for page in range(1, 2001):
            response = self.client.call('erp.bill.billcenterlist', dict(params, pageindex=page))
            bills = collection(response.get('details'), 'details')
            if page == 1:
                first = stable_page(bills)
            for bill in bills:
                bill_id = text(bill.get('vchcode'))
                if not bill_id:
                    raise SourceIncomplete('进货流水缺少身份，未发布')
                if bill_id in seen:
                    raise SourceChanged('进货流水分页重复，正在重新读取')
                seen.add(bill_id)
                document_no = text(bill.get('number'))
                document_type = text(bill.get('vchtypename'))
                document_date = text(bill.get('date'))[:10]
                if document_type == '进货换货单':
                    exchange_seen = True
                    continue
                internal = text(bill.get('vchtype')) == '16' or document_type in {'生产组装','生产组装单'}
                if internal:
                    document_type = '内部组装／拆卸'
                    if bill.get('redold'):
                        signature=sorted((text(x.get('ktypeid') or bill.get('ktypeid')),text(x.get('ptypeid')),text(x.get('skuid')),
                            str((-decimal(x['qty']) if bill.get('redword') else decimal(x['qty'])).normalize())) for x in collection(bill.get('details'),'details'))
                        target=internal_reds if bill.get('redword') else internal_voids
                        key=(document_no,document_date)
                        if key in target:raise SourceIncomplete('组装红冲原单关联不唯一')
                        target[key]=signature
                    if bill.get('redword'):
                        if not bill.get('redold'):raise SourceIncomplete('组装红冲缺少原单标记')
                        continue
                if document_type not in {'进货入库单', '进货退货单', '内部组装／拆卸'}:
                    raise SourceIncomplete('单据中心返回了未识别的进货单据类型')
                line_ids = set()
                for line in collection(bill.get('details'), 'details'):
                    line_warehouse = text(line.get('ktypeid') or bill.get('ktypeid'))
                    if line_warehouse != warehouse_id:
                        continue
                    line_id = text(line.get('dlyorder'))
                    if not line_id or line_id in line_ids:
                        raise SourceIncomplete('进货明细缺少稳定身份或明细身份重复')
                    line_ids.add(line_id)
                    product_id = text(line.get('ptypeid'))
                    sku_id = text(line.get('skuid'))
                    product = product_map.get(product_id, {})
                    if not product or not sku_id or not document_no or not document_date:
                        raise SourceIncomplete('进货流水缺少商品、SKU、单号或日期身份')
                    # Type 16 uses signed quantities and an explicit direction.
                    # Outgoing raw materials are never incoming supply. Red
                    # documents need the original line reference before use.
                    if internal:
                        qty=decimal(line['qty'])
                        direction=text(line.get('inout'))
                        if direction=='1' and qty<=0:continue
                        if direction!='0' or qty<0 or not source_time(bill.get('overtime')):
                            raise SourceIncomplete('组装拆卸产出方向或过账时间未验证')
                        if bill.get('redold'):line={**line,'qty':0}
                    event_id = receipt_event_id(profile, document_no, document_type,
                        line_warehouse, product_id, sku_id, '1', document_date)
                    event = event_groups.setdefault(event_id, {
                        'event_id': event_id, 'document_no': document_no,
                        'posted_date': document_date, 'posted_at': source_time(bill.get('overtime')), 'type': document_type,
                        'supplier_id': 'internal' if internal else text(bill.get('btypeid')),
                        'supplier_name': '内部组装／拆卸' if internal else text(bill.get('bfullname')),
                        'profile_id': profile, 'warehouse_id': line_warehouse,
                        'product_id': product_id, 'sku_id': sku_id, 'base_unit': '1',
                        'quantity': Decimal(0),
                    })
                    event['quantity'] += decimal(line['qty'])
                    event['posted_at'] = max(event['posted_at'], source_time(bill.get('overtime')))
                    rows.append({'id': stable_id('purchase', profile, bill_id, line_id),
                        'profile_id': profile, 'warehouse_id': line_warehouse,
                        'product_id': product_id, 'sku_id': sku_id, 'base_unit': '1',
                        'document_no': document_no, 'document_type': document_type,
                        'document_date': document_date, 'posted_at': source_time(bill.get('overtime')),
                        'source_bill_id': bill_id, 'source_line_id': line_id,
                        'sku': text(line.get('usercode')).upper(), 'product_name': text(line.get('pfullname')),
                        'spec': text(line.get('standard')), 'model': text(line.get('type')),
                        'quantity': number(line['qty']), 'amount': number(line.get('total', 0)),
                        'tax_total': number(line.get('total', 0)), 'party': '内部组装／拆卸' if internal else text(bill.get('bfullname')),
                        'party_id': 'internal' if internal else text(bill.get('btypeid')),
                        'warehouse': text(line.get('kfullname') or bill.get('kfullname')),
                        'note': text(bill.get('summary')), 'summary': text(bill.get('summary')),
                        'extra_note': text(bill.get('comment')), 'quantity_basis': 'posted_movement',
                        'redold': bill.get('redold'), 'redword': bill.get('redword'),
                        'unit_name': text(product.get('unit1'))})
            self.progress({'source': 'purchase_history', 'current': len(seen), 'total': 0})
            if len(bills) < page_size:
                # This endpoint has no count. Empty/short terminal page plus a
                # stable first page detects the common shifting-offset race.
                if page > 1:
                    check = self.client.call('erp.bill.billcenterlist', dict(params, pageindex=1))
                    if stable_page(collection(check.get('details'), 'details')) != first:
                        raise SourceChanged('进货流水分页期间发生变化，正在重新读取')
                if any(internal_voids.get(key)!=value for key,value in internal_reds.items()):
                    raise SourceIncomplete('组装红冲无法唯一对应原入库明细，保留上次正确数据')
                events = [{**event, 'quantity': number(event['quantity'])}
                          for event in sorted(event_groups.values(), key=lambda item:item['event_id'])]
                issues = []
                if exchange_seen:
                    issues.append({'scope':'global','code':'receipt_exchange_unverified',
                        'reason':'单据中心出现进货换货单，数量方向尚无真实样本验收'})
                return rows, events, begin, issues
        raise SourceIncomplete('进货流水超过分页上限，未发布')

    def _stock_batch(self, profile, selected, source='inventory_stability'):
        rows = self._pages('erp.goodsstocks.list', {
            'ptypeids': [int(value) for value in selected],
            'ktypeids': [int(self.warehouse_id)]}, 'goodsstocks',
            lambda row: tuple(text(row.get(key)) for key in ('profileid', 'ktypeid', 'ptypeid', 'skuid')),
            source, INVENTORY_BATCH_SIZE)
        if any(text(row.get('ptypeid')) not in selected or
               text(row.get('ktypeid')) != self.warehouse_id or
               text(row.get('profileid')) != profile for row in rows):
            raise SourceIncomplete('库存返回了请求范围以外的商品、仓库或账套')
        return rows

    def _stabilize_supply(self, profile, started, product_map, stocks, observed):
        """Bounded receipt-tail checks and stock rereads, never a full collect retry.

        A receipt newer than a stock observation must not be clipped against
        that old stock. Conversely stock newer than the receipt cutoff must
        not become permanent anonymous inventory. Tail reads cover the entire
        collection interval; any tail change triggers only its product scope.
        """
        cutoff = datetime.now(ZoneInfo('Asia/Shanghai'))
        rows, events, begin, issues = self._purchases(profile, cutoff, product_map, self.warehouse_id)
        receipt_issues = list(issues)
        tail_begin = (started - timedelta(seconds=1)).replace(microsecond=0)
        tail_iso = tail_begin.isoformat(timespec='seconds')
        pending = {row['product_id'] for row in rows
                   if source_time(row.get('posted_at')) >= tail_iso}
        blocked, checks, reread = set(), 0, set()
        for attempt in range(SUPPLY_STABILITY_ROUNDS):
            selected = sorted(pending - blocked)
            for offset in range(0, len(selected), INVENTORY_BATCH_SIZE):
                batch_ids = selected[offset:offset + INVENTORY_BATCH_SIZE]
                try:
                    replacement = self._stock_batch(profile, batch_ids)
                except (ErpUnavailable, SourceIncomplete):
                    blocked.update(batch_ids)
                    continue
                stocks = [row for row in stocks if text(row.get('ptypeid')) not in batch_ids] + replacement
                stamp = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
                observed.update({product: stamp for product in batch_ids})
                reread.update(batch_ids)
            tail_cutoff = datetime.now(ZoneInfo('Asia/Shanghai'))
            try:
                # Outer stability rounds already bound retries; do not multiply
                # them by the full-history paging retry decorator.
                tail, _, _, tail_issues = self._purchases.__wrapped__(
                    self, profile, tail_cutoff, product_map, self.warehouse_id,
                    begin=tail_begin.strftime('%Y-%m-%d %H:%M:%S'))
            except (ErpUnavailable, SourceIncomplete):
                receipt_issues.append({'scope': 'global', 'code': 'receipt_tail_unverified',
                    'reason': '采集期间进货变化未完成核验，保留原入库账本等待下次更新'})
                break
            checks += 1
            receipt_issues.extend(tail_issues)
            old_window = {row['id']: row for row in rows
                          if source_time(row.get('posted_at')) >= tail_iso}
            # Real ERP calls are time filtered. Keep legacy/mock rows before
            # the requested window out of the tail replacement as well.
            tail_window = {row['id']: row for row in tail
                           if not source_time(row.get('posted_at')) or
                           source_time(row.get('posted_at')) >= tail_iso}
            changed_ids = {item for item in set(old_window) | set(tail_window)
                           if old_window.get(item) != tail_window.get(item)}
            pending = {row['product_id'] for item in changed_ids
                       for row in (old_window.get(item), tail_window.get(item)) if row}
            missing_timestamp = {row['product_id'] for row in tail_window.values()
                                 if not source_time(row.get('posted_at'))}
            blocked.update(missing_timestamp)
            # A bill absent from a short time window is not proof of voiding:
            # ERP may have moved its posting timestamp outside that window.
            # Keep its last full-read rows and hold only its product scopes
            # until the next full history read establishes what happened.
            old_bills = {row['source_bill_id'] for row in old_window.values()}
            replace_bills = {row['source_bill_id'] for row in tail_window.values()}
            missing_bills = old_bills - replace_bills
            blocked.update(row['product_id'] for row in rows
                           if row['source_bill_id'] in missing_bills)
            # A bill that is actually returned is complete, so removed detail
            # rows can be replaced. Aggregate after replacement, never add.
            rows = [row for row in rows if row['source_bill_id'] not in replace_bills] + list(tail_window.values())
            events = receipt_events_from_rows(rows)
            cutoff = tail_cutoff
            if not pending:
                break
        else:
            blocked.update(pending)
        for product_id in sorted(blocked):
            for variant in product_map[product_id].get('skus') or []:
                sku_id = variant_id(variant)
                if sku_id:
                    issues.append({'scope': 'inventory', 'code': 'supply_observation_unstable',
                        'inventory_key': [profile, self.warehouse_id, product_id, sku_id, '1'],
                        'reason': '采集期间库存或进货仍在变化，该商品保留原分配等待下次核验'})
        issues.extend(receipt_issues)
        return stocks, rows, events, begin, cutoff, issues, not bool(receipt_issues), {
            'supply_tail_checks': checks, 'supply_stock_reread_products': len(reread),
            'supply_unstable_products': len(blocked)}

    def collect(self, progress=None, full_reconcile=False):
        self.progress = progress or (lambda value: None)
        started = datetime.now(ZoneInfo('Asia/Shanghai'))
        profile = self.client.account()
        warehouses, products, reference_cache_used = self._references(profile, force=full_reconcile)
        if not warehouses or any(text(row.get('profileid')) != profile for row in warehouses):
            raise SourceIncomplete('仓库账套与授权不一致')
        all_warehouse_map = {text(row['id']): row for row in warehouses}
        if self.warehouse_id not in all_warehouse_map:
            raise SourceIncomplete('指定业务仓库不存在，未发布快照')
        warehouse = all_warehouse_map[self.warehouse_id]
        warehouse_map = {self.warehouse_id: warehouse}
        product_map = {text(row['ptypeid']): row for row in products}
        product_images, invalid_product_images = image_metadata(
            profile, products, started.isoformat(timespec='seconds'))
        pictured_products = {item['product_id'] for item in product_images}
        code_map = {}
        for product in products:
            for variant in product.get('skus') or []:
                code = text(variant.get('usercode') or variant.get('xcode') or product.get('usercode')).upper()
                if code:
                    code_map.setdefault(code, []).append((product, variant))
        indexed_raw = self._orders(profile)
        indexed = [row for row in indexed_raw
                   if text(row['trade_order']['trade_id']) not in self.ignored_order_ids]
        ignored_order_ids = sorted(
            text(row['trade_order']['trade_id']) for row in indexed_raw
            if text(row['trade_order']['trade_id']) in self.ignored_order_ids
        )
        lifecycle_stage = None
        active_order_count = len(indexed)
        if self.order_lifecycle is not None:
            shipped_raw = self._orders(profile, bill_status='6',
                                       source='shipped_unposted_orders')
            indexed, lifecycle_stage = self.order_lifecycle.prepare(
                self.client, profile, indexed,
                started.isoformat(timespec='seconds'), shipped_rows=shipped_raw,
                excluded_order_ids=self.ignored_order_ids)
        lifecycle_shadow_ids = set(lifecycle_stage['shadow_order_ids']) \
            if lifecycle_stage is not None else set()
        # Refresh WMS first so newly visible Qimen orders can use the durable
        # WMS row both for summary details and for creation-date verification.
        wms, issues, wms_counts = self._wms_details(
            profile, indexed, started, force_full=full_reconcile)
        order_dates, date_issues, date_counts = self._order_dates(profile, indexed, started)
        if lifecycle_stage is not None:
            date_hints = dict(lifecycle_stage.get('order_date_hints', {}))
            lifecycle_stage['order_date_fallback_ids'] = []
            for lifecycle_order_no in lifecycle_shadow_ids:
                snapshot = lifecycle_stage['updates'].get(lifecycle_order_no, {}).get('business_snapshot', {})
                previous_order = snapshot.get('order') if isinstance(snapshot, dict) else None
                if isinstance(previous_order, dict) and text(previous_order.get('order_date')):
                    date_hints[lifecycle_order_no] = text(previous_order['order_date'])[:10]
            for lifecycle_order_no, value in date_hints.items():
                if lifecycle_order_no in lifecycle_shadow_ids and value:
                    order_dates[lifecycle_order_no] = value
            source_order_ids = {text(row['trade_order']['trade_id']) for row in indexed}
            lifecycle_stage['order_date_fallback_ids'] = sorted(
                (lifecycle_shadow_ids & source_order_ids) - set(order_dates))
            date_issues = [item for item in date_issues if not (
                item.get('order_no') in lifecycle_shadow_ids)]
            # A state-500 shadow only needs WMS if it has no durable prior
            # business snapshot.  Missing WMS summary must not stop its verified
            # quantity from occupying FIFO.
            issues = [item for item in issues if not (
                item.get('code') == 'summary_detail_missing' and
                item.get('order_no') in lifecycle_shadow_ids)]
        issues.extend(date_issues)
        stocks, stock_observed = [], {}
        product_ids = sorted(text(p['ptypeid']) for p in products if not p.get('isclass'))
        for offset in range(0, len(product_ids), INVENTORY_BATCH_SIZE):
            selected = product_ids[offset:offset + INVENTORY_BATCH_SIZE]
            batch = self._stock_batch(profile, selected, 'inventory')
            observed_at = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
            stock_observed.update({product: observed_at for product in selected})
            stocks.extend(batch)
            self.progress({'source': 'inventory_products',
                'current': min(offset + INVENTORY_BATCH_SIZE, len(product_ids)),
                'total': len(product_ids)})
        (stocks, purchase_rows, receipt_events, purchase_begin, receipt_cutoff,
         supply_issues, receipts_complete, supply_counts) = self._stabilize_supply(
            profile, started, product_map, stocks, stock_observed)
        issues.extend(supply_issues)
        stock_map = {}
        for row in stocks:
            key = (text(row.get('ptypeid')), text(row.get('skuid')))
            if key in stock_map:
                raise SourceIncomplete('业务仓库返回了重复商品/SKU库存')
            stock_map[key] = row
        standard_stock, inventory_rows = [], []
        catalog_keys = set()
        for product in sorted(products, key=lambda row: text(row.get('ptypeid'))):
            product_id = text(product.get('ptypeid'))
            variants = product.get('skus') or [None]
            for variant in sorted(variants, key=lambda row: variant_id(row) if row else ''):
                sku_id = variant_id(variant) if variant else ''
                key = (product_id, sku_id)
                if key in catalog_keys:
                    raise SourceIncomplete('商品主数据存在重复商品/SKU身份')
                catalog_keys.add(key)
                row = stock_map.get(key)
                physical = number(row['qty']) if row else '0'
                saleable = number(row['saleqty']) if row else '0'
                code = text((variant or {}).get('usercode') or (variant or {}).get('xcode') or
                            product.get('usercode')).upper()
                ident = dict(profile_id=profile, warehouse_id=self.warehouse_id,
                    product_id=product_id, sku_id=sku_id, base_unit='1')
                if sku_id:
                    standard_stock.append({**ident, 'physical_qty': physical, 'saleable_qty': saleable,
                        'stock_observed_at': stock_observed.get(product_id, '')})
                inventory_rows.append({**ident, 'id': stable_id('stock', ident), 'sku': code,
                    'product_name': text(product.get('fullname')), 'spec': text(product.get('standard')),
                    'model': text(product.get('type')), 'material': text(product.get('area')),
                    'location': text(product.get('comment')), 'standard_no': '',
                    'warehouse': text(warehouse.get('fullname')),
                    'stock_quantity': physical, 'available_stock': saleable,
                    'stock_observed_at': stock_observed.get(product_id, ''),
                    'unit_name': text(product.get('unit1')), 'product_note': '',
                    'has_image': product_id in pictured_products,
                    'is_deleted': str(product.get('deleted','')).strip().lower() in {'true','1'},
                    'is_stopped': str(product.get('isstop','')).strip().lower() in {'true','1'},
                    'status': '停用' if product.get('isstop') else '',
                    'print_blocked': not bool(code and product.get('fullname')),
                    'print_block_reason': '' if code and product.get('fullname') else '商品编号或商品名称缺失'})
        unmatched = sorted(set(stock_map) - catalog_keys)
        if unmatched:
            raise SourceIncomplete('库存商品/SKU无法关联商品主数据，未发布快照')
        inventory_ledger = self.inventory_ledger.prepare(
            self.client, profile, inventory_rows, started,
            force_full=full_reconcile, progress=self.progress,
        ) if self.inventory_ledger else None
        standard_orders, order_rows, sale_price_facts = [], [], []
        inventory_by_identity={tuple(text(row.get(key)) for key in ('profile_id','warehouse_id','product_id','sku_id','base_unit')):row for row in inventory_rows}
        for raw in indexed:
            head = raw['trade_order']; ext = expanded(head.get('extendProps'))
            order_no = text(head['trade_id']); details = wms.get(order_no, {})
            lifecycle_shadow = order_no in lifecycle_shadow_ids
            lines = collection(raw.get('order_info'), 'order_info')
            summary = text(details.get('summary'))
            # UL deliberately stores the customer/company name in the WMS
            # receiveraddress field.  Do not treat it as a delivery address and
            # do not fall back to receiver name/account guesses.
            customer_name = text(details.get('receiveraddress'))
            confirmation = confirmed_delivery(
                lifecycle_stage.get('delivery_confirmations', {}) if lifecycle_stage else {},
                order_no, order_dates.get(order_no, ''), details) or {}
            delivery_invalid = False
            try:
                delivery_date = note_delivery_date(summary)
            except ValueError:
                delivery_date = None
                delivery_invalid = True
                issues.append({'scope': 'order', 'order_no': order_no,
                    'code': 'delivery_date_invalid',
                    'reason': '订单摘要末尾交期格式无效，应为 #交期=YYYY-MM-DD'})
            if (lifecycle_shadow and not delivery_date and lifecycle_stage is not None):
                delivery_date = lifecycle_stage.get('delivery_date_hints', {}).get(order_no)
            if confirmation and not delivery_date and not delivery_invalid:
                delivery_date = confirmation['delivery_date']
            if not delivery_date and not delivery_invalid and not lifecycle_shadow:
                issues.append({'scope': 'order', 'order_no': order_no,
                    'code': 'delivery_date_missing',
                    'reason': '订单摘要末尾缺少 #交期=YYYY-MM-DD'})
            order = {'order_no': order_no, 'order_date': order_dates.get(order_no, ''),
                     **confirmation, 'delivery_date': delivery_date, 'lines': []}
            if lifecycle_shadow and not order['order_date']:
                # Historical status-500 rows can predate both WMS and the
                # lifecycle cache.  They must still reserve stock.  An early,
                # stable allocation-only date is conservative: it cannot hand
                # their already-shipped stock to a later active order.  The
                # uncertainty is exposed and holds writes for the shared scope.
                order['allocation_order_date'] = '1900-01-01'
            summary_info = parse_summary(summary)
            if confirmation:
                summary_info['delivery_date_status'] = 'known'
                summary_info['warnings'] = [w for w in summary_info['warnings'] if w != '交期待确认']
            warehouse_id = text(details.get('ktypeid') or ext.get('k_type_id') or head.get('warehouse_id'))
            if warehouse_id not in all_warehouse_map:
                by_name = [k for k, v in all_warehouse_map.items() if text(v.get('fullname')) == text(head.get('warehouse_name'))]
                warehouse_id = by_name[0] if len(by_name) == 1 else ''
            if not lines:
                raise SourceIncomplete('未发货订单缺少商品明细')
            line_ids = set()
            sorted_lines=sorted(lines,key=canonical)
            resolved_wms=match_wms_lines(sorted_lines,details)
            for item,matched_line in zip(sorted_lines,resolved_wms):
                line_ext = expanded(item.get('extendProps'))
                line_id = text(item.get('recid') or item.get('oid'))
                code = text(item.get('spec_code') or item.get('goods_code')).upper()
                matches = code_map.get(code, [])
                if len(matches) == 1:
                    product, variant = matches[0]
                    product_id, sku_id = text(product['ptypeid']), variant_id(variant)
                else:
                    product_id, sku_id = '', ''
                    product = {}; variant = {}
                reported = number(item['sell_count'])
                verified = bool(matched_line)
                if verified:
                    product_id, sku_id = text(matched_line.get('ptypeid')), text(matched_line.get('skuid'))
                    verified = product_id in product_map and any(variant_id(v) == sku_id
                        for v in product_map[product_id].get('skus') or [])
                unit_verified = verified and text(matched_line.get('unit')) == '1' and decimal(matched_line.get('unitrate')) == 1
                # Qimen's quantity has no reliable unit or per-line stock IDs.
                # Only an unambiguous WMS line establishes the stock relation.
                source_line_warehouse = text(matched_line.get('ktypeid') or details.get('ktypeid')) if verified else ''
                # When WMS detail is absent, a unique catalog code and the
                # resolved order-header warehouse still identify the affected
                # inventory component. The order remains blocked below because
                # quantity/unit evidence was not verified, but unrelated SKU
                # components can continue safely.
                scoped_from_header = len(matches) == 1 and warehouse_id in all_warehouse_map
                line_warehouse = source_line_warehouse or (warehouse_id if scoped_from_header else '')
                lifecycle_identity_verified = bool(lifecycle_shadow and len(matches) == 1 and
                                                   line_warehouse == self.warehouse_id)
                scoped_unit = '1' if unit_verified or (len(matches) == 1 and line_warehouse) else ''
                if verified and text(matched_line.get('id')) not in ('', '0'):
                    line_id = text(matched_line['id'])
                if line_id in ('', '0') or line_id in line_ids:
                    stem = stable_id('query-line', order_no, item)
                    line_id = stem
                    occurrence = 1
                    while line_id in line_ids:
                        occurrence += 1
                        line_id = stem + '-' + str(occurrence)
                line_ids.add(line_id)
                ident = dict(profile_id=profile, warehouse_id=line_warehouse,
                    product_id=product_id, sku_id=sku_id, base_unit=scoped_unit)
                if verified and source_line_warehouse and source_line_warehouse != self.warehouse_id:
                    issues.append({'scope': 'order', 'order_no': order_no,
                        'code': 'outside_inventory_warehouse',
                        'reason': '订单不属于08390业务仓库，不参与库存分配'})
                elif all(ident.values()) and not (verified and unit_verified) and not lifecycle_identity_verified:
                    issues.append({'scope': 'order', 'order_no': order_no,
                        'code': 'order_detail_unverified',
                        'reason': '订单明细未由WMS唯一核验；仅用于确定受影响库存范围'})
                elif not all(ident.values()):
                    issues.append({'scope': 'global', 'order_no': order_no,
                        'code': 'order_inventory_identity_unverified',
                        'reason': '订单与库存身份尚未唯一关联，本批物流暂停'})
                order['lines'].append({**ident, 'line_id': line_id, 'product_code': code,
                    'open_qty': reported, 'reported_qty': reported,
                    'delivery_date': delivery_date})
                label_identity_verified = bool(verified and unit_verified and all(ident.values()))
                product_info = product_map.get(product_id, product)
                order_rows.append({**ident, 'id': stable_id('order', profile, order_no, line_id),
                    'line_id': line_id, 'order_no': order_no, 'sku': code,
                    'product_name': text(item.get('goods_name') or product.get('fullname')),
                    'spec': text(product_info.get('standard')),
                    'model': text(product_info.get('type')),
                    'quantity': reported, 'quantity_basis': 'reported_sales_quantity',
                    'buyer': customer_name, 'customer_name': customer_name,
                    'customer_source': 'wms_receiveraddress', 'summary': summary,
                    'status': '来源未发货清单（状态待核对）', 'document_date': order['order_date'],
                    'source_shipping_id': text(details.get('freightbillno') or head.get('logistic_no')),
                    'source_status': text(head.get('trade_status')), 'warehouse': text(all_warehouse_map.get(warehouse_id, {}).get('fullname')),
                    'unit_name': text(item.get('goods_unit')), 'identity_complete': label_identity_verified,
                    'print_warnings': summary_info['warnings'],
                    'summary_person': summary_info['fields'].get('summaryPerson', ''),
                    'delivery_date': delivery_date,
                    **confirmation,
                    'delivery_date_status': summary_info['delivery_date_status'],
                    'print_blocked': not label_identity_verified,
                    'print_block_reason': '' if label_identity_verified else '订单商品身份或数量单位尚未唯一核验'})
                if verified and unit_verified and not lifecycle_shadow:
                    price_fact=sale_price_fact(order_rows[-1],matched_line,details,
                        inventory_by_identity.get(tuple(ident[key] for key in ('profile_id','warehouse_id','product_id','sku_id','base_unit')),{}),started.isoformat(timespec='seconds'))
                    if price_fact: sale_price_facts.append(price_fact)
            standard_orders.append(order)
        if lifecycle_stage is not None:
            try:
                standard_orders, order_rows, lifecycle_stage = self.order_lifecycle.finalize(
                    lifecycle_stage, standard_orders, order_rows)
            except ValueError as error:
                raise SourceIncomplete(str(error)) from None
        # The ERP saleable figure already reflects every open sales line and
        # may also contain other draft reservations.  Split its residual only
        # after active and lifecycle-shadow demand has been assembled under one
        # inventory identity: positive is pending supply; negative is inferred
        # other reservation that reduces allocatable physical stock.
        demand_by_identity = {}
        identity_fields = ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')
        for order in standard_orders:
            for line in order['lines']:
                identity = tuple(text(line.get(field)) for field in identity_fields)
                if all(identity):
                    demand_by_identity[identity] = (demand_by_identity.get(identity, Decimal(0)) +
                                                    decimal(line['open_qty']))
        for row in inventory_rows:
            identity = tuple(text(row.get(field)) for field in identity_fields)
            total_demand = demand_by_identity.get(identity, Decimal(0))
            balance = inventory_supply({
                'physical_qty': row['stock_quantity'],
                'saleable_qty': row['available_stock'],
            }, total_demand, allow_negative_physical=True)
            row.update(
                purchase_pending=number(balance['pending']),
                inferred_other_reserved=number(balance['inferred_other_reserved']),
                effective_physical=number(balance['effective_physical']),
            )
        # Date, supplier and remaining batch evidence cannot be replaced by the
        # stock-derived pending total. No invented empty "complete" batch list.
        unique_issues = {canonical(i): i for i in issues}
        return {'query_complete': True, 'rules_contract': 'v2', 'profile_id': profile,
            'source_updated_at': started.isoformat(timespec='seconds'),
            # Observations are synchronization evidence, not business fields:
            # changing only a read timestamp must not invalidate mobile lists
            # or frozen draft/print versions. Keep explicit empty ERP values.
            'logistics_observations': {n: freight_observation(row) for n, row in wms.items()
                if 'freightbillno' in row and n not in lifecycle_shadow_ids},
            'orders': standard_orders, 'inventory': standard_stock,
            'records': {'orders': order_rows, 'inventory': inventory_rows, 'purchases': purchase_rows},
            'wms_sale_price_facts': sale_price_facts,
            'calculation_issues': list(unique_issues.values()),
            'product_images': product_images,
            'receipt_events': receipt_events,
            'receipt_period': [purchase_begin[:10], started.date().isoformat()],
            'receipt_events_complete': receipts_complete,
            'receipt_covered_through': receipt_cutoff.isoformat(timespec='seconds'),
            'purchase_batches_complete': False,
            'query_scope': {'kind': 'unshipped_documents', 'start': START, 'end': END,
                'statuses': '1,2,3,4,5',
                'quantity_basis': 'remaining_unshipped_quantity_user_confirmed_2026_09_29',
                'ignored_order_ids': ignored_order_ids,
                'lifecycle': 'qimen_bill_status_6_with_exact_disappearance_check'
                    if lifecycle_stage is not None else 'disabled',
                'inventory_warehouse_id': self.warehouse_id,
                'inventory_warehouse': text(warehouse.get('fullname')),
                'purchase_history_start': purchase_begin, 'purchase_history_end': receipt_cutoff.isoformat()},
            'source_counts': {'products': len(products), 'warehouses': 1,
                'warehouses_discovered': len(warehouses), 'order_documents': active_order_count,
                'order_documents_raw': len(indexed_raw),
                'ignored_order_documents': len(ignored_order_ids),
                'inventory': len(inventory_rows), 'inventory_stock_rows': len(stocks),
                'receipt_events': len(receipt_events),
                **supply_counts,
                'product_images': len(product_images),
                'invalid_product_images': invalid_product_images,
                'reference_cache_used': reference_cache_used,
                'full_reconcile': bool(full_reconcile),
                **({'shipped_unposted_documents': lifecycle_stage['shipped_set_count'],
                    'lifecycle_shadow_orders': len(lifecycle_stage['shadow_order_ids']),
                    'lifecycle_order_date_fallbacks': len(
                        lifecycle_stage.get('order_date_fallback_ids', [])),
                    'lifecycle_exact_checks': len(lifecycle_stage['exact_checked_order_ids'])}
                   if lifecycle_stage is not None else {}),
                **wms_counts,
                **date_counts,
                'inventory_zero_rows': sum(decimal(row['stock_quantity']) == 0 and
                    decimal(row['available_stock']) == 0 for row in inventory_rows)},
            **({'inventory_ledger': inventory_ledger} if inventory_ledger is not None else {}),
            **({'order_lifecycle': lifecycle_stage,
                'receipt_release_order_nos': lifecycle_stage['release_order_ids'],
                'lifecycle_freight': lifecycle_stage['freight']}
               if lifecycle_stage is not None else {})}
