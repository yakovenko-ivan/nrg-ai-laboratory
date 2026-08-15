import csv
import json
from pathlib import Path
import tempfile
import unittest

from campaign_tools.campaign_runner import CampaignRunner


class PhysicalRunnerIntegrationTests(unittest.TestCase):
    def test_runner_stops_on_quasistationary_products(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
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
                "cat > reactor_history.dat <<'EOF'\n"
                "0    1000 1 101325 1.0 0.10 0.90\n"
                "50   2600 1 250000 1.0 0.001 0.999\n"
                "100  2600 1 250000 1.0 0.001 0.999\n"
                "150  2600 1 250000 1.0 0.001 0.999\n"
                "200  2600 1 250000 1.0 0.001 0.999\n"
                "EOF\n"
                "i=0\n"
                "while [ ! -f run_control.stop ] && [ $i -lt 500 ]; do\n"
                "  sleep 0.01\n"
                "  i=$((i+1))\n"
                "done\n"
                "if [ -f run_control.stop ]; then\n"
                "cat > run_control_status.inf <<'EOF'\n"
                "&run_control_status\n"
                " termination_reason = 'external_stop_request'\n"
                " final_simulation_time = 2.0e-4\n"
                " elapsed_wall_time = 0.1\n"
                " restart_checkpoint_required = .false.\n"
                "/\n"
                "EOF\n"
                "exit 0\n"
                "fi\n"
                "exit 9\n",
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
                    "test_physical": {
                        "history_file": "reactor_history.dat",
                        "check_wall_interval_s": 0.02,
                        "required_run_control_modes": ["wall_time"],
                        "activation": {
                            "mode": "any",
                            "minimum_temperature_rise_K": 200,
                            "fuel_species": "H2",
                            "minimum_fuel_consumed_fraction": 0.05,
                        },
                        "window": {"duration_s": 1.0e-4, "min_points": 3},
                        "tolerances": {
                            "relative_temperature_span": 1e-6,
                            "relative_pressure_span": 1e-6,
                            "relative_density_span": 1e-8,
                            "max_species_mass_fraction_span": 1e-8,
                            "max_sumY_error": 1e-8,
                        },
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
runner_config = "{run_config}"
""", encoding="utf-8")

            case = root / "runs" / "R000001"
            task = case / "task_setup"
            task.mkdir(parents=True)
            fingerprint = "fingerprint-1"
            (case / "setup_input.nml").write_text(
                f"&case\n case_fingerprint = '{fingerprint}'\n/\n", encoding="utf-8"
            )
            (task / "post_processor# 1.inf").write_text(
                "&post_processor\n"
                " POST_PROCESSOR_OUTPUT_FILE = 'reactor_history.dat'\n"
                " SAVE_TIME_UNITS = 'microseconds'\n"
                " OPERATIONS_NUMBER = 5\n"
                " FIELD_NAME = 'temperature'\n"
                " FIELD_NAME = 'pressure'\n"
                " FIELD_NAME = 'density'\n"
                " FIELD_NAME = 'specie_mass_fraction(H2)'\n"
                " FIELD_NAME = 'specie_mass_fraction(H2O)'\n"
                "/\n",
                encoding="utf-8",
            )

            manifest = root / "campaigns" / "cases.csv"
            with manifest.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "case_id", "case_fingerprint", "case_path", "label",
                    "run_control_config.termination_mode",
                ])
                writer.writeheader()
                writer.writerow({
                    "case_id": "R000001",
                    "case_fingerprint": fingerprint,
                    "case_path": str(case),
                    "label": "physical integration test",
                    "run_control_config.termination_mode": "wall_time",
                })

            runner = CampaignRunner(
                manifest,
                run_config,
                lab_toml,
                runner_job_id="job-physical-001",
                termination_profile_name="test_physical",
            )
            rc = runner.run()
            self.assertEqual(rc, 0)

            status = json.loads((case / "run_status.json").read_text())
            self.assertEqual(status["status"], "condition_met")
            self.assertTrue(status["physical_condition_met"])
            self.assertEqual(status["termination_condition"], "test_physical")
            self.assertEqual(status["runner_job_id"], "job-physical-001")
            self.assertIn("attempt_id", status["identifier_semantics"])
            self.assertIn("runner_job_id", status["identifier_semantics"])
            self.assertIsNotNone(status["product_state"])
            self.assertAlmostEqual(status["product_state"]["temperature_K"], 2600.0)
            self.assertAlmostEqual(status["product_state"]["density_kg_m3"], 1.0)
            self.assertTrue((case / "run_control.stop").is_file())
            self.assertTrue((case / "quasistationary_status.json").is_file())


if __name__ == "__main__":
    unittest.main()
