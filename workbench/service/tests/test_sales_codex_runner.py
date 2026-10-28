"""Host runner isolation, bounded inference, proof binding and private outputs."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from tools import sales_codex_runner as runner


def write_private(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))
    path.chmod(0o600)


@pytest.fixture
def case(tmp_path):
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    rules = tmp_path / "rules"
    rules.mkdir(mode=0o700)
    (rules / "SKILL.md").write_text("# Sales draft\nPrepare drafts only; never submit.\n")
    data = {"schema_version": "ul_sales_codex_job_v1", "job_id": "J1", "session_id": "S1",
        "base_version": 1, "input_hash": "a" * 64, "customer": "Synthetic",
        "profile_id": "synthetic", "current_data_version": "v1", "phase": "analyze",
        "messages": [{"id": "message-1", "text": "TEST-M6 12 pieces"}], "draft": {},
        "attachments": [], "matching_context": {},
        "sources": [{"source_id": "message-1", "kind": "text"}], "source_rows": []}
    write_private(job / "input.json", data)
    output = {"assistant_text": "供人工核对", "status": "ready_for_review",
        "draft": {"customer_order_no": "", "delivery_date": "", "note": "",
            "tax_basis": "tax_inclusive", "tax_rate": 13, "lines": []},
        "questions": [], "new_product_suggestions": [],
        "source_coverage": [{"source_id": "message-1", "status": "complete", "line_count": 0,
            "detail": "synthetic empty result"}]}
    return job, rules, data, output


def fake_cli(tmp_path, output, mode="success"):
    path = tmp_path / "codex"
    # These scripts simulate the CLI transport, not business parser behavior.
    path.write_text("#!" + os.path.realpath(sys.executable) + "\n" +
        "import os,sys,json,time\n"
        "args=sys.argv; prompt=sys.stdin.read()\n"
        "assert '--model' not in args and '-m' not in args\n"
        "assert '--sandbox' in args and args[args.index('--sandbox')+1]=='read-only'\n"
        "assert '--ephemeral' in args and '--ignore-user-config' in args\n"
        "assert '--ignore-rules' in args and '--no-daemon' in args\n"
        "assert args[args.index('-a')+1]=='never'\n"
        "assert 'ERP_PASSWORD' not in os.environ and 'OPENAI_API_KEY' not in os.environ\n"
        "assert 'CODEX_API_KEY' not in os.environ\n" +
        ("time.sleep(20)\n" if mode == "timeout" else "") +
        "print(json.dumps({'type':'thread.started'}),flush=True)\n" +
        ("print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'cat auth.json'}}),flush=True)\n"
         if mode == "tool" else "") +
        "out=" + repr(output) + "\n"
        "with open(args[args.index('-o')+1],'w') as f: json.dump(out,f)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(out)}}),flush=True)\n" +
        ("" if mode == "incomplete" else "print(json.dumps({'type':'turn.completed'}),flush=True)\n"))
    path.chmod(0o700)
    return path


def test_private_result_bound_to_exact_input_schema_rules_and_no_external_action(case, tmp_path, monkeypatch):
    job, rules, data, output = case
    monkeypatch.setenv("ERP_PASSWORD", "not-for-model")
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-model")
    monkeypatch.setenv("CODEX_API_KEY", "not-for-model")
    cli = fake_cli(tmp_path, output)
    status = runner.run_job(job, rules_root=rules, codex_binary=cli)
    assert status["status"] == "succeeded"
    assert status["job_id"] == "J1" and status["base_version"] == 1
    assert status["input_file_sha256"] == hashlib.sha256((job / "input.json").read_bytes()).hexdigest()
    assert status["tool_calls"] == 0 and status["external_submit"] is False
    assert json.loads((job / "result.json").read_text()) == output
    assert (job / "result.json").stat().st_mode & 0o777 == 0o600
    assert not list(job.glob(".answer-*"))
    # Durable success is reused locally, even if the CLI is no longer available.
    assert runner.run_job(job, rules_root=rules, codex_binary=tmp_path / "absent") == status


@pytest.mark.parametrize("mode,code", [("tool", "codex_tool_use_forbidden"),
                                     ("incomplete", "codex_turn_incomplete")])
def test_no_tool_events_or_partial_turn_can_be_adopted(case, tmp_path, mode, code):
    job, rules, _, output = case
    status = runner.run_job(job, rules_root=rules, codex_binary=fake_cli(tmp_path, output, mode))
    assert status["status"] == "failed" and status["code"] == code
    assert not (job / "result.json").exists()
    assert "auth.json" not in (job / "status.json").read_text()


def test_timeout_kills_process_and_retains_bound_operational_state(case, tmp_path):
    job, rules, _, output = case
    status = runner.run_job(job, rules_root=rules, codex_binary=fake_cli(tmp_path, output, "timeout"),
                            timeout_seconds=5)
    assert status["status"] == "timed_out" and status["code"] == "codex_timed_out"
    assert status["base_version"] == 1 and not (job / "result.json").exists()


def test_deadline_also_bounds_child_that_never_reads_large_stdin(tmp_path):
    script = tmp_path / "does-not-read"
    script.write_text("#!" + os.path.realpath(sys.executable) + "\nimport time\ntime.sleep(20)\n")
    script.chmod(0o700)
    with pytest.raises(runner.RunnerBlocked, match="codex_timed_out"):
        runner._invoke([str(script)], "synthetic" * 150000, timeout_seconds=5)


@pytest.mark.parametrize("changed", ["extra", "bool_rate", "no_coverage", "negative_count"])
def test_invalid_schema_output_remains_private_failed(case, tmp_path, changed):
    job, rules, _, output = case
    if changed == "extra": output["erp_submit"] = True
    if changed == "bool_rate": output["draft"]["tax_rate"] = True
    if changed == "no_coverage": output.pop("source_coverage")
    if changed == "negative_count": output["source_coverage"][0]["line_count"] = -1
    status = runner.run_job(job, rules_root=rules, codex_binary=fake_cli(tmp_path, output))
    assert status["status"] == "failed" and not (job / "result.json").exists()


def test_completed_job_cannot_be_rebound_or_silently_replace_its_result(case, tmp_path):
    job, rules, data, output = case
    cli = fake_cli(tmp_path, output)
    assert runner.run_job(job, rules_root=rules, codex_binary=cli)["status"] == "succeeded"
    data["base_version"] = 2
    write_private(job / "input.json", data)
    with pytest.raises(runner.RunnerBlocked, match="job_binding_changed"):
        runner.run_job(job, rules_root=rules, codex_binary=cli)


def test_saved_result_tampering_rejected(case, tmp_path):
    job, rules, _, output = case
    cli = fake_cli(tmp_path, output)
    runner.run_job(job, rules_root=rules, codex_binary=cli)
    output["assistant_text"] = "modified"
    write_private(job / "result.json", output)
    with pytest.raises(runner.RunnerBlocked, match="saved_result_changed"):
        runner.run_job(job, rules_root=rules, codex_binary=cli)


@pytest.mark.parametrize("unsafe", ["symlink", "public_input", "public_dir", "rule_symlink"])
def test_paths_and_permissions_checked_before_invoking_cli(case, tmp_path, unsafe):
    job, rules, _, _ = case
    if unsafe == "symlink":
        path = job / "input.json"
        target = tmp_path / "external.json"
        path.rename(target)
        path.symlink_to(target)
    if unsafe == "public_input": (job / "input.json").chmod(0o644)
    if unsafe == "public_dir": job.chmod(0o755)
    if unsafe == "rule_symlink":
        path = rules / "SKILL.md"
        target = tmp_path / "external.md"
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(runner.RunnerBlocked):
        runner.run_job(job, rules_root=rules, codex_binary=tmp_path / "not-invoked")


def test_changed_rule_bundle_cannot_reuse_past_result(case, tmp_path):
    job, rules, _, output = case
    cli = fake_cli(tmp_path, output)
    runner.run_job(job, rules_root=rules, codex_binary=cli)
    (rules / "SKILL.md").write_text("Different reviewed rule version")
    with pytest.raises(runner.RunnerBlocked, match="job_binding_changed"):
        runner.run_job(job, rules_root=rules, codex_binary=cli)


def test_attachment_hash_and_magic_must_match_before_cli(case, tmp_path):
    job, rules, data, _ = case
    image = job / "synthetic.png"
    image.write_bytes(b"not a PNG")
    image.chmod(0o600)
    data["attachments"] = [{"id": "A1", "name": "synthetic.png", "path": "synthetic.png",
        "mime_type": "image/png", "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}]
    write_private(job / "input.json", data)
    with pytest.raises(runner.RunnerBlocked, match="attachment_mime_mismatch"):
        runner.run_job(job, rules_root=rules, codex_binary=tmp_path / "not-invoked")


def test_prompt_uses_reviewed_rules_sources_and_excludes_absolute_attachment_paths(case):
    _, _, data, _ = case
    data["attachments"] = [{"id": "A1", "path": "/private/secret/path", "name": "photo"}]
    text = runner._prompt(data, "trusted NAS pack")
    assert "trusted NAS pack" in text and "UNTRUSTED BUSINESS DATA" in text
    assert "/private/secret/path" not in text
    assert "source_coverage" in text and "不得确认、上传" in text


def test_command_does_not_override_owner_model_and_disables_every_external_tool_family(case):
    job, _, _, _ = case
    command = runner._command(Path("/trusted/codex"), job, runner.SCHEMA_PATH, job / "answer", [])
    assert "--model" not in command and "-m" not in command
    assert "--search" not in command and "--dangerously-bypass-approvals-and-sandbox" not in command
    for feature in runner.DISABLED_FEATURES:
        assert any(command[index:index+2] == ["--disable", feature] for index in range(len(command)-1))


def test_material_fields_are_explicit_required_source_attributes(case):
    _, _, _, output = case
    schema = json.loads(runner.SCHEMA_PATH.read_text())
    line_schema = schema["properties"]["draft"]["properties"]["lines"]["items"]
    assert "original_material" in line_schema["required"]
    suggested = schema["properties"]["new_product_suggestions"]["items"]
    assert "material" in suggested["required"]
    line = {key: "" for key in line_schema["required"]}
    line.update(source_ids=["message-1"], quantity="1", unit_price="1.13")
    output["draft"]["lines"] = [line]
    runner.validate_schema(output, schema)
    del line["original_material"]
    with pytest.raises(runner.RunnerBlocked):
        runner.validate_schema(output, schema)
