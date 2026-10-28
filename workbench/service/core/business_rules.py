"""Full production-rule pipeline over verified API facts and a durable receipt ledger."""
import copy
import json
from datetime import date
from decimal import Decimal
from .classification import allocation, receipts, procurement
from .logistics import key, decimal, iso_date, inventory_supply, CODES, RULE_VERSION
from .receipt_identity import ledger_warehouse


def identity(row):
    profile, warehouse, product, sku, unit = key(row)
    # Preserve all five dimensions while keeping the proven two-key algorithm.
    return ledger_warehouse(profile, warehouse, sku, unit), product


def serializable(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, allocation.Demand):
        return serializable(vars(value))
    if isinstance(value, dict):
        return {k:serializable(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    return value


def conflict_code(error):
    message = str(error)
    for code, fragments in (
        ('negative_inventory_or_pending', ('实物库存或采购未到数量为负', '采购未到数量不能为负')),
        ('receipt_provenance_gap', ('历史来货来源存在缺口',)),
        ('receipt_order_date_changed', ('历史订单日期变化',)),
        ('receipt_history_conflict', ('历史订单', '历史来货', '订单来货分配', '订单现货来源数量不足')),
        ('receipt_event_conflict', ('入库事件', '入库批次', '入库观察窗口')),
        ('delivery_date_conflict', ('交期', '日期')),
        ('inventory_identity_conflict', ('库存键', '库存未覆盖', '商品编码')),
        ('quantity_conflict', ('数量', '需求')),
    ):
        if any(fragment in message for fragment in fragments):
            return code
    return 'calculation_contract_conflict'


def calculate_snapshot(snapshot, previous_state=None, today=None):
    """No HTTP, files or mutation of caller facts/state. Conflicts never yield a ledger."""
    today = today or date.today()
    result = {'plans':[], 'conflicts':[], 'allocations':[], 'rule_version':RULE_VERSION}
    orders = snapshot['orders']
    try:
        if snapshot.get('rules_contract') != 'v2':
            raise ValueError('完整分类必须使用 v2 数据合同')
        if snapshot.get('receipt_events_complete') is not True:
            raise ValueError('实际入库事件未完整读取')
        if previous_state is not None and str(previous_state.get('profile_id')) != str(snapshot['profile_id']):
            raise ValueError('入库账本未完成账套身份映射')
        period = tuple(snapshot['receipt_period'])
        if len(period) != 2 or iso_date(period[0]) > iso_date(period[1]) or iso_date(period[1]) != today:
            raise ValueError('入库观察窗口必须覆盖本次业务日期')
        stocks, stock_rows = {}, {}
        for row in snapshot['inventory']:
            k = identity(row)
            if k in stocks: raise ValueError('库存键重复')
            stocks[k] = float(decimal(row['physical_qty']))
            stock_rows[k] = row
        grouped, sales, numbers = {}, [], set()
        for order in orders:
            number = order['order_no']
            if not number or number in numbers: raise ValueError('订单编号缺失或重复')
            numbers.add(number)
            ordered = iso_date(order.get('allocation_order_date') or order['order_date'])
            # Delivery dates affect urging and freight writes, not FIFO or
            # eligibility to print actual order lines.
            seen = set()
            for index, line in enumerate(order['lines']):
                line_id = line['line_id']
                if not line_id or line_id in seen: raise ValueError('订单明细标识缺失或重复')
                seen.add(line_id)
                qty = decimal(line['open_qty'])
                if qty <= 0: raise ValueError('标准需求必须只包含正数未完成明细')
                code = str(line['product_code']).upper()
                if not code: raise ValueError('商品编码缺失')
                k = identity(line)
                group = grouped.setdefault((number, *k), {'date':ordered, 'qty':Decimal(0), 'rows':[], 'code':code})
                if group['code'] != code: raise ValueError('同一商品身份对应多个商品编码')
                group['qty'] += qty
                group['rows'].append(index + 1)
                sales.append({'订单编号':number, '仓库':k[0], '商品编号':k[1], '未完成数量':float(qty),
                              '到货日期':line.get('delivery_date'), '_row':index + 1,
                              '_shadow_demand':bool(order.get('shadow_demand'))})
        demand_totals = {}
        for grouped_key, group in grouped.items():
            k = grouped_key[1:]
            demand_totals[k] = demand_totals.get(k, Decimal(0)) + group['qty']
        supply_balances = {k:inventory_supply(row, demand_totals.get(k, Decimal(0)))
                           for k, row in stock_rows.items()}
        incoming = {k:float(balance['pending']) for k,balance in supply_balances.items()}
        allocation_stocks = {k:float(balance['effective_physical'])
                             for k,balance in supply_balances.items()}
        demands = [allocation.Demand(*k, v['date'], float(v['qty']), tuple(v['rows']), v['code']) for k,v in grouped.items()]
        if len({d.order for d in demands}) != len(orders): raise ValueError('存在无有效未完成明细的订单')
        for _, wh, sku in grouped:
            k = wh, sku
            if k not in stocks: raise ValueError('库存未覆盖全部需求键')
        events, ids = [], set()
        for row in snapshot['receipt_events']:
            event_id = row['event_id']
            if not event_id or event_id in ids: raise ValueError('入库事件标识缺失或重复')
            ids.add(event_id)
            k = identity(row)
            event_date = iso_date(row['posted_date'])
            # billcenterlist uses overbegin/overend (posting time). A backdated
            # bill can legitimately predate the window while posting inside it.
            # Do not widen the verified window or change durable event identity.
            observed_day = iso_date(receipts.observation_day(row))
            if not iso_date(period[0]) <= observed_day <= today:
                raise ValueError('入库事件超出完整观察窗口')
            if event_date > today: raise ValueError('入库事件单据日期晚于业务日期')
            if row['type'] not in {'进货入库单','进货退货单','内部组装／拆卸'}: raise ValueError('入库事件类型未验证')
            events.append({'id':event_id, 'document':row['document_no'], 'warehouse':k[0], 'sku':k[1],
                           'date':event_date.isoformat(), 'type':row['type'], 'quantity':float(decimal(row['quantity'])),
                           'supplier_id':row.get('supplier_id',''), 'supplier_name':row.get('supplier_name',''),
                           **({'posted_at': row['posted_at']} if row.get('posted_at') else {})})
        rows = allocation.allocate(demands, allocation_stocks, incoming)
        for row in rows:
            balance = supply_balances[row['demand'].key]
            row.update(
                physical_stock=float(balance['physical']),
                effective_physical=float(balance['effective_physical']),
                inferred_other_reserved=float(balance['inferred_other_reserved']),
                stock_observed_at=stock_rows[row['demand'].key].get('stock_observed_at') or snapshot.get('stock_observed_at', ''),
                receipt_covered_through=snapshot.get('receipt_covered_through', ''),
            )
        state = receipts.reconcile(events, copy.deepcopy(previous_state), period, stocks)
        state['profile_id'] = str(snapshot['profile_id'])
        rows = receipts.classify(rows, state, today)
        metadata = procurement.classify(rows, sales, today)
        categories = {row['demand'].order:row['order_category'] for row in rows}
        for order in orders:
            category = categories[order['order_no']]
            if category == '库存件':
                category = '库存件组合'
            if category not in CODES and category != '库存件组合':
                raise ValueError('订单分类没有对应规则')
            shipping = procurement.as_date(order.get('delivery_date'))
            if not shipping or category == '库存件组合':
                result['plans'].append({'tradeid':order['order_no'],
                    'freightname':category, 'freightcode':CODES.get(category, ''),
                    'freightnumber':f'@{shipping:%m/%d}#{order["order_no"]}' if shipping else '',
                    'rule_version':RULE_VERSION, 'write_enabled':False,
                    'write_block_reason':'交期待确认' if not shipping else '组合库存件仅作本地分类，无ERP物流编码'})
                continue
            result['plans'].append({'tradeid':order['order_no'], 'freightname':category, 'freightcode':CODES[category],
                'freightnumber':f'@{shipping:%m/%d}#{order["order_no"]}', 'rule_version':RULE_VERSION})
        result.update(allocations=serializable(rows), receipt_state=state, procurement=serializable(metadata))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        # Raw supplier/order data remains in private input, never exception messages.
        result['plans'] = []
        safe_reasons = {'历史来货来源存在缺口，齐货分工待核对',
                        '历史订单日期变化，来货归属待核对'}
        reason = str(error) if str(error) in safe_reasons else '完整分类数据不闭合或存在冲突，请核对交期、入库账本及库存口径'
        result['conflicts'] = [{'code':conflict_code(error), 'reason':reason}]
    return result
