"""Stage the historical purchase whitelist without granting checkout authority.

The NAS workbook has two historical column layouts. Only the stable identity
columns are imported. Platform product and current price must be reverified
before a mapping can be activated by a separate controlled executor.
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

from .store import timestamp


MAX_BYTES = 8 * 1024 * 1024
HEADERS = ("启用", "ERP商品编号", "平台", "平台商品链接/SKU", "商品名校验", "标准校验",
           "规格校验", "型号/材质/表面处理校验", "是否自营", "最小包装", "起订量",
           "成交单价", "单价上限")
PLATFORMS = {"淘钉钉", "工品一号"}


def _text(value):
    return str(value).strip() if value is not None else ""


def _platform(value):
    value = _text(value)
    return "淘钉钉" if value.casefold() == "taodingding" else value


def _latest_verified(row):
    # Rows affected by the old O:T shift still keep the final timestamp in T.
    for index in (19, 18, 17, 14):
        value = row[index] if index < len(row) else None
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat(sep=" ", timespec="seconds")
            except TypeError:
                return value.isoformat()
        if isinstance(value, str) and re.fullmatch(r"20\d\d-\d\d-\d\d[ T]\d\d:\d\d:\d\d", value.strip()):
            return value.strip()
    return ""


def read_whitelist(path):
    """Return a hash and structurally checked candidates; never active rows."""
    file = Path(path)
    if file.is_symlink() or not file.is_file():
        raise ValueError("白名单文件不存在或是链接")
    size = file.stat().st_size
    if size <= 0 or size > MAX_BYTES:
        raise ValueError("白名单文件大小异常")
    raw = file.read_bytes()
    if len(raw) != size:
        raise ValueError("白名单读取期间发生变化")
    book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    try:
        if len(book.worksheets) != 1 or book.active.title != "采购正式白名单":
            raise ValueError("白名单工作表不匹配")
        values = book.active.iter_rows(values_only=True)
        header = tuple(_text(item) for item in next(values, ()))
        if header[:len(HEADERS)] != HEADERS:
            raise ValueError("白名单列名不匹配")
        records = []
        for number, row in enumerate(values, 2):
            if not any(_text(value) for value in row):
                continue
            if number > 100_001:
                raise ValueError("白名单行数超过限制")
            code = _text(row[1]).upper() if len(row) > 1 else ""
            platform = _platform(row[2] if len(row) > 2 else None)
            sku = _text(row[3]) if len(row) > 3 else ""
            reasons = []
            if _text(row[0]) != "是":
                reasons.append("原表未启用")
            if not re.fullmatch(r"[A-Z0-9_-]{2,40}", code):
                reasons.append("ERP商品编号异常")
            if platform not in PLATFORMS:
                reasons.append("平台待核对")
            if not sku or len(sku) > 500:
                reasons.append("平台SKU待核对")
            if not _text(row[4] if len(row) > 4 else None):
                reasons.append("商品名称待核对")
            if not _text(row[6] if len(row) > 6 else None):
                reasons.append("规格待核对")
            if not _text(row[7] if len(row) > 7 else None):
                reasons.append("材质型号待核对")
            if isinstance(row[14] if len(row) > 14 else None, (int, float)):
                reasons.append("历史日期列错位")
            records.append({
                "source_row": number, "erp_code": code, "platform": platform,
                "platform_sku": sku, "product_name": _text(row[4] if len(row) > 4 else None),
                "standard_check": _text(row[5] if len(row) > 5 else None),
                "spec_check": _text(row[6] if len(row) > 6 else None),
                "model_check": _text(row[7] if len(row) > 7 else None),
                "warehouse_check": "08390", "pack_size": _text(row[9] if len(row) > 9 else None),
                "price_limit": _text(row[12] if len(row) > 12 else None),
                "last_verified_at": _latest_verified(row), "reasons": reasons,
            })
        counts = Counter(record["erp_code"] for record in records if record["erp_code"])
        for record in records:
            if counts[record["erp_code"]] > 1:
                record["reasons"].append("ERP商品编号重复")
        return hashlib.sha256(raw).hexdigest(), records
    finally:
        book.close()


def stage_whitelist(store, path):
    """Import candidates atomically. This never enables purchasing."""
    source_hash, records = read_whitelist(path)
    source_id = "nas:" + source_hash[:24]
    now = timestamp()
    with store.connect(write=True) as db:
        existing = db.execute("SELECT * FROM purchase_whitelist_sources WHERE source_id=?",
                              (source_id,)).fetchone()
        if existing:
            return {"source_id": source_id, "source_hash": source_hash,
                    "row_count": existing["row_count"], "valid_count": existing["valid_count"],
                    "review_count": existing["review_count"], "replayed": True}
        valid = sum(not record["reasons"] for record in records)
        db.execute("INSERT INTO purchase_whitelist_sources VALUES (?,?,?,?,?,?)",
                   (source_id, source_hash, now, len(records), valid, len(records)-valid))
        for record in records:
            db.execute("""INSERT INTO purchase_whitelist_entries
                (id,profile_id,erp_code,platform,platform_sku,product_name,
                 standard_check,spec_check,model_check,warehouse_check,pack_size,
                 price_limit,last_verified_at,enabled,review_reason,source_id,
                 source_row,imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, "", record["erp_code"], record["platform"],
                 record["platform_sku"], record["product_name"], record["standard_check"],
                 record["spec_check"], record["model_check"], record["warehouse_check"],
                 record["pack_size"], record["price_limit"], record["last_verified_at"],
                 0, "、".join(record["reasons"]) or "待平台实时复核", source_id,
                 record["source_row"], now))
    return {"source_id": source_id, "source_hash": source_hash,
            "row_count": len(records), "valid_count": valid,
            "review_count": len(records)-valid, "replayed": False}
