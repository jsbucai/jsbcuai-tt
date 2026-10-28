"""One-shot acceptance for erp.purchaseorder.add. Default is dry-run.

Reads supplier, product and warehouse through the existing ErpClient allow-list,
prints the business body, and does not send. ``--confirm-write`` sends that
body once. The response field is ``vchcode`` only; there is no purchase-order
read API, so the script prints the number it sent and tells you to check
进货订单管理 by hand.

Run inside the workbench container, which already has ``UL_ERP_VAULT``:

    python scripts/erp_purchase_draft_smoketest.py \\
        --supplier-code 供应商编号 --product-code 商品编号 --warehouse-code 仓库编号 \\
        --quantity 1 --unit-price 1.00 --remark '测试单-可删除'

Add ``--number`` from that preview and ``--confirm-write`` to send once.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.erp_api import ErpClient, ErpUnavailable, PURCHASE_ORDER_ADD_METHOD  # noqa: E402
from core.purchase_erp_adapter import (PurchaseDirectoryError, READBACK_METHOD, assemble_order,  # noqa: E402
    document_number, find_product, find_supplier, find_warehouse, vchcode_of, wire_purchaseorder)

MARK = '测试单-可删除'
ZONE = ZoneInfo('Asia/Shanghai')
SECRET_KEYS = {'appkey', 'appsecret', 'token', 'sign', 'signkey', 'auth_token', 'refresh_token',
               'password', 'secret', 'authorization'}
DEFAULT_LOCK = '/tmp/ul-erp-purchase-draft-smoketest.lock'


def redact(value):
    if isinstance(value, dict):
        return {key: ('[已隐去]' if str(key).casefold() in SECRET_KEYS else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for key in SECRET_KEYS:
            if key in value.casefold() and len(value) > 24:
                return '[已隐去]'
    return value


def _decimal(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PurchaseDirectoryError(label + '无效') from None
    if not number.is_finite():
        raise PurchaseDirectoryError(label + '无效')
    return number


def build_body(client, args, moment=None):
    quantity = _decimal(args.quantity, '数量')
    price = _decimal(args.unit_price, '单价')
    tax = _decimal(args.tax_rate, '税率')
    if quantity != 1:
        raise PurchaseDirectoryError('验收数量必须是 1')
    if price <= 0 or tax < 0:
        raise PurchaseDirectoryError('单价必须为正数，税率不能为负')
    remark = str(args.remark or '')
    if MARK not in remark:
        raise PurchaseDirectoryError('备注必须包含「测试单-可删除」')
    if any(key in remark.casefold() for key in SECRET_KEYS):
        raise PurchaseDirectoryError('备注里不能放密钥')
    identity = client.identity()
    employee = str(args.handler_id or identity.get('employee_id') or '').strip()
    if not employee:
        raise PurchaseDirectoryError('授权里没有经手人，请传 --handler-id')
    moment = moment or datetime.now(ZONE)
    number = document_number('ULP-' + '0' * 32, args.number) if args.number else (
        'ULCG' + moment.strftime('%y%m%d%H%M%S') + uuid.uuid4().hex[:4].upper())
    supplier = find_supplier(client, supplier_code=args.supplier_code)
    warehouse = find_warehouse(client, warehouse_code=args.warehouse_code)
    product, sku = find_product(client, args.product_code, args.sku_id)
    day = datetime.strptime(args.delivery_date, '%Y-%m-%d').date() if args.delivery_date else moment.date()
    if day < moment.date():
        raise PurchaseDirectoryError('交货日期早于今天')
    comment = f'{remark} 单据{number}'
    order = assemble_order(
        number=number, when=moment, delivery=datetime.combine(day, moment.time()),
        total=price * quantity, warehouse_id=warehouse['id'], supplier_id=supplier['id'],
        employee_id=employee, summary=f'测试进货订单{number}', comment=comment,
        product=product, sku=sku, quantity=quantity, unit_price=price, tax=tax)
    wire = wire_purchaseorder(order)
    facts = {
        'profile_id': identity.get('profile_id', ''),
        'supplier_usercode': supplier.get('usercode'),
        'supplier_name': supplier.get('fullname'),
        'product_usercode': product.get('usercode'),
        'product_name': product.get('fullname'),
        'warehouse_usercode': warehouse.get('usercode'),
        'warehouse_name': warehouse.get('fullname'),
        'employee_id': employee,
        'number': number,
        'readback_method': READBACK_METHOD,
    }
    return wire, facts


def _print(payload):
    print(json.dumps(redact(payload), ensure_ascii=False, indent=2))


def run(argv, client_factory, lock_path=DEFAULT_LOCK):
    parser = argparse.ArgumentParser(description='进货订单未审核草稿验收。默认只打印请求体。')
    parser.add_argument('--supplier-code', required=True)
    parser.add_argument('--product-code', required=True)
    parser.add_argument('--warehouse-code', required=True)
    parser.add_argument('--quantity', required=True)
    parser.add_argument('--unit-price', required=True)
    parser.add_argument('--remark', required=True)
    parser.add_argument('--number', default='')
    parser.add_argument('--tax-rate', default='0')
    parser.add_argument('--delivery-date', default='')
    parser.add_argument('--handler-id', default='')
    parser.add_argument('--sku-id', default='')
    parser.add_argument('--confirm-write', action='store_true')
    args = parser.parse_args(argv)
    client = client_factory(False)
    try:
        wire, facts = build_body(client, args)
    except (PurchaseDirectoryError, ErpUnavailable, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    body = json.loads(wire)
    will_send = bool(args.confirm_write and args.number)
    _print({
        'dry_run': not will_send,
        'method': PURCHASE_ORDER_ADD_METHOD,
        'endpoint': '/allnewapi',
        'business_body': {'purchaseorder': body},
        'omitted_gateway_fields': ['appkey', 'token', 'timestamp', 'sign'],
        'facts': facts,
        'readback': '目录中没有按单据编号回查进货订单的接口。写入后请在管家婆进货订单管理按 number 人工核对。',
    })
    if not args.confirm_write:
        print('未发送。真正发送需要 --confirm-write，并且带上本次打印的 --number。', file=sys.stderr)
        return 0
    if not args.number:
        print('真正发送必须显式传入 --number，请使用上面预览中的单据编号。本次未发送。', file=sys.stderr)
        return 2
    lock = Path(lock_path)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f'已经有一次确认写入（{lock}）。不会再次发送。', file=sys.stderr)
        return 1
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(body['number'] + '\n')
    client.allow_purchase_order = True
    try:
        response = client.call(PURCHASE_ORDER_ADD_METHOD, {'purchaseorder': wire})
    except ErpUnavailable as error:
        _print({'sent': True, 'result': 'unconfirmed', 'number': body['number'],
                'error_category': error.category, 'error_code': error.code,
                'readback_method': None,
                'human_check': '没有取得有效回执。请按单据编号在进货订单管理核对，不要再次发送。'})
        return 1
    finally:
        client.allow_purchase_order = False
    vchcode = vchcode_of(response if isinstance(response, dict) else {})
    _print({'sent': True, 'result': 'accepted' if vchcode else 'unconfirmed',
            'number': body['number'], 'vchcode': vchcode, 'readback_method': None,
            'human_check': '官方返回只有单据ID，没有按单号回查接口。请到管家婆【进货】-【进货订单管理】按单据编号核对未审核、未付款、未入库，确认后可删除这张测试单。'})
    return 0 if vchcode else 1


def main(argv=None):
    vault = os.environ.get('UL_ERP_VAULT')
    if not vault:
        print('缺少环境变量 UL_ERP_VAULT', file=sys.stderr)
        return 2

    def factory(allow):
        return ErpClient(vault, allow_purchase_order=allow)

    return run(argv, factory)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        sys.exit(1)
