"""Inspect or atomically stage the NAS purchase whitelist on N150.

Run with --commit only after the path is the intended immutable NAS snapshot.
Staging does not enable any platform mapping or make payments possible.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from core.procurement_assistant import ProcurementAssistant
from core.procurement_whitelist import read_whitelist, stage_whitelist
from core.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--runtime", type=Path, default=SERVICE / "runtime" / "data")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    digest, rows = read_whitelist(args.workbook)
    report = {
        "sha256": digest, "row_count": len(rows),
        "structurally_valid": sum(not item["reasons"] for item in rows),
        "review_reasons": dict(Counter(reason for item in rows for reason in item["reasons"])),
        "commit": args.commit,
    }
    if args.commit:
        store = Store(args.runtime)
        ProcurementAssistant(store, None)  # Install only this additive schema.
        report["staged"] = stage_whitelist(store, args.workbook)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
