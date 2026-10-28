"""The host freezes every source and stores only a verified AI proposal."""
import copy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from core.sales_codex_pipeline import enqueue
from core.store import dumps
from tools import sales_codex_runner as runner
from tools import sales_codex_worker as worker
from test_daily_work import store, user
from test_sales_assistant import engine, create
from test_sales_codex_pipeline import enable, request


RULES = Path(__file__).resolve().parents[2] / "deploy/skills/ul-sales-order"
TEXT = "LS1000 六角头螺栓 GB/T5783 M20*80 8.8 发黑 3支 含税1.13元，订单PO-AI，交期2026-10-15"


@pytest.fixture
def active_case(engine, tmp_path):
    with engine.store.connect(write=True) as db:
        product = json.loads(db.execute("SELECT payload FROM records WHERE kind='inventory'").fetchone()[0])
        product.update(product_name="六角头螺栓", model="8.8级 发黑", standard_no="GB/T5783",
                       warehouse_name="江苏优螺", material="碳钢")
        db.execute("UPDATE records SET payload=? WHERE kind='inventory'", (dumps(product),))
    config = worker.WorkerConfig(tmp_path / "jobs", RULES, codex_user=None)
    return engine, config


def proposal(packet):
    rows = []
    for item in packet["source_rows"]:
        if item["ignored"]:
            continue
        parsed = item["parsed"]
        rows.append({"source_ids": [item["id"]], "source_quote": item["text"],
            "product_code": "LS1000", "original_name": "六角头螺栓", "original_spec": "M20*80", "original_material": "",
            "standard": "GB/T5783", "grade": "8.8", "surface": "发黑", "customer_part_no": "",
            "quantity": parsed.get("quantity", "1"), "unit_price": parsed.get("unit_price", "1.13"),
            "unit": "支", "delivery_date": "2026-10-15", "project_no": "", "line_no": "", "drawing_no": ""})
    if not rows:
        source = packet["sources"][0]
        rows.append({"source_ids": [source["source_id"]], "source_quote": source.get("text", ""),
            "product_code": "LS1000", "original_name": "六角头螺栓", "original_spec": "M20*80", "original_material": "",
            "standard": "GB/T5783", "grade": "8.8", "surface": "发黑", "customer_part_no": "",
            "quantity": "3", "unit_price": "1.13", "unit": "支", "delivery_date": "2026-10-15",
            "project_no": "", "line_no": "", "drawing_no": ""})
    return {"assistant_text": "草稿已整理，供人工核对。", "status": "ready_for_review",
        "draft": {"customer_order_no": "PO-AI", "delivery_date": "2026-10-15", "note": "",
                  "tax_basis": "tax_inclusive", "tax_rate": 13, "lines": rows},
        "questions": [], "new_product_suggestions": [],
        "source_coverage": [{"source_id": item["source_id"], "status": "complete", "line_count": len(rows),
                            "detail": "合成资料完整提取"} for item in packet["sources"]]}


class FakeRunner:
    def __init__(self, *, tamper=None, callback=None):
        self.tamper = tamper
        self.callback = callback
        self.packets = []

    def __call__(self, phase_dir, config):
        raw = (phase_dir / "input.json").read_bytes()
        packet = json.loads(raw)
        self.packets.append(packet)
        output = proposal(packet)
        phase = packet["phase"]
        if phase == "match_and_review":
            if self.tamper == "missing_row": output["draft"]["lines"].pop()
            if self.tamper == "merged_rows":
                output["draft"]["lines"][0]["source_ids"] += output["draft"]["lines"].pop()["source_ids"]
            if self.tamper == "unknown_code": output["draft"]["lines"][0]["product_code"] = "NOT-IN-CATALOG"
            if self.tamper == "quote": output["draft"]["lines"][0]["source_quote"] = "invented customer promise"
            if self.tamper == "final_surface": output["draft"]["lines"][0]["surface"] = "热浸锌"
            if self.tamper == "new_product":
                line = output["draft"]["lines"][0]
                line["product_code"] = ""
                output["status"] = "needs_clarification"
                output["new_product_suggestions"] = [{"source_ids": line["source_ids"], "source_quote": line["source_quote"],
                    "product_name": line["original_name"], "spec": line["original_spec"], "standard": line["standard"],
                    "grade": line["grade"], "surface": line["surface"], "material": line["original_material"],
                    "base_unit": "支", "reason": "合成新品建议，尚未创建"}]
            if self.tamper == "coverage": output["source_coverage"][0]["source_id"] = "other-source"
        runner._atomic_json(phase_dir / "result.json", output)
        status = {key: packet[key] for key in ("job_id", "session_id", "base_version", "input_hash")}
        status.update(input_file_sha256=sha256(raw).hexdigest(), rules_hash=runner._rules(config.rules_root)[1],
            output_schema_sha256=sha256(runner.SCHEMA_PATH.read_bytes()).hexdigest(), engine="codex",
            external_submit=False, tool_calls=0, status="succeeded",
            result_sha256=sha256((phase_dir / "result.json").read_bytes()).hexdigest())
        if self.tamper in {"status_hash", "tool_bool", "external", "result_hash", "packet", "failed"}:
            if self.tamper == "status_hash": status["input_hash"] = "0" * 64
            if self.tamper == "tool_bool": status["tool_calls"] = False
            if self.tamper == "external": status["external_submit"] = True
            if self.tamper == "result_hash": status["result_sha256"] = "0" * 64
            if self.tamper == "packet": runner._atomic_json(phase_dir / "input.json", {**packet, "customer": "changed"})
            if self.tamper == "failed": status["status"] = "failed"
        runner._atomic_json(phase_dir / "status.json", status)
        if self.callback:
            self.callback(packet)
        return 1 if self.tamper == "failed" else 0


