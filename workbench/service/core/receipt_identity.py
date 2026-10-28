"""Stable identities shared by ERP receipt reads and ledger bootstrap."""
import hashlib
import json


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ledger_warehouse(profile_id, warehouse_id, sku_id, base_unit):
    parts = [str(value).strip() for value in (profile_id, warehouse_id, sku_id, base_unit)]
    if any(not value for value in parts):
        raise ValueError("入库账本的账套、仓库、SKU或基本单位缺失")
    return json.dumps(parts, ensure_ascii=False)


def receipt_event_id(profile_id, document_no, document_type, warehouse_id,
                     product_id, sku_id, base_unit, posted_date):
    parts = [str(value).strip() for value in (
        profile_id, document_no, document_type, warehouse_id,
        product_id, sku_id, base_unit, posted_date,
    )]
    if any(not value for value in parts):
        raise ValueError("入库事件身份字段缺失")
    return hashlib.sha256(compact(["receipt-event-v2", *parts]).encode()).hexdigest()
