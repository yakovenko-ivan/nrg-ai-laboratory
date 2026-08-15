import csv
import json
from pathlib import Path
import tempfile
import unittest

from agent_workspace.lab_bridge import selective_rerun_plan
from campaign_tools.campaign_runner import CampaignRunner
from nrg_analysis.laboratory import Laboratory


def make_lab(root: Path) -> tuple[Laboratory, Path, Path]:
    for p in (
        root / "campaigns",
        root / "runs",
        root / "studies",
        root / "resources" / "task_setup",
        root / "bin",
        root / "config",
    ):
        p.mkdir(parents=True, exist_ok=True)

    executable = root / "bin" / "computing_module"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '0 1000\\n1e-6 1100\\n' > reactor_history.dat\n"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    interface = root / "bin" / "package_interface"
    interface.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interface.chmod(0o755)

    run_config = root / "config" / "campaign_runner.json"
    run_config.write_text(
        json.dumps(
            {
                "threads": 1,
                "max_concurrent_cases": 1,
                "limit_library_threads": True,
                "poll_seconds": 0.01,
                "max_runtime_seconds": 10,
                "success_exit_codes": [0],
                "skip_statuses": ["finished", "condition_met"],
                "rerun_failed": True,
                "archive_old_monitor_files": False,
                "verify_case_fingerprint": True,
                "monitor_rules": [],
            }
        ),
        encoding="utf-8",
    )

    lab_toml = root / "config" / "laboratory.toml"
    lab_toml.write_text(
        f"""
[paths]
research_root = "{root}"
campaign_root = "{root / 'campaigns'}"
runs_root = "{root / 'runs'}"
studies_root = "{root / 'studies'}"
task_setup_template = "{root / 'resources' / 'task_setup'}"

[runtime]
computing_module = "{executable}"
package_interface_0d = "{interface}"

[execution]
default_threads = 1
runner_config = "{run_config}"
""",
        encoding="utf-8",
    )
    return Laboratory.load(lab_toml), lab_toml, run_config


def make_campaign(root: Path) -> Path:
    manifest = root / "campaigns" / "cases.csv"
    rows = []
    for n in range(1, 4):
        cid = f"R{n:06d}"
        fingerprint = f"fp-{n}"
        cp = root / "runs" / cid
        (cp / "task_setup").mkdir(parents=True)
        (cp / "setup_input.nml").write_text(
            f"&case\n case_fingerprint = '{fingerprint}'\n/\n",
            encoding="utf-8",
        )
        (cp / "run_status.json").write_text(
            json.dumps({"case_id": cid, "status": "finished", "process_pid": None}),
            encoding="utf-8",
        )
        (cp / "reactor_history.dat").write_text(f"old-{cid}\n", encoding="utf-8")
        rows.append(
            {
                "case_id": cid,
                "case_fingerprint": fingerprint,
                "label": cid,
                "case_path": str(cp),
            }
        )

    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["case_id", "case_fingerprint", "label", "case_path"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return manifest


class SelectiveRerunTests(unittest.TestCase):
    def test_plan_bypasses_skip_only_for_exact_named_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, _lab_toml, config = make_lab(root)
            manifest = make_campaign(root)
            plan = selective_rerun_plan(
                manifest,
                ["R000002", "R000003"],
                lab,
                config,
            )
            self.assertTrue(plan["can_start"])
            self.assertEqual(plan["selected_count"], 2)
            self.assertEqual(plan["policy_bypass_count"], 2)
            self.assertEqual(plan["other_cases_untouched"], 1)
            self.assertEqual(
                [x["case_id"] for x in plan["selected_cases"]],
                ["R000002", "R000003"],
            )
            self.assertTrue(all(x["current_status"] == "finished" for x in plan["selected_cases"]))

    def test_runner_executes_only_selected_finished_case_and_archives_prior_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, lab_toml, config = make_lab(root)
            manifest = make_campaign(root)

            runner = CampaignRunner(
                manifest,
                config,
                lab_toml,
                selective_rerun_case_ids=["R000002"],
                rerun_job_id="test-selective-job",
            )
            rc = runner.run()
            self.assertEqual(rc, 0)

            case1 = root / "runs" / "R000001"
            case2 = root / "runs" / "R000002"
            case3 = root / "runs" / "R000003"

            # Non-selected finished cases are untouched.
            self.assertEqual((case1 / "reactor_history.dat").read_text(), "old-R000001\n")
            self.assertEqual((case3 / "reactor_history.dat").read_text(), "old-R000003\n")

            # Selected case was genuinely re-executed.
            self.assertIn("1e-6 1100", (case2 / "reactor_history.dat").read_text())
            new_status = json.loads((case2 / "run_status.json").read_text())
            self.assertEqual(new_status["status"], "finished")
            self.assertTrue(new_status["selective_rerun"])
            self.assertEqual(new_status["selective_rerun_job_id"], "test-selective-job")

            archive = case2 / "_rerun_archive" / "test-selective-job"
            self.assertTrue(archive.is_dir())
            self.assertEqual(
                (archive / "reactor_history.dat").read_text(),
                "old-R000002\n",
            )
            archived_status = json.loads((archive / "run_status.json").read_text())
            self.assertEqual(archived_status["status"], "finished")
            archive_manifest = json.loads((archive / "archive_manifest.json").read_text())
            archived_names = {x["name"] for x in archive_manifest["files"]}
            self.assertIn("reactor_history.dat", archived_names)
            self.assertIn("run_status.json", archived_names)

    def test_unknown_case_is_blocked_before_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, _lab_toml, config = make_lab(root)
            manifest = make_campaign(root)
            plan = selective_rerun_plan(manifest, ["R999999"], lab, config)
            self.assertFalse(plan["can_start"])
            self.assertEqual(plan["unknown_case_ids"], ["R999999"])


if __name__ == "__main__":
    unittest.main()