def viewed(engine, identity):
    return engine.detail(user("sales", ["sales"]), identity)["session"]


def test_two_stage_catalog_matching_stores_proposal_not_draft_or_submission(active_case):
    engine, config = active_case
    requested = request(engine, text=TEXT)
    original = copy.deepcopy(requested["session"])
    fake = FakeRunner()
    result = worker.run_one(engine.store, config, invoke_runner=fake)
    assert result["status"] == "ready_for_review" and result["external_submit"] is False
    assert [packet["phase"] for packet in fake.packets] == ["analyze", "match_and_review"]
    first, second = fake.packets
    assert first["matching_context"] == {} and first["input_hash"] != second["input_hash"]
    assert "actor_id" not in second and "owner_id" not in second and "customer_name" not in second
    match = second["matching_context"]["matching"]["lines"][0]
    assert match["candidates"][0]["product_code"] == "LS1000"
    assert second["matching_context"]["matching"]["history_prices_used"] is False
    current = viewed(engine, original["id"])
    assert current["draft"] == original["draft"] and current["version"] == original["version"]
    assert current["codex_job"]["preview"]["lines"][0]["product_code"] == "LS1000"
    assert current["status"] == original["status"] and not current["submission_operation_id"]
    assert worker.run_one(engine.store, config, invoke_runner=fake)["claimed"] is False
    assert len(fake.packets) == 2


@pytest.mark.parametrize("tamper,code", [
    ("status_hash", "sales_codex_runner_binding_mismatch"),
    ("tool_bool", "sales_codex_runner_proof_invalid"),
    ("external", "sales_codex_runner_proof_invalid"),
    ("result_hash", "sales_codex_runner_result_changed"),
    ("packet", "sales_codex_packet_changed"),
    ("failed", "sales_codex_runner_failed"),
    ("coverage", "sales_codex_coverage_incomplete"),
    ("unknown_code", "sales_codex_product_not_offered"),
    ("quote", "sales_codex_source_quote_mismatch"),
])
def test_tampering_or_incomplete_failure_never_adopts_a_result(active_case, tamper, code):
    engine, config = active_case
    requested = request(engine, text=TEXT)
    result = worker.run_one(engine.store, config, invoke_runner=FakeRunner(tamper=tamper))
    assert result["status"] == "failed" and result["error_code"] == code
    current = viewed(engine, requested["session"]["id"])
    assert current["draft"]["lines"] == [] and not current["codex_job"]["can_adopt"]
    assert not current["submission_operation_id"] and current["version"] == requested["session"]["version"]


def test_relevant_catalog_change_between_phases_rechecks_and_keeps_original_draft(active_case):
    engine, config = active_case
    requested = request(engine, text=TEXT)
    def changed(_packet):
        with engine.store.connect(write=True) as db:
            db.execute("UPDATE settings SET value=? WHERE key='data_version'", (dumps("v2"),))
            db.execute("UPDATE records SET payload=json_set(payload,'$.spec','M20*90') WHERE kind='inventory'")
    fake = FakeRunner(callback=changed)
    result = worker.run_one(engine.store, config, invoke_runner=fake)
    assert result["status"] == "needs_clarification" and len(fake.packets) == 2
    assert fake.packets[1]['matching_context']['matching']['lines'][0]['needs_question']
    assert viewed(engine, requested["session"]["id"])["draft"]["lines"] == []


