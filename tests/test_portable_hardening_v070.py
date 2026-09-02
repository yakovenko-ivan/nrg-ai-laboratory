"""Phase-1 post-v0.7.0 portable execution hardening regression tests.

Covers:
  * failure-tolerant production defaults (``rerun_failed=false``,
    ``continue_on_failure=true``) in the exported config template and the
    executor fallback, with independent explicit overrides;
  * completed-only ``completion_outcome`` / ``failed_case_count`` /
    ``failed_case_ids`` terminal metadata (never attached to non-completed
    jobs);
  * default resume policy (skip failed) and explicit ``rerun_failed=true``;
  * strict ``continue_on_failure=false`` fail-fast behavior;
  * ``status`` / ``pack-results`` consistency with the additive outcome model;
  * recursive compute-host path-budget preflight (Windows enforcement +
    non-Windows diagnostics), including ``run``/``resume`` refusal.

Tests are deterministic and require neither Windows, network access, an NRG
binary, nor an actual CFD simulation.  ``run_bundle`` is driven by a tiny fake
computing_module script; path-budget behavior is exercised through a pure
helper that accepts an explicit platform indicator and safe limit.
"""

import csv
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from campaign_tools import portable_bundle as pb
from campaign_tools import portable_executor as pe


FAKE_COMPUTING_MODULE = """\
#!/usr/bin/env python3
import os
import sys

cwd = os.getcwd()
outcome = "finished"
try:
    with open(os.path.join(cwd, "outcome.txt"), encoding="utf-8") as handle:
        outcome = handle.read().strip() or "finished"
except FileNotFoundError:
    pass

log = os.path.join(cwd, "invocations.log")
count = 0
try:
    with open(log, encoding="utf-8") as handle:
        count = int(handle.read().strip() or "0")
except (FileNotFoundError, ValueError):
    pass
with open(log, "w", encoding="utf-8") as handle:
    handle.write(str(count + 1))

if outcome == "finished":
    with open(os.path.join(cwd, "run_control_status.inf"), "w", encoding="utf-8") as handle:
        handle.write(
            "termination_reason = 'final_simulation_time'\\n"
            "restart_checkpoint_required = .false.\\n"
            "final_simulation_time = 1.0\\n"
            "elapsed_wall_time = 0.1\\n"
        )
    sys.exit(0)
sys.exit(1)
"""


def _write_fake_computing_module(path: Path) -> None:
    path.write_text(FAKE_COMPUTING_MODULE, encoding="utf-8")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IREAD)


def _minimal_config(computing_module: Path, **overrides: object) -> str:
    exec_lines = [
        'backend = "local"',
        "threads = 1",
        "poll_seconds = 0.05",
        "graceful_pause_timeout_seconds = 5.0",
        "limit_library_threads = true",
    ]
    for key, value in overrides.items():
        if isinstance(value, bool):
            exec_lines.append(f"{key} = {str(value).lower()}")
        else:
            exec_lines.append(f"{key} = {value}")
    lines = [
        "[runtime]",
        f'computing_module = "{computing_module}"',
        "require_exact_binary = false",
        "[execution]",
        *exec_lines,
        "[termination]",
        'profile_name = ""',
    ]
    return "\n".join(lines) + "\n"


class FakeLab:
    pass


