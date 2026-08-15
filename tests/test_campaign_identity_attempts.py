import contextlib
import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from agent_workspace.lab_bridge import (
    cmd_campaign_prepare,
    physical_launch_plan,
    reset_plan,
)
from campaign_tools.campaign_append_0d import append as append_campaign, plan as append_plan
from campaign_tools.campaign_attempts import (
    effective_field_value,
    load_override_record,
    materialize_effective_setup,
    reset_case_to_generated,
)
from campaign_tools.campaign_generator_0d import (
    expand_campaign,
    create_files,
    read_campaign,
)
from campaign_tools.campaign_identity import (
    build_policy,
    identity_fingerprint,
    validate_overrides,
)
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

    computing = root / "bin" / "computing_module"
    computing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    computing.chmod(0o755)

    interface = root / "bin" / "package_interface"
    interface.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,re,shutil,sys\n"
        "setup=pathlib.Path(sys.argv[1]).resolve()\n"
        "text=setup.read_text()\n"
        "def val(name):\n"
        " m=re.search(r'(?im)^\\s*'+re.escape(name)+r'\\s*=\\s*[\\'\\\"]([^\\'\\\"]+)[\\'\\\"]',text)\n"
        " if not m: raise SystemExit('missing '+name)\n"
        " return m.group(1)\n"
        "root=pathlib.Path(val('results_root'))\n"
        "campaign=val('campaign_id')\n"
        "directory=val('case_directory')\n"
        "case=root/campaign/directory\n"
        "(case/'task_setup').mkdir(parents=True,exist_ok=True)\n"
        "shutil.copy2(setup,case/'setup_input.nml')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    interface.chmod(0o755)

    runner_config = root / "config" / "campaign_runner.json"
    runner_config.write_text(json.dumps({
        "threads": 1,
        "max_concurrent_cases": 1,
        "limit_library_threads": True,
        "poll_seconds": 0.01,
        "max_runtime_seconds": 10,
        "success_exit_codes": [0],
        "skip_statuses": ["finished", "condition_met"],
        "rerun_failed": True,
        "archive_old_monitor_files": False,
        "monitor_log_each_read": False,
        "verify_case_fingerprint": True,
        "stop_request_file": "run_control.stop",
        "run_control_status_file": "run_control_status.inf",
        "graceful_stop_timeout_seconds": 1,
        "force_kill_on_graceful_stop_timeout": True,
        "monitor_rules": [],
    }), encoding="utf-8")

    profiles = root / "config" / "termination_profiles.json"
    profiles.write_text(json.dumps({
        "schema_version": 1,
        "profiles": {
            "p": {
                "history_file": "reactor_history.dat",
                "check_wall_interval_s": 0.1,
                "required_run_control_modes": ["wall_time"],
                "activation": {
                    "mode": "any",
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
computing_module = "{computing}"
package_interface_0d = "{interface}"

[execution]
default_threads = 1
runner_config = "{runner_config}"
""", encoding="utf-8")
    return Laboratory.load(lab_toml)


def campaign_toml(path: Path, name: str, temps, mechanisms, explicit_identity=False,
                  hydrogen=20.0):
    identity = """
[case_identity]
fields = [
  "mixture_config.initial_temperature",
  "physics_config.mechanism_id",
]

[attempt_overrides]
allowed = [
  "physics_config.initial_time_step",
  "physics_config.cfl_enabled",
  "physics_config.cfl_coefficient",
  "run_control_config.*",
  "output_config.*",
]
""" if explicit_identity else ""
    path.write_text(f"""
[campaign]
name = "{name}"
generator_output_directory = "{name}_generated"
max_cases = 100

{identity}
[defaults.case_config]

[defaults.reactor_config]
reactor_type = "constant_volume"
cells_number_x = 2
cell_length_x = 1.0e-4

[defaults.mixture_config]
hydrogen_mole_percent = {hydrogen}
n2_o2_molar_ratio = 3.762
initial_temperature = 1000.0
initial_pressure = 101325.0

[defaults.physics_config]
mechanism_id = "konnov"
solver_id = "cpm"
initial_time_step = 1.0e-8
cfl_enabled = false
cfl_coefficient = 0.25

[defaults.run_control_config]
termination_mode = "either"
final_time_ms = 2.0
wall_time_limit_s = 3600.0
wall_time_reserve_s = 60.0

[defaults.output_config]
postprocess_interval_us = 0.1
field_save_interval_us = 1000.0
checkpoint_interval_us = 250.0
save_spatial_fields = false

[sweep.mixture_config]
initial_temperature = {json.dumps(temps)}

[sweep.physics_config]
mechanism_id = {json.dumps(mechanisms)}
""", encoding="utf-8")


def generate(definition: Path, lab: Laboratory) -> Path:
    data = read_campaign(definition)
    cases = expand_campaign(data, lab)
    out = create_files(data, definition, cases, False, lab)
    return out / "cases.csv"


def rows(cases: Path):
    with cases.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def create_runtime_cases(cases: Path):
    for row in rows(cases):
        cp = Path(row["case_path"])
        (cp / "task_setup").mkdir(parents=True)
        source = cases.parent / "_setups" / f"{row['case_id']}.nml"
        (cp / "setup_input.nml").write_text(source.read_text(), encoding="utf-8")
        (cp / "run_status.json").write_text(json.dumps({
            "status": "finished",
            "process_pid": None,
        }), encoding="utf-8")
        (cp / "reactor_history.dat").write_text("old incomplete data\n", encoding="utf-8")


class CampaignIdentityAttemptTests(unittest.TestCase):
    def test_duplicate_logical_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "duplicate.toml"
            campaign_toml(definition, "duplicate", [1000.0, 1000.0], ["konnov"])
            data = read_campaign(definition)
            with self.assertRaisesRegex(ValueError, "duplicate logical case identity"):
                build_cases = __import__(
                    "campaign_tools.campaign_generator_0d",
                    fromlist=["expand_campaign"],
                ).expand_campaign
                build_cases(data, lab)

    def test_inferred_identity_uses_design_axes_not_attempt_knobs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            definition = root / "c.toml"
            campaign_toml(definition, "c", [1000.0, 1100.0], ["konnov", "keromnes"])
            data = read_campaign(definition)
            policy = build_policy(data)
            self.assertEqual(
                policy["identity_fields"],
                [
                    "mixture_config.initial_temperature",
                    "physics_config.mechanism_id",
                ],
            )
            accepted, rejected = validate_overrides({
                "physics_config.initial_time_step": 5e-9,
                "run_control_config.termination_mode": "wall_time",
                "output_config.postprocess_interval_us": 0.02,
            }, policy)
            self.assertEqual(len(rejected), 0)
            self.assertEqual(len(accepted), 3)

            _accepted, rejected = validate_overrides({
                "mixture_config.initial_temperature": 1200.0,
            }, policy)
            self.assertEqual(len(rejected), 1)
            self.assertIn("identity", rejected[0]["reason"])

    def test_reset_same_logical_case_and_prepare_with_attempt_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "campaign.toml"
            campaign_toml(definition, "same_campaign", [1000.0, 1100.0], ["konnov", "keromnes"])
            cases = generate(definition, lab)
            create_runtime_cases(cases)

            first = rows(cases)[0]
            cid = first["case_id"]
            cp = Path(first["case_path"])
            original_fp = first["case_fingerprint"]
            other_paths = [Path(r["case_path"]) for r in rows(cases)[1:]]

            overrides = {
                "physics_config.initial_time_step": 5e-9,
                "output_config.postprocess_interval_us": 0.02,
                "run_control_config.termination_mode": "wall_time",
            }
            plan = reset_plan(cases, [cid], overrides, lab)
            self.assertTrue(plan["can_reset"])
            self.assertEqual(plan["other_cases_untouched"], 3)

            result = reset_case_to_generated(
                cases, cid, original_fp, cp, overrides,
                "test-attempt", "unit-test unresolved case"
            )
            self.assertFalse(cp.exists())
            self.assertEqual(result["target_state"], "generated")
            self.assertTrue(Path(result["attempt_history"]).is_dir())
            self.assertFalse((Path(result["attempt_history"]) / "reactor_history.dat").exists())
            for p in other_paths:
                self.assertTrue(p.is_dir())

            setup, meta = materialize_effective_setup(cases, cid)
            text = setup.read_text()
            self.assertIn("initial_time_step = 5e-09", text)
            self.assertIn("postprocess_interval_us = 0.02", text)
            self.assertIn("termination_mode = 'wall_time'", text)
            self.assertEqual(meta["case_fingerprint"], original_fp)
            self.assertNotEqual(meta["attempt_fingerprint"], meta["base_attempt_fingerprint"])
            self.assertEqual(meta["attempt_id"], "test-attempt")

            # Prepare the same campaign. Existing three cases are skipped;
            # the reset case is regenerated from the effective attempt setup.
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                rc = cmd_campaign_prepare(SimpleNamespace(cases=str(cases)), lab)
            self.assertEqual(rc, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["generated_now"], 1)
            self.assertEqual(payload["skipped_existing_matching"], 3)

            self.assertTrue(cp.is_dir())
            attempt_config = json.loads((cp / "attempt_config.json").read_text())
            self.assertEqual(attempt_config["case_fingerprint"], original_fp)
            self.assertEqual(
                attempt_config["overrides"]["run_control_config.termination_mode"],
                "wall_time",
            )
            self.assertEqual(
                effective_field_value(cases, cid, "run_control_config.termination_mode"),
                "wall_time",
            )

            # Physical run-control preflight must use the effective attempt mode,
            # not the old flattened cases.csv value ("either").
            physical = physical_launch_plan(cases, [cid], "p", lab)
            self.assertTrue(physical["can_start"])

    def test_attempt_override_type_mismatch_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "campaign.toml"
            campaign_toml(definition, "types", [1000.0], ["konnov"])
            cases = generate(definition, lab)
            create_runtime_cases(cases)
            cid = rows(cases)[0]["case_id"]
            plan = reset_plan(
                cases, [cid],
                {"physics_config.initial_time_step": "5e-9"},
                lab,
            )
            self.assertFalse(plan["can_reset"])
            reasons = [b["reason"] for b in plan["blockers"]]
            self.assertIn("attempt_override_type_or_value_error", reasons)

    def test_identity_axis_override_is_blocked_for_reset(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "campaign.toml"
            campaign_toml(definition, "blocked", [1000.0, 1100.0], ["konnov"])
            cases = generate(definition, lab)
            create_runtime_cases(cases)
            cid = rows(cases)[0]["case_id"]
            plan = reset_plan(
                cases, [cid],
                {"mixture_config.initial_temperature": 1300.0},
                lab,
            )
            self.assertFalse(plan["can_reset"])
            reasons = [b["reason"] for b in plan["blockers"]]
            self.assertIn("attempt_override_policy_violation", reasons)

    def test_append_adds_only_new_identities_and_preserves_existing_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)

            base_def = root / "base.toml"
            campaign_toml(
                base_def, "appendable", [1000.0], ["konnov"],
                explicit_identity=True,
            )
            cases = generate(base_def, lab)
            before = rows(cases)
            old_fp = before[0]["case_fingerprint"]

            revised = root / "revised.toml"
            campaign_toml(
                revised, "appendable", [1000.0, 1100.0], ["konnov", "zhang"],
                explicit_identity=True,
            )
            p = append_plan(cases, revised, lab)
            self.assertEqual(p["existing_case_count"], 1)
            self.assertEqual(p["new_case_count"], 3)
            self.assertEqual(p["already_present_count"], 1)
            self.assertEqual(p["conflict_count"], 0)

            result = append_campaign(cases, revised, lab)
            self.assertEqual(result["appended_case_count"], 3)
            after = rows(cases)
            self.assertEqual([r["case_id"] for r in after], [
                "R000001", "R000002", "R000003", "R000004"
            ])
            self.assertEqual(after[0]["case_fingerprint"], old_fp)
            self.assertTrue((cases.parent / "_revisions" / "rev001" / "revision_manifest.json").is_file())

    def test_append_rejects_changed_protected_campaign_constant(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            base_def = root / "base.toml"
            campaign_toml(
                base_def, "append_conflict", [1000.0], ["konnov"],
                explicit_identity=True, hydrogen=20.0,
            )
            cases = generate(base_def, lab)

            revised = root / "bad.toml"
            campaign_toml(
                revised, "append_conflict", [1000.0, 1100.0], ["konnov"],
                explicit_identity=True, hydrogen=18.0,
            )
            p = append_plan(cases, revised, lab)
            self.assertGreater(p["conflict_count"], 0)


if __name__ == "__main__":
    unittest.main()
