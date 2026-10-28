"""Customer PO overview over one published snapshot; row work stays independent."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, g, jsonify, request

from .daily_work import DailyWorkConflict, SHANGHAI, _day, _is_admin, _next_workday, _roles, _text, _user_id, _erp_completion, _lifecycle_review_reason, _in_unfulfilled_scope
from .rules import note_delivery_date
from .store import timestamp
from .sales_ownership import migrate_customer_owners, fixed_sales_owner, sync_sales_tasks

WAREHOUSE = "29871293290508390"  # 08390


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _po(number):
    return number.split("#", 1)[0] if "#" in number else number


def _group_id(profile, warehouse, number):
    """Legacy identity, retained for unambiguous groups and old links."""
    return hashlib.sha256(json.dumps([profile, warehouse, number], ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()[:32]


def _scoped_group_id(profile, warehouse, number, customer):
    return hashlib.sha256(json.dumps([profile, warehouse, number, customer], ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest()[:32]


def groups_for_order(groups, profile_id, order_no):
    """Resolve a complete order number without guessing its customer."""
    return [group for group in groups.values() if group["profile_id"] == profile_id and
            any(line["order_no"] == order_no for line in group["lines"])]


def _customer(item):
    return _text(item.get("customer_name") or item.get("buyer")) if item.get("customer_source") == "wms_receiveraddress" else ""


def _due(item):
    raw = _day(item.get("delivery_date"))
    if raw:
        return raw
    try:
        return note_delivery_date(item.get("summary") or item.get("system_note") or "") or ""
    except ValueError:
        return ""


def _allocation_identity(row):
    demand = row.get("demand") if isinstance(row.get("demand"), dict) else {}
    raw = demand.get("warehouse")
    try:
        parts = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        parts = None
    if isinstance(parts, list) and len(parts) >= 4:
        return (str(parts[0]), str(parts[1]), _text(demand.get("sku")),
                str(parts[2]), str(parts[3]))
    return None


def _line_identity(item):
    return tuple(_text(item.get(k)) for k in
                 ("profile_id", "warehouse_id", "product_id", "sku_id", "base_unit"))


def order_supply_state(number, lines, allocations, blocked):
    """Canonical published-allocation state; no identity, notes or staff access."""
    missing = []
    if _erp_completion(lines):
        row_state = "shipped_unposted"
    else:
        allocation_rows = allocations.get(number, [])
        demanded = defaultdict(Decimal)
        supplied = defaultdict(Decimal)
        pending = defaultdict(Decimal)
        shortage = defaultdict(Decimal)
        bad = bool(_lifecycle_review_reason(lines)) or number in blocked or not allocation_rows or any(
            _text(item.get("calculation_status")) != "ready" or
            item.get("lifecycle_pending_review") for item in lines)
        for item in lines:
            qty = _number(item.get("quantity"))
            key = _line_identity(item)
            if qty is None or qty < 0 or not all(key):
                bad = True
            else:
                demanded[key] += qty
        for row_alloc in allocation_rows:
            key = _allocation_identity(row_alloc)
            raw_need = _number((row_alloc.get("demand") or {}).get("quantity"))
            actual = _number(row_alloc.get("actual"))
            onway = _number(row_alloc.get("pending"))
            gap = _number(row_alloc.get("shortage"))
            if key not in demanded or None in (raw_need, actual, onway, gap) or any(
                value < 0 for value in (raw_need, actual, onway, gap)):
                bad = True
                continue
            supplied[key] += actual
            pending[key] += onway
            shortage[key] += gap
        if set(demanded) != set(supplied) or any(
            demanded[key] != supplied[key] + pending[key] + shortage[key]
            for key in demanded):
            bad = True
        if bad:
            row_state = "review"
        elif any(shortage.values()):
            row_state = "procurement"
        elif any(pending.values()):
            row_state = "arrival"
        else:
            row_state = "ready"
        if row_state in {"arrival", "procurement"}:
            for item in lines:
                key = _line_identity(item)
                qty = pending.get(key, Decimal(0)) + shortage.get(key, Decimal(0))
                if qty > 0 and not any(m["order_no"] == number and m["product_id"] == key[2] and
                                       m["sku_id"] == key[3] and m["base_unit"] == key[4]
                                       for m in missing):
                    missing.append({"order_no": number, "sku": _text(item.get("sku")),
                                    "product_id": key[2], "sku_id": key[3], "base_unit": key[4],
                                    "pending": str(pending.get(key, 0)),
                                    "shortage": str(shortage.get(key, 0))})
    return row_state, missing


def supply_source_groups(orders):
    """Group exactly as the customer board, including conflicting names."""
    source_orders = defaultdict(list)
    for item in orders:
        if not isinstance(item, dict) or not _in_unfulfilled_scope(item):
            continue
        profile, warehouse, number = (_text(item.get("profile_id")),
                                       _text(item.get("warehouse_id")), _text(item.get("order_no")))
        if not profile or warehouse != WAREHOUSE or not number:
            continue
        source_orders[(profile, warehouse, _po(number), number)].append(item)
    groups = defaultdict(lambda: defaultdict(list))
    for (profile, warehouse, po, number), lines in source_orders.items():
        names = {_customer(line) for line in lines}
        # Conflicting line-level customer names remain one reviewable order.
        customer = next(iter(names)) if len(names) == 1 and "" not in names else ""
        groups[(profile, warehouse, po, customer)][number].extend(lines)
    legacy_customers = defaultdict(set)
    for profile, warehouse, po, customer in groups:
        legacy_customers[(profile, warehouse, po)].add(customer)

    return groups, legacy_customers


class CustomerOrders:
    def __init__(self, store, daily_work):
        self.store, self.daily_work = store, daily_work

    def reconcile(self):
        """Import confirmed phone rules and align current work atomically."""
        with self.store.connect(write=True) as db:
            changed = migrate_customer_owners(db)
            groups, version, updated, current, _ = self._build(db)
            if not current:
                return {"changed": changed, "current": False}
            now = timestamp()
            for group in groups.values():
                snapshot = dict(group)
                prior_snapshot = db.execute('SELECT payload FROM customer_order_board_snapshots WHERE group_id=?',
                                            (group['id'],)).fetchone()
                if prior_snapshot:
                    old = json.loads(prior_snapshot['payload'])
                    seen = {line['order_no'] for line in group['lines']}
                    missing = [line for line in old.get('lines',[]) if line['order_no'] not in seen
                               and line['status'] != 'shipped_unposted']
                    if missing:
                        snapshot['lines'] = [*group['lines'],*missing]
                        snapshot['source_missing_review'] = True
                        snapshot['unshipped_count'] += len(missing)
                db.execute('''INSERT INTO customer_order_board_snapshots VALUES (?,?,?,?)
                    ON CONFLICT(group_id) DO UPDATE SET payload=excluded.payload,
                    completed=excluded.completed,updated_at=excluded.updated_at''',
                    (group['id'],json.dumps(snapshot,ensure_ascii=False),
                     int(not snapshot['unshipped_count']),now))
                if group["id"] != group["legacy_group_id"]:
                    db.execute("""INSERT OR IGNORE INTO customer_order_group_aliases
                        (legacy_group_id,group_id,profile_id,warehouse_id,customer_order_no,
                         customer_name,review_required,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (group["legacy_group_id"], group["id"], group["profile_id"],
                         group["warehouse_id"], group["customer_order_no"], group["customer"],
                         int(group["migration_review"]), now))
                owner = group["assignee_id"]
                if group["customer_review"]:
                    owner = ""
                changed += sync_sales_tasks(db, group["profile_id"], None if group["customer_review"] else group["customer"], owner,
                    order_nos={line["order_no"] for line in group["lines"]})
                # A renamed/split legacy record still belongs to its original customer.
                if group["identity_review"] or group["migration_review"]:
                    continue
                existing = db.execute("SELECT * FROM customer_order_followups WHERE group_id=?",
                                      (group["id"],)).fetchone()
                if existing:
                    if group["status"] == "completed":
                        continue
                    legacy = hashlib.sha256(json.dumps([line["order_no"] for line in group["lines"]]).encode()).hexdigest()
                    fingerprint = group["source_fingerprint"] if existing["source_fingerprint"] == legacy else existing["source_fingerprint"]
                    if (existing["assignee_id"], existing["customer_name"], existing["source_fingerprint"]) != (owner, group["customer"], fingerprint):
                        db.execute("""UPDATE customer_order_followups SET assignee_id=?,customer_name=?,
                            source_fingerprint=?,version=version+1,updated_at=? WHERE group_id=?""",
                            (owner, group["customer"], fingerprint, now, group["id"]))
                        changed += 1
                    continue
                if not group["needs_follow_up"] and group["status"] != "packing":
                    continue
                db.execute("""INSERT INTO customer_order_followups
                    (group_id,profile_id,warehouse_id,customer_order_no,assignee_id,status,
                     next_follow_up_date,version,source_data_version,source_fingerprint,
                     created_at,updated_at,customer_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (group["id"], group["profile_id"], group["warehouse_id"], group["customer_order_no"],
                     owner, "todo", "", 1, version, group["source_fingerprint"], now, now, group["customer"]))
                changed += 1
            if changed:
                self.daily_work._bump_version(db)
            return {"changed": changed, "current": True}

    def transfer(self, user, identity, assignee_id, key, version, reason):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以调整客户负责人", "admin_required")
        raise DailyWorkConflict("销售负责人按整个客户统一设置，请升级客户端并使用客户固定销售或临时代办入口",
                                "customer_owner_scope_required")

    def set_customer_owner(self, user, profile_id, customer, owner_id, deputy_id,
                           deputy_until, expected_version, key, reason, *, _mode="owner",
                           _directory_id="", _only_unassigned=False):

        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以设置客户负责人", "admin_required")
        profile_id, customer = _text(profile_id), _text(customer)
        owner_id, deputy_id = _text(owner_id), _text(deputy_id)
        key, reason = _text(key), _text(reason)
        if not profile_id or not customer or not key or not reason or len(key) > 128:
            raise ValueError("请提供账套、客户、操作标识和变更原因")
        try:
            expected = int(expected_version)
        except (TypeError, ValueError):
            raise ValueError("请提供当前客户归属版本") from None
        with self.store.connect(write=True) as db:
            row = db.execute("""SELECT * FROM sales_customer_owners
                WHERE profile_id=? AND customer_name=?""", (profile_id, customer)).fetchone()
            # Each endpoint changes exactly one concept; a deputy is never an owner transfer.
            if _mode == "deputy":
                owner_id = row["owner_id"] if row else ""
            else:
                if deputy_id or deputy_until:
                    raise ValueError("请在独立的临时代办入口设置代办人")
                deputy_id = row["deputy_id"] if row and owner_id else ""
                deputy_until = row["deputy_until"] if deputy_id else ""
                if deputy_id == owner_id:
                    deputy_id, deputy_until = "", ""
            if not deputy_id:
                deputy_until = ""
            event_key = _mode + ":" + key
            replay = db.execute("""SELECT * FROM sales_customer_owner_events
                WHERE profile_id=? AND customer_name=? AND idempotency_key=?""",
                (profile_id, customer, event_key)).fetchone()
            if replay:
                requested = (deputy_id, deputy_until) if _mode == "deputy" else (owner_id,)
                recorded = (replay["deputy_id"], replay["deputy_until"]) if _mode == "deputy" else (replay["owner_id"],)
                if requested != recorded or replay["reason"] != reason:
                    raise DailyWorkConflict("操作标识已用于不同的负责人变更", "idempotency_conflict")
                return {"replayed": True, "owner": dict(row) if row else None}
            directory_verified = False
            if _directory_id:
                matches = db.execute('''SELECT erp_id FROM sales_erp_customers
                    WHERE profile_id=? AND customer_name=? AND active=1''', (profile_id, customer)).fetchall()
                directory_verified = len(matches) == 1 and matches[0][0] == _directory_id
                if not directory_verified:
                    raise DailyWorkConflict("ERP 客户身份变化或存在同名单位，请刷新核对", "customer_identity_review")
            if _only_unassigned and fixed_sales_owner(db, profile_id, customer):
                raise DailyWorkConflict("客户已有固定销售，请刷新；此入口不会转移已有归属", "task_version_changed")
            if deputy_id and (not owner_id or deputy_id == owner_id or not _day(deputy_until) or
                              deputy_until < datetime.now(SHANGHAI).date().isoformat()):
                raise ValueError("临时代办必须不同于负责人，并提供未过期的有效截止日期")
            for actor in (owner_id, deputy_id):
                if not actor:
                    continue
                person = db.execute("SELECT roles,active,display_name FROM staff_users WHERE id=?", (actor,)).fetchone()
                if not person or not person["active"] or "sales" not in json.loads(person["roles"] or "[]"):
                    raise ValueError("负责人或代办人没有有效销售岗位")
                if _only_unassigned and person['display_name'] not in {'李瑄','刘锋'}:
                    raise ValueError("请选择李瑄或刘锋")
            if expected != (row["version"] if row else 0):
                raise DailyWorkConflict("客户归属已变化，请刷新", "task_version_changed")
            groups, _, _, current, _ = self._build(db)
            matching = [group for group in groups.values() if group["profile_id"] == profile_id and
                        (group["customer"] == customer or customer in group["source_customer_names"])]
            has_orders = db.execute('''SELECT 1 FROM records WHERE kind='orders'
                AND json_extract(payload,'$.profile_id')=? AND
                (json_extract(payload,'$.customer_name')=? OR json_extract(payload,'$.buyer')=?) LIMIT 1''',
                (profile_id,customer,customer)).fetchone()
            if not current and (not directory_verified or matching or has_orders):
                raise DailyWorkConflict("任务数据尚未刷新完成，保留输入并稍后重试", "task_version_changed")
            if any(group["customer_review"] for group in matching):
                raise DailyWorkConflict("客户身份或旧订单关联待核对，不能部分调整归属", "customer_identity_review")
            if not row and not matching and not directory_verified:
                raise DailyWorkConflict("客户不在已核实的账套客户范围内", "customer_identity_review")
            if _mode == "deputy" and (not row or row["status"] != "assigned" or not fixed_sales_owner(db, profile_id, customer)):
                raise DailyWorkConflict("请先确认客户固定销售，再设置临时代办", "customer_owner_scope_required")
            current_groups = [group for group in matching if group["status"] != "completed"]
            now = timestamp()
            if row:
                db.execute("""UPDATE sales_customer_owners SET owner_id=?,deputy_id=?,
                    deputy_until=?,status=?,source='customer_confirmed',version=version+1,updated_at=?
                    WHERE profile_id=? AND customer_name=?""",
                    (owner_id, deputy_id, deputy_until, "assigned" if owner_id else "unassigned",
                     now, profile_id, customer))
            else:
                db.execute("""INSERT INTO sales_customer_owners
                    (profile_id,customer_name,owner_id,deputy_id,deputy_until,status,source,updated_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (profile_id, customer, owner_id, deputy_id, deputy_until,
                     "assigned" if owner_id else "unassigned", "customer_confirmed", now))
            db.execute("""INSERT INTO sales_customer_owner_events
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, profile_id, customer, event_key, _user_id(user),
                 row["owner_id"] if row else "", owner_id, deputy_id, deputy_until, reason, now))
            sync_sales_tasks(db, profile_id, customer, owner_id, actor=_user_id(user),
                             reason=reason, key=event_key)
            for group in current_groups:
                db.execute("""UPDATE customer_order_group_aliases SET review_required=0
                    WHERE group_id=?""", (group["id"],))
                followup = db.execute("""SELECT assignee_id,customer_name,status,next_follow_up_date
                    FROM customer_order_followups WHERE group_id=?""", (group["id"],)).fetchone()
                if not followup or (followup["assignee_id"] == owner_id and
                                    followup["customer_name"] == customer):
                    continue
                db.execute("""UPDATE customer_order_followups SET assignee_id=?,customer_name=?,
                    version=version+1,updated_at=? WHERE group_id=?""",
                    (owner_id, customer, now, group["id"]))
                db.execute("""INSERT INTO customer_order_followup_events
                    VALUES (?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, group["id"],
                    "customer-owner:" + event_key, _user_id(user), reason,
                    "客户固定负责人变更:" + owner_id, followup["next_follow_up_date"], now))
            db.execute("""UPDATE daily_work_settings SET value=CAST(value AS INTEGER)+1,
                updated_at=? WHERE key='task_version'""", (now,))
            current = db.execute("""SELECT * FROM sales_customer_owners
                WHERE profile_id=? AND customer_name=?""", (profile_id, customer)).fetchone()
            return {"replayed": False, "owner": dict(current)}

    def _build(self, db):
        version_row = db.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
        version = json.loads(version_row[0]) if version_row else ""
        source_row = db.execute("SELECT value FROM settings WHERE key='data_source_updated_at'").fetchone()
        source_updated_at = json.loads(source_row[0]) if source_row else ""
        projected = db.execute("SELECT value FROM daily_work_settings WHERE key='projection_data_version'").fetchone()
        projected_at = db.execute("SELECT value FROM daily_work_settings WHERE key='projection_source_updated_at'").fetchone()
        task_version = db.execute("SELECT value FROM daily_work_settings WHERE key='task_version'").fetchone()
        current = bool(version and projected and version == projected[0] and
                       source_updated_at and projected_at and source_updated_at == projected_at[0])
        row = db.execute("SELECT payload FROM logistics_calculations WHERE data_version=? ORDER BY rowid DESC LIMIT 1",
                         (version,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='logistics_calculations'").fetchone() else None
        calculation = json.loads(row[0]) if row else {}
        alloc = defaultdict(list)
        for item in calculation.get("allocations", []):
            if isinstance(item, dict):
                demand = item.get("demand") if isinstance(item.get("demand"), dict) else {}
                number = _text(item.get("order_no") or demand.get("order"))
                if number:
                    alloc[number].append(item)
        blocked = {_text(item.get("order_no")) for item in calculation.get("blocked_orders", [])
                   if isinstance(item, dict)}
        groups, legacy_customers = supply_source_groups(
            json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE kind='orders'"))

        task_rows = db.execute("""SELECT * FROM daily_work_tasks WHERE condition_state='active'""").fetchall()
        active_sales = {row["id"] for row in db.execute("SELECT id,roles FROM staff_users WHERE active=1")
                        if "sales" in json.loads(row["roles"] or "[]")}
        task_index = defaultdict(list)
        purchase_by_order = defaultdict(list)
        print_states = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_print_states'").fetchone():
            print_states = {(row["profile_id"], row["order_no"]): row["state"] for row in
                            db.execute("SELECT profile_id,order_no,state FROM order_print_states")}
        for task in task_rows:
            order_no = _text(task["order_no"])
            if order_no:
                task_index[(task["profile_id"], order_no)].append(task)
            if task["role"] == "purchase":
                payload = json.loads(task["payload"] or "{}")
                refs = payload.get("order_nos") if isinstance(payload, dict) else None
                for ref in refs if isinstance(refs, list) else ([order_no] if order_no else []):
                    purchase_by_order[(task["profile_id"], _text(ref))].append(task)

        followups = {row["group_id"]: row for row in db.execute("SELECT * FROM customer_order_followups")}
        aliases = {(row["legacy_group_id"], row["customer_name"]): row for row in
                   db.execute("SELECT * FROM customer_order_group_aliases")}
        owner_rows = {(row["profile_id"], row["customer_name"]): row for row in
                      db.execute("SELECT * FROM sales_customer_owners")}
        today = datetime.now(SHANGHAI).date().isoformat()
        staff_names = {row["id"]: row["display_name"] for row in db.execute("SELECT id,display_name FROM staff_users")}
        results = {}
        for (profile, warehouse, po, customer_key), numbers in groups.items():
            legacy_id = _group_id(profile, warehouse, po)
            alias = aliases.get((legacy_id, customer_key))
            split = len(legacy_customers[(profile, warehouse, po)]) > 1
            identity = alias["group_id"] if alias else (
                _scoped_group_id(profile, warehouse, po, customer_key) if split else legacy_id)
            source_names = {_customer(line) for lines in numbers.values() for line in lines}
            migration_review = bool(alias["review_required"] if alias else
                                    split and legacy_id in followups)
            prior = followups.get(identity)
            identity_review = bool(prior and prior["customer_name"] and
                                   prior["customer_name"] != customer_key)
            source_customer_review = len(source_names) != 1 or "" in source_names
            conflicts = source_customer_review or migration_review or identity_review
            customer = customer_key
            all_lines = []
            missing = []
            purchased = {}
            due_days = []
            state_counts = defaultdict(int)
            owner_candidates = set()
            urgent = False
            next_follow = ""
            for number, lines in sorted(numbers.items()):
                candidate_tasks = task_index.get((profile, number), [])
                sales = [task for task in candidate_tasks if task["role"] == "sales"]
                owner_candidates.update(task["assignee_id"] for task in sales
                                        if task["assignee_id"] in active_sales)
                urgent |= any(task["urgent"] for task in candidate_tasks)
                due = min((day for day in (_due(item) for item in lines) if day), default="")
                if due:
                    due_days.append(due)
                row_state, order_missing = order_supply_state(number, lines, alloc, blocked)
                missing.extend(order_missing)
                packing = [task for task in candidate_tasks if task["role"] in {"stock_packing", "arrival_packing"}
                           and task["task_type"] == "whole_order_pack"
                           and not json.loads(task['payload'] or '{}').get('source_scope_review')]
                packed = row_state == "shipped_unposted" or bool(packing and any(
                    task["status"] in {"packed", "business_completed"} for task in packing))
                if row_state != "shipped_unposted":
                    state_counts["unshipped"] += 1
                    state_counts[row_state] += 1
                    if packed:
                        state_counts["packed"] += 1
                else:
                    state_counts["shipped_unposted"] += 1
                print_state = print_states.get((profile, number), "unprinted")
                all_lines.append({"order_no": number, "status": row_state, "packed": packed,
                                  "packing_basis": "erp" if row_state == "shipped_unposted" else "human" if packed else "",
                                  "fulfillment_label": "包装完成，依据 ERP" if row_state == "shipped_unposted" else
                                      "打印待核对" if print_state in {"reserved", "uncertain"} else
                                      "待出库" if print_state == "printed" else "",
                                  "print_state": print_state,
                                  "due_date": due, "tasks": [task["id"] for task in candidate_tasks],
                                  "skus": sorted({_text(item.get("sku")) for item in lines if _text(item.get("sku"))})})
                for task in purchase_by_order.get((profile, number), []):
                    purchased[task["id"]] = task
            followup = followups.get(identity)
            fixed = owner_rows.get((profile, customer)) if customer else None
            fixed_owner = fixed_sales_owner(db, profile, customer) if customer else ""
            deputy_until = _text(fixed["deputy_until"]) if fixed else ""
            deputy = (_text(fixed["deputy_id"]) if fixed and fixed_owner and
                      not conflicts and deputy_until >= today and fixed["deputy_id"] in active_sales else "")
            owner = fixed_owner if not conflicts else ""
            if followup:
                next_follow = _text(followup["next_follow_up_date"])
            legacy_fingerprint = hashlib.sha256(json.dumps([line["order_no"] for line in all_lines]).encode()).hexdigest()
            source_facts = {"lines": [{"order_no":number,"items":sorted([
                [_text(row.get(field)) for field in ("id","product_id","sku_id","base_unit","quantity","summary","system_note","delivery_date")]
                for row in lines])} for number,lines in sorted(numbers.items())],
                "states":[(line["order_no"],line["status"]) for line in all_lines],"missing":missing,"customers":sorted(source_names)}
            fingerprint = hashlib.sha256(json.dumps(source_facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            lines_changed = bool(followup and followup["source_fingerprint"] and
                                 followup["source_fingerprint"] not in {fingerprint, legacy_fingerprint})
            review = conflicts or state_counts["review"] > 0
            shortage_count = state_counts["procurement"] + state_counts["arrival"]
            ready_to_ship = bool(state_counts["unshipped"] and
                                 state_counts["ready"] == state_counts["unshipped"] and
                                 state_counts["packed"] == state_counts["unshipped"] and not review)
            group = {
                "id": identity, "profile_id": profile, "warehouse_id": warehouse,
                "customer_order_no": po, "customer": customer,
                "legacy_group_id": legacy_id, "migration_review": migration_review,
                "identity_review": identity_review,
                "customer_review": conflicts, "source_customer_review": source_customer_review,
                "source_customer_names": sorted(source_names),
                "owner_review": bool(not owner),
                "fixed_owner_id": fixed_owner,
                "fixed_owner_name": staff_names.get(fixed_owner, ""),
                "deputy_name": staff_names.get(deputy, ""),
                "customer_owner_status": "review" if conflicts or (fixed and fixed["owner_id"] and not fixed_owner) else fixed["status"] if fixed else "unassigned",
                "can_set_owner": not conflicts, "can_set_deputy": bool(fixed_owner and not conflicts),
                "deputy_id": deputy,
                "deputy_until": deputy_until, "customer_owner_version": fixed["version"] if fixed else 0,
                "lines_changed": lines_changed,
                "source_fingerprint": fingerprint,
                "assignee_id": owner, "follow_up_version": followup["version"] if followup else 0,
                "next_follow_up_date": next_follow,
                "follow_up_status": followup["status"] if followup else "todo",
                "due_date_from": min(due_days) if due_days else "",
                "due_date_to": max(due_days) if due_days else "",
                "order_count": len(numbers), "unshipped_count": state_counts["unshipped"],
                "ready_count": state_counts["ready"], "packed_count": state_counts["packed"],
                "procurement_count": state_counts["procurement"],
                "arrival_count": state_counts["arrival"], "review_count": state_counts["review"],
                "shipped_unposted_count": state_counts["shipped_unposted"],
                "missing_count": shortage_count, "missing_product_count": len({
                    (item["product_id"], item["sku_id"], item["base_unit"]) for item in missing}),
                "ready_to_ship": ready_to_ship,
                "urgent": urgent, "missing_preview": missing[:2],
                "needs_follow_up": bool(shortage_count or review or ready_to_ship or lines_changed),
                "data_version": version, "task_version": int(task_version[0]) if task_version else 0,
                "tasks_current": current,
                "lines": all_lines, "missing_items": missing,
                "purchase_tasks": [{"id": task["id"], "title": task["title"],
                                    "sku": task["sku"], "assignee_id": task["assignee_id"],
                                    "status": task["status"]} for task in purchased.values()],
            }
            group["status"] = ("completed" if not state_counts["unshipped"] else "review" if review else "ready_to_ship" if ready_to_ship else
                               "shortage" if shortage_count else "packing")
            group["priority"] = (0 if urgent or lines_changed else 1 if group["due_date_from"] and
                                 group["due_date_from"] <= today else 2 if group["due_date_from"] else 3)
            results[identity] = group
        return results, version, source_updated_at, current, int(task_version[0]) if task_version else 0

    def list(self, user, role="", view="mine", status="all", customer="", keyword="", page=1):
        if view == "role" and not role:
            raise ValueError("本岗位视图必须指定岗位")
        if role and role not in {"sales", "purchase", "stock_packing", "arrival_packing"}:
            raise ValueError("岗位无效")
        if view not in {"mine", "role", "all"} or view == "all" and not _is_admin(user):
            raise DailyWorkConflict("没有查看全部任务的权限", "role_forbidden")
        if role and role not in _roles(user) and not _is_admin(user):
            raise DailyWorkConflict("账号没有该岗位权限", "role_forbidden")
        if status not in {"all", "shortage", "packing", "ready_to_ship", "review"}:
            raise ValueError("筛选状态无效")
        if not isinstance(page, int) or page < 1:
            raise ValueError("页码必须为正整数")
        with self.store.connect() as db:
            groups, version, updated, current, task_version = self._build(db)
            task_by_group = defaultdict(list)
            seen_task_groups = set()
            for row in db.execute("SELECT id,role,assignee_id,profile_id,order_no,status,task_type,payload FROM daily_work_tasks WHERE condition_state='active'"):
                payload = json.loads(row["payload"] or "{}")
                refs = payload.get("order_nos") if isinstance(payload, dict) else None
                order_nos = refs if isinstance(refs, list) else ([row["order_no"]] if row["order_no"] else [])
                for number in set(_text(value) for value in order_nos if _text(value)):
                    for group in groups_for_order(groups, row["profile_id"], number):
                        task_key = (group["id"], row["id"])
                        if task_key not in seen_task_groups:
                            task_by_group[group["id"]].append(dict(row))
                            seen_task_groups.add(task_key)
            staff = {row["id"]: row["display_name"] for row in db.execute("SELECT id,display_name FROM staff_users")}
            today = datetime.now(SHANGHAI).date().isoformat()
            items = []
            for group in groups.values():
                tasks = task_by_group[group["id"]]
                if role == "purchase" and not any(item["role"] == "purchase" for item in tasks) and not group["missing_count"]:
                    continue
                if role in {"stock_packing", "arrival_packing"} and not any(item["role"] == role for item in tasks):
                    continue
                if view == "mine":
                    actor = _user_id(user)
                    if role == "sales" and actor not in {group["assignee_id"], group["deputy_id"]}:
                        continue
                    if role in {"purchase", "stock_packing", "arrival_packing"} and not any(
                        item["assignee_id"] == actor and item["role"] == role for item in tasks):
                        continue
                    if not role and actor not in {group["assignee_id"], group["deputy_id"]} and not any(
                        item["assignee_id"] == actor and item["role"] != "sales" for item in tasks):
                        continue
                if customer and group["customer"] != customer:
                    continue
                if keyword and keyword.casefold() not in group["customer_order_no"].casefold() and keyword.casefold() not in group["customer"].casefold():
                    continue
                copy = {key: value for key, value in group.items() if key not in {"lines", "missing_items", "purchase_tasks"}}
                copy["assignee_name"] = staff.get(copy["assignee_id"], "")
                copy["my_packing_count"] = sum(item["assignee_id"] == _user_id(user) and
                    item["role"] in {"stock_packing", "arrival_packing"} and
                    item["task_type"] == "whole_order_pack" for item in tasks)
                copy["purchase_task_count"] = len(group["purchase_tasks"])
                items.append(copy)
            # Keep simple overview counts independent of the selected quick filter.
            counts = {key: sum(item["status"] == key for item in items) for key in
                      ("shortage", "packing", "ready_to_ship", "review")}
            if status != "all":
                items = [item for item in items if item["status"] == status]
            items.sort(key=lambda item: (item["priority"], item["due_date_from"] or "9999-12-31",
                                         item["missing_count"], -item["ready_count"], item["customer_order_no"]))
            total = len(items)
            start = (page - 1) * 20
            return {"items": items[start:start + 20], "total": total, "page": page,
                    "page_size": 20, "has_more": start + 20 < total, "counts": counts,
                    "data_version": version, "source_updated_at": updated,
                    "task_version": task_version, "tasks_current": current}

    def detail(self, user, identity, role="", view="mine"):
        if view == "role" and not role:
            raise ValueError("本岗位视图必须指定岗位")
        if role and role not in {"sales", "purchase", "stock_packing", "arrival_packing"}:
            raise ValueError("岗位无效")
        if view not in {"mine", "role", "all"} or view == "all" and not _is_admin(user):
            raise DailyWorkConflict("没有查看全部任务的权限", "role_forbidden")
        if role and role not in _roles(user) and not _is_admin(user):
            raise DailyWorkConflict("账号没有该岗位权限", "role_forbidden")
        with self.store.connect() as db:
            groups, version, updated, current, task_version = self._build(db)
            item = groups.get(identity)
            if not item:
                raise KeyError("客户订单不存在")
            if view == "mine" and not _is_admin(user):
                actor = _user_id(user)
                task_ids = [task_id for line in item["lines"] for task_id in line["tasks"]]
                owned = db.execute("SELECT 1 FROM daily_work_tasks WHERE id IN (%s) AND assignee_id=? AND role<>'sales' LIMIT 1" %
                    ",".join("?" for _ in task_ids), [*task_ids, actor]).fetchone() if task_ids else None
                if actor not in {item["assignee_id"], item["deputy_id"]} and not owned:
                    raise DailyWorkConflict("客户订单未分配给当前员工", "task_owner_conflict")
            staff = {row["id"]: row["display_name"] for row in db.execute("SELECT id,display_name FROM staff_users")}
            item["assignee_name"] = staff.get(item["assignee_id"], "")
            item["purchase_tasks"] = [{**row, "assignee_name": staff.get(row["assignee_id"], "")}
                                      for row in item["purchase_tasks"]]
            events = db.execute("SELECT * FROM customer_order_followup_events WHERE group_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (identity,)).fetchall()
            item["followups"] = [dict(row) for row in events]
            return {"group": item, "data_version": version, "source_updated_at": updated,
                    "task_version": task_version, "tasks_current": current}

    def followup(self, user, identity, key, version, note, waiting_for="", next_date="", db=None):
        if "sales" not in _roles(user) and not _is_admin(user):
            raise DailyWorkConflict("账号没有销售岗位权限", "role_forbidden")
        if not _text(key) or len(_text(key)) > 128 or not _text(note) or len(_text(note)) > 1000:
            raise ValueError("请填写处理结果和有效幂等标识")
        if len(_text(waiting_for)) > 200:
            raise ValueError("等待对象不能超过 200 字")
        try:
            expected = int(version)
        except (TypeError, ValueError):
            raise ValueError("请提供跟进版本号") from None
        today = datetime.now(SHANGHAI).date().isoformat()
        follow_date = _day(next_date) or _next_workday(today)
        with (self.store.connect(write=True) if db is None else nullcontext(db)) as db:
            existing = db.execute("SELECT * FROM customer_order_followup_events WHERE group_id=? AND idempotency_key=?", (identity, key)).fetchone()
            if existing:
                if existing["note"] != _text(note) or existing["waiting_for"] != _text(waiting_for) or (next_date and existing["next_follow_up_date"] != follow_date):
                    raise DailyWorkConflict("幂等标识已用于其他处理记录", "idempotency_conflict")
                return {"replayed": True, "version": db.execute("SELECT version FROM customer_order_followups WHERE group_id=?", (identity,)).fetchone()[0]}
            groups, data_version, _, current, _ = self._build(db)
            if not current:
                raise DailyWorkConflict("整单汇总未跟上当前订单，请更新后重试", "task_version_changed")
            group = groups.get(identity)
            if not group:
                raise KeyError("客户订单不存在")
            if group["identity_review"]:
                raise DailyWorkConflict("订单客户身份待核对，原跟进记录不能用于新客户", "customer_identity_review")
            if (group["customer_review"] or group["owner_review"]) and not _is_admin(user):
                raise DailyWorkConflict("客户或负责人归属待核对，请由管理员先确认", "customer_identity_review")
            row = db.execute("SELECT * FROM customer_order_followups WHERE group_id=?", (identity,)).fetchone()
            if expected != (row["version"] if row else 0):
                raise DailyWorkConflict("跟进记录已更新，请刷新后重试", "task_version_changed")
            owner = group["assignee_id"]
            if not owner or (_user_id(user) not in {owner, group["deputy_id"]} and not _is_admin(user)):
                raise DailyWorkConflict("请先由管理员确定整单销售负责人", "task_owner_conflict")
            now = timestamp()
            fingerprint = group["source_fingerprint"]
            if row:
                db.execute("""UPDATE customer_order_followups SET status=?,next_follow_up_date=?,version=version+1,
                    source_data_version=?,source_fingerprint=?,updated_at=? WHERE group_id=?""",
                    ("waiting" if waiting_for else "in_progress", follow_date, data_version, fingerprint, now, identity))
            else:
                db.execute("""INSERT INTO customer_order_followups
                    (group_id,profile_id,warehouse_id,customer_order_no,assignee_id,status,
                     next_follow_up_date,version,source_data_version,source_fingerprint,
                     created_at,updated_at,customer_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (identity, group["profile_id"], group["warehouse_id"], group["customer_order_no"],
                     owner, "waiting" if waiting_for else "in_progress", follow_date, 1, data_version,
                     fingerprint, now, now, group["customer"]))
            db.execute("""INSERT INTO customer_order_followup_events VALUES (?,?,?,?,?,?,?,?)""",
                       (uuid.uuid4().hex, identity, key, _user_id(user), _text(note), _text(waiting_for), follow_date, now))
            db.execute("UPDATE daily_work_settings SET value=CAST(value AS INTEGER)+1,updated_at=? WHERE key='task_version'", (now,))
            return {"replayed": False, "version": expected + 1}

    def customer_owners(self, user, profile_id=""):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以查看客户负责人配置", "admin_required")
        with self.store.connect() as db:
            rows = db.execute("""SELECT * FROM sales_customer_owners
                WHERE (?='' OR profile_id=?) ORDER BY customer_name,profile_id""",
                (_text(profile_id), _text(profile_id))).fetchall()
            groups = self._build(db)[0]
            names = {row["id"]: row["display_name"] for row in db.execute("SELECT id,display_name FROM staff_users")}
            items = []
            for row in rows:
                relevant = [group for group in groups.values() if group["profile_id"] == row["profile_id"] and
                            (group["customer"] == row["customer_name"] or row["customer_name"] in group["source_customer_names"])]
                identity_review = any(group["customer_review"] for group in relevant)
                reasons = []
                if any(group["identity_review"] for group in relevant):
                    reasons.append("客户名称与旧跟进记录不一致，需先核实客户身份")
                if any(group["migration_review"] for group in relevant):
                    reasons.append("旧订单关联多个客户，需先核实历史对应关系")
                if any(group["source_customer_review"] for group in relevant):
                    reasons.append("ERP 客户来源缺失或存在冲突")
                owner = fixed_sales_owner(db, row["profile_id"], row["customer_name"])
                if row["source"] == "phone_rule_scope_review":
                    reasons.append("旧规则未区分账套，同名客户跨账套，需分别确认")
                if not owner:
                    reasons.append("请确认客户固定销售，不使用旧任务负责人推断")
                conflicts = []
                for group in relevant:
                    if not group["customer_review"]:
                        continue
                    source_names = sorted({name for sibling in groups.values()
                                    if sibling["legacy_group_id"] == group["legacy_group_id"]
                                    for name in sibling["source_customer_names"]})
                    prior = db.execute("SELECT customer_name FROM customer_order_followups WHERE group_id=?",
                                       (group["legacy_group_id"],)).fetchone()
                    conflicts.append({"group_id": group["id"], "customer_order_no": group["customer_order_no"],
                        "source_names": source_names, "previous_customer_name": prior[0] if prior else "",
                        "identity_review": group["identity_review"], "migration_review": group["migration_review"]})
                value = dict(row)
                value.update(identity_conflicts=conflicts, owner_name=names.get(row["owner_id"], ""),
                    deputy_name=names.get(row["deputy_id"], ""), review_reasons=reasons,
                    can_set_owner=not identity_review, can_set_deputy=bool(owner and not identity_review),
                    active_task_count=db.execute("""SELECT count(*) FROM daily_work_tasks
                        WHERE profile_id=? AND customer=? AND role='sales' AND condition_state='active'
                        AND status IN ('todo','in_progress','waiting')""",
                        (row["profile_id"], row["customer_name"])).fetchone()[0],
                    active_followup_count=sum(group["status"] != "completed" for group in relevant))
                if identity_review:
                    value["status"] = "review"
                elif row["owner_id"] and not owner:
                    value["status"] = "review"
                items.append(value)
            return {"items": items}

    def history(self, user, view="mine", scope="processed", page=1):
        if view not in {"mine", "role", "all"} or (view == "all" and not _is_admin(user)):
            raise DailyWorkConflict("没有查看历史事项的权限", "role_forbidden")
        if view == "role" and "sales" not in _roles(user) and not _is_admin(user):
            raise DailyWorkConflict("账号没有销售岗位权限", "role_forbidden")
        if scope not in {"processed", "archived", "all"}:
            raise ValueError("历史范围无效")
        if not isinstance(page, int) or page < 1:
            raise ValueError("页码必须为正整数")
        actor = _user_id(user)
        with self.store.connect() as db:
            current_groups = self._build(db)[0]
            rows = []
            for row in db.execute("""SELECT f.*,
                    (SELECT e.note FROM customer_order_followup_events e
                     WHERE e.group_id=f.group_id ORDER BY e.created_at DESC,e.rowid DESC LIMIT 1)
                     AS latest_note,
                    (SELECT COUNT(*) FROM customer_order_followup_events e
                     WHERE e.group_id=f.group_id) AS event_count
                    FROM customer_order_followups f ORDER BY f.updated_at DESC,f.group_id DESC"""):
                current = current_groups.get(row["group_id"])
                source_present = bool(current and not current["identity_review"])
                work_count=db.execute('SELECT count(*) FROM customer_order_work_actions WHERE group_id=?',(row['group_id'],)).fetchone()[0]
                if scope == "processed" and row["status"] == "todo" and not row["event_count"] and not work_count:
                    continue
                if scope == "archived" and source_present:
                    continue
                if view == "mine" and not (_is_admin(user) or row["assignee_id"] == actor or
                    db.execute("""SELECT 1 FROM sales_customer_owners WHERE profile_id=?
                        AND customer_name=? AND deputy_id=? AND deputy_until>=? LIMIT 1""",
                        (row["profile_id"], row["customer_name"], actor,
                         datetime.now(SHANGHAI).date().isoformat())).fetchone() or
                    db.execute("""SELECT 1 FROM customer_order_followup_events
                        WHERE group_id=? AND actor_id=? LIMIT 1""",
                        (row["group_id"], actor)).fetchone() or
                    db.execute('SELECT 1 FROM customer_order_work_actions WHERE group_id=? AND actor_id=? LIMIT 1',
                               (row['group_id'],actor)).fetchone()):
                    continue
                value = dict(row)
                value['event_count']+=work_count
                from .task_board import TaskBoard
                notes=TaskBoard._sales_notes(db,row['group_id'])
                value['latest_note']=notes[0]['note'] if notes else ''
                value["source_present"] = source_present
                value["review_required"] = bool(db.execute("""SELECT 1
                    FROM customer_order_group_aliases WHERE legacy_group_id=?
                    AND review_required=1 LIMIT 1""", (row["group_id"],)).fetchone())
                rows.append(value)
            start = (page - 1) * 30
            return {"items": rows[start:start + 30], "total": len(rows), "page": page,
                    "page_size": 30, "has_more": start + 30 < len(rows)}

    def history_detail(self, user, identity, view="mine", page=1):
        if not isinstance(page, int) or page < 1:
            raise ValueError("页码必须为正整数")
        if view not in {"mine", "role", "all"} or (view == "all" and not _is_admin(user)):
            raise DailyWorkConflict("没有查看历史事项的权限", "role_forbidden")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM customer_order_followups WHERE group_id=?", (identity,)).fetchone()
            if not row:
                raise KeyError("历史事项不存在")
            actor = _user_id(user)
            if view == "mine" and not (_is_admin(user) or row["assignee_id"] == actor or
                db.execute("""SELECT 1 FROM sales_customer_owners WHERE profile_id=?
                    AND customer_name=? AND deputy_id=? AND deputy_until>=? LIMIT 1""",
                    (row["profile_id"], row["customer_name"], actor,
                     datetime.now(SHANGHAI).date().isoformat())).fetchone() or
                db.execute("""SELECT 1 FROM customer_order_followup_events
                    WHERE group_id=? AND actor_id=? LIMIT 1""", (identity, actor)).fetchone() or
                db.execute('SELECT 1 FROM customer_order_work_actions WHERE group_id=? AND actor_id=? LIMIT 1',
                           (identity,actor)).fetchone()):
                raise DailyWorkConflict("没有查看历史事项的权限", "role_forbidden")
            if view == "role" and "sales" not in _roles(user) and not _is_admin(user):
                raise DailyWorkConflict("账号没有销售岗位权限", "role_forbidden")
            start = (page - 1) * 100
            events = [{**dict(event),'event_type':'followup','_source':1} for event in db.execute("SELECT *,rowid AS _rowid FROM customer_order_followup_events WHERE group_id=?",(identity,))]
            events += [{'id':event['id'],'group_id':identity,'idempotency_key':event['idempotency_key'],
                'actor_id':event['actor_id'],'event_type':event['action'],'note':event['note'],
                'waiting_for':'','next_follow_up_date':'','created_at':event['created_at'],
                '_rowid':event['_rowid'],'_source':2} for event in db.execute('SELECT *,rowid AS _rowid FROM customer_order_work_actions WHERE group_id=?',(identity,))]
            events.sort(key=lambda event:(event['created_at'],event['_source'],event['_rowid']),reverse=True)
            return {"item": dict(row), "events": [{key:value for key,value in event.items() if not key.startswith('_')} for event in events[start:start+100]],
                    "page": page, "has_more": len(events) > start+100,
                    "source_present": bool((current := self._build(db)[0].get(identity)) and
                                           not current["identity_review"])}


def customer_order_routes(engine):
    bp = Blueprint("customer_orders", __name__)

    @bp.get("/api/v2/daily-work/groups")
    def groups():
        try:
            return jsonify(ok=True, **engine.list(g.current_user, request.args.get("role", ""),
                request.args.get("view", "mine"), request.args.get("status", "all"),
                request.args.get("customer", ""), request.args.get("keyword", ""),
                int(request.args.get("page", "1"))))
        except (ValueError, TypeError) as error:
            return jsonify(ok=False, error="invalid_request", message=str(error)), 400

    @bp.get("/api/v2/daily-work/groups/<identity>")
    def detail(identity):
        return jsonify(ok=True, **engine.detail(g.current_user, identity,
            request.args.get("role", ""), request.args.get("view", "mine")))

    @bp.get("/api/v2/daily-work/groups/history")
    def history():
        return jsonify(ok=True, **engine.history(g.current_user,
            request.args.get("view", "mine"), request.args.get("scope", "processed"),
            int(request.args.get("page", "1"))))

    @bp.get("/api/v2/daily-work/groups/history/<identity>")
    def history_detail(identity):
        return jsonify(ok=True, **engine.history_detail(g.current_user, identity,
            request.args.get("view", "mine"), int(request.args.get("page", "1"))))

    @bp.get("/api/v2/daily-work/customer-owners")
    def customer_owners():
        return jsonify(ok=True, **engine.customer_owners(g.current_user,
            request.args.get("profile_id", "")))

    @bp.put("/api/v2/daily-work/customer-owners")
    def set_customer_owner():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("请求必须为 JSON 对象")
        if body.get("scope") != "customer" or "version" not in body:
            raise DailyWorkConflict("请升级客户端并确认影响整个客户的未完成订单", "client_upgrade_required")
        return jsonify(ok=True, **engine.set_customer_owner(g.current_user,
            body.get("profile_id"), body.get("customer"), body.get("owner_id"),
            body.get("deputy_id"), body.get("deputy_until"), body.get("version"),
            body.get("idempotency_key"), body.get("reason")))

    @bp.put("/api/v2/daily-work/customer-deputies")
    def set_customer_deputy():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("请求必须为 JSON 对象")
        if body.get("scope") != "customer" or "version" not in body:
            raise DailyWorkConflict("请升级客户端并确认客户临时代办范围", "client_upgrade_required")
        return jsonify(ok=True, **engine.set_customer_owner(g.current_user,
            body.get("profile_id"), body.get("customer"), None,
            body.get("deputy_id"), body.get("deputy_until"), body.get("version"),
            body.get("idempotency_key"), body.get("reason"), _mode="deputy"))

    @bp.post("/api/v2/daily-work/groups/<identity>/followups")
    def followup(identity):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("请求必须为 JSON 对象")
        return jsonify(ok=True, **engine.followup(g.current_user, identity,
            body.get("idempotency_key"), body.get("version"), body.get("note"),
            body.get("waiting_for", ""), body.get("next_follow_up_date", "")))

    @bp.post("/api/v2/daily-work/groups/<identity>/transfer")
    def transfer(identity):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("请求必须为 JSON 对象")
        return jsonify(ok=True, **engine.transfer(g.current_user, identity,
            body.get("assignee_id"), body.get("idempotency_key"), body.get("version"), body.get("reason")))

    return bp
