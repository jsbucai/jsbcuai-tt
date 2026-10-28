"""Validate or install the verified receipt-ledger bootstrap into SQLite."""
import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.receipt_bootstrap import convert_receipt_state


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--warehouse-id", default="29871293290508390")
    parser.add_argument("--profile-id")
    parser.add_argument("--order-dates-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    inputs = json.loads((args.package / "source-snapshot/sanitized-inputs.json").read_text(encoding="utf-8-sig"))
    order_dates = {}
    for row in inputs.get("sales", []):
        tradeid = str(row.get("order") or "").strip()
        order_date = str(row.get("order_date") or "").strip()
        datetime.strptime(order_date, "%Y-%m-%d")
        if not tradeid or (tradeid in order_dates and order_dates[tradeid] != order_date):
            raise RuntimeError("初始订单日期缺失或冲突")
        order_dates[tradeid] = order_date
    with sqlite3.connect(args.database) as database:
        database.row_factory = sqlite3.Row
        if args.order_dates_only:
            profile_id = str(args.profile_id or "").strip()
            if not profile_id:
                raise RuntimeError("首次同步前安装订单日期必须提供账套 ID")
            if args.apply:
                for tradeid, order_date in order_dates.items():
                    prior = database.execute(
                        "SELECT order_date FROM erp_order_date_cache WHERE profile=? AND tradeid=?",
                        (profile_id, tradeid),
                    ).fetchone()
                    if prior and prior[0] != order_date:
                        raise RuntimeError("数据库已有不同订单日期，拒绝覆盖")
                    database.execute(
                        "INSERT OR IGNORE INTO erp_order_date_cache VALUES(?,?,?,?,?)",
                        (profile_id, tradeid, order_date, "verified_legacy_bootstrap",
                         datetime.now().astimezone().isoformat(timespec="seconds")),
                    )
            print(json.dumps({
                "outcome": "installed" if args.apply else "validated",
                "profile_id": profile_id,
                "order_dates": len(order_dates),
                "order_dates_only": True,
            }, ensure_ascii=False, indent=2))
            return

        raw = json.loads((args.package / "source-snapshot/原始-入库批次.json").read_text(encoding="utf-8-sig"))
        mapping = json.loads((args.package / "账本与API身份映射.json").read_text(encoding="utf-8-sig"))
        inventory = [json.loads(row[0]) for row in database.execute(
            "SELECT payload FROM records WHERE kind='inventory'")]
        profile_row = database.execute(
            "SELECT profile FROM sync_batches WHERE profile<>'' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if profile_row is None:
            raise RuntimeError("数据库中没有已发布 ERP 账套")
        state = convert_receipt_state(raw, mapping, inventory, profile_row[0], args.warehouse_id)
        serialized = canonical(state)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        existing = database.execute(
            "SELECT value FROM settings WHERE key='receipt_ledger_v1'"
        ).fetchone()
        outcome = "validated"
        if args.apply:
            if existing and canonical(json.loads(existing[0])) != serialized:
                raise RuntimeError("数据库已有不同入库账本，拒绝覆盖")
            if not existing:
                database.execute(
                    "INSERT INTO settings(key,value) VALUES('receipt_ledger_v1',?)",
                    (serialized,),
                )
                outcome = "installed"
            else:
                outcome = "already_installed"
            for tradeid, order_date in order_dates.items():
                prior = database.execute(
                    "SELECT order_date FROM erp_order_date_cache WHERE profile=? AND tradeid=?",
                    (state["profile_id"], tradeid),
                ).fetchone()
                if prior and prior[0] != order_date:
                    raise RuntimeError("数据库已有不同订单日期，拒绝覆盖")
                database.execute(
                    "INSERT OR IGNORE INTO erp_order_date_cache VALUES(?,?,?,?,?)",
                    (state["profile_id"], tradeid, order_date, "verified_legacy_bootstrap",
                     datetime.now().astimezone().isoformat(timespec="seconds")),
                )
    print(json.dumps({
        "outcome": outcome,
        "sha256": digest,
        "profile_id": state["profile_id"],
        "period_start": state["period_start"],
        "period_end": state["period_end"],
        "events": len(state["events"]),
        "balances": len(state["balances"]),
        "orders": len(state["orders"]),
        "scope_periods": len(state["scope_periods"]),
        "order_dates": len(order_dates),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
