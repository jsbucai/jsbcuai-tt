"""Physical label order, independent of card selection and summary wording."""
import re
import unicodedata
import json
from decimal import Decimal


def sort_fields(row):
    return row.get('_print_sort_fields') or [row.get('product_name') or row.get('name') or '',
        row.get('specification') or row.get('spec') or '', row.get('model') or '', row.get('sku') or '']


def catalog_sort_fields(db, rows):
    """Match the card's specification fallback using full stock identity."""
    from .queries import inventory_key
    skus = sorted({str(row.get('sku') or '').upper() for row in rows})
    candidates = {}
    for start in range(0, len(skus), 500):
        batch = skus[start:start + 500]
        for value in db.execute("SELECT payload FROM records WHERE kind='inventory' AND sku IN (" +
                                ','.join('?' for _ in batch) + ')', batch):
            item = json.loads(value[0])
            candidates.setdefault(inventory_key(item), []).append(item)
    result = []
    for row in rows:
        found = candidates.get(inventory_key(row), [])
        inv = found[0] if len(found) == 1 else {}
        fields = [row.get('product_name') or inv.get('product_name') or row.get('name') or '',
                  row.get('specification') or row.get('spec') or inv.get('specification') or inv.get('spec') or
                  row.get('model') or inv.get('model') or '', row.get('model') or inv.get('model') or '', row.get('sku') or '']
        result.append(dict(row, _print_sort_fields=fields))
    return result


def natural(value):
    value = unicodedata.normalize('NFKC', str(value or '')).strip().casefold()
    value = re.sub(r'\s+', '', value).replace('×', 'x').replace('*', 'x')
    return tuple((1, Decimal(part)) if re.fullmatch(r'\d+(?:\.\d+)?', part)
                 else (0, part) for part in re.split(r'(\d+(?:\.\d+)?)', value) if part)


def label_key(row):
    # Use order/catalog fields, never shared order-summary descriptions.
    name, spec, model, sku = sort_fields(row)
    return (natural(name), natural(spec), natural(model),
            natural(sku), natural(row.get('order_no')),
            natural(row.get('line_id')), natural(row.get('id')))


def ordered_segments(orders, groups):
    """Keep whole-order claims and globally sorted labels across 100-label segments.

    Overlapping multi-line order ranges must share one segment. Splitting such
    a range would either break global sort or release only part of an order.
    Equal single-line specs still split freely using deterministic identity ties.
    """
    ranges = []
    for order in orders:
        keys = [label_key(row) for row in groups[(order['profile_id'], order['order_no'])]]
        ranges.append((min(keys), max(keys), order))
    ranges.sort(key=lambda value: value[0])
    blocks = []
    for lower, upper, order in ranges:
        if blocks and lower < blocks[-1]['upper']:
            blocks[-1]['orders'].append(order)
            blocks[-1]['upper'] = max(blocks[-1]['upper'], upper)
        else:
            blocks.append({'upper': upper, 'orders': [order]})
    segments = []
    for block in blocks:
        members = block['orders']
        labels = sum(order['label_count'] for order in members)
        if labels > 100 or len(members) > 100:
            raise ValueError('多行整单交叉覆盖多个规格，顺序打印超过单段100张；请缩小选择范围，未截断或拆开整单')
        if not segments or segments[-1]['label_count'] + labels > 100 or segments[-1]['order_count'] + len(members) > 100:
            segments.append({'index': len(segments) + 1, 'order_nos': [], 'order_count': 0, 'label_count': 0})
        segment = segments[-1]
        segment['order_nos'].extend(order['order_no'] for order in members)
        segment['order_count'] += len(members)
        segment['label_count'] += labels
    return [value[2] for value in ranges], segments
