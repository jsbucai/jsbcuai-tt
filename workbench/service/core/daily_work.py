"""Persistent role-based daily work derived from one published ERP generation.

The module owns its schema and exposes a Blueprint so it can be registered by
``create_app`` without making the task engine depend on the application module.
ERP data is read-only here: refresh only reconciles local task state.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Blueprint, g, jsonify, request

from .order_print_state import (invalidate_changed_print_state, natural_location_key,
                                order_fingerprint)
from .print_jobs import print_readiness
from .rules import note_delivery_date
from .store import dumps, timestamp


SHANGHAI = ZoneInfo("Asia/Shanghai")
ROLES = ("sales", "purchase", "stock_packing", "arrival_packing")
PACKING_ROLES = frozenset({"stock_packing", "arrival_packing"})
STOCK_CATEGORIES = frozenset({"LS库存件", "LD库存件", "LM库存件", "DQ库存件", "QT库存件", "库存件组合"})
PRINT_REVIEW_BLOCK = "打印结果无法确认，需人工核对"
ARRIVAL_SHORTAGE_TASK = "arrival_shortage_waiting"
SHORTAGE_REVIEW_BLOCK = "订单重新出现缺项，已暂停包装；等待采购补齐并核对已拣或已包实物"
SHORTAGE_READY_REVIEW_BLOCK = "缺项已经补齐，请核对先前已拣或已包实物后继续包装"
LABEL_REVIEW_BLOCK = "订单标签内容已变化，需重新核对包装结果"
ACTION_STATUSES = {
    "start": "in_progress",
    "progress": "in_progress",
    "wait": "waiting",
    "resolve": "resolved",
    "packed": "packed",
    "reopen": "todo",
}


class DailyWorkConflict(Exception):
    def __init__(self, message, code="daily_work_conflict"):
        super().__init__(message)
        self.code = code


def _text(value):
    return "" if value is None else str(value).strip()


def _flag(value):
    return value is True or value == 1 or _text(value).lower() in {"1", "true", "yes", "on"}


def _json(value, default=None):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _decimal(value):
    try:
        return Decimal(_text(value) or "0")
    except InvalidOperation:
        return Decimal(0)


def _day(value):
    raw = _text(value)[:10]
    try:
        return date.fromisoformat(raw).isoformat() if raw else ""
    except ValueError:
        return ""


def _business_date(now=None):
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    return now.astimezone(SHANGHAI).date().isoformat()


def _next_workday(today):
    value = date.fromisoformat(today) + timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value.isoformat()


def _source_is_today(value, business_date):
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.date().isoformat() == business_date
        return parsed.astimezone(SHANGHAI).date().isoformat() == business_date
    except ValueError:
        return False


def _packing_limit(value, default):
    """Use the configured limit verbatim, including an intentional zero."""
    raw = _text(value)
    return int(raw) if raw else int(default)


def _setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    value = _json(row[0], row[0])
    return value if value is not None else default


def _roles(user):
    value = user.get("roles", []) if isinstance(user, dict) else []
    if isinstance(value, str):
        parsed = _json(value)
        value = parsed if isinstance(parsed, list) else re.split(r"[,\s]+", value)
    return [role for role in value if role in ROLES]


def _user_id(user):
    return _text(user.get("id")) if isinstance(user, dict) else ""


def _is_admin(user):
    return bool(isinstance(user, dict) and _flag(user.get("is_admin")))


def _hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _print_state_version(db):
    """Cheap immutable token for print-driven paging, independent of task jobs."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_states'").fetchone():
        return _hash([])
    return _hash([dict(row) for row in db.execute(
        'SELECT * FROM order_print_states ORDER BY profile_id,order_no')])


def _location_from_inventory(inventory):
    index = {}
    for row in inventory:
        keys = {
            _text(row.get("sku")).upper(),
            _text(row.get("product_id")),
            _text(row.get("sku_id")),
        } - {""}
        location = _text(row.get("location") or row.get("position") or row.get("storage_location") or
                         row.get("货位") or row.get("库位"))
        for key in keys:
            if key and (key not in index or (not index[key] and location)):
                index[key] = location
    return index


def _in_unfulfilled_scope(item):
    """Shadow demand reserves stock but is not a current App order.

    Only inspect published facts: an unavailable refresh never replaces the
    last complete snapshot. Review flags on an actual unfulfilled row remain
    visible; they are not evidence that an order left the source.
    """
    return (not _flag(item.get('shadow_demand')) and
            _text(item.get('process_state')) not in {'500', '600'} and
            _text(item.get('lifecycle_state')) not in {'shipped_unposted', 'archived'})


def _current_task(task, source):
    payload = task['payload']
    if not isinstance(payload, dict):
        payload = _json(payload, {})
    numbers = payload.get('order_nos', [task['order_no']]) if task['role'] == 'purchase' else [task['order_no']]
    return any((task['profile_id'], _text(number)) in source for number in numbers)


def _published_order_groups(db, current_only=False):
    groups = defaultdict(list)
    for row in db.execute("SELECT payload FROM records WHERE kind='orders'"):
        item = _json(row["payload"], {})
        if not isinstance(item, dict) or current_only and not _in_unfulfilled_scope(item):
            continue
        number = _text(item.get("order_no"))
        if number:
            groups[(_text(item.get("profile_id")), number)].append(item)
    return groups


def _delivery_date(item):
    direct = _day(item.get("delivery_date"))
    if direct:
        return direct
    try:
        return note_delivery_date(item.get("summary") or item.get("system_note") or "") or ""
    except ValueError:
        return ""


def _record_identity(item):
    values = tuple(_text(item.get(name)) for name in
                   ("profile_id", "warehouse_id", "product_id", "sku_id", "base_unit"))
    if all(values):
        return "|".join(values)
    return "sku:" + _text(item.get("sku")).upper()


def _legacy_purchase_identity(task):
    value = _text(task["business_key"]).partition("allocation:")[2]
    parts = value.split("|")
    warehouse = _json(parts[1], None) if len(parts) >= 3 else None
    if isinstance(warehouse, list) and len(warehouse) >= 4:
        return "|".join((_text(warehouse[0]),_text(warehouse[1]),_text(parts[2]),_text(warehouse[2]),_text(warehouse[3])))
    return ""


def _task_has_work_history(db, task):
    if db.execute("""SELECT 1 FROM daily_work_events WHERE task_id=? AND
        event_type NOT IN ('created','source_updated','auto_assigned','product_regrouped','assignment_released') LIMIT 1""", (task['id'],)).fetchone():
        return True
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_audit'").fetchone():
        return bool(db.execute("SELECT 1 FROM order_print_audit WHERE profile_id=? AND order_no=? LIMIT 1", (task['profile_id'],task['order_no'])).fetchone())
    return False


def _allocation_record(lines, allocation):
    """Resolve a classifier allocation back to one published order line.

    The classifier's ``demand.sku`` is the stable ERP product identity in
    production snapshots, while an order's ``sku`` is the visible FB/LS code.
    Older fixtures used the visible code in both places, so match all stable
    identities and use ``product_code`` as the display-code fallback.
    """
    demand = allocation.get("demand") if isinstance(allocation.get("demand"), dict) else {}
    line_id = _text(allocation.get("line_id") or demand.get("line_id"))
    warehouse = _text(demand.get("warehouse") or allocation.get("warehouse"))
    stable = _text(demand.get("sku") or allocation.get("sku"))
    product_code = _text(demand.get("product_code") or allocation.get("product_code")).upper()
    source_values = set()
    source_rows = demand.get("source_rows")
    if not isinstance(source_rows, list):
        source_rows = allocation.get("source_rows")
    if isinstance(source_rows, list):
        for source in source_rows:
            if isinstance(source, dict):
                source_values.update(_text(source.get(name)) for name in
                                     ("id", "line_id", "record_id", "sku", "product_id", "sku_id"))
            else:
                source_values.add(_text(source))
        source_values.discard("")

    def warehouse_ok(item):
        parts = _json(warehouse, None)
        if isinstance(parts, list) and len(parts) >= 4:
            return tuple(_text(value) for value in parts[:4]) == tuple(
                _text(item.get(name)) for name in ("profile_id", "warehouse_id", "sku_id", "base_unit"))
        return not warehouse or warehouse == _text(item.get("warehouse_id") or item.get("warehouse"))

    if line_id:
        exact = next((item for item in lines if warehouse_ok(item) and
                      line_id in {_text(item.get("line_id")), _text(item.get("id"))}), None)
        if exact:
            return exact
    if source_values:
        exact = next((item for item in lines if warehouse_ok(item) and source_values.intersection({
            _text(item.get("id")), _text(item.get("line_id")), _text(item.get("product_id")),
            _text(item.get("sku_id")), _text(item.get("sku")),
        })), None)
        if exact:
            return exact
    if stable:
        exact = next((item for item in lines if warehouse_ok(item) and stable in {
            _text(item.get("product_id")), _text(item.get("sku_id")), _text(item.get("sku")),
        }), None)
        if exact:
            return exact
    if product_code:
        exact = next((item for item in lines if warehouse_ok(item) and
                      product_code == _text(item.get("sku")).upper()), None)
        if exact:
            return exact
    return None


def _allocation_is_urging(allocation):
    if _text(allocation.get("category") or allocation.get("order_category")) == "催货":
        return True
    value = allocation.get("urging")
    if _flag(value):
        return True
    return _decimal(value) > 0


def _customer(item):
    if _text(item.get("customer_source")) != "wms_receiveraddress":
        return ""
    return _text(item.get("customer_name") or item.get("buyer") or item.get("receiveraddress"))


def _lifecycle_active(lines):
    for item in lines:
        if (_flag(item.get("shadow_demand")) or _flag(item.get("lifecycle_print_blocked")) or
                _text(item.get("lifecycle_state")) in {"candidate", "shipped_unposted", "archived"} or
                _text(item.get("process_state")) in {"500", "600"}):
            return False
    return True


def _lifecycle_review_reason(lines):
    """Explain why current source evidence must pause packing actions."""
    if any(_flag(item.get("shadow_demand")) for item in lines):
        return "订单已进入出库待结存处理，需要先核对 ERP 状态"
    if any(_flag(item.get("lifecycle_pending_review")) for item in lines):
        return "ERP 发货状态正在连续确认，暂停包装确认"
    if any(_flag(item.get("lifecycle_print_blocked")) for item in lines):
        return "ERP 生命周期已阻止打印和包装确认"
    if any(_text(item.get("lifecycle_state")) in {"candidate", "shipped_unposted", "archived"}
           or _text(item.get("process_state")) in {"500", "600"} for item in lines):
        return "ERP 显示订单可能已发货或待结存，需要先核对"
    return ""


def _erp_completion(lines):
    """Return confirmed whole-order lifecycle evidence, never a raw WMS guess."""
    if not lines or any(_flag(item.get("lifecycle_pending_review")) or
                        _flag(item.get('lifecycle_release_pending')) or
                        _text(item.get("last_error")) for item in lines):
        return None
    states = {_text(item.get("lifecycle_state")) for item in lines}
    processes = {_text(item.get("process_state")) for item in lines}
    if (len(states) != 1 or next(iter(states)) not in {"shipped_unposted", "archived"} or
            len(processes) != 1 or next(iter(processes)) not in {"500", "600"}):
        return None
    confirmations=min(int(item.get('lifecycle_confirmations') or item.get('confirmation_count') or 0) for item in lines)
    if confirmations<2:return None
    return {"lifecycle_state": next(iter(states)), "process_state": next(iter(processes)),
            "confirmation_count": confirmations}


def _printable(lines):
    for item in lines:
        value = dict(item)
        value.setdefault("system_note", value.get("summary", ""))
        ready, _ = print_readiness("order", value)
        if not ready:
            return False
    return bool(lines)


def _current_user():
    value = getattr(g, "current_user", None)
    if not isinstance(value, dict):
        raise DailyWorkConflict("登录账号未绑定员工身份", "staff_identity_required")
    return value


