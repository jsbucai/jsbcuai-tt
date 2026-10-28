"""Pure full-snapshot FIFO allocation; Decimal quantities and explicit evidence.

Inputs are normalized, verified ERP facts. No HTTP, database, or UI dependencies.
Receipt quantities refer to already verified allocations inside physical stock,
not additional stock. Supply and receipt evidence must not be counted twice.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

RULE_VERSION = '5-api-7'
STOCK = {p: p + '库存件' for p in ('LS', 'LD', 'LM', 'DQ', 'QT')}
PRIORITY = {'需采购': 0, '催货': 1, '待到货': 2, '异常': 3, '来货件': 4,
            **{v: 5 for v in STOCK.values()}}
CODES = {'需采购':'xcg', '待到货':'ddh', '异常':'YC', '来货件':'lhj', '催货':'CH',
         **{p + '库存件':p + 'kcj' for p in STOCK}}


def decimal(value):
    if isinstance(value, bool):
        raise ValueError('数量不能为布尔值')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('缺少有效数量') from None
    if not result.is_finite():
        raise ValueError('数量必须为有限数')
    return result


def key(row):
    fields = ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')
    result = tuple('' if row.get(k) is None else str(row[k]).strip() for k in fields)
    if any(not x for x in result):
        raise ValueError('账套、仓库、商品、SKU 或基本单位缺失')
    return result


def iso_date(value):
    return date.fromisoformat(str(value))


def inventory_supply(stock, total_sales_open, *, allow_negative_physical=False):
    """Split the ERP saleable-stock residual into supply or other reservations.

    The stock API exposes only physical and saleable quantities.  After all
    sales demand sharing one inventory identity has been aggregated, its net
    residual is::

        net = saleable - physical + total sales open

    A positive residual is purchase/rework supply.  A negative residual is a
    conservative observation of some other draft reservation already included
    by ERP in saleable stock (for example, an assembly/disassembly draft).  It
    reduces the physical quantity available to FIFO and is not malformed
    purchase supply.  ``allow_negative_physical`` is reserved for query/cache
    adapters: allocation callers keep the strict default so a demanded item
    with negative physical stock cannot silently enter FIFO.
    """
    physical = decimal(stock['physical_qty'])
    demand = decimal(total_sales_open)
    net = decimal(stock['saleable_qty']) - physical + demand
    if demand < 0:
        raise ValueError('总需求数量不能为负')
    if physical < 0 and not allow_negative_physical:
        raise ValueError('实物库存数量为负')
    pending = max(net, Decimal(0))
    reserved = max(-net, Decimal(0))
    fields = ('stock_order_qty', 'rework_qty')
    if any(name in stock for name in fields):
        if not all(name in stock for name in fields):
            raise ValueError('订单数量与改制单数量未完整提供')
        parts = [decimal(stock[name]) for name in fields]
        if any(q < 0 for q in parts) or sum(parts) != pending:
            raise ValueError('订单数量与改制单数量未与库存差额闭合')
    return {
        'physical': physical,
        'pending': pending,
        'inferred_other_reserved': reserved,
        'effective_physical': max(Decimal(0), physical - reserved),
    }


def purchase_pending(stock, total_sales_open):
    """Return non-negative purchase/rework supply; kept for API compatibility."""
    return inventory_supply(stock, total_sales_open)['pending']


def calculate(orders, inventory, today=None):
    """Returns plans/conflicts. Never mutates inputs or assumes missing evidence false."""
    today = today or date.today()
    supply, groups, results, conflicts = {}, defaultdict(list), defaultdict(list), {}
    order_map = {}
    for row in inventory:
        k = key(row)
        if k in supply:
            raise ValueError('同一库存键重复')
        supply[k] = row
    seen_lines = set()
    for order in orders:
        number = str(order.get('order_no', '')).strip()
        if not number or number in order_map:
            raise ValueError('订单编号缺失或重复')
        order_map[number] = order
        if not order.get('lines'):
            conflicts[number] = '订单无明细'
            continue
        # Keep demand in allocation even when the header date/format conflicts.
        for line in order['lines']:
            identity = (number, str(line.get('line_id', '')))
            if not identity[1] or identity in seen_lines:
                raise ValueError('明细标识缺失或重复')
            seen_lines.add(identity)
            k = key(line)
            q = decimal(line.get('open_qty'))
            if q < 0:
                raise ValueError('未完成数量不能为负')
            groups[k].append((order, line, q))
    allocations = []
    for k, rows in groups.items():
        try:
            stock = supply[k]
            total_demand = sum((quantity for _, _, quantity in rows), Decimal(0))
            balance = inventory_supply(stock, total_demand)
            physical = max(Decimal(0), balance['effective_physical'])
            transit = balance['pending']
            ordered = sorted(rows, key=lambda item: (
                iso_date(item[0].get('allocation_order_date') or item[0]['order_date']),
                item[0]['order_no'], str(item[1]['line_id'])))
        except (KeyError, ValueError):
            for order, _, _ in rows:
                conflicts[order['order_no']] = '库存、订货日期或在途数据冲突'
            continue
        remaining_physical, remaining_transit = physical, transit
        receipt_ids = set()
        for order, line, need in ordered:
            number = order['order_no']
            if not need:
                continue
            p = min(need, remaining_physical)
            t = min(need - p, remaining_transit)
            gap = need - p - t
            remaining_physical -= p
            remaining_transit -= t
            try:
                # FIFO consumes supplies regardless of final status or conflict.
                prefix = str(line['product_code']).upper()[:2]
                status = '需采购' if gap else '待到货' if t else STOCK.get(prefix)
                if not status:
                    raise ValueError('库存件前缀不明确')
                if line.get('receipt_evidence_complete') is not True or line.get('expedite_evidence_complete') is not True:
                    raise ValueError('实际到货或催货证据未补齐')
                if not isinstance(line.get('expedite_required'), bool) or not isinstance(line.get('allocated_receipts'), list):
                    raise ValueError('到货分配清单或催货结论缺失')
                received = Decimal(0)
                expired = False
                for receipt in line.get('allocated_receipts', []):
                    rid = str(receipt['allocation_id'])
                    if not rid or rid in receipt_ids:
                        raise ValueError('到货分配证据重复')
                    receipt_ids.add(rid)
                    qty = decimal(receipt['qty'])
                    age = (today - iso_date(receipt['received_date'])).days
                    if qty <= 0 or age < 0:
                        raise ValueError('到货分配数量或日期无效')
                    received += qty
                    expired |= age > 7
                if received > p:
                    raise ValueError('到货分配超过本单实物配额')
                if received >= need:
                    status = '来货件'
                if line.get('expedite_required') is True and status == '待到货':
                    status = '催货'
                if expired and p >= need:
                    status = '异常'
                results[number].append(status)
            except (KeyError, ValueError) as exc:
                conflicts[number] = str(exc)
            allocations.append({'order_no':number, 'line_id':str(line['line_id']),
                                'physical':str(p), 'transit':str(t), 'gap':str(gap), 'open_qty':str(need)})
            allocations[-1].update({
                'physical_stock': str(balance['physical']),
                'effective_physical': str(balance['effective_physical']),
                'inferred_other_reserved': str(balance['inferred_other_reserved']),
            })
    plans = []
    for number, order in order_map.items():
        if number in conflicts or not results[number]:
            continue
        states = results[number]
        status = min(states, key=lambda s: PRIORITY[s])
        if PRIORITY[status] == 5 and len(set(states)) > 1:
            status = '库存件组合'
        try:
            shipping = iso_date(order['delivery_date'])
        except (KeyError, ValueError):
            shipping = None
        plan = {'tradeid':number, 'freightname':status, 'freightcode':CODES.get(status, ''),
                'freightnumber':f'@{shipping:%m/%d}#{number}' if shipping else '', 'rule_version':RULE_VERSION}
        if not shipping or status == '库存件组合':
            plan.update(write_enabled=False, write_block_reason='交期待确认' if not shipping else '组合库存件仅作本地分类')
        plans.append(plan)
    return {'plans':plans, 'conflicts':[{'order_no':k, 'reason':v} for k,v in sorted(conflicts.items())],
            'allocations':allocations, 'rule_version':RULE_VERSION}
