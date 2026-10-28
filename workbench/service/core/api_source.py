"""API source boundary. Unverified semantics must not become a production snapshot."""
import hashlib
import json
from datetime import datetime, date
from .rules import normalized, quantity, text
from .store import dumps, timestamp


class SourceIncomplete(Exception):
    pass


class SourceChanged(SourceIncomplete):
    """A read-only paginated source moved; retry without discarding good data."""


class WmsSource:
    def __init__(self, client, store=None, ignored_order_ids=None):
        self.client = client
        self.store = store
        self.ignored_order_ids = tuple(
            str(value).strip() for value in (ignored_order_ids or ()) if str(value).strip()
        )
        if store is None:
            self.reference_cache = None
            self.wms_cache = None
            self.inventory_ledger = None
            self.inventory_subset = None
            self.order_lifecycle = None
        else:
            from .erp_cache import ReferenceCache
            from .wms_cache import WmsCache
            from .inventory_ledger import InventoryLedger, InventorySubsetRefresher
            from .order_lifecycle import OrderLifecycle
            self.reference_cache = ReferenceCache(store)
            self.wms_cache = WmsCache(store)
            self.inventory_ledger = InventoryLedger(store)
            self.inventory_subset = InventorySubsetRefresher(store, client)
            self.order_lifecycle = OrderLifecycle(store)

    def collect(self, progress=None, full_reconcile=False):
        from .erp_source import LiveSource
        return LiveSource(self.client, reference_cache=self.reference_cache,
            wms_cache=self.wms_cache, inventory_ledger=self.inventory_ledger,
            order_lifecycle=self.order_lifecycle,
            ignored_order_ids=self.ignored_order_ids).collect(
                progress=progress, full_reconcile=full_reconcile)

    def refresh_inventory_subset(self, product_ids=None, identities=None, progress=None):
        if self.inventory_subset is None:
            raise SourceIncomplete('库存子集刷新需要持久化数据库')
        return self.inventory_subset.refresh(product_ids=product_ids, identities=identities,
                                             progress=progress)


