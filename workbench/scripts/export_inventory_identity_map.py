"""Export non-customer inventory identity fields for ledger conversion checks."""
import json
import sqlite3
import sys


DATABASE = "/app/service/runtime/data/workbench.sqlite3"
FIELDS = ("profile_id", "warehouse_id", "product_id", "sku_id", "base_unit", "sku")


def main():
    with sqlite3.connect(DATABASE) as database:
        rows = []
        for (payload,) in database.execute("SELECT payload FROM records WHERE kind='inventory'"):
            item = json.loads(payload)
            rows.append({name: item.get(name) for name in FIELDS})
    sys.stdout.write(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
    print(json.dumps({"exported_inventory_identities": len(rows)}), file=sys.stderr)


if __name__ == "__main__":
    main()
