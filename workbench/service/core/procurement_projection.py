"""Minimal purchase-board projection over the already published calculation.

These functions are also used by TaskBoard itself. No staff identity, raw
record, remarks, customer follow-up or authentication table is loaded here.
"""
import json
from collections import defaultdict
from decimal import Decimal

from .daily_work import _text, _decimal, _record_identity, _in_unfulfilled_scope, _current_task
from .customer_orders import (supply_source_groups, order_supply_state,
                              _group_id, _scoped_group_id, _line_identity)


def purchase_identity(task, source):
    identity = _text(task['payload'].get('product_identity'))
    if not identity:
        matches = {_record_identity(row) for (profile, number), lines in source.items()
                   if profile == task['profile_id'] and number in task['payload'].get('order_nos', [task['order_no']])
                   for row in lines if (not task['product_id'] and not task['sku']) or
                   (task['product_id'] and row.get('product_id') == task['product_id']) or
                   (task['sku'] and row.get('sku') == task['sku'])}
        identity = next(iter(matches)) if len(matches) == 1 else 'review:' + task['id']
    return identity


def purchase_totals(members, groups, matching_identity):
    if not groups:
        return tuple(sum((_decimal(t['payload'].get(field)) for t in members), Decimal(0))
                     for field in ('shortage', 'pending'))
    return tuple(sum((_decimal(m[field]) for g in groups for m in g['missing_items']
                      if matching_identity and (m['product_id'], m['sku_id'], m['base_unit']) == matching_identity[2:]), Decimal(0))
                 for field in ('shortage', 'pending'))


def needs_procurement(card):
    return _decimal(card.get('shortage')) > 0


class ProjectionUnavailable(ValueError):
    pass


ORDER_FIELDS = ('profile_id', 'warehouse_id', 'order_no', 'sku', 'product_id', 'sku_id',
                'base_unit', 'quantity', 'customer_source', 'customer_name', 'buyer',
                'calculation_status', 'lifecycle_pending_review', 'lifecycle_release_pending',
                'lifecycle_print_blocked', 'shadow_demand', 'lifecycle_state', 'process_state',
                'lifecycle_confirmations', 'confirmation_count')


