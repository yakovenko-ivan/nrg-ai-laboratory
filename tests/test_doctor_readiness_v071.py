"""P1C regression: doctor readiness semantics for a runtime-free installation.

Doctor must report a valid Python/repository/offline-analysis installation as
usable without external NRG, while still enforcing complete NRG readiness when
``--require-nrg`` is requested.  All fixtures are synthetic temporary
directories; no CFD, no NRG binary and no package interface is ever executed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

import agent_workspace.lab_bridge as lb
from agent_workspace import run_study
from nrg_analysis import doctor
from nrg_analysis.laboratory import Laboratory


REPO_ROOT = Path(doctor.__file__).resolve().parents[1]


def write_lab(
    root: Path,
    *,
    computing_module: bool = False,
    configure_0d: bool = True,
    interface_0d: bool = False,
    interface_1d: bool = False,
    configure_1d: bool = False,
    task_setup_configured: str = "task_setup",
    create_task_setup: bool = True,
    runner_config: bool = True,
    termination_profiles: bool = True,
) -> Path:
    """Write a synthetic laboratory configuration for diagnostics only."""

    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if runner_config:
        (config_dir / "campaign_runner.json").write_text(
            json.dumps(
                {
                    "threads": 1,
                    "max_concurrent_cases": 1,
                    "skip_statuses": ["finished", "condition_met"],
                    "rerun_failed": True,
                }
            ),
            encoding="utf-8",
        )
    if termination_profiles:
        (config_dir / "termination_profiles.json").write_text(
            json.dumps({"0d_cv_post_ignition_quasistationary_v1": {}}),
            encoding="utf-8",
        )

    module = root / "bin" / "computing_module"
    interface0 = root / "bin" / "package_interface_0d"
    interface1 = root / "bin" / "package_interface_1d"
    for path, present, payload in (
        (module, computing_module, "module"),
        (interface0, interface_0d, "interface-0d"),
        (interface1, interface_1d, "interface-1d"),
    ):
        if present:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
            path.chmod(0o755)

    if create_task_setup:
        (root / task_setup_configured).mkdir(parents=True, exist_ok=True)

    lines = [
        "[paths]",
        f'research_root = "{root}"',
        'campaign_root = "campaigns"',
        'runs_root = "runs"',
        'studies_root = "studies"',
        f'task_setup_template = "{task_setup_configured}"',
        "",
        "[runtime]",
        f'computing_module = "{module}"',
    ]
    if configure_0d:
        lines.append(f'package_interface_0d = "{interface0}"')
    if configure_1d:
        lines.append(f'package_interface_1d = "{interface1}"')
    lines.extend(
        [
            "",
            "[execution]",
            "default_threads = 1",
            'runner_config = "config/campaign_runner.json"',
            'termination_profiles = "config/termination_profiles.json"',
            "",
        ]
    )
    config = config_dir / "laboratory.toml"
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def run_doctor_cli(argv: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with mock.patch.object(sys, "argv", ["nrg-lab-doctor", *argv]):
        with contextlib.redirect_stdout(stdout):
            code = doctor.main()
    return code, json.loads(stdout.getvalue())


def bridge_runtime(config: Path) -> dict:
    lab = Laboratory.load(config, use_local=False, require_runtime=False)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = lb.cmd_lab_info(None, lab)
    assert code == 0
    return json.loads(stdout.getvalue())["runtime"]


def study_runtime(config: Path) -> dict:
    lab = Laboratory.load(config, use_local=False, require_runtime=False)
    return run_study._runtime_provenance(lab)


class DoctorReadinessV071Tests(unittest.TestCase):
    def test_fresh_repository_without_nrg_is_repository_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)

            report = doctor.collect_diagnostics(config, use_local=False)
            summary = report["summary"]
            runtime = report["runtime"]

            self.assertTrue(summary["repository_ready"])
            self.assertEqual(summary["repository_failures"], 0)
            self.assertFalse(summary["execution_ready"])
            self.assertFalse(summary["runtime_available"])
            self.assertFalse(summary["nrg_runtime_ready"])
            self.assertEqual(
                summary["preparation_ready"], {"0d": False, "1d": False}
            )

            self.assertFalse(runtime["computing_module"]["exists"])
            self.assertIsNone(runtime["computing_module"]["sha256"])
            self.assertFalse(runtime["package_interfaces"]["0d"]["exists"])
            self.assertIsNone(runtime["package_interfaces"]["0d"]["sha256"])
            self.assertTrue(runtime["task_setup"]["exists"])
            self.assertTrue(runtime["task_setup"]["named_task_setup"])
            self.assertEqual(
                runtime["unavailable"], ["computing_module", "package_interface_0d"]
            )
            self.assertEqual(runtime["unreadable"], [])

            checks = {item["name"]: item for item in report["checks"]}
            self.assertEqual(checks["computing-module"]["severity"], "optional")
            self.assertTrue(checks["task-setup-template"]["ok"])
            self.assertTrue(checks["laboratory-base-validation"]["ok"])

            # Diagnostics must not create output roots or job artifacts.
            self.assertFalse((root / "campaigns").exists())
            self.assertFalse((root / "runs").exists())
            self.assertFalse((root / "studies").exists())

            code, payload = run_doctor_cli(
                ["--laboratory", str(config), "--no-local"]
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["summary"]["repository_ready"])

    def test_require_nrg_keeps_repository_ready_but_fails_complete_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)

            report = doctor.collect_diagnostics(
                config, use_local=False, require_nrg=True
            )
            summary = report["summary"]
            checks = {item["name"]: item for item in report["checks"]}

            self.assertTrue(summary["repository_ready"])
            self.assertEqual(summary["repository_failures"], 0)
            self.assertFalse(summary["nrg_runtime_ready"])
            self.assertGreater(summary["required_failures"], 0)
            self.assertFalse(summary["full_environment_ready"])
            self.assertEqual(checks["computing-module"]["severity"], "required")
            # Base/repository validation must not inherit the NRG requirement.
            self.assertTrue(checks["laboratory-base-validation"]["ok"])
            self.assertFalse(checks["laboratory-runtime-validation"]["ok"])

            code, payload = run_doctor_cli(
                ["--laboratory", str(config), "--no-local", "--require-nrg"]
            )
            self.assertEqual(code, 2)
            self.assertTrue(payload["summary"]["repository_ready"])
            self.assertFalse(payload["summary"]["nrg_runtime_ready"])

    def test_strict_flag_returns_optional_warning_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)

            code, payload = run_doctor_cli(
                ["--laboratory", str(config), "--no-local", "--strict"]
            )
            self.assertEqual(code, 3)
            self.assertTrue(payload["summary"]["repository_ready"])
            self.assertEqual(payload["summary"]["required_failures"], 0)
            self.assertGreater(payload["summary"]["optional_warnings"], 0)

    def test_combined_flags_keep_repository_then_require_nrg_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)

            code, _ = run_doctor_cli(
                [
                    "--laboratory",
                    str(config),
                    "--no-local",
                    "--require-nrg",
                    "--strict",
                ]
            )
            self.assertEqual(code, 2)

            broken = write_lab(root / "broken", runner_config=False)
            code, payload = run_doctor_cli(
                ["--laboratory", str(broken), "--no-local", "--strict"]
            )
            self.assertEqual(code, 2)
            self.assertFalse(payload["summary"]["repository_ready"])

    def test_repository_defects_and_invalid_configuration_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            no_runner = write_lab(root / "no_runner", runner_config=False)
            report = doctor.collect_diagnostics(no_runner, use_local=False)
            checks = {item["name"]: item for item in report["checks"]}
            self.assertFalse(report["summary"]["repository_ready"])
            self.assertGreater(report["summary"]["repository_failures"], 0)
            self.assertFalse(checks["runner-config"]["ok"])

            no_profiles = write_lab(root / "no_profiles", termination_profiles=False)
            report = doctor.collect_diagnostics(no_profiles, use_local=False)
            checks = {item["name"]: item for item in report["checks"]}
            self.assertFalse(report["summary"]["repository_ready"])
            self.assertFalse(checks["termination-profiles"]["ok"])

            missing = doctor.collect_diagnostics(
                root / "config" / "absent.toml", use_local=False
            )
            self.assertFalse(missing["summary"]["repository_ready"])
            self.assertFalse(missing["summary"]["nrg_runtime_ready"])
            self.assertFalse(missing["runtime"]["execution_ready"])
            self.assertEqual(missing["runtime"]["unavailable"], ["laboratory_config"])

            code, payload = run_doctor_cli(
                ["--laboratory", str(no_runner), "--no-local"]
            )
            self.assertEqual(code, 2)
            self.assertFalse(payload["summary"]["repository_ready"])

    def test_runtime_readiness_matrix(self) -> None:
        fixtures = (
            (
                "computing-module-only",
                dict(computing_module=True, create_task_setup=False),
                {
                    "execution_ready": True,
                    "preparation_ready": {"0d": False, "1d": False},
                    "runtime_available": True,
                    "nrg_runtime_ready": False,
                    "unavailable": ["task_setup", "package_interface_0d"],
                },
            ),
            (
                "interface-with-task-setup",
                dict(interface_0d=True),
                {
                    "execution_ready": False,
                    "preparation_ready": {"0d": True, "1d": False},
                    "runtime_available": True,
                    "nrg_runtime_ready": False,
                    "unavailable": ["computing_module"],
                },
            ),
            (
                "interface-without-task-setup",
                dict(interface_0d=True, create_task_setup=False),
                {
                    "execution_ready": False,
                    "preparation_ready": {"0d": False, "1d": False},
                    "runtime_available": False,
                    "nrg_runtime_ready": False,
                    "unavailable": [
                        "computing_module",
                        "task_setup",
                    ],
                },
            ),
            (
                "misnamed-task-setup",
                dict(interface_0d=True, task_setup_configured="task_setup_v2"),
                {
                    "execution_ready": False,
                    "preparation_ready": {"0d": False, "1d": False},
                    "runtime_available": False,
                    "nrg_runtime_ready": False,
                    "unavailable": [
                        "computing_module",
                        "task_setup_name",
                    ],
                },
            ),
            (
                "complete-runtime",
                dict(computing_module=True, interface_0d=True),
                {
                    "execution_ready": True,
                    "preparation_ready": {"0d": True, "1d": False},
                    "runtime_available": True,
                    "nrg_runtime_ready": True,
                    "unavailable": [],
                },
            ),
            (
                "0d-ready-configured-1d-missing",
                dict(computing_module=True, interface_0d=True, configure_1d=True),
                {
                    "execution_ready": True,
                    "preparation_ready": {"0d": True, "1d": False},
                    "runtime_available": True,
                    "nrg_runtime_ready": False,
                    "unavailable": ["package_interface_1d"],
                },
            ),
            (
                "1d-only-complete",
                dict(
                    computing_module=True,
                    configure_0d=False,
                    configure_1d=True,
                    interface_1d=True,
                ),
                {
                    "execution_ready": True,
                    "preparation_ready": {"0d": False, "1d": True},
                    "runtime_available": True,
                    "nrg_runtime_ready": True,
                    "unavailable": [],
                },
            ),
        )
        for label, options, expected in fixtures:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    config = write_lab(root, **options)
                    report = doctor.collect_diagnostics(config, use_local=False)
                    runtime = report["runtime"]
                    for key, value in expected.items():
                        self.assertEqual(runtime[key], value, f"{label}:{key}")
                    self.assertEqual(
                        report["summary"]["execution_ready"],
                        expected["execution_ready"],
                    )
                    self.assertEqual(
                        report["summary"]["runtime_available"],
                        expected["runtime_available"],
                    )
                    self.assertEqual(
                        report["summary"]["nrg_runtime_ready"],
                        expected["nrg_runtime_ready"],
                    )
                    # 1D-only laboratories must not crash on missing 0D lookups.
                    if not options.get("configure_0d", True):
                        self.assertNotIn("0d", runtime["package_interfaces"])
                        names = {item["name"] for item in report["checks"]}
                        self.assertNotIn("package-interface-0d", names)
                        self.assertIn("package-interface-1d", names)

    def test_non_executable_component_keeps_hash_but_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, computing_module=True, interface_0d=True)
            for name in ("computing_module", "package_interface_0d"):
                (root / "bin" / name).chmod(0o644)

            report = doctor.collect_diagnostics(config, use_local=False)
            runtime = report["runtime"]

            self.assertFalse(runtime["computing_module"]["executable"])
            self.assertTrue(runtime["computing_module"]["readable"])
            self.assertEqual(
                runtime["computing_module"]["sha256"],
                hashlib.sha256(b"module").hexdigest(),
            )
            self.assertFalse(runtime["package_interfaces"]["0d"]["executable"])
            self.assertEqual(
                runtime["package_interfaces"]["0d"]["sha256"],
                hashlib.sha256(b"interface-0d").hexdigest(),
            )
            self.assertFalse(runtime["execution_ready"])
            self.assertFalse(runtime["preparation_ready"]["0d"])
            self.assertFalse(runtime["runtime_available"])
            self.assertFalse(runtime["nrg_runtime_ready"])
            self.assertIn("computing_module", runtime["unavailable"])
            self.assertIn("package_interface_0d", runtime["unavailable"])
            self.assertEqual(runtime["unreadable"], [])
            self.assertTrue(report["summary"]["repository_ready"])

    def test_unreadable_component_reports_null_hash_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, computing_module=True, interface_0d=True)

            with mock.patch.object(
                doctor, "sha256_file", side_effect=OSError("permission denied")
            ):
                report = doctor.collect_diagnostics(config, use_local=False)
            runtime = report["runtime"]

            self.assertIsNone(runtime["computing_module"]["sha256"])
            self.assertFalse(runtime["computing_module"]["readable"])
            self.assertIn("permission denied", runtime["computing_module"]["read_error"])
            self.assertIsNone(runtime["package_interfaces"]["0d"]["sha256"])
            self.assertIn(
                "computing_module", runtime["unreadable"]
            )
            self.assertIn("package_interface_0d", runtime["unreadable"])
            self.assertFalse(runtime["execution_ready"])
            self.assertFalse(runtime["runtime_available"])
            self.assertFalse(runtime["nrg_runtime_ready"])
            checks = {item["name"]: item for item in report["checks"]}
            self.assertFalse(checks["computing-module"]["ok"])
            self.assertIn("read_error", checks["computing-module"]["detail"])

    def test_parity_with_bridge_and_study_provenance(self) -> None:
        fixtures = (
            {},
            dict(interface_0d=True),
            dict(computing_module=True, create_task_setup=False),
            dict(computing_module=True, interface_0d=True),
            dict(computing_module=True, interface_0d=True, configure_1d=True),
            dict(interface_0d=True, task_setup_configured="task_setup_v2"),
            dict(
                computing_module=True,
                configure_0d=False,
                configure_1d=True,
                interface_1d=True,
            ),
        )
        for options in fixtures:
            with self.subTest(options=str(options)):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    config = write_lab(root, **options)

                    doctor_runtime = doctor.collect_diagnostics(
                        config, use_local=False
                    )["runtime"]
                    bridge = bridge_runtime(config)
                    study = study_runtime(config)

                    for key in (
                        "execution_ready",
                        "preparation_ready",
                        "runtime_available",
                        "unavailable",
                        "unreadable",
                        "task_setup",
                    ):
                        self.assertEqual(doctor_runtime[key], bridge[key], key)
                        self.assertEqual(doctor_runtime[key], study[key], key)

                    for key in ("path", "exists", "executable", "readable", "sha256"):
                        self.assertEqual(
                            doctor_runtime["computing_module"][key],
                            bridge["computing_module"][key],
                            key,
                        )
                        self.assertEqual(
                            doctor_runtime["computing_module"][key],
                            study["computing_module"][key],
                            key,
                        )
                    self.assertEqual(
                        sorted(doctor_runtime["package_interfaces"]),
                        sorted(bridge["package_interfaces"]),
                    )
                    self.assertEqual(
                        sorted(doctor_runtime["package_interfaces"]),
                        sorted(study["package_interfaces"]),
                    )
                    for name, component in bridge["package_interfaces"].items():
                        for key in (
                            "path",
                            "exists",
                            "executable",
                            "readable",
                            "sha256",
                        ):
                            self.assertEqual(
                                doctor_runtime["package_interfaces"][name][key],
                                component[key],
                            )

    def test_check_and_summary_schema_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)
            report = doctor.collect_diagnostics(config, use_local=False)

        self.assertEqual(report["schema_version"], 2)
        self.assertTrue(report["nrg_external_dependency"])
        self.assertIsNotNone(report["repository_root"])

        for item in report["checks"]:
            self.assertIn(item["severity"], {"required", "optional"})
            self.assertIsInstance(item["name"], str)
            self.assertIsInstance(item["ok"], bool)

        names = {item["name"] for item in report["checks"]}
        for expected in (
            "python>=3.11",
            "import:nrg_analysis",
            "import:agent_workspace",
            "import:campaign_tools",
            "laboratory-config-load",
            "research-root",
            "runner-config",
            "termination-profiles",
            "task-setup-template",
            "computing-module",
            "package-interface-0d",
            "laboratory-base-validation",
            "laboratory-runtime-validation",
            "campaign-workspace",
            "runs-workspace",
            "studies-workspace",
            "repository-pyproject",
            "pi-extension",
            "pi-skills",
            "no-bundled-nrg-runtime",
            "external:git",
        ):
            self.assertIn(expected, names)

        summary = report["summary"]
        for key in (
            "required_failures",
            "optional_warnings",
            "repository_failures",
            "repository_ready",
            "execution_ready",
            "preparation_ready",
            "runtime_available",
            "nrg_runtime_ready",
            "full_environment_ready",
        ):
            self.assertIn(key, summary)

        runtime = report["runtime"]
        for key in (
            "computing_module",
            "task_setup",
            "package_interfaces",
            "execution_ready",
            "preparation_ready",
            "preparation_ready_0d",
            "preparation_ready_1d",
            "runtime_available",
            "nrg_runtime_ready",
            "unavailable",
            "unreadable",
            "note",
        ):
            self.assertIn(key, runtime)


if __name__ == "__main__":
    unittest.main()
