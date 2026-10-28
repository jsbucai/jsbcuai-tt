"""Read cached spreadsheet values only. Never starts Excel or refreshes external links."""
import hashlib
import json
import shutil
import tempfile
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
import xlrd
from openpyxl import load_workbook
from .rules import text, normalized, quantity, parse_note, as_bool, excel_date, excel_time
from .schema import FIELDS, INVENTORY_FIELD_ALIASES, PURCHASE_FIELD_ALIASES, SALES_ORDER_FIELD_ALIASES, TASK_HEADERS
from .store import Store, dumps, timestamp
from .import_safety import ImportFailed, import_lock

FILES = {"orders": "orders.xls", "sales": "sales.xls", "inventory": "inventory.xls", "purchases": "purchases.xls", "tasks": "tasks.xlsx", "order_labels": "label_styles.json", "inventory_labels": "inventory_label_styles.json"}


def read_matrix(source, sheet_name=None, preserve_text=False):
    def cell_value(value):
        # Sales notes are evidence: retain their exact text, including newlines.
        return value if preserve_text and isinstance(value, str) else text(value)

    content = source.read_bytes() if isinstance(source, Path) else source.read()
    if len(content) > 60 * 1024 * 1024:
        raise ValueError("单个数据文件不能超过 60 MB")
    if isinstance(source, Path) and source.suffix.lower() == ".json":
        matrix = json.loads(content.decode("utf-8-sig"))
        if not isinstance(matrix, list) or any(not isinstance(row, list) for row in matrix):
            raise ValueError("规范化快照必须为二维数据矩阵")
        return [[cell_value(value) for value in row] for row in matrix]
    if content[:2] == b"PK":
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
        try:
            sheet = workbook[sheet_name] if sheet_name and sheet_name in workbook.sheetnames else workbook[workbook.sheetnames[0]]
            return [[cell_value(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
        finally:
            workbook.close()
    workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
    try:
        sheet = workbook.sheet_by_name(sheet_name) if sheet_name and sheet_name in workbook.sheet_names() else workbook.sheet_by_index(0)
        rows = []
        for index in range(sheet.nrows):
            values = []
            for cell in sheet.row(index):
                if cell.ctype == xlrd.XL_CELL_ERROR:
                    values.append("")
                elif cell.ctype == xlrd.XL_CELL_DATE:
                    values.append(xlrd.xldate_as_datetime(cell.value, workbook.datemode).strftime("%Y-%m-%d"))
                else:
                    values.append(cell_value(cell.value))
            rows.append(values)
        return rows
    finally:
        workbook.release_resources()


def table_rows(matrix, aliases, required):
    normalized_aliases = {key: {normalized(value) for value in values} for key, values in aliases.items()}
    for row_index, row in enumerate(matrix[:60]):
        columns = {}
        for index, value in enumerate(row):
            for key, candidates in normalized_aliases.items():
                if normalized(value) in candidates:
                    columns[key] = index
        if set(required).issubset(columns):
            return [{key: text(row[index]) if index < len(row) else "" for key, index in columns.items()} for row in matrix[row_index + 1:] if any(text(value) for value in row)], columns
    raise ValueError("未找到完整表头：" + "、".join(required))


def sales_order_rows(matrix):
    """Retain historical sales note channels without guessing their meaning.

    The old order remark is an explicitly accepted summary fallback. Additional
    and detail notes remain separate. Conflicting summary evidence is retained
    for review but cannot become the canonical matching summary.
    """
    rows, columns = table_rows(matrix, SALES_ORDER_FIELD_ALIASES, {"order_no", "sku"})
    aliases = {key: {normalized(value) for value in values}
               for key, values in SALES_ORDER_FIELD_ALIASES.items()}
    note_fields = ("summary", "legacy_order_note", "extra_note", "detail_note")
    for header_index, header in enumerate(matrix[:60]):
        present = {key for value in header for key, candidates in aliases.items()
                   if normalized(value) in candidates}
        if {"order_no", "sku"}.issubset(present):
            break
    note_columns = [(field, index, value) for index, value in enumerate(header)
                    for field in note_fields if normalized(value) in aliases[field]]
    values_rows = (row for row in matrix[header_index + 1:] if any(text(value) for value in row))
    for row, values in zip(rows, values_rows):
        evidence = [{"field": field, "header": label, "column": index + 1,
                     "value": values[index] if index < len(values) and isinstance(values[index], str)
                              else text(values[index]) if index < len(values) else ""}
                    for field, index, label in note_columns]
        originals = {field: list(dict.fromkeys(item["value"] for item in evidence
                     if item["field"] == field and item["value"].strip()))
                     for field in note_fields}
        conflicts = [field for field, original in originals.items() if len(original) > 1]
        explicit = originals["summary"][0] if originals["summary"] else ""
        legacy = originals["legacy_order_note"][0] if originals["legacy_order_note"] else ""
        if explicit and legacy and explicit != legacy:
            conflicts.append("summary_legacy_order_note")
        summary_conflict = any(field in conflicts for field in
                               ("summary", "legacy_order_note", "summary_legacy_order_note"))
        reasons = {
            "summary": "多个摘要列内容不一致",
            "legacy_order_note": "多个旧订单备注列内容不一致",
            "summary_legacy_order_note": "订单摘要与旧订单备注内容不一致",
            "extra_note": "多个附加说明列内容不一致",
            "detail_note": "多个明细备注列内容不一致",
        }
        row["summary_original"] = explicit
        row["legacy_order_note"] = legacy
        for field in ("extra_note", "detail_note"):
            row[field] = originals[field][0] if len(originals[field]) == 1 else ""
        row["summary"] = "" if summary_conflict else explicit or legacy
        row["system_note"] = row["summary"]
        row["summary_source"] = ("conflict" if summary_conflict else "explicit_summary"
                                 if explicit else "legacy_order_note" if legacy else "")
        row["summary_review_required"] = summary_conflict
        row["summary_review_reason"] = "；".join(reasons[field] for field in conflicts
                                                  if field in ("summary", "legacy_order_note", "summary_legacy_order_note"))
        row["sales_note_conflicts"] = conflicts
        row["sales_note_sources"] = evidence
    return rows, columns


def task_rows(matrix, strict=False):
    aliases = {header: {header} for header in TASK_HEADERS}
    rows, _ = table_rows(matrix, aliases, {"事项"})
    tasks = []
    for row in rows:
        if not row.get("事项"):
            if strict:
                raise ValueError("导入失败：存在事项标题为空的行")
            continue
        status = row.get("反馈状态", "")
        tasks.append({"id": row.get("任务ID") or uuid.uuid4().hex, "date": excel_date(row.get("日期", ""), date_only=True), "time": excel_time(row.get("时间", "")), "department": row.get("部门", ""), "title": row["事项"], "note": row.get("备注", ""), "completed": as_bool(row.get("已完成")), "completedAt": excel_date(row.get("完成时间", "")), "updatedAt": excel_date(row.get("更新时间")) or timestamp(), "urgent": as_bool(row.get("紧急")), "important": as_bool(row.get("重要")), "feedbackStatus": "open" if status in {"open", "待处理", "有问题"} else "resolved" if status in {"resolved", "已处理", "已解决"} else "", "feedbackText": row.get("反馈内容", ""), "feedbackAt": excel_date(row.get("反馈时间", "")), "feedbackResolvedAt": excel_date(row.get("反馈处理时间", ""))})
    ids = [task["id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("导入表含重复任务 ID，请先修正")
    return tasks


class Importer:
    def __init__(self, store):
        self.store = store

    def source_path(self, kind):
        path = self.store.snapshots / FILES[kind]
        if not path.resolve().is_relative_to(self.store.snapshots.resolve()):
            raise ValueError("原始快照不能链接到开发快照目录之外")
        if kind in {"sales", "purchases"}:
            converted = self.store.snapshots / f"normalized-{kind}.json"
            derived_meta = self.store.snapshots / f"normalized-{kind}.metadata.json"
            if converted.exists() and derived_meta.exists() and path.exists():
                if not derived_meta.resolve().is_relative_to(self.store.snapshots.resolve()):
                    raise ValueError("规范化元数据不能指向开发目录外")
                metadata = json.loads(derived_meta.read_text(encoding="utf-8-sig"))
                if metadata.get("source_sha256") == hashlib.sha256(path.read_bytes()).hexdigest():
                    path = converted
        if not path.resolve().is_relative_to(self.store.snapshots.resolve()):
            raise ValueError("快照不能链接到开发快照目录之外")
        return path

    def metadata(self, kind, status, count=0, message=""):
        path = self.store.snapshots / FILES[kind]
        manifest_path = self.store.snapshots / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.exists() else {}
        record = manifest.get("files", {}).get({"order_labels": "label_styles", "inventory_labels": "inventory_label_styles"}.get(kind, kind), {})
        return {"kind": kind, "file": path.name, "status": status, "count": count, "message": message, "imported_at": timestamp(), "source_modified_at": record.get("source_mtime"), "snapshot_modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if path.exists() else None, "sha256": record.get("sha256") or (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None)}

    def record_meta(self, meta):
        with self.store.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO sources VALUES (?,?)", (meta["kind"], dumps(meta)))

    def save_records(self, kind, rows):
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM records WHERE kind=?", (kind,))
            for index, row in enumerate(rows):
                row.setdefault("id", hashlib.sha256(f"{kind}:{index}:{dumps(row)}".encode()).hexdigest()[:20])
                db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)", (kind, row["id"], dumps(row), text(row.get("sku")).upper(), text(row.get("buyer")), text(row.get("system_note") or row.get("note")), normalized(" ".join(map(text, row.values()))), row.get("document_date", ""), quantity(row.get("quantity")), quantity(row.get("tax_total") or row.get("amount")), row.get("logistics_company", "")))

    def import_one(self, kind, overwrite=False):
        if kind not in FILES:
            raise ValueError("未知数据类型")
        if not overwrite and kind in {"tasks", "order_labels", "inventory_labels"}:
            with self.store.connect() as db:
                existing = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] if kind == "tasks" else db.execute("SELECT COUNT(*) FROM labels WHERE kind=?", ("inventory" if kind == "inventory_labels" else "order",)).fetchone()[0]
            if existing:
                meta = self.metadata(kind, "preserved", existing, "保留工作台内任务和标签修改；导入来源时间取原文件清单")
                self.record_meta(meta)
                return meta
        path = self.source_path(kind)
        if not path.exists():
            meta = self.metadata(kind, "missing", message="尚未提供开发快照")
            self.record_meta(meta)
            return meta
        try:
            if kind == "orders":
                aliases = {key: {value} for key, value in FIELDS.items()}
                aliases["logistics_company"] = {"物流公司"}
                source, columns = table_rows(read_matrix(path, "导出记录"), aliases, {"order_no", "sku", "system_note", "status"})
                sales_path = self.source_path("sales")
                sales = {}
                if sales_path.exists():
                    sales_rows, _ = sales_order_rows(read_matrix(sales_path, "导出数据", preserve_text=True))
                    for item in sales_rows:
                        sales.setdefault(item["order_no"], item)
                    self.record_meta(self.metadata("sales", "ready", len(sales_rows)))
                rows = []
                for row in source:
                    parsed = parse_note(row.get("system_note"))
                    row["order_no"] = row.get("order_no") or parsed.get("orderNo", "")
                    fallback = sales.get(row["order_no"], {})
                    for key, note_key in (("sku", "sku"), ("product_name", "name"), ("quantity", "quantity"), ("buyer", "buyer")):
                        row[key] = row.get(key) or fallback.get(key) or parsed.get(note_key, "")
                    row["sku"] = text(row["sku"]).upper()
                    if row.get("status") != "待发货" or row["order_no"] in {"", "0", "0.0"} or row["sku"] in {"", "0", "0.0"} or sales and row["order_no"] not in sales:
                        continue
                    row.setdefault("logistics_company", "")
                    rows.append(row)
                self.save_records(kind, rows)
                with self.store.connect(write=True) as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('logistics_available',?)", (dumps("logistics_company" in columns),))
                message = "仅导入待发货且符合销售表校验的订单；物流公司直接来自订单表" if sales else "销售表无有效内容，沿原规则未额外排除订单"
            elif kind in {"inventory", "purchases", "sales"}:
                aliases = {"inventory": INVENTORY_FIELD_ALIASES, "purchases": PURCHASE_FIELD_ALIASES, "sales": SALES_ORDER_FIELD_ALIASES}[kind]
                required = {"sku", "product_name"} if kind == "inventory" else {"sku", "document_no", "party", "document_date"} if kind == "purchases" else {"order_no", "sku"}
                rows, _ = (sales_order_rows(read_matrix(path, preserve_text=True)) if kind == "sales"
                           else table_rows(read_matrix(path), aliases, required))
                for row in rows:
                    row["sku"] = row.get("sku", "").upper()
                rows = [row for row in rows if row["sku"]]
                self.save_records(kind, rows)
                message = "从开发副本读取已保存的数据，未改写原文件或刷新外链"
                if kind == "sales":
                    conflicts = sum(bool(row["summary_review_required"]) for row in rows)
                    if conflicts:
                        message += f"；{conflicts} 行摘要或旧订单备注冲突，保留原文待核对"
            elif kind == "tasks":
                rows = task_rows(read_matrix(path))
                with self.store.connect(write=True) as db:
                    existing = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                    if existing and not overwrite:
                        return {"kind": kind, "status": "preserved", "count": existing, "message": "保留工作台内任务修改；可使用 Excel 上传按 ID 合并"}
                    for task in rows:
                        db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,1)", (task["id"], dumps(task)))
                message = "完整导入待办及所有已完成任务"
            else:
                label_kind = "inventory" if kind == "inventory_labels" else "order"
                content = json.loads(path.read_text(encoding="utf-8-sig"))
                rows = content.get("styles", [])
                with self.store.connect(write=True) as db:
                    existing = db.execute("SELECT COUNT(*) FROM labels WHERE kind=?", (label_kind,)).fetchone()[0]
                    if existing and not overwrite:
                        return {"kind": kind, "status": "preserved", "count": existing, "message": "保留开发标签样式"}
                    for style in rows:
                        style_id = text(style.get("id")) or uuid.uuid4().hex
                        style["id"] = style_id
                        db.execute("INSERT OR REPLACE INTO labels VALUES (?,?,?,1,?)", (label_kind, style_id, dumps(style), int(style_id == content.get("active_id"))))
                message = "标签样式保存于独立 SQLite"
            meta = self.metadata(kind, "ready", len(rows), message)
        except Exception as exc:
            meta = self.metadata(kind, "error", message=f"导入失败，保留已有开发数据：{exc}")
        self.record_meta(meta)
        return meta

    def run(self, kind="all", refresh=None):
        """Parse a frozen input into a disposable database, then publish one transaction."""
        groups = {"all": ["sales", "orders", "inventory"]}
        kinds = groups.get(kind, [kind])
        if any(item not in FILES for item in kinds):
            raise ValueError("未知数据类型")
        with import_lock(self.store.runtime):
            try:
                if refresh:
                    refresh()
                with tempfile.TemporaryDirectory(prefix="import-stage-", dir=self.store.runtime) as directory:
                    staged = Store(directory)
                    # Readers and task editors continue using the live WAL database.
                    with self.store.connect() as source, staged.connect() as target:
                        source.backup(target)
                    snapshots = self.store.snapshots  # select exactly one immutable generation
                    for source in snapshots.iterdir():
                        if source.is_file() and source.suffix.lower() in {".json", ".xls", ".xlsx"} and source.name != "current.json":
                            if not source.resolve().is_relative_to(snapshots.resolve()):
                                raise ValueError("快照文件不得链接到开发目录之外")
                            shutil.copy2(source, staged.snapshots / source.name)
                    self.validate_snapshot(staged.snapshots)
                    worker = Importer(staged)
                    results = [worker.import_one(item) for item in kinds]
                    previous = {item["kind"]: item for item in self.store.source_list()}
                    failed = [item for item in results if item["status"] == "error" or item["status"] == "missing" and previous.get(item["kind"], {}).get("status") in {"ready", "preserved"}]
                    if failed:
                        raise ImportFailed("本次导入失败，所有现有开发数据保持原样：" + "; ".join(item["message"] for item in failed), results)
                    self.publish(staged, kinds, results)
                    return results
            except (ImportFailed, ValueError):
                raise
            except Exception as exc:
                raise ImportFailed(f"读取或准备快照失败，现有开发数据保持原样：{exc}") from exc

    @staticmethod
    def validate_snapshot(directory):
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            return  # uploaded/local fixtures can be supplied without a manifest
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        for record in manifest.get("files", {}).values():
            filename, expected = record.get("filename"), record.get("sha256")
            if not filename or not expected:
                raise ImportFailed("快照清单缺少文件名或校验值，未发布数据")
            source = (directory / filename).resolve()
            if not source.is_relative_to(directory.resolve()) or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise ImportFailed(f"快照校验失败：{filename}；未发布任何数据")

    def publish(self, staged, kinds, results):
        statuses = {item["kind"]: item["status"] for item in results}
        with self.store.connect(write=True) as db:
            db.execute("ATTACH DATABASE ? AS imported", (str(staged.path),))
            for kind in kinds:
                if kind in {"orders", "inventory", "purchases", "sales"} and statuses[kind] == "ready":
                    db.execute("DELETE FROM records WHERE kind=?", (kind,))
                    db.execute("INSERT INTO records SELECT * FROM imported.records WHERE kind=?", (kind,))
                elif kind == "tasks" and statuses[kind] == "ready" and not db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
                    db.execute("INSERT INTO tasks SELECT * FROM imported.tasks")
                elif kind in {"order_labels", "inventory_labels"} and statuses[kind] == "ready":
                    label_kind = "order" if kind == "order_labels" else "inventory"
                    if not db.execute("SELECT 1 FROM labels WHERE kind=? LIMIT 1", (label_kind,)).fetchone():
                        db.execute("INSERT INTO labels SELECT * FROM imported.labels WHERE kind=?", (label_kind,))
                db.execute("INSERT OR REPLACE INTO sources SELECT * FROM imported.sources WHERE kind=?", (kind,))
            if "orders" in kinds:
                db.execute("INSERT OR REPLACE INTO settings SELECT * FROM imported.settings WHERE key='logistics_available'")
                db.execute("INSERT OR REPLACE INTO sources SELECT * FROM imported.sources WHERE kind='sales'")
            hashes = [json.loads(row[0]).get('sha256') for row in db.execute("SELECT payload FROM sources WHERE kind IN ('orders','sales','inventory') ORDER BY kind")]
            version = hashlib.sha256(dumps(hashes).encode()).hexdigest()[:20]
            db.execute("INSERT OR REPLACE INTO settings VALUES ('data_version',?)", (dumps(version),))
