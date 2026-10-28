"""Read malformed ERP XLS exports through a separate, read-only Excel instance.

Only approved development copies may be opened. No workbook is saved, refreshed,
or recalculated. Values are emitted into development JSON files for pure import.
"""
from datetime import date, datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "runtime" / "snapshots"


def normalize(keys=("sales", "purchases"), snapshot_dir=None):
    import pythoncom
    import win32com.client

    snapshots = Path(snapshot_dir or SNAPSHOTS).resolve()
    if not snapshots.is_relative_to((ROOT / "runtime").resolve()):
        raise ValueError("Normalization is limited to development runtime copies")
    for key in keys:
        if key not in {"sales", "purchases"}:
            raise ValueError("Only approved ERP snapshot types can use Excel normalization")
        path = (snapshots / f"{key}.xls").resolve()
        if not path.is_relative_to(snapshots):
            raise ValueError("Refusing to open any workbook outside development snapshots")
    pythoncom.CoInitialize()
    excel = None
    results = []
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        if excel.Workbooks.Count:
            excel = None
            raise RuntimeError("Refusing to reuse an Excel instance containing existing workbooks")
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.AskToUpdateLinks = False
        excel.EnableEvents = False
        excel.AutomationSecurity = 3
        scratch = excel.Workbooks.Add()
        excel.Calculation = -4135
        excel.CalculateBeforeSave = False
        for key in keys:
            source = snapshots / f"{key}.xls"
            workbook = excel.Workbooks.Open(str(source), UpdateLinks=0, ReadOnly=True,
                                           IgnoreReadOnlyRecommended=True, AddToMru=False)
            try:
                values = workbook.Worksheets(1).UsedRange.Value
                if values is None:
                    raise ValueError(f"Excel could not recover any values from {source.name}")
                matrix = []
                for row in values:
                    matrix.append([value.isoformat() if isinstance(value, (date, datetime)) else value for value in row])
                if len(matrix) < 2:
                    raise ValueError(f"No data rows found in {source.name}")
                output = snapshots / f"normalized-{key}.json"
                temp = output.with_suffix(".json.tmp")
                temp.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
                temp.replace(output)
                metadata = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                            "generated_at": datetime.now().isoformat(timespec="seconds"),
                            "mode": "read_only_excel_values"}
                output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
                results.append({"kind": key, "rows": len(matrix), "columns": max(map(len, matrix)), "output": output.name})
            finally:
                workbook.Close(SaveChanges=False)
    finally:
        if excel is not None:
            excel.Quit()
        pythoncom.CoUninitialize()
    return results


if __name__ == "__main__":
    print(json.dumps(normalize(), ensure_ascii=False))
