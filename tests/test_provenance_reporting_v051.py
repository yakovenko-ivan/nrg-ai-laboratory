import csv
import json
from pathlib import Path
import tempfile
import unittest

from agent_workspace.lab_bridge import summarize_current_execution_provenance
from nrg_analysis.campaign import Campaign


class ProvenanceReportingV051Tests(unittest.TestCase):
    def test_execution_provenance_is_separate_and_legacy_job_id_is_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cases_csv = root / "cases.csv"
            case1 = root / "R000001"
            case2 = root / "R000002"
            case1.mkdir()
            case2.mkdir()
            with cases_csv.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["case_id", "case_path"])
                writer.writeheader()
                writer.writerow({"case_id": "R000001", "case_path": str(case1)})
                writer.writerow({"case_id": "R000002", "case_path": str(case2)})

            (case1 / "run_status.json").write_text(json.dumps({
                "status": "finished",
                "nrg_termination_reason": "final_simulation_time",
                "termination_condition": "final_simulation_time",
                "physical_condition_status": None,
            }), encoding="utf-8")
            (case2 / "run_status.json").write_text(json.dumps({
                "status": "condition_met",
                "nrg_termination_reason": "external_stop_request",
                "termination_condition": "quasistationary",
                "physical_condition_met": True,
                "physical_condition_status": "quasistationary",
                "attempt_id": "attempt-reset-001",
                "attempt_fingerprint": "abc123",
                "selective_rerun_job_id": "job-pilot-001",
                "termination_profile": "0d_cv_post_ignition_quasistationary_v1",
            }), encoding="utf-8")

            campaign = Campaign.load(cases_csv, root)
            summary = summarize_current_execution_provenance(campaign)

            self.assertFalse(summary["affects_quasistationarity_classification"])
            self.assertEqual(summary["status_counts"], {
                "condition_met": 1,
                "finished": 1,
            })
            self.assertEqual(summary["nrg_termination_reason_counts"], {
                "external_stop_request": 1,
                "final_simulation_time": 1,
            })
            self.assertEqual(summary["unique_attempt_ids"], ["attempt-reset-001"])
            self.assertEqual(summary["unique_runner_job_ids"], ["job-pilot-001"])
            recalc = next(x for x in summary["examples"] if x["case_id"] == "R000002")
            self.assertEqual(recalc["attempt_id"], "attempt-reset-001")
            self.assertEqual(recalc["runner_job_id"], "job-pilot-001")
            self.assertEqual(
                recalc["runner_job_id_source"],
                "selective_rerun_job_id_legacy_fallback",
            )
            self.assertIn("writes run_control.stop", summary["external_stop_semantics"])

    def test_runner_and_pi_contract_expose_distinct_identifiers(self):
        root = Path(__file__).resolve().parents[1]
        runner = (root / "campaign_tools" / "campaign_runner.py").read_text(encoding="utf-8")
        pi = (root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts").read_text(encoding="utf-8")
        bridge = (root / "agent_workspace" / "lab_bridge.py").read_text(encoding="utf-8")

        self.assertIn('"runner_job_id": self.runner_job_id', runner)
        self.assertIn('"attempt_id": attempt_config.get("attempt_id")', runner)
        self.assertIn("job_file.stem if job_file is not None", runner)
        self.assertIn("Keep attempt_id and runner_job_id distinct", pi)
        self.assertIn("execution_provenance.nrg_termination_reason_counts", pi)
        self.assertIn('"execution_provenance": execution_provenance', bridge)
        self.assertIn("Do not infer termination reason from quasistationarity status.", bridge)




if __name__ == "__main__":
    unittest.main()
