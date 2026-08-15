import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nrg_analysis.laboratory import (
    DEFAULT_CONFIG_ENV,
    LOCAL_CONFIG_ENV,
    Laboratory,
)


class LaboratoryTests(unittest.TestCase):
    def _make_runtime(self, root: Path) -> Path:
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "nrg_runtime" / "resources" / "task_setup").mkdir(parents=True, exist_ok=True)
        bin_dir = root / "nrg_runtime" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ("computing_module", "package_interface_0D_ignition_delay_campaign"):
            path = bin_dir / name
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        (root / "config" / "campaign_runner.json").write_text("{}\n", encoding="utf-8")
        (root / "config" / "termination_profiles.json").write_text("{}\n", encoding="utf-8")
        return bin_dir

    def _write_portable_base(self, root: Path) -> Path:
        config = root / "config" / "laboratory.toml"
        config.write_text(
            '''[paths]\nresearch_root = ".."\ncampaign_root = "campaigns"\nruns_root = "runs"\nstudies_root = "agent_workspace/studies"\ntask_setup_template = "nrg_runtime/resources/task_setup"\n\n[runtime]\ncomputing_module = "nrg_runtime/bin/computing_module"\npackage_interface_0d = "nrg_runtime/bin/package_interface_0D_ignition_delay_campaign"\n\n[execution]\ndefault_threads = 2\nrunner_config = "config/campaign_runner.json"\ntermination_profiles = "config/termination_profiles.json"\n''',
            encoding="utf-8",
        )
        return config

    def test_load_and_derive_interface_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = self._make_runtime(root)
            config = self._write_portable_base(root)

            lab = Laboratory.load(config)
            self.assertEqual(lab.research_root, root.resolve())
            self.assertEqual(lab.runs_root, (root / "runs").resolve())
            self.assertEqual(
                lab.package_interface_workdir,
                (root / "nrg_runtime" / "resources").resolve(),
            )
            self.assertEqual(lab.default_threads, 2)
            self.assertEqual(
                lab.package_interface_0d,
                (bin_dir / "package_interface_0D_ignition_delay_campaign").resolve(),
            )
            self.assertIsNone(lab.local_config_path)
            self.assertEqual(lab.config_sources, (config.resolve(),))

    def test_sibling_local_config_partially_overrides_portable_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_runtime(root)
            config = self._write_portable_base(root)
            external = root / "external data"
            external.mkdir()
            local = root / "config" / "laboratory.local.toml"
            local.write_text(
                '''[paths]\nruns_root = "external data/runs"\n\n[execution]\ndefault_threads = 4\n''',
                encoding="utf-8",
            )

            lab = Laboratory.load(config, require_runtime=True)
            self.assertEqual(lab.research_root, root.resolve())
            self.assertEqual(lab.runs_root, (root / "external data" / "runs").resolve())
            self.assertEqual(lab.campaign_root, (root / "campaigns").resolve())
            self.assertEqual(lab.default_threads, 4)
            self.assertEqual(lab.local_config_path, local.resolve())
            self.assertEqual(lab.config_sources, (config.resolve(), local.resolve()))

    def test_local_override_expands_environment_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_runtime(root)
            config = self._write_portable_base(root)
            external_runs = root / "large disk" / "runs"
            local = root / "config" / "laboratory.local.toml"
            local.write_text('[paths]\nruns_root = "${NRG_TEST_RUNS_ROOT}"\n', encoding="utf-8")

            with patch.dict(os.environ, {"NRG_TEST_RUNS_ROOT": str(external_runs)}, clear=False):
                lab = Laboratory.load(config)
            self.assertEqual(lab.runs_root, external_runs.resolve())

    def test_environment_can_select_external_local_override_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo with spaces"
            root.mkdir(parents=True)
            self._make_runtime(root)
            config = self._write_portable_base(root)
            overrides = Path(tmp) / "machine" / "lab.toml"
            overrides.parent.mkdir()
            overrides.write_text('[paths]\ncampaign_root = "${NRG_TEST_CAMPAIGNS}"\n', encoding="utf-8")
            external_campaigns = Path(tmp) / "campaign workspace"

            with patch.dict(
                os.environ,
                {
                    DEFAULT_CONFIG_ENV: str(config),
                    LOCAL_CONFIG_ENV: str(overrides),
                    "NRG_TEST_CAMPAIGNS": str(external_campaigns),
                },
                clear=False,
            ):
                lab = Laboratory.load()

            self.assertEqual(lab.config_path, config.resolve())
            self.assertEqual(lab.local_config_path, overrides.resolve())
            self.assertEqual(lab.campaign_root, external_campaigns.resolve())

    def test_local_research_root_is_relative_to_local_config_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            self._make_runtime(repo)
            config = self._write_portable_base(repo)

            local_dir = tmp_path / "local-config"
            local_dir.mkdir()
            external_root = local_dir / "external-root"
            self._make_runtime(external_root)
            local = local_dir / "laboratory.local.toml"
            local.write_text(
                '''[paths]\nresearch_root = "external-root"\ncampaign_root = "campaigns"\nruns_root = "runs"\nstudies_root = "studies"\ntask_setup_template = "nrg_runtime/resources/task_setup"\n\n[runtime]\ncomputing_module = "nrg_runtime/bin/computing_module"\npackage_interface_0d = "nrg_runtime/bin/package_interface_0D_ignition_delay_campaign"\n\n[execution]\nrunner_config = "config/campaign_runner.json"\ntermination_profiles = "config/termination_profiles.json"\n''',
                encoding="utf-8",
            )

            lab = Laboratory.load(config, local_path=local)
            self.assertEqual(lab.research_root, external_root.resolve())
            self.assertEqual(lab.runs_root, (external_root / "runs").resolve())

    def test_use_local_false_ignores_sibling_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_runtime(root)
            config = self._write_portable_base(root)
            (root / "config" / "laboratory.local.toml").write_text(
                '[execution]\ndefault_threads = 9\n', encoding="utf-8"
            )
            lab = Laboratory.load(config, use_local=False)
            self.assertEqual(lab.default_threads, 2)
            self.assertIsNone(lab.local_config_path)

    def test_explicit_missing_local_override_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_runtime(root)
            config = self._write_portable_base(root)
            missing = root / "missing.local.toml"
            with self.assertRaises(FileNotFoundError):
                Laboratory.load(config, local_path=missing)


if __name__ == "__main__":
    unittest.main()
