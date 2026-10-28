"""Synthetic historical sales imports only; no refresh, ERP or production I/O."""
import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from core.importers import Importer, read_matrix, sales_order_rows
from core.store import Store


@pytest.fixture
def store():
    runtime = Path(__file__).resolve().parents[1] / "runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sales-history-note-test-", dir=runtime) as directory:
        yield Store(directory)


def write_sheet(path, matrix, title="导出数据"):
    book = Workbook()
    book.active.title = title
    for row in matrix:
        book.active.append(row)
    book.save(path)
    book.close()


@pytest.mark.parametrize("header", ["备注", "订单备注", "系统备注"])
def test_legacy_order_remark_is_the_accepted_summary_fallback(header):
    original = "  PO-1___#项目___#1\n#图纸___#M6*30  "
    rows, _ = sales_order_rows([["订单编号", "商品编号", header], ["SO1", "abc", original]])
    row = rows[0]
    assert row["summary"] == row["system_note"] == original
    assert row["summary_original"] == ""
    assert row["legacy_order_note"] == original
    assert row["summary_source"] == "legacy_order_note"
    assert row["summary_review_required"] is False
    assert row["sales_note_sources"] == [{"field": "legacy_order_note", "header": header,
                                           "column": 3, "value": original}]


@pytest.mark.parametrize("header", ["摘要", "订单摘要"])
def test_explicit_summary_retains_exact_original_text(header):
    original = "  PO-1___#物料\r\n说明___#交期=2026-10-20  "
    rows, _ = sales_order_rows([["订单编号", "商品编码", header, "备注", "附加说明", "明细备注"],
                               ["SO1", "abc", original, "", "人工说明", "仅该行说明"]])
    row = rows[0]
    assert row["summary_original"] == row["summary"] == row["system_note"] == original
    assert row["summary_source"] == "explicit_summary"
    assert row["extra_note"] == "人工说明" and row["detail_note"] == "仅该行说明"
    assert row["summary_review_required"] is False


def test_equal_summary_and_legacy_remark_prefers_explicit_source_without_conflict():
    rows, _ = sales_order_rows([["订单编号", "商品编号", "摘要", "系统备注"],
                               ["SO1", "abc", "PO1___#1", "PO1___#1"]])
    assert rows[0]["summary_source"] == "explicit_summary"
    assert rows[0]["summary"] == "PO1___#1"
    assert not rows[0]["summary_review_required"]


def test_conflicting_summary_and_legacy_remark_preserve_both_and_disable_matching_summary():
    rows, _ = sales_order_rows([["订单编号", "商品编号", "摘要", "订单备注", "附加说明", "行备注"],
                               ["SO1", "abc", "新摘要\nA", "旧备注\nB", "人工说明C", "行说明D"]])
    row = rows[0]
    assert row["summary"] == row["system_note"] == ""
    assert row["summary_original"] == "新摘要\nA" and row["legacy_order_note"] == "旧备注\nB"
    assert row["extra_note"] == "人工说明C" and row["detail_note"] == "行说明D"
    assert row["summary_review_required"] and row["summary_source"] == "conflict"
    assert row["sales_note_conflicts"] == ["summary_legacy_order_note"]
    assert "不一致" in row["summary_review_reason"]
    assert [item["value"] for item in row["sales_note_sources"]] == ["新摘要\nA", "旧备注\nB", "人工说明C", "行说明D"]


@pytest.mark.parametrize("headers", [["摘要", "订单摘要"], ["备注", "系统备注"]])
def test_multiple_nonempty_alias_columns_cannot_silently_overwrite_each_other(headers):
    rows, _ = sales_order_rows([["订单编号", "商品编号", *headers], ["SO1", "abc", "原文A", "原文B"]])
    row = rows[0]
    assert row["summary"] == row["system_note"] == ""
    assert row["summary_review_required"]
    assert [source["value"] for source in row["sales_note_sources"]] == ["原文A", "原文B"]


def test_empty_alias_column_does_not_erase_nonempty_original():
    rows, _ = sales_order_rows([["订单编号", "商品编号", "摘要", "订单摘要", "备注"],
                               ["SO1", "abc", "摘要原文", "", ""]])
    assert rows[0]["summary"] == "摘要原文"
    assert not rows[0]["summary_review_required"]


def test_additional_and_line_notes_alone_are_never_promoted_to_order_summary():
    rows, _ = sales_order_rows([["订单编号", "商品编号", "附加说明", "明细备注"],
                               ["SO1", "abc", "  请按图纸\n供货  ", " 行说明\n第二行 "]])
    row = rows[0]
    assert row["summary"] == row["system_note"] == row["legacy_order_note"] == row["summary_source"] == ""
    assert row["extra_note"] == "  请按图纸\n供货  "
    assert row["detail_note"] == " 行说明\n第二行 "
    assert not row["summary_review_required"]