def validate_snapshot(snapshot):
    """Shared contract for an API-backed adapter; fixtures use the same validator."""
    if not isinstance(snapshot, dict) or not (snapshot.get('query_complete') is True or snapshot.get('complete') is True):
        raise SourceIncomplete('未取得完整、同一批次的 API 数据')
    for field in ('profile_id', 'source_updated_at', 'orders', 'inventory', 'records'):
        if field not in snapshot:
            raise SourceIncomplete('标准快照缺少必要字段')
    if not snapshot['profile_id'] or not isinstance(snapshot['orders'], list) or not isinstance(snapshot['inventory'], list):
        raise SourceIncomplete('账套或标准数据集合无效')
    try:
        updated = datetime.fromisoformat(snapshot['source_updated_at'])
        if updated.tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError):
        raise SourceIncomplete('数据时间必须包含时区') from None
    if not isinstance(snapshot['records'], dict) or set(snapshot['records']) != {'orders','inventory','purchases'}:
        raise SourceIncomplete('订单、库存和采购记录必须同时提供')
    for kind, records in snapshot['records'].items():
        if not isinstance(records, list):
            raise SourceIncomplete('标准快照集合无效')
        if any(not isinstance(x, dict) for x in records):
            raise SourceIncomplete('标准快照记录无效')
        ids = [str(x.get('id') or '') for x in records]
        if any(not x for x in ids) or len(ids) != len(set(ids)):
            raise SourceIncomplete('标准快照缺少稳定标识或存在重复记录')
    if snapshot.get('rules_contract', 'v1') not in {'v1','v2'}:
        raise SourceIncomplete('分类数据合同版本未支持')
    # Optional for old adapters; when supplied these timestamps are evidence
    # used to distinguish late receipt facts from genuinely pre-existing stock.
    # Never accept a timezone-less or malformed observation as proof.
    observed_times = [(snapshot, name) for name in
                      ('stock_observed_at', 'receipt_covered_through')]
    observed_times.extend((row, 'stock_observed_at') for row in snapshot['inventory'])
    receipt_rows = snapshot.get('receipt_events', [])
    observed_times.extend((row, 'posted_at') for row in
                          (receipt_rows if isinstance(receipt_rows, list) else [])
                          if isinstance(row, dict))
    for item, name in observed_times:
        if not isinstance(item, dict) or not item.get(name):
            continue
        try:
            observed = datetime.fromisoformat(item[name])
            if observed.tzinfo is None:
                raise ValueError()
        except (TypeError, ValueError):
            raise SourceIncomplete('库存或入库来源的观察时间必须有效且包含时区') from None
    issues = snapshot.get('calculation_issues', [])
    if not isinstance(issues,list) or any(not isinstance(item,dict) for item in issues):
        raise SourceIncomplete('分类缺项清单无效')
    if 'query_scope' in snapshot and not isinstance(snapshot['query_scope'],dict):
        raise SourceIncomplete('查询覆盖范围元数据无效')
    if 'inventory_ledger' in snapshot:
        from .inventory_ledger import InventoryLedger
        InventoryLedger.validate_stage(snapshot['inventory_ledger'], snapshot['profile_id'])
    if 'product_images' in snapshot:
        images = snapshot['product_images']
        if (not isinstance(images, list) or any(not isinstance(item, dict) for item in images)
                or any(str(item.get('profile_id')) != str(snapshot['profile_id'])
                       or not item.get('image_id') or not item.get('product_id')
                       or not item.get('source_url') for item in images)):
            raise SourceIncomplete('商品图片元数据无效')
    if 'order_lifecycle' in snapshot:
        lifecycle = snapshot['order_lifecycle']
        if (not isinstance(lifecycle, dict) or
                str(lifecycle.get('profile_id')) != str(snapshot['profile_id']) or
                not isinstance(lifecycle.get('updates'), dict) or
                not isinstance(lifecycle.get('shadow_order_ids'), list) or
                not isinstance(lifecycle.get('release_order_ids'), list)):
            raise SourceIncomplete('订单生命周期暂存数据无效')
        from .order_lifecycle import STATES
        if any(not isinstance(row, dict) or row.get('lifecycle_state') not in STATES
               for row in lifecycle['updates'].values()):
            raise SourceIncomplete('订单生命周期状态无效')
    extra = []
    if snapshot.get('rules_contract') == 'v2':
        for name in ('receipt_events',):
            if snapshot.get('query_complete') is True and name not in snapshot:
                continue
            if not isinstance(snapshot.get(name), list):
                raise SourceIncomplete('实际入库事件必须提供完整集合')
            extra.extend(snapshot[name])
    if any(not isinstance(o,dict) or not o.get('order_no') or not isinstance(o.get('lines'),list) for o in snapshot['orders']):
        raise SourceIncomplete('订单或明细集合无效')
    if len({str(o['order_no']) for o in snapshot['orders']}) != len(snapshot['orders']):
        raise SourceIncomplete('标准订单编号重复')
    for item in (*snapshot['inventory'], *(line for o in snapshot['orders'] for line in o['lines']), *extra):
        if str(item.get('profile_id')) != str(snapshot['profile_id']):
            raise SourceIncomplete('快照中混入其他账套')
    # Prevent display rows and allocation facts representing different demand sets.
    normalized = {(str(o['order_no']),str(l['line_id'])) for o in snapshot['orders'] for l in o['lines']}
    display = {(str(r.get('order_no')),str(r.get('line_id'))) for r in snapshot['records']['orders']}
    if normalized != display or len(display) != len(snapshot['records']['orders']) or len(normalized) != sum(len(o['lines']) for o in snapshot['orders']):
        raise SourceIncomplete('订单显示记录与计算明细未一一对应')
    from .purchase_price_bounds import validate_sale_price_facts
    try: validate_sale_price_facts(snapshot)
    except ValueError as error: raise SourceIncomplete(str(error)) from None
    return snapshot


