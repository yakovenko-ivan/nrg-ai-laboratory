import csv
import json
from pathlib import Path
import tempfile
import unittest

from agent_workspace.lab_bridge import physical_launch_plan
from nrg_analysis.laboratory import Laboratory


def make_lab(root: Path) -> Laboratory:
    for p in (
        root / "campaigns", root / "runs", root / "studies",
        root / "resources" / "task_setup", root / "bin", root / "config",
    ):
        p.mkdir(parents=True, exist_ok=True)
    exe = root / "bin" / "computing_module"
    interface = root / "bin" / "package_interface"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interface.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755); interface.chmod(0o755)
    (root / "config" / "campaign_runner.json").write_text(json.dumps({
        "threads": 1, "max_concurrent_cases": 1,
        "skip_statuses": ["finished", "condition_met"],
    }), encoding="utf-8")
    (root / "config" / "termination_profiles.json").write_text(json.dumps({
        "schema_version": 1,
        "profiles": {
            "p": {
                "required_run_control_modes": ["wall_time"],
                "activation": {
                    "minimum_temperature_rise_K": 200,
                    "fuel_species": "H2",
                    "minimum_fuel_consumed_fraction": 0.05,
                },
                "window": {"duration_s": 1e-4, "min_points": 5},
                "tolerances": {},
            }
        },
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
computing_module = "{exe}"
package_interface_0d = "{interface}"

[execution]
default_threads = 1
runner_config = "{root / 'config' / 'campaign_runner.json'}"
""", encoding="utf-8")
    return Laboratory.load(lab_toml)


def manifest(root: Path, mode: str) -> Path:
    path = root / "campaigns" / "cases.csv"
    cp = root / "runs" / "R000001"
    (cp / "task_setup").mkdir(parents=True)
    fp = "abc"
    (cp / "setup_input.nml").write_text(f"&x\n case_fingerprint='{fp}'\n/\n", encoding="utf-8")
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["case_id", "case_fingerprint", "case_path",
                        "run_control_config.termination_mode"],
        )
        writer.writeheader()
        writer.writerow({
            "case_id": "R000001",
            "case_fingerprint": fp,
            "case_path": str(cp),
            "run_control_config.termination_mode": mode,
        })
    return path


class PhysicalRunControlContractTests(unittest.TestCase):
    def test_wall_time_campaign_is_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            plan = physical_launch_plan(manifest(root, "wall_time"), ["R000001"], "p", lab)
            self.assertTrue(plan["can_start"])

    def test_finite_simulation_time_campaign_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            plan = physical_launch_plan(manifest(root, "either"), ["R000001"], "p", lab)
            self.assertFalse(plan["can_start"])
            reasons = [x["reason"] for x in plan["blockers"]]
            self.assertIn("incompatible_run_control_mode", reasons)


if __name__ == "__main__":
    unittest.main()
