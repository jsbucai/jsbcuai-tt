import json
import re
from datetime import date, datetime, timedelta
from .rules import ORDER_LOGISTICS, STOCK_LOGISTICS, normalized, quantity, text, parse_summary
from .print_jobs import JobConflict, data_version
from .daily_work import _in_unfulfilled_scope
from .product_catalog import current_products
from .order_print_state import (PRINT_STATES, annotate_print_states, filter_print_state, location_sort_key,
                                natural_location_key, normalized_order_dates,
                                print_state_counts)


INVENTORY_IDENTITY = ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')
CUSTOMER_SOURCE = 'wms_receiveraddress'


class QueryCoverageUnavailable(Exception):
    def __init__(self, coverage):
        self.coverage = coverage
        super().__init__('所选日期超出已同步的进货流水范围，请缩短查询时间；未返回不完整结果')


def inventory_key(item):
    return (text(item.get('sku')).upper(), any(key in item for key in INVENTORY_IDENTITY),
            *(text(item.get(key)) for key in INVENTORY_IDENTITY))


def paginate(items, args):
    page = max(1, int(args.get("page", 1)))
    size = min(100, max(1, int(args.get("page_size", 50))))
    return {"items": items[(page - 1) * size:page * size], "count": len(items), "total": len(items), "page": page, "page_size": size, "returned": len(items[(page - 1) * size:page * size])}


