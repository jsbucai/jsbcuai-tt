"""Opt-in purchase execution with durable intents and read-before-retry recovery.

This module has no browser, scheduler, HTTP route, credentials, or production
adapter. ``inspect`` and ``lookup`` MUST be read-only. An adapter may implement
each write method as exactly one irreversible action; internal write retries,
coordinate fallbacks, split recovery, and automatic product substitutions are
outside this contract. A previously persisted intent is NEVER executed again.

Only one item and the exact frozen shortage are supported. Unknown outcomes
stay unknown even if a later lookup finds no order: absence is not permission
to submit or pay again. ERP recording is a separate confirmed/read-back stage.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from time import time
from typing import Protocol
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ExecutionPolicy:
    enabled: bool = False
    allow_order_submit: bool = False
    allow_payment: bool = False
    account_id: str = ""
    allowed_platforms: tuple[str, ...] = ("淘钉钉",)
    max_quote_age_seconds: int = 300
    max_source_age_seconds: int = 86400


@dataclass(frozen=True)
class AdapterCapabilities:
    platform: str
    account_id: str
    account_verified: bool = False
    complete_order_lookup: bool = False
    durable_order_binding: bool = False
    order_submission_is_unpaid: bool = False
    order_submit_enabled: bool = False
    payment_enabled: bool = False


@dataclass(frozen=True)
class PurchaseRequest:
    attempt_id: str
    job_id: str
    plan_id: str
    task_id: str
    profile_id: str
    erp_code: str
    product_identity: str
    platform: str
    platform_sku: str
    quantity: str
    unit: str
    price_limit: str
    price_source_hash: str
    whitelist_source_hash: str
    checks: tuple[tuple[str, str], ...]
    checkout_reference: str = ""


@dataclass(frozen=True)
class PreparedPurchase:
    platform: str
    account_id: str
    checkout_reference: str
    platform_sku: str
    erp_code: str
    product_identity: str
    unit: str
    quantity: str
    package_size: str
    package_count: str
    package_unit: str
    inclusive_unit_price: str
    line_amount: str
    shipping_amount: str
    fee_amount: str
    payable_amount: str
    tax_basis: str
    price_source_hash: str
    observed_at: float
    checks: tuple[tuple[str, str], ...]
    platform_unit: str = ""
    platform_quantity: str = ""
    base_units_per_platform_unit: str = ""
    unit_conversion_evidence_hash: str = ""
    currency: str = ""


@dataclass(frozen=True)
class ExecutionIntent:
    id: str
    attempt_id: str
    phase: str
    request: PurchaseRequest
    prepared: PreparedPurchase
    platform_order_no: str = ""


@dataclass(frozen=True)
class PlatformOrder:
    order_no: str
    platform: str
    account_id: str
    checkout_reference: str
    platform_sku: str
    erp_code: str
    product_identity: str
    unit: str
    quantity: str
    payable_amount: str
    paid_amount: str
    status: str  # unpaid | paid | cancelled | rejected
    evidence_hash: str
    observed_at: float
    platform_unit: str = ""
    platform_quantity: str = ""
    base_units_per_platform_unit: str = ""
    unit_conversion_evidence_hash: str = ""
    currency: str = ""


@dataclass(frozen=True)
class OrderLookup:
    complete: bool
    account_id: str
    checkout_reference: str
    orders: tuple[PlatformOrder, ...] = ()


class PurchaseAdapter(Protocol):
    capabilities: AdapterCapabilities

    def inspect(self, request: PurchaseRequest) -> PreparedPurchase: ...

    def lookup(self, intent: ExecutionIntent) -> OrderLookup: ...

    def submit_order(self, intent: ExecutionIntent) -> None: ...

    def pay_order(self, intent: ExecutionIntent) -> None: ...


class ExecutionBlocked(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _number(value, *, positive=False):
    if isinstance(value, bool) or value is None or len(str(value)) > 80:
        raise ExecutionBlocked("number_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ExecutionBlocked("number_invalid") from None
    if (not result.is_finite() or result < 0 or (positive and not result)
            or result.adjusted() > 15 or result.as_tuple().exponent < -12):
        raise ExecutionBlocked("number_invalid")
    return result


def _valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _prepared(value):
    value = dict(value)
    value["checks"] = tuple(tuple(pair) for pair in value["checks"])
    return PreparedPurchase(**value)


class PurchaseExecution:
    def __init__(self, store, adapter: PurchaseAdapter, policy=None, *, clock=time):
        self.store, self.adapter = store, adapter
        self.policy = policy or ExecutionPolicy()
        self.clock = clock
        # Additive tables only; the existing intake and job contract is kept.
        with store.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS purchase_execution_attempts(
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, plan_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL, profile_id TEXT NOT NULL, frozen_hash TEXT NOT NULL,
                frozen TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                prepared TEXT NOT NULL DEFAULT '', platform_order_no TEXT NOT NULL DEFAULT '',
                receipt TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(profile_id,task_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS purchase_execution_intents(
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, phase TEXT NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(attempt_id,phase))""")
            db.execute("""CREATE TABLE IF NOT EXISTS purchase_execution_events(
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, event_type TEXT NOT NULL,
                details TEXT NOT NULL, created_at TEXT NOT NULL)""")

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")

    def _age(self, value, maximum, code):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                value = parsed.timestamp()
            except ValueError:
                raise ExecutionBlocked(code) from None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or maximum <= 0
                or not 0 <= self.clock() - value <= maximum):
            raise ExecutionBlocked(code)

    def _capabilities(self, *, writing=False):
        cap = self.adapter.capabilities
        if (not isinstance(cap, AdapterCapabilities) or not self.policy.account_id
                or cap.account_id != self.policy.account_id or cap.account_verified is not True):
            raise ExecutionBlocked("platform_account_unverified")
        if cap.platform not in self.policy.allowed_platforms:
            raise ExecutionBlocked("platform_not_allowed")
        if cap.complete_order_lookup is not True or cap.durable_order_binding is not True:
            raise ExecutionBlocked("platform_order_recovery_unverified")
        if writing and (cap.order_submission_is_unpaid is not True or cap.order_submit_enabled is not True):
            raise ExecutionBlocked("order_submit_unverified")
        return cap

    @contextmanager
    def _lock(self, job_id):
        # flock is released by process death. Lock the account as well as the
        # task so two batches cannot concurrently inspect/use one browser cart.
        root = Path(self.store.runtime) / "purchase-execution-locks"
        root.mkdir(mode=0o700, exist_ok=True)
        handles = []
        try:
            for key in sorted({"job:" + job_id, "account:" + self.policy.account_id}):
                path = root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                handles.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ExecutionBlocked("execution_busy") from None
            yield
        finally:
            for fd in reversed(handles):
                os.close(fd)

    def _event(self, db, attempt_id, kind, detail):
        db.execute("INSERT INTO purchase_execution_events VALUES (?,?,?,?,?)",
                   (uuid.uuid4().hex, attempt_id, kind, _json(detail), self._now()))

    @staticmethod
    def _source_guard(db):
        def setting(key, default=None):
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row[0])
            except (ValueError, TypeError):
                return row[0]
        hold = setting("restoration_hold", False)
        if hold not in (False, 0, "", "0", "false", None):
            raise ExecutionBlocked("restoration_hold")
        version, updated = setting("data_version", ""), setting("data_source_updated_at", "")
        rows = {row["key"]: row["value"] for row in db.execute("""SELECT key,value FROM daily_work_settings
            WHERE key IN ('projection_data_version','projection_source_updated_at')""")}
        if (not version or not updated or rows.get("projection_data_version") != version
                or rows.get("projection_source_updated_at") != updated):
            raise ExecutionBlocked("task_projection_conflict")
        return version, updated

    def _set(self, db, attempt, status, detail="", *, receipt=None, prepared=None):
        if receipt and attempt["platform_order_no"] and receipt.order_no != attempt["platform_order_no"]:
            raise ExecutionBlocked("platform_order_mismatch")
        order_no = receipt.order_no if receipt else attempt["platform_order_no"]
        db.execute("""UPDATE purchase_execution_attempts SET status=?,detail=?,updated_at=?,
            platform_order_no=?,receipt=CASE WHEN ? IS NULL THEN receipt ELSE ? END,
            prepared=CASE WHEN ? IS NULL THEN prepared ELSE ? END WHERE id=?""",
            (status, detail, self._now(), order_no, "yes" if receipt else None, _json(asdict(receipt)) if receipt else "",
             "yes" if prepared else None, _json(asdict(prepared)) if prepared else "", attempt["id"]))
        paid = receipt.paid_amount if receipt and status == "paid_confirmed" else ""
        db.execute("""UPDATE purchase_assistant_jobs SET status=?,detail=?,attempt_id=?,
            platform_order_no=?,paid_amount=CASE WHEN ?='' THEN paid_amount ELSE ? END,
            started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,updated_at=? WHERE id=?""",
            (status, detail, attempt["id"], order_no, paid, paid, self._now(), self._now(), attempt["job_id"]))
        self._event(db, attempt["id"], status, {"code": detail, "order_no": order_no})

    def _attempt(self, db, job_id, *, create=False):
        row = db.execute("SELECT * FROM purchase_execution_attempts WHERE job_id=?", (job_id,)).fetchone()
        if row:
            return dict(row)
        if not create:
            raise ExecutionBlocked("execution_attempt_not_found")
        plan = db.execute("""SELECT p.*,j.status AS job_status FROM purchase_assistant_jobs j
            JOIN purchase_assistant_plans p ON p.id=j.plan_id WHERE j.id=?""", (job_id,)).fetchone()
        if not plan or plan["job_status"] != "queued":
            raise ExecutionBlocked("purchase_job_not_queued")
        self._source_guard(db)
        frozen = json.loads(plan["frozen"])
        attempt_id, now = uuid.uuid4().hex, self._now()
        db.execute("INSERT INTO purchase_execution_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (attempt_id, job_id, plan["id"], plan["task_id"], plan["profile_id"], _hash(frozen),
                    _json(frozen), "preflight", "", "", "", "", now, now))
        result = dict(db.execute("SELECT * FROM purchase_execution_attempts WHERE id=?", (attempt_id,)).fetchone())
        self._set(db, result, "preflight")
        return result

    def _current(self, db, attempt):
        version, source_updated = self._source_guard(db)
        plan = db.execute("SELECT * FROM purchase_assistant_plans WHERE id=?", (attempt["plan_id"],)).fetchone()
        if not plan or plan["status"] != "in_progress":
            raise ExecutionBlocked("purchase_plan_not_active")
        frozen = json.loads(attempt["frozen"])
        if _hash(json.loads(plan["frozen"])) != attempt["frozen_hash"] or _hash(frozen) != attempt["frozen_hash"]:
            raise ExecutionBlocked("frozen_plan_changed")
        task = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (attempt["task_id"],)).fetchone()
        payload = json.loads(task["payload"]) if task else {}
        if (not task or task["role"] != "purchase" or task["task_type"] != "procurement_gap"
                or task["condition_state"] != "active" or task["status"] not in ("in_progress", "waiting")
                or payload.get("read_only") or payload.get("source_scope_review")):
            raise ExecutionBlocked("purchase_task_not_active")
        if not version or frozen.get("source_data_version") != version or task["source_data_version"] != version:
            raise ExecutionBlocked("purchase_source_changed")
        if (task["profile_id"] != frozen.get("profile_id") or task["sku"] != frozen.get("erp_code")
                or payload.get("product_identity") != frozen.get("product_identity")
                or not frozen.get("product_identity")
                or payload.get("order_nos") != frozen.get("order_nos")
                or _number(payload.get("shortage"), positive=True) != _number(frozen.get("base_shortage"), positive=True)):
            raise ExecutionBlocked("purchase_demand_changed")
        identity = frozen["product_identity"].split("|")
        if (len(identity) != 5 or not all(identity) or identity[0] != frozen["profile_id"] or identity[-1] != "1"
                or not isinstance(frozen.get("order_nos"), list) or not frozen["order_nos"]
                or len(set(frozen["order_nos"])) != len(frozen["order_nos"])):
            raise ExecutionBlocked("purchase_identity_unverified")
        white = db.execute("""SELECT w.*,s.source_hash FROM purchase_whitelist_entries w
            JOIN purchase_whitelist_sources s ON s.source_id=w.source_id WHERE w.id=?""",
            (plan["whitelist_entry_id"],)).fetchone()
        checks = frozen.get("whitelist_checks")
        if (not white or white["enabled"] != 1 or white["review_reason"] or not isinstance(checks, dict)
                or not _valid_hash(frozen.get("whitelist_source_hash"))
                or white["source_hash"] != frozen["whitelist_source_hash"]
                or white["profile_id"] not in ("", frozen["profile_id"])
                or any(white[key] != frozen.get(key) for key in ("erp_code", "platform", "platform_sku"))
                or not frozen.get("platform_sku")
                or any(str(white[key]) != str(checks.get(key, "")) for key in
                       ("product_name", "standard_check", "spec_check", "model_check", "warehouse_check", "pack_size", "price_limit"))):
            raise ExecutionBlocked("purchase_whitelist_changed")
        price = db.execute("SELECT * FROM purchase_price_bounds WHERE task_id=?", (attempt["task_id"],)).fetchone()
        bound = frozen.get("price_bound")
        if not price or not isinstance(bound, dict):
            raise ExecutionBlocked("price_source_missing")
        if (not _valid_hash(bound.get("source_hash")) or any(price[key] != bound.get(key) for key in price.keys())
                or any(price[key] != frozen.get(key) for key in ("profile_id", "erp_code", "product_identity"))
                or price["data_version"] != version or price["tax_basis"] != "tax_inclusive"
                or price["source_updated_at"] != source_updated
                or not price["unit"] or price["unit"].isdigit()):
            raise ExecutionBlocked("price_source_changed")
        self._age(price["source_updated_at"], self.policy.max_source_age_seconds, "price_source_stale")
        _number(price["price_limit"], positive=True)
        limits = frozen.get("amount_limits") or {}
        _number(limits.get("per_item"), positive=True)
        _number(limits.get("per_batch"), positive=True)
        if (isinstance(limits.get("max_items"), bool) or not isinstance(limits.get("max_items"), int)
                or limits["max_items"] < 1):
            raise ExecutionBlocked("purchase_batch_limit_invalid")
        return frozen, PurchaseRequest(attempt["id"], attempt["job_id"], attempt["plan_id"], attempt["task_id"],
            frozen["profile_id"], frozen["erp_code"], frozen["product_identity"], frozen["platform"],
            frozen["platform_sku"], str(frozen["base_shortage"]), price["unit"], price["price_limit"],
            price["source_hash"], frozen["whitelist_source_hash"], tuple(sorted((str(k), str(v)) for k, v in checks.items())))

    def _check_quote(self, request, frozen, quote):
        if not isinstance(quote, PreparedPurchase):
            raise ExecutionBlocked("platform_quote_invalid")
        if quote.currency != "CNY":
            raise ExecutionBlocked("platform_currency_unverified")
        cap = self._capabilities(writing=True)
        if (quote.platform != cap.platform or quote.account_id != cap.account_id
                or any(getattr(quote, key) != getattr(request, key) for key in
                       ("platform", "platform_sku", "erp_code", "product_identity", "unit", "price_source_hash"))
                or not quote.checkout_reference or len(quote.checkout_reference) > 200
                or (request.checkout_reference and quote.checkout_reference != request.checkout_reference)
                or quote.tax_basis != "tax_inclusive" or quote.checks != request.checks):
            raise ExecutionBlocked("platform_identity_or_quote_changed")
        self._age(quote.observed_at, self.policy.max_quote_age_seconds, "platform_quote_stale")
        # Only an unambiguous, already approved literal packaging rule can
        # authorize execution. Multi-option/opaque legacy packaging remains a
        # review item; merely echoing the frozen checks is not package proof.
        pack = re.fullmatch(r"(\d+(?:\.\d+)?)([^/\s]+)/([^/\s]+)",
                            dict(request.checks).get("pack_size", "").strip())
        if not pack:
            raise ExecutionBlocked("whitelist_packaging_unverified")
        if (_number(quote.package_size, positive=True) != _number(pack[1], positive=True)
                or quote.platform_unit != pack[2] or quote.package_unit != pack[3]):
            raise ExecutionBlocked("platform_packaging_changed")
        quantity = _number(quote.quantity, positive=True)
        # unit may be an ERP identity such as base_unit:1. It says nothing
        # about pieces, boxes, kilograms, or the platform's price denominator.
        if (not quote.platform_unit or quote.platform_unit.isdigit()
                or quote.platform_unit.startswith("base_unit:")
                or not _valid_hash(quote.unit_conversion_evidence_hash)):
            raise ExecutionBlocked("platform_unit_conversion_unverified")
        platform_quantity = _number(quote.platform_quantity, positive=True)
        conversion = _number(quote.base_units_per_platform_unit, positive=True)
        if platform_quantity * conversion != quantity:
            raise ExecutionBlocked("platform_unit_conversion_mismatch")
        count = _number(quote.package_count, positive=True)
        if (quantity != _number(request.quantity, positive=True) or count != count.to_integral_value()
                or not quote.package_unit or platform_quantity != _number(quote.package_size, positive=True) * count):
            raise ExecutionBlocked("platform_quantity_or_packaging_changed")
        price = _number(quote.inclusive_unit_price, positive=True)
        limit = _number(request.price_limit, positive=True)
        line = (platform_quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amount = _number(quote.payable_amount, positive=True)
        extras = _number(quote.shipping_amount) + _number(quote.fee_amount)
        if line != _number(quote.line_amount) or amount != (line + extras).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
            raise ExecutionBlocked("platform_amount_unverified")
        if price > limit * conversion or amount > (quantity * limit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
            raise ExecutionBlocked("platform_price_over_limit")
        limits = frozen["amount_limits"]
        if amount > min(_number(limits["per_item"], positive=True), _number(limits["per_batch"], positive=True)):
            raise ExecutionBlocked("platform_amount_over_limit")

    def _intent(self, db, attempt, phase, request, quote):
        # Read-only inspection may take time. Revoked permissions must be
        # checked here, immediately before committing permission to click.
        cap = self._capabilities(writing=True)
        if self.policy.enabled is not True:
            raise ExecutionBlocked("execution_disabled")
        if phase == "order" and self.policy.allow_order_submit is not True:
            raise ExecutionBlocked("order_submit_disabled")
        if phase == "payment" and (self.policy.allow_payment is not True or cap.payment_enabled is not True):
            raise ExecutionBlocked("payment_disabled")
        if db.execute("SELECT 1 FROM purchase_execution_intents WHERE attempt_id=? AND phase=?", (attempt["id"], phase)).fetchone():
            raise ExecutionBlocked("irreversible_intent_already_exists")
        # Recheck mutable sources and the quote in the same transaction that
        # commits permission/intent. No network call happens under this writer.
        frozen, current = self._current(db, attempt)
        current = replace(current, checkout_reference=request.checkout_reference)
        if current != request:
            raise ExecutionBlocked("purchase_request_changed")
        self._check_quote(request, frozen, quote)
        intent = ExecutionIntent(uuid.uuid4().hex, attempt["id"], phase, request, quote, attempt["platform_order_no"])
        payload = asdict(intent)
        db.execute("INSERT INTO purchase_execution_intents VALUES (?,?,?,?,?,?)",
                   (intent.id, attempt["id"], phase, _json(payload), _hash(payload), self._now()))
        self._set(db, attempt, phase + "_result_unknown", "intent_persisted_before_click")
        self._event(db, attempt["id"], "intent_persisted", {"intent_id": intent.id, "phase": phase})
        return intent

    @staticmethod
    def _decode_intent(row):
        payload = json.loads(row["payload"])
        if (_hash(payload) != row["payload_hash"] or payload.get("id") != row["id"]
                or payload.get("attempt_id") != row["attempt_id"] or payload.get("phase") != row["phase"]
                or payload.get("phase") not in ("order", "payment")):
            raise ExecutionBlocked("execution_intent_corrupt")
        values = dict(payload["request"])
        values["checks"] = tuple(tuple(pair) for pair in values["checks"])
        return ExecutionIntent(payload["id"], payload["attempt_id"], payload["phase"], PurchaseRequest(**values),
                               _prepared(payload["prepared"]), payload["platform_order_no"])

    def _check_lookup(self, intent, lookup):
        quote = intent.prepared
        if (not isinstance(lookup, OrderLookup) or lookup.complete is not True
                or lookup.account_id != self.policy.account_id or lookup.checkout_reference != quote.checkout_reference):
            raise ExecutionBlocked("platform_lookup_incomplete")
        if len(lookup.orders) != 1:
            raise ExecutionBlocked("platform_order_not_unique" if lookup.orders else "platform_order_not_found")
        order = lookup.orders[0]
        if (not isinstance(order, PlatformOrder) or not order.order_no or not _valid_hash(order.evidence_hash)
                or order.account_id != quote.account_id or order.checkout_reference != quote.checkout_reference
                or any(getattr(order, key) != getattr(quote, key) for key in
                       ("platform", "platform_sku", "erp_code", "product_identity", "unit", "platform_unit",
                        "unit_conversion_evidence_hash", "currency"))
                or _number(order.quantity, positive=True) != _number(quote.quantity, positive=True)
                or _number(order.platform_quantity, positive=True) != _number(quote.platform_quantity, positive=True)
                or _number(order.base_units_per_platform_unit, positive=True) != _number(quote.base_units_per_platform_unit, positive=True)
                or _number(order.payable_amount, positive=True) != _number(quote.payable_amount, positive=True)
                or (intent.platform_order_no and order.order_no != intent.platform_order_no)
                or order.status not in ("unpaid", "paid", "cancelled", "rejected")):
            raise ExecutionBlocked("platform_order_mismatch")
        self._age(order.observed_at, self.policy.max_quote_age_seconds, "platform_order_evidence_stale")
        if order.status == "paid" and _number(order.paid_amount, positive=True) != _number(quote.payable_amount, positive=True):
            raise ExecutionBlocked("platform_paid_amount_mismatch")
        if order.status == "unpaid" and _number(order.paid_amount) != 0:
            raise ExecutionBlocked("platform_paid_amount_mismatch")
        return order

    def _reconcile(self, job_id):
        self._capabilities()
        with self.store.connect() as db:
            attempt = self._attempt(db, job_id)
            phase = "payment" if attempt["status"] == "payment_result_unknown" else "order"
            row = db.execute("SELECT * FROM purchase_execution_intents WHERE attempt_id=? AND phase=?", (attempt["id"], phase)).fetchone()
            if not row:
                raise ExecutionBlocked("execution_intent_missing")
            intent = self._decode_intent(row)
            if (intent.request.attempt_id != attempt["id"] or intent.request.job_id != job_id
                    or intent.request.plan_id != attempt["plan_id"] or intent.request.task_id != attempt["task_id"]
                    or intent.request.profile_id != attempt["profile_id"]):
                raise ExecutionBlocked("execution_intent_scope_mismatch")
            if attempt["platform_order_no"]:
                if intent.platform_order_no and intent.platform_order_no != attempt["platform_order_no"]:
                    raise ExecutionBlocked("platform_order_mismatch")
                intent = replace(intent, platform_order_no=attempt["platform_order_no"])
        try:
            order = self._check_lookup(intent, self.adapter.lookup(intent))
        except Exception as exc:
            code = exc.code if isinstance(exc, ExecutionBlocked) else "platform_lookup_failed"
            with self.store.connect(write=True) as db:
                self._set(db, attempt, attempt["status"], code)
            return self.result(job_id)
        status = ("paid_confirmed" if order.status == "paid" else
                  "manual_review" if order.status in ("cancelled", "rejected") else
                  "awaiting_payment" if phase == "order" else "payment_result_unknown")
        with self.store.connect(write=True) as db:
            self._set(db, attempt, status, "platform_order_readback_confirmed", receipt=order)
        return self.result(job_id)

    def result(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM purchase_execution_attempts WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return {"job_id": job_id, "status": "not_started"}
            result = {key: row[key] for key in ("job_id", "id", "status", "detail", "platform_order_no", "updated_at")}
            result["erp_status"] = "not_recorded"
            return result

    def recover(self, job_id):
        """Explicit read-only platform recovery, including while writes disabled."""
        try:
            with self._lock(job_id):
                result = self.result(job_id)
                if result["status"] in ("order_result_unknown", "payment_result_unknown"):
                    return self._reconcile(job_id)
                return result
        except ExecutionBlocked as exc:
            return {"job_id": job_id, "status": "blocked", "detail": exc.code}

    def execute(self, job_id):
        if self.policy.enabled is not True:
            return {"job_id": job_id, "status": "disabled", "detail": "execution_disabled"}
        attempt = None
        try:
            with self._lock(job_id):
                self._capabilities()
                with self.store.connect(write=True) as db:
                    attempt = self._attempt(db, job_id, create=True)
                if attempt["status"] in ("order_result_unknown", "payment_result_unknown", "awaiting_payment"):
                    self._reconcile(job_id)
                    attempt = None  # refresh after readback; never use stale phase
                    with self.store.connect() as db:
                        attempt = self._attempt(db, job_id)
                if attempt["status"] in ("paid_confirmed", "manual_review", "blocked", "order_result_unknown", "payment_result_unknown"):
                    return self.result(job_id)
                self._capabilities(writing=True)
                with self.store.connect() as db:
                    frozen, request = self._current(db, attempt)
                if attempt["prepared"]:
                    request = replace(request, checkout_reference=_prepared(json.loads(attempt["prepared"])).checkout_reference)
                quote = self.adapter.inspect(request)
                self._check_quote(request, frozen, quote)
                if attempt["status"] == "awaiting_payment" and attempt["prepared"]:
                    previous = _prepared(json.loads(attempt["prepared"]))
                    if replace(quote, observed_at=previous.observed_at) != previous:
                        raise ExecutionBlocked("platform_frozen_checkout_changed")
                if attempt["status"] != "awaiting_payment":
                    # This is only a read. Existing orders are never silently
                    # adopted as this plan's own purchase before its intent.
                    probe = ExecutionIntent("preflight", attempt["id"], "preflight", request, quote)
                    lookup = self.adapter.lookup(probe)
                    if (not isinstance(lookup, OrderLookup) or lookup.complete is not True
                            or lookup.account_id != self.policy.account_id
                            or lookup.checkout_reference != quote.checkout_reference or lookup.orders):
                        raise ExecutionBlocked("preexisting_platform_order_or_incomplete_lookup")
                with self.store.connect(write=True) as db:
                    self._set(db, attempt, "awaiting_payment" if attempt["status"] == "awaiting_payment" else "ready", prepared=quote)
                if attempt["status"] != "awaiting_payment":
                    if self.policy.allow_order_submit is not True:
                        return self.result(job_id)
                    with self.store.connect(write=True) as db:
                        attempt = self._attempt(db, job_id)
                        intent = self._intent(db, attempt, "order", request, quote)
                    try:
                        self.adapter.submit_order(intent)
                    except Exception:
                        pass  # The durable unknown phase is authoritative.
                    self._reconcile(job_id)
                    with self.store.connect() as db:
                        attempt = self._attempt(db, job_id)
                    if attempt["status"] != "awaiting_payment":
                        return self.result(job_id)
                if self.policy.allow_payment is not True or self.adapter.capabilities.payment_enabled is not True:
                    return self.result(job_id)
                # Re-read the final payable checkout after order creation. A
                # new amount, package, or conversion is a new proposal; it must
                # not pay the previously confirmed external order silently.
                request = replace(request, checkout_reference=quote.checkout_reference)
                final_quote = self.adapter.inspect(request)
                self._check_quote(request, frozen, final_quote)
                if replace(final_quote, observed_at=quote.observed_at) != quote:
                    raise ExecutionBlocked("platform_frozen_checkout_changed")
                quote = final_quote
                # Another operator can pay/cancel while this worker inspects
                # the final page. The persisted earlier unpaid receipt never
                # grants permission for another payment click.
                probe = ExecutionIntent("prepayment", attempt["id"], "payment", request, quote,
                                        attempt["platform_order_no"])
                try:
                    current_order = self._check_lookup(probe, self.adapter.lookup(probe))
                except ExecutionBlocked:
                    raise
                except Exception:
                    raise ExecutionBlocked("platform_lookup_failed") from None
                if current_order.status != "unpaid":
                    with self.store.connect(write=True) as db:
                        status = "paid_confirmed" if current_order.status == "paid" else "manual_review"
                        self._set(db, attempt, status, "platform_order_readback_confirmed", receipt=current_order)
                    return self.result(job_id)
                with self.store.connect(write=True) as db:
                    attempt = self._attempt(db, job_id)
                    intent = self._intent(db, attempt, "payment", request, quote)
                try:
                    self.adapter.pay_order(intent)
                except Exception:
                    pass
                return self._reconcile(job_id)
        except ExecutionBlocked as exc:
            if attempt and exc.code != "execution_busy":
                with self.store.connect(write=True) as db:
                    current = self._attempt(db, job_id)
                    # Never erase a committed unknown outcome with a preflight
                    # error (including revoked credentials or changed sources).
                    status = current["status"] if current["status"].endswith("_result_unknown") else "blocked"
                    self._set(db, current, status, exc.code)
                return self.result(job_id)
            return {"job_id": job_id, "status": "blocked", "detail": exc.code}
        except Exception:
            if attempt:
                with self.store.connect(write=True) as db:
                    current = self._attempt(db, job_id)
                    status = current["status"] if current["status"].endswith("_result_unknown") else "blocked"
                    self._set(db, current, status, "execution_internal_error")
                return self.result(job_id)
            return {"job_id": job_id, "status": "blocked", "detail": "execution_internal_error"}
