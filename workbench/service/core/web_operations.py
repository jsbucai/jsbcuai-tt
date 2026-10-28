"""One durable browser lane for ERP export and document imports.

This module coordinates browser ownership and publishes progress. It does not
grant permission to write an ERP document: each writer still needs a frozen
business intent, a single external attempt, and its accepted completion evidence.
Sales completion uses the website import receipt without an ERP readback.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from .store import dumps


KINDS = {
    "purchase_download": "采购未交下载",
    "sales_order_import": "销售订单录入",
    "purchase_order_import": "采购订单录入",
}
ACTIVE = ("running", "needs_human", "uncertain")
FINAL = ("succeeded", "failed", "blocked")
KEY = re.compile(r"[A-Za-z0-9_.:-]{8,96}\Z")


def _user_id(user):
    return str(user.get("id") or "").strip() if isinstance(user, dict) else ""


def _roles(user):
    value = user.get("roles", []) if isinstance(user, dict) else []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = value.split(",")
    return value if isinstance(value, list) else []


def _is_admin(user):
    return bool(isinstance(user, dict) and user.get("is_admin") in (True, 1, "1"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WebOperationConflict(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.status = code, status
        super().__init__(message)


class WebOperations:
    def __init__(self, store):
        self.store = store
        with store.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS web_operations(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor_id TEXT NOT NULL,
                role TEXT NOT NULL, profile_id TEXT NOT NULL, target_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL, detail TEXT NOT NULL,
                step INTEGER NOT NULL, total_steps INTEGER NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                finished_at TEXT NOT NULL, worker_token TEXT NOT NULL,
                UNIQUE(actor_id,idempotency_key))""")
            db.execute("""CREATE INDEX IF NOT EXISTS web_operations_queue
                ON web_operations(status,created_at,id)""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS web_operations_target
                ON web_operations(kind,profile_id,target_id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS web_operation_payloads(
                operation_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
                FOREIGN KEY(operation_id) REFERENCES web_operations(id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS web_operation_events(
                id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(operation_id) REFERENCES web_operations(id))""")
            db.execute("""CREATE INDEX IF NOT EXISTS web_operation_event_order
                ON web_operation_events(operation_id,created_at,id)""")

    @staticmethod
    def _event(db, operation_id, status, stage, detail):
        db.execute("INSERT INTO web_operation_events VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, operation_id, status, stage, detail, now()))

    @staticmethod
    def _row(row):
        result = dict(row)
        result["label"] = KINDS[result["kind"]]
        result["result"] = json.loads(result.pop("result_json"))
        result.pop("payload_hash")
        result.pop("worker_token")
        return result

    def enqueue(self, *, kind, actor_id, role, profile_id, target_id, key,
                payload, total_steps=4):
        if kind not in KINDS or role not in {"sales", "purchase"}:
            raise ValueError("网页操作类型或岗位无效")
        if (kind == "sales_order_import") != (role == "sales"):
            raise ValueError("网页操作岗位与单据类型不符")
        if not all(isinstance(value, str) and value.strip() for value in
                   (actor_id, profile_id, target_id)):
            raise ValueError("网页操作缺少账号、账套或目标身份")
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("网页操作需要有效幂等键")
        if not isinstance(payload, dict) or not 1 <= total_steps <= 20:
            raise ValueError("网页操作参数无效")
        fingerprint = hashlib.sha256(dumps({
            "kind": kind, "role": role, "profile_id": profile_id,
            "target_id": target_id, "payload": payload,
        }).encode()).hexdigest()
        with self.store.connect(write=True) as db:
            prior = db.execute("SELECT * FROM web_operations WHERE actor_id=? AND idempotency_key=?",
                               (actor_id, key)).fetchone()
            if prior:
                if prior["payload_hash"] != fingerprint:
                    raise WebOperationConflict("idempotency_conflict", "幂等键已用于另一项网页操作")
                return {"operation": self._row(prior), "replayed": True}
            if kind == 'purchase_download' and self._purchase_pause(db):
                raise WebOperationConflict('purchase_download_paused', '采购网页采集已暂停；处理登录后由管理员恢复')
            if db.execute("""SELECT 1 FROM web_operations WHERE kind=? AND profile_id=?
                AND target_id=? LIMIT 1""", (kind, profile_id, target_id)).fetchone():
                raise WebOperationConflict("target_already_enqueued", "该文件或单据已经进入网页执行队列，请核对原记录")
            stamp = now()
            operation_id = uuid.uuid4().hex
            db.execute("""INSERT INTO web_operations VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (operation_id, kind, actor_id, role, profile_id, target_id,
                 key, fingerprint, "queued", "排队", "等待浏览器执行槽",
                 0, total_steps, "{}", stamp, "", stamp, "", ""))
            db.execute("INSERT INTO web_operation_payloads VALUES (?,?)",
                       (operation_id, dumps(payload)))
            self._event(db, operation_id, "queued", "排队", "等待浏览器执行槽")
            return {"operation": self._row(db.execute(
                "SELECT * FROM web_operations WHERE id=?", (operation_id,)).fetchone()), "replayed": False}

    def claim_next(self, worker_token):
        if not isinstance(worker_token, str) or len(worker_token) < 16:
            raise ValueError("执行者身份无效")
        with self.store.connect(write=True) as db:
            if (db.execute("SELECT 1 FROM sqlite_master WHERE name='update_jobs'").fetchone()
                    and db.execute("SELECT 1 FROM update_jobs WHERE status='running' LIMIT 1").fetchone()):
                return None
            # A completed sales attempt with an unknown website result stays
            # immutable, but its browser has closed. It must not permanently
            # block unrelated downloads or sales. The lane flock still covers
            # the entire child process, including browser cleanup.
            if db.execute("""SELECT 1 FROM web_operations
                WHERE status IN ('running','needs_human') OR
                    (status='uncertain' AND kind!='sales_order_import')
                LIMIT 1""").fetchone():
                return None
            row = db.execute("""SELECT * FROM web_operations WHERE status='queued'
                ORDER BY rowid LIMIT 1""").fetchone()
            if not row:
                return None
            stamp = now()
            db.execute("""UPDATE web_operations SET status='running',stage='准备',
                detail='已取得独占浏览器执行槽',started_at=?,updated_at=?,worker_token=?
                WHERE id=? AND status='queued'""",
                (stamp, stamp, worker_token, row["id"]))
            self._event(db, row["id"], "running", "准备", "已取得独占浏览器执行槽")
            return self._row(db.execute("SELECT * FROM web_operations WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def _purchase_pause(db):
        row = db.execute("SELECT value FROM settings WHERE key='purchase_download_pause'").fetchone()
        return json.loads(row[0]) if row else None

    def pause_purchase_downloads(self, reason, actor='system:login-review'):
        """Release the read-only download backlog, never cancel an ERP writer."""
        if not reason or not actor:
            raise ValueError('暂停采购采集需要原因和操作者')
        with self.store.connect(write=True) as db:
            if db.execute("SELECT 1 FROM web_operations WHERE kind='purchase_download' AND status='running'").fetchone():
                raise WebOperationConflict('download_running', '先结束当前采集进程再暂停')
            stamp = now()
            value = {'reason': reason, 'actor': actor, 'paused_at': stamp}
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('purchase_download_pause',?)", (dumps(value),))
            rows = db.execute("SELECT id FROM web_operations WHERE kind='purchase_download' AND status IN ('queued','needs_human','uncertain')").fetchall()
            for row in rows:
                db.execute("UPDATE web_operations SET status='blocked',stage='采购采集已暂停',detail=?,updated_at=?,finished_at=? WHERE id=?",
                           (reason[:500], stamp, stamp, row['id']))
                self._event(db, row['id'], 'blocked', '采购采集已暂停', reason[:500])
            return {'paused': True, 'retired_downloads': len(rows), **value}

    def resume_purchase_downloads(self):
        # Old failed/held jobs stay in history. A new scheduled or supervised
        # request is required; resuming never replays the accumulated backlog.
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM settings WHERE key='purchase_download_pause'")
        return {'paused': False}

    def claim_sales_reconciliation(self, worker_token, operation_id=None):
        """Claim an interrupted sale for local import-receipt recovery.

        Without an ID, only an interrupted operation is selected once. An
        ordinary unknown needs a deliberate `reconcile-sales` command, so the
        service cannot busy-loop. Neither path contacts ERP or retries upload.
        """
        if not isinstance(worker_token, str) or len(worker_token) < 16:
            raise ValueError("执行者身份无效")
        if operation_id is not None and (not isinstance(operation_id, str) or
                                         not re.fullmatch(r"[0-9a-f]{32}", operation_id)):
            raise ValueError("销售网页操作 ID 无效")
        with self.store.connect(write=True) as db:
            blocker_sql = ("status IN ('running','needs_human') OR "
                "(status='uncertain' AND kind!='sales_order_import')")
            blockers = db.execute("SELECT 1 FROM web_operations WHERE " + blocker_sql +
                                  " LIMIT 1").fetchone()
            if blockers:
                return None
            if operation_id:
                row = db.execute("""SELECT * FROM web_operations WHERE id=?
                    AND kind='sales_order_import' AND status='uncertain'""",
                    (operation_id,)).fetchone()
            else:
                row = db.execute("""SELECT * FROM web_operations
                    WHERE kind='sales_order_import' AND status='uncertain'
                        AND stage='中断待核对' ORDER BY rowid LIMIT 1""").fetchone()
            if not row:
                return None
            stamp = now()
            changed = db.execute("""UPDATE web_operations SET status='running',
                stage='恢复导入记录',detail='仅恢复本地网页确认；缺失时人工核对，禁止再次上传',
                updated_at=?,worker_token=? WHERE id=? AND status='uncertain'""",
                (stamp, worker_token, row["id"]))
            if changed.rowcount != 1:
                return None
            self._event(db, row["id"], "running", "恢复导入记录",
                        "仅恢复本地网页确认；缺失时人工核对，禁止再次上传")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_assistant_sessions'").fetchone():
                db.execute("""UPDATE sales_assistant_sessions SET status='submitting',updated_at=?
                    WHERE submission_operation_id=? AND status='submission_unknown'""",
                    (stamp, row["id"]))
            result = self._row(db.execute(
                "SELECT * FROM web_operations WHERE id=?", (row["id"],)).fetchone())
            result["readback_only"] = True
            result["receipt_recovery_only"] = True
            return result

    def _owned(self, db, operation_id, worker_token):
        row = db.execute("SELECT * FROM web_operations WHERE id=?", (operation_id,)).fetchone()
        if not row or row["worker_token"] != worker_token or row["status"] != "running":
            raise WebOperationConflict("operation_not_owned", "网页执行状态已变化，停止当前进程")
        return row

    def progress(self, operation_id, worker_token, *, step, stage, detail):
        if not isinstance(step, int) or isinstance(step, bool) or len(stage) > 40 or len(detail) > 500:
            raise ValueError("进度参数无效")
        with self.store.connect(write=True) as db:
            row = self._owned(db, operation_id, worker_token)
            if step < row["step"] or step > row["total_steps"]:
                raise WebOperationConflict("progress_regressed", "进度不能倒退或超出范围")
            db.execute("""UPDATE web_operations SET step=?,stage=?,detail=?,updated_at=? WHERE id=?""",
                       (step, stage, detail, now(), operation_id))
            self._event(db, operation_id, "running", stage, detail)

    def finish(self, operation_id, worker_token, *, status, stage, detail, result=None):
        if status not in (*FINAL, "needs_human", "uncertain"):
            raise ValueError("网页结果状态无效")
        result = result or {}
        if not isinstance(result, dict) or len(dumps(result)) > 10000:
            raise ValueError("网页结果无效")
        if status == "succeeded" and (result.get("verified") is not True or
                                      not result.get("reference")):
            raise WebOperationConflict("readback_required", "缺少文件或 ERP 单据回查，不能判定成功")
        with self.store.connect(write=True) as db:
            row = self._owned(db, operation_id, worker_token)
            if row["kind"] == "sales_order_import":
                raise WebOperationConflict("sales_reconciliation_required",
                    "销售单只能由专用执行器原子更新会话和队列")
            stamp = now()
            db.execute("""UPDATE web_operations SET status=?,stage=?,detail=?,result_json=?,
                updated_at=?,finished_at=? WHERE id=?""",
                (status, stage[:40], detail[:500], dumps(result), stamp,
                 stamp if status in FINAL else "", operation_id))
            self._event(db, operation_id, status, stage[:40], detail[:500])

    def hold_sales_operation(self, operation_id, worker_token, *, status, code):
        """Atomically hold a sales session when its host subprocess cannot finish.

        This fallback cannot record success. It is only for an unavailable
        runner, timeout, or process crash; the normal sales finisher validates
        the validated workbook and website import receipt in the host runner.
        """
        if status not in {"blocked", "uncertain"} or not isinstance(code, str) or not code:
            raise ValueError("销售网页保留状态无效")
        with self.store.connect(write=True) as db:
            row = self._owned(db, operation_id, worker_token)
            if row["kind"] != "sales_order_import":
                raise WebOperationConflict("sales_operation_required", "仅销售网页操作可使用此恢复入口")
            stage = "提交受阻" if status == "blocked" else "导入待核对"
            detail = ("销售网页执行器未开始上传：" if status == "blocked" else
                      "网页导入结果需人工核对，禁止再次上传：") + code[:120]
            stamp = now()
            db.execute("""UPDATE web_operations SET status=?,stage=?,detail=?,
                result_json=?,updated_at=?,finished_at=? WHERE id=?""",
                (status, stage, detail, dumps({"code": code[:120], "verified": False,
                    "web_import_confirmed": False, "erp_readback_performed": False}),
                 stamp, stamp if status == "blocked" else "", operation_id))
            self._event(db, operation_id, status, stage, detail)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_assistant_sessions'").fetchone():
                db.execute("""UPDATE sales_assistant_sessions SET status=?,updated_at=?
                    WHERE submission_operation_id=? AND status='submitting'""",
                    ("submission_blocked" if status == "blocked" else "submission_unknown",
                     stamp, operation_id))

    def recover_interrupted(self):
        """Run only after the process-wide worker lock is acquired.

        Never retry a possibly submitted document. Even read-only collection is
        held because the browser may be awaiting a human verification session.
        """
        with self.store.connect(write=True) as db:
            rows = db.execute("SELECT id,kind FROM web_operations WHERE status='running'").fetchall()
            for row in rows:
                status = "needs_human" if row["kind"] == "purchase_download" else "uncertain"
                detail = ("执行进程中断；仅恢复本地网页确认，缺失时人工核对，禁止再次上传"
                          if row["kind"] == "sales_order_import" else
                          "执行进程中断；请核对原浏览器会话或 ERP 结果，禁止直接重试")
                db.execute("""UPDATE web_operations SET status=?,stage='中断待核对',detail=?,updated_at=?
                    WHERE id=?""", (status, detail, now(), row["id"]))
                self._event(db, row["id"], status, "中断待核对", detail)
                if row["kind"] == "sales_order_import" and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales_assistant_sessions'").fetchone():
                    db.execute("""UPDATE sales_assistant_sessions SET status='submission_unknown',
                        updated_at=? WHERE submission_operation_id=? AND status='submitting'""",
                        (now(), row["id"]))
            return len(rows)

    def frozen_payload(self, operation_id, worker_token):
        """Only the active worker can obtain the frozen business parameters."""
        with self.store.connect() as db:
            row = self._owned(db, operation_id, worker_token)
            payload = db.execute("SELECT payload_json FROM web_operation_payloads WHERE operation_id=?",
                                 (row["id"],)).fetchone()
            if not payload:
                raise WebOperationConflict("payload_missing", "冻结参数缺失，禁止执行外部操作")
            return json.loads(payload[0])

    def resolve(self, operation_id, *, status, detail, result, actor_id):
        """Human evidence closes a held operation; it never requeues it."""
        if status not in FINAL or not actor_id or not detail:
            raise ValueError("核对结论不完整")
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM web_operations WHERE id=?", (operation_id,)).fetchone()
            if not row or row["status"] not in {"needs_human", "uncertain"}:
                raise WebOperationConflict("resolution_not_allowed", "当前操作不在人工核对阶段")
            if row["kind"] == "sales_order_import":
                raise WebOperationConflict("sales_reconciliation_required",
                    "销售单仅能恢复原尝试的网页确认；没有确认时需人工核对，不可直接结案或重传")
            if status == "succeeded" and (not isinstance(result, dict) or
                                          result.get("verified") is not True or not result.get("reference")):
                raise WebOperationConflict("readback_required", "成功结论需要可核对的文件或 ERP 单号")
            stamp = now()
            evidence = dict(result or {}, resolved_by=actor_id)
            db.execute("""UPDATE web_operations SET status=?,stage='人工核对',detail=?,result_json=?,
                updated_at=?,finished_at=? WHERE id=?""",
                (status, detail[:500], dumps(evidence), stamp, stamp, operation_id))
            self._event(db, operation_id, status, "人工核对", detail[:500])
            return self._row(db.execute("SELECT * FROM web_operations WHERE id=?", (operation_id,)).fetchone())

    def list(self, user, limit=30):
        if not isinstance(user, dict):
            raise WebOperationConflict("role_forbidden", "请先登录", 403)
        if not 1 <= limit <= 100:
            raise ValueError("分页大小无效")
        with self.store.connect() as db:
            if _is_admin(user):
                rows = db.execute("SELECT * FROM web_operations ORDER BY rowid DESC LIMIT ?",
                                  (limit,)).fetchall()
            else:
                roles = set(_roles(user))
                clauses, params = [], []
                if "sales" in roles:
                    clauses.append("(role='sales' AND actor_id=?)")
                    params.append(_user_id(user))
                if "purchase" in roles:
                    clauses.append("role='purchase'")
                if not clauses:
                    raise WebOperationConflict("role_forbidden", "没有查看网页操作的岗位权限", 403)
                rows = db.execute("SELECT * FROM web_operations WHERE " + " OR ".join(clauses) +
                                  " ORDER BY rowid DESC LIMIT ?", (*params, limit)).fetchall()
            return [self._row(row) for row in rows]

    def detail(self, user, operation_id):
        with self.store.connect() as db:
            raw = db.execute("SELECT * FROM web_operations WHERE id=?", (operation_id,)).fetchone()
            if not raw or not isinstance(user, dict):
                raise WebOperationConflict("not_found", "网页操作不存在或无权查看", 404)
            visible = (_is_admin(user) or
                       raw["role"] == "sales" and "sales" in _roles(user) and raw["actor_id"] == _user_id(user) or
                       raw["role"] == "purchase" and "purchase" in _roles(user))
            if not visible:
                raise WebOperationConflict("not_found", "网页操作不存在或无权查看", 404)
            events = [dict(event) for event in db.execute("""SELECT status,stage,detail,created_at
                FROM web_operation_events WHERE operation_id=? ORDER BY rowid""", (operation_id,))]
        return {"operation": self._row(raw), "events": events}


def web_operation_routes(queue):
    from flask import Blueprint, g, jsonify, request

    bp = Blueprint("web_operations", __name__)

    @bp.errorhandler(WebOperationConflict)
    def conflict(error):
        return jsonify(ok=False, error=error.code, message=str(error)), error.status

    @bp.get("/api/v2/assistant/web-operations")
    def operations():
        try:
            limit = int(request.args.get("limit", "30"))
        except ValueError:
            return jsonify(ok=False, error="bad_request", message="分页大小无效"), 400
        return jsonify(ok=True, items=queue.list(g.current_user, min(100, max(1, limit))))

    @bp.get("/api/v2/assistant/web-operations/<operation_id>")
    def detail(operation_id):
        return jsonify(ok=True, **queue.detail(g.current_user, operation_id))

    return bp
