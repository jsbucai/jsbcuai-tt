#!/usr/bin/env python3
"""N150 serial loss/overflow worker. One persistent claim, no automatic replay."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.store import Store, dumps, timestamp
from core.sql_queries import Queries
from core.adjustment_web import WebAdjustmentJobs
from core.adjustments import AdjustmentFailure
from core.adjustment_web_import import prepare_workbook, submit_once, AdjustmentBrowser
from core.sales_web_transport import PlaywrightUploadSelectors
from tools.sales_web_operation_worker import _private_config, _host_scope, _web_identity
from tools.erp_human_challenge import wait_login


def pre_upload_failure(error):
    """Keep a known preparation failure distinct from authentication problems.

    Only allowlisted login codes are exposed. Browser exceptions can contain
    URLs or credentials, so arbitrary exception text must not reach the App.
    """
    if isinstance(error, AdjustmentFailure):
        return error
    messages = {
        'erp_service_unavailable': '管家婆网页暂时不可用（503），尚未上传 ERP，请稍后手动重试',
        'login_password_rejected': 'ERP 拒绝了登录密码，尚未上传；请管理员核对登录配置',
        'manual_verification_expired_or_failed': '本次登录验证已过期或未通过，尚未上传；请手动重试并处理新的微信验证提醒',
        'erp_main_not_ready': 'ERP 登录后的主页未加载完成，尚未上传；请稍后手动重试',
        'login_not_completed_without_verification': 'ERP 未完成登录，当前未检测到验证码；尚未上传，请稍后手动重试',
    }
    return AdjustmentFailure(messages.get(str(error),
        '网页登录或验证未完成，尚未上传 ERP；若收到微信验证提醒请先处理，再手动重试'),
        retryable=True)


def progress(jobs, identity, value):
    with jobs.store.connect(write=True) as db:
        db.execute("UPDATE adjustment_batches SET message=?,updated_at=? WHERE id=? AND status='running'",
                   (value, timestamp(), identity))


def send_batch(jobs, batch, config):
    with jobs.store.connect() as db:
        row = db.execute('SELECT binding FROM adjustment_web_bindings WHERE batch_id=?', (batch['id'],)).fetchone()
    if not row: raise AdjustmentFailure('旧执行器批次不能迁移后自动重传，请核对原单据')
    binding = json.loads(row[0])
    host = _host_scope(config)
    for key in ('profile_id', 'account_id', 'warehouse_id', 'warehouse_name', 'maker_name'):
        if binding.get(key) != getattr(host, key):
            raise AdjustmentFailure('报损报溢的账套／仓库配置已变化，需核对原清单')
    template = Path(config['template_path'])
    if hashlib.sha256(template.read_bytes()).hexdigest() != config['template_sha256']:
        raise AdjustmentFailure('原始 ERP 模板发生变化，尚未提交')
    marker = jobs.store.runtime / 'adjustments' / batch['id'] / 'submission.json'
    try:
        path, number = prepare_workbook(jobs.store.runtime, template, batch, binding)
    except Exception:
        if marker.exists(): raise
        raise AdjustmentFailure('报损报溢表格校验未通过，尚未提交 ERP') from None
    if marker.exists():
        return submit_once(path, number, batch, None, binding)
    for identity in binding['items']:
        current = jobs.queries.inventory_lookup(identity['sku'])
        if not current.get('found') or any(str(current.get(key) or '') != identity[key]
                for key in ('profile_id', 'warehouse_id', 'product_id', 'sku_id', 'base_unit')):
            raise AdjustmentFailure('商品、仓库或单位身份已变化，尚未上传 ERP，请重新核对清单')
    def report(value): progress(jobs, batch['id'], value)
    try:
        sys.path.insert(0, config['collector_source_path'])
        from purchase_export_collector.collector import _login, _read_credentials, _account_scope
        from playwright.sync_api import sync_playwright
        credentials = _read_credentials(Path(config['credentials_path']))
        if (credentials['company'], credentials['username']) != (host.tenant_name, host.operator_name):
            raise ValueError('login_identity_mismatch')
        profile = Path(config['adjustment_browser_profile'])
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        if profile.is_symlink(): raise ValueError('invalid_browser_profile')
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), headless=False,
                executable_path=config['chromium_path'], viewport={'width': 1920, 'height': 930})
            try:
                report('N150 正在登录 ERP（罗炳升），尚未导入')
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15000)
                page = _login(page, credentials, wait_for_human=True,
                    human_handler=lambda page: wait_login(page, batch['id'], report))
                if _account_scope(credentials['company'], page.url) != host.account_id:
                    raise ValueError('account_mismatch')
                browser = AdjustmentBrowser(page, PlaywrightUploadSelectors(progress_timeout_ms=60000),
                    identity=lambda: _web_identity(page, host))
                return submit_once(path, number, batch, browser, binding, report)
            finally:
                context.close()
    except Exception as error:
        if marker.exists():
            error.document_no = number
            raise
        raise pre_upload_failure(error) from None


def heartbeat(jobs):
    with jobs.store.connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('adjustment_web_heartbeat',?)", (str(time.time()),))


def run_one(jobs, config):
    heartbeat(jobs)
    if not jobs.enabled(): return 'paused'
    with (jobs.store.runtime / '.web-browser-lane.lock').open('a+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 'busy'
        if Path('/proc/meminfo').exists():
            available = next((int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                              if line.startswith('MemAvailable:')), 0)
            if available < 512 * 1024: return 'busy'
        with jobs.store.connect(write=True) as db:
            if db.execute("SELECT 1 FROM settings WHERE key='restoration_hold' AND value='true'").fetchone(): return 'paused'
            if db.execute("SELECT 1 FROM update_jobs WHERE status='running'").fetchone(): return 'busy'
            # A restart never blindly resends a running batch. A durable receipt
            # can finish it; without an attempt marker it is safe to retry manually.
            interrupted = list(db.execute("SELECT * FROM adjustment_batches WHERE status='running'"))
        for row in interrupted:
            batch = jobs._result(row)
            marker = jobs.store.runtime / 'adjustments' / batch['id'] / 'submission.json'
            if marker.exists():
                jobs.sender = lambda value: send_batch(jobs, value, config)
                jobs._send(batch)
            else:
                jobs._finish(batch, 'failed', 'N150 在提交前中断，尚未上传 ERP，可手动重试', retryable=True)
        batch = jobs._take()
        if not batch: return 'empty'
        jobs.sender = lambda value: send_batch(jobs, value, config)
        jobs._send(batch)
        return batch['id']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    jobs = WebAdjustmentJobs(Store(args.runtime), Queries(Store(args.runtime)))
    config = _private_config(args.config)
    if not jobs._acquire_worker(): raise SystemExit('adjustment_worker_already_active')
    stop = threading.Event()
    def keep_alive():
        while not stop.wait(30):
            try: heartbeat(jobs)
            except Exception: pass  # The durable batch/attempt state still governs recovery.
    threading.Thread(target=keep_alive, daemon=True, name='adjustment-heartbeat').start()
    try:
        while True:
            run_one(jobs, config)
            if args.once: break
            time.sleep(5)
    finally:
        stop.set()
        jobs._release_worker()


if __name__ == '__main__': main()
