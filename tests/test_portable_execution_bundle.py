import csv
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from campaign_tools import portable_bundle as pb


class FakeLab:
    pass


class PortableExecutionBundleTests(unittest.TestCase):
    def make_source(self, root: Path):
        lab = FakeLab()
        lab.research_root = root / "repo"
        (lab.research_root / "campaign_tools").mkdir(parents=True)
        shutil.copy2(
            Path(pb.__file__).with_name("portable_executor.py"),
            lab.research_root / "campaign_tools" / "portable_executor.py",
        )

        # Portable physical termination is trusted bundle infrastructure.
        # Mirror the two additional Laboratory resources required by
        # portable_bundle.export_plan()/export_bundle().
        source_root = Path(pb.__file__).resolve().parents[1]
        (lab.research_root / "nrg_analysis").mkdir(parents=True)
        shutil.copy2(
            source_root / "nrg_analysis" / "quasistationary_core.py",
            lab.research_root / "nrg_analysis" / "quasistationary_core.py",
        )
        lab.termination_profiles = (
            lab.research_root / "config" / "termination_profiles.json"
        )
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

    def test_export_contract_uses_relative_case_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, cases = self.make_source(root)
            out = root / "bundle"
            result = pb.export_bundle(cases, lab, out)
            self.assertEqual(result["case_count"], 2)
            with (out / "cases.csv").open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["case_path"], "camp/R000001")
            self.assertEqual(rows[0]["case_relpath"], "camp/R000001")
            self.assertFalse(Path(rows[0]["case_path"]).is_absolute())
            manifest = json.loads((out / "bundle_manifest.json").read_text())
            self.assertFalse(manifest["path_contract"]["absolute_source_paths_authoritative"])
            self.assertTrue((out / "portable_executor.py").is_file())
            self.assertTrue((out / "execution.local.toml").is_file())

    def test_result_archive_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab, cases = self.make_source(root)
            out = root / "bundle"
            pb.export_bundle(cases, lab, out)
            bundle_id = json.loads((out / "bundle_manifest.json").read_text())["bundle_id"]
            for cid in ("R000001", "R000002"):
                cp = out / "runs" / "camp" / cid
                (cp / "front_history.dat").write_text("1 2 3\n", encoding="utf-8")
                (cp / "run_status.json").write_text(
                    json.dumps(
                        {
                            "status": "finished",
                            "bundle_id": bundle_id,
                            "execution_origin": "portable_bundle_v1",
                            "nrg_termination_reason": "final_simulation_time",
                        }
                    ),
                    encoding="utf-8",
                )
            from campaign_tools import portable_executor as pe

            archive = root / "results.zip"
            pe.pack_results(out, archive)
            plan = pb.import_plan(cases, archive, lab)
            self.assertTrue(plan["can_import"], plan)
            result = pb.import_results(cases, archive, lab)
            self.assertTrue(result["imported"])
            self.assertTrue((lab.runs_root / "camp" / "R000001" / "front_history.dat").is_file())


class PortableToolContractTests(unittest.TestCase):
    def test_bridge_and_extension_expose_portable_tools(self):
        root = Path(__file__).resolve().parents[1]
        bridge = (root / "agent_workspace" / "lab_bridge.py").read_text(encoding="utf-8")
        extension = (root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts").read_text(encoding="utf-8")
        self.assertIn("campaign-export-bundle-plan", bridge)
        self.assertIn("campaign_generator_for_definition", bridge)
        self.assertIn("package_interface_for_generated", bridge)
        self.assertIn("campaign-import-results-plan", bridge)
        self.assertIn('name: "nrg_campaign_export_bundle"', extension)
        self.assertIn('name: "nrg_campaign_import_results"', extension)

    def test_generators_emit_relative_case_locators(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("campaign_generator_0d.py", "campaign_generator_1d_flame.py"):
            path = root / "campaign_tools" / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIn('"case_relpath": case_relpath.as_posix()', text)
            self.assertIn('"case_path": case_relpath.as_posix()', text)



if __name__ == "__main__":
    unittest.main()