def read_cards(db, profile, warehouse, data_version):
    """Return only shortage totals, using the same groups/filter as the role board.

    Caller must hold one read snapshot and validate the task projection version.
    SQL extracts only the fields required for the canonical calculation.
    """
    calc = db.execute("SELECT rowid,profile,json_type(payload,'$.allocations'),"
                      "json_type(payload,'$.blocked_orders') FROM logistics_calculations "
                      "WHERE data_version=? ORDER BY rowid DESC LIMIT 1", (data_version,)).fetchone()
    if not calc or calc[1] != profile or calc[2:] != ('array', 'array'):
        # sqlite.Row slices are tuples; empty arrays are valid, missing arrays aren't.
        raise ProjectionUnavailable('calculation_unavailable')
    columns = ','.join("json_extract(payload,'$." + f + "') AS " + f for f in ORDER_FIELDS)
    orders = [dict(row) for row in db.execute("SELECT " + columns +
              ",CASE WHEN coalesce(json_extract(payload,'$.last_error'),'')<>'' THEN 'present' ELSE '' END AS last_error "
              "FROM records WHERE kind='orders'")]
    if any(r['profile_id'] != profile or r['warehouse_id'] != warehouse for r in orders):
        raise ProjectionUnavailable('projection_scope_mismatch')
    orders = [r for r in orders if _in_unfulfilled_scope(r)]
    allocations = defaultdict(list)
    columns = ','.join("json_extract(j.value,'$." + f + "')" for f in
                      ('order_no', 'demand.order', 'demand.sku', 'demand.warehouse',
                       'demand.quantity', 'actual', 'pending', 'shortage'))
    for r in db.execute('SELECT ' + columns + " FROM logistics_calculations c,"
                        "json_each(c.payload,'$.allocations') j WHERE c.rowid=?", (calc[0],)):
        number = _text(r[0] or r[1])
        if not number:
            continue
        scope = json.loads(r[3]) if isinstance(r[3], str) else r[3]
        if not isinstance(scope, list) or len(scope) < 4 or scope[:2] != [profile, warehouse]:
            raise ProjectionUnavailable('projection_scope_mismatch')
        allocations[number].append({'demand': {'order': r[1], 'sku': r[2], 'warehouse': r[3], 'quantity': r[4]},
                                    'actual': r[5], 'pending': r[6], 'shortage': r[7]})
    blocked = {_text(r[0]) for r in db.execute("SELECT json_extract(j.value,'$.order_no') "
               "FROM logistics_calculations c,json_each(c.payload,'$.blocked_orders') j WHERE c.rowid=?", (calc[0],))}
    grouped, legacy_customers = supply_source_groups(orders)
    aliases = {(r[0], r[1]): r[2] for r in db.execute(
        'SELECT legacy_group_id,customer_name,group_id FROM customer_order_group_aliases WHERE profile_id=? AND warehouse_id=?',
        (profile, warehouse))}
    groups = {}
    for (p, w, po, customer), numbers in grouped.items():
        legacy = _group_id(p, w, po)
        identity = aliases.get((legacy, customer)) or (
            _scoped_group_id(p, w, po, customer) if len(legacy_customers[(p, w, po)]) > 1 else legacy)
        missing = []
        for number, lines in sorted(numbers.items()):
            _, items = order_supply_state(number, lines, allocations, blocked)
            missing.extend(items)
        groups[identity] = {'id': identity, 'profile_id': p, 'numbers': list(numbers), 'missing_items': missing}
    by_order, source = defaultdict(list), defaultdict(list)
    for group in groups.values():
        for number in group['numbers']:
            by_order[(group['profile_id'], number)].append(group)
    for row in orders:
        if _text(row['order_no']):
            source[(_text(row['profile_id']), _text(row['order_no']))].append(row)
    bins = defaultdict(list)
    for row in db.execute("SELECT id,profile_id,order_no,product_id,sku,"
            "json_extract(payload,'$.product_identity') AS product_identity,"
            "json_extract(payload,'$.order_nos') AS order_nos,"
            "json_extract(payload,'$.shortage') AS shortage,json_extract(payload,'$.pending') AS pending "
            "FROM daily_work_tasks WHERE role='purchase' AND condition_state='active'"):
        task = {'role': 'purchase', **dict(row)}
        if task['profile_id'] != profile:
            raise ProjectionUnavailable('projection_scope_mismatch')
        payload = {f: task.pop(f) for f in ('product_identity', 'shortage', 'pending')}
        refs = task.pop('order_nos')
        if refs is not None:
            payload['order_nos'] = json.loads(refs)
            if not isinstance(payload['order_nos'], list):
                raise ProjectionUnavailable('projection_invalid')
        task['payload'] = payload
        if not _current_task(task, source):
            continue
        identity = purchase_identity(task, source)
        parts = identity.split('|')
        refs = task['payload'].get('order_nos', [task['order_no']])
        if len(parts) == 5:
            if parts[:2] != [profile, warehouse]:
                raise ProjectionUnavailable('projection_scope_mismatch')
        elif not refs or any((profile, n) not in source for n in refs):
            raise ProjectionUnavailable('projection_scope_mismatch')
        bins[identity].append(task)
    cards = []
    for identity, members in bins.items():
        refs = {(t['profile_id'], n) for t in members for n in t['payload'].get('order_nos', [t['order_no']]) if n}
        originals = [r for ref in refs for r in source.get(ref, []) if _record_identity(r) == identity]
        identities = {_line_identity(r) for r in originals}
        if len(identities) > 1:
            raise ProjectionUnavailable('projection_identity_ambiguous')
        matching = _line_identity(originals[0]) if originals else None
        related = {g['id']: g for ref in sorted(refs) for g in by_order.get(ref, [])}
        shortage, _ = purchase_totals(members, list(related.values()), matching)
        if not shortage.is_finite():
            raise ProjectionUnavailable('projection_invalid')
        cards.append({'shortage': shortage})
    return cards
