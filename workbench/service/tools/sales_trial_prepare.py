#!/usr/bin/env python3
"""Prepare one private two-line LS3073 acceptance order; never upload or enqueue.

Reads an existing private host preflight and the local catalog SQLite database
in read-only mode. All generated state lives in a new directory directly under
this preview checkout's service/runtime. The temporary policy switch is made
and cleared in one transaction in that isolated database only.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.daily_work import ensure_schema
from core.sales_assistant import SalesAssistant
from core.sales_web_import import (WebImportConfig, build_sales_web_import_xls,
                                   catalog_spec_dimension, _validate_template)
from core.sales_web_queue import (POLICY_SETTING, line_document_numbers,
                                  prepare_sales_web_payload)
from core.store import Store, dumps, timestamp
from core.unit_policy import unit_evidence


CUSTOMER = '备用零售客户'
SKU = 'LS3073'
WAREHOUSE = '江苏优螺'
MAKER = '罗健树'
HANDLER = '李瑄'
DIMENSION = 'M20X80'
RUNTIME_PARENT = Path(__file__).resolve().parents[1] / 'runtime'
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class TrialPreparationBlocked(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _block(code: str):
    raise TrialPreparationBlocked(code)


def _private_file(path: Path, *, max_bytes: int | None = None) -> bytes:
    if (not path.is_absolute() or path.is_symlink() or not path.is_file() or
            path.stat().st_mode & 0o077):
        _block('private_source_unavailable')
    if max_bytes is not None and path.stat().st_size > max_bytes:
        _block('private_source_too_large')
    return path.read_bytes()


def _preflight(path: Path) -> tuple[dict, dict]:
    raw_report = _private_file(path, max_bytes=1_000_000)
    try:
        report = json.loads(raw_report.decode('utf-8-sig'))
        host = report['host_config_candidate']
        account = report['account']
        binding = report['customer_binding']
        staff = report['staff']
        warehouse = report['warehouse']
        observed = datetime.fromisoformat(report['observed_at_utc'])
        customer_observed = datetime.fromisoformat(binding['observed_at'])
    except (KeyError, TypeError, ValueError, UnicodeError):
        _block('sales_preflight_invalid')
    if (not isinstance(report, dict) or report.get('schema') != 'sales_web_host_preflight_v1' or
            not isinstance(host, dict) or host.get('enabled') is not False or
            host.get('acceptance_complete') is not False or
            host.get('trial_authorization') is not None or
            not isinstance(account, dict) or
            account.get('profile_id') != host.get('profile_id') or
            account.get('account_id') != host.get('account_id') or
            account.get('tenant_name') != host.get('tenant_name') or
            account.get('operator_name') != host.get('operator_name') or
            not isinstance(binding, dict) or binding.get('customer_name') != CUSTOMER or
            binding.get('profile_id') != host.get('profile_id') or
            binding.get('account_id') != host.get('account_id') or
            type(binding.get('match_count')) is not int or binding['match_count'] != 1 or
            binding.get('source') != 'erp_customer_exact_lookup' or
            not isinstance(binding.get('erp_customer_id'), str) or
            not binding['erp_customer_id'].strip() or
            not isinstance(binding.get('source_hash'), str) or
            not SHA256.fullmatch(binding['source_hash']) or
            customer_observed.tzinfo is None or not timedelta(0) <=
            datetime.now(timezone.utc) - customer_observed.astimezone(timezone.utc) <= timedelta(hours=24) or
            not isinstance(staff, dict) or
            not isinstance(staff.get(MAKER), dict) or
            not isinstance(staff.get(HANDLER), dict) or
            type(staff[MAKER].get('match_count')) is not int or
            staff[MAKER]['match_count'] != 1 or
            type(staff[HANDLER].get('match_count')) is not int or
            staff[HANDLER]['match_count'] != 1 or
            not isinstance(staff[MAKER].get('erp_id'), str) or
            not staff[MAKER]['erp_id'] or
            not isinstance(staff[HANDLER].get('erp_id'), str) or
            not staff[HANDLER]['erp_id'] or
            report.get('maker_matches_erp_token_employee') is not True or
            not isinstance(warehouse, dict) or
            type(warehouse.get('match_count')) is not int or
            warehouse['match_count'] != 1 or
            warehouse.get('erp_id') != host.get('warehouse_id') or
            host.get('warehouse_name') != WAREHOUSE or
            host.get('maker_name') != MAKER or
            not isinstance(host.get('allowed_handlers'), list) or
            HANDLER not in host.get('allowed_handlers', ()) or
            not isinstance(host.get('department_name'), str) or
            not host['department_name'].strip() or
            not isinstance(host.get('profile_id'), str) or not host['profile_id'] or
            not isinstance(host.get('warehouse_id'), str) or not host['warehouse_id'] or
            not isinstance(host.get('account_id'), str) or
            not SHA256.fullmatch(host['account_id']) or
            not isinstance(host.get('template_sha256'), str) or
            not SHA256.fullmatch(host['template_sha256']) or
            report.get('template_sha256') != host['template_sha256'] or
            observed.tzinfo is None or not timedelta(0) <=
            datetime.now(timezone.utc) - observed.astimezone(timezone.utc) <= timedelta(hours=24)):
        _block('sales_preflight_scope_invalid')
    try:
        template = Path(host['template_path'])
        raw = _private_file(template)
    except (KeyError, TypeError, ValueError):
        _block('sales_template_unavailable')
    if hashlib.sha256(raw).hexdigest() != host['template_sha256']:
        _block('sales_template_changed')
    try:
        _validate_template(template)
    except (OSError, ValueError):
        _block('sales_template_invalid')
    return report, host


def _catalog_product(path: Path, *, profile: str, warehouse: str) -> tuple[dict, str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        _block('catalog_source_unavailable')
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as source:
            source.row_factory = sqlite3.Row
            source.execute('PRAGMA query_only=ON')
            source.execute('BEGIN')
            version = source.execute("SELECT value FROM settings WHERE key='data_version'").fetchone()
            rows = source.execute(
                "SELECT id,payload FROM records WHERE kind='inventory' AND sku=?", (SKU,)).fetchall()
            data_version = json.loads(version['value']) if version else None
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _block('catalog_snapshot_unavailable')
    if not isinstance(data_version, str) or not data_version:
        _block('catalog_data_version_missing')
    matches = []
    for row in rows:
        try:
            item = json.loads(row['payload'])
        except (TypeError, ValueError):
            _block('catalog_record_invalid')
        if (isinstance(item, dict) and item.get('profile_id') == profile and
                item.get('warehouse_id') == warehouse):
            matches.append((row, item))
    if len(matches) != 1:
        _block('catalog_identity_not_unique')
    row, product = matches[0]
    if (str(product.get('base_unit')) != '1' or
            not all(str(product.get(key) or '').strip() for key in
                ('product_id', 'sku_id', 'product_name')) or
            product.get('sku') not in (None, '', SKU) or
            product.get('warehouse_name') not in (None, '', WAREHOUSE) or
            catalog_spec_dimension(product.get('spec')) != DIMENSION or
            unit_evidence(product.get(key) for key in ('unit_name', 'base_unit_name', 'unit')) is None):
        _block('catalog_product_not_reviewed')
    return product, data_version, hashlib.sha256(row['payload'].encode()).hexdigest()


def _new_runtime(path: Path) -> Path:
    allowed = RUNTIME_PARENT.resolve()
    if (not path.is_absolute() or path.is_symlink() or path.exists() or
            path.parent.resolve() != allowed or not path.name.startswith('sales-trial-')):
        _block('isolated_runtime_required')
    path.mkdir(mode=0o700)
    return path


def _write_private(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def prepare(report_path: Path, catalog_path: Path, runtime_path: Path,
            delivery: date) -> dict:
    if not isinstance(delivery, date) or isinstance(delivery, datetime):
        _block('delivery_invalid')
    if delivery < datetime.now(ZoneInfo('Asia/Shanghai')).date():
        _block('delivery_before_order_date')
    report, host = _preflight(report_path)
    product, data_version, product_sha = _catalog_product(
        catalog_path, profile=host['profile_id'], warehouse=host['warehouse_id'])
    previous_umask = os.umask(0o077)
    try:
        runtime = _new_runtime(runtime_path)
        store = Store(runtime)
        with store.connect(write=True) as db:
            db.execute('''CREATE TABLE staff_users(id TEXT PRIMARY KEY,username TEXT,
                display_name TEXT,password_hash TEXT,roles TEXT,is_admin INTEGER,
                active INTEGER,packing_limit INTEGER,created_at TEXT,updated_at TEXT)''')
            db.execute('INSERT INTO staff_users VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('trial-sales', 'trial-sales', HANDLER, '', dumps(['sales']), 0, 1, 3, '', ''))
            for key, value in {'data_version': data_version, 'restoration_hold': False}.items():
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, dumps(value)))
            db.execute("INSERT INTO records(kind,id,sku,payload) VALUES ('inventory','trial-product',?,?)",
                       (SKU, dumps(product)))
        ensure_schema(store)
        with store.connect(write=True) as db:
            db.execute('''INSERT INTO sales_customer_owners
                (profile_id,customer_name,owner_id,status,source,updated_at)
                VALUES (?,?,?,?,?,?)''',
                (host['profile_id'], CUSTOMER, 'trial-sales', 'assigned',
                 'customer_confirmed', timestamp()))
        engine = SalesAssistant(store)
        actor = {'id': 'trial-sales', 'roles': ['sales'], 'active': True, 'is_admin': False}
        session = engine.create(actor, {'profile_id': host['profile_id'],
            'customer_name': CUSTOMER, 'idempotency_key': 'trial-prepare-create'})['session']
        numbers = line_document_numbers(host['profile_id'], session['id'], 2)
        number = numbers[0]
        edited = engine.edit(actor, session['id'], {'version': session['version'],
            'idempotency_key': 'trial-prepare-edit', 'customer_order_no': 'TEST-' + number,
            'delivery_date': delivery.isoformat(),
            'note': 'UL随行两行同商品录单验收；未审核，验收后由用户删除',
            'lines': [{'product_code': SKU, 'quantity': str(quantity),
                       'unit_price': '1.13', 'tax_basis': 'tax_inclusive',
                       'tax_rate': '13', 'unit': '支',
                       'warehouse': host['warehouse_id'],
                       'delivery_date': delivery.isoformat()} for quantity in (1, 2)]})['session']
        confirmed = engine.confirm(actor, session['id'], {'version': edited['version'],
            'idempotency_key': 'trial-prepare-confirm'})['session']
        if confirmed['status'] != 'confirmed' or confirmed['draft']['issues']:
            _block('trial_draft_not_confirmed')
        policy = {'enabled': True, 'profile_id': host['profile_id'],
            'account_id': host['account_id'], 'department_name': host['department_name'],
            'maker_name': host['maker_name'], 'handler_by_owner': {'trial-sales': HANDLER},
            'warehouse_id': host['warehouse_id'], 'warehouse_name': host['warehouse_name'],
            'template_sha256': host['template_sha256'],
            'customer_bindings': {CUSTOMER: report['customer_binding']},
            'retail_test_session_id': session['id']}
        with store.connect(write=True) as db:
            # The enabled policy never commits: preview-only derivation, then
            # the same transaction stores the disabled value.
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                       (POLICY_SETTING, dumps(policy)))
            row = db.execute('SELECT * FROM sales_assistant_sessions WHERE id=?',
                             (session['id'],)).fetchone()
            derived_number, payload = prepare_sales_web_payload(db, row, 'trial-sales')
            policy['enabled'] = False
            db.execute('UPDATE settings SET value=? WHERE key=?',
                       (dumps(policy), POLICY_SETTING))
        if derived_number != number or tuple(payload.get('document_numbers', ())) != numbers:
            _block('trial_order_number_changed')
        config = WebImportConfig(Path(host['template_path']), runtime / 'review-order.xls',
            number, date.fromisoformat(payload['import_config']['order_date']),
            host['profile_id'], payload['import_config']['data_version'],
            'trial-sales', host['department_name'], MAKER, HANDLER,
            host['warehouse_id'], WAREHOUSE, 'line_document', tuple(payload['document_numbers']))
        result = build_sales_web_import_xls(payload['frozen_session'], config)
        draft = json.loads(payload['frozen_session']['draft'])
        lines = draft['lines']
        if (len(lines) != 2 or len(result.lines) != 2 or
                [line['product_code'] for line in lines] != [SKU, SKU] or
                [line['quantity'] for line in lines] != ['1', '2'] or
                [line['unit_price'] for line in lines] != ['1.13', '1.13'] or
                [line['summary_dimension'] for line in lines] != [DIMENSION, DIMENSION] or
                [line['summary_dimension_source'] for line in lines] !=
                    ['catalog_spec_shape', 'catalog_spec_shape'] or
                [line.line_id for line in result.lines] != ['1', '2'] or
                [line.sku for line in result.lines] != [SKU, SKU] or
                [line.quantity for line in result.lines] != ['1', '2'] or
                [line.untaxed_unit_price for line in result.lines] != ['1.000', '1.000'] or
                [line.untaxed_line_amount for line in result.lines] != ['1.00', '2.00'] or
                not result.summary.endswith('#交期=' + delivery.isoformat())):
            _block('trial_review_values_mismatch')
        net = sum(Decimal(line.untaxed_line_amount) for line in result.lines)
        tax = sum((Decimal(line.untaxed_line_amount) * Decimal('0.13')).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP) for line in result.lines)
        taxed = net + tax
        if net != Decimal('3.00') or taxed != Decimal('3.39'):
            _block('trial_review_total_mismatch')
        with store.connect() as db:
            saved_policy = json.loads(db.execute('SELECT value FROM settings WHERE key=?',
                (POLICY_SETTING,)).fetchone()[0])
            saved = db.execute('SELECT status,submission_operation_id FROM sales_assistant_sessions '
                               'WHERE id=?', (session['id'],)).fetchone()
            queued = db.execute('SELECT count(*) FROM web_operations').fetchone()[0]
        if saved_policy.get('enabled') is not False or saved['status'] != 'confirmed' or \
                saved['submission_operation_id'] or queued:
            _block('trial_isolation_failed')
        authorization = {'schema': 'sales_web_trial_v2', 'session_id': session['id'],
            'confirmed_hash': payload['confirmed_hash'], 'order_number': number,
            'customer_name': CUSTOMER, 'line_count': 2, 'taxed_total': '3.39',
            'payload_schema': payload['schema'], 'document_mode': payload['document_mode'],
            'document_numbers': list(payload['document_numbers'])}
        review = {'status': 'prepared_not_uploaded', 'session_id': session['id'],
            'order_number': number, 'confirmed_hash': confirmed['confirmed_hash'],
            'document_mode': payload['document_mode'], 'document_numbers': list(payload['document_numbers']),
            'confirmed_version': confirmed['version'], 'customer': CUSTOMER,
            'maker': MAKER, 'handler': HANDLER, 'warehouse': WAREHOUSE,
            'delivery_date': delivery.isoformat(), 'tax_rate': '13%',
            'catalog_data_version': data_version, 'catalog_product_sha256': product_sha,
            'lines': [{'sku': line.sku, 'quantity': line.quantity, 'unit': '支',
                       'order_number': line.order_number,
                       'summary_dimension': lines[index]['summary_dimension'],
                       'summary_dimension_source': lines[index]['summary_dimension_source'],
                       'untaxed_unit_price': line.untaxed_unit_price,
                       'untaxed_amount': line.untaxed_line_amount,
                       'detail_note': line.detail_note}
                      for index, line in enumerate(result.lines)],
            'untaxed_total': '3.00', 'taxed_total': '3.39', 'summary': result.summary,
            'file_sha256': result.sha256, 'file': str(result.path),
            'trial_authorization_candidate': authorization,
            'isolated_policy_enabled': False, 'host_candidate_enabled': False,
            'queue_operations': 0, 'erp_writes': 0}
        _write_private(runtime / 'frozen-payload.json', dumps(payload).encode())
        _write_private(runtime / 'review.json', json.dumps(
            review, ensure_ascii=False, indent=2).encode())
        return {'status': review['status'], 'review': str(runtime / 'review.json'),
                'file_sha256': result.sha256, 'line_count': len(result.lines)}
    finally:
        os.umask(previous_umask)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--delivery', type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.report, args.catalog, args.runtime, args.delivery)
    except TrialPreparationBlocked as error:
        print(json.dumps({'status': 'blocked', 'code': error.code}), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({'status': 'blocked', 'code': 'trial_prepare_failed'}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
