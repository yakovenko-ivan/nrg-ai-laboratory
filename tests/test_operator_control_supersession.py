from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from agent_workspace.lab_bridge import operator_stop_plan
from campaign_tools.campaign_runner import CampaignRunner
from nrg_analysis.laboratory import Laboratory


def build_lab(root: Path):
    for p in (
        root / "campaigns",
        root / "runs",
        root / "studies",
        root / "resources" / "task_setup",
        root / "bin",
        root / "config",
    ):
        p.mkdir(parents=True, exist_ok=True)

    exe = root / "bin" / "computing_module"
    exe.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ $i -lt 100 ]; do\n"
        "  if [ -f run_control.pause ]; then\n"
        "    cat > run_control_status.inf <<'EOF'\n"
        "&run_control_status\n"
        " termination_reason = 'external_pause_request'\n"
        " final_simulation_time = 7.5e-3\n"
        " elapsed_wall_time = 0.1\n"
        " restart_checkpoint_required = .true.\n"
        "/\nEOF\n"
        "    mkdir -p data_output\n"
        "    echo restart > data_output/restart.marker\n"
        "    exit 0\n"
        "  fi\n"
        "  if [ -f run_control.stop ]; then\n"
        "    cat > run_control_status.inf <<'EOF'\n"
        "&run_control_status\n"
        " termination_reason = 'external_stop_request'\n"
        " final_simulation_time = 7.5e-3\n"
        " elapsed_wall_time = 0.1\n"
        " restart_checkpoint_required = .false.\n"
        "/\nEOF\n"
        "    exit 0\n"
        "  fi\n"
        "  sleep 0.01\n"
        "  i=$((i+1))\n"
        "done\n"
        "cat > run_control_status.inf <<'EOF'\n"
        "&run_control_status\n"
        " termination_reason = 'final_simulation_time'\n"
        " final_simulation_time = 2.0e-2\n"
        " elapsed_wall_time = 1.0\n"
        " restart_checkpoint_required = .false.\n"
        "/\nEOF\n"
        "exit 0\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)

    interface = root / "bin" / "package_interface"
    interface.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interface.chmod(0o755)

    config = root / "config" / "campaign_runner.json"
    config.write_text(json.dumps({
        "threads": 1,
        "max_concurrent_cases": 1,
        "poll_seconds": 0.01,
        "max_runtime_seconds": 5,
        "success_exit_codes": [0],
        "skip_statuses": ["finished", "condition_met"],
        "rerun_failed": True,
        "archive_old_monitor_files": False,
        "verify_case_fingerprint": False,
        "stop_request_file": "run_control.stop",
        "pause_request_file": "run_control.pause",
        "run_control_status_file": "run_control_status.inf",
        "graceful_stop_timeout_seconds": 1,
        "force_kill_on_graceful_stop_timeout": True,
        "monitor_rules": [],
    }), encoding="utf-8")

    lab_toml = root / "config" / "laboratory.toml"
    lab_toml.write_text(
        f'[paths]\nresearch_root="{root}"\n'
        f'campaign_root="{root / "campaigns"}"\n'
        f'runs_root="{root / "runs"}"\n'
        f'studies_root="{root / "studies"}"\n'
        f'task_setup_template="{root / "resources" / "task_setup"}"\n'
        f'[runtime]\ncomputing_module="{exe}"\npackage_interface_0d="{interface}"\n'
        f'[execution]\ndefault_threads=1\nrunner_config="{config}"\n',
        encoding="utf-8",
    )

    manifest = root / "campaigns" / "cases.csv"
    with manifest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "case_fingerprint", "case_path", "label"])
        w.writeheader()
        for cid in ("R000001", "R000002"):
            case = root / "runs" / cid
            (case / "task_setup").mkdir(parents=True)
            w.writerow({
                "case_id": cid,
                "case_fingerprint": f"fp-{cid}",
                "case_path": str(case),
                "label": cid,
            })

    return Laboratory.load(lab_toml), config, lab_toml, manifest


def wait_for(path: Path, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                data = json.loads(path.read_text())
            except Exception:
                data = None
            if data is not None and predicate(data):
                return data
        time.sleep(0.01)
    raise AssertionError(f"condition not reached for {path}")


class OperatorControlSupersessionTests(unittest.TestCase):
    def test_stop_plan_allows_superseding_acknowledged_after_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, config, lab_toml, manifest = build_lab(root)
            jobs = lab.campaign_root / "_jobs"
            jobs.mkdir(parents=True)
            control = jobs / "job.control.json"
            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "acknowledged",
                "request_id": "old",
                "action": "pause_after_current",
                "runner_job_id": "job",
            }), encoding="utf-8")
            job = jobs / "job.json"
            job.write_text(json.dumps({
                "job_id": "job",
                "state": "running",
                "runner_pid": os.getpid(),
                "manifest": str(manifest),
                "run_config": str(config),
                "control_file": str(control),
                "command": ["python", "campaign_runner.py", "--control-file", str(control)],
            }), encoding="utf-8")

            plan = operator_stop_plan(lab, action="stop_campaign", job_ref=str(job))
            self.assertTrue(plan["can_stop"])
            self.assertEqual(plan["superseded_control"]["request_id"], "old")

    def test_runner_stop_campaign_supersedes_after_current_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, config, lab_toml, manifest = build_lab(root)
            control = root / "campaigns" / "_jobs" / "runner.control.json"
            control.parent.mkdir(parents=True, exist_ok=True)
            runner = CampaignRunner(
                manifest, config, lab_toml,
                runner_job_id="runner", control_file=control,
            )
            result = {}
            th = threading.Thread(target=lambda: result.setdefault("rc", runner.run()), daemon=True)
            th.start()

            status_path = root / "runs" / "R000001" / "run_status.json"
            wait_for(status_path, lambda d: d.get("status") == "running")

            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "old-after",
                "action": "pause_after_current",
                "runner_job_id": "runner",
                "case_id": None,
            }), encoding="utf-8")
            wait_for(control, lambda d: d.get("state") == "acknowledged")

            control.write_text(json.dumps({
                "schema_version": 1,
                "state": "requested",
                "request_id": "new-stop",
                "action": "stop_campaign",
                "runner_job_id": "runner",
                "case_id": None,
            }), encoding="utf-8")

            th.join(timeout=5)
            self.assertFalse(th.is_alive())
            self.assertEqual(result.get("rc"), 0)

            status = json.loads(status_path.read_text())
            self.assertEqual(status["status"], "stopped")
            self.assertEqual(status["nrg_termination_reason"], "external_stop_request")
            self.assertTrue(runner.campaign_stop_requested)
            self.assertFalse(runner.campaign_pause_requested)
            self.assertFalse(runner.pause_after_current_requested)
            self.assertFalse((root / "runs" / "R000002" / "run_status.json").exists())

            observed = json.loads(control.read_text())
            self.assertEqual(observed["action"], "stop_campaign")
            self.assertEqual(observed["state"], "handled")
            self.assertEqual(observed["outcome"], "case_stopped")


if __name__ == "__main__":
    unittest.main()
