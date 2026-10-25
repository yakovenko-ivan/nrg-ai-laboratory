from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import agent_workspace.lab_bridge as lb
from nrg_analysis.laboratory import Laboratory


# Anchor on the imported trusted module, not on this file's location: the file
# may live in the ignored study workspace, while the runtime/config artifacts it
# reads belong to the actual checked-out laboratory.
REPO_ROOT = Path(lb.__file__).resolve().parents[1]


def run_bridge(argv: list[str]) -> tuple[int, dict]:
    stdout = io.StringIO()
    with mock.patch.object(sys, "argv", ["lab_bridge", *argv]):
        with contextlib.redirect_stdout(stdout):
            code = lb.main()
    return code, json.loads(stdout.getvalue())


def _write_lab(
    root: Path,
    *,
    computing_module: bool = False,
    task_setup: bool = False,
    interface_0d: bool = False,
    interface_1d: bool = False,
    configure_1d: bool = False,
) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    if task_setup:
        (root / "task_setup").mkdir(parents=True, exist_ok=True)

    module = root / "bin" / "computing_module"
    interface0 = root / "bin" / "package_interface_0d"
    interface1 = root / "bin" / "package_interface_1d"
    for path, present, content in (
        (module, computing_module, "module"),
        (interface0, interface_0d, "interface-0d"),
        (interface1, interface_1d, "interface-1d"),
    ):
        if present:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)

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
    (config_dir / "termination_profiles.json").write_text(
        (REPO_ROOT / "config" / "termination_profiles.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    lines = [
        "[paths]",
        f'research_root = "{root}"',
        'campaign_root = "campaigns"',
        'runs_root = "runs"',
        'studies_root = "studies"',
        'task_setup_template = "task_setup"',
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
            'termination_profiles = "config/termination_profiles.json"',
            "",
        ]
    )
    config = config_dir / "laboratory.toml"
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def _write_campaign(root: Path) -> Path:
    campaign_dir = root / "campaigns" / "casegen"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "campaign.toml").write_text(
        "\n".join(
            [
                "[campaign]",
                'name = "casegen"',
                "",
                "[defaults.reactor_config]",
                'reactor_type = "constant_volume"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (campaign_dir / "_setups").mkdir()
    cases = campaign_dir / "cases.csv"
    with cases.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id",
                "case_fingerprint",
                "case_path",
                "run_control_config.termination_mode",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "case_id": "R000001",
                "case_fingerprint": "abc",
                "case_path": "runs/R000001",
                "run_control_config.termination_mode": "wall_time",
            }
        )
    return cases


class BridgeRuntimeBoundaryTests(unittest.TestCase):
    def test_empty_workspace_discovery_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(root)

            code, payload = run_bridge(
                ["--laboratory", str(config), "lab-info"]
            )
            self.assertEqual(code, 0)
            self.assertIn("laboratory", payload)
            runtime = payload["runtime"]
            self.assertIsNone(runtime["computing_module_sha256"])
            self.assertIsNone(runtime["package_interface_0d_sha256"])
            self.assertFalse(runtime["execution_ready"])
            self.assertFalse(runtime["preparation_ready"]["0d"])
            self.assertFalse(runtime["runtime_available"])
            self.assertIn("computing_module", runtime["unavailable"])
            self.assertIn("package_interface_0d", runtime["unavailable"])

            code, payload = run_bridge(
                ["--laboratory", str(config), "campaign-list"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["campaigns"], [])
            self.assertEqual(payload["count"], 0)

            code, payload = run_bridge(
                ["--laboratory", str(config), "study-list"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["studies"], [])
            self.assertEqual(payload["count"], 0)

            self.assertFalse((root / "campaigns").exists())
            self.assertFalse((root / "runs").exists())
            self.assertFalse((root / "studies").exists())

    def test_missing_base_configuration_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code, payload = run_bridge(
                ["--laboratory", str(root / "missing.toml"), "lab-info"]
            )
            self.assertEqual(code, 2)
            self.assertIn("FileNotFoundError", payload["error"])

    def test_no_path_inference_for_computing_module(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(root)
            fake_bin = root / "path-bin"
            fake_bin.mkdir()
            candidate = fake_bin / "computing_module"
            candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            candidate.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(fake_bin)}):
                code, payload = run_bridge(
                    ["--laboratory", str(config), "lab-info"]
                )
            self.assertEqual(code, 0)
            self.assertFalse(payload["runtime"]["execution_ready"])
            self.assertIsNone(payload["runtime"]["computing_module_sha256"])

    def test_partial_runtime_readiness_is_not_globally_gated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(root, task_setup=True, interface_0d=True)

            code, payload = run_bridge(
                ["--laboratory", str(config), "lab-info"]
            )
            self.assertEqual(code, 0)
            runtime = payload["runtime"]
            self.assertTrue(runtime["preparation_ready"]["0d"])
            self.assertFalse(runtime["preparation_ready"]["1d"])
            self.assertFalse(runtime["execution_ready"])
            self.assertTrue(runtime["runtime_available"])
            self.assertIn("computing_module", runtime["unavailable"])
            self.assertNotIn("package_interface_0d", runtime["unavailable"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(
                root,
                task_setup=True,
                interface_0d=True,
                configure_1d=True,
            )
            code, payload = run_bridge(
                ["--laboratory", str(config), "lab-info"]
            )
            self.assertEqual(code, 0)
            runtime = payload["runtime"]
            self.assertTrue(runtime["preparation_ready"]["0d"])
            self.assertFalse(runtime["preparation_ready"]["1d"])
            self.assertIn("package_interface_1d", runtime["unavailable"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(
                root,
                computing_module=True,
                interface_0d=True,
            )
            code, payload = run_bridge(
                ["--laboratory", str(config), "lab-info"]
            )
            self.assertEqual(code, 0)
            runtime = payload["runtime"]
            self.assertFalse(runtime["preparation_ready"]["0d"])
            self.assertIn("task_setup", runtime["unavailable"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(
                root,
                task_setup=True,
                interface_1d=True,
                configure_1d=True,
            )
            code, payload = run_bridge(
                ["--laboratory", str(config), "lab-info"]
            )
            self.assertEqual(code, 0)
            runtime = payload["runtime"]
            self.assertFalse(runtime["preparation_ready"]["0d"])
            self.assertTrue(runtime["preparation_ready"]["1d"])
            self.assertIn("package_interface_0d", runtime["unavailable"])

    def test_valid_runtime_preserves_hashes_and_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(
                root,
                computing_module=True,
                task_setup=True,
                interface_0d=True,
            )
            code, payload = run_bridge(
                ["--laboratory", str(config), "lab-info"]
            )
            self.assertEqual(code, 0)
            runtime = payload["runtime"]
            self.assertEqual(
                runtime["computing_module_sha256"],
                hashlib.sha256(b"module").hexdigest(),
            )
            self.assertEqual(
                runtime["package_interface_0d_sha256"],
                hashlib.sha256(b"interface-0d").hexdigest(),
            )
            self.assertTrue(runtime["execution_ready"])
            self.assertTrue(runtime["preparation_ready"]["0d"])
            self.assertTrue(runtime["runtime_available"])
            self.assertEqual(runtime["unavailable"], [])

    def test_preparation_gate_does_not_require_computing_module(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(root, task_setup=True, interface_0d=True)
            lab = Laboratory.load(config, require_runtime=False)
            self.assertIsNone(
                lb._preparation_runtime_error(lab, lab.package_interface_0d)
            )

    def test_prepare_fails_closed_before_preparation_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(root, task_setup=True)
            cases = _write_campaign(root)

            code, payload = run_bridge(
                ["--laboratory", str(config), "campaign-prepare", "--cases", str(cases)]
            )
            self.assertEqual(code, 2)
            self.assertIn("preparation runtime unavailable", payload["error"])
            self.assertFalse((cases.parent / "_preparation").exists())

    def test_execution_commands_fail_closed_before_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(root, task_setup=True, interface_0d=True)
            cases = _write_campaign(root)

            code, payload = run_bridge(
                ["--laboratory", str(config), "campaign-start", "--cases", str(cases)]
            )
            self.assertEqual(code, 2)
            self.assertIn("execution runtime unavailable", payload["error"])
            self.assertFalse((root / "campaigns" / "_jobs").exists())

            code, payload = run_bridge(
                [
                    "--laboratory",
                    str(config),
                    "campaign-rerun-start",
                    "--cases",
                    str(cases),
                    "--case-id",
                    "R000001",
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("execution runtime unavailable", payload["error"])
            self.assertFalse((root / "campaigns" / "_jobs").exists())

            code, payload = run_bridge(
                [
                    "--laboratory",
                    str(config),
                    "campaign-physical-cases-plan",
                    "--cases",
                    str(cases),
                    "--profile",
                    "0d_cv_post_ignition_quasistationary_v1",
                    "--case-id",
                    "R000001",
                ]
            )
            self.assertEqual(code, 3)
            self.assertFalse(payload["can_start"])
            reasons = [item["reason"] for item in payload["blockers"]]
            self.assertIn("execution_runtime_unavailable", reasons)

            # The physical preflight creates a lock file but no CFD job.
            jobs_dir = root / "campaigns" / "_jobs"
            self.assertTrue(jobs_dir.is_dir())

            files_after_plan = {path.name for path in jobs_dir.iterdir()}
            self.assertEqual(files_after_plan, {"laboratory_runner.lock"})

            code, payload = run_bridge(
                [
                    "--laboratory",
                    str(config),
                    "campaign-physical-cases-start",
                    "--cases",
                    str(cases),
                    "--profile",
                    "0d_cv_post_ignition_quasistationary_v1",
                    "--case-id",
                    "R000001",
                ]
            )
            self.assertEqual(code, 3)
            self.assertIn("preflight failed", payload["error"])

            # The rejected start must not create additional job files.
            self.assertEqual(
                {path.name for path in jobs_dir.iterdir()},
                files_after_plan,
            )

    def test_unreadable_executable_is_not_verified_provenance(self) -> None:
        # A component that exists and is executable but cannot be read/hashed has
        # no verified provenance and must not be reported as ready.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _write_lab(
                root,
                computing_module=True,
                task_setup=True,
                interface_0d=True,
            )
            with mock.patch.object(
                lb, "sha256_file", side_effect=OSError("permission denied")
            ):
                code, payload = run_bridge(
                    ["--laboratory", str(config), "lab-info"]
                )
            self.assertEqual(code, 0)
            runtime = payload["runtime"]
            self.assertIsNone(runtime["computing_module_sha256"])
            self.assertIsNone(runtime["package_interface_0d_sha256"])
            self.assertFalse(runtime["execution_ready"])
            self.assertFalse(runtime["preparation_ready"]["0d"])
            self.assertFalse(runtime["runtime_available"])
            self.assertIn("computing_module", runtime["unavailable"])
            self.assertIn("computing_module", runtime["unreadable"])
            self.assertIn("package_interface_0d", runtime["unreadable"])
            self.assertIsNotNone(runtime["computing_module"]["read_error"])
            self.assertFalse(runtime["computing_module"]["readable"])

            lab = Laboratory.load(config, require_runtime=False)
            with mock.patch.object(
                lb, "sha256_file", side_effect=OSError("permission denied")
            ):
                error = lb._execution_runtime_error(lab)
            self.assertIsNotNone(error)
            self.assertIn("execution runtime unavailable", error["error"])

    def test_pi_ensure_lab_contract_does_not_require_runtime(self) -> None:
        source = (
            REPO_ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
        ).read_text(encoding="utf-8")
        block = source.split("async function ensureLab", 1)[1].split(
            "function toolResult", 1
        )[0]
        self.assertIn("info.laboratory", block)
        self.assertNotIn("runtime", block)
        self.assertNotIn("sha256", block)


if __name__ == "__main__":
    unittest.main()
