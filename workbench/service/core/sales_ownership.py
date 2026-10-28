"""Customer-level sales ownership, independent of historical work actors."""
from __future__ import annotations

import json
import uuid
from collections import defaultdict

from .store import timestamp, dumps

CONFIRMED_SOURCES = {"customer_confirmed", "phone_rule_migrated"}


def migrate_customer_owners(db):
    """Import explicit phone rules once; never infer an owner from old work."""
    now = timestamp()
    staff = {row['id']: row for row in db.execute('SELECT * FROM staff_users')}
    def eligible(identity):
        person = staff.get(identity)
        return bool(person and person['active'] and 'sales' in json.loads(person['roles'] or '[]'))
    keys = {(row['profile_id'], row['customer_name']) for row in
            db.execute('SELECT profile_id,customer_name FROM sales_customer_owners')}
    for row in db.execute("SELECT payload FROM records WHERE kind='orders'"):
        item = json.loads(row[0])
        if isinstance(item, dict) and item.get('customer_source') == 'wms_receiveraddress':
            profile, name = str(item.get('profile_id') or '').strip(), str(item.get('customer_name') or item.get('buyer') or '').strip()
            if profile and name:
                keys.add((profile, name))
    for row in db.execute("SELECT profile_id,customer_name FROM customer_order_followups WHERE customer_name<>''"):
        keys.add((row['profile_id'], row['customer_name']))
    rules = defaultdict(set)
    for row in db.execute("SELECT match_value,staff_id FROM daily_work_assignment_rules WHERE role='sales' AND match_type='customer' AND active=1"):
        rules[row['match_value']].add(row['staff_id'])
    profiles_by_name = defaultdict(set)
    for profile, name in keys:
        profiles_by_name[name].add(profile)
    changed = 0
    for profile, name in sorted(keys):
        old = db.execute('SELECT * FROM sales_customer_owners WHERE profile_id=? AND customer_name=?', (profile, name)).fetchone()
        if old and old['source'] in CONFIRMED_SOURCES:
            continue
        candidates = rules.get(name, set())
        owner = next(iter(candidates)) if len(candidates) == 1 else ''
        scope_conflict = bool(candidates and len(profiles_by_name[name]) > 1)
        status = ('review' if candidates and (scope_conflict or len(candidates) != 1 or not eligible(owner)) else
                  'assigned' if owner else 'unassigned')
        if status != 'assigned':
            owner = ''
        # A successful customer-scoped admin call is an explicit assignment.
        if not candidates and old and old['source'] == 'admin':
            owner = old['owner_id']
            status = 'assigned' if owner and eligible(owner) else 'review' if owner else 'unassigned'
        source = 'phone_rule_scope_review' if scope_conflict else 'phone_rule_migrated' if candidates else 'customer_confirmed' if old and old['source'] == 'admin' else 'awaiting_confirmation'
        deputy = old['deputy_id'] if old and old['source'] == 'admin' and owner else ''
        until = old['deputy_until'] if deputy else ''
        if old and (old['owner_id'], old['status'], old['source'], old['deputy_id'], old['deputy_until']) == (owner, status, source, deputy, until):
            continue
        db.execute('''INSERT INTO sales_customer_owners
            (profile_id,customer_name,owner_id,deputy_id,deputy_until,status,source,updated_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(profile_id,customer_name) DO UPDATE SET
            owner_id=excluded.owner_id,deputy_id=excluded.deputy_id,deputy_until=excluded.deputy_until,
            status=excluded.status,source=excluded.source,version=sales_customer_owners.version+1,updated_at=excluded.updated_at''',
            (profile, name, owner, deputy, until, status, source, now))
        db.execute("""INSERT OR IGNORE INTO sales_customer_owner_events VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, profile, name, 'migration:fixed-sales-v2', 'system:ownership_migration',
             old['owner_id'] if old else '', owner, deputy, until,
             '迁移手机明确客户规则' if owner else '无明确客户规则或范围待核对，取消历史推断', now))
        changed += 1
    return changed


def fixed_sales_owner(db, profile, customer):
    row = db.execute('''SELECT o.owner_id FROM sales_customer_owners o JOIN staff_users u ON u.id=o.owner_id
        WHERE o.profile_id=? AND o.customer_name=? AND o.status='assigned'
        AND o.source IN ('customer_confirmed','phone_rule_migrated') AND u.active=1''', (profile, customer)).fetchone()
    if not row:
        return ''
    person = db.execute('SELECT roles FROM staff_users WHERE id=?', (row[0],)).fetchone()
    return row[0] if person and 'sales' in json.loads(person[0] or '[]') else ''


def sync_sales_tasks(db, profile, customer, owner, *, actor='system', reason='统一客户固定销售', key=None, order_nos=None):
    """Only current assignments change. Status, notes and completion facts stay intact."""
    changed = 0
    sql = "SELECT * FROM daily_work_tasks WHERE role='sales' AND condition_state='active' AND status IN ('todo','in_progress','waiting') AND profile_id=?"
    params = [profile]
    if customer is not None:
        sql += " AND customer=?"
        params.append(customer)
    elif order_nos is None:
        raise ValueError("Customer-independent alignment requires explicit orders")
    for task in db.execute(sql, params).fetchall():
        if order_nos is not None and task['order_no'] not in order_nos:
            continue
        if task['assignee_id'] == owner:
            continue
        now, version = timestamp(), task['version'] + 1
        db.execute('UPDATE daily_work_tasks SET assignee_id=?,version=?,updated_at=? WHERE id=?', (owner, version, now, task['id']))
        db.execute('INSERT INTO daily_work_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (uuid.uuid4().hex, task['id'], 'customer_owner_change', actor, task['status'], task['status'],
             reason, task['next_follow_up_date'], ('customer-owner:' + key) if key else None, version,
             dumps({'from': task['assignee_id'], 'to': owner, 'scope': 'customer'}), now))
        changed += 1
    return changed
