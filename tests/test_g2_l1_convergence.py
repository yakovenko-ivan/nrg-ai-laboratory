"""Offline contract tests; synthetic outputs are NOT scientific validation."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "agent_workspace" / "g2_l1_convergence.py"
spec = importlib.util.spec_from_file_location("g2_l1_convergence", MODULE)
g2 = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(g2)

SPECIES = ("H2", "O2", "N2", "O", "H", "OH", "H2O", "HO2", "H2O2", "AR", "HE")
FIXTURE = b"""&g2_l1_input_nml
    mechanism_file    = '../chemical_mechanisms/hydrogen/KEROMNES.txt'
    thermo_file       = '../thermophysical_data/KEROMNES_THERMO.txt'
    transport_file    = '../thermophysical_data/KEROMNES_TRANSDATA.txt'
    backend           = 'slatec'
    eps               = 1.0e-9
    ewt               = 1.0e-4
    max_internal_step = 1.0e-7
    max_steps         = 10000
    initial_temperature_K = 1500.0
    initial_pressure_Pa   = 101325.0
    cell_length_m    = 1.0e-3
    delta_t_s        = 1.0e-8
    n_steps          = 100
    n_composition_entries = 3
    composition(1)%name = 'H2'
    composition(1)%mole_fraction = 0.2957704821
    composition(2)%name = 'O2'
    composition(2)%mole_fraction = 0.1478852411
    composition(3)%name = 'N2'
    composition(3)%mole_fraction = 0.5563442768
    neg_test         = 'none'
    state_id         = 'M1_S1'
