#!/usr/bin/env python3
"""N150 single browser lane. Run as the workbench data owner (ulcollector).

The existing collector remains the sole purchase-download implementation.
Sales uploads once and completes from the website confirmation. Recovery
reads only its local receipt. Purchase import remains separately gated.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.store import Store
from core.web_operations import WebOperations
from core.sales_trial_authorization import sales_write_mode, trial_request_matches


DEFAULT_TRIAL = Path("/opt/ul-purchase-collector/bin/run-trial.sh")
DEFAULT_TRIAL_ROOT = Path("/var/lib/ul-purchase-collector/trial")
DEFAULT_SALES_CONFIG = Path("/etc/ul-sales-web-worker/config.json")
SALES_SCRIPT = Path(__file__).with_name("sales_web_operation_worker.py")
SALES_TIMEOUT_SECONDS = 15 * 60


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _purchase_download(queue, operation, token, script, root):
    if not script.is_file() or script.is_symlink() or not os.access(script, os.X_OK):
        queue.finish(operation["id"], token, status="blocked", stage="环境待核对",
                     detail="采购采集器未安装或不可执行")
        return
    previous = _json(root / "latest.json").get("snapshot_id")
    queue.progress(operation["id"], token, step=1, stage="启动采购网页", detail="等待网页登录和筛选")
    status_path = root / 'collector-status.json'
    prior_stamp = status_path.stat().st_mtime_ns if status_path.exists() else 0
    process = subprocess.Popen([str(script)], cwd=script.parent, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    started = time.monotonic()
    def stop_download():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    shown = ""
    while process.poll() is None:
        state = _json(root / "collector-status.json")
        status = str(state.get("status") or "")
        fresh_status = status_path.exists() and status_path.stat().st_mtime_ns > prior_stamp
        login_problem = fresh_status and (status == 'needs_human' or
            status == 'failed' and any(word in str(state.get('reason', '')) for word in ('login', 'auth', 'password', 'verification')))
        if login_problem or time.monotonic() - started > 10 * 60:
            stop_download()
            reason = ('网页登录需要人工处理，采购采集暂停；上次正确快照保留' if login_problem else
                      '采购网页超过十分钟未完成，采集暂停；上次正确快照保留')
            queue.finish(operation['id'], token, status='blocked', stage='采购采集已暂停', detail=reason)
            queue.pause_purchase_downloads(reason)
            return
        if status == "needs_human":
            stage, detail = "等待人工验证", "保留浏览器会话；验证码需人工处理"
        elif status == "running":
            stage, detail = "网页采集中", "正在筛选、导出并验证采购未交文件"
        else:
            stage, detail = "启动采购网页", "等待网页登录和筛选"
        if stage != shown:
            queue.progress(operation["id"], token, step=2, stage=stage, detail=detail)
            shown = stage
        time.sleep(2)
    code = process.wait()
    state = _json(root / "collector-status.json")
    if state.get("status") == "needs_human":
        queue.finish(operation["id"], token, status="needs_human", stage="等待人工验证",
                     detail="验证码或中断需在原浏览器会话人工核对")
        queue.pause_purchase_downloads('网页登录需要人工处理，采购采集暂停；上次正确快照保留')
        return
    if code != 0 or state.get("status") != "succeeded":
        queue.finish(operation["id"], token, status="failed", stage="采集失败",
                     detail=f"采集器退出 {code}；原采购快照保留")
        if any(word in str(state.get('reason', '')) for word in ('login', 'auth', 'password', 'verification')):
            queue.pause_purchase_downloads('网页登录失败，采购采集暂停；上次正确快照保留')
        return
    queue.progress(operation["id"], token, step=3, stage="核对下载文件", detail="核对新快照、行数与文件校验值")
    latest = _json(root / "latest.json")
    snapshot_id = str(latest.get("snapshot_id") or "")
    relative = str(latest.get("manifest") or "")
    if (not snapshot_id or snapshot_id == previous or
            relative != f"snapshots/{snapshot_id}/manifest.json"):
        queue.finish(operation["id"], token, status="uncertain", stage="下载待核对",
                     detail="未找到本次生成的独立快照；禁止把旧文件算作成功")
        return
    snapshot = root / "snapshots" / snapshot_id
    manifest = _json(snapshot / "manifest.json")
    file = snapshot / "purchase-unfinished.xlsx"
    if (manifest.get("status") != "validated" or manifest.get("snapshot_id") != snapshot_id or
            not isinstance(manifest.get("row_count"), int) or manifest["row_count"] < 0 or
            manifest.get("row_count") != manifest.get("web_row_count") or
            not file.is_file() or file.is_symlink() or
            file.stat().st_size != manifest.get("bytes") or
            hashlib.sha256(file.read_bytes()).hexdigest() != manifest.get("sha256")):
        queue.finish(operation["id"], token, status="uncertain", stage="文件待核对",
                     detail="下载文件、网页行数或校验值不一致；原快照不发布")
        return
    queue.progress(operation["id"], token, step=4, stage="校验完成", detail="已取得完整文件；仍为独立试验快照")
    queue.finish(operation["id"], token, status="succeeded", stage="下载完成",
                 detail=f"采购未交 {manifest['row_count']} 行；正式发布仍需单位与覆盖验收",
                 result={"verified": True, "reference": snapshot_id,
                         "row_count": manifest["row_count"], "sha256": manifest["sha256"],
                         "captured_at_utc": manifest.get("captured_at_utc"),
                         "units_verified": manifest.get("units_verified") is True,
                         "coverage_complete": manifest.get("coverage_complete") is True,
                         "formal_business_publish": False})


def _sales_child_process(queue, operation, token, frozen, *, config_path, readback_only):
    """Run the host-only Playwright process while this worker owns the lane."""
    if (not config_path or config_path.is_symlink() or not config_path.is_file() or
            config_path.stat().st_mode & 0o077):
        return {"status": "blocked", "code": "sales_host_config_unavailable"}
    config = _json(config_path)
    if not readback_only:
        mode = sales_write_mode(config.get("enabled"), config.get("acceptance_complete"),
                                config.get("trial_authorization"))
        if mode == "disabled":
            return {"status": "blocked", "code": "sales_writer_disabled"}
        if mode == "trial" and not trial_request_matches(
                config["trial_authorization"], frozen, operation):
            return {"status": "blocked", "code": "sales_trial_scope_mismatch"}
    python = Path(config.get("runner_python") or "")
    if (not python.is_absolute() or
            not python.is_file() or not os.access(python, os.X_OK) or
            not SALES_SCRIPT.is_file()):
        return {"status": "blocked", "code": "sales_host_runner_unavailable"}
    command = [str(python), str(SALES_SCRIPT), "--config", str(config_path),
               "--runtime", str(queue.store.runtime)]
    status_path = (queue.store.runtime / "sales-web-worker-status" /
                   (operation["id"] + "-" + token + ".json"))
    command.extend(["--status-path", str(status_path)])
    if readback_only:
        command.append("--readback-only")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, cwd=SALES_SCRIPT.parent,
                               start_new_session=False)
    try:
        process.stdin.write(json.dumps({"operation": operation, "payload": frozen,
                                       "worker_token": token},
                                       ensure_ascii=False, separators=(",", ":")).encode())
        process.stdin.close()
        started = time.monotonic()
        shown = ""
        while process.poll() is None:
            current = _json(status_path)
            state = current.get("status")
            if state == "needs_human":
                stage, detail = "等待人工验证", "保留当前浏览器；ERP 登录验证需人工处理"
            elif state == "running":
                stage, detail = "ERP 网页处理中", "等待唯一上传尝试和网页完整导入确认"
            elif state == "recovering_receipt" or readback_only:
                stage, detail = "恢复导入记录", "仅检查本地确认记录；不查询 ERP，不再次上传"
            else:
                stage, detail = "启动销售网页", "核对登录、账套和冻结单据"
            if stage != shown:
                queue.progress(operation["id"], token, step=max(2, operation.get("step", 0)),
                               stage=stage, detail=detail)
                shown = stage
            if state != "needs_human" and time.monotonic() - started > SALES_TIMEOUT_SECONDS:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return {"status": "unknown", "code": "sales_worker_timeout"}
            time.sleep(2)
        output = process.stdout.read(65536)
        if process.wait() != 0 or len(output) >= 65536:
            return {"status": "unknown", "code": "sales_runner_exited"}
        try:
            result = json.loads(output)
        except (TypeError, ValueError):
            return {"status": "unknown", "code": "sales_runner_result_invalid"}
        return _validated_sales_runner_result(result, operation["target_id"], frozen.get("line_count"),
            frozen.get('document_numbers') if frozen.get('schema') == 'sales_web_import_v2' else None)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _validated_sales_runner_result(result, order_number, expected_rows, document_numbers=None):
    """Accept web-confirmed completion only; never reinterpret ERP verification."""
    invalid = {"status": "unknown", "code": "sales_runner_result_invalid"}
    if not isinstance(result, dict) or result.get("status") not in {"imported", "unknown", "blocked"}:
        return invalid
    if result["status"] == "imported" and (
            result.get("code") != "web_import_confirmed" or
            result.get("reference") != order_number or
            result.get("completion_basis") != "web_import_confirmation" or
            result.get("erp_readback_performed") is not False or
            result.get("unit_verified") is not False or
            result.get("table_verified") is not True or
            not isinstance(result.get("file_sha256"), str) or
            re.fullmatch(r"[0-9a-f]{64}", result["file_sha256"]) is None or
            type(expected_rows) is not int or expected_rows <= 0 or
            type(result.get("imported_rows")) is not int or
            type(result.get("expected_rows")) is not int or
            result["imported_rows"] != result["expected_rows"] or
            result["expected_rows"] != expected_rows):
        return invalid
    if document_numbers is not None and result['status'] == 'imported' and (
            not isinstance(document_numbers, list) or len(document_numbers) != expected_rows or
            len(set(document_numbers)) != len(document_numbers) or
            not document_numbers or document_numbers[0] != order_number or
            result.get('document_numbers') != document_numbers or
            type(result.get('document_count')) is not int or result['document_count'] != len(document_numbers)):
        return invalid
    return result


def _sales_order_import(queue, operation, token, frozen, *, config_path,
                        readback_only=False, runner=_sales_child_process):
    if (not isinstance(frozen, dict) or frozen.get("schema") not in {"sales_web_import_v1", "sales_web_import_v2"} or
            frozen.get("order_number") != operation["target_id"] or
            frozen.get("profile_id") != operation["profile_id"]):
        queue.hold_sales_operation(operation["id"], token,
                                   status="uncertain" if readback_only else "blocked",
                                   code="sales_frozen_payload_invalid")
        return
    queue.progress(operation["id"], token, step=max(1, operation.get("step", 0)),
                   stage="核对冻结稿", detail="准备使用唯一单号和原确认版本")
    try:
        result = runner(queue, operation, token, frozen, config_path=config_path,
                        readback_only=readback_only)
    except Exception:
        result = {"status": "unknown", "code": "sales_runner_unavailable"}
    # The child normally calls SalesAssistant.finish_web_import in its own
    # complete-dependency venv, atomically updating both SQLite records.
    with queue.store.connect() as db:
        recorded = db.execute("SELECT status FROM web_operations WHERE id=?",
                              (operation["id"],)).fetchone()
    if recorded and recorded["status"] != "running":
        return
    code = str(result.get("code") or "sales_result_unknown")[:80]
    intent_id = str(uuid.UUID(operation["id"]))
    attempted = (queue.store.runtime / "sales-web-import" / intent_id / "attempt.json").is_file()
    hold = "blocked" if result.get("status") == "blocked" and not attempted and not readback_only else "uncertain"
    queue.hold_sales_operation(operation["id"], token, status=hold, code=code)


def _run_one(queue, script, root, sales_config=None, sales_runner=_sales_child_process):
    lock_path = queue.store.runtime / ".web-browser-lane.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        queue.recover_interrupted()
        token = uuid.uuid4().hex
        operation = queue.claim_sales_reconciliation(token)
        if operation is None:
            operation = queue.claim_next(token)
        if not operation:
            return "empty"
        try:
            frozen = queue.frozen_payload(operation["id"], token)
            if operation["kind"] == "purchase_download":
                if frozen.get("slot") != operation["target_id"]:
                    queue.finish(operation["id"], token, status="blocked", stage="参数待核对",
                                 detail="冻结的采购下载时段与队列目标不一致")
                    return operation["id"]
                _purchase_download(queue, operation, token, script, root)
            elif operation["kind"] == "sales_order_import":
                _sales_order_import(queue, operation, token, frozen,
                    config_path=sales_config,
                    readback_only=operation.get("readback_only") is True,
                    runner=sales_runner)
            else:
                # No browser click is made. The job remains visible and can be
                # diagnosed without falsely reporting that ERP was changed.
                queue.finish(operation["id"], token, status="blocked", stage="执行器待验收",
                             detail="此单据类型尚未完成网页登录、单次上传和 ERP 精确回查验收")
        except BaseException:
            # Preserve running as an explicit hold on next worker start.
            raise
        return operation["id"]


def _reconcile_sales_once(queue, operation_id, sales_config=None,
                          sales_runner=_sales_child_process):
    """Explicit request: restore one local receipt; never contact ERP or upload."""
    lock_path = queue.store.runtime / ".web-browser-lane.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        queue.recover_interrupted()
        token = uuid.uuid4().hex
        operation = queue.claim_sales_reconciliation(token, operation_id)
        if not operation:
            return "not_uncertain_or_busy"
        frozen = queue.frozen_payload(operation["id"], token)
        _sales_order_import(queue, operation, token, frozen,
            config_path=sales_config, readback_only=True, runner=sales_runner)
        return operation["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--trial-script", type=Path, default=DEFAULT_TRIAL)
    parser.add_argument("--trial-root", type=Path, default=DEFAULT_TRIAL_ROOT)
    parser.add_argument("--sales-config", type=Path, default=DEFAULT_SALES_CONFIG)
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("enqueue-download")
    add.add_argument("--slot", required=True, help="Asia/Shanghai date/window or auto")
    sub.add_parser("run-once")
    sub.add_parser("serve")
    pause = sub.add_parser('pause-downloads')
    pause.add_argument('--reason', required=True)
    sub.add_parser('resume-downloads')
    reconcile = sub.add_parser("reconcile-sales")
    reconcile.add_argument("--operation-id", required=True,
                           help="original 32-character sales queue operation ID")
    args = parser.parse_args(argv)
    queue = WebOperations(Store(args.runtime))
    if args.action in {'pause-downloads', 'resume-downloads'}:
        result = queue.pause_purchase_downloads(args.reason, actor='admin:requested') if args.action == 'pause-downloads' else queue.resume_purchase_downloads()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.action == "enqueue-download":
        if args.slot == "auto":
            local = datetime.now(ZoneInfo("Asia/Shanghai"))
            windows = [local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                       for hour, minute in ((7, 40), (11, 40), (16, 10))]
            valid = [value for value in windows if value <= local < value + timedelta(minutes=15)]
            if not valid:
                parser.error("auto slot is only valid within 15 minutes of a scheduled collection")
            args.slot = valid[0].strftime("%Y-%m-%d-%H%M")
        else:
            datetime.strptime(args.slot, "%Y-%m-%d-%H%M")
        with queue.store.connect() as db:
            if queue._purchase_pause(db):
                print(json.dumps({'status': 'purchase_download_paused'}))
                return 0
        result = queue.enqueue(kind="purchase_download", actor_id="system:purchase-export",
            role="purchase", profile_id="08390", target_id=args.slot,
            key="purchase-download:" + args.slot, payload={"slot": args.slot}, total_steps=4)
        print(json.dumps({"id": result["operation"]["id"], "replayed": result["replayed"]}))
        return 0
    if args.action == "run-once":
        print(_run_one(queue, args.trial_script, args.trial_root, args.sales_config))
        return 0
    if args.action == "reconcile-sales":
        print(_reconcile_sales_once(queue, args.operation_id, args.sales_config))
        return 0
    while True:
        result = _run_one(queue, args.trial_script, args.trial_root, args.sales_config)
        if result in {"empty", "busy"}:
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
