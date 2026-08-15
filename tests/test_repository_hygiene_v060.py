from pathlib import Path
import tempfile
import tomllib
import unittest

from nrg_analysis.doctor import collect_diagnostics
from nrg_analysis.laboratory import Laboratory


REPO_ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneV060Tests(unittest.TestCase):
    def test_nrg_runtime_is_not_vendored(self):
        self.assertFalse((REPO_ROOT / "nrg_runtime").exists())
        self.assertFalse((REPO_ROOT / "campaigns").exists())
        self.assertFalse((REPO_ROOT / "runs").exists())

    def test_portable_defaults_reference_ignored_external_nrg_layout(self):
        data = tomllib.loads((REPO_ROOT / "config" / "laboratory.toml").read_text(encoding="utf-8"))
        self.assertTrue(data["paths"]["task_setup_template"].startswith(".local/NRG/"))
        self.assertTrue(data["runtime"]["computing_module"].startswith(".local/NRG/"))
        self.assertTrue(data["runtime"]["package_interface_0d"].startswith(".local/NRG/"))

    def test_repository_gitignore_protects_local_research_and_nrg(self):
        text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for rule in (
            "/config/laboratory.local.toml",
            "/.local/",
            "/campaigns/",
            "/runs/",
            "/agent_workspace/studies/*",
            "!/agent_workspace/studies/_template/",
        ):
            self.assertIn(rule, text)

    def test_no_runtime_validation_allows_repository_without_external_nrg(self):
        lab = Laboratory.load(REPO_ROOT / "config" / "laboratory.toml", require_runtime=False)
        self.assertEqual(lab.research_root, REPO_ROOT.resolve())
        self.assertFalse(lab.computing_module.exists())

    def test_doctor_can_promote_external_nrg_to_required(self):
        report = collect_diagnostics(
            REPO_ROOT / "config" / "laboratory.toml",
            require_nrg=True,
        )
        names = {item["name"]: item for item in report["checks"]}
        self.assertEqual(names["computing-module"]["severity"], "required")
        self.assertFalse(report["summary"]["nrg_runtime_ready"])
        self.assertFalse(report["summary"]["repository_ready"])

    def test_no_known_developer_machine_paths_are_committed(self):
        forbidden = (
            "/" + "home" + "/cfd-agent",
            "Desktop/Work/Projects/" + "research",
            "/mnt/" + "data/",
        )
        text_suffixes = {
            ".md", ".toml", ".json", ".py", ".ts", ".txt", ".gitignore", ".gitattributes"
        }
        offenders = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", ".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
                continue
            if path.name not in {".gitignore", ".gitattributes"} and path.suffix not in text_suffixes:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for marker in forbidden:
                if marker in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {marker}")
        self.assertEqual(offenders, [])

    def test_local_config_example_does_not_define_fake_env_helpers(self):
        example = REPO_ROOT / "config" / "laboratory.local.toml.example"
        data = tomllib.loads(example.read_text(encoding="utf-8"))
        self.assertNotIn("NRG_SOURCE_ROOT", data.get("paths", {}))
        self.assertNotIn("NRG_BUILD_ROOT", data.get("runtime", {}))
        self.assertNotIn("NRG_PACKAGE_INTERFACE_0D", data.get("runtime", {}))
        text = example.read_text(encoding="utf-8")
        self.assertIn("PROCESS ENVIRONMENT VARIABLES", text)
        self.assertIn("does NOT define a variable", text)

    def test_documentation_is_outside_pi_skill_discovery(self):
        self.assertTrue((REPO_ROOT / "docs" / "repository-policy.md").is_file())
        self.assertTrue((REPO_ROOT / "docs" / "nrg-integration.md").is_file())
        self.assertFalse((REPO_ROOT / "PI_INTEGRATION.md").exists())
        self.assertFalse((REPO_ROOT / "VALIDATION.md").exists())


if __name__ == "__main__":
    unittest.main()
