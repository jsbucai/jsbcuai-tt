"""Run the full live read and calculation against an isolated database copy."""
import json
import os
import sys
from collections import Counter
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from core.api_source import validate_snapshot
from core.classification_pipeline import calculate_query_snapshot
from core.erp_api import ErpClient
from core.erp_cache import ReferenceCache
from core.erp_source import LiveSource
from core.store import Store
from core.wms_cache import WmsCache


def main():
    runtime = Path(os.environ.get("UL_PREFLIGHT_RUNTIME", "/app/service/runtime/data"))
    store = Store(runtime)
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    snapshot = validate_snapshot(LiveSource(
        client, reference_cache=ReferenceCache(store), wms_cache=WmsCache(store)
    ).collect())
    with store.connect() as database:
        row = database.execute(
            "SELECT value FROM settings WHERE key='receipt_ledger_v1'"
        ).fetchone()
    previous = json.loads(row[0]) if row else None
    result = calculate_query_snapshot(snapshot, previous)
    order_rows = snapshot["records"]["orders"]
    issue_examples = {}
    for item in snapshot["calculation_issues"]:
        code = item.get("code", "unknown")
        values = issue_examples.setdefault(code, [])
        example = {key: item[key] for key in ("scope", "order_no", "reason") if item.get(key)}
        if example not in values and len(values) < 8:
            values.append(example)
    report = {
        "profile_id": snapshot["profile_id"],
        "source_updated_at": snapshot["source_updated_at"],
        "source_counts": snapshot["source_counts"],
        "orders": len(snapshot["orders"]),
        "order_lines": len(order_rows),
        "summaries_nonempty": sum(bool(row.get("summary")) for row in order_rows),
        "delivery_dates_ready": sum(bool(order.get("delivery_date")) for order in snapshot["orders"]),
        "source_issue_codes": dict(sorted(Counter(
            item.get("code", "unknown") for item in snapshot["calculation_issues"]
        ).items())),
        "source_issue_examples": dict(sorted(issue_examples.items())),
        "plans": len(result["plans"]),
        "blocked_orders": len(result["blocked_orders"]),
        "blocked_issue_codes": dict(sorted(Counter(
            item.get("code", "unknown") for item in result["issues"]
        ).items())),
        "categories": dict(sorted(Counter(
            item["freightname"] for item in result["plans"]
        ).items())),
        "receipt_state": None,
    }
    state = result.get("receipt_state")
    if state:
        report["receipt_state"] = {
            "period_start": state.get("period_start"),
            "period_end": state.get("period_end"),
            "events": len(state.get("events", {})),
            "balances": len(state.get("balances", {})),
            "orders": len(state.get("orders", {})),
            "scope_periods": len(state.get("scope_periods", {})),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
