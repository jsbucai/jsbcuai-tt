"""Read-only aggregate coverage check for WMS exact-query status values."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app/service")

from core.erp_api import ErpClient, ErpUnavailable
from core.erp_source import collection, text


def main():
    with sqlite3.connect("/app/service/runtime/data/workbench.sqlite3") as database:
        missing = []
        for (payload,) in database.execute("SELECT payload FROM records WHERE kind='orders'"):
            row = json.loads(payload)
            if not text(row.get("summary")) and text(row.get("order_no")):
                missing.append(text(row["order_no"]))
    selected = sorted(set(missing))[:20]
    client = ErpClient(os.environ["UL_ERP_VAULT"])
    output = {"sampled_missing_orders": len(selected), "statuses": {}}
    for status in (0, 1, 2, 3, 4, 5):
        try:
            response = client.call("erp.ewms.stockoutbill.query", {
                "numbers": selected,
                "status": status,
            })
            rows = collection(response.get("orders"), "orders")
            returned = {text(row.get("tradeid")) for row in rows}
            output["statuses"][str(status)] = {
                "returned_rows": len(rows),
                "matched_requested": len(returned & set(selected)),
            }
        except ErpUnavailable as error:
            output["statuses"][str(status)] = {
                "error_category": error.category,
                "error_code": error.code,
            }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
