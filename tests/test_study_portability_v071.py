"""P1B regression: study authoring and offline analysis must not require NRG.

These tests use synthetic inputs and a laboratory configuration whose external
NRG executables may be absent.  They never execute CFD, never call a package
interface, and never create ``_jobs``/``_preparation`` artifacts.

Runtime readiness must stay aligned with the established P1A bridge contract
(``agent_workspace/lab_bridge.py::cmd_lab_info``): ``runtime_available`` means at
least one usable capability (execution or preparation), preparation also needs a
``task_setup`` directory, and a readable hash does not imply executable
readiness.  The final test compares both implementations on the same synthetic
fixtures so the two contracts cannot drift apart silently.
"""

from __future__ import annotations

import contextlib
import csv
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
from agent_workspace import create_study, run_study
from nrg_analysis.laboratory import Laboratory


REPO_ROOT = Path(create_study.__file__).resolve().parents[1]
TEMPLATE_CONFIG = (
    REPO_ROOT / "agent_workspace" / "studies" / "_template" / "analysis_config.toml"
)


def write_lab(
    root: Path,
    *,
    computing_module: bool = False,
    interface_0d: bool = False,
    interface_1d: bool = False,
    configure_1d: bool = False,
    task_setup_configured: str = "task_setup",
    create_task_setup: bool = True,
) -> Path:
    """Write a minimal laboratory config with opt-in runtime components.

    Every executable is optional so the same fixture can express no runtime, an
    execution-only runtime, a preparation-only runtime, and a full runtime.
    """

    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
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
        f'package_interface_0d = "{interface0}"',
    ]
    if configure_1d:
        lines.append(f'package_interface_1d = "{interface1}"')
    lines.extend(
        [
            "",
            "[execution]",
            "default_threads = 1",
            'runner_config = "config/campaign_runner.json"',
            "",
        ]
    )
    config = config_dir / "laboratory.toml"
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def write_cases(root: Path) -> tuple[Path, Path]:
    """Write one synthetic campaign manifest plus a raw history file."""

    data = root / "data"
    case_dir = data / "caseA"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "reactor_history.dat").write_text(
        'VARIABLES = "time" "T"\n0.0 300.0\n', encoding="utf-8"
    )
    cases = data / "cases.csv"
    with cases.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["case_id", "case_path", "case_fingerprint"]
        )
        writer.writeheader()
        writer.writerow(
            {"case_id": "R000001", "case_path": "caseA", "case_fingerprint": "abc"}
        )
    return cases, data


