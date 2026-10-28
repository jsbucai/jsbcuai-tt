"""Print aggregate logistics-calculation gaps from the live SQLite database."""
import json
import sqlite3
from collections import Counter


DATABASE = "/app/service/runtime/data/workbench.sqlite3"


def main():
    with sqlite3.connect(DATABASE) as database:
        database.row_factory = sqlite3.Row
        latest = database.execute(
            "SELECT id,status,stage,result,created_at,updated_at FROM update_jobs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            print(json.dumps({"latest_job": None}, ensure_ascii=False))
            return
        saved = database.execute(
            "SELECT payload,data_version,created_at FROM logistics_calculations WHERE job_id=?",
            (latest["id"],),
        ).fetchone()
        order_rows = database.execute(
            "SELECT payload FROM records WHERE kind='orders'"
        ).fetchall()

    job_result = json.loads(latest["result"] or "{}")
    calculation = json.loads(saved["payload"]) if saved else {}
    source_codes = Counter()
    source_scopes = Counter()
    for item in calculation.get("source_issues", []):
        source_codes[str(item.get("code") or "unknown")] += 1
        source_scopes[str(item.get("scope") or "unknown")] += 1

    display_status = Counter()
    display_issue_codes = Counter()
    identity_missing_rows = Counter()
    identity_missing_orders = {name: set() for name in
        ("profile_id", "warehouse_id", "product_id", "sku_id", "base_unit")}
    empty_summary_orders = set()
    source_status_rows = Counter()
    source_status_orders = {}
    summary_orders = set()
    unique_orders = set()
    for row in order_rows:
        payload = json.loads(row["payload"])
        unique_orders.add(str(payload.get("order_no") or ""))
        order_no = str(payload.get("order_no") or "")
        source_status = str(payload.get("source_status") or "missing")
        source_status_rows[source_status] += 1
        source_status_orders.setdefault(source_status, set()).add(order_no)
        for name in identity_missing_orders:
            if not str(payload.get(name) or "").strip():
                identity_missing_rows[name] += 1
                if order_no:
                    identity_missing_orders[name].add(order_no)
        if not str(payload.get("summary") or "").strip() and order_no:
            empty_summary_orders.add(order_no)
        elif order_no:
            summary_orders.add(order_no)
        display_status[str(payload.get("calculation_status") or "unknown")] += 1
        for item in payload.get("calculation_issues", []):
            display_issue_codes[str(item.get("code") or "unknown")] += 1

    output = {
        "latest_job": {
            "id": latest["id"],
            "status": latest["status"],
            "stage": latest["stage"],
            "created_at": latest["created_at"],
            "updated_at": latest["updated_at"],
            "published": bool(job_result.get("published")),
            "query_status": job_result.get("query_status"),
            "logistics_status": job_result.get("logistics_status"),
        },
        "orders": {
            "unique_orders": len(unique_orders - {""}),
            "display_rows": len(order_rows),
            "calculation_status_rows": dict(sorted(display_status.items())),
            "plans": len(calculation.get("plans", [])),
            "blocked_orders": len(calculation.get("blocked_orders", [])),
            "empty_summary_orders": len(empty_summary_orders),
            "orders_with_summary": len(summary_orders),
            "source_status_rows": dict(sorted(source_status_rows.items())),
            "source_status_orders": {
                name: len(values - {""}) for name, values in sorted(source_status_orders.items())
            },
            "identity_missing_rows": dict(sorted(identity_missing_rows.items())),
            "identity_missing_orders": {
                name: len(values) for name, values in sorted(identity_missing_orders.items())
            },
        },
        "source_issue_codes": dict(sorted(source_codes.items())),
        "source_issue_scopes": dict(sorted(source_scopes.items())),
        "display_issue_codes": dict(sorted(display_issue_codes.items())),
        "calculation_version": calculation.get("rule_version"),
        "data_version": saved["data_version"] if saved else None,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