/
"""


class G2L1ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lab = Path(self.tmp.name) / "lab"
        (self.lab / "config").mkdir(parents=True)
        (self.lab / ".local" / "g2_l1_runs").mkdir(parents=True)
        (self.lab / "agent_workspace" / "studies").mkdir(parents=True)
        self.source = Path(self.tmp.name) / "NRG"
        self.source.mkdir()
        self.binary = Path(self.tmp.name) / "g2_l1_driver"
        self.binary.write_bytes(b"synthetic binary - never executed")
        self.archive = Path(self.tmp.name) / "gateb.tar.gz"
        self.resources = {
            rel: (rel + " synthetic").encode("utf-8") for rel in g2.RESOURCES
        }
        with tarfile.open(self.archive, "w:gz") as tf:
            for rel, data in {"g2_l1_driver_input.nml": FIXTURE,
                              **{f"task_setup/{k}": v for k,v in self.resources.items()}}.items():
                member = tarfile.TarInfo("primary_100_steps/" + rel)
                member.size = len(data)
                tf.addfile(member, io.BytesIO(data))
        self.run_root = self.lab / ".local" / "g2_l1_runs" / "r1"
        self.export = self.lab / "agent_workspace" / "studies" / "G2_L1_pilot_r1"
        self.auth = self.lab / "config" / "g2_l1_authorization.toml"
        self.auth.write_text(f'''[g2_l1]
schema_version = 1
nrg_source_root = "{self.source}"
binary_path = "{self.binary}"
gate_b_archive = "{self.archive}"
run_root = "{self.run_root}"
export_dir = "{self.export}"
per_case_timeout_s = 15
''')
        self.patches = [
            mock.patch.dict("os.environ", {"OMP_NUM_THREADS": "1"}),
            mock.patch.object(g2, "_git_source_hashes", lambda root: None),
            mock.patch.object(g2, "_check_dynamic_loader", lambda binary: None),
            mock.patch.object(g2, "BINARY_SHA256", g2._file_hash(self.binary)),
            mock.patch.object(g2, "GATE_B_SHA256", g2._file_hash(self.archive)),
            mock.patch.object(g2, "RESOURCES", {k: g2._sha(v) for k,v in self.resources.items()}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def fake_driver(self, argv, label, timeout, log_path, cwd):
        self.assertEqual(argv, [str(self.binary), "g2_l1_driver_input.nml"])
        self.assertLessEqual(timeout, 25)
        self.assertTrue((cwd / "task_setup" / "chemical_mechanisms" / "hydrogen" / "KEROMNES.txt").is_file())
        case = next(x for x in g2._cases() if cwd.name == x["name"])
        log_path.write_text(f"# {label}\n# result: EXIT_0\n== STDOUT ==\nsynthetic\n")
        (cwd / "g2_l1_run_header.txt").write_text("synthetic header\n")
        (cwd / "g2_l1_summary.csv").write_text(
            f"state_id,verdict\n{case['name']},G2_A1_NOT_ASSESSED_STAGE_0\n")
        cols = ["schema_version", "state_id", "backend", "eps", "delta_t_s", "T_K", "step_index", "t_s", "failure_code",
                "mass_closure_defect", "mass_closure_tolerance"] + [f"Y_{s}" for s in SPECIES] + [f"c_molm3_{s}" for s in SPECIES]
        with (cwd / "g2_l1_points.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=cols)
            writer.writeheader()
            for i in range(1, case["n_steps"] + 1):
                y = [0.028, 0.226, 0.746] + [0.0]*8
                c = [0.01 * (j+1) + i*case["dt"] for j in range(11)]
                writer.writerow(dict(schema_version="g2-l1-driver/2", state_id=case["name"],
                                     backend="slatec", eps=case["eps"], delta_t_s=case["dt"],
                                     T_K=1500.0, step_index=i, t_s=f"{i*case['dt']:.17e}",
                                     failure_code="OK", mass_closure_defect=0.0,
                                     mass_closure_tolerance=1e-8,
                                     **{f"Y_{s}": y[j] for j,s in enumerate(SPECIES)},
                                     **{f"c_molm3_{s}": c[j] for j,s in enumerate(SPECIES)}))
        return ""

    def test_readonly_preflight_and_exact_grid(self):
        p = g2.plan(self.lab)
        self.assertTrue(p["can_run"], p)
        self.assertEqual(len(p["cases"]), 6)
        self.assertEqual({x["n_steps"] for x in p["cases"]}, {100, 200})
        self.assertFalse(self.run_root.exists())
        self.assertFalse(self.export.exists())

    def test_missing_authorization_fails(self):
        self.auth.unlink()
        self.assertFalse(g2.plan(self.lab)["can_run"])

    def test_missing_openmp_setting_fails(self):
        import os
        with mock.patch.dict(os.environ, {"OMP_NUM_THREADS": "2"}):
            self.assertFalse(g2.plan(self.lab)["can_run"])

    def test_modified_binary_fails(self):
        self.binary.write_bytes(b"tampered")
        self.assertFalse(g2.plan(self.lab)["can_run"])

    def test_wrong_archive_hash_fails(self):
        self.archive.write_bytes(self.archive.read_bytes() + b"x")
        self.assertFalse(g2.plan(self.lab)["can_run"])

    def test_reused_run_root_fails(self):
        self.run_root.mkdir()
        self.assertFalse(g2.plan(self.lab)["can_run"])

    def test_output_outside_studies_fails(self):
        self.auth.write_text(self.auth.read_text().replace(str(self.export), str(Path(self.tmp.name)/"outside")))
        self.assertFalse(g2.plan(self.lab)["can_run"])

    def test_bad_plan_binding_makes_no_dirs(self):
        with self.assertRaisesRegex(RuntimeError, "plan-to-execute"):
            g2.execute(self.lab, "0"*64, self.fake_driver)
        self.assertFalse(self.run_root.exists())

    def test_six_case_mock_integration_evidence(self):
        plan = g2.plan(self.lab)
        self.assertTrue(plan["can_run"], plan)
        rec = g2.execute(self.lab, plan["plan_sha256"], self.fake_driver)
        self.assertEqual(rec["status"], "COMPLETED_REVIEW_REQUIRED", rec.get("stop_reason"))
        self.assertEqual(len(rec["cases"]), 6)
        self.assertEqual(len(rec["comparison"]["same_dt_vs_tightest_eps_linf_molm3"]), 6)
        self.assertEqual(len(rec["comparison"]["same_eps_coarse_vs_fine_dt_linf_molm3"]), 3)
        self.assertEqual(rec["comparison"]["reference_status"], "NOT_ASSESSED_CANTERA")
        archive = self.export / "G2_L1_Evidence.tar.gz"
        self.assertEqual(g2._file_hash(archive), rec["evidence"]["sha256"])
        with tarfile.open(archive, "r:gz") as tf:
            self.assertIsNotNone(tf.getmember("EXECUTION_RECORD.json"))
            self.assertIsNotNone(tf.getmember("SHA256SUMS.txt"))
        self.assertFalse((self.lab / "runs").exists())

    def test_unexpected_output_stops_and_preserves(self):
        p = g2.plan(self.lab)
        def unexpected(*args, **kwargs):
            self.fake_driver(*args, **kwargs)
            (kwargs["cwd"] / "unexpected.txt").write_text("must stop")
        rec = g2.execute(self.lab, p["plan_sha256"], unexpected)
        self.assertEqual(rec["status"], "STOPPED")
        self.assertEqual(len(rec["cases"]), 1)
        self.assertTrue((self.export / "G2_L1_Evidence.tar.gz").is_file())

    def test_authorization_tamper_invalidates_plan(self):
        p = g2.plan(self.lab)
        self.auth.write_text(self.auth.read_text().replace("per_case_timeout_s = 15", "per_case_timeout_s = 16"))
        with self.assertRaisesRegex(RuntimeError, "plan-to-execute"):
            g2.execute(self.lab, p["plan_sha256"], self.fake_driver)


if __name__ == "__main__":
    unittest.main()