def make_source(root: Path):
    lab = FakeLab()
    lab.research_root = root / "repo"
    (lab.research_root / "campaign_tools").mkdir(parents=True)
    shutil.copy2(
        Path(pb.__file__).with_name("portable_executor.py"),
        lab.research_root / "campaign_tools" / "portable_executor.py",
    )
    source_root = Path(pb.__file__).resolve().parents[1]
    (lab.research_root / "nrg_analysis").mkdir(parents=True)
    shutil.copy2(
        source_root / "nrg_analysis" / "quasistationary_core.py",
        lab.research_root / "nrg_analysis" / "quasistationary_core.py",
    )
    lab.termination_profiles = lab.research_root / "config" / "termination_profiles.json"
    lab.termination_profiles.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        source_root / "config" / "termination_profiles.json",
        lab.termination_profiles,
    )
    lab.campaign_root = lab.research_root / "campaigns"
    lab.campaign_root.mkdir()
    lab.runs_root = lab.research_root / "runs"
    lab.runs_root.mkdir()
    lab.config_path = lab.research_root / "config" / "laboratory.toml"
    lab.config_path.parent.mkdir(parents=True, exist_ok=True)
    lab.config_path.write_text("# fake\n", encoding="utf-8")
    lab.local_config_path = None
    lab.computing_module = lab.research_root / "bin" / "computing_module"
    lab.computing_module.parent.mkdir()
    lab.computing_module.write_text("fake binary placeholder", encoding="utf-8")
    lab.runtime_manifest = lab.research_root / "bin" / "runtime_manifest.json"
    lab.runtime_manifest.write_text("{}\n", encoding="utf-8")

    camp = lab.campaign_root / "camp"
    (camp / "_setups").mkdir(parents=True)
    (camp / "campaign.toml").write_text('[campaign]\nname="camp"\n', encoding="utf-8")
    rows = []
    for number in (1, 2):
        cid = f"R{number:06d}"
        rel = f"camp/{cid}"
        cp = lab.runs_root / rel
        (cp / "task_setup").mkdir(parents=True)
        (cp / "data_output").mkdir()
        (cp / "setup_input.nml").write_text(
            f"&case_config\n case_fingerprint='{cid}-fp'\n/\n",
            encoding="utf-8",
        )
        (cp / "task_setup" / "solver_setup.inf").write_text("setup\n", encoding="utf-8")
        (cp / "data_output" / "000.dat").write_text("initial\n", encoding="utf-8")
        (camp / "_setups" / f"{cid}.nml").write_text("nml\n", encoding="utf-8")
        rows.append(
            {
                "case_id": cid,
                "case_fingerprint": f"{cid}-fp",
                "case_identity_fingerprint": f"{cid}-fp",
                "generated_attempt_fingerprint": f"{cid}-att",
                "case_path": str(cp),
                "data_save_path": str(cp / "data_save"),
                "data_output_path": str(cp / "data_output"),
            }
        )
    with (camp / "cases.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (camp / "campaign_manifest.json").write_text(
        json.dumps({"generation_runs_root": str(lab.runs_root)}), encoding="utf-8"
    )
    return lab, camp / "cases.csv"


class FailureDefaultsTests(unittest.TestCase):
    def test_exported_config_template_defaults_to_failure_tolerant(self):
        data = tomllib.loads(pb.execution_config_template(None))
        execution = data["execution"]
        self.assertFalse(execution["rerun_failed"])
        self.assertTrue(execution["continue_on_failure"])

    def test_exported_bundle_writes_failure_tolerant_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, cases = make_source(root)
            out = root / "bundle"
            pb.export_bundle(cases, lab, out)
            data = tomllib.loads((out / "execution.local.toml").read_text(encoding="utf-8"))
            self.assertFalse(data["execution"]["rerun_failed"])
            self.assertTrue(data["execution"]["continue_on_failure"])

    def test_missing_config_keys_use_failure_tolerant_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "fake_module"
            _write_fake_computing_module(exe)
            config_path = Path(td) / "execution.local.toml"
            config_path.write_text(_minimal_config(exe), encoding="utf-8")
            config = pe.read_config(config_path)
            self.assertFalse(config["rerun_failed"])
            self.assertTrue(config["continue_on_failure"])

    def test_explicit_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "fake_module"
            _write_fake_computing_module(exe)
            config_path = Path(td) / "execution.local.toml"
            config_path.write_text(
                _minimal_config(exe, rerun_failed=True, continue_on_failure=False),
                encoding="utf-8",
            )
            config = pe.read_config(config_path)
            self.assertTrue(config["rerun_failed"])
            self.assertFalse(config["continue_on_failure"])


class FailureSemanticsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.root = root
        lab, cases = make_source(root)
        self.bundle = root / "bundle"
        pb.export_bundle(cases, lab, self.bundle)
        self.exe = root / "fake_module"
        _write_fake_computing_module(self.exe)
        self.config_path = self.bundle / "execution.local.toml"

    def tearDown(self):
        self._td.cleanup()

    def _set_outcome(self, case_id: str, outcome: str) -> None:
        (self.bundle / "runs" / "camp" / case_id / "outcome.txt").write_text(
            outcome, encoding="utf-8"
        )

    def _write_config(self, **overrides) -> None:
        self.config_path.write_text(_minimal_config(self.exe, **overrides), encoding="utf-8")

    def _invocations(self, case_id: str) -> int:
        log = self.bundle / "runs" / "camp" / case_id / "invocations.log"
        if not log.is_file():
            return 0
        return int(log.read_text(encoding="utf-8").strip() or "0")

    def _job(self) -> dict:
        return json.loads((self.bundle / "executor_job.json").read_text(encoding="utf-8"))

    def _run_status(self, case_id: str) -> dict:
        return json.loads(
            (self.bundle / "runs" / "camp" / case_id / "run_status.json").read_text()
        )

    def test_failed_then_finished_continues_and_preserves_failure(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config()  # production defaults
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        self.assertEqual(self._invocations("R000001"), 1)
        self.assertEqual(self._invocations("R000002"), 1)
        self.assertEqual(self._run_status("R000001")["status"], "failed")
        self.assertEqual(self._run_status("R000002")["status"], "finished")
        # Failure provenance is retained, not converted to success.
        self.assertEqual(self._run_status("R000001")["termination_condition"], "process_exit")

    def test_clean_completion_reports_completed_cleanly(self):
        self._set_outcome("R000001", "finished")
        self._set_outcome("R000002", "finished")
        self._write_config()
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        job = self._job()
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["completion_outcome"], "completed_cleanly")
        self.assertEqual(job["failed_case_count"], 0)
        self.assertEqual(job["failed_case_ids"], [])

    def test_completion_with_failures_is_deterministic(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config()
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        job = self._job()
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["completion_outcome"], "completed_with_failures")
        self.assertEqual(job["failed_case_count"], 1)
        self.assertEqual(job["failed_case_ids"], ["R000001"])

    def test_pre_existing_failed_skipped_by_default_resume_still_summarized(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config()
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        # Resume with default (rerun_failed absent/false): failed case not rerun.
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        self.assertEqual(self._invocations("R000001"), 1)
        self.assertEqual(self._invocations("R000002"), 1)
        job = self._job()
        self.assertEqual(job["completion_outcome"], "completed_with_failures")
        self.assertEqual(job["failed_case_ids"], ["R000001"])

    def test_explicit_rerun_failed_true_permits_rerun(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config()
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        self._set_outcome("R000001", "finished")
        self._write_config(rerun_failed=True)
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        self.assertEqual(self._invocations("R000001"), 2)
        job = self._job()
        self.assertEqual(job["completion_outcome"], "completed_cleanly")
        self.assertEqual(job["failed_case_ids"], [])

    def test_strict_continue_on_failure_false_preserves_fail_fast(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config(continue_on_failure=False)
        self.assertEqual(pe.run_bundle(self.bundle, self.config_path), 0)
        self.assertEqual(self._invocations("R000001"), 1)
        # Strict fail-fast: the runner stops at the first failure; R000002 not run.
        self.assertEqual(self._invocations("R000002"), 0)
        job = self._job()
        self.assertEqual(job["state"], "failed")

    def test_non_completed_job_does_not_receive_completed_only_fields(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config(continue_on_failure=False)
        pe.run_bundle(self.bundle, self.config_path)
        job = self._job()
        self.assertNotEqual(job["state"], "completed")
        self.assertNotIn("completion_outcome", job)
        self.assertNotIn("failed_case_count", job)
        self.assertNotIn("failed_case_ids", job)

    def test_status_reports_additive_completion_for_completed_job(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config()
        pe.run_bundle(self.bundle, self.config_path)
        payload = pe.status_payload(self.bundle)
        self.assertEqual(payload["executor_job"]["state"], "completed")
        self.assertEqual(payload["completion"]["completion_outcome"], "completed_with_failures")
        self.assertEqual(payload["completion"]["failed_case_count"], 1)
        self.assertEqual(payload["completion"]["failed_case_ids"], ["R000001"])

    def test_status_does_not_invent_completion_for_non_completed_job(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config(continue_on_failure=False)
        pe.run_bundle(self.bundle, self.config_path)
        payload = pe.status_payload(self.bundle)
        self.assertNotEqual(payload["executor_job"]["state"], "completed")
        self.assertEqual(payload["completion"], {})

    def test_pack_results_accepts_completed_with_failures(self):
        self._set_outcome("R000001", "failed")
        self._set_outcome("R000002", "finished")
        self._write_config()
        pe.run_bundle(self.bundle, self.config_path)
        archive = self.root / "results.zip"
        pe.pack_results(self.bundle, archive)
        import zipfile

        with zipfile.ZipFile(archive, "r") as zf:
            manifest = json.loads(zf.read("results_manifest.json"))
        self.assertEqual(manifest["executor_job"]["completion_outcome"], "completed_with_failures")
        self.assertEqual(manifest["executor_job"]["failed_case_ids"], ["R000001"])
        # Failed returned case remains failed, not rewritten as successful.
        self.assertEqual(manifest["cases"]["R000001"]["status"], "failed")
        self.assertEqual(manifest["cases"]["R000002"]["status"], "finished")


class PathBudgetTests(unittest.TestCase):
    def _make_tree(self, root: Path) -> Path:
        bundle = root / "bundle"
        case = bundle / "runs" / "camp" / "R000001"
        # Two resource files with equal-length names in the same deep directory so
        # multiple paths can exceed a budget at the same length deterministically.
        nested = case / "task_setup" / "kinetic_mechanisms" / "deeply" / "nested" / "thermo"
        nested.mkdir(parents=True)
        (nested / ("a" * 40 + ".dat")).write_text("resource\n", encoding="utf-8")
        (nested / ("b" * 40 + ".dat")).write_text("resource\n", encoding="utf-8")
        (case / "data_output").mkdir(parents=True)
        (case / "data_output" / "000.dat").write_text("init\n", encoding="utf-8")
        (case / "setup_input.nml").write_text("&case_config\n/\n", encoding="utf-8")
        return bundle

    def test_recursive_collector_includes_root_dirs_and_files(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_tree(Path(td))
            paths = {str(p) for p in pe.collect_runtime_paths(bundle)}
            root_resolved = str(bundle.resolve())
            self.assertIn(root_resolved, paths)
            self.assertIn(str((bundle / "runs").resolve()), paths)  # nested directory
            self.assertIn(
                str((bundle / "runs" / "camp" / "R000001" / "setup_input.nml").resolve()),
                paths,
            )  # nested file

    def test_deep_nested_resource_paths_participate_in_maximum(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_tree(Path(td))
            deep = (
                bundle / "runs" / "camp" / "R000001" / "task_setup"
                / "kinetic_mechanisms" / "deeply" / "nested" / "thermo"
                / ("a" * 40 + ".dat")
            ).resolve()
            audit = pe.audit_path_budget(bundle, None, "Linux", safe_limit=10_000)
            self.assertEqual(audit["max_observed_length"], len(str(deep)))
            # With a small limit the deep path appears among offending paths.
            small = pe.audit_path_budget(bundle, None, "Linux", safe_limit=len(str(deep)) - 1)
            self.assertTrue(any(item["path"] == str(deep) for item in small["offending_paths"]))

    def test_windows_below_safe_limit_passes(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_tree(Path(td))
            audit = pe.audit_path_budget(bundle, None, "Windows", safe_limit=10_000)
            self.assertTrue(audit["enforced"])
            self.assertTrue(audit["ok"])
            self.assertNotIn("recommendation", audit)

    def test_windows_above_safe_limit_fails(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_tree(Path(td))
            probe = pe.audit_path_budget(bundle, None, "Windows", safe_limit=10_000)
            safe_limit = probe["max_observed_length"] - 1
            audit = pe.audit_path_budget(bundle, None, "Windows", safe_limit=safe_limit)
            self.assertTrue(audit["enforced"])
            self.assertFalse(audit["ok"])
            self.assertEqual(audit["safe_limit"], safe_limit)
            self.assertEqual(audit["max_observed_length"], probe["max_observed_length"])
            self.assertGreater(len(audit["offending_paths"]), 0)
            self.assertIn("recommendation", audit)
            self.assertIn("shorter root", audit["recommendation"])
            # Each offending path carries its own observed length.
            for item in audit["offending_paths"]:
                self.assertEqual(item["length"], len(item["path"]))
            self.assertEqual(audit["offending_paths"][0]["length"], audit["max_observed_length"])

    def test_non_windows_long_path_is_diagnostic_not_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_tree(Path(td))
            audit = pe.audit_path_budget(bundle, None, "Linux", safe_limit=1)
            self.assertFalse(audit["enforced"])
            self.assertTrue(audit["ok"])
            self.assertGreater(audit["max_observed_length"], 0)
            # A Windows-limit exceedance alone must not produce a recommendation.
            self.assertNotIn("recommendation", audit)

    def test_multiple_offending_paths_are_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_tree(Path(td))
            probe = pe.audit_path_budget(bundle, None, "Windows", safe_limit=10_000)
            safe_limit = probe["max_observed_length"] - 1
            first = pe.audit_path_budget(bundle, None, "Windows", safe_limit=safe_limit)
            second = pe.audit_path_budget(bundle, None, "Windows", safe_limit=safe_limit)
            self.assertGreater(len(first["offending_paths"]), 1)
            self.assertEqual(first["offending_paths"], second["offending_paths"])
            lengths = [item["length"] for item in first["offending_paths"]]
            self.assertEqual(lengths, sorted(lengths, reverse=True))

    def test_validation_uses_actual_resolved_host_paths(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = self._make_tree(Path(td))
            deep = (
                bundle / "runs" / "camp" / "R000001" / "task_setup"
                / "kinetic_mechanisms" / "deeply" / "nested" / "thermo"
                / ("a" * 40 + ".dat")
            ).resolve()
            audit = pe.audit_path_budget(bundle, None, "Windows", safe_limit=len(str(deep)) - 1)
            self.assertTrue(any(item["path"] == str(deep) for item in audit["offending_paths"]))

    def _make_real_bundle(self, root: Path):
        lab, cases = make_source(root)
        bundle = root / "bundle"
        pb.export_bundle(cases, lab, bundle)
        exe = root / "fake_module"
        _write_fake_computing_module(exe)
        config_path = bundle / "execution.local.toml"
        config_path.write_text(_minimal_config(exe), encoding="utf-8")
        return bundle, config_path

    def _tiny_windows_limit(self, bundle: Path) -> int:
        probe = pe.audit_path_budget(
            bundle, None, "Windows", safe_limit=10_000
        )
        return max(1, probe["max_observed_length"] - 1)

    def test_run_refuses_to_launch_when_path_budget_fails(self):
        with tempfile.TemporaryDirectory() as td:
            bundle, config_path = self._make_real_bundle(Path(td))
            tiny = self._tiny_windows_limit(bundle)
            with patch.object(pe, "detected_platform", return_value="Windows"), \
                 patch.object(pe, "WINDOWS_PATH_SAFE_LIMIT", tiny):
                rc = pe.run_bundle(bundle, config_path)
            self.assertEqual(rc, 3)
            self.assertFalse((bundle / "executor_job.json").is_file())
            for cid in ("R000001", "R000002"):
                self.assertFalse(
                    (bundle / "runs" / "camp" / cid / "invocations.log").is_file()
                )

    def test_resume_refuses_to_launch_when_path_budget_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle, config_path = self._make_real_bundle(root)
            # First, a clean completed execution so a resume scenario exists.
            for cid in ("R000001", "R000002"):
                (bundle / "runs" / "camp" / cid / "outcome.txt").write_text("finished", encoding="utf-8")
            self.assertEqual(pe.run_bundle(bundle, config_path), 0)
            self.assertTrue((bundle / "executor_job.json").is_file())
            prior_invocations = {
                cid: self._invocations(bundle, cid) for cid in ("R000001", "R000002")
            }
            tiny = self._tiny_windows_limit(bundle)
            with patch.object(pe, "detected_platform", return_value="Windows"), \
                 patch.object(pe, "WINDOWS_PATH_SAFE_LIMIT", tiny):
                rc = pe.run_bundle(bundle, config_path)
            self.assertEqual(rc, 3)
            # No additional case execution occurred during the refused resume.
            for cid in ("R000001", "R000002"):
                self.assertEqual(self._invocations(bundle, cid), prior_invocations[cid])

    @staticmethod
    def _invocations(bundle: Path, case_id: str) -> int:
        log = bundle / "runs" / "camp" / case_id / "invocations.log"
        if not log.is_file():
            return 0
        return int(log.read_text(encoding="utf-8").strip() or "0")


if __name__ == "__main__":
    unittest.main()