def run_entry(main, argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(sys, "argv", ["prog", *argv]):
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main()
    return code, stdout.getvalue(), stderr.getvalue()


def bridge_lab_info_runtime(lab: Laboratory) -> dict:
    """Return the P1A ``lab-info`` runtime payload for the same laboratory."""

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = lb.cmd_lab_info(None, lab)
    assert code == 0
    return json.loads(stdout.getvalue())["runtime"]


def create_offline_study(root: Path, config: Path, cases: Path, data: Path) -> Path:
    code, stdout, _ = run_entry(
        create_study.main,
        [
            "--slug",
            "offline_probe",
            "--request",
            "Synthetic offline probe",
            "--cases",
            str(cases),
            "--case-root",
            str(data),
            "--laboratory",
            str(config),
        ],
    )
    assert code == 0, "study creation failed"
    study = root / "studies" / "offline_probe"
    assert Path(stdout.strip()).resolve() == study.resolve()
    return study


class StudyPortabilityTests(unittest.TestCase):
    def test_create_study_without_nrg_propagates_slug_and_keeps_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)
            cases, data = write_cases(root)

            study = create_offline_study(root, config, cases, data)

            for name in (
                "STUDY.md",
                "analysis_config.toml",
                "analyze.py",
                "study_manifest.json",
            ):
                self.assertTrue((study / name).is_file(), name)

            generated = tomllib.loads(
                (study / "analysis_config.toml").read_text(encoding="utf-8")
            )
            template = tomllib.loads(TEMPLATE_CONFIG.read_text(encoding="utf-8"))
            self.assertEqual(generated["study"]["name"], "offline_probe")
            self.assertNotEqual(generated["study"]["name"], "template")

            expected = json.loads(json.dumps(template))
            expected["study"]["name"] = "offline_probe"
            self.assertEqual(json.loads(json.dumps(generated)), expected)

            manifest = json.loads(
                (study / "study_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["study_id"], "offline_probe")
            self.assertEqual(manifest["scientific_request"], "Synthetic offline probe")
            self.assertEqual(Path(manifest["cases_csv"]).resolve(), cases.resolve())
            self.assertEqual(Path(manifest["case_root"]).resolve(), data.resolve())
            self.assertEqual(
                Path(manifest["laboratory_config"]).resolve(), config.resolve()
            )
            self.assertEqual(
                manifest["cases_csv_sha256"],
                hashlib.sha256(cases.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                manifest["analysis_config_sha256"],
                hashlib.sha256((study / "analysis_config.toml").read_bytes()).hexdigest(),
            )
            self.assertEqual(manifest["status"], "draft")

            # Study creation is repository-only work: no CFD artifacts appear.
            self.assertFalse((root / "campaigns" / "_jobs").exists())
            self.assertEqual(list((root / "runs").iterdir()), [])

    def test_offline_synthetic_analysis_runs_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)
            cases, data = write_cases(root)
            study = create_offline_study(root, config, cases, data)

            history = data / "caseA" / "reactor_history.dat"
            bytes_before = history.read_bytes()
            stat_before = history.stat()

            code, _, stderr = run_entry(
                run_study.main,
                [str(study), "--laboratory", str(config), "--mode", "full"],
            )
            self.assertEqual(code, 0, stderr)

            provenance = json.loads(
                (study / "provenance.json").read_text(encoding="utf-8")
            )
            runtime = provenance["runtime"]
            self.assertFalse(runtime["computing_module"]["exists"])
            self.assertIsNone(runtime["computing_module"]["sha256"])
            self.assertFalse(runtime["package_interfaces"]["0d"]["exists"])
            self.assertIsNone(runtime["package_interfaces"]["0d"]["sha256"])
            self.assertFalse(runtime["execution_ready"])
            self.assertFalse(runtime["runtime_available"])
            self.assertIn("computing_module", runtime["unavailable"])
            self.assertIn("package_interface_0d", runtime["unavailable"])
            # Task setup exists and is correctly named, so it is not reported.
            self.assertTrue(runtime["task_setup"]["exists"])
            self.assertTrue(runtime["task_setup"]["named_task_setup"])
            self.assertNotIn("task_setup", runtime["unavailable"])
            self.assertIn("analysis time", runtime["provenance_note"])

            self.assertTrue(provenance["raw_data_integrity_ok"])
            self.assertEqual(provenance["raw_files_changed"], [])
            self.assertEqual(history.read_bytes(), bytes_before)
            self.assertEqual(history.stat().st_mtime_ns, stat_before.st_mtime_ns)

            summary = json.loads(
                (study / "results" / "study_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["study"]["name"], "offline_probe")
            self.assertEqual(summary["case_count"], 1)
            self.assertEqual(summary["execution_mode"], "full")

            # No campaign execution, interface call, or case preparation happened.
            self.assertFalse((root / "campaigns" / "_jobs").exists())
            self.assertFalse((root / "campaigns" / "_preparation").exists())
            self.assertEqual(
                sorted(path.name for path in (data / "caseA").iterdir()),
                ["reactor_history.dat"],
            )

    def test_missing_cases_csv_fails_clearly_not_as_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)
            with self.assertRaises(SystemExit) as context:
                run_entry(
                    create_study.main,
                    [
                        "--slug",
                        "no_cases",
                        "--request",
                        "missing cases",
                        "--cases",
                        str(root / "data" / "missing.csv"),
                        "--laboratory",
                        str(config),
                    ],
                )
            message = str(context.exception)
            self.assertIn("cases.csv not found", message)
            self.assertNotIn("runtime", message.lower())

    def test_malformed_cases_csv_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)
            cases, data = write_cases(root)
            study = create_offline_study(root, config, cases, data)

            cases.write_text("case_id\nR000001\n", encoding="utf-8")
            with self.assertRaises(ValueError) as context:
                run_entry(
                    run_study.main,
                    [str(study), "--laboratory", str(config), "--mode", "full"],
                )
            message = str(context.exception)
            self.assertIn("case_path", message)
            self.assertNotIn("runtime", message.lower())

    def test_missing_laboratory_config_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root)
            cases, data = write_cases(root)
            study = create_offline_study(root, config, cases, data)

            with self.assertRaises(FileNotFoundError) as context:
                run_entry(
                    run_study.main,
                    [
                        str(study),
                        "--laboratory",
                        str(root / "config" / "missing.toml"),
                        "--mode",
                        "full",
                    ],
                )
            self.assertIn("laboratory configuration not found", str(context.exception))

    def test_no_runtime_and_no_task_setup_reports_all_unready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, create_task_setup=False)
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            self.assertFalse(provenance["task_setup"]["exists"])
            self.assertFalse(provenance["execution_ready"])
            self.assertFalse(provenance["runtime_available"])
            self.assertEqual(
                provenance["preparation_ready"], {"0d": False, "1d": False}
            )
            self.assertFalse(provenance["preparation_ready_0d"])
            self.assertFalse(provenance["preparation_ready_1d"])
            self.assertEqual(
                provenance["unavailable"],
                ["computing_module", "task_setup", "package_interface_0d"],
            )
            self.assertIsNone(provenance["computing_module"]["sha256"])
            self.assertIsNone(provenance["package_interfaces"]["0d"]["sha256"])

    def test_preparation_ready_without_computing_module(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, interface_0d=True)
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            self.assertTrue(provenance["preparation_ready"]["0d"])
            self.assertFalse(provenance["preparation_ready"]["1d"])
            self.assertTrue(provenance["runtime_available"])
            self.assertFalse(provenance["execution_ready"])
            self.assertEqual(provenance["unavailable"], ["computing_module"])
            self.assertTrue(provenance["task_setup"]["exists"])
            self.assertTrue(provenance["task_setup"]["named_task_setup"])

    def test_computing_module_alone_is_execution_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(
                root, computing_module=True, create_task_setup=False
            )
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            self.assertTrue(provenance["execution_ready"])
            self.assertTrue(provenance["runtime_available"])
            self.assertFalse(provenance["preparation_ready"]["0d"])
            self.assertFalse(provenance["preparation_ready_0d"])
            self.assertEqual(
                provenance["unavailable"],
                ["task_setup", "package_interface_0d"],
            )

    def test_interface_without_task_setup_is_not_preparation_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, interface_0d=True, create_task_setup=False)
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            self.assertFalse(provenance["preparation_ready"]["0d"])
            self.assertFalse(provenance["runtime_available"])
            self.assertFalse(provenance["execution_ready"])
            self.assertIn("task_setup", provenance["unavailable"])

    def test_misnamed_task_setup_is_not_preparation_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(
                root,
                interface_0d=True,
                task_setup_configured="task_setup_v2",
            )
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            self.assertTrue(provenance["task_setup"]["exists"])
            self.assertFalse(provenance["task_setup"]["named_task_setup"])
            self.assertFalse(provenance["preparation_ready"]["0d"])
            self.assertFalse(provenance["runtime_available"])
            self.assertIn("task_setup_name", provenance["unavailable"])

    def test_missing_optional_1d_interface_does_not_block_0d_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, interface_0d=True, configure_1d=True)
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            self.assertTrue(provenance["preparation_ready"]["0d"])
            self.assertFalse(provenance["preparation_ready"]["1d"])
            self.assertTrue(provenance["runtime_available"])
            self.assertEqual(
                provenance["unavailable"],
                ["computing_module", "package_interface_1d"],
            )

    def test_non_executable_component_keeps_hash_but_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, computing_module=True, interface_0d=True)
            for name in ("computing_module", "package_interface_0d"):
                (root / "bin" / name).chmod(0o644)
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            # Readable but not executable: real digest, still unready.
            self.assertFalse(provenance["computing_module"]["executable"])
            self.assertTrue(provenance["computing_module"]["readable"])
            self.assertEqual(
                provenance["computing_module"]["sha256"],
                hashlib.sha256(b"module").hexdigest(),
            )
            self.assertFalse(provenance["package_interfaces"]["0d"]["executable"])
            self.assertEqual(
                provenance["package_interfaces"]["0d"]["sha256"],
                hashlib.sha256(b"interface-0d").hexdigest(),
            )
            self.assertFalse(provenance["execution_ready"])
            self.assertFalse(provenance["preparation_ready"]["0d"])
            self.assertFalse(provenance["runtime_available"])
            self.assertIn("computing_module", provenance["unavailable"])
            self.assertIn("package_interface_0d", provenance["unavailable"])
            # Not unreadable: the digest exists, so this is an executable-readiness
            # failure rather than a provenance failure.
            self.assertEqual(provenance["unreadable"], [])

    def test_unreadable_runtime_component_never_fabricates_hash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, computing_module=True, interface_0d=True)
            lab = Laboratory.load(config, require_runtime=False)

            with mock.patch.object(
                run_study, "sha256_file", side_effect=OSError("permission denied")
            ):
                provenance = run_study._runtime_provenance(lab)

            self.assertIsNone(provenance["computing_module"]["sha256"])
            self.assertFalse(provenance["computing_module"]["readable"])
            self.assertIsNotNone(provenance["computing_module"]["read_error"])
            self.assertIsNone(provenance["package_interfaces"]["0d"]["sha256"])
            self.assertFalse(provenance["runtime_available"])
            self.assertFalse(provenance["execution_ready"])
            self.assertIn("computing_module", provenance["unavailable"])
            self.assertIn("package_interface_0d", provenance["unavailable"])
            self.assertIn("computing_module", provenance["unreadable"])
            self.assertIn("package_interface_0d", provenance["unreadable"])

    def test_existing_runtime_provenance_field_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = write_lab(root, computing_module=True, interface_0d=True)
            lab = Laboratory.load(config, require_runtime=False)

            provenance = run_study._runtime_provenance(lab)

            self.assertEqual(
                provenance["computing_module"]["sha256"],
                hashlib.sha256(b"module").hexdigest(),
            )
            self.assertEqual(
                provenance["computing_module"]["path"],
                str((root / "bin" / "computing_module").resolve()),
            )
            self.assertEqual(
                provenance["package_interfaces"]["0d"]["sha256"],
                hashlib.sha256(b"interface-0d").hexdigest(),
            )
            self.assertEqual(
                provenance["package_interfaces"]["0d"]["path"],
                str((root / "bin" / "package_interface_0d").resolve()),
            )
            self.assertTrue(provenance["runtime_available"])
            self.assertTrue(provenance["execution_ready"])
            self.assertTrue(provenance["preparation_ready"]["0d"])
            self.assertEqual(provenance["unavailable"], [])
            self.assertEqual(provenance["unreadable"], [])

    def test_runtime_provenance_matches_bridge_lab_info_contract(self) -> None:
        # Six synthetic fixtures covering the documented readiness combinations.
        fixtures = (
            {"create_task_setup": False},
            {"interface_0d": True},
            {"computing_module": True, "create_task_setup": False},
            {"computing_module": True, "interface_0d": True},
            {"interface_0d": True, "configure_1d": True},
            {"interface_0d": True, "task_setup_configured": "task_setup_v2"},
        )
        for options in fixtures:
            with self.subTest(options=str(options)):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    config = write_lab(root, **options)
                    lab = Laboratory.load(config, require_runtime=False)

                    study = run_study._runtime_provenance(lab)
                    bridge = bridge_lab_info_runtime(lab)

                    self.assertEqual(
                        study["runtime_available"], bridge["runtime_available"]
                    )
                    self.assertEqual(
                        study["execution_ready"], bridge["execution_ready"]
                    )
                    self.assertEqual(
                        study["preparation_ready"], bridge["preparation_ready"]
                    )
                    self.assertEqual(study["unavailable"], bridge["unavailable"])
                    self.assertEqual(study["unreadable"], bridge["unreadable"])
                    self.assertEqual(study["task_setup"], bridge["task_setup"])
                    for key in ("path", "exists", "executable", "readable", "sha256"):
                        self.assertEqual(
                            study["computing_module"][key],
                            bridge["computing_module"][key],
                        )
                    self.assertEqual(
                        sorted(study["package_interfaces"]),
                        sorted(bridge["package_interfaces"]),
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
                                study["package_interfaces"][name][key],
                                component[key],
                            )


if __name__ == "__main__":
    unittest.main()