def ensure_schema(store):
    """Install additive task schema without changing Store's global user_version."""
    with store.connect(write=True) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_work_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_work_tasks(
                id TEXT PRIMARY KEY,
                business_key TEXT NOT NULL,
                cycle INTEGER NOT NULL DEFAULT 1,
                role TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'todo',
                condition_state TEXT NOT NULL DEFAULT 'active',
                assignee_id TEXT NOT NULL DEFAULT '',
                profile_id TEXT NOT NULL DEFAULT '',
                order_no TEXT NOT NULL DEFAULT '',
                customer TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL DEFAULT '',
                product_id TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                source_category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                next_action TEXT NOT NULL DEFAULT '',
                due_date TEXT NOT NULL DEFAULT '',
                next_follow_up_date TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 50,
                urgent INTEGER NOT NULL DEFAULT 0,
                source_data_version TEXT NOT NULL DEFAULT '',
                source_updated_at TEXT NOT NULL DEFAULT '',
                source_fingerprint TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                condition_resolved_at TEXT NOT NULL DEFAULT '',
                human_completed_at TEXT NOT NULL DEFAULT '',
                business_completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(business_key,cycle));
            CREATE INDEX IF NOT EXISTS daily_work_role_status
                ON daily_work_tasks(role,condition_state,status,priority,due_date);
            CREATE INDEX IF NOT EXISTS daily_work_assignee
                ON daily_work_tasks(assignee_id,condition_state,status);
            CREATE INDEX IF NOT EXISTS daily_work_order
                ON daily_work_tasks(profile_id,order_no,role);
            CREATE TABLE IF NOT EXISTS daily_work_events(
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',
                from_status TEXT NOT NULL DEFAULT '',
                to_status TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                next_follow_up_date TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT,
                task_version INTEGER NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES daily_work_tasks(id),
                UNIQUE(task_id,idempotency_key));
            CREATE INDEX IF NOT EXISTS daily_work_event_created
                ON daily_work_events(created_at,task_id);
            CREATE TABLE IF NOT EXISTS daily_work_assignment_rules(
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                match_type TEXT NOT NULL,
                match_value TEXT NOT NULL,
                staff_id TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(role,match_type,match_value));
            CREATE TABLE IF NOT EXISTS daily_work_settings(
                key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_work_refreshes(
                idempotency_key TEXT PRIMARY KEY,data_version TEXT NOT NULL,
                result TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_work_attendance(
                business_date TEXT NOT NULL,user_id TEXT NOT NULL,on_duty INTEGER NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,
                PRIMARY KEY(business_date,user_id));
            CREATE TABLE IF NOT EXISTS customer_order_work_actions(
                id TEXT PRIMARY KEY,group_id TEXT NOT NULL,actor_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,
                action TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',result TEXT NOT NULL,
                created_at TEXT NOT NULL,UNIQUE(actor_id,idempotency_key));
            CREATE INDEX IF NOT EXISTS customer_order_work_action_group
                ON customer_order_work_actions(group_id,created_at);
            CREATE TABLE IF NOT EXISTS customer_order_board_snapshots(
                group_id TEXT PRIMARY KEY,payload TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL);
        """)
        db.execute("""INSERT INTO daily_work_schema VALUES (1,3)
            ON CONFLICT(singleton) DO UPDATE SET version=MAX(version,excluded.version)""")
        db.execute("""INSERT OR IGNORE INTO daily_work_settings VALUES
            ('packing_limit_default','3',?)""", (timestamp(),))
        db.execute("""INSERT OR IGNORE INTO daily_work_settings VALUES
            ('task_version','0',?)""", (timestamp(),))
        db.execute("""INSERT OR IGNORE INTO daily_work_settings VALUES
            ('assignment_enabled','false',?)""", (timestamp(),))
        db.execute("""INSERT OR IGNORE INTO daily_work_settings VALUES
            ('projection_data_version','',?)""", (timestamp(),))
        db.execute("""INSERT OR IGNORE INTO daily_work_settings VALUES
            ('projection_source_updated_at','',?)""", (timestamp(),))


class DailyWork:
    def __init__(self, store, now=None, auto_project=True):
        self.store = store
        self._now = now
        ensure_schema(store)
        if auto_project:
            self.ensure_projection()

    def now(self):
        value = self._now() if callable(self._now) else self._now
        return value or datetime.now(SHANGHAI)

    def today(self):
        return _business_date(self.now())

    def ensure_projection(self):
        """Build the first local projection from published SQLite data only."""
        with self.store.connect() as db:
            tasks = db.execute("SELECT 1 FROM daily_work_tasks LIMIT 1").fetchone()
            source = db.execute("SELECT 1 FROM records WHERE kind='orders' LIMIT 1").fetchone()
            current = _text(_setting(db, "data_version", ""))
            current_updated = _text(_setting(db, "data_source_updated_at", ""))
            projected = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='projection_data_version'""").fetchone()[0])
            projected_updated = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='projection_source_updated_at'""").fetchone()[0])
        if source and (not tasks or current != projected or current_updated != projected_updated):
            try:
                return self.refresh(actor_id="system:first_projection")
            except DailyWorkConflict as error:
                if error.code != "task_projection_conflict":
                    raise
                return {"status": "blocked", "error": error.code, "message": str(error)}
        return None

    @staticmethod
    def _bump_version(db):
        row = db.execute("SELECT value FROM daily_work_settings WHERE key='task_version'").fetchone()
        value = int(row[0]) + 1 if row else 1
        db.execute("""INSERT INTO daily_work_settings VALUES ('task_version',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (str(value), timestamp()))
        return value

    @staticmethod
    def _assert_projection_current(db):
        current = _text(_setting(db, "data_version", ""))
        current_updated = _text(_setting(db, "data_source_updated_at", ""))
        projected = _text(db.execute("""SELECT value FROM daily_work_settings
            WHERE key='projection_data_version'""").fetchone()[0])
        projected_updated = _text(db.execute("""SELECT value FROM daily_work_settings
            WHERE key='projection_source_updated_at'""").fetchone()[0])
        if (not current or current != projected or
                not current_updated or current_updated != projected_updated):
            raise DailyWorkConflict("每日任务尚未跟上最新订单数据，暂停包装操作",
                                    "task_projection_conflict")

    @staticmethod
    def _staff(db, business_date=None, include_off_duty=False):
        exists = db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='staff_users'""").fetchone()
        if not exists:
            return {}
        values = {str(row["id"]): {**dict(row), "id": str(row["id"]),
                                   "roles": _roles(dict(row)), "on_duty": True}
                  for row in db.execute("SELECT * FROM staff_users WHERE active=1")}
        if business_date:
            for row in db.execute("SELECT user_id,on_duty FROM daily_work_attendance WHERE business_date=?",
                                  (business_date,)):
                if row["user_id"] in values:
                    values[row["user_id"]]["on_duty"] = bool(row["on_duty"])
        if include_off_duty:
            return values
        return {identity: value for identity, value in values.items() if value["on_duty"]}

    def _load_source(self, db, data_version=None, source_updated_at=None):
        version = _text(data_version or _setting(db, "data_version", ""))
        updated = _text(source_updated_at or _setting(db, "data_source_updated_at", ""))
        records = defaultdict(list)
        for row in db.execute("SELECT kind,payload FROM records WHERE kind IN ('orders','inventory')"):
            value = _json(row["payload"])
            if isinstance(value, dict):
                records[row["kind"]].append(value)
        calculation = {}
        exists = db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='logistics_calculations'""").fetchone()
        if exists and version:
            row = db.execute("""SELECT payload FROM logistics_calculations
                WHERE data_version=? ORDER BY rowid DESC LIMIT 1""", (version,)).fetchone()
            calculation = _json(row[0], {}) if row else {}
        print_states = {}
        if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='order_print_states'""").fetchone():
            print_states = {(row["profile_id"], row["order_no"]): row["state"]
                            for row in db.execute("SELECT profile_id,order_no,state FROM order_print_states")}
        return (version, updated, records, calculation if isinstance(calculation, dict) else {},
                print_states)

    def _derive(self, records, calculation, version, updated, today, print_states=None,
                known_packing=None):
        candidates = []
        erp_completed_orders = {}
        erp_pending_orders = set()
        orders = defaultdict(list)
        for item in records["orders"]:
            number = _text(item.get("order_no"))
            if number:
                orders[(_text(item.get("profile_id")), number)].append(item)
        plans = {_text(row.get("tradeid")): row for row in calculation.get("plans", [])
                 if isinstance(row, dict)}
        blocked = defaultdict(list)
        for row in calculation.get("blocked_orders", []):
            if isinstance(row, dict):
                blocked[_text(row.get("order_no"))].append(_text(row.get("reason")))
        allocations = defaultdict(list)
        for row in calculation.get("allocations", []):
            if not isinstance(row, dict):
                continue
            demand = row.get("demand") if isinstance(row.get("demand"), dict) else {}
            number = _text(row.get("order_no") or demand.get("order"))
            if number:
                allocations[number].append(row)
        location_index = _location_from_inventory(records["inventory"])
        calculation_ready = bool(calculation and plans)
        print_states = print_states or {}
        known_packing = known_packing or {}

        def add(role, task_type, key, title, reason, next_action, **extra):
            payload = dict(extra.pop("payload", {}))
            candidate = {
                "business_key": f"{role}:{task_type}:{key}", "role": role,
                "task_type": task_type, "title": title, "reason": reason,
                "next_action": next_action, "priority": 50, "urgent": 0,
                "profile_id": "", "order_no": "", "customer": "", "sku": "",
                "product_id": "", "location": "", "source_category": "",
                "due_date": "", "next_follow_up_date": "", "payload": payload,
                **extra,
            }
            candidate["source_data_version"] = version
            candidate["source_updated_at"] = updated
            candidate["source_fingerprint"] = _hash({k: candidate[k] for k in candidate
                if k not in {"source_data_version", "source_updated_at", "source_fingerprint"}})
            candidates.append(candidate)

        for (profile, number), lines in sorted(orders.items()):
            plan = plans.get(number, {})
            categories = {_text(item.get("logistics_company")) for item in lines} - {""}
            category = _text(plan.get("freightname")) or (next(iter(categories)) if len(categories) == 1 else "")
            customer = next((_customer(item) for item in lines if _customer(item)), "")
            due = next((_delivery_date(item) for item in lines if _delivery_date(item)), "")
            order_date = next((_day(item.get("order_date") or item.get("document_date"))
                               for item in lines if _day(item.get("order_date") or item.get("document_date"))), "")
            active = _lifecycle_active(lines)
            completion = _erp_completion(lines)
            if completion:
                erp_completed_orders[(profile, number)] = completion
            elif any(_text(item.get("process_state")) in {"500", "600"} or
                     _text(item.get("lifecycle_state")) == "candidate" or
                     _flag(item.get("lifecycle_pending_review")) for item in lines):
                erp_pending_orders.add((profile, number))
            locations = []
            for item in lines:
                value = (_text(item.get("location")) or
                         location_index.get(_text(item.get("sku")).upper(), "") or
                         location_index.get(_text(item.get("product_id")), ""))
                if value and value not in locations:
                    locations.append(value)
            location = min(locations, key=natural_location_key) if locations else ""
            payload = {
                "order_date": order_date, "line_count": len(lines),
                "skus": sorted({_text(item.get("sku")).upper() for item in lines if _text(item.get("sku"))}),
                "locations": sorted(locations, key=natural_location_key),
                "calculation_ready": bool(plan), "printable": _printable(lines),
                "lifecycle_active": active, "process_state": next((_text(item.get("process_state"))
                    for item in lines if _text(item.get("process_state"))), ""),
                "quantity": str(sum((_decimal(item.get("quantity") or item.get("open_qty") or
                    item.get("uncompleted_quantity")) for item in lines), Decimal(0))),
                "label_fingerprint": order_fingerprint(lines),
                "product_identities": sorted({_record_identity(item) for item in lines}),
                "identity_complete": all(all(_text(item.get(field)) for field in ("profile_id", "warehouse_id", "product_id", "sku_id", "base_unit")) for item in lines),
            }
            receipt_sources=[]
            allocated=allocations.get(number,[])
            for allocation in allocated:
                demand=allocation.get('demand') or {}
                source=_allocation_record(lines,allocation) or {}
                for receipt in allocation.get('receipt_allocations',[]):
                    if _decimal(receipt.get('quantity'))<=0:continue
                    supplier_id=_text(receipt.get('supplier_id'))
                    supplier_name=_text(receipt.get('supplier_name'))
                    receipt_sources.append({
                        'event_id':receipt.get('event_id',''),'document_no':receipt.get('document',''),
                        'receipt_date':receipt.get('date',''),'quantity':str(receipt.get('quantity',0)),
                        'product_id':source.get('product_id') or demand.get('sku',''),
                        'sku':source.get('sku') or demand.get('product_code',''),
                        'supplier_key':_hash([profile,supplier_id]) if supplier_id else '',
                        'supplier_name':supplier_name or '来源供应商待核对','source_type':receipt.get('type','')})
            required=sum((_decimal(a.get('demand',{}).get('quantity')) for a in allocated),Decimal(0))
            available=sum((_decimal(a.get('actual')) for a in allocated),Decimal(0))
            gap=sum((_decimal(a.get('shortage'))+_decimal(a.get('pending')) for a in allocated),Decimal(0))
            payload.update(arrival_sources=receipt_sources,arrival_received=str(available),
                arrival_required=str(required),arrival_shortage=str(gap),arrival_partial=bool(receipt_sources and gap>0))
            common = dict(profile_id=profile, order_no=number, customer=customer,
                          source_category=category, due_date=due, location=location,
                          payload=payload)

            if active and due and due < today:
                add("sales", "overdue_order", number, f"逾期未发 · {customer or number}",
                    f"订单交期为 {due}，当前仍未完成", "确认阻碍并向客户反馈新的安排",
                    priority=5, urgent=1,
                    business_key=f"sales:delivery_follow_up:{number}", **common)
            elif active and category == "催货":
                add("sales", "delivery_follow_up", number, f"跟进交期 · {customer or number}",
                    "订单已进入交期前催货窗口", "协调采购到货承诺并反馈客户",
                    priority=15, business_key=f"sales:delivery_follow_up:{number}", **common)
            if not due and active:
                add("sales", "missing_delivery_date", number, f"补齐交期 · {customer or number}",
                    "订单摘要缺少有效交期", "核实交期并补充到订单摘要末尾",
                    priority=10, **common)
            if not customer and active:
                add("sales", "missing_customer", number, f"核对客户 · {number}",
                    "待发订单缺少可靠客户名称", "核实 WMS 收货地址对应的客户名称",
                    priority=25, **common)

            if category == "已完成订单需结存" or any(_flag(item.get("shadow_demand")) for item in lines):
                add("purchase", "settlement_follow_up", number, f"补齐结存资料 · {number}",
                    "订单已经出库但 ERP 尚未结存", "核对采购入库、成本价及结存阻碍",
                    priority=8, urgent=1, **common)

            reasons = [reason for reason in blocked.get(number, []) if reason]
            if any(_text(item.get("calculation_status")) == "blocked" for item in lines) and not reasons:
                reasons = ["订单分类资料待核对"]
            if reasons:
                joined = "；".join(dict.fromkeys(reasons))[:800]
                sales_block = any(word in joined for word in ("交期", "日期", "摘要", "客户"))
                role = "sales" if sales_block else "purchase"
                add(role, "data_review", number, f"数据待核对 · {number}", joined,
                    "补齐缺失资料后重新执行更新", priority=3, urgent=1, **common)

            pack_role = "stock_packing" if category in STOCK_CATEGORIES else (
                "arrival_packing" if category in {"来货件", "异常"} else "")
            fully_ready_abnormal = category != "异常" or (bool(allocations.get(number)) and all(
                _decimal(row.get("shortage")) <= 0 and _decimal(row.get("pending")) <= 0
                for row in allocations.get(number, [])))
            package_ready = (pack_role and active and bool(plan) and not reasons and
                             all(_text(item.get("calculation_status")) == "ready" for item in lines) and
                             _printable(lines) and
                             (category != "异常" or fully_ready_abnormal))
            if package_ready:
                print_state = print_states.get((profile, number), "unprinted")
                if print_state in {"printed", "uncertain", "reserved"}:
                    known = known_packing.get((profile, number))
                    if known and known["task_type"] != "print_history_review":
                        review = print_state == "uncertain"
                        add(pack_role, "whole_order_pack", number, f"包装整单 · {number}",
                            "打印结果待人工核对" if review else "订单已进入现有包装流程",
                            "核对是否出纸后再继续" if review else "完成整单包装并确认待发货",
                            priority=4 if review else (10 if category == "异常" else
                                (5 if due and due <= today else 30)),
                            urgent=1 if review else 0, force_review=review,
                            payload={**payload, "print_state": print_state},
                            **{k:v for k,v in common.items() if k != "payload"})
                    else:
                        label = {"printed": "已有历史已打印记录", "uncertain": "历史出纸结果待核对",
                                 "reserved": "历史打印任务仍处于占用状态"}[print_state]
                        add(pack_role, "print_history_review", number, f"核实历史打印 · {number}",
                            label, "确认实际包装和出纸结果后，由管理员恢复或关闭",
                            priority=4, urgent=1, initial_status="waiting", assignable=False,
                            payload={**payload, "print_state": print_state},
                            **{k:v for k,v in common.items() if k != "payload"})
                else:
                    add(pack_role, "whole_order_pack", number, f"包装整单 · {number}",
                        "订单已经具备整单包装条件" if category != "异常" else "来货已齐且滞留超过 7 天",
                        "按货位拣货、核对图纸并打印整单标签",
                        priority=10 if category == "异常" else (5 if due and due <= today else 30),
                        urgent=1 if category == "异常" or (due and due <= today) else 0,
                        **common)
            elif category == "异常" and active and not fully_ready_abnormal:
                missing_items = []
                for allocation in allocations.get(number, []):
                    demand = allocation.get("demand") if isinstance(
                        allocation.get("demand"), dict) else {}
                    shortage = _decimal(allocation.get("shortage"))
                    pending = _decimal(allocation.get("pending"))
                    if shortage <= 0 and pending <= 0:
                        continue
                    source = _allocation_record(lines, allocation) or {}
                    missing_items.append({
                        "sku": (_text(demand.get("product_code")) or
                                _text(source.get("sku")) or _text(demand.get("sku"))),
                        "product_id": (_text(source.get("product_id")) or
                                       _text(demand.get("sku"))),
                        "shortage": str(shortage),
                        "pending": str(pending),
                    })
                missing_label = "；".join(
                    f"{item['sku'] or item['product_id'] or '商品'}"
                    f" 缺{item['shortage']}、待到{item['pending']}"
                    for item in missing_items)[:500]
                waiting_reason = ("等待采购补齐：" + missing_label if missing_label else
                                  "来货已滞留超过 7 天，缺项明细等待采购核对")
                add("purchase", "abnormal_shortage", number, f"补齐滞留来货 · {number}",
                    "来货已滞留超过 7 天，但整单仍有缺项", "核对缺口并落实剩余到货",
                    priority=6, urgent=1, **common)
                add("arrival_packing", ARRIVAL_SHORTAGE_TASK, number,
                    f"等待缺项 · {number}", waiting_reason,
                    "查看缺项与关联采购事项；齐货前不可包装或打印",
                    priority=6, urgent=1, initial_status="waiting", assignable=False,
                    payload={**payload, "read_only": True, "blocked_by": waiting_reason,
                             "missing_items": missing_items,
                             "linked_purchase_business_key":
                                 f"purchase:abnormal_shortage:{number}"},
                    **{k:v for k,v in common.items() if k != "payload"})

            if active and receipt_sources and gap>0 and category!='异常':
                add('arrival_packing',ARRIVAL_SHORTAGE_TASK,number,f'部分到货 · {number}',
                    '部分已到货，整单仍有缺口','等待采购补齐；齐货后才能打印整单',
                    initial_status='waiting',assignable=False,
                    payload={**payload,'read_only':True,'blocked_by':'整单尚未齐货，不能打印'},
                    **{k:v for k,v in common.items() if k!='payload'})

            if completion:
                payload["erp_business_completed"] = True

        # Purchase obligations are grouped by stable inventory identity/SKU.
        grouped = {}
        for (profile, number), lines in orders.items():
            plan = plans.get(number, {})
            category = _text(plan.get("freightname")) or next(iter(
                {_text(item.get("logistics_company")) for item in lines} - {""}), "")
            if category not in {"需采购", "催货", "待到货", "异常"}:
                continue
            by_line = allocations.get(number, [])
            # Consume allocation rows directly so a repeated order line cannot
            # count the same shortage twice. If a classifier generation has no
            # allocation detail, keep the plan-visible obligation with an
            # unknown/zero amount rather than dropping the task entirely.
            relevant_allocations = (by_line if category != "催货" else
                                    [row for row in by_line if _allocation_is_urging(row)])
            work = [(row, _allocation_record(lines, row)) for row in relevant_allocations]
            if not work:
                # Only fall back when the calculation did not provide line
                # allocations. With mixed-line allocations an empty urging
                # subset means no purchase follow-up for those other lines.
                work = [(None, item) for item in lines] if not by_line else []
            for row_alloc, item in work:
                demand = (row_alloc.get("demand") if row_alloc and
                          isinstance(row_alloc.get("demand"), dict) else {})
                shortage = _decimal((row_alloc or {}).get("shortage",
                                    (row_alloc or {}).get("gap", 0)))
                pending = _decimal((row_alloc or {}).get("pending", 0))
                if row_alloc is not None and shortage <= 0 and pending <= 0:
                    continue
                if item:
                    sku = _text(item.get("sku")).upper()
                    identity = _record_identity(item)
                    product_id = _text(item.get("product_id"))
                    location_value = location_index.get(sku, "") or _text(item.get("location"))
                    item_due = _delivery_date(item)
                else:
                    sku = _text(demand.get("product_code") or demand.get("sku")).upper()
                    stable = _text(demand.get("sku"))
                    warehouse = _text(demand.get("warehouse"))
                    identity = "allocation:" + "|".join((profile, warehouse, stable, sku))
                    product_id = stable
                    location_value = location_index.get(sku, "") or location_index.get(stable, "")
                    item_due = next((_delivery_date(value) for value in lines
                                     if _delivery_date(value)), "")
                key = (profile, identity)
                entry = grouped.setdefault(key, {"orders": set(), "skus": set(), "product_id": "",
                    "due_dates": [], "shortage": Decimal(0), "pending": Decimal(0), "category": category,
                    "location": "", "profile_id": profile})
                entry["orders"].add(number)
                if sku:
                    entry["skus"].add(sku)
                entry["product_id"] = entry["product_id"] or product_id
                if item_due:
                    entry["due_dates"].append(item_due)
                entry["shortage"] += shortage
                entry["pending"] += pending
                entry["location"] = entry["location"] or location_value
                if category == "需采购":
                    entry["category"] = category
        for (profile, identity), item in sorted(grouped.items()):
            sku = sorted(item["skus"])[0] if item["skus"] else identity
            orders_list = sorted(item["orders"])
            due = min(item["due_dates"]) if item["due_dates"] else ""
            category = item["category"]
            title = ("安排采购" if category == "需采购" else "落实催货" if category == "催货" else "跟进到货") + f" · {sku}"
            reason = (f"关联 {len(orders_list)} 张订单，缺口 {item['shortage']}"
                      if category == "需采购" else f"关联 {len(orders_list)} 张订单，待到 {item['pending']}")
            add("purchase", "procurement_gap" if category == "需采购" else "supply_follow_up",
                identity, title, reason, "记录采购安排、预计到货及阻碍原因",
                priority=5 if due and due <= today else (10 if category == "需采购" else 14),
                urgent=1 if due and due <= today else 0, profile_id=profile,
                sku=sku, product_id=item["product_id"], location=item["location"],
                source_category=category, due_date=due,
                payload={"order_nos": orders_list, "shortage": str(item["shortage"]),
                         "pending": str(item["pending"]), "product_identity": identity})
        candidates.sort(key=lambda row: (row["priority"], row["due_date"] or "9999-12-31",
                                         natural_location_key(row["location"]), row["business_key"]))
        return candidates, calculation_ready, erp_completed_orders, erp_pending_orders

    @staticmethod
    def _rule_owner(db, candidate, staff):
        match = [("customer", candidate.get("customer", "")),
                 ("sku", candidate.get("sku", "")),
                 ("source_category", candidate.get("source_category", ""))]
        for match_type, value in match:
            if not value:
                continue
            row = db.execute("""SELECT staff_id FROM daily_work_assignment_rules
                WHERE role=? AND match_type=? AND match_value=? AND active=1
                ORDER BY priority,id LIMIT 1""", (candidate["role"], match_type, value)).fetchone()
            if row and row[0] in staff and candidate["role"] in staff[row[0]]["roles"]:
                return row[0]
        return ""

    @staticmethod
    def _sticky_owner(db, candidate, staff):
        field, value = ("customer", candidate.get("customer", "")) if candidate["role"] == "sales" \
            else ("sku", candidate.get("sku", ""))
        if candidate["role"] not in {"sales", "purchase"} or not value:
            return ""
        if candidate["role"] == "sales":
            return ""
        row = db.execute(f"""SELECT assignee_id FROM daily_work_tasks
            WHERE role=? AND {field}=? AND assignee_id<>'' ORDER BY updated_at DESC LIMIT 1""",
            (candidate["role"], value)).fetchone()
        if row and row[0] in staff and candidate["role"] in staff[row[0]]["roles"]:
            return row[0]
        return ""

    @staticmethod
    def _counts(db):
        rows = db.execute("""SELECT assignee_id,role,count(*) AS n FROM daily_work_tasks
            WHERE assignee_id<>'' AND condition_state='active'
              AND status IN ('todo','in_progress','waiting') GROUP BY assignee_id,role""").fetchall()
        role_counts = defaultdict(int)
        packing_counts = defaultdict(int)
        for row in rows:
            role_counts[(row["assignee_id"], row["role"])] += row["n"]
            if row["role"] in PACKING_ROLES:
                packing_counts[row["assignee_id"]] += row["n"]
        return role_counts, packing_counts

    def _choose_owner(self, db, candidate, staff, role_counts, packing_counts):
        # Administrator accounts can hold every role for supervision, but are
        # not ordinary auto-assignment capacity. An explicit assignment rule
        # may still nominate one when that is intentional.
        eligible = [value for value in staff.values() if candidate["role"] in value["roles"]
                    and not _flag(value.get("is_admin"))]
        if candidate["role"] == "sales":
            from .sales_ownership import fixed_sales_owner
            return fixed_sales_owner(db, _text(candidate.get("profile_id")),
                                     _text(candidate.get("customer")))
        if not eligible:
            return ""
        if candidate["role"] in PACKING_ROLES:
            payload = candidate.get("payload", {})
            payload = _json(payload, {}) if isinstance(payload, str) else payload
            if payload.get("identity_complete") is False:
                return ""
            identities = payload.get("product_identities", [])
            group_key = _hash([candidate["role"], identities]) if len(identities) == 1 else ""
            if group_key and db.execute("SELECT 1 FROM sqlite_master WHERE name='packing_product_owners'").fetchone():
                previous = db.execute("SELECT assignee_id FROM packing_product_owners WHERE group_key=?", (group_key,)).fetchone()
                if previous and any(value["id"] == previous[0] for value in eligible):
                    return previous[0]
            minimum = min(packing_counts[value["id"]] for value in eligible)
            eligible = [value for value in eligible if packing_counts[value["id"]] == minimum]
            detail_load = defaultdict(int)
            for row in db.execute("SELECT assignee_id,payload FROM daily_work_tasks WHERE role IN ('stock_packing','arrival_packing') AND condition_state='active' AND status IN ('todo','in_progress','waiting')"):
                detail_load[row["assignee_id"]] += int(_json(row["payload"], {}).get("line_count", 1) or 1)
            fewest = min(detail_load[value["id"]] for value in eligible)
            eligible = [value for value in eligible if detail_load[value["id"]] == fewest]
        preferred = self._rule_owner(db, candidate, staff) or self._sticky_owner(db, candidate, staff)
        if preferred and any(value["id"] == preferred for value in eligible):
            return preferred
        minimum = min(role_counts[(value["id"], candidate["role"])] for value in eligible)
        tied = sorted(value["id"] for value in eligible
                      if role_counts[(value["id"], candidate["role"])] == minimum)
        cursor_key = "round_robin:" + candidate["role"]
        row = db.execute("SELECT value FROM daily_work_settings WHERE key=?", (cursor_key,)).fetchone()
        cursor = int(row[0]) if row and _text(row[0]).isdigit() else 0
        owner = tied[cursor % len(tied)]
        db.execute("""INSERT INTO daily_work_settings VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (cursor_key, str(cursor + 1), timestamp()))
        if candidate["role"] in PACKING_ROLES and group_key and db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='packing_product_owners'").fetchone():
            db.execute("INSERT INTO packing_product_owners VALUES (?,?,?,?) ON CONFLICT(group_key) DO UPDATE SET assignee_id=excluded.assignee_id,updated_at=excluded.updated_at",
                       (group_key, candidate["role"], owner, timestamp()))
        return owner

    def refresh(self, data_version=None, source_updated_at=None, idempotency_key=None, actor_id="system"):
        """Idempotently reconcile current published data into persistent tasks."""
        today = self.today()
        with self.store.connect(write=True) as db:
            if idempotency_key:
                replay = db.execute("SELECT result FROM daily_work_refreshes WHERE idempotency_key=?",
                                    (_text(idempotency_key),)).fetchone()
                if replay:
                    result = _json(replay[0], {})
                    result["replayed"] = True
                    return result
            version, updated, records, calculation, print_states = self._load_source(
                db, data_version=data_version, source_updated_at=source_updated_at)
            print_details = {}
            if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                AND name='order_print_states'""").fetchone():
                print_details = {(row["profile_id"], row["order_no"]): dict(row)
                                 for row in db.execute("SELECT * FROM order_print_states")}
            calculation_complete = (isinstance(calculation.get("plans"), list) and
                isinstance(calculation.get("allocations"), list) and
                isinstance(calculation.get("blocked_orders"), list))
            order_numbers = {_text(row.get("order_no")) for row in records["orders"]
                             if _text(row.get("order_no"))}
            covered_orders = ({_text(row.get("tradeid")) for row in calculation.get("plans", [])
                               if isinstance(row, dict) and _text(row.get("tradeid"))} |
                              {_text(row.get("order_no")) for row in calculation.get("blocked_orders", [])
                               if isinstance(row, dict) and _text(row.get("order_no"))})
            calculation_complete = calculation_complete and order_numbers <= covered_orders
            if records["orders"] and not calculation_complete:
                raise DailyWorkConflict(
                    "当前数据版本缺少完整物流计算，保留上一份每日任务并等待重新计算",
                    "task_projection_conflict")
            from .sales_ownership import migrate_customer_owners
            migrate_customer_owners(db)
            stale = not _source_is_today(updated, today)
            existing = defaultdict(list)
            for row in db.execute("SELECT * FROM daily_work_tasks ORDER BY cycle"):
                existing[row["business_key"]].append(row)
            known_packing = {}
            for rows in existing.values():
                row = rows[-1]
                if row["role"] in PACKING_ROLES and row["condition_state"] == "active":
                    known_packing[(row["profile_id"], row["order_no"])] = row
            candidates, _, erp_completed_orders, erp_pending_orders = self._derive(
                records, calculation, version, updated, today, print_states, known_packing)
            shortage_refs = {(candidate["profile_id"], candidate["order_no"])
                             for candidate in candidates
                             if candidate["task_type"] == ARRIVAL_SHORTAGE_TASK}
            current_order_refs = {(_text(row.get("profile_id")), _text(row.get("order_no")))
                                  for row in records["orders"] if _text(row.get("order_no")) and _in_unfulfilled_scope(row)}
            candidates = [candidate for candidate in candidates if _current_task(candidate, current_order_refs)]
            current_lines = defaultdict(list)
            for item in records["orders"]:
                current_lines[(_text(item.get("profile_id")), _text(item.get("order_no")))].append(item)
            current_plans = {_text(plan.get("tradeid")): _text(plan.get("freightname"))
                             for plan in calculation.get("plans", [])}
            blocked_numbers = {_text(item.get("order_no")) for item in calculation.get("blocked_orders", [])}
            nonstock_categories = {}
            for ref, lines in current_lines.items():
                category = current_plans.get(ref[1], "")
                if (category in {"催货", "需采购", "待到货", "来货件", "异常"} and
                        ref[1] not in blocked_numbers and _lifecycle_active(lines) and
                        all(_text(item.get("calculation_status")) == "ready" and
                            _text(item.get("logistics_company")) == category and
                            not _text(item.get("lifecycle_error") or item.get("last_error")) and
                            not _flag(item.get("source_review")) for item in lines)):
                    nonstock_categories[ref] = category
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_lifecycle_state'").fetchone():
                # confirmation_count counts consecutive 500 observations, not
                # the independent release_pending -> archived 600 transition.
                # Orders going directly from 400 to confirmed 600 can have 0.
                for lifecycle in db.execute('''SELECT * FROM order_lifecycle_state
                    WHERE last_error='' AND (
                      (lifecycle_state='shipped_unposted' AND process_state='500' AND confirmation_count>=2)
                      OR (lifecycle_state='archived' AND process_state='600'
                          AND (confirmation_count>=2 OR (ended_at<>'' AND last_confirmed_at<>''))))'''):
                    ref=(lifecycle['profile_id'],lifecycle['order_no'])
                    if ref not in current_order_refs:
                        erp_completed_orders[ref]={name:lifecycle[name] for name in
                            ('lifecycle_state','process_state','confirmation_count')}
            staff = self._staff(db, today)
            all_staff = self._staff(db, today, include_off_duty=True)
            assignment_enabled = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='assignment_enabled'""").fetchone()[0]).lower() == "true"
            role_counts, packing_counts = self._counts(db)
            now = timestamp()
            seen = set()
            changed = False
            created = updated_count = resolved_count = completed_count = 0
            for candidate in candidates:
                key = candidate["business_key"]
                seen.add(key)
                previous = existing.get(key, [])[-1] if existing.get(key) else None
                # Legacy allocations encoded inventory identity in a JSON warehouse
                # key. Normalize the key while keeping the task ID, owner and events.
                if not previous and candidate["role"] == "purchase" and candidate.get("product_id"):
                    legacy = [rows[-1] for old_key, rows in existing.items()
                        if "allocation:" in old_key and rows[-1]["condition_state"] == "active" and
                        rows[-1]["role"] == "purchase" and rows[-1]["task_type"] == candidate["task_type"] and
                        rows[-1]["profile_id"] == candidate["profile_id"] and rows[-1]["product_id"] == candidate["product_id"] and
                        _legacy_purchase_identity(rows[-1]) == candidate["payload"].get("product_identity")]
                    if len(legacy) == 1:
                        previous = legacy[0]
                        existing.pop(previous["business_key"], None)
                        db.execute("UPDATE daily_work_tasks SET business_key=? WHERE id=?", (key, previous["id"]))
                        previous = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (previous["id"],)).fetchone()
                        existing[key] = [previous]
                if not previous and candidate["role"] == "purchase" and candidate["payload"].get("product_identity"):
                    continuing = [rows[-1] for rows in existing.values() if rows[-1]["condition_state"] == "active" and
                        rows[-1]["role"] == "purchase" and rows[-1]["task_type"] in {"procurement_gap","supply_follow_up"} and
                        (_json(rows[-1]["payload"], {}).get("product_identity") == candidate["payload"]["product_identity"] or
                         _legacy_purchase_identity(rows[-1]) == candidate["payload"]["product_identity"])]
                    if len(continuing) == 1:
                        previous = continuing[0]
                        existing.pop(previous["business_key"],None)
                        db.execute("UPDATE daily_work_tasks SET business_key=? WHERE id=?",(key,previous["id"]))
                        previous=db.execute("SELECT * FROM daily_work_tasks WHERE id=?",(previous["id"],)).fetchone()
                        existing[key]=[previous]
                if previous and previous["condition_state"] == "active":
                    task_id, cycle = previous["id"], previous["cycle"]
                    status, assignee = previous["status"], previous["assignee_id"]
                    old_fingerprint = previous["source_fingerprint"]
                    old_payload = _json(previous["payload"], {})
                    for field in ("waiting_for", "promised_date", "followup_result", "followup_source_changed"):
                        if field in old_payload:
                            candidate["payload"][field] = old_payload[field]
                    if candidate["role"] == "purchase" and (previous["next_follow_up_date"] or old_payload.get("followup_result")):
                        old_need=_decimal(old_payload.get("shortage"))+_decimal(old_payload.get("pending"))
                        new_need=_decimal(candidate["payload"].get("shortage"))+_decimal(candidate["payload"].get("pending"))
                        if new_need>old_need or set(candidate["payload"].get("order_nos",[]))-set(old_payload.get("order_nos",[])):
                            candidate["payload"]["followup_source_changed"]=True
                    # A source refresh updates facts, never employee workflow
                    # decisions. Follow-up and manager urgency only change via
                    # explicit task operations.
                    candidate["next_follow_up_date"] = previous["next_follow_up_date"]
                    if previous["urgent"]:
                        candidate["urgent"] = 1
                        candidate["priority"] = min(candidate["priority"], previous["priority"])
                        if "base_priority" in old_payload:
                            candidate["payload"].setdefault("base_priority", old_payload["base_priority"])
                    label_changed = (candidate["role"] in PACKING_ROLES and
                        _text(old_payload.get("label_fingerprint")) and
                        _text(old_payload.get("label_fingerprint")) !=
                        _text(candidate["payload"].get("label_fingerprint")))
                    ref = (candidate["profile_id"], candidate["order_no"])
                    print_detail = print_details.get(ref, {})
                    current_label = _text(candidate["payload"].get("label_fingerprint"))
                    corrected_label = (
                        candidate["role"] == "stock_packing" and
                        _text(print_detail.get("state")) == "unprinted" and
                        _text(print_detail.get("reason")).startswith("correction:") and
                        current_label and _text(print_detail.get("fingerprint")) == current_label)
                    if corrected_label and old_payload.get("blocked_by") == LABEL_REVIEW_BLOCK:
                        # Administrator confirmed there was no physical output.
                        # Clear only this label review, keeping notes, handoffs,
                        # identities and the original events. A future content
                        # change has a different fingerprint and is not waived.
                        old_payload.pop("label_review_required", None)
                        old_payload.pop("status_before_label_review", None)
                        old_payload.pop("blocked_by", None)
                        status = "todo"
                    stale_printed_label = (
                        _text(print_detail.get("state")) in {"printed", "uncertain"} and
                        current_label and
                        _text(print_detail.get("fingerprint")) != current_label)
                    label_requires_review = label_changed and (
                        status in {"in_progress", "packed"} or stale_printed_label or
                        _text(print_detail.get("state")) == "reserved")
                    if label_requires_review:
                        before_label = status if status in {"todo", "in_progress"} else "in_progress"
                        status = "waiting"
                        candidate["payload"]["blocked_by"] = LABEL_REVIEW_BLOCK
                        candidate["payload"]["label_review_required"] = True
                        candidate["payload"]["status_before_label_review"] = before_label
                        candidate["reason"] = "订单数量、摘要或标签内容已变化"
                        candidate["next_action"] = "核对已包装实物与新标签内容，再决定恢复包装或关闭"
                        if stale_printed_label and invalidate_changed_print_state(
                                db, ref[0], ref[1], current_label):
                            print_states[ref] = "unprinted"
                            print_details[ref] = {**print_detail, "state": "unprinted",
                                                  "fingerprint": current_label}
                    elif _flag(old_payload.get("label_review_required")):
                        print_is_current = (
                            _text(print_detail.get("state")) == "printed" and current_label and
                            _text(print_detail.get("fingerprint")) == current_label)
                        old_label_block = _text(old_payload.get("blocked_by"))
                        if print_is_current and old_label_block == LABEL_REVIEW_BLOCK:
                            prior = _text(old_payload.get("status_before_label_review"))
                            status = prior if prior in {"todo", "in_progress"} else "in_progress"
                        else:
                            status = "waiting"
                            candidate["payload"]["label_review_required"] = True
                            candidate["payload"]["status_before_label_review"] = _text(
                                old_payload.get("status_before_label_review")) or "todo"
                            if old_label_block == LABEL_REVIEW_BLOCK:
                                candidate["payload"]["blocked_by"] = LABEL_REVIEW_BLOCK
                    old_block = _text(old_payload.get("blocked_by"))
                    if (candidate["task_type"] == "whole_order_pack" and
                            _flag(old_payload.get("shortage_review_required")) and
                            old_block in {"", SHORTAGE_REVIEW_BLOCK, PRINT_REVIEW_BLOCK} and
                            not candidate["payload"].get("blocked_by")):
                        status = "waiting"
                        candidate["payload"]["blocked_by"] = SHORTAGE_READY_REVIEW_BLOCK
                        candidate["reason"] = "先前包装过程中订单重新出现缺项，现已恢复齐货"
                        candidate["next_action"] = "核对已拣或已包实物后继续包装"
                    if candidate.get("force_review"):
                        protected_block = (_text(candidate["payload"].get("blocked_by")) or
                                           _text(old_payload.get("blocked_by")))
                        existing_workflow_wait = (
                            status == "waiting" and
                            not _text(old_payload.get("status_before_print_review")) and
                            protected_block != PRINT_REVIEW_BLOCK)
                        if existing_workflow_wait:
                            # An employee's explicit wait/defer remains the
                            # workflow state. The print table independently
                            # prevents packing until it is reviewed.
                            if protected_block:
                                candidate["payload"]["blocked_by"] = protected_block
                        elif protected_block and protected_block != PRINT_REVIEW_BLOCK:
                            # A print-state review must not erase a stronger
                            # employee handoff or changed-label review.
                            candidate["payload"]["blocked_by"] = protected_block
                        else:
                            before_review = (_text(old_payload.get("status_before_print_review"))
                                             if status == "waiting" else status)
                            candidate["payload"]["status_before_print_review"] = (
                                before_review if before_review in {"todo", "in_progress"} else "todo")
                            candidate["payload"]["blocked_by"] = PRINT_REVIEW_BLOCK
                        status = "waiting"
                    print_review_cleared = (
                        candidate["task_type"] == "whole_order_pack" and
                        print_states.get((candidate["profile_id"], candidate["order_no"]),
                                         "unprinted") == "unprinted" and
                        status == "waiting" and
                        _text(old_payload.get("blocked_by")) == PRINT_REVIEW_BLOCK and
                        not candidate["payload"].get("blocked_by"))
                    if print_review_cleared:
                        prior = _text(old_payload.get("status_before_print_review"))
                        status = prior if prior in {"todo", "in_progress"} else "todo"
                        candidate["payload"].pop("status_before_print_review", None)
                    resolution_not_confirmed = (status == "resolved" and (
                        (version and version != previous["source_data_version"]) or
                        (updated and updated != previous["source_updated_at"])))
                    if resolution_not_confirmed:
                        status = "todo"
                        candidate["next_follow_up_date"] = today
                        candidate["payload"]["blocked_by"] = "处理结果尚未被新一轮 ERP 数据确认"
                    elif (not print_review_cleared and status == "waiting" and
                          old_payload.get("blocked_by") and
                          not old_payload.get('source_missing_review') and
                          not old_payload.get('source_scope_review') and
                          not candidate["payload"].get("blocked_by")):
                        # Review/handoff blocks are workflow state. A fresh ERP
                        # payload must not silently erase them.
                        candidate["payload"]["blocked_by"] = old_payload["blocked_by"]
                else:
                    task_id, cycle = uuid.uuid4().hex, (previous["cycle"] + 1 if previous else 1)
                    status = candidate.get("initial_status", "todo")
                    assignee, old_fingerprint, old_payload = "", "", {}
                if candidate["role"] == "sales" and status in {"todo", "in_progress", "waiting"}:
                    # Old task owners are work history, never customer assignment evidence.
                    assignee = self._choose_owner(db, candidate, all_staff, role_counts, packing_counts)
                invalid_assignment = False
                if assignee:
                    owner = (all_staff if candidate["role"] == "sales" else staff).get(assignee)
                    invalid_assignment = not owner or candidate["role"] not in owner["roles"]
                    if invalid_assignment:
                        print_state = print_states.get(
                            (candidate["profile_id"], candidate["order_no"]), "unprinted")
                        if status == "todo" and print_state == "unprinted" and not (previous and _task_has_work_history(db, previous)):
                            assignee = ""
                        else:
                            candidate["payload"]["blocked_by"] = (
                                "原负责人当前不可用，已开始或已打印任务需要管理员交接")
                if _flag(candidate["payload"].get("read_only")):
                    # Broadcast information is visible to every matching role,
                    # but it is never workload, ownership, or packing capacity.
                    assignee = ""
                elif candidate.get("assignable") is False:
                    # This flag suppresses automatic assignment only. Keep an
                    # administrator's explicit owner on an existing review
                    # task across later source refreshes.
                    if not previous or previous["condition_state"] != "active":
                        assignee = ""
                elif candidate["role"] in PACKING_ROLES and stale and not assignee:
                    assignee = ""
                elif not assignee and assignment_enabled:
                    assignee = self._choose_owner(db, candidate,
                        all_staff if candidate["role"] == "sales" else staff,
                        role_counts, packing_counts)
                    if assignee:
                        role_counts[(assignee, candidate["role"])] += 1
                        if candidate["role"] in PACKING_ROLES:
                            packing_counts[assignee] += 1
                business_complete = bool(candidate["payload"].get("erp_business_completed"))
                if business_complete and candidate["role"] in PACKING_ROLES:
                    status = "business_completed"
                values = (candidate["role"], candidate["task_type"], status, "resolved" if business_complete else "active",
                    assignee, candidate["profile_id"], candidate["order_no"], candidate["customer"], candidate["sku"],
                    candidate["product_id"], candidate["location"], candidate["source_category"], candidate["title"],
                    candidate["reason"], candidate["next_action"], candidate["due_date"],
                    candidate["next_follow_up_date"], candidate["priority"], candidate["urgent"], version, updated,
                    candidate["source_fingerprint"], dumps(candidate["payload"]), now)
                if not previous or previous["condition_state"] != "active":
                    db.execute("""INSERT INTO daily_work_tasks(
                        id,business_key,cycle,role,task_type,status,condition_state,assignee_id,
                        profile_id,order_no,customer,sku,product_id,location,source_category,title,reason,next_action,
                        due_date,next_follow_up_date,priority,urgent,source_data_version,source_updated_at,
                        source_fingerprint,payload,version,first_seen_at,last_seen_at,condition_resolved_at,
                        human_completed_at,business_completed_at,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                        (task_id, key, cycle, *values[:-1], now, now,
                         now if business_complete else "", "", now if business_complete else "", now, now))
                    db.execute("""INSERT INTO daily_work_events VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, task_id, "created", actor_id, "", status,
                        "", "", None, 1, dumps({"data_version": version}), now))
                    created += 1
                    changed = True
                else:
                    assignee_changed = assignee != previous["assignee_id"]
                    status_changed = status != previous["status"]
                    source_generation_changed = (version != previous["source_data_version"] or
                                                 updated != previous["source_updated_at"])
                    payload_changed = dumps(candidate["payload"]) != previous["payload"]
                    business_changed = (old_fingerprint != candidate["source_fingerprint"] or business_complete or
                                resolution_not_confirmed or assignee_changed or status_changed or
                                payload_changed)
                    material = business_changed or source_generation_changed
                    if material:
                        next_version = previous["version"] + 1
                        db.execute("""UPDATE daily_work_tasks SET
                            role=?,task_type=?,status=?,condition_state=?,assignee_id=?,profile_id=?,order_no=?,
                            customer=?,sku=?,product_id=?,location=?,source_category=?,title=?,reason=?,next_action=?,
                            due_date=?,next_follow_up_date=?,priority=?,urgent=?,source_data_version=?,source_updated_at=?,
                            source_fingerprint=?,payload=?,last_seen_at=?,condition_resolved_at=?,business_completed_at=?,
                            version=?,updated_at=? WHERE id=?""",
                            (*values, now if business_complete else previous["condition_resolved_at"],
                             now if business_complete else previous["business_completed_at"], next_version, now, task_id))
                        event_type = ("erp_business_completed" if business_complete else
                                      "source_changed_requires_review" if label_requires_review else
                                      "resolution_not_confirmed" if resolution_not_confirmed else
                                      "auto_assigned" if not previous["assignee_id"] and assignee else
                                      "assignment_released" if previous["assignee_id"] and not assignee else
                                      "staff_handoff_required" if invalid_assignment and
                                      candidate["payload"].get("blocked_by") else
                                      "source_updated")
                        event_note = ("订单标签内容已变化，原包装结果转待核对"
                                      if event_type == "source_changed_requires_review" else
                                      "新一轮 ERP 数据仍存在该问题，恢复为今日待办"
                                      if event_type == "resolution_not_confirmed" else "")
                        # Keep version checks across generations, without
                        # inventing thousands of business events for a new
                        # fetch timestamp alone. Real changes remain audited.
                        if business_changed:
                            db.execute("""INSERT INTO daily_work_events VALUES
                                (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, task_id, event_type, actor_id,
                                previous["status"], status, event_note, "", None, next_version,
                                dumps({"data_version": version}), now))
                        if event_type == "source_changed_requires_review":
                            self._resolve_dispatch_follow_up(
                                db, previous, actor_id, "包装标签内容变化，暂停待发货跟进")
                        updated_count += 1
                        completed_count += int(business_complete and previous["status"] != "business_completed")
                        changed = True
                    elif previous["last_seen_at"] != now:
                        db.execute("UPDATE daily_work_tasks SET last_seen_at=? WHERE id=?", (now, task_id))
            # If a formerly-ready order becomes short again, untouched todo
            # work may safely leave the queue. Started, printed, or packed work
            # stays with its owner as a review item so physical work is not
            # silently lost. The read-only broadcast and purchase task carry
            # the current shortage details.
            for key, rows in existing.items():
                previous = rows[-1]
                ref = (previous["profile_id"], previous["order_no"])
                if (ref not in shortage_refs or previous["task_type"] != "whole_order_pack" or
                        previous["role"] not in PACKING_ROLES or key in seen):
                    continue
                row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?",
                                 (previous["id"],)).fetchone()
                if not row or row["condition_state"] != "active":
                    continue
                print_state = print_states.get(ref, "unprinted")
                unsafe = (row["status"] in {"in_progress", "waiting", "packed"} or
                          print_state in {"printed", "uncertain", "reserved"})
                if not unsafe:
                    continue
                payload = _json(row["payload"], {})
                stored_block = _text(payload.get("blocked_by"))
                block = stored_block
                if block == PRINT_REVIEW_BLOCK and print_state == "unprinted":
                    block = ""
                if (row["status"] == "waiting" and
                        _flag(payload.get("shortage_review_required")) and
                        row["source_data_version"] == version and
                        row["source_updated_at"] == updated and
                        not (stored_block == PRINT_REVIEW_BLOCK and
                             print_state == "unprinted")):
                    seen.add(key)
                    continue
                payload.update({
                    "blocked_by": block or SHORTAGE_REVIEW_BLOCK,
                    "shortage_review_required": True,
                    "linked_arrival_shortage_business_key":
                        f"arrival_packing:{ARRIVAL_SHORTAGE_TASK}:{row['order_no']}",
                    "linked_purchase_business_key":
                        f"purchase:abnormal_shortage:{row['order_no']}",
                })
                next_version = row["version"] + 1
                db.execute("""UPDATE daily_work_tasks SET status='waiting',reason=?,next_action=?,
                    source_data_version=?,source_updated_at=?,payload=?,last_seen_at=?,version=?,updated_at=?
                    WHERE id=?""", ("订单在包装流程中重新出现缺项",
                    "等待采购补齐；齐货后核对已拣或已包实物", version, updated,
                    dumps(payload), now, next_version, now, row["id"]))
                db.execute("""INSERT INTO daily_work_events VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"],
                    "source_shortage_requires_review", actor_id, row["status"], "waiting",
                    "订单重新出现缺项，保留负责人及包装历史等待复核", "", None,
                    next_version, dumps({"data_version": version,
                                         "print_state": print_state}), now))
                if row["status"] == "packed":
                    self._resolve_dispatch_follow_up(
                        db, row, actor_id, "订单重新出现缺项，暂停待发货跟进")
                seen.add(key)
                updated_count += 1
                changed = True

            # A 500/600 observation removes an order from the printable queue.
            # Complete the persistent packaging task explicitly before generic
            # condition reconciliation so the business result is auditable.
            for profile, number in sorted(erp_completed_orders):
                evidence = erp_completed_orders[(profile, number)]
                process_state = evidence["process_state"]
                for row in db.execute("""SELECT * FROM daily_work_tasks
                    WHERE profile_id=? AND order_no=?
                      AND role IN ('stock_packing','arrival_packing')
                      AND condition_state='active'""", (profile, number)).fetchall():
                    if row["status"] == "business_completed":
                        continue
                    next_version = row["version"] + 1
                    db.execute("""UPDATE daily_work_tasks SET status='business_completed',
                        condition_state='resolved',condition_resolved_at=?,business_completed_at=?,
                        version=?,updated_at=? WHERE id=?""",
                        (now, now, next_version, now, row["id"]))
                    db.execute("""INSERT INTO daily_work_events VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"],
                        "erp_business_completed", actor_id, row["status"], "business_completed",
                        f"ERP process_state={process_state} 已确认出库", "", None, next_version,
                        dumps({"data_version": version, **evidence}), now))
                    completed_count += 1
                    changed = True
                for row in db.execute("""SELECT * FROM daily_work_tasks
                    WHERE profile_id=? AND order_no=? AND role='sales'
                      AND condition_state='active'""",
                    (profile, number)).fetchall():
                    next_version = row["version"] + 1
                    db.execute("""UPDATE daily_work_tasks SET status='business_completed',
                        condition_state='resolved',condition_resolved_at=?,business_completed_at=?,
                        version=?,updated_at=? WHERE id=?""",
                        (now, now, next_version, now, row["id"]))
                    db.execute("""INSERT INTO daily_work_events VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"],
                        "erp_business_completed", actor_id, row["status"], "business_completed",
                        f"ERP process_state={process_state} 已确认发货", "", None, next_version,
                        dumps({"data_version": version, **evidence}), now))
                    completed_count += 1
                    changed = True
            # Only active conditions from the same or older generation are retired.
            for key, rows in existing.items():
                row = rows[-1]
                current_row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (row["id"],)).fetchone()
                if current_row is None or current_row["condition_state"] != "active":
                    continue
                row = current_row
                if key in seen or row["condition_state"] != "active":
                    continue
                if (row["role"] in PACKING_ROLES and
                        (row["profile_id"], row["order_no"]) in erp_completed_orders):
                    continue
                if (row["role"] in PACKING_ROLES and
                        (row["profile_id"], row["order_no"]) in erp_pending_orders):
                    continue
                if (row["role"] == "sales" and row["task_type"] == "packed_waiting_dispatch" and
                        (row["profile_id"], row["order_no"]) in current_order_refs and
                        (row["profile_id"], row["order_no"]) not in erp_completed_orders):
                    dispatch_payload = _json(row["payload"], {})
                    linked = db.execute("SELECT * FROM daily_work_tasks WHERE id=?",
                        (_text(dispatch_payload.get("packing_task_id")),)).fetchone()
                    linked_payload = _json(linked["payload"], {}) if linked else {}
                    if (linked and linked["business_key"] in seen and
                            linked["condition_state"] == "active" and linked["status"] == "packed" and
                            _text(linked_payload.get("label_fingerprint")) ==
                            _text(dispatch_payload.get("label_fingerprint"))):
                        continue
                replacement=(row['role']=='stock_packing' and db.execute('''SELECT 1 FROM daily_work_tasks
                    WHERE role='stock_packing' AND profile_id=? AND order_no=?
                    AND condition_state='active' AND id<>? AND task_type='whole_order_pack' LIMIT 1''',
                    (row['profile_id'],row['order_no'],row['id'])).fetchone())
                # A verified classification change is different from missing
                # source data. Untouched, unprinted work can leave this role;
                # started/packed work and live print facts still require review.
                ref = (row['profile_id'], row['order_no'])
                left_stock_scope = (row['role'] == 'stock_packing' and
                    row['task_type'] == 'whole_order_pack' and row['status'] == 'todo' and
                    print_states.get(ref, 'unprinted') == 'unprinted' and ref in nonstock_categories)
                # A verified receipt correction can move an already printed
                # order to arrival packing. Its print fact belongs to the
                # order, not to the old role. Keep the old task and event
                # history, but do not leave two active packaging tasks.
                # Sending/uncertain output and changed labels still require
                # review; never treat source loss as a role transfer.
                arrival_replacement = None
                if (row['role'] == 'stock_packing' and
                        row['task_type'] == 'whole_order_pack' and row['status'] == 'todo' and
                        nonstock_categories.get(ref) in {'来货件', '异常'} and
                        print_states.get(ref) == 'printed'):
                    other = db.execute('''SELECT * FROM daily_work_tasks
                        WHERE role='arrival_packing' AND profile_id=? AND order_no=?
                        AND condition_state='active' AND task_type='whole_order_pack'
                        ORDER BY cycle DESC LIMIT 1''', ref).fetchone()
                    old_payload = _json(row['payload'], {})
                    new_payload = _json(other['payload'], {}) if other else {}
                    fingerprint = _text(new_payload.get('label_fingerprint'))
                    if (other and other['business_key'] in seen and fingerprint and
                            fingerprint == _text(old_payload.get('label_fingerprint')) and
                            fingerprint == _text(print_details.get(ref, {}).get('fingerprint')) and
                            new_payload.get('arrival_sources') and
                            new_payload.get('identity_complete') and
                            not new_payload.get('blocked_by') and not new_payload.get('read_only')):
                        arrival_replacement = other['id']
                if (row['role']=='stock_packing' and ref in current_order_refs and
                        not replacement and not left_stock_scope and not arrival_replacement):
                    # A present order whose category changed still needs review.
                    # Missing orders leave the live projection below, without
                    # fabricating ERP completion or changing print/history facts.
                    payload = _json(row['payload'], {})
                    marker='source_scope_review'
                    if not payload.get(marker):
                        payload.update({marker:True,'read_only':True,
                            'blocked_by':'库存包装条件已变化，请核对 ERP 与实际处理'})
                        db.execute('UPDATE daily_work_tasks SET payload=?,version=version+1,updated_at=? WHERE id=?',
                                   (dumps(payload),now,row['id']))
                        changed = True
                    continue
                next_version = row["version"] + 1
                db.execute("""UPDATE daily_work_tasks SET condition_state='resolved',condition_resolved_at=?,
                    version=?,updated_at=? WHERE id=?""", (now, next_version, now, row["id"]))
                db.execute("""INSERT INTO daily_work_events VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"],
                    "packing_role_corrected" if arrival_replacement else "condition_resolved", actor_id,
                    row["status"], row["status"],
                    "来货来源已核实，转入来货包装；保留原打印记录及历史任务" if arrival_replacement else
                    f"订单已转为{nonstock_categories[ref]}，未开始包装且未打印，退出库存件包装" if left_stock_scope else
                    "已不在有效未交清单，退出当前任务；保留处理及打印记录" if ref not in current_order_refs else "来源条件已解除", "", None, next_version,
                    dumps({"data_version": version, **({'replacement_task_id':arrival_replacement}
                        if arrival_replacement else {})}), now))
                resolved_count += 1
                changed = True
            task_version = self._bump_version(db) if changed else int(
                db.execute("SELECT value FROM daily_work_settings WHERE key='task_version'").fetchone()[0])
            db.execute("""UPDATE daily_work_settings SET value=?,updated_at=?
                WHERE key='projection_data_version'""", (version, now))
            db.execute("""UPDATE daily_work_settings SET value=?,updated_at=?
                WHERE key='projection_source_updated_at'""", (updated, now))
            regrouped = self.reconcile_product_assignments(actor_id=actor_id, db=db) if not stale else {"assigned": 0}
            if regrouped["assigned"]:
                task_version = int(db.execute("SELECT value FROM daily_work_settings WHERE key='task_version'").fetchone()[0])
            completed_by_role = {role: db.execute("""SELECT count(*) FROM daily_work_tasks
                WHERE role=? AND substr(business_completed_at,1,10)=?""", (role, today)).fetchone()[0]
                                 for role in ROLES}
            if getattr(self,'packing_index',None):self.packing_index.refresh(db)
            result = {"business_date": today, "data_version": version, "source_updated_at": updated,
                      "task_version": task_version, "stale": stale, "created": created,
                      "updated": updated_count, "condition_resolved": resolved_count,
                      "business_completed": completed_count, "assignment_enabled": assignment_enabled,
                      "business_completed_by_role": completed_by_role, "replayed": False}
            result["counts"] = {name: result[name] for name in
                                ("created", "updated", "condition_resolved", "business_completed")}
            if idempotency_key:
                db.execute("INSERT INTO daily_work_refreshes VALUES (?,?,?,?)",
                           (_text(idempotency_key), version, dumps(result), now))
            return result

    @staticmethod
    def _card(row, staff=None, print_states=None, blocked_by=""):
        value = dict(row)
        value["payload"] = _json(value.get("payload"), {})
        for name in ("urgent",):
            value[name] = bool(value[name])
        orders = value["payload"].get("order_nos")
        if not isinstance(orders, list):
            orders = [value["order_no"]] if value.get("order_no") else []
        value["order_nos"] = [_text(item) for item in orders if _text(item)]
        value["follow_up_date"] = value.get("next_follow_up_date", "")
        value["logistics_company"] = value.get("source_category", "")
        value["quantity"] = _text(value["payload"].get("quantity") or value["payload"].get("shortage"))
        # Current system safety blocks (label, lifecycle, handoff, shortage)
        # supersede an older employee wait note. The note remains visible when
        # there is no newer safety condition.
        value["blocked_by"] = _text(value["payload"].get("blocked_by")) or blocked_by
        value["read_only"] = _flag(value["payload"].get("read_only"))
        assignee = (staff or {}).get(value.get("assignee_id"), {})
        value["assignee_name"] = _text(assignee.get("display_name"))
        value["assignee"] = ({"id": value["assignee_id"], "display_name": value["assignee_name"]}
                             if value.get("assignee_id") else None)
        ref = (value.get("profile_id", ""), value.get("order_no", ""))
        value["print_state"] = (print_states or {}).get(ref, "")
        if (value.get("role") in PACKING_ROLES and
                value.get("task_type") == "whole_order_pack" and
                value.get("condition_state") == "active" and
                value["print_state"] == "uncertain" and
                value.get("status") in {"todo", "in_progress"}):
            # Print result changes can happen between ERP refreshes. Reflect
            # the unsafe state immediately in GET responses while the next
            # refresh persists the same waiting transition and audit event.
            value["status"] = "waiting"
            value["blocked_by"] = value["blocked_by"] or "打印结果无法确认，需要先核对是否出纸"
        value["packing_state"] = value["status"] if value.get("role") in PACKING_ROLES else ""
        return value

    def list(self, user, role="", view="mine"):
        if role and role not in ROLES:
            raise ValueError("岗位无效")
        if view not in {"mine", "role", "all"}:
            raise ValueError("任务视图无效")
        if view == "all" and not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以查看全部任务", "admin_required")
        allowed = _roles(user)
        if role and role not in allowed and not _is_admin(user):
            raise DailyWorkConflict("账号没有该岗位权限", "role_forbidden")
        today = self.today()
        with self.store.read_snapshot(), self.store.connect() as db:
            version = _text(_setting(db, "data_version", ""))
            print_state_version=_print_state_version(db)
            updated = _text(_setting(db, "data_source_updated_at", ""))
            task_version = int(db.execute("SELECT value FROM daily_work_settings WHERE key='task_version'").fetchone()[0])
            assignment_enabled = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='assignment_enabled'""").fetchone()[0]).lower() == "true"
            projection_version = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='projection_data_version'""").fetchone()[0])
            projection_updated = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='projection_source_updated_at'""").fetchone()[0])
            sql = "SELECT * FROM daily_work_tasks WHERE condition_state='active'"
            params = []
            if role:
                sql += " AND role=?"; params.append(role)
            elif not _is_admin(user):
                if not allowed:
                    sql += " AND 0"
                else:
                    sql += " AND role IN (%s)" % ",".join("?" for _ in allowed); params.extend(allowed)
            if view == "mine":
                if (not _is_admin(user) and "arrival_packing" in allowed and
                        (not role or role == "arrival_packing")):
                    sql += (" AND (assignee_id=? OR (assignee_id='' AND role='arrival_packing' "
                            "AND task_type=?))")
                    params.extend([_user_id(user), ARRIVAL_SHORTAGE_TASK])
                else:
                    sql += " AND (assignee_id=? OR role='sales')"; params.append(_user_id(user))
            rows = db.execute(sql, params).fetchall()
            staff_all = self._staff(db, today, include_off_duty=True)
            print_states = {}
            if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                AND name='order_print_states'""").fetchone():
                print_states = {(row["profile_id"], row["order_no"]): row["state"]
                                for row in db.execute("SELECT profile_id,order_no,state FROM order_print_states")}
            waiting_notes = {}
            for event in db.execute("""SELECT task_id,note FROM daily_work_events
                WHERE event_type='wait' ORDER BY created_at"""):
                waiting_notes[event["task_id"]] = event["note"]
            source = _published_order_groups(db, current_only=True)
            lifecycle_reviews = {ref: _lifecycle_review_reason(lines) for ref, lines in source.items()}
            from .customer_orders import CustomerOrders, groups_for_order
            customer_groups = CustomerOrders(self.store, self)._build(db)[0]
            items = []
            for row in rows:
                if not _current_task(row, source):
                    continue
                if row["role"] == "sales":
                    relevant = groups_for_order(customer_groups, row["profile_id"], row["order_no"])
                    principals = {identity for group in relevant if not group["customer_review"]
                                  for identity in (group["assignee_id"], group["deputy_id"]) if identity}
                    if not relevant:
                        from .sales_ownership import fixed_sales_owner
                        fixed = fixed_sales_owner(db, row["profile_id"], row["customer"])
                        if fixed:
                            principals.add(fixed)
                    if view == "mine" and _user_id(user) not in principals:
                        continue
                card = self._card(row, staff_all, print_states,
                    waiting_notes.get(row["id"], "") if row["status"] == "waiting" else "")
                review_reason = lifecycle_reviews.get((row["profile_id"], row["order_no"]), "")
                if row["role"] in PACKING_ROLES and review_reason:
                    card["status"] = "waiting"
                    card["packing_state"] = "waiting"
                    card["blocked_by"] = review_reason
                items.append(card)
            deferred = [item for item in items if item["status"] == "waiting" and
                        item["next_follow_up_date"] and item["next_follow_up_date"] > today]
            items.sort(key=lambda item:(bool(item in deferred and not item["urgent"] and (not item["due_date"] or item["due_date"] > today)), item["priority"], item["due_date"] or "9999-12-31",
                                        natural_location_key(item["location"]), item["business_key"]))
            if any(item["role"] == "purchase" for item in items):
                from .customer_orders import CustomerOrders, groups_for_order
                customer_groups = CustomerOrders(self.store, self)._build(db)[0]
                for item in items:
                    if item["role"] != "purchase":
                        continue
                    impacted = {}
                    for number in item["order_nos"]:
                        for group in groups_for_order(customer_groups, item["profile_id"], number):
                            impacted[group["id"]] = group
                    item["customer_order_impact"] = len(impacted)
                    item["near_complete_customer_orders"] = sum(
                        group["missing_product_count"] == 1 and not group["customer_review"] and
                        group["review_count"] == 0 and any(
                            missing["sku"] == item["sku"] and Decimal(missing["shortage"]) > 0
                            for missing in group["missing_items"])
                        for group in impacted.values())
            actor = _user_id(user)
            event_sql = """SELECT e.*,t.role,t.title,t.assignee_id FROM daily_work_events e
                JOIN daily_work_tasks t ON t.id=e.task_id WHERE substr(e.created_at,1,10)=?
                AND e.event_type IN ('start','progress','wait','resolve','packed','reopen')"""
            event_params = [today]
            if role:
                event_sql += " AND t.role=?"; event_params.append(role)
            if view == "mine":
                event_sql += " AND e.actor_id=?"; event_params.append(actor)
            today_event_rows = db.execute(event_sql + " ORDER BY e.created_at,e.id", event_params).fetchall()
            today_events = [{"id": event["id"], "task_id": event["task_id"],
                "event_type": event["event_type"], "actor_id": event["actor_id"],
                "role": event["role"], "title": event["title"], "note": event["note"],
                "created_at": event["created_at"]} for event in today_event_rows]
            complete_sql = """SELECT * FROM daily_work_tasks WHERE
                (substr(human_completed_at,1,10)=? OR substr(business_completed_at,1,10)=?)"""
            complete_params = [today, today]
            if role:
                complete_sql += " AND role=?"; complete_params.append(role)
            if view == "mine":
                complete_sql += " AND assignee_id=?"; complete_params.append(actor)
            completed_items = [self._card(row, staff_all, print_states)
                               for row in db.execute(complete_sql, complete_params)]
            action_count = len(today_events)
            completed_count = sum(bool(item["business_completed_at"]) for item in completed_items)
            staff_load = []
            if _is_admin(user):
                staff = staff_all
                load_rows = db.execute("""SELECT assignee_id,role,status,profile_id,order_no,payload
                    FROM daily_work_tasks WHERE condition_state='active' AND assignee_id<>''
                    """).fetchall()
                loads = defaultdict(lambda: defaultdict(int))
                for row in load_rows:
                    if not _current_task(row, source):
                        continue
                    loads[row["assignee_id"]][row["status"]] += 1
                    loads[row["assignee_id"]]["total"] += 1
                for identity, value in sorted(staff.items(), key=lambda pair:(
                        pair[1].get("display_name", ""), pair[0])):
                    staff_load.append({"user_id": identity,
                        "display_name": value.get("display_name", ""), "roles": value["roles"],
                        "on_duty": value["on_duty"],
                        "packing_limit": _packing_limit(value.get("packing_limit"), 3),
                        **dict(loads[identity])})
        summary = {
            "todo": sum(item["status"] == "todo" for item in items),
            "in_progress": sum(item["status"] == "in_progress" for item in items),
            "waiting": sum(item["status"] == "waiting" for item in items),
            "packed_waiting": sum(item["status"] == "packed" for item in items),
            "human_resolved": sum(item["status"] == "resolved" for item in items),
            "unassigned": sum(not item["assignee_id"] and not item["read_only"] for item in items),
            "actions_completed_today": action_count,
            "business_completed_today": completed_count,
            "total": len(items),
            "overdue": sum(bool(item["due_date"] and item["due_date"] < today)
                           for item in items),
            "deferred": len(deferred),
        }
        result = {"business_date": today, "data_version": version, "source_updated_at": updated,
                'print_state_version':print_state_version,
                "task_version": task_version, "stale": not _source_is_today(updated, today),
                "task_data_version": projection_version,
                "task_source_updated_at": projection_updated,
                "tasks_current": bool(version and version == projection_version and
                                      updated and updated == projection_updated),
                "assignment_enabled": assignment_enabled,
                "roles": allowed if not _is_admin(user) else list(ROLES), "summary": summary,
                "items": items, "completed_items": completed_items, "today_events": today_events}
        if _is_admin(user):
            result["staff_load"] = staff_load
        return result

    def version(self):
        """Return only projection/version metadata for inexpensive client polling."""
        today = self.today()
        with self.store.read_snapshot(), self.store.connect() as db:
            data_version = _text(_setting(db, "data_version", ""))
            print_state_version=_print_state_version(db)
            source_updated_at = _text(_setting(db, "data_source_updated_at", ""))
            task_version = int(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='task_version'""").fetchone()[0])
            task_data_version = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='projection_data_version'""").fetchone()[0])
            task_source_updated_at = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='projection_source_updated_at'""").fetchone()[0])
        return {
            "business_date": today,
            'print_state_version':print_state_version,
            "data_version": data_version,
            "source_updated_at": source_updated_at,
            "task_version": task_version,
            "task_data_version": task_data_version,
            "tasks_current": bool(data_version and data_version == task_data_version and
                                  source_updated_at and source_updated_at == task_source_updated_at),
        }

    def action(self, task_id, user, action, idempotency_key, version, note="", next_follow_up_date="", db=None):
        if action not in ACTION_STATUSES:
            raise ValueError("任务操作无效")
        key = _text(idempotency_key)
        if not key or len(key) > 128:
            raise ValueError("任务操作必须提供幂等标识")
        try:
            expected = int(version)
        except (TypeError, ValueError):
            raise ValueError("任务操作必须提供版本号") from None
        note = _text(note)
        if len(note) > 1000:
            raise ValueError("处理说明不能超过 1000 字")
        if action in {"progress", "wait", "resolve"} and not note:
            raise ValueError("请填写本次处理结果")
        actor = _user_id(user)
        today = self.today()
        follow_up = _day(next_follow_up_date)
        if action in {"progress", "wait"} and not follow_up:
            follow_up = _next_workday(today)
        with (self.store.connect(write=True) if db is None else nullcontext(db)) as db:
            replay = db.execute("SELECT * FROM daily_work_events WHERE task_id=? AND idempotency_key=?",
                                (task_id, key)).fetchone()
            if replay:
                if replay["event_type"] != action:
                    raise DailyWorkConflict("幂等标识已用于不同操作", "idempotency_conflict")
                row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
                return {"task": self._card(row, self._staff(db, today, include_off_duty=True)),
                        "replayed": True}
            row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            if row["version"] != expected:
                raise DailyWorkConflict("任务已经更新，请刷新后重试", "task_version_changed")
            if row["role"] in PACKING_ROLES or row["role"] == "sales":
                self._assert_projection_current(db)
            if row["condition_state"] != "active":
                raise DailyWorkConflict("任务来源条件已经解除，不能继续操作", "task_condition_resolved")
            if not _current_task(row, _published_order_groups(db, current_only=True)):
                raise DailyWorkConflict("订单已不在未交清单，请刷新任务", "task_condition_resolved")
            sales_principals = set()
            if row["role"] == "sales" and row["order_no"]:
                from .customer_orders import CustomerOrders, groups_for_order
                relevant = groups_for_order(CustomerOrders(self.store, self)._build(db)[0],
                                            row["profile_id"], row["order_no"])
                sales_principals = {identity for group in relevant for identity in
                                    (group["assignee_id"], group["deputy_id"]) if identity}
                if not relevant:
                    from .sales_ownership import fixed_sales_owner
                    fixed = fixed_sales_owner(db, row["profile_id"], row["customer"])
                    if fixed:
                        sales_principals.add(fixed)
                if not sales_principals or any(group["customer_review"] or group["owner_review"] for group in relevant):
                    raise DailyWorkConflict("客户或负责人归属待核对，请由管理员先确认", "customer_identity_review")
            if not _is_admin(user):
                if row["role"] not in _roles(user):
                    raise DailyWorkConflict("账号没有该岗位权限", "role_forbidden")
                current_staff = self._staff(db, today).get(actor)
                if not current_staff or row["role"] not in current_staff["roles"]:
                    raise DailyWorkConflict("账号当前未在岗或已失去该岗位权限", "role_forbidden")
            row_payload = _json(row["payload"], {})
            if _flag(row_payload.get("read_only")):
                raise DailyWorkConflict("该事项只用于显示缺项与关联采购，不能记录或完成包装操作",
                                        "task_read_only")
            if not _is_admin(user) and ((row["role"] == "sales" and actor not in sales_principals) or
                                       (row["role"] != "sales" and row["assignee_id"] != actor)):
                if row["role"] in PACKING_ROLES and not row["assignee_id"]:
                    raise DailyWorkConflict("未分配的包装任务必须由系统派工或管理员转交",
                                            "packing_assignment_required")
                raise DailyWorkConflict("任务未分配给当前员工，请刷新任务列表", "task_owner_conflict")
            if action == "packed" and row["role"] not in PACKING_ROLES:
                raise ValueError("只有包装任务可以确认已包装")
            if action == "packed":
                current_lines = _published_order_groups(db).get(
                    (row["profile_id"], row["order_no"]), [])
                lifecycle_reason = _lifecycle_review_reason(current_lines)
                if lifecycle_reason:
                    raise DailyWorkConflict(lifecycle_reason, "lifecycle_review_required")
            if action == "start" and row["role"] in PACKING_ROLES:
                current_lines = _published_order_groups(db).get((row["profile_id"], row["order_no"]), [])
                lifecycle_reason = _lifecycle_review_reason(current_lines)
                if lifecycle_reason:
                    raise DailyWorkConflict(lifecycle_reason, "lifecycle_review_required")
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_states'").fetchone():
                    print_row = db.execute("SELECT state FROM order_print_states WHERE profile_id=? AND order_no=?", (row["profile_id"], row["order_no"])).fetchone()
                    if print_row and print_row[0] in {"reserved", "uncertain"}:
                        raise DailyWorkConflict("打印仍在进行或结果待核对", "print_state_requires_review")
            label_review_satisfied = False
            if action == "packed" and db.execute("""SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='order_print_states'""").fetchone():
                print_row = db.execute("""SELECT state,fingerprint FROM order_print_states
                    WHERE profile_id=? AND order_no=?""",
                    (row["profile_id"], row["order_no"])).fetchone()
                if print_row and print_row["state"] in {"reserved", "uncertain"}:
                    raise DailyWorkConflict("打印仍在进行或出纸结果待核对，暂不能确认已包装",
                                            "print_state_requires_review")
                if _flag(row_payload.get("label_review_required")):
                    label_review_satisfied = bool(
                        print_row and print_row["state"] == "printed" and
                        _text(print_row["fingerprint"]) ==
                        _text(row_payload.get("label_fingerprint")))
                    if not label_review_satisfied:
                        raise DailyWorkConflict("订单标签内容已经变化，请先打印当前标签再确认包装",
                                                "label_review_required")
            if action == "resolve" and row["role"] in PACKING_ROLES:
                raise ValueError("包装任务必须使用已包装操作，不能直接标记解决")
            if action == "reopen" and not _is_admin(user):
                raise DailyWorkConflict("只有管理员可以重新打开任务", "admin_required")
            allowed_from = {
                "start": {"todo", "waiting", "in_progress"},
                "progress": {"todo", "in_progress", "waiting"},
                "wait": {"todo", "in_progress", "waiting"},
                "resolve": {"todo", "in_progress", "waiting"},
                "packed": {"todo", "in_progress"},
                "reopen": {"resolved", "packed"},
            }
            if row["status"] not in allowed_from[action]:
                raise DailyWorkConflict("当前任务状态不允许该操作，请刷新后核对",
                                        "task_transition_conflict")
            if action == "start" and row["status"] == "in_progress":
                return {"task": self._card(row, self._staff(db, today, include_off_duty=True)), "replayed": True}
            target = ACTION_STATUSES[action]
            assignee = row["assignee_id"] or actor
            human_completed = timestamp() if action == "resolve" else ("" if action == "reopen" else row["human_completed_at"])
            next_version = row["version"] + 1
            now = timestamp()
            payload = row_payload
            if label_review_satisfied:
                if _text(payload.get("blocked_by")) == LABEL_REVIEW_BLOCK:
                    payload.pop("blocked_by", None)
                payload.pop("label_review_required", None)
                payload.pop("status_before_label_review", None)
            if action in {"start", "progress", "reopen"}:
                payload.pop("blocked_by", None)
            db.execute("""UPDATE daily_work_tasks SET status=?,assignee_id=?,next_follow_up_date=?,
                human_completed_at=?,payload=?,version=?,updated_at=? WHERE id=?""",
                (target, assignee, follow_up, human_completed, dumps(payload),
                 next_version, now, task_id))
            db.execute("""INSERT INTO daily_work_events VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, task_id, action, actor, row["status"], target,
                note, follow_up, key, next_version, "{}", now))
            if action == "packed":
                self._activate_dispatch_follow_up(db, row, actor)
                self._fill_packing_slot(db, assignee, today)
            elif action == "reopen" and row["role"] in PACKING_ROLES:
                self._resolve_dispatch_follow_up(db, row, actor, "包装任务已重新打开")
            self._bump_version(db)
            updated = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
            return {"task": self._card(updated, self._staff(db, today, include_off_duty=True)), "replayed": False}

    def _staff_for_card(self):
        with self.store.connect() as db:
            return self._staff(db, self.today(), include_off_duty=True)

    def _activate_dispatch_follow_up(self, db, packing_task, actor):
        key = "sales:packed_waiting_dispatch:" + packing_task["order_no"]
        rows = db.execute("""SELECT * FROM daily_work_tasks WHERE business_key=?
            ORDER BY cycle""", (key,)).fetchall()
        previous = rows[-1] if rows else None
        if previous and previous["condition_state"] == "active":
            return previous["id"]
        cycle = previous["cycle"] + 1 if previous else 1
        payload = _json(packing_task["payload"], {})
        payload = {"order_nos": [packing_task["order_no"]],
                   "packing_task_id": packing_task["id"],
                   "label_fingerprint": payload.get("label_fingerprint", "")}
        candidate = {"role": "sales", "customer": packing_task["customer"], "sku": "",
                     "profile_id": packing_task["profile_id"], "source_category": packing_task["source_category"]}
        staff = self._staff(db, self.today())
        enabled = db.execute("SELECT value FROM daily_work_settings WHERE key='assignment_enabled'").fetchone()
        role_counts, packing_counts = self._counts(db)
        owner = self._choose_owner(db, candidate, staff, role_counts, packing_counts)
        identity, now = uuid.uuid4().hex, timestamp()
        fingerprint = _hash(payload)
        db.execute("""INSERT INTO daily_work_tasks(
            id,business_key,cycle,role,task_type,status,condition_state,assignee_id,
            profile_id,order_no,customer,sku,product_id,location,source_category,title,reason,next_action,
            due_date,next_follow_up_date,priority,urgent,source_data_version,source_updated_at,
            source_fingerprint,payload,version,first_seen_at,last_seen_at,condition_resolved_at,
            human_completed_at,business_completed_at,created_at,updated_at)
            VALUES (?,?,?,'sales','packed_waiting_dispatch','todo','active',?,?,?,?,?,?,?,?,?,?,?,?,
                    '',20,0,?,?,?,?,1,?,?, '', '', '',?,?)""",
            (identity, key, cycle, owner, packing_task["profile_id"], packing_task["order_no"],
             packing_task["customer"], "", "", packing_task["location"], packing_task["source_category"],
             f"已包装待发货 · {packing_task['order_no']}", "包装已经完成，等待 ERP 确认发货",
             "核对发货安排并跟进未发原因", packing_task["due_date"],
             packing_task["source_data_version"], packing_task["source_updated_at"], fingerprint,
             dumps(payload), now, now, now, now))
        db.execute("""INSERT INTO daily_work_events VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, identity, "created", actor, "", "todo",
            "包装确认后自动建立发货跟进", "", None, 1,
            dumps({"packing_task_id": packing_task["id"]}), now))
        return identity

    @staticmethod
    def _resolve_dispatch_follow_up(db, packing_task, actor, reason):
        now = timestamp()
        for row in db.execute("""SELECT * FROM daily_work_tasks WHERE role='sales'
            AND task_type='packed_waiting_dispatch' AND profile_id=? AND order_no=?
            AND condition_state='active'""", (packing_task["profile_id"], packing_task["order_no"])).fetchall():
            next_version = row["version"] + 1
            db.execute("""UPDATE daily_work_tasks SET condition_state='resolved',condition_resolved_at=?,
                version=?,updated_at=? WHERE id=?""", (now, next_version, now, row["id"]))
            db.execute("""INSERT INTO daily_work_events VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"], "condition_resolved",
                actor, row["status"], row["status"], reason, "", None, next_version, "{}", now))

    def _fill_packing_slot(self, db, actor, business_date):
        # Work is fully assigned by stable product identity; completion does not
        # release an artificial per-person display/dispatch slot.
        return

    def transfer(self, task_id, user, assignee_id, idempotency_key, version, reason):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以转交任务", "admin_required")
        assignee_id, reason, key = _text(assignee_id), _text(reason), _text(idempotency_key)
        if not assignee_id or not reason or not key:
            raise ValueError("转交必须提供员工、原因和幂等标识")
        try:
            expected = int(version)
        except (TypeError, ValueError):
            raise ValueError("任务转交必须提供版本号") from None
        with self.store.connect(write=True) as db:
            replay = db.execute("SELECT * FROM daily_work_events WHERE task_id=? AND idempotency_key=?",
                                (task_id, key)).fetchone()
            if replay:
                if replay["event_type"] != "transfer":
                    raise DailyWorkConflict("幂等标识已用于不同操作", "idempotency_conflict")
                return {"task": self._card(db.execute("SELECT * FROM daily_work_tasks WHERE id=?",
                    (task_id,)).fetchone(), self._staff(db, self.today(), include_off_duty=True)),
                    "replayed": True}
            task = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError("任务不存在")
            if task["role"] == "sales":
                raise DailyWorkConflict("销售任务统一使用客户固定销售，请在客户设置修改或设置临时代办", "customer_owner_scope_required")
            if task["version"] != expected:
                raise DailyWorkConflict("任务已经更新，请刷新后重试", "task_version_changed")
            if _flag(_json(task["payload"], {}).get("read_only")):
                raise DailyWorkConflict("只读缺项事项不允许转交", "task_read_only")
            if task["role"] in PACKING_ROLES and db.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='order_print_states'""").fetchone():
                print_row = db.execute("""SELECT state FROM order_print_states
                    WHERE profile_id=? AND order_no=?""",
                    (task["profile_id"], task["order_no"])).fetchone()
                if (print_row and (print_row["state"] == "reserved" or
                        (print_row["state"] == "uncertain" and
                         task["task_type"] != "print_history_review"))):
                    raise DailyWorkConflict("订单正在打印或出纸结果待核对，暂不能转交",
                                            "print_handoff_conflict")
            staff = self._staff(db, self.today()).get(assignee_id)
            if not staff or task["role"] not in staff["roles"]:
                raise ValueError("目标员工未在岗或没有该岗位")
            next_version, now = task["version"] + 1, timestamp()
            payload = _json(task["payload"], {})
            if _text(payload.get("blocked_by")).startswith("原负责人当前不可用"):
                payload.pop("blocked_by", None)
            db.execute("""UPDATE daily_work_tasks SET assignee_id=?,payload=?,version=?,updated_at=?
                WHERE id=?""", (assignee_id, dumps(payload), next_version, now, task_id))
            db.execute("""INSERT INTO daily_work_events VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, task_id, "transfer", _user_id(user),
                task["status"], task["status"], reason, task["next_follow_up_date"], key,
                next_version, dumps({"from": task["assignee_id"], "to": assignee_id}), now))
            self._bump_version(db)
            updated = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
        return {"task": self._card(updated, self._staff_for_card()), "replayed": False}

    def assert_print_access(self, user, order_nos, profile_id="", db=None,
                            allow_review=False):
        """Require every whole order to belong to the current packaging worker.

        PrintJobs should pass its ``BEGIN IMMEDIATE`` connection at claim time,
        closing the gap between an earlier HTTP check and reservation.
        """
        if _is_admin(user):
            # Resetting an unsafe/unknown print state is a recovery action and
            # remains available to administrators even while a new task
            # projection is pending. Starting a new print still requires the
            # task projection to match the published data generation.
            if not allow_review:
                if db is not None:
                    self._assert_projection_current(db)
                else:
                    with self.store.connect() as database:
                        self._assert_projection_current(database)
            return True
        actor = _user_id(user)
        if not PACKING_ROLES.intersection(_roles(user)):
            raise DailyWorkConflict("账号当前没有包装岗位权限", "role_forbidden")
        wanted = {_text(value) for value in order_nos if _text(value)}
        if not wanted:
            raise DailyWorkConflict("打印任务缺少整单订单号", "daily_work_order_required")
        def validate(database):
            self._assert_projection_current(database)
            staff = self._staff(database, self.today(), include_off_duty=True).get(actor)
            if (not staff or not staff["on_duty"] or
                    not PACKING_ROLES.intersection(staff["roles"])):
                raise DailyWorkConflict("账号当前没有在岗包装岗位权限", "role_forbidden")
            allowed_statuses = ("todo", "in_progress", "waiting", "packed", "resolved") if allow_review else (
                "todo", "in_progress")
            placeholders = ",".join("?" for _ in allowed_statuses)
            rows = database.execute(f"""SELECT profile_id,order_no,assignee_id,status,task_type
                FROM daily_work_tasks
                WHERE role IN ('stock_packing','arrival_packing') AND condition_state='active'
                  AND status IN ({placeholders})""", allowed_statuses).fetchall()
            allowed_types = {"whole_order_pack", "print_history_review"} if allow_review else {
                "whole_order_pack"}
            allowed = {row["order_no"] for row in rows if row["assignee_id"] == actor and
                       row["task_type"] in allowed_types and
                       (not profile_id or row["profile_id"] == profile_id)}
            missing = sorted(wanted - allowed)
            if missing:
                raise DailyWorkConflict(f"订单 {missing[0]} 未分配给当前包装人员", "daily_work_owner_required")
        if db is not None:
            validate(db)
        else:
            with self.store.connect() as database:
                validate(database)
        return True

    def assert_reprint_access(self, user, order_nos, profile_id="", db=None):
        """A confirmed print preview may reuse an owned historical print task.

        Review confirmation only relaxes the print-result block. It cannot
        bypass source, identity, label-change, or shortage safety restrictions.
        """
        actor = _user_id(user)
        admin = _is_admin(user)
        wanted = {_text(n) for n in order_nos if _text(n)}
        if not wanted:
            raise DailyWorkConflict("打印任务缺少整单订单号", "daily_work_order_required")
        def validate(database):
            self._assert_projection_current(database)
            current_staff = self._staff(database, self.today(), include_off_duty=True)
            if not admin:
                staff = current_staff.get(actor)
                if (not PACKING_ROLES.intersection(_roles(user)) or not staff or
                        not staff['on_duty'] or not PACKING_ROLES.intersection(staff['roles'])):
                    raise DailyWorkConflict("账号当前没有在岗包装岗位权限", "role_forbidden")
            rows = database.execute("""SELECT * FROM daily_work_tasks
                WHERE role IN ('stock_packing','arrival_packing') AND condition_state='active'
                  AND status IN ('todo','in_progress','waiting','packed','resolved')""").fetchall()
            allowed = set()
            for row in rows:
                if row['order_no'] not in wanted or (profile_id and row['profile_id'] != profile_id):
                    continue
                if not admin and row['assignee_id'] != actor:
                    continue
                owner = current_staff.get(row['assignee_id'])
                if not owner or not owner['on_duty'] or row['role'] not in owner['roles']:
                    raise DailyWorkConflict(f"订单 {row['order_no']} 的当前包装人员不在岗或无岗位权限", 'daily_work_owner_required')
                value = _json(row['payload'], {})
                if row['task_type'] not in {'whole_order_pack', 'print_history_review'}:
                    continue
                if _flag(value.get('read_only')) or _text(value.get('blocked_by')) not in {'', PRINT_REVIEW_BLOCK}:
                    raise DailyWorkConflict(f"订单 {row['order_no']} 仍有来源或业务异常，请先核对", 'task_read_only')
                allowed.add(row['order_no'])
            missing = sorted(wanted - allowed)
            if missing:
                raise DailyWorkConflict(f"订单 {missing[0]} 未分配给当前包装人员或已结束", 'daily_work_owner_required')
        if db is not None:
            validate(db)
        else:
            with self.store.connect() as database:
                validate(database)
        return True

    def assert_print_reset_access(self, user, order_nos, profile_id="", db=None):
        """Allow the current worker to recover an assigned print-review task."""
        return self.assert_print_access(user, order_nos, profile_id, db=db,
                                        allow_review=True)

    def attendance(self, user, business_date=""):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以查看在岗安排", "admin_required")
        target = _day(business_date) or self.today()
        with self.store.connect() as db:
            staff = self._staff(db, target, include_off_duty=True)
        return {"business_date": target, "items": [{
            "user_id": value["id"], "username": value.get("username", ""),
            "display_name": value.get("display_name", ""), "roles": value["roles"],
            "on_duty": value["on_duty"], "present": value["on_duty"],
            "packing_limit": _packing_limit(value.get("packing_limit"), 3),
        } for value in sorted(staff.values(), key=lambda row:(row.get("display_name", ""), row["id"]))]}

    def set_attendance(self, user, user_id, on_duty, business_date=""):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以修改在岗安排", "admin_required")
        if not isinstance(on_duty, bool):
            raise ValueError("在岗状态必须为布尔值")
        target, identity = _day(business_date) or self.today(), _text(user_id)
        if not identity:
            raise ValueError("缺少员工标识")
        with self.store.connect(write=True) as db:
            staff = self._staff(db, target, include_off_duty=True)
            if identity not in staff:
                raise KeyError("员工不存在或已停用")
            db.execute("""INSERT INTO daily_work_attendance VALUES (?,?,?,?,?)
                ON CONFLICT(business_date,user_id) DO UPDATE SET
                    on_duty=excluded.on_duty,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (target, identity, int(on_duty), _user_id(user), timestamp()))
            self._bump_version(db)
        reconciled = self.reconcile_staff_assignments(user=user)
        return {**self.attendance(user, target), "reconcile": reconciled}

    def reconcile_staff_assignments(self, user=None, actor_id=""):
        """Release safe orphaned work and flag started work for handoff."""
        if user is not None and not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以核对人员派工", "admin_required")
        actor = _user_id(user) if user is not None else (_text(actor_id) or "system:staff_reconcile")
        today, now = self.today(), timestamp()
        released = handoff = assigned = 0
        with self.store.connect(write=True) as db:
            all_staff = self._staff(db, today, include_off_duty=True)
            available = self._staff(db, today)
            print_states = {}
            if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                AND name='order_print_states'""").fetchone():
                print_states = {(row["profile_id"], row["order_no"]): row["state"]
                                for row in db.execute("SELECT profile_id,order_no,state FROM order_print_states")}
            changed = False
            rows = db.execute("""SELECT * FROM daily_work_tasks
                WHERE condition_state='active' AND assignee_id<>''""").fetchall()
            for row in rows:
                staff = (all_staff if row["role"] == "sales" else available).get(row["assignee_id"])
                valid = bool(staff and row["role"] in staff["roles"])
                if valid:
                    continue
                printed = print_states.get((row["profile_id"], row["order_no"]), "unprinted")
                safely_releasable = row["status"] == "todo" and printed == "unprinted" and not _task_has_work_history(db, row)
                next_version = row["version"] + 1
                if safely_releasable and row["role"] != "sales":
                    db.execute("""UPDATE daily_work_tasks SET assignee_id='',version=?,updated_at=?
                        WHERE id=?""", (next_version, now, row["id"]))
                    db.execute("""INSERT INTO daily_work_events VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"],
                        "assignment_released", actor, row["status"], row["status"],
                        "员工离岗、停用或岗位已移除，任务退回待安排", row["next_follow_up_date"],
                        None, next_version, dumps({"previous_assignee": row["assignee_id"]}), now))
                    released += 1
                else:
                    payload = _json(row["payload"], {})
                    message = "原负责人当前不可用，已开始或已打印任务需要管理员交接"
                    if payload.get("blocked_by") == message:
                        continue
                    payload["blocked_by"] = message
                    db.execute("""UPDATE daily_work_tasks SET payload=?,version=?,updated_at=? WHERE id=?""",
                               (dumps(payload), next_version, now, row["id"]))
                    db.execute("""INSERT INTO daily_work_events VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"],
                        "staff_handoff_required", actor, row["status"], row["status"], message,
                        row["next_follow_up_date"], None, next_version,
                        dumps({"assignee_id": row["assignee_id"], "print_state": printed}), now))
                    handoff += 1
                changed = True
            enabled = db.execute("SELECT value FROM daily_work_settings WHERE key='assignment_enabled'").fetchone()
            if enabled and _text(enabled[0]).lower() == "true":
                role_counts, packing_counts = self._counts(db)
                rows = db.execute("""SELECT * FROM daily_work_tasks WHERE condition_state='active'
                    AND assignee_id='' AND status='todo'""").fetchall()
                rows = sorted(rows, key=lambda row:(row["priority"], row["due_date"] or "9999-12-31",
                    natural_location_key(row["location"]), row["business_key"]))
                for row in rows:
                    if row["task_type"] == "print_history_review" or _flag(_json(row["payload"], {}).get("read_only")):
                        continue
                    if row["role"] in PACKING_ROLES and not _source_is_today(row["source_updated_at"], today):
                        continue
                    candidate = dict(row)
                    owner = self._choose_owner(db, candidate,
                        all_staff if row["role"] == "sales" else available,
                        role_counts, packing_counts)
                    if not owner:
                        continue
                    next_version = row["version"] + 1
                    db.execute("""UPDATE daily_work_tasks SET assignee_id=?,version=?,updated_at=?
                        WHERE id=?""", (owner, next_version, now, row["id"]))
                    db.execute("""INSERT INTO daily_work_events VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, row["id"],
                        "auto_assigned", actor, row["status"], row["status"], "人员状态核对后自动派工",
                        row["next_follow_up_date"], None, next_version, dumps({"assignee_id": owner}), now))
                    role_counts[(owner, row["role"])] += 1
                    if row["role"] in PACKING_ROLES:
                        packing_counts[owner] += 1
                    assigned += 1
                    changed = True
            if changed:
                self._bump_version(db)
            assigned += self.reconcile_product_assignments(actor_id=actor, db=db)["assigned"]
        return {"released": released, "handoff_required": handoff, "assigned": assigned}

    def reconcile_product_assignments(self, actor_id="system:product_assignment", db=None, existing_task_ids=None):
        """Regroup only untouched packaging work in the same write transaction.

        This migrates dispatch ownership of an already-published calculation;
        it never reallocates stock or fetches ERP data. Date staleness is still
        reported by the board and new source tasks follow refresh safeguards.
        """
        with (self.store.connect(write=True) if db is None else nullcontext(db)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='packing_product_owners'").fetchone():
                return {"assigned": 0}
            enabled = db.execute("SELECT value FROM daily_work_settings WHERE key='assignment_enabled'").fetchone()
            if not enabled or enabled[0] != "true":
                return {"assigned": 0}
            try:
                self._assert_projection_current(db)
            except DailyWorkConflict:
                return {"assigned": 0, "current": False}
            staff = self._staff(db, self.today())
            records = _published_order_groups(db)
            rows = db.execute("SELECT * FROM daily_work_tasks WHERE role IN ('stock_packing','arrival_packing') AND condition_state='active'").fetchall()
            protected_events = {row[0] for row in db.execute("""SELECT DISTINCT task_id FROM daily_work_events
                WHERE event_type NOT IN ('created','source_updated','auto_assigned','product_regrouped','assignment_released')""")}
            historical_prints = set()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_audit'").fetchone():
                historical_prints.update((row[0], row[1]) for row in db.execute("SELECT DISTINCT profile_id,order_no FROM order_print_audit"))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='order_print_states'").fetchone():
                historical_prints.update((row[0], row[1]) for row in db.execute("SELECT profile_id,order_no FROM order_print_states WHERE state<>'unprinted'"))
            groups = defaultdict(list)
            safe_ids = set()
            for row in rows:
                source = records.get((row['profile_id'], row['order_no']), [])
                if not source:
                    continue
                identities = sorted({_record_identity(item) for item in source})
                if any(not all(_text(item.get(k)) for k in ('profile_id','warehouse_id','product_id','sku_id','base_unit')) for item in source):
                    continue
                payload = _json(row['payload'], {})
                key = _hash([row['role'], identities]) if len(identities) == 1 else _hash([row['role'], 'combination', row['profile_id'], row['order_no']])
                groups[key].append(row)
                if ((existing_task_ids is None or row['id'] in existing_task_ids) and row['task_type'] == 'whole_order_pack' and row['status'] == 'todo' and
                        row['id'] not in protected_events and (row['profile_id'],row['order_no']) not in historical_prints and
                        not payload.get('blocked_by') and not _flag(payload.get('read_only'))):
                    safe_ids.add(row['id'])
            loads, details = defaultdict(int), defaultdict(int)
            for row in rows:
                if row['assignee_id'] and row['status'] in {'todo','in_progress','waiting'} and row['id'] not in safe_ids:
                    loads[row['assignee_id']] += 1
                    details[row['assignee_id']] += int(_json(row['payload'], {}).get('line_count', 1) or 1)
            count, now = 0, timestamp()
            keys = sorted(groups, key=lambda key: min((row['priority'], row['due_date'] or '9999-12-31', natural_location_key(row['location']), key) for row in groups[key]))
            for key in keys:
                members = groups[key]
                role = members[0]['role']
                eligible = {uid: person for uid, person in staff.items() if role in person['roles'] and not _flag(person.get('is_admin'))}
                safe = [row for row in members if row['id'] in safe_ids]
                if not safe or not eligible:
                    continue
                previous = db.execute("SELECT assignee_id FROM packing_product_owners WHERE group_key=?", (key,)).fetchone()
                owner = previous[0] if previous and previous[0] in eligible else ''
                protected_owners = {row['assignee_id'] for row in members if row['id'] not in safe_ids and row['assignee_id'] in eligible}
                if not owner and protected_owners:
                    owner = min(protected_owners, key=lambda uid: (loads[uid], details[uid], uid))
                if not owner:
                    existing_owners = defaultdict(int)
                    for row in members:
                        if row['assignee_id'] in eligible:
                            existing_owners[row['assignee_id']] += 1
                    if existing_owners:
                        owner = min(existing_owners, key=lambda uid: (-existing_owners[uid], loads[uid], uid))
                if not owner:
                    lowest = min((loads[uid], details[uid]) for uid in eligible)
                    tied = sorted(uid for uid in eligible if (loads[uid], details[uid]) == lowest)
                    cursor_key = 'round_robin:' + role
                    cursor_row = db.execute("SELECT value FROM daily_work_settings WHERE key=?", (cursor_key,)).fetchone()
                    cursor = int(cursor_row[0]) if cursor_row else 0
                    owner = tied[cursor % len(tied)]
                    db.execute("INSERT INTO daily_work_settings VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (cursor_key,str(cursor+1),now))
                db.execute("INSERT INTO packing_product_owners VALUES (?,?,?,?) ON CONFLICT(group_key) DO UPDATE SET assignee_id=excluded.assignee_id,updated_at=excluded.updated_at", (key,role,owner,now))
                for row in safe:
                    loads[owner] += 1
                    details[owner] += int(_json(row['payload'], {}).get('line_count', 1) or 1)
                    if row['assignee_id'] == owner:
                        continue
                    version = row['version'] + 1
                    db.execute("UPDATE daily_work_tasks SET assignee_id=?,version=?,updated_at=? WHERE id=?", (owner,version,now,row['id']))
                    db.execute("INSERT INTO daily_work_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex,row['id'],'product_regrouped',actor_id,row['status'],row['status'],
                         '同品整单归并派工',row['next_follow_up_date'],None,version,
                         dumps({'from':row['assignee_id'],'to':owner,'group_key':key}),now))
                    count += 1
            if count:
                self._bump_version(db)
            return {"assigned": count, "current": True}

    def settings(self, user):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以查看任务设置", "admin_required")
        with self.store.connect() as db:
            enabled = _text(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='assignment_enabled'""").fetchone()[0]).lower() == "true"
            packing_limit = int(db.execute("""SELECT value FROM daily_work_settings
                WHERE key='packing_limit_default'""").fetchone()[0])
        return {"assignment_enabled": enabled, "packing_limit_default": packing_limit}

    def set_settings(self, user, assignment_enabled):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以修改任务设置", "admin_required")
        if not isinstance(assignment_enabled, bool):
            raise ValueError("自动派工开关必须为布尔值")
        with self.store.connect(write=True) as db:
            db.execute("""UPDATE daily_work_settings SET value=?,updated_at=?
                WHERE key='assignment_enabled'""",
                ("true" if assignment_enabled else "false", timestamp()))
            self._bump_version(db)
        # Enabling is explicit authorization to fill current safe candidates.
        refresh = self.refresh(actor_id=_user_id(user)) if assignment_enabled else None
        return {**self.settings(user), "refresh": refresh}

    def assignment_rules(self, user):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以查看负责人规则", "admin_required")
        with self.store.connect() as db:
            rows = db.execute("""SELECT r.*,u.display_name FROM daily_work_assignment_rules r
                LEFT JOIN staff_users u ON u.id=r.staff_id WHERE r.role<>'sales'
                ORDER BY r.role,r.priority,r.match_value""").fetchall()
            items = [{**dict(row), "active": bool(row["active"])} for row in rows]
            for row in db.execute("""SELECT o.*,u.display_name FROM sales_customer_owners o
                LEFT JOIN staff_users u ON u.id=o.owner_id ORDER BY o.customer_name,o.profile_id"""):
                from .sales_ownership import fixed_sales_owner
                owner = fixed_sales_owner(db, row["profile_id"], row["customer_name"])
                items.append({"id":"fixed-sales:" + _hash([row["profile_id"],row["customer_name"]]),
                    "role":"sales", "match_type":"customer", "match_value":row["customer_name"],
                    "staff_id":owner, "display_name":row["display_name"] if owner else "待指定",
                    "profile_id":row["profile_id"], "priority":100, "active":bool(owner),
                    "created_at":row["updated_at"], "updated_at":row["updated_at"], "legacy_read_only":True})
        return {"items": items}

    def set_assignment_rule(self, user, role, match_type, match_value, staff_id, priority=100):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以修改负责人规则", "admin_required")
        role, match_type = _text(role), _text(match_type)
        if role == "sales":
            raise DailyWorkConflict("请升级客户端，在客户固定销售中确认整客户归属", "client_upgrade_required")
        match_value, staff_id = _text(match_value), _text(staff_id)
        allowed = {"sales": "customer", "purchase": "sku"}
        if role not in allowed or match_type != allowed[role]:
            raise ValueError("销售仅支持客户负责人规则，采购仅支持商品负责人规则")
        if not match_value or not staff_id:
            raise ValueError("负责人规则缺少匹配值或员工")
        if match_type == "sku":
            match_value = match_value.upper()
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise ValueError("规则优先级必须为整数") from None
        with self.store.connect(write=True) as db:
            staff = self._staff(db, self.today(), include_off_duty=True).get(staff_id)
            if not staff or role not in staff["roles"]:
                raise ValueError("目标员工已停用或没有对应岗位")
            old = db.execute("""SELECT id FROM daily_work_assignment_rules
                WHERE role=? AND match_type=? AND match_value=?""",
                (role, match_type, match_value)).fetchone()
            identity, now = old[0] if old else uuid.uuid4().hex, timestamp()
            db.execute("""INSERT INTO daily_work_assignment_rules
                (id,role,match_type,match_value,staff_id,priority,active,created_at,updated_at)
                VALUES (?,?,?,?,?,?,1,?,?) ON CONFLICT(role,match_type,match_value) DO UPDATE SET
                staff_id=excluded.staff_id,priority=excluded.priority,active=1,updated_at=excluded.updated_at""",
                (identity, role, match_type, match_value, staff_id, priority, now, now))
            self._bump_version(db)
        return self.assignment_rules(user)

    def delete_assignment_rule(self, user, rule_id):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以删除负责人规则", "admin_required")
        if _text(rule_id).startswith("fixed-sales:"):
            raise DailyWorkConflict("请升级客户端，在客户固定销售中修改归属", "client_upgrade_required")
        with self.store.connect(write=True) as db:
            old = db.execute("SELECT role FROM daily_work_assignment_rules WHERE id=?", (_text(rule_id),)).fetchone()
            if old and old["role"] == "sales":
                raise DailyWorkConflict("请升级客户端，在客户固定销售中修改归属", "client_upgrade_required")
            cursor = db.execute("DELETE FROM daily_work_assignment_rules WHERE id=?", (_text(rule_id),))
            if not cursor.rowcount:
                raise KeyError("负责人规则不存在")
            self._bump_version(db)
        return {"deleted": True, "id": _text(rule_id)}

    def set_urgent(self, task_id, user, urgent, idempotency_key, version, reason=""):
        if not _is_admin(user):
            raise DailyWorkConflict("只有管理员可以调整急单", "admin_required")
        if not isinstance(urgent, bool):
            raise ValueError("急单状态必须为布尔值")
        key, reason = _text(idempotency_key), _text(reason)
        if not key or not reason:
            raise ValueError("调整急单必须提供幂等标识和原因")
        try:
            expected = int(version)
        except (TypeError, ValueError):
            raise ValueError("调整急单必须提供版本号") from None
        with self.store.connect(write=True) as db:
            replay = db.execute("SELECT * FROM daily_work_events WHERE task_id=? AND idempotency_key=?",
                                (task_id, key)).fetchone()
            if replay:
                if replay["event_type"] != "urgent":
                    raise DailyWorkConflict("幂等标识已用于不同操作", "idempotency_conflict")
                return {"task": self._card(db.execute("SELECT * FROM daily_work_tasks WHERE id=?",
                    (task_id,)).fetchone(), self._staff(db, self.today(), include_off_duty=True)),
                    "replayed": True}
            row = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            if row["version"] != expected:
                raise DailyWorkConflict("任务已经更新，请刷新后重试", "task_version_changed")
            payload = _json(row["payload"], {})
            if "base_priority" not in payload:
                payload["base_priority"] = row["priority"]
            priority = 0 if urgent else int(payload.get("base_priority", 50))
            next_version, now = row["version"] + 1, timestamp()
            db.execute("""UPDATE daily_work_tasks SET urgent=?,priority=?,payload=?,version=?,updated_at=?
                WHERE id=?""", (int(urgent), priority, dumps(payload), next_version, now, task_id))
            db.execute("""INSERT INTO daily_work_events VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, task_id, "urgent", _user_id(user),
                row["status"], row["status"], reason, row["next_follow_up_date"], key,
                next_version, dumps({"urgent": urgent}), now))
            self._bump_version(db)
            updated = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
        return {"task": self._card(updated, self._staff_for_card()), "replayed": False}