def excel_source(engine, *, formula=False):
    session = create(engine)
    book = Workbook()
    sheet = book.active; sheet.title = "原始采购需求"
    sheet.append(["商品编码", "商品名称", "规格", "标准", "等级", "表面处理", "数量", "含税单价", "单位"])
    for quantity in (1, 2):
        sheet.append(["LS1000", "六角头螺栓", "M20*80", "GB/T5783", "8.8", "发黑", quantity, 1.13, "支"])
    if formula: sheet.append(["LS1000", "六角头螺栓", "M20*80", "GB/T5783", "8.8", "发黑", "=1+2", 1.13, "支"])
    raw = BytesIO(); book.save(raw); content = raw.getvalue()
    aid, blob_name = "a" * 32, "b" * 32 + ".bin"
    path = engine.blobs / blob_name; path.write_bytes(content); path.chmod(0o600)
    enable(engine)
    with engine.store.connect(write=True) as db:
        db.execute("INSERT INTO sales_assistant_attachments VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, session["id"], "合成需求.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             sha256(content).hexdigest(), len(content), blob_name, "uploaded", "sales", "2026-10-07"))
        snapshot = {"schema_version": "ul_sales_codex_job_v1", "job_id": "J-excel", "session_id": session["id"],
            "base_version": session["version"], "profile_id": "P", "customer_name": "客户甲", "actor_id": "sales",
            "owner_id": "sales", "current_data_version": "v1", "messages": [], "draft": session["draft"],
            "attachments": [{"id": aid, "name": "合成需求.xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "sha256": sha256(content).hexdigest(), "size": len(content), "blob_name": blob_name}]}
        enqueue(db, snapshot)
    return session, aid, path


def test_excel_duplicate_sku_keeps_two_physical_rows_and_bound_provenance(active_case):
    engine, config = active_case
    session, aid, _ = excel_source(engine)
    fake = FakeRunner()
    result = worker.run_one(engine.store, config, invoke_runner=fake)
    assert result["status"] == "ready_for_review"
    rows = fake.packets[0]["source_rows"]
    assert len(rows) == 3 and rows[0]["ignored"]
    data = [row for row in rows if not row["ignored"]]
    assert [row["row"] for row in data] == [2, 3]
    assert len({row["id"] for row in data}) == 2 and all(row["sheet"] == "原始采购需求" for row in data)
    current = viewed(engine, session["id"])
    lines = current["codex_job"]["preview"]["lines"]
    assert [row["quantity"] for row in lines] == ["1", "2"]
    assert [row["product_code"] for row in lines] == ["LS1000", "LS1000"]
    assert not current["draft"]["lines"]
    with engine.store.connect() as db:
        provenance = json.loads(db.execute("SELECT source_provenance FROM sales_codex_jobs WHERE id='J-excel'").fetchone()[0])
    assert len(provenance) == 2 and all(row["source_id"] == aid for row in provenance.values())


@pytest.mark.parametrize("tamper,status,code", [
    ("missing_row", "needs_clarification", None),
    ("merged_rows", "failed", "sales_codex_rows_merged"),
])
def test_excel_missing_or_merged_rows_do_not_become_ready(active_case, tamper, status, code):
    engine, config = active_case
    session, _, _ = excel_source(engine)
    result = worker.run_one(engine.store, config, invoke_runner=FakeRunner(tamper=tamper))
    assert result["status"] == status
    if code: assert result["error_code"] == code
    current = viewed(engine, session["id"])
    assert not current["draft"]["lines"] and not current["codex_job"]["can_adopt"]


def test_excel_formula_cannot_be_declared_complete_by_the_model(active_case):
    engine, config = active_case
    session, _, _ = excel_source(engine, formula=True)
    fake = FakeRunner()
    result = worker.run_one(engine.store, config, invoke_runner=fake)
    assert result["status"] == "needs_clarification"
    formula = fake.packets[0]["source_rows"][-1]
    assert formula["requires_review"] and "=1+2" in formula["cells"]
    assert not viewed(engine, session["id"])["codex_job"]["can_adopt"]


def test_changed_original_blob_fails_before_any_inference(active_case):
    engine, config = active_case
    _, _, path = excel_source(engine)
    path.write_bytes(path.read_bytes() + b"changed")
    fake = FakeRunner()
    result = worker.run_one(engine.store, config, invoke_runner=fake)
    assert result["status"] == "failed" and result["error_code"] == "sales_codex_attachment_changed"
    assert fake.packets == []


def test_final_attribute_change_is_rechecked_against_current_catalog(active_case):
    engine, config = active_case
    requested = request(engine, text=TEXT)
    fake = FakeRunner(tamper="final_surface")
    result = worker.run_one(engine.store, config, invoke_runner=fake)
    assert result["status"] == "needs_clarification"
    assert fake.packets[1]["matching_context"]["matching"]["lines"][0]["candidates"][0]["available"]
    current = viewed(engine, requested["session"]["id"])
    assert not current["codex_job"]["can_adopt"] and not current["draft"]["lines"]


def test_existing_candidate_cannot_be_presented_as_a_new_product(active_case):
    engine, config = active_case
    session, _, _ = excel_source(engine)
    fake = FakeRunner(tamper="new_product")
    result = worker.run_one(engine.store, config, invoke_runner=fake)
    assert result["status"] == "needs_clarification" and not result.get("error_code")
    current = viewed(engine, session["id"])
    assert not current["codex_job"]["new_product_suggestions"]
    assert any('现有编码' in q for q in current['codex_job']['questions'])
    assert not current["codex_job"]["can_adopt"] and not current["draft"]["lines"]


def test_large_unavailable_batch_keeps_every_line_and_bounds_questions(active_case):
    engine, _ = active_case
    source = {"source_id": "source-1", "text": TEXT}
    packet = {"source_rows": [], "sources": [source]}
    value = proposal(packet)
    original = value["draft"]["lines"][0]
    value["draft"]["lines"] = [{**copy.deepcopy(original), "product_code": "TEST-" + str(index)} for index in range(500)]
    value["source_coverage"][0]["line_count"] = 500
    context = {"matching": {"catalog_complete": True, "lines": [
        {"candidates": [{"product_code": row["product_code"], "available": False}]}
        for row in value["draft"]["lines"]]}}
    checked = worker._review_result(engine.store, {}, value, context, [], {}, [source], set(), {"source-1": TEXT})
    assert checked["status"] == "needs_clarification" and len(checked["draft"]["lines"]) == 500
    assert len(checked["questions"]) == 30 and "471" in checked["questions"][-1]
    runner.validate_schema(checked, json.loads(runner.SCHEMA_PATH.read_text()))


def test_pdf_partial_text_and_unrendered_page_cannot_be_declared_complete(active_case, monkeypatch):
    engine, config = active_case
    session, aid, old_path = excel_source(engine)
    raw = b"%PDF-1.4\nsynthetic-no-content"
    old_path.write_bytes(raw)
    with engine.store.connect(write=True) as db:
        db.execute("UPDATE sales_assistant_attachments SET name='合成.pdf',mime_type='application/pdf',sha256=?,size=? WHERE id=?",
                   (sha256(raw).hexdigest(), len(raw), aid))
        frozen = json.loads(db.execute("SELECT input_json FROM sales_codex_jobs WHERE id='J-excel'").fetchone()[0])
        frozen["attachments"][0].update(name="合成.pdf", mime_type="application/pdf", sha256=sha256(raw).hexdigest(), size=len(raw))
        # A fresh immutable enqueue uses the new original file digest.
        db.execute("DELETE FROM sales_codex_jobs WHERE id='J-excel'")
        enqueue(db, frozen)
    monkeypatch.setattr(worker, "_document", lambda *args: SimpleNamespace(status="needs_review", page_count=2,
        unready_pages=[2], lines=[SimpleNamespace(page_number=1, line_number=1, text=TEXT)]))
    class PDFRunner(FakeRunner):
        def __call__(self, phase_dir, config):
            result = super().__call__(phase_dir, config)
            value = json.loads((phase_dir / "result.json").read_text())
            value["draft"]["lines"][0]["source_quote"] = TEXT
            runner._atomic_json(phase_dir / "result.json", value)
            status = json.loads((phase_dir / "status.json").read_text())
            status["result_sha256"] = sha256((phase_dir / "result.json").read_bytes()).hexdigest()
            runner._atomic_json(phase_dir / "status.json", status)
            return result
    result = worker.run_one(engine.store, config, invoke_runner=PDFRunner())
    assert result["status"] == "needs_clarification"
    current = viewed(engine, session["id"])
    assert not current["codex_job"]["can_adopt"]
    with engine.store.connect() as db:
        proof = json.loads(db.execute("SELECT result_json FROM sales_codex_jobs WHERE id='J-excel'").fetchone()[0])
    assert proof["source_coverage"][0]["status"] == "needs_review"


def test_database_identity_restores_root_even_when_database_operation_fails(monkeypatch):
    current = {"uid": 0, "gid": 0}; changes = []
    monkeypatch.setattr(worker.os, "geteuid", lambda: current["uid"])
    monkeypatch.setattr(worker.os, "getegid", lambda: current["gid"])
    monkeypatch.setattr(worker.os, "seteuid", lambda uid: (current.update(uid=uid), changes.append(("uid", uid))))
    monkeypatch.setattr(worker.os, "setegid", lambda gid: (current.update(gid=gid), changes.append(("gid", gid))))
    with pytest.raises(RuntimeError):
        with worker._database_identity(10001, 10001):
            assert current == {"uid": 10001, "gid": 10001}
            raise RuntimeError("transaction failed")
    assert current == {"uid": 0, "gid": 0}
    assert changes == [("gid", 10001), ("uid", 10001), ("uid", 0), ("gid", 0)]