class Queries:
    def __init__(self, store):
        self.store = store

    def check_version(self, args):
        expected = args.get('expected_data_version')
        if expected is not None and expected != data_version(self.store):
            raise JobConflict('业务数据已更新，正在重新查询第一页', 'data_version_changed')

    def source_scope(self):
        with self.store.connect() as db:
            rows = {row['key']: json.loads(row['value']) for row in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('data_query_scope','query_scope','data_quantity_basis')")}
        scope = rows.get('data_query_scope', rows.get('query_scope', {}))
        return {'query_scope': scope, 'quantity_basis': rows.get('data_quantity_basis', scope.get('quantity_basis'))}

    def purchase_coverage(self, days, cutoff):
        scope = self.source_scope()['query_scope']
        start, end = scope.get('purchase_history_start'), scope.get('purchase_history_end')
        coverage = {'kind': 'posted_purchase_history' if start and end else 'unverified',
            'start': start, 'end': end, 'available': bool(start and end),
            'date_basis': 'posted_at' if start and end else 'document_date',
            'quantity_basis': 'posted_movement' if start and end else None,
            'pending_purchase_batches': False, 'requested_days': days, 'requested_start': cutoff,
            'message': '已过账进货流水，数量不代表采购未到批次' if start and end else '尚无已验证的采购读取覆盖记录'}
        if start and cutoff < str(start)[:10]:
            raise QueryCoverageUnavailable(coverage)
        return coverage

    def inventory_lookup(self, sku, identity=None):
        """Never attach another warehouse or unit's stock to an order line."""
        sku = text(sku).upper()
        with self.store.connect() as db:
            values = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM records WHERE kind='inventory' AND sku=?", (sku,))]
        values = current_products(values)
        missing = {'sku': sku, 'found': False, 'stock_quantity': '', 'available_stock': ''}
        if identity is not None and not any(key in identity for key in INVENTORY_IDENTITY) and any(
                key in value for value in values for key in INVENTORY_IDENTITY):
            return {**missing, 'reason': 'inventory_identity_incomplete'}
        if identity is not None and any(key in identity for key in INVENTORY_IDENTITY):
            if not all(text(identity.get(key)) for key in INVENTORY_IDENTITY):
                return {**missing, 'reason': 'inventory_identity_incomplete'}
            values = [value for value in values if all(
                text(value.get(key)) == text(identity[key]) for key in INVENTORY_IDENTITY)]
        if len(values) == 1:
            return {**values[0], 'found': True}
        if len(values) > 1:
            return {**missing, 'ambiguous': True, 'reason': 'inventory_identity_ambiguous',
                    'candidate_count': len(values)}
        return missing

    def orders(self, args):
        self.check_version(args)
        q = text(args.get("q") or args.get("sku") or args.get("buyer") or
                 args.get("summary") or args.get("note"))
        mode = args.get("mode") or args.get("kind") or "sku"
        scope = args.get("scope", "all")
        if mode not in {"sku", "order_no", "buyer", "note", "summary"} or scope not in {"all", "stock"}:
            raise ValueError("查询方式或查询范围无效")
        state_filter = text(args.get('print_state') or 'all')
        sort = text(args.get('sort') or 'default')
        customer = text(args.get('customer'))
        if state_filter not in PRINT_STATES | {'all'}:
            raise ValueError('打印状态无效')
        if sort not in {'default', 'location', 'order_date', 'delivery_date'}:
            raise ValueError('订单排序方式无效')
        boundaries = {}
        for key in ('order_date_from', 'order_date_to', 'delivery_date_from', 'delivery_date_to'):
            value = text(args.get(key))
            if value:
                try:
                    boundaries[key] = date.fromisoformat(value).isoformat()
                except ValueError:
                    raise ValueError('订单日期必须使用 YYYY-MM-DD') from None
        if (boundaries.get('order_date_from', '') > boundaries.get('order_date_to', '9999-12-31') or
                boundaries.get('delivery_date_from', '') > boundaries.get('delivery_date_to', '9999-12-31')):
            raise ValueError('日期起点不能晚于终点')
        structured = (scope == 'stock' or bool(text(args.get('logistics_company'))) or bool(customer) or
                      'print_state' in args or bool(boundaries))
        with self.store.connect() as db:
            if scope == "stock":
                setting = db.execute("SELECT value FROM settings WHERE key='logistics_available'").fetchone()
                available = bool(setting and json.loads(setting[0]))
                if not available:
                    # Versions before the row-level partial-publication fix
                    # stored false whenever even one API order was blocked.
                    # The API publisher nevertheless wrote logistics_company
                    # on every row, so an already-published ERP generation is
                    # safe to filter immediately after upgrading the service.
                    source = db.execute("SELECT payload FROM sources WHERE kind='orders'").fetchone()
                    try:
                        available = bool(source and json.loads(source[0]).get('source') == 'erp_api')
                    except (TypeError, ValueError):
                        available = False
                if not available:
                    raise ValueError("订单副本缺少物流公司列，不能确定库存件范围")
            if (not q or q in {"0", "0.0"}) and not structured:
                rows = []
            elif not q or q in {"0", "0.0"}:
                rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE kind='orders'")]
            elif mode == "order_no":
                rows = [row for row in self.store.rows('orders')
                        if text(row.get('order_no')) == q]
            elif mode == "sku":
                exact = db.execute("SELECT payload FROM records WHERE kind='orders' AND sku=?", (q.upper(),)).fetchall()
                rows = [json.loads(row[0]) for row in exact]
                if not rows:
                    rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE kind='orders'")]
                    scanned = re.fullmatch(r'@\d{2}/\d{2}#(.+)', q)
                    order_number = scanned.group(1) if scanned else q
                    order_matches = [row for row in rows if text(row.get('order_no')) == order_number]
                    rows = order_matches if scanned or order_matches else [row for row in rows if
                            any(normalized(q) in normalized(row.get(field)) for field in
                                ("sku", "order_no", "shipping_id", "source_shipping_id", "system_note", "summary"))]
            else:
                if mode == 'buyer':
                    rows = [row for row in self.store.rows('orders')
                            if row.get('customer_source') == CUSTOMER_SOURCE and
                            q.casefold() in text(row.get('buyer')).casefold()]
                else:
                    rows = [row for row in self.store.rows('orders')
                            if q.casefold() in text(row.get('summary') or row.get('system_note')).casefold()]
        rows = [row for row in rows if _in_unfulfilled_scope(row)]
        if scope == "stock":
            rows = [row for row in rows if row.get("logistics_company") in STOCK_LOGISTICS]
        rows = [row for row in rows if all(not text(args.get(key)) or
                text(row.get(key)) == text(args[key]) for key in INVENTORY_IDENTITY)]
        company = text(args.get('logistics_company'))
        if company:
            if company not in ORDER_LOGISTICS or (scope == 'stock' and company not in STOCK_LOGISTICS):
                raise ValueError('物流分类与当前查询范围不匹配')
            rows = [row for row in rows if row.get('logistics_company') == company]
        identity = {key: args[key] for key in INVENTORY_IDENTITY if args.get(key) is not None}
        inventory_rows = []
        wanted_skus = sorted({text(row.get('sku')).upper() for row in rows if text(row.get('sku'))})
        if wanted_skus:
            with self.store.connect() as db:
                for start in range(0, len(wanted_skus), 500):
                    batch = wanted_skus[start:start + 500]
                    inventory_rows.extend(json.loads(item[0]) for item in db.execute(
                        "SELECT payload FROM records WHERE kind='inventory' AND sku IN (" +
                        ','.join('?' for _ in batch) + ')', batch))
        exact_inventory = {}
        legacy_inventory = {}
        for item in inventory_rows:
            exact_inventory.setdefault(tuple(text(item.get(key)) for key in INVENTORY_IDENTITY), []).append(item)
            legacy_inventory.setdefault(text(item.get('sku')).upper(), []).append(item)
        decorated = []
        for source in rows:
            row = dict(source)
            # Published snapshots created before 0.3.1 may contain a marketplace
            # account in buyer.  Never expose that legacy value as a customer.
            # It becomes authoritative only after a WMS receiveraddress sync.
            if row.get('customer_source') != CUSTOMER_SOURCE:
                row['buyer'] = ''
                row['customer_name'] = ''
            row['summary'] = text(row.get('summary') or row.get('system_note'))
            row['system_note'] = text(row.get('system_note') or row.get('summary'))
            parsed_summary = parse_summary(row['system_note'])
            row['print_warnings'] = parsed_summary['warnings']
            row['summary_person'] = parsed_summary['fields'].get('summaryPerson', '')
            row['delivery_date_status'] = parsed_summary['delivery_date_status']
            order_date, delivery_date = normalized_order_dates(row)
            row['order_date'], row['delivery_date'] = order_date, delivery_date
            if (delivery_date and row.get('delivery_date_confirmation_id') and
                    parsed_summary['delivery_date_status'] == 'missing'):
                row['delivery_date_status'] = 'known'
                row['print_warnings'] = [w for w in row['print_warnings'] if w != '交期待确认']
            location = text(row.get('location'))
            if not location:
                key = tuple(text(row.get(name)) for name in INVENTORY_IDENTITY)
                candidates = exact_inventory.get(key, []) if all(key) else legacy_inventory.get(text(row.get('sku')).upper(), [])
                if len(candidates) == 1:
                    location = text(candidates[0].get('location'))
            row['location'] = location
            row['location_sort_key'] = location_sort_key(location)
            if boundaries.get('order_date_from') and (not order_date or order_date < boundaries['order_date_from']):
                continue
            if boundaries.get('order_date_to') and (not order_date or order_date > boundaries['order_date_to']):
                continue
            if boundaries.get('delivery_date_from') and (not delivery_date or delivery_date < boundaries['delivery_date_from']):
                continue
            if boundaries.get('delivery_date_to') and (not delivery_date or delivery_date > boundaries['delivery_date_to']):
                continue
            decorated.append(row)
        customer_options = sorted({text(row.get('buyer')) for row in decorated if text(row.get('buyer'))},
                                  key=lambda value: value.casefold())
        if customer:
            decorated = [row for row in decorated if text(row.get('buyer')) == customer]
        rows = annotate_print_states(self.store, decorated)
        counts = print_state_counts(rows)
        line_counts = {state: sum(row['print_state'] == state for row in rows) for state in PRINT_STATES}
        line_counts['all'] = len(rows)
        if state_filter != 'all':
            rows = ([row for row in rows if row['print_state'] == state_filter]
                    if args.get('count_basis') == 'lines' else filter_print_state(rows, state_filter))
        if sort == 'location':
            rows.sort(key=lambda row: (natural_location_key(row.get('location')), row.get('order_no', ''), row.get('id', '')))
        elif sort in {'order_date', 'delivery_date'}:
            rows.sort(key=lambda row: (not bool(row.get(sort)), row.get(sort, ''),
                                       natural_location_key(row.get('location')), row.get('order_no', ''), row.get('id', '')))
        result_mode = 'summary' if mode == 'summary' else mode
        return {**paginate(rows, args), "scope": scope, "mode": result_mode, "q": q,
                "sku": q.upper(), "total_quantity": sum(quantity(row.get("quantity")) for row in rows),
                "sku_count": len({row.get("sku") for row in rows}),
                "inventory": self.inventory_lookup(q, identity or None) if mode == "sku" and q else None,
                "print_state": state_filter, "print_state_counts": counts, "sort": sort,
                "line_state_counts": line_counts, "line_count": len(rows),
                "order_count": len({(text(row.get('profile_id')), text(row.get('order_no'))) for row in rows}),
                "customer": customer, "customer_options": customer_options,
                "customer_count": len(customer_options)}

    def inventory(self, args):
        self.check_version(args)
        rows = current_products(self.store.rows("inventory"))
        fields = {"sku": "sku", "name": "product_name", "spec": "spec", "model": "model", "material": "material", "status": "status"}
        q = normalized(args.get("q"))
        matched = [row for row in rows if (not q or q in normalized(" ".join(text(value) for value in row.values()))) and all(not args.get(key) or normalized(args[key]) in normalized(row.get(field)) for key, field in fields.items()) and (text(args.get("only_available")).lower() not in {"1", "true", "yes", "on"} or quantity(row.get("available_stock")) > 0)]
        return {**paginate(matched, args), "total_stock": sum(quantity(row.get("stock_quantity")) for row in matched), "total_available": sum(quantity(row.get("available_stock")) for row in matched)}

    def purchases(self, args):
        self.check_version(args)
        days = min(3650, max(1, int(args.get("days", 180))))
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        coverage = self.purchase_coverage(days, cutoff)
        q = normalized(args.get("q"))
        fields = {"sku": "sku", "name": "product_name", "spec": "spec", "model": "model", "party": "party", "document_no": "document_no"}
        matched = []
        for row in self.store.rows("purchases"):
            date = text(row.get(coverage['date_basis']))[:10]
            if date and date < cutoff:
                continue
            if q and q not in normalized(" ".join(text(value) for value in row.values())):
                continue
            if any(args.get(key) and normalized(args[key]) not in normalized(row.get(field)) for key, field in fields.items()):
                continue
            if args.get("note") and not any(normalized(args["note"]) in normalized(row.get(field)) for field in ("note", "summary", "extra_note")):
                continue
            matched.append(row)
        matched.sort(key=lambda row: row.get("document_date", ""), reverse=True)
        def grouped(field):
            groups = {}
            for row in matched:
                key = row.get(field) or "-"
                item = groups.setdefault(key, {"key": key, "count": 0, "quantity": 0, "amount": 0, "latest_date": ""})
                item["count"] += 1
                item["quantity"] += quantity(row.get("quantity"))
                item["amount"] += quantity(row.get("tax_total") or row.get("amount"))
                item["latest_date"] = max(item["latest_date"], row.get("document_date", ""))
            return sorted(groups.values(), key=lambda item: (item["latest_date"], item["quantity"]), reverse=True)
        parties, skus = grouped("party"), grouped("sku")
        return {**paginate(matched, args), "coverage": coverage, "days": days, "total_quantity": sum(quantity(row.get("quantity")) for row in matched), "total_amount": round(sum(quantity(row.get("tax_total") or row.get("amount")) for row in matched), 4), "party_count": len(parties), "sku_count": len(skus), "party_summary": parties[:30], "sku_summary": skus[:30], "latest_date": max((row.get("document_date", "") for row in matched), default="")}