def daily_work_routes(engine, after_settings=None, after_refresh=None):
    """Return routes; the app factory owns registration and global auth boundary."""
    bp = Blueprint("daily_work", __name__)

    def response(call):
        try:
            return jsonify(ok=True, **call())
        except DailyWorkConflict as error:
            return jsonify(ok=False, error=error.code, message=str(error)), 409 if error.code.endswith(
                ("conflict", "changed", "reached")) else 403
        except KeyError as error:
            return jsonify(ok=False, error="not_found", message=str(error).strip("'")), 404
        except (ValueError, TypeError) as error:
            return jsonify(ok=False, error="invalid_request", message=str(error)), 400

    @bp.get("/api/v2/daily-work")
    def list_tasks():
        return response(lambda: engine.list(_current_user(), request.args.get("role", ""),
                                            request.args.get("view", "mine")))

    @bp.get("/api/v2/daily-work/version")
    def task_version():
        _current_user()
        return response(engine.version)

    @bp.post("/api/v2/daily-work/refresh")
    def refresh_tasks():
        def run():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            user = _current_user()
            if not _is_admin(user):
                raise DailyWorkConflict("只有管理员可以刷新任务", "admin_required")
            result = engine.refresh(idempotency_key=body.get("idempotency_key"),
                                    actor_id=_user_id(user))
            if after_refresh:
                after_refresh()
            return {"refresh": result}
        return response(run)

    @bp.post("/api/v2/daily-work/tasks/<task_id>/actions")
    def task_action(task_id):
        def run():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            return engine.action(task_id, _current_user(), body.get("action"),
                                 body.get("idempotency_key"), body.get("version"),
                                 body.get("note", ""), body.get("next_follow_up_date", ""))
        return response(run)

    @bp.post("/api/v2/daily-work/tasks/<task_id>/transfer")
    def transfer_task(task_id):
        def run():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            return engine.transfer(task_id, _current_user(), body.get("assignee_id"),
                                   body.get("idempotency_key"), body.get("version"),
                                   body.get("reason", ""))
        return response(run)

    @bp.get("/api/v2/daily-work/attendance")
    def get_attendance():
        return response(lambda: engine.attendance(_current_user(), request.args.get("business_date", "")))

    @bp.put("/api/v2/daily-work/attendance")
    def put_attendance():
        def run():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            present = body.get("on_duty") if "on_duty" in body else body.get("present")
            return engine.set_attendance(_current_user(), body.get("user_id"), present,
                                         body.get("business_date", ""))
        return response(run)

    @bp.get("/api/v2/daily-work/settings")
    def get_daily_settings():
        return response(lambda: engine.settings(_current_user()))

    @bp.put("/api/v2/daily-work/settings")
    def put_daily_settings():
        def run():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            result = engine.set_settings(_current_user(), body.get("assignment_enabled"))
            if after_settings:
                after_settings()
            return result
        return response(run)

    @bp.post("/api/v2/daily-work/reconcile-staff")
    def reconcile_staff():
        return response(lambda: {"reconcile": engine.reconcile_staff_assignments(
            user=_current_user())})

    @bp.get("/api/v2/daily-work/assignment-rules")
    def get_assignment_rules():
        return response(lambda: engine.assignment_rules(_current_user()))

    @bp.put("/api/v2/daily-work/assignment-rules")
    def put_assignment_rule():
        def run():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            return engine.set_assignment_rule(_current_user(), body.get("role"),
                body.get("match_type"), body.get("match_value"), body.get("staff_id"),
                body.get("priority", 100))
        return response(run)

    @bp.delete("/api/v2/daily-work/assignment-rules/<rule_id>")
    def delete_assignment_rule(rule_id):
        return response(lambda: engine.delete_assignment_rule(_current_user(), rule_id))

    @bp.post("/api/v2/daily-work/tasks/<task_id>/urgent")
    def urgent_task(task_id):
        def run():
            body = request.get_json(silent=True) or {}
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            return engine.set_urgent(task_id, _current_user(), body.get("urgent"),
                body.get("idempotency_key"), body.get("version"), body.get("reason", ""))
        return response(run)

    return bp