def test_sales_import_keeps_originals_and_indexes_only_unambiguous_summary(store):
    path = store.snapshots / "sales.xls"
    write_sheet(path, [["订单编号", "商品编号", "数量", "摘要", "订单备注", "附加说明", "明细备注"],
                       ["SO1", "abc", "5", "  明确摘要\n原文  ", "", "人工说明A", "行说明A"],
                       ["SO2", "abc", "7", "明确摘要B", "另一旧备注B", "人工说明B", "行说明B"]])
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    meta = Importer(store).import_one("sales")
    assert meta["status"] == "ready" and meta["count"] == 2
    assert "1 行" in meta["message"] and "待核对" in meta["message"]
    rows = {row["order_no"]: row for row in store.rows("sales")}
    assert rows["SO1"]["summary"] == rows["SO1"]["system_note"] == "  明确摘要\n原文  "
    assert rows["SO1"]["sku"] == "ABC" and rows["SO1"]["quantity"] == "5"
    assert rows["SO2"]["summary"] == rows["SO2"]["system_note"] == ""
    assert rows["SO2"]["summary_original"] == "明确摘要B" and rows["SO2"]["legacy_order_note"] == "另一旧备注B"
    with store.connect() as db:
        indexed = {json.loads(row["payload"])["order_no"]: row["note"]
                   for row in db.execute("SELECT payload,note FROM records WHERE kind='sales'")}
    assert indexed["SO2"] == ""
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


def test_normalized_sales_snapshot_retains_note_channels_too(store):
    original = store.snapshots / "sales.xls"
    original.write_bytes(b"synthetic original snapshot; not an actual workbook")
    converted = store.snapshots / "normalized-sales.json"
    converted.write_text(json.dumps([["订单编号", "商品编号", "订单备注", "附加说明"],
                                     ["SO1", "abc", "  原订单备注\n第二行  ", " 人工说明 "]], ensure_ascii=False), encoding="utf-8")
    (store.snapshots / "normalized-sales.metadata.json").write_text(
        json.dumps({"source_sha256": hashlib.sha256(original.read_bytes()).hexdigest()}), encoding="utf-8")
    assert Importer(store).import_one("sales")["status"] == "ready"
    row = store.rows("sales")[0]
    assert row["summary"] == row["system_note"] == "  原订单备注\n第二行  "
    assert row["extra_note"] == " 人工说明 "


def test_default_matrix_reader_keeps_existing_non_sales_text_behavior(store):
    path = store.snapshots / "sales.xls"
    write_sheet(path, [["备注"], ["  原文\n第二行  "]])
    assert read_matrix(path)[1][0] == "原文 第二行"
    assert read_matrix(path, preserve_text=True)[1][0] == "  原文\n第二行  "


def test_order_sales_fallback_keeps_order_summary_and_only_fills_existing_business_fields(store):
    write_sheet(store.snapshots / "sales.xls", [["订单编号", "商品编号", "单位名称", "商品名称", "数量", "摘要", "备注"],
                                               ["SO1", "abc", "客户甲", "螺栓", "2", "冲突摘要", "冲突旧备注"]])
    own_summary = "PO1___#项目___#1___#图纸___#螺栓___#M6*30___#8.8___#白锌___#2___#ABC"
    write_sheet(store.snapshots / "orders.xls", [["订单编号", "商品编号", "系统备注", "处理状态", "买家账号", "商品名称", "数量"],
                                                ["SO1", "abc", own_summary, "待发货", "", "", ""]], title="导出记录")
    assert Importer(store).import_one("orders")["status"] == "ready"
    row = store.rows("orders")[0]
    assert row["system_note"] == own_summary
    assert row["buyer"] == "客户甲" and row["product_name"] == "螺栓" and row["quantity"] == "2"
    assert row["sku"] == "ABC"


def test_purchase_and_inventory_note_channels_keep_existing_contract(store):
    write_sheet(store.snapshots / "purchases.xls", [["商品编号", "单据编号", "往来单位", "单据日期", "备注", "摘要", "附加说明"],
                                                   ["abc", "PO1", "供应商甲", "2026-10-07", "明细备注", "采购摘要", "附加采购说明"]])
    write_sheet(store.snapshots / "inventory.xls", [["商品编号", "商品名称", "备注"], ["abc", "螺栓", "商品备注"]])
    importer = Importer(store)
    assert importer.import_one("purchases")["status"] == "ready"
    assert importer.import_one("inventory")["status"] == "ready"
    purchase = store.rows("purchases")[0]
    assert purchase["note"] == "明细备注" and purchase["summary"] == "采购摘要" and purchase["extra_note"] == "附加采购说明"
    assert "system_note" not in purchase and "summary_review_required" not in purchase
    inventory = store.rows("inventory")[0]
    assert inventory["product_note"] == "商品备注" and "summary" not in inventory