def publish(store, snapshot, calculation, job_id=None):
    """Publish display data and version atomically; print/adjustment ledgers survive."""
    validate_snapshot(snapshot)
    plans = {x['tradeid']:x for x in calculation['plans']}
    partial = snapshot.get('query_complete') is True
    if calculation['conflicts'] and not partial:
        # Do not silently drop conflicted demand from an otherwise plausible list.
        raise SourceIncomplete('存在分类数据冲突，保留上一份完整快照')
    records = json.loads(dumps(snapshot['records']))
    by_line = {(str(o['order_no']),str(l['line_id'])):l for o in snapshot['orders'] for l in o['lines']}
    by_order = {str(o['order_no']):o for o in snapshot['orders']}
    order_issues = {}
    for item in calculation.get('issues',[]):
        order_issues.setdefault(item.get('affected_order_no',item.get('order_no')),[]).append(item)
    invalid_delivery_orders=set()
    invalid_delivery_all=False
    for item in snapshot.get('calculation_issues',[]):
        if 'delivery' in str(item.get('code','')):
            if item.get('scope')=='order' and item.get('order_no'):
                invalid_delivery_orders.add(str(item['order_no']))
            else:
                invalid_delivery_all=True
    for row in records['orders']:
        target = plans.get(row['order_no'])
        if target is None and not partial:
            raise SourceIncomplete('存在未计算完成的订单')
        if partial:
            row['calculation_status'] = 'ready' if target else 'blocked'
            row['calculation_issues'] = sorted(order_issues.get(row['order_no'],[]),
                key=lambda item:json.dumps(item,sort_keys=True,ensure_ascii=False)) if not target else []
            if not target and not row['calculation_issues']:
                row['calculation_issues'] = [{'code':'calculation_incomplete','reason':'分类资料待核对','message':'分类资料待核对'}]
            line = by_line[(str(row['order_no']),str(row['line_id']))]
            for name in ('profile_id','warehouse_id','product_id','sku_id','base_unit'):
                if name in line:
                    row[name] = line[name]
        row['logistics_company'] = target['freightname'] if target else ''
        row['shipping_id'] = target['freightnumber'] if target else ''
        if row.get('shadow_demand'):
            # State-500 rows are visible locally and occupy FIFO, but printing
            # is always disabled.  The plan may deliberately have no ERP code
            # while consecutive confirmation or freight mapping is pending.
            row['logistics_company'] = ('订单状态待核对'
                if row.get('lifecycle_pending_review') else '已完成订单需结存')
            row['printable'] = False
            row['print_blocked'] = True
            row['print_block_reason'] = ('订单状态待核对，暂停包装打印'
                if row.get('lifecycle_pending_review') else
                '已出库未结存订单不参与包装打印')
            if target and target.get('freightnumber'):
                row['shipping_id'] = target['freightnumber']
        if partial and not target:
            try:
                if invalid_delivery_all or str(row['order_no']) in invalid_delivery_orders:
                    raise ValueError()
                shipping = date.fromisoformat(by_order[str(row['order_no'])]['delivery_date'])
                row['shipping_id'] = f'@{shipping:%m/%d}#{row["order_no"]}'
            except (ValueError,TypeError,KeyError):
                pass
        if 'summary' not in row:
            raise SourceIncomplete('缺少订单摘要原文')
        row['system_note'] = row['summary']
    for rows in records.values():
        rows.sort(key=lambda row:str(row['id']))
    version = hashlib.sha256(json.dumps(records,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:20]
    published_at = timestamp()
    query_scope=snapshot.get('query_scope',{})
    quantity_basis=snapshot.get('quantity_basis',query_scope.get('quantity_basis',''))
    with store.connect(write=True) as db:
        for kind, rows in records.items():
            db.execute('DELETE FROM records WHERE kind=?', (kind,))
            for row in rows:
                db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (kind, row['id'], dumps(row), text(row.get('sku')).upper(), text(row.get('buyer')),
                     text(row.get('system_note') or row.get('note')), normalized(' '.join(map(text,row.values()))),
                     row.get('document_date',''), quantity(row.get('quantity')), quantity(row.get('amount')),
                     row.get('logistics_company','')))
            meta = {'kind':kind, 'status':'ready', 'count':len(rows), 'source':'erp_api',
                    'source_modified_at':snapshot['source_updated_at'], 'imported_at':published_at, 'sha256':version,
                    'query_scope':query_scope}
            if kind=='orders' and quantity_basis:
                meta['quantity_basis']=quantity_basis
            db.execute('INSERT OR REPLACE INTO sources VALUES (?,?)', (kind,dumps(meta)))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps(version),))
        # API snapshots always publish a row-level logistics_company field.  A
        # partial calculation can legitimately leave a few blocked rows empty;
        # that must not disable the stock filter for every independently
        # calculated row.  ``logistics_available`` only guards legacy file
        # imports whose source did not contain the column at all.
        db.execute("INSERT OR REPLACE INTO settings VALUES ('logistics_available',?)", (dumps(True),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_published_at',?)", (dumps(published_at),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_source_updated_at',?)", (dumps(snapshot['source_updated_at']),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_query_scope',?)", (dumps(query_scope),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('data_quantity_basis',?)", (dumps(quantity_basis),))
        from .purchase_price_bounds import persist_sale_price_facts
        persist_sale_price_facts(db,snapshot,version)
        if 'receipt_state' in calculation:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('receipt_ledger_v1',?)", (dumps(calculation['receipt_state']),))
        if 'inventory_ledger' in snapshot:
            from .inventory_ledger import InventoryLedger
            InventoryLedger.persist(db, snapshot['inventory_ledger'], version)
        if 'product_images' in snapshot:
            from .product_images import persist_image_metadata
            persist_image_metadata(db, snapshot['profile_id'], snapshot['product_images'])
        if 'order_lifecycle' in snapshot:
            from .order_lifecycle import OrderLifecycle
            OrderLifecycle.persist(db, snapshot['order_lifecycle'])
        if job_id:
            db.execute('UPDATE sync_batches SET status=?,data_version=?,published_at=?,snapshot=? WHERE job_id=?',
                       ('published',version,published_at,'{}',job_id))
            db.execute('DELETE FROM sync_issues WHERE job_id=?',(job_id,))
            for item in calculation.get('issues',calculation['conflicts']):
                db.execute('INSERT INTO sync_issues VALUES (?,?,?)',(job_id,item.get('affected_order_no',item.get('order_no','')),dumps(item)))
    return version
