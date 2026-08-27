import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from campaign_tools.campaign_runner import CampaignRunner


class CampaignPauseResumeTests(unittest.TestCase):
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
            "# A paused case leaves a synthetic restart marker. On the next launch,\n"
            "# finish normally so the test can verify ordinary resume semantics.\n"
            "if [ -f data_output/restart.marker ]; then\n"
            "  sleep 0.03\n"
            "  cat > run_control_status.inf <<'EOF'\n"
            "&run_control_status\n"
            " termination_reason = 'final_simulation_time'\n"
            " final_simulation_time = 2.0e-2\n"
            " elapsed_wall_time = 0.03\n"
            " restart_checkpoint_required = .false.\n"
            "/\n"
            "EOF\n"
            "  exit 0\n"
            "fi\n"
            "i=0\n"
            "while [ $i -lt 80 ]; do\n"
            "  if [ -f run_control.pause ]; then\n"
            "    mkdir -p data_output\n"
            "    echo restart > data_output/restart.marker\n"
            "    cat > run_control_status.inf <<'EOF'\n"
            "&run_control_status\n"
            " termination_reason = 'external_pause_request'\n"
            " final_simulation_time = 7.5e-3\n"
            " elapsed_wall_time = 0.1\n"
            " restart_checkpoint_required = .true.\n"
            "/\n"
            "EOF\n"
            "    exit 0\n"
            "  fi\n"
            "  if [ -f run_control.stop ]; then\n"
            "    cat > run_control_status.inf <<'EOF'\n"
            "&run_control_status\n"
            " termination_reason = 'external_stop_request'\n"
            " final_simulation_time = 7.5e-3\n"
            " elapsed_wall_time = 0.1\n"
            " restart_checkpoint_required = .false.\n"
            "/\n"
            "EOF\n"
            "    exit 0\n"
            "  fi\n"
            "  sleep 0.01\n"
            "  i=$((i+1))\n"
            "done\n"
            "cat > run_control_status.inf <<'EOF'\n"
            "&run_control_status\n"
            " termination_reason = 'final_simulation_time'\n"
            " final_simulation_time = 2.0e-2\n"
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
            "pause_request_file": "run_control.pause",
            "run_control_status_file": "run_control_status.inf",
            "graceful_stop_timeout_seconds": 1,
            "force_kill_on_graceful_stop_timeout": True,
            "monitor_rules": [],
        }), encoding="utf-8")

        lab_toml = root / "config" / "laboratory.toml"
        lab_toml.write_text(f'''\n[paths]\nresearch_root = "{root}"\ncampaign_root = "{root / 'campaigns'}"\nruns_root = "{root / 'runs'}"\nstudies_root = "{root / 'studies'}"\ntask_setup_template = "{root / 'resources' / 'task_setup'}"\n\n[runtime]\ncomputing_module = "{exe}"\npackage_interface_0d = "{interface}"\n\n[execution]\ndefault_threads = 1\nrunner_config = "{run_config}"\n''', encoding="utf-8")

        manifest = root / "campaigns" / "cases.csv"
        with manifest.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["case_id", "case_fingerprint", "case_path", "label"],
            )
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
        return run_config, lab_toml, manifest

    @staticmethod
    def wait_running(case: Path, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = case / "run_status.json"
            if path.is_file():
                status = json.loads(path.read_text())
                if status.get("status") == "running":
                    return status
            time.sleep(0.01)
        raise AssertionError(f"case did not enter running state: {case}")

    def test_pause_now_writes_restart_and_prevents_next_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_config, lab_toml, manifest = self.build_lab(root)
            control = root / "campaigns" / "_jobs" / "pause-now.control.json"
            control.parent.mkdir(parents=True)
            runner = CampaignRunner(
                manifest,
                run_config,
                lab_toml,
                runner_job_id="pause-now",
                control_file=control,
            )
            result = {}
            thread = threading.Thread(
                target=lambda: result.setdefault("rc", runner.run()), daemon=True
            )
            thread.start()
            self.wait_running(root / "runs" / "R000001")
            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "req-pause-now",
                "action": "pause_campaign",
                "runner_job_id": "pause-now",
                "case_id": None,
            }), encoding="utf-8")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result.get("rc"), 0)

            case1 = root / "runs" / "R000001"
            status = json.loads((case1 / "run_status.json").read_text())
            self.assertEqual(status["status"], "paused")
            self.assertEqual(status["termination_condition"], "operator_campaign_pause")
            self.assertEqual(status["nrg_termination_reason"], "external_pause_request")
            self.assertTrue(status["nrg_restart_required"])
            self.assertTrue((case1 / "data_output" / "restart.marker").is_file())
            self.assertTrue((case1 / "run_paused.done").is_file())
            self.assertFalse((root / "runs" / "R000002" / "run_status.json").exists())
            self.assertTrue(runner.campaign_pause_requested)
            observed = json.loads(control.read_text())
            self.assertEqual(observed["state"], "handled")
            self.assertEqual(observed["outcome"], "pause_verified")

    def test_pause_after_current_does_not_interrupt_active_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_config, lab_toml, manifest = self.build_lab(root)
            control = root / "campaigns" / "_jobs" / "pause-after.control.json"
            control.parent.mkdir(parents=True)
            runner = CampaignRunner(
                manifest,
                run_config,
                lab_toml,
                runner_job_id="pause-after",
                control_file=control,
            )
            result = {}
            thread = threading.Thread(
                target=lambda: result.setdefault("rc", runner.run()), daemon=True
            )
            thread.start()
            self.wait_running(root / "runs" / "R000001")
            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "req-pause-after",
                "action": "pause_after_current",
                "runner_job_id": "pause-after",
                "case_id": None,
            }), encoding="utf-8")

            # The request should be acknowledged while the CFD case is still active.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                observed = json.loads(control.read_text())
                if observed.get("state") == "acknowledged":
                    break
                time.sleep(0.01)
            else:
                self.fail("pause_after_current was not acknowledged")

            case1 = root / "runs" / "R000001"
            self.assertFalse((case1 / "run_control.pause").exists())
            self.assertFalse((case1 / "run_control.stop").exists())

            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            status = json.loads((case1 / "run_status.json").read_text())
            self.assertEqual(status["status"], "finished")
            self.assertEqual(status["nrg_termination_reason"], "final_simulation_time")
            self.assertFalse((case1 / "data_output" / "restart.marker").exists())
            self.assertFalse((root / "runs" / "R000002" / "run_status.json").exists())
            observed = json.loads(control.read_text())
            self.assertEqual(observed["state"], "handled")
            self.assertEqual(observed["outcome"], "current_case_completed_runner_paused")

    def test_runner_max_runtime_creates_restart_required_and_stops_campaign(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_config, lab_toml, manifest = self.build_lab(root)
            cfg = json.loads(run_config.read_text(encoding="utf-8"))
            cfg["max_runtime_seconds"] = 0.10
            run_config.write_text(json.dumps(cfg), encoding="utf-8")

            runner = CampaignRunner(
                manifest,
                run_config,
                lab_toml,
                runner_job_id="runtime-ceiling",
            )
            rc = runner.run()
            self.assertEqual(rc, 0)

            case1 = root / "runs" / "R000001"
            status = json.loads((case1 / "run_status.json").read_text())
            self.assertEqual(status["status"], "restart_required")
            self.assertEqual(status["termination_condition"], "runner_max_runtime")
            self.assertEqual(status["nrg_termination_reason"], "external_pause_request")
            self.assertTrue(status["nrg_restart_required"])
            self.assertTrue((case1 / "data_output" / "restart.marker").is_file())
            self.assertTrue((case1 / "run_restart_required.done").is_file())
            self.assertTrue(runner.campaign_restart_required)
            self.assertFalse((root / "runs" / "R000002" / "run_status.json").exists())

    def test_ordinary_resume_relaunches_paused_case_without_deleting_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_config, lab_toml, manifest = self.build_lab(root)
            control = root / "campaigns" / "_jobs" / "resume.control.json"
            control.parent.mkdir(parents=True)

            first = CampaignRunner(
                manifest,
                run_config,
                lab_toml,
                runner_job_id="pause-first",
                control_file=control,
            )
            thread = threading.Thread(target=first.run, daemon=True)
            thread.start()
            self.wait_running(root / "runs" / "R000001")
            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "req-first-pause",
                "action": "pause_campaign",
                "runner_job_id": "pause-first",
                "case_id": None,
            }), encoding="utf-8")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

            case1 = root / "runs" / "R000001"
            restart = case1 / "data_output" / "restart.marker"
            self.assertTrue(restart.is_file())
            self.assertEqual(json.loads((case1 / "run_status.json").read_text())["status"], "paused")

            # Ordinary continuation: same manifest, no selective-rerun flags.
            resume = CampaignRunner(manifest, run_config, lab_toml, runner_job_id="resume")
            rc = resume.run()
            self.assertEqual(rc, 0)
            self.assertTrue(restart.is_file(), "restart data must survive ordinary resume")
            self.assertFalse((case1 / "run_control.pause").exists(), "stale pause marker must be cleaned")
            self.assertEqual(json.loads((case1 / "run_status.json").read_text())["status"], "finished")
            self.assertEqual(
                json.loads((root / "runs" / "R000002" / "run_status.json").read_text())["status"],
                "finished",
            )


class CampaignPauseContractTests(unittest.TestCase):
    def test_extension_exposes_pause_tool(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts").read_text()
        self.assertIn('name: "nrg_campaign_pause"', text)
        self.assertIn('campaign-pause-plan', text)
        self.assertIn('campaign-pause-execute', text)

    def test_runner_distinguishes_pause_and_stop_markers(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "campaign_tools" / "campaign_runner.py").read_text()
        self.assertIn('"run_control.pause"', text)
        self.assertIn('external_pause_request', text)
        self.assertIn('pause_after_current', text)
        self.assertIn('run_status = "paused"', text)
        self.assertIn('runner_max_runtime', text)
        self.assertIn('self.campaign_restart_required', text)


if __name__ == "__main__":
    unittest.main()
