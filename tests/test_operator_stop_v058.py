import csv
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest

from campaign_tools.campaign_runner import CampaignRunner


class OperatorStopRunnerTests(unittest.TestCase):
    def build_lab(self, root: Path, case_ids=("R000001", "R000002")):
        for directory in (
            root / "campaigns",
            root / "runs",
            root / "studies",
            root / "resources" / "task_setup",
            root / "bin",
            root / "config",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        exe = root / "bin" / "computing_module"
        exe.write_text(
            "#!/bin/sh\n"
            "i=0\n"
            "while [ ! -f run_control.stop ] && [ $i -lt 80 ]; do\n"
            "  sleep 0.01\n"
            "  i=$((i+1))\n"
            "done\n"
            "if [ -f run_control.stop ]; then\n"
            "cat > run_control_status.inf <<'EOF'\n"
            "&run_control_status\n"
            " termination_reason = 'external_stop_request'\n"
            " final_simulation_time = 1.0e-4\n"
            " elapsed_wall_time = 0.1\n"
            " restart_checkpoint_required = .false.\n"
            "/\n"
            "EOF\n"
            "exit 0\n"
            "fi\n"
            "cat > run_control_status.inf <<'EOF'\n"
            "&run_control_status\n"
            " termination_reason = 'final_simulation_time'\n"
            " final_simulation_time = 2.0e-3\n"
            " elapsed_wall_time = 0.8\n"
            " restart_checkpoint_required = .false.\n"
            "/\n"
            "EOF\n"
            "exit 0\n",
            encoding="utf-8",
        )
        exe.chmod(0o755)
        interface = root / "bin" / "package_interface"
        interface.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interface.chmod(0o755)

        run_config = root / "config" / "campaign_runner.json"
        run_config.write_text(json.dumps({
            "threads": 1,
            "max_concurrent_cases": 1,
            "limit_library_threads": True,
            "poll_seconds": 0.01,
            "max_runtime_seconds": 5,
            "success_exit_codes": [0],
            "skip_statuses": ["finished", "condition_met"],
            "rerun_failed": True,
            "archive_old_monitor_files": False,
            "monitor_log_each_read": False,
            "verify_case_fingerprint": False,
            "stop_request_file": "run_control.stop",
            "run_control_status_file": "run_control_status.inf",
            "graceful_stop_timeout_seconds": 1,
            "force_kill_on_graceful_stop_timeout": True,
            "monitor_rules": [],
        }), encoding="utf-8")
        lab_toml = root / "config" / "laboratory.toml"
        lab_toml.write_text(f'''\n[paths]\nresearch_root = "{root}"\ncampaign_root = "{root / 'campaigns'}"\nruns_root = "{root / 'runs'}"\nstudies_root = "{root / 'studies'}"\ntask_setup_template = "{root / 'resources' / 'task_setup'}"\n\n[runtime]\ncomputing_module = "{exe}"\npackage_interface_0d = "{interface}"\n\n[execution]\ndefault_threads = 1\nrunner_config = "{run_config}"\n''', encoding="utf-8")

        manifest = root / "campaigns" / "cases.csv"
        with manifest.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["case_id", "case_fingerprint", "case_path", "label"])
            writer.writeheader()
            for cid in case_ids:
                case = root / "runs" / cid
                (case / "task_setup").mkdir(parents=True)
                writer.writerow({
                    "case_id": cid,
                    "case_fingerprint": f"fp-{cid}",
                    "case_path": str(case),
                    "label": cid,
                })
        return exe, run_config, lab_toml, manifest

    @staticmethod
    def wait_running(case: Path, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sp = case / "run_status.json"
            if sp.is_file():
                data = json.loads(sp.read_text())
                if data.get("status") == "running":
                    return data
            time.sleep(0.01)
        raise AssertionError(f"case did not enter running state: {case}")

    def test_campaign_stop_stops_current_case_and_prevents_next_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, run_config, lab_toml, manifest = self.build_lab(root)
            control = root / "campaigns" / "_jobs" / "job-campaign-stop.control.json"
            control.parent.mkdir(parents=True)
            runner = CampaignRunner(
                manifest, run_config, lab_toml,
                runner_job_id="job-campaign-stop",
                control_file=control,
            )
            result = {}
            thread = threading.Thread(target=lambda: result.setdefault("rc", runner.run()), daemon=True)
            thread.start()
            self.wait_running(root / "runs" / "R000001")
            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "req-campaign",
                "action": "stop_campaign",
                "runner_job_id": "job-campaign-stop",
                "case_id": None,
            }), encoding="utf-8")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result.get("rc"), 0)
            s1 = json.loads((root / "runs" / "R000001" / "run_status.json").read_text())
            self.assertEqual(s1["status"], "stopped")
            self.assertEqual(s1["termination_condition"], "operator_campaign_stop")
            self.assertEqual(s1["nrg_termination_reason"], "external_stop_request")
            self.assertTrue(s1["operator_stop_requested"])
            self.assertFalse((root / "runs" / "R000002" / "run_status.json").exists())
            ctl = json.loads(control.read_text())
            self.assertEqual(ctl["state"], "handled")
            self.assertEqual(ctl["outcome"], "case_stopped")
            self.assertTrue(runner.campaign_stop_requested)

    def test_case_stop_stops_only_target_and_runner_continues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, run_config, lab_toml, manifest = self.build_lab(root)
            control = root / "campaigns" / "_jobs" / "job-case-stop.control.json"
            control.parent.mkdir(parents=True)
            runner = CampaignRunner(
                manifest, run_config, lab_toml,
                runner_job_id="job-case-stop",
                control_file=control,
            )
            result = {}
            thread = threading.Thread(target=lambda: result.setdefault("rc", runner.run()), daemon=True)
            thread.start()
            self.wait_running(root / "runs" / "R000001")
            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "req-case",
                "action": "stop_case",
                "runner_job_id": "job-case-stop",
                "case_id": "R000001",
            }), encoding="utf-8")
            thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result.get("rc"), 0)
            s1 = json.loads((root / "runs" / "R000001" / "run_status.json").read_text())
            s2 = json.loads((root / "runs" / "R000002" / "run_status.json").read_text())
            self.assertEqual(s1["status"], "stopped")
            self.assertEqual(s1["termination_condition"], "operator_case_stop")
            self.assertEqual(s2["status"], "finished")
            self.assertEqual(s2["nrg_termination_reason"], "final_simulation_time")
            ctl = json.loads(control.read_text())
            self.assertEqual(ctl["state"], "handled")
            self.assertFalse(runner.campaign_stop_requested)

    def test_runner_main_records_stopped_job_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, run_config, lab_toml, manifest = self.build_lab(root)
            jobs = root / "campaigns" / "_jobs"
            jobs.mkdir(parents=True)
            job_file = jobs / "job-main-stop.json"
            control = jobs / "job-main-stop.control.json"
            import campaign_tools.campaign_runner as runner_module
            env = os.environ.copy()
            project_root = str(Path(runner_module.__file__).resolve().parents[1])
            env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.Popen([
                sys.executable, str(Path(runner_module.__file__).resolve()),
                str(manifest), str(run_config),
                "--laboratory", str(lab_toml),
                "--job-file", str(job_file),
                "--control-file", str(control),
            ], cwd=project_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.wait_running(root / "runs" / "R000001")
            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "req-main",
                "action": "stop_campaign",
                "runner_job_id": "job-main-stop",
                "case_id": None,
            }), encoding="utf-8")
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, msg=stdout + "\n" + stderr)
            job = json.loads(job_file.read_text())
            self.assertEqual(job["state"], "stopped")
            self.assertFalse(job["runner_lock"]["active"])
            self.assertTrue(job["operator_stop_events"])
            self.assertEqual(job["operator_stop_events"][0]["action"], "stop_campaign")



class OperatorStopContractTests(unittest.TestCase):
    def test_extension_exposes_stop_tools(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts").read_text()
        self.assertIn('name: "nrg_campaign_stop"', text)
        self.assertIn('name: "nrg_case_stop"', text)
        self.assertIn('campaign-stop-plan', text)
        self.assertIn('case-stop-plan', text)

    def test_launch_paths_pass_control_file(self):
        root = Path(__file__).resolve().parents[1]
        bridge = (root / "agent_workspace" / "lab_bridge.py").read_text()
        self.assertGreaterEqual(bridge.count('"--control-file"'), 3)
        self.assertIn('campaign-stop-execute', bridge)
        self.assertIn('case-stop-execute', bridge)
        self.assertIn('runner_job_predates_operator_control', bridge)

    def test_agents_defines_stopped_semantics(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "AGENTS.md").read_text()
        self.assertIn("**STOPPED**", text)
        self.assertIn("`nrg_campaign_stop`", text)
        self.assertIn("`nrg_case_stop`", text)
        self.assertIn("operator_campaign_stop", text)


if __name__ == "__main__":
    unittest.main()
