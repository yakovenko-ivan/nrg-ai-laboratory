import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from agent_workspace.lab_bridge import cmd_campaign_job_status
from nrg_analysis.laboratory import Laboratory


def make_lab(root: Path) -> Laboratory:
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
    interface = root / "bin" / "package_interface"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interface.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    interface.chmod(0o755)

    runner_config = root / "config" / "campaign_runner.json"
    runner_config.write_text(json.dumps({
        "threads": 1,
        "max_concurrent_cases": 1,
        "skip_statuses": ["finished", "condition_met"],
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
computing_module = "{executable}"
package_interface_0d = "{interface}"

[execution]
default_threads = 1
runner_config = "{runner_config}"
""", encoding="utf-8")
    return Laboratory.load(lab_toml)


class LiveJobLockStatusTests(unittest.TestCase):
    def test_live_lock_field_overrides_stale_job_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            jobs = lab.campaign_root / "_jobs"
            jobs.mkdir(parents=True, exist_ok=True)
            job = jobs / "completed.json"
            job.write_text(json.dumps({
                "job_id": "completed",
                "state": "completed",
                "runner_pid": 999999,
                "runner_lock": {
                    "active": True,
                    "owner": {"state": "running", "runner_pid": 999999},
                },
            }), encoding="utf-8")

            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                rc = cmd_campaign_job_status(SimpleNamespace(job=str(job)), lab)
            self.assertEqual(rc, 0)
            payload = json.loads(stream.getvalue())
            self.assertTrue(payload["job"]["runner_lock"]["active"])  # historical stale snapshot retained
            self.assertFalse(payload["laboratory_runner"]["active"])  # authoritative current observation
            self.assertEqual(
                payload["runner_lock_semantics"]["authoritative_current_field"],
                "laboratory_runner",
            )


if __name__ == "__main__":
    unittest.main()
