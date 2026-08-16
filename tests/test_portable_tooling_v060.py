import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from nrg_analysis.config_cli import main as config_main
from nrg_analysis.doctor import collect_diagnostics
from nrg_analysis.laboratory import Laboratory


REPO_ROOT = Path(__file__).resolve().parents[1]


class PortableToolingV060Tests(unittest.TestCase):
    def test_pyproject_packages_all_python_layers(self):
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        self.assertIn("version", project["dynamic"])
        self.assertEqual(
            data["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "nrg_analysis._version.__version__",
        )
        include = set(data["tool"]["setuptools"]["packages"]["find"]["include"])
        self.assertIn("nrg_analysis*", include)
        self.assertIn("agent_workspace*", include)
        self.assertIn("campaign_tools*", include)
        scripts = project["scripts"]
        self.assertIn("nrg-lab", scripts)
        self.assertIn("nrg-lab-config", scripts)
        self.assertIn("nrg-lab-doctor", scripts)

    def test_config_init_local_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            base = config_dir / "laboratory.toml"
            base.write_text("[paths]\n", encoding="utf-8")
            example = config_dir / "laboratory.local.toml.example"
            example.write_text("[runtime]\n", encoding="utf-8")

            argv = ["nrg-lab-config", "--laboratory", str(base), "init-local"]
            with patch.object(sys, "argv", argv):
                self.assertEqual(config_main(), 0)
            local = config_dir / "laboratory.local.toml"
            self.assertEqual(local.read_text(encoding="utf-8"), "[runtime]\n")

            with patch.object(sys, "argv", argv):
                self.assertEqual(config_main(), 2)

    def test_doctor_repository_checks_pass_without_bundled_nrg(self):
        report = collect_diagnostics(REPO_ROOT / "config" / "laboratory.toml", use_local=False)
        self.assertTrue(report["summary"]["repository_ready"])
        self.assertFalse(report["summary"]["nrg_runtime_ready"])
        names = {item["name"]: item for item in report["checks"]}
        self.assertTrue(names["import:campaign_tools"]["ok"])
        self.assertTrue(names["pi-extension"]["ok"])
        self.assertTrue(names["pi-skills"]["ok"])
        self.assertTrue(names["no-bundled-nrg-runtime"]["ok"])
        self.assertEqual(names["computing-module"]["severity"], "optional")

    def test_bootstrap_dry_run_is_location_independent(self):
        script = REPO_ROOT / "scripts" / "bootstrap.py"
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, str(script), "--dry-run", "--skip-doctor"],
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("pip install", completed.stdout)
        self.assertIn("Bootstrap complete", completed.stdout)

    def test_pi_extension_prefers_project_venv_python(self):
        text = (REPO_ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("PROJECT_VENV_PYTHON", text)
        self.assertIn("existsSync(PROJECT_VENV_PYTHON)", text)
        self.assertIn("process.env.NRG_PYTHON", text)

    def test_campaign_tools_is_regular_package(self):
        import campaign_tools

        self.assertTrue(Path(campaign_tools.__file__).is_file())


if __name__ == "__main__":
    unittest.main()
