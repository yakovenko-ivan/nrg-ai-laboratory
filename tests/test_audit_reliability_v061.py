from __future__ import annotations

import csv
import json
from pathlib import Path

from agent_workspace.lab_bridge import summarize_current_execution_provenance
from nrg_analysis.campaign import Campaign


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
AGENTS = ROOT / "AGENTS.md"
IGNITION_SKILL = ROOT / ".pi" / "skills" / "nrg-0d-ignition-analysis" / "SKILL.md"
BRIDGE = ROOT / "agent_workspace" / "lab_bridge.py"


def test_empty_bridge_stdout_is_never_silently_converted_to_empty_object():
    source = INDEX.read_text(encoding="utf-8")
    assert 'parseJson(result.stdout || "{}")' not in source
    assert 'error: "laboratory bridge returned empty stdout"' in source
    assert 'error: "laboratory bridge execution failed"' in source


def test_full_history_audit_has_extended_timeout_and_explicit_contract():
    source = INDEX.read_text(encoding="utf-8")
    assert "LONG_READ_ONLY_AUDIT_TIMEOUT_MS = 30 * 60_000" in source
    assert "signal, LONG_READ_ONLY_AUDIT_TIMEOUT_MS" in source
    assert "case_count, counts, quasistationary_count, and needs_recalculation_count" in source


def test_fast_execution_summary_tool_is_registered_end_to_end():
    index = INDEX.read_text(encoding="utf-8")
    bridge = BRIDGE.read_text(encoding="utf-8")
    assert 'name: "nrg_campaign_execution_summary"' in index
    assert '"campaign-execution-summary"' in index
    assert 'def cmd_campaign_execution_summary(' in bridge
    assert '"campaign-execution-summary": cmd_campaign_execution_summary' in bridge


def test_policy_rejects_empty_audit_as_success():
    agents = AGENTS.read_text(encoding="utf-8")
    skill = IGNITION_SKILL.read_text(encoding="utf-8")
    assert "empty object (`{}`)" in agents
    assert "must never be interpreted as zero anomalies" in agents
    assert "empty object (`{}`)" in skill
    assert "is **not** evidence that every case passed" in skill


def test_execution_provenance_summary_counts_physical_metadata(tmp_path: Path):
    run1 = tmp_path / "R000001"
    run2 = tmp_path / "R000002"
    run1.mkdir()
    run2.mkdir()

    payload = {
        "status": "condition_met",
        "nrg_termination_reason": "external_stop_request",
        "termination_condition": "physical_condition",
        "physical_condition_met": True,
        "physical_condition_status": "quasistationary",
        "termination_profile": "0d_cv_post_ignition_quasistationary_v1",
        "runner_job_id": "job-a",
    }
    (run1 / "run_status.json").write_text(json.dumps(payload), encoding="utf-8")
    (run2 / "run_status.json").write_text(json.dumps(payload), encoding="utf-8")

    cases = tmp_path / "cases.csv"
    with cases.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "case_path"])
        writer.writeheader()
        writer.writerow({"case_id": "R000001", "case_path": str(run1)})
        writer.writerow({"case_id": "R000002", "case_path": str(run2)})

    campaign = Campaign.load(cases)
    summary = summarize_current_execution_provenance(campaign)

    assert summary["status_counts"] == {"condition_met": 2}
    assert summary["nrg_termination_reason_counts"] == {"external_stop_request": 2}
    assert summary["physical_condition_status_counts"] == {"quasistationary": 2}
    assert summary["physical_condition_met_counts"] == {"true": 2}
    assert summary["termination_profile_counts"] == {
        "0d_cv_post_ignition_quasistationary_v1": 2
    }
