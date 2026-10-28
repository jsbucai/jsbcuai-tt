"""Versioned procurement intake, independent of browser and payment execution.

Only an explicit, scoped purchase action can reserve a demand. A model message,
catalog refresh, or historic whitelist row cannot create an execution job.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation

from flask import Blueprint, g, jsonify, request

from .daily_work import DailyWorkConflict, _flag, _is_admin, _roles, _user_id, _setting
from .store import dumps, timestamp


class ProcurementConflict(Exception):
    def __init__(self, message, code="purchase_conflict", status=409):
        super().__init__(message)
        self.code, self.status = code, status


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _hash(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class ProcurementAssistant:
    def __init__(self, store, daily_work):
        self.store, self.daily = store, daily_work
        with store.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS purchase_whitelist_sources(
                    source_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
                    imported_at TEXT NOT NULL, row_count INTEGER NOT NULL,
                    valid_count INTEGER NOT NULL, review_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS purchase_whitelist_entries(
                    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL DEFAULT '',
                    erp_code TEXT NOT NULL, platform TEXT NOT NULL,
                    platform_sku TEXT NOT NULL, product_name TEXT NOT NULL,
                    standard_check TEXT NOT NULL DEFAULT '', spec_check TEXT NOT NULL DEFAULT '',
                    model_check TEXT NOT NULL DEFAULT '', warehouse_check TEXT NOT NULL DEFAULT '',
                    pack_size TEXT NOT NULL DEFAULT '', price_limit TEXT NOT NULL DEFAULT '',
                    last_verified_at TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL,
                    source_row INTEGER NOT NULL, imported_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS purchase_whitelist_code
                    ON purchase_whitelist_entries(profile_id,erp_code,enabled);
                CREATE TABLE IF NOT EXISTS purchase_price_bounds(
                    task_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                    erp_code TEXT NOT NULL, product_identity TEXT NOT NULL,
                    data_version TEXT NOT NULL, unit TEXT NOT NULL,
                    weighted_sale_price TEXT NOT NULL, price_limit TEXT NOT NULL,
                    tax_basis TEXT NOT NULL, source_hash TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS purchase_assistant_plans(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
                    actor_id TEXT NOT NULL, profile_id TEXT NOT NULL,
                    erp_code TEXT NOT NULL, source_data_version TEXT NOT NULL,
                    source_task_version INTEGER NOT NULL,
                    whitelist_entry_id TEXT NOT NULL, whitelist_source_hash TEXT NOT NULL,
                    frozen TEXT NOT NULL, status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(actor_id,idempotency_key));
                CREATE TABLE IF NOT EXISTS purchase_assistant_jobs(
                    id TEXT PRIMARY KEY, plan_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    platform_order_no TEXT NOT NULL DEFAULT '',
                    paid_amount TEXT NOT NULL DEFAULT '',
                    erp_order_no TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL, FOREIGN KEY(plan_id)
                    REFERENCES purchase_assistant_plans(id));
                CREATE TABLE IF NOT EXISTS purchase_assistant_events(
                    id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, actor_id TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                    FOREIGN KEY(plan_id) REFERENCES purchase_assistant_plans(id));
                CREATE INDEX IF NOT EXISTS purchase_assistant_event_plan
                    ON purchase_assistant_events(plan_id,created_at);
            """)

    @staticmethod
    def _authorize(user):
        if not isinstance(user, dict) or not (_is_admin(user) or "purchase" in _roles(user)):
            raise ProcurementConflict("账号没有采购岗位权限", "role_forbidden", 403)

    @staticmethod
    def _version(db):
        return str(_setting(db, "data_version") or "")

    @staticmethod
    def _projection_current(db):
        version = str(_setting(db, "data_version") or "")
        updated = str(_setting(db, "data_source_updated_at") or "")
        projected = db.execute("SELECT value FROM daily_work_settings WHERE key='projection_data_version'").fetchone()
        projected_at = db.execute("SELECT value FROM daily_work_settings WHERE key='projection_source_updated_at'").fetchone()
        return bool(version and updated and projected and projected_at and
                    projected[0] == version and projected_at[0] == updated)

    @staticmethod
    def _whitelist(db, profile_id, code):
        # An old mapping is a candidate, never authority to buy. Prefer the
        # platform covered by the first Linux implementation.
        return db.execute("""SELECT w.*,s.source_hash FROM purchase_whitelist_entries w
            JOIN purchase_whitelist_sources s ON s.source_id=w.source_id
            WHERE w.enabled=1 AND w.review_reason='' AND w.erp_code=?
            AND w.profile_id IN ('',?)
            ORDER BY CASE w.platform WHEN '淘钉钉' THEN 0 ELSE 1 END,
                     CASE WHEN w.profile_id=? THEN 0 ELSE 1 END,w.id LIMIT 1""",
            (code, profile_id, profile_id)).fetchone()

    def _visible_tasks(self, db, user):
        actor = _user_id(user)
        clause = "" if _is_admin(user) else " AND t.assignee_id=?"
        params = [] if _is_admin(user) else [actor]
        return db.execute("""SELECT t.* FROM daily_work_tasks t
            WHERE t.role='purchase' AND t.task_type='procurement_gap'
            AND t.condition_state='active' AND t.status IN ('todo','in_progress','waiting')"""
            + clause + " ORDER BY t.priority,t.due_date,t.id", params).fetchall()

    @staticmethod
    def _job(db, plan_id):
        row = db.execute("SELECT * FROM purchase_assistant_jobs WHERE plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def _card(self, db, task, data_version, projection_current, restoration_hold):
        payload = json.loads(task["payload"] or "{}")
        prior = db.execute("SELECT * FROM purchase_assistant_plans WHERE task_id=?", (task["id"],)).fetchone()
        whitelist = self._whitelist(db, task["profile_id"], task["sku"])
        shortage = _decimal(payload.get("shortage"))
        price = db.execute("""SELECT * FROM purchase_price_bounds
            WHERE task_id=? AND profile_id=? AND erp_code=? AND product_identity=?
            AND data_version=?""",
            (task["id"], task["profile_id"], task["sku"],
             str(payload.get("product_identity") or ""), data_version)).fetchone()
        reason = ("已有采购计划" if prior else
                  "恢复核对期间暂停采购" if restoration_hold else
                  "任务尚未跟上最新订单数据" if not projection_current or task["source_data_version"] != data_version else
                  "商品不在已核对采购白名单" if not whitelist else
                  "缺口数量待核对" if shortage is None or shortage <= 0 else
                  "销售价格资料待更新" if not price else
                  "基本单位与平台包装待核对" if price["unit"].startswith("base_unit:") else "")
        can_enroll = (not prior and not restoration_hold and projection_current and
                      task["source_data_version"] == data_version and bool(whitelist) and
                      shortage is not None and shortage > 0)
        return {
            "id": task["id"], "version": task["version"], "profile_id": task["profile_id"],
            "product_code": task["sku"], "product_name": task["title"],
            "quantity": str(shortage) if shortage is not None else "",
            "unit": "基本单位（包装待核对）" if price and price["unit"].startswith("base_unit:")
                else price["unit"] if price else "", "warehouse": "08390",
            "unit_verified": bool(price and not price["unit"].startswith("base_unit:")),
            "source_order_count": len(payload.get("order_nos") or []),
            "platform": whitelist["platform"] if whitelist else "",
            "platform_sku": whitelist["platform_sku"] if whitelist else "",
            "price_limit": price["price_limit"] if price else "",
            "state": "in_progress" if prior else task["status"],
            "eligible": bool(whitelist), "can_enroll": can_enroll,
            "reason": reason, "plan_id": prior["id"] if prior else "",
            "job": self._job(db, prior["id"]) if prior else None,
        }

    def list_plan(self, user):
        self._authorize(user)
        with self.store.read_snapshot(), self.store.connect() as db:
            version = self._version(db)
            current = self._projection_current(db)
            restoration_hold = _flag(_setting(db, 'restoration_hold', False))
            items = [self._card(db, task, version, current, restoration_hold)
                     for task in self._visible_tasks(db, user)]
            latest = db.execute("SELECT source_hash,imported_at FROM purchase_whitelist_sources ORDER BY imported_at DESC LIMIT 1").fetchone()
            counts = db.execute("""SELECT count(*) AS candidate_total,
                coalesce(sum(CASE WHEN enabled=1 AND review_reason='' THEN 1 ELSE 0 END),0)
                AS verified_total FROM purchase_whitelist_entries""").fetchone()
            # Catalog mappings and current demand are different populations.
            # Staged rows remain visible in the count without granting checkout.
            whitelist_counts = {"candidate_total": counts["candidate_total"],
                                "verified_total": counts["verified_total"],
                                "review_total": counts["candidate_total"] - counts["verified_total"],
                                "matched_demand_total": sum(bool(item["eligible"]) for item in items)}
            return {"source_version": version, "whitelist_version": latest["source_hash"] if latest else "",
                    "generated_at": timestamp(), "items": items,
                    "whitelist_counts": whitelist_counts}

    def enroll(self, user, task_id, body):
        self._authorize(user)
        if not isinstance(body, dict):
            raise ValueError("请求必须为 JSON 对象")
        key = str(body.get("idempotency_key") or "").strip()
        if not 8 <= len(key) <= 96:
            raise ValueError("采购操作需要幂等标识")
        expected = body.get("version")
        if isinstance(expected, bool) or not str(expected).isdigit():
            raise ValueError("采购操作需要任务版本")
        with self.store.connect(write=True) as db:
            existing = db.execute("SELECT * FROM purchase_assistant_plans WHERE actor_id=? AND idempotency_key=?",
                                  (_user_id(user), key)).fetchone()
            if existing:
                if existing["task_id"] != task_id:
                    raise ProcurementConflict("幂等标识已用于另一商品", "idempotency_conflict")
                return {"plan_id": existing["id"], "job": self._job(db, existing["id"]), "replayed": True}
            if _flag(_setting(db, 'restoration_hold', False)):
                raise ProcurementConflict("恢复核对期间暂停采购", "restoration_hold")
            if not self._projection_current(db):
                raise ProcurementConflict("任务尚未跟上最新订单数据，请先刷新任务", "task_projection_conflict")
            task = db.execute("SELECT * FROM daily_work_tasks WHERE id=?", (task_id,)).fetchone()
            if not task or task["role"] != "purchase" or task["task_type"] != "procurement_gap":
                raise ProcurementConflict("采购需求不存在", "not_found", 404)
            if task["version"] != int(expected):
                raise ProcurementConflict("采购需求已更新，请刷新", "task_version_changed")
            if task["condition_state"] != "active":
                raise ProcurementConflict("需求来源已变化，请核对", "task_condition_resolved")
            if db.execute("SELECT 1 FROM purchase_assistant_plans WHERE task_id=?", (task_id,)).fetchone():
                raise ProcurementConflict("该需求已有采购计划", "purchase_already_planned")
            version = self._version(db)
            if body.get("source_version") != version:
                raise ProcurementConflict("ERP 数据已更新，请刷新采购计划", "data_version_changed")
            if task["source_data_version"] != version:
                raise ProcurementConflict("该采购需求来源已过期，请先刷新任务", "task_source_stale")
            whitelist = self._whitelist(db, task["profile_id"], task["sku"])
            if not whitelist:
                raise ProcurementConflict("商品没有已核对的采购白名单映射", "whitelist_review_required")
            payload = json.loads(task["payload"] or "{}")
            shortage = _decimal(payload.get("shortage"))
            if shortage is None or shortage <= 0:
                raise ProcurementConflict("采购数量待核对", "quantity_review_required")
            # DailyWork supplies the role, on-duty, assignee, read-only and
            # version checks, and writes the task event in this same transaction.
            started = self.daily.action(task_id, user, "start", "purchase-plan:" + key,
                                        int(expected), db=db)
            frozen = {"profile_id": task["profile_id"], "erp_code": task["sku"],
                      "product_identity": payload.get("product_identity", ""),
                      "base_shortage": str(shortage), "order_nos": payload.get("order_nos") or [],
                      "platform": whitelist["platform"], "platform_sku": whitelist["platform_sku"],
                      "whitelist_checks": {field: whitelist[field] for field in
                          ("product_name", "standard_check", "spec_check", "model_check",
                           "warehouse_check", "pack_size", "price_limit")},
                      "amount_limits": {"per_item": "2000", "per_batch": "4000", "max_items": 3},
                      "source_data_version": version, "source_task_version": task["version"],
                      "whitelist_source_hash": whitelist["source_hash"]}
            price = db.execute("""SELECT * FROM purchase_price_bounds
                WHERE task_id=? AND profile_id=? AND erp_code=? AND product_identity=?
                AND data_version=?""",
                (task_id, task["profile_id"], task["sku"],
                 str(payload.get("product_identity") or ""), version)).fetchone()
            if price:
                frozen["price_bound"] = dict(price)
            plan_id, job_id, now = uuid.uuid4().hex, uuid.uuid4().hex, timestamp()
            db.execute("""INSERT INTO purchase_assistant_plans VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (plan_id, task_id, _user_id(user),
                task["profile_id"], task["sku"], version, task["version"],
                whitelist["id"], whitelist["source_hash"], dumps(frozen),
                "in_progress", key, now, now))
            # A fresh price source is required before any platform action. The
            # job is durable even when this input is missing and can be resumed.
            job_status = ("awaiting_unit_review" if price and price["unit"].startswith("base_unit:")
                          else "queued" if price else "awaiting_price_source")
            db.execute("INSERT INTO purchase_assistant_jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (job_id, plan_id, job_status,
                        "待核对基本单位与平台包装支数" if job_status=="awaiting_unit_review"
                        else "待平台实时核验" if price else "待更新销售价格资料",
                        "", "", "", "", "", now))
            db.execute("INSERT INTO purchase_assistant_events VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex, plan_id, "enrolled", _user_id(user),
                        dumps({"task_version": task["version"], "new_task_version": started["task"]["version"]}), now))
            return {"plan_id": plan_id, "job": self._job(db, plan_id), "replayed": False}

    def job(self, user, job_id):
        self._authorize(user)
        with self.store.connect() as db:
            row = db.execute("""SELECT j.*,p.actor_id,p.task_id,p.profile_id,p.erp_code
                FROM purchase_assistant_jobs j JOIN purchase_assistant_plans p ON p.id=j.plan_id
                WHERE j.id=?""", (job_id,)).fetchone()
            if not row:
                raise ProcurementConflict("执行任务不存在", "not_found", 404)
            if not _is_admin(user) and row["actor_id"] != _user_id(user):
                raise ProcurementConflict("无权查看该采购任务", "role_forbidden", 403)
            return {"job": dict(row)}

    def whitelist_status(self, user):
        if not _is_admin(user):
            raise ProcurementConflict("只有管理员可以查看采购白名单核对清单", "admin_required", 403)
        with self.store.connect() as db:
            sources = [dict(row) for row in db.execute("""SELECT * FROM purchase_whitelist_sources
                ORDER BY imported_at DESC,source_id DESC LIMIT 20""")]
            latest = sources[0]["source_id"] if sources else ""
            rows = db.execute("""SELECT source_row,erp_code,platform,platform_sku,
                product_name,spec_check,model_check,enabled,review_reason,last_verified_at
                FROM purchase_whitelist_entries WHERE source_id=? ORDER BY source_row""",
                (latest,)).fetchall() if latest else []
            return {"sources": sources, "items": [dict(row) for row in rows]}


def procurement_assistant_routes(engine):
    bp = Blueprint("procurement_assistant", __name__)

    def respond(callback):
        try:
            return jsonify(ok=True, **callback())
        except ProcurementConflict as error:
            return jsonify(ok=False, error=error.code, message=str(error)), error.status
        except DailyWorkConflict as error:
            return jsonify(ok=False, error=error.code, message=str(error)), 409
        except (ValueError, TypeError) as error:
            return jsonify(ok=False, error="invalid_request", message=str(error)), 400

    @bp.get("/api/v2/assistant/purchase/plan")
    def plan():
        return respond(lambda: engine.list_plan(g.current_user))

    @bp.post("/api/v2/assistant/purchase/plan/<task_id>/enroll")
    def enroll(task_id):
        return respond(lambda: engine.enroll(g.current_user, task_id, request.get_json(silent=True)))

    @bp.get("/api/v2/assistant/jobs/<job_id>")
    def job(job_id):
        return respond(lambda: engine.job(g.current_user, job_id))

    @bp.get("/api/v2/assistant/purchase/whitelist/status")
    def whitelist_status():
        return respond(lambda: engine.whitelist_status(g.current_user))

    return bp
