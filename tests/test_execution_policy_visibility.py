from pathlib import Path
import csv
import json
import tempfile
import unittest

from nrg_analysis.laboratory import Laboratory
from agent_workspace.lab_bridge import campaign_status_payload


class ExecutionPolicyVisibilityTests(unittest.TestCase):
    def test_finished_cases_are_explicitly_skipped_by_trusted_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for p in [
                root / "campaigns",
                root / "runs",
                root / "studies",
                root / "resources" / "task_setup",
                root / "bin",
                root / "config",
            ]:
                p.mkdir(parents=True, exist_ok=True)

            cm = root / "bin" / "computing_module"
            pi = root / "bin" / "package_interface"
            cm.write_text("", encoding="utf-8")
            pi.write_text("", encoding="utf-8")
            cm.chmod(0o755)
            pi.chmod(0o755)

            runner = root / "config" / "campaign_runner.json"
            runner.write_text(json.dumps({
                "threads": 1,
                "max_concurrent_cases": 1,
                "skip_statuses": ["finished", "condition_met"],
                "rerun_failed": True,
            }), encoding="utf-8")

            lab_toml = root / "config" / "laboratory.toml"
            lab_toml.write_text(f"""
[paths]
research_root = "{root}"
campaign_root = "{root / 'campaigns'}"
runs_root = "{root / 'runs'}"
studies_root = "{root / 'studies'}"
task_setup_template = "{root / 'resources' / 'task_setup'}"

[runtime]
computing_module = "{cm}"
package_interface_0d = "{pi}"

[execution]
default_threads = 1
runner_config = "{runner}"
""", encoding="utf-8")
            lab = Laboratory.load(lab_toml)

            rows = [
                ("R000001", "finished"),
                ("R000002", "not_started"),
                ("R000003", "invalid_status"),
            ]
            cases = root / "campaigns" / "cases.csv"
            with cases.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["case_id", "case_path"])
                w.writeheader()
                for cid, status in rows:
                    cp = root / "runs" / cid
                    cp.mkdir()
                    if status == "finished":
                        (cp / "run_status.json").write_text(json.dumps({"status": "finished"}), encoding="utf-8")
                    elif status == "invalid_status":
                        (cp / "run_status.json").write_bytes(b"")
                    w.writerow({"case_id": cid, "case_path": str(cp)})

            payload = campaign_status_payload(cases, lab, runner)
            self.assertEqual(payload["runnable_cases"], 2)
            self.assertEqual(payload["skipped_by_policy"], {"finished": 1})
            self.assertEqual(payload["runnable_by_status"], {"invalid_status": 1, "not_started": 1})
            self.assertEqual(payload["execution_policy"]["skip_statuses"], ["condition_met", "finished"])
            self.assertEqual(payload["execution_policy"]["threads"], 1)
            self.assertEqual(payload["execution_policy"]["max_concurrent_cases"], 1)


if __name__ == "__main__":
    unittest.main()
