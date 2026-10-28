"""Incremental purchase refresh. Read-only ERP queries, no order submission.

Manual 「刷新订单」 uses the last success watermark minus a 72 hour overlap.
Stock has no time filter, so only shortage SKUs and SKUs on those bills are
read. Open sales orders are not rewritten here: a time window would drop
orders that did not change. The dawn full reconcile is the correction path.
`/integrations/wsgjp/notify` stays disabled and is not called.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json

from .purchase_workspace_data import WAREHOUSE, decimal, number, text
from .store import dumps

OVERLAP = timedelta(hours=72)
WATERMARK_KEY = 'erp_refresh_watermark'
PAGE_SIZE = 100
STOCK_BATCH = 500

# Confirmed from the existing collectors (erp_source / inventory_ledger / wms cache).
CAPABILITIES = {
    'erp.bill.billcenterlist': {
        'time_filter': True,
        'params': ['overbegin', 'overend'],
        'used': True,
        'note': '过账时间。增量进货只查水位减去重叠窗口之后的单据。',
    },
    'erp.ewms.stockinbill.list': {
        'time_filter': True,
        'params': ['begintime', 'endtime', 'timetype'],
        'used': False,
        'note': 'timetype 0 是创建时间，1 是更新时间。未采以可销售库存为准，不调用入库任务接口。',
    },
    'erp.goodsstocks.list': {
        'time_filter': False,
        'params': ['ptypeids', 'ktypeids'],
        'used': True,
        'note': '没有时间参数。只定向查询当前未采和本窗口进货涉及的商品。',
    },
    'qimen.taobao.erp.order.sync': {
        'time_filter': True,
        'params': ['query_time_type', 'start_time', 'end_time'],
        'used': False,
        'note': '能按时间过滤，但未修改的未交订单不会出现。增量刷新不重写订单，避免漏掉仍未交的需求。',
    },
}


def shanghai_now():
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def parse_watermark(value):
    raw = text(value).strip('"')
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    return parsed


def read_watermark(db):
    row = db.execute(
        "SELECT value FROM purchase_workspace_meta WHERE key=?", (WATERMARK_KEY,)).fetchone()
    if not row:
        return None
    return parse_watermark(row[0])


def build_plan(watermark, now, *, full=False):
    now = now.astimezone(ZoneInfo('Asia/Shanghai'))
    plan = {'overlap_hours': int(OVERLAP.total_seconds() // 3600), 'capabilities': CAPABILITIES,
            'orders': 'kept_until_full_reconcile', 'stockin': 'not_called'}
    if full or watermark is None:
        plan.update(mode='full', reason='explicit_full' if full else 'no_watermark')
        return plan
    begin = watermark.astimezone(ZoneInfo('Asia/Shanghai')) - OVERLAP
    plan.update(mode='incremental', reason='watermark',
                begin=begin.strftime('%Y-%m-%d %H:%M:%S'),
                end=now.strftime('%Y-%m-%d %H:%M:%S'),
                watermark=watermark.astimezone(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
                billcenter='posting_time_window', stock='sku_directed')
    return plan


def _pages(client, method, params, key, page_name):
    rows = []
    for page in range(1, 2001):
        response = client.call(method, dict(params, **{page_name: page}))
        items = response.get(key) if isinstance(response, dict) else None
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ValueError('ERP 返回的单据结构不完整')
        rows.extend(item for item in items if isinstance(item, dict))
        if len(items) < params.get('pagesize', PAGE_SIZE):
            return rows
    raise ValueError('ERP 增量结果超过分页上限，未写入')


def normalize_purchase_bills(bills, profile):
    """Posted warehouse receipts only. Red and non-purchase documents are skipped."""
    rows = []
    for bill in bills:
        if bill.get('redold') or bill.get('redword'):
            continue
        document_type = text(bill.get('vchtypename'))
        if document_type != '进货入库单':
            continue
        bill_id = text(bill.get('vchcode'))
        document_no = text(bill.get('number'))
        document_date = text(bill.get('date'))[:10]
        party_id = text(bill.get('btypeid'))
        if not bill_id or not document_no or not party_id:
            continue
        for line in bill.get('details') or []:
            if not isinstance(line, dict):
                continue
            warehouse = text(line.get('ktypeid') or bill.get('ktypeid'))
            if warehouse != WAREHOUSE:
                continue
            quantity = decimal(line.get('qty') or 0)
            if quantity <= 0:
                continue
            line_id = text(line.get('dlyorder'))
            product_id = text(line.get('ptypeid'))
            sku_id = text(line.get('skuid'))
            if not line_id or not product_id or not sku_id:
                continue
            rows.append({
                'id': f'purchase:{profile}:{bill_id}:{line_id}',
                'profile_id': profile, 'warehouse_id': warehouse,
                'product_id': product_id, 'sku_id': sku_id, 'base_unit': '1',
                'document_no': document_no, 'document_type': document_type,
                'document_date': document_date,
                'posted_at': text(bill.get('overtime') or document_date),
                'source_bill_id': bill_id, 'source_line_id': line_id,
                'sku': text(line.get('usercode')).upper(),
                'product_name': text(line.get('pfullname')),
                'spec': text(line.get('standard')), 'model': text(line.get('type')),
                'quantity': number(quantity), 'amount': number(line.get('total') or 0),
                'party': text(bill.get('bfullname')), 'party_id': party_id,
                'redold': False, 'redword': False,
            })
    return rows


def short_product_ids(db):
    found = set()
    for row in db.execute("SELECT payload FROM records WHERE kind='inventory'"):
        payload = json.loads(row[0])
        if decimal(payload.get('available_stock') or 0) < 0 and text(payload.get('product_id')):
            found.add(text(payload['product_id']))
    return found


def apply_incremental(db, purchase_rows, stock_rows):
    """Upsert touched receipts and saleable qty. Leave every other row in place."""
    fresh_ids = {row['id'] for row in purchase_rows}
    bill_ids = {row['source_bill_id'] for row in purchase_rows}
    if bill_ids:
        for existing in db.execute("SELECT id, payload FROM records WHERE kind='purchases'"):
            payload = json.loads(existing['payload'])
            if text(payload.get('source_bill_id')) in bill_ids and existing['id'] not in fresh_ids:
                db.execute("DELETE FROM records WHERE kind='purchases' AND id=?", (existing['id'],))
    for row in purchase_rows:
        db.execute(
            'INSERT OR REPLACE INTO records(kind,id,payload,sku,quantity,amount) VALUES (?,?,?,?,?,?)',
            ('purchases', row['id'], dumps(row), row.get('sku'), float(row['quantity'] or 0),
             float(row['amount'] or 0)))
    inventory = {}
    for existing in db.execute("SELECT id, payload FROM records WHERE kind='inventory'"):
        payload = json.loads(existing['payload'])
        inventory[(text(payload.get('product_id')), text(payload.get('sku_id')))] = (existing['id'], payload)
    updated = 0
    for stock in stock_rows:
        if text(stock.get('ktypeid')) != WAREHOUSE:
            continue
        found = inventory.get((text(stock.get('ptypeid')), text(stock.get('skuid'))))
        if not found:
            continue
        identity, payload = found
        payload['stock_quantity'] = number(stock.get('qty') or 0)
        payload['available_stock'] = number(stock.get('saleqty') or 0)
        db.execute("UPDATE records SET payload=? WHERE kind='inventory' AND id=?", (dumps(payload), identity))
        updated += 1
    return {'purchase_rows': len(purchase_rows), 'stock_rows': updated}


class PurchaseIncrementalRefresh:
    def __init__(self, client, store):
        self.client = client
        self.store = store

    def run(self, now=None):
        now = now or shanghai_now()
        with self.store.connect() as db:
            watermark = read_watermark(db)
            plan = build_plan(watermark, now)
            if plan['mode'] != 'incremental':
                return {'applied': False, 'plan': plan}
            profile = text(getattr(self.client, 'account', lambda: '')()) or ''
            product_ids = short_product_ids(db)
        bills = _pages(self.client, 'erp.bill.billcenterlist', {
            'overbegin': plan['begin'], 'overend': plan['end'], 'vchtype': [34],
            'showredbill': True, 'hascustom': True, 'pagesize': PAGE_SIZE,
        }, 'details', 'pageindex')
        purchases = normalize_purchase_bills(bills, profile)
        product_ids.update(row['product_id'] for row in purchases)
        stocks = []
        ids = sorted(product_ids)
        for offset in range(0, len(ids), STOCK_BATCH):
            batch = []
            for value in ids[offset:offset + STOCK_BATCH]:
                batch.append(int(value) if text(value).isdigit() else value)
            if not batch:
                continue
            stocks.extend(_pages(self.client, 'erp.goodsstocks.list', {
                'ptypeids': batch, 'ktypeids': [int(WAREHOUSE)] if WAREHOUSE.isdigit() else [WAREHOUSE],
                'pagesize': PAGE_SIZE,
            }, 'goodsstocks', 'pageno'))
        with self.store.connect(write=True) as db:
            counts = apply_incremental(db, purchases, stocks)
        counts.update(applied=True, plan=plan)
        return counts
