"""Seed a new production database from an already audited, same-profile cache."""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from sys import path as sys_path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys_path.insert(0, str(SERVICE))

from core.wms_cache import HEAD_FIELDS, LINE_FIELDS


TABLES = {
    "erp_reference_cache": ("kind", "profile", "payload", "fetched_at", "source_hash"),
    "erp_wms_cache": ("profile", "tradeid", "payload", "updated_at"),
    "erp_sync_watermarks": ("kind", "profile", "payload", "updated_at"),
    "erp_order_date_cache": ("profile", "tradeid", "order_date", "source", "updated_at"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    profile = str(args.profile_id).strip()
    # Deployment passes a closed, consistent backup file. Immutable mode keeps
    # SQLite from trying to create WAL/SHM files beside the read-only mount.
    source = sqlite3.connect(f"file:{args.source.resolve()}?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    destination = sqlite3.connect(args.database)
    destination.row_factory = sqlite3.Row
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("缓存来源数据库校验失败")
        if destination.execute("PRAGMA user_version").fetchone()[0] < 3:
            raise RuntimeError("目标数据库尚未完成 v3 迁移")
        copied = {}
        rows_by_table = {}
        for table, columns in TABLES.items():
            rows = source.execute(
                f"SELECT {','.join(columns)} FROM {table} WHERE profile=?", (profile,)
            ).fetchall()
            if not rows:
                raise RuntimeError(f"缓存来源缺少 {table}")
            rows_by_table[table] = rows
            copied[table] = len(rows)

        for row in rows_by_table["erp_reference_cache"]:
            value = json.loads(row["payload"])
            if not isinstance(value.get("products"), list) or not isinstance(value.get("warehouses"), list):
                raise RuntimeError("商品仓库缓存结构无效")
        for row in rows_by_table["erp_wms_cache"]:
            value = json.loads(row["payload"])
            if set(value) - (HEAD_FIELDS | {"goodslist"}):
                raise RuntimeError("WMS 缓存包含未批准字段")
            if any(set(line) - LINE_FIELDS for line in value.get("goodslist", [])):
                raise RuntimeError("WMS 明细缓存包含未批准字段")
        for row in rows_by_table["erp_sync_watermarks"]:
            value = json.loads(row["payload"])
            datetime.fromisoformat(value["last_sync"])
            datetime.fromisoformat(value["last_full"])
        for row in rows_by_table["erp_order_date_cache"]:
            datetime.strptime(row["order_date"], "%Y-%m-%d")

        if args.apply:
            destination.execute("BEGIN IMMEDIATE")
            for table, columns in TABLES.items():
                placeholders = ",".join("?" for _ in columns)
                destination.executemany(
                    f"INSERT OR REPLACE INTO {table}({','.join(columns)}) VALUES({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows_by_table[table]],
                )
            destination.commit()
        print(json.dumps({
            "outcome": "installed" if args.apply else "validated",
            "profile_id": profile,
            "counts": copied,
        }, ensure_ascii=False, indent=2))
    except BaseException:
        destination.rollback()
        raise
    finally:
        source.close()
        destination.close()


if __name__ == "__main__":
    main()
