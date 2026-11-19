"""Stage-2 offline contract tests. Synthetic reference and driver: NOT scientific evidence."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest import mock

from agent_workspace import g2_l1_reactivity as stage2
from agent_workspace import g2_l1_convergence as base

FIXTURE = b"""&g2_l1_input_nml
    mechanism_file    = '../chemical_mechanisms/hydrogen/KEROMNES.txt'
    thermo_file       = '../thermophysical_data/KEROMNES_THERMO.txt'
    transport_file    = '../thermophysical_data/KEROMNES_TRANSDATA.txt'
    backend           = 'slatec'
    eps               = 1.0e-9
    ewt               = 1.0e-4
    delta_t_s        = 1.0e-8
    n_steps          = 100
    initial_temperature_K = 1500.0
    initial_pressure_Pa = 101325.0
    n_composition_entries = 3
    composition(1)%name = 'H2'
    composition(1)%mole_fraction = 0.2957704821
    composition(2)%name = 'O2'
    composition(2)%mole_fraction = 0.1478852411
    composition(3)%name = 'N2'
    composition(3)%mole_fraction = 0.5563442768
    state_id = 'M1_S1'
    neg_test = 'none'
/
"""

class Stage2ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/"lab"
        self.run = self.root/".local"/"g2_l1_runs"/"reactivity_r1"
        self.run.parent.mkdir(parents=True)
        self.export = self.root/"agent_workspace"/"studies"/"pilot_r1"
        self.export.parent.mkdir(parents=True)
        (self.root/"config").mkdir()
        self.binary = self.root/"bin"
        self.binary.write_bytes(b"synthetic; never executed")
        self.archive=self.root/"gate-b.tar.gz"
        header = "initial_density_kgm3:  1.69911488395184662E-001\n"
        for i,sp in enumerate(stage2.SPECIES,1):
            y= [0.02851108737535357,0.22626432890501277,0.7452245837196336]+[0.0]*8
            header+=f" {i} {sp} W_kg_per_mol= 1.000E-002 Y0= {y[i-1]:.17e}\n"
        with tarfile.open(self.archive,"w:gz") as tf:
            import io
            member=tarfile.TarInfo("primary_100_steps/g2_l1_run_header.txt")
            data=header.encode();member.size=len(data);tf.addfile(member,io.BytesIO(data))
        self.resources={k:("resource:"+k).encode() for k in base.RESOURCES}
        self.plan={"plan_sha256":"a"*64,"case_timeout_s":10,"reference_timeout_s":90}
        self.checked={"plan":self.plan,"run_root":self.run,"export":self.export,
                      "binary":self.binary,"fixture":FIXTURE,"resources":self.resources}
        auth = self.root/"config"/"g2_l1_authorization.toml"
        auth.write_text(f'''[g2_l1]
schema_version = 1
nrg_source_root = "{self.root}"
binary_path = "{self.binary}"
gate_b_archive = "{self.archive}"
run_root = "{self.root}/old-run"
export_dir = "{self.root}/old-export"
per_case_timeout_s = 25
''')
        self.patcher=mock.patch.object(stage2,"_context",return_value=self.checked)
        self.patcher.start();self.addCleanup(self.patcher.stop)
        self.pinned=mock.patch.object(base,"RESOURCES",{k:base._sha(v) for k,v in self.resources.items()})
        self.pinned.start();self.addCleanup(self.pinned.stop)
        self.binpin=mock.patch.object(base,"BINARY_SHA256",base._file_hash(self.binary))
        self.binpin.start();self.addCleanup(self.binpin.stop)
        self.chosen=5e-6
        self.ndrivers=0

    def ref_fixture(self, ref:Path, chosen:float|None):
        signals={"5.000000000e-06":.02 if chosen==5e-6 else .002,
                 "2.000000000e-05":.03 if chosen==20e-6 else (.002 if chosen is None else .02)}
        ladder=[]
        if chosen is not None:
            n=round(chosen/min(stage2.DT_GRID))
            for k,tol in enumerate(stage2.REF_LADDER,1):
                file=ref/f"reference_rung_{k}.csv"
                with file.open("w",newline="") as f:
                    cols=["t_s"]+[f"c_molm3_{sp}" for sp in stage2.SPECIES]
                    writer=csv.DictWriter(f,cols);writer.writeheader()
                    for i in range(1,n+1):
                        writer.writerow({"t_s":i*min(stage2.DT_GRID),**{
                            f"c_molm3_{sp}":(1.0 if sp=="H2" else 0.1) + (i*1e-9) + k*1e-12
                            for sp in stage2.SPECIES}})
                ladder.append({"tolerances":list(tol),"rows":n,"file_sha256":base._file_hash(file)})
        (ref/"reference_selection.json").write_text(json.dumps({
            "schema":stage2.SCHEMA,"cantera_version":stage2.CAN_VERSION,
            "signals":signals,"chosen_horizon_s":chosen,"reference_ladder":ladder,
            "reference_spread_molm3":{"rung1_vs_rung2":1e-12,"rung2_vs_rung3":1e-12},
            "reaction_signal_max_molm3": .05 if chosen else None}))

    def fake_bounded(self,argv,label,timeout,log_path,cwd):
        self.assertLessEqual(timeout,120)
        self.assertFalse(log_path.exists())
        log_path.write_text(f"# {label}\n# result: EXIT_0\n== STDOUT ==\n")
        if argv[1:3]==["-m","agent_workspace.g2_l1_reactivity"]:
            self.assertIn("--reference",argv)
            self.assertEqual(cwd,self.root)
            self.ref_fixture(Path(argv[-1]),self.chosen)
            return ""
        self.ndrivers+=1
        self.assertEqual(argv,[str(self.binary),"g2_l1_driver_input.nml"])
        self.assertTrue((cwd/"task_setup"/"chemical_mechanisms"/"hydrogen"/"KEROMNES.txt").exists())
        case=next(c for c in stage2.case_grid(self.chosen) if c["name"]==cwd.name)
        (cwd/"g2_l1_run_header.txt").write_text("synthetic header\n")
        (cwd/"g2_l1_summary.csv").write_text(f"state_id,verdict\n{case['name']},G2_A1_NOT_ASSESSED_STAGE_0\n")
        cols=["schema_version","state_id","backend","eps","delta_t_s","T_K","step_index","t_s",
              "failure_code","mass_closure_defect","mass_closure_tolerance"]+[
              f"Y_{s}" for s in stage2.SPECIES]+[f"c_molm3_{s}" for s in stage2.SPECIES]
        with (cwd/"g2_l1_points.csv").open("w",newline="") as f:
            writer=csv.DictWriter(f,cols);writer.writeheader()
            for i in range(1,case["n_steps"]+1):
                Y=[.0285,.2262,.7453]+[0.0]*8
                writer.writerow({"schema_version":"g2-l1-driver/2","state_id":case["name"],
                  "backend":"slatec","eps":case["eps"],"delta_t_s":case["dt"],
                  "T_K":1500.0,"step_index":i,"t_s":i*case["dt"],"failure_code":"OK",
                  "mass_closure_defect":0.0,"mass_closure_tolerance":1e-7,
                  **{f"Y_{s}":Y[k] for k,s in enumerate(stage2.SPECIES)},
                  **{f"c_molm3_{s}":(1 if s=="H2" else .1)+i*case["dt"]*.2
                     for s in stage2.SPECIES}})
        return ""

    def test_grid_is_bounded(self):
        self.assertEqual(len(stage2.case_grid(5e-6)),6)
        self.assertEqual(max(c["n_steps"] for c in stage2.case_grid(20e-6)),4000)
        with self.assertRaises(RuntimeError): stage2.case_grid(50e-6)

    def test_reference_gate(self):
        self.assertEqual(stage2.select_horizon({"5.000000000e-06":.02,"2.000000000e-05":.03}),5e-6)
        self.assertEqual(stage2.select_horizon({"5.000000000e-06":.003,"2.000000000e-05":.02}),20e-6)
        self.assertIsNone(stage2.select_horizon({"5.000000000e-06":.003,"2.000000000e-05":.005}))
        with self.assertRaises(RuntimeError): stage2.select_horizon({"5.000000000e-06":math.nan})

    def test_plan_is_readonly(self):
        self.assertTrue(stage2.plan(self.root)["can_run"])
        self.assertFalse(self.run.exists())
        self.assertFalse(self.export.exists())

    def test_plan_mismatch_creates_nothing(self):
        with self.assertRaisesRegex(RuntimeError,"plan-to-execute"):
            stage2.execute(self.root,"b"*64,self.fake_bounded)
        self.assertFalse(self.run.exists())

    def test_insufficient_signal_skips_nrg(self):
        self.chosen=None
        result=stage2.execute(self.root,"a"*64,self.fake_bounded)
        self.assertEqual(result["status"],"INSUFFICIENT_REFERENCE_REACTIVITY")
        self.assertEqual(self.ndrivers,0)
        self.assertTrue((self.export/"Stage2_Evidence.tar.gz").is_file())

    def test_six_case_execution_and_evidence(self):
        result=stage2.execute(self.root,"a"*64,self.fake_bounded)
        self.assertEqual(result["status"],"COMPLETED_REVIEW_REQUIRED",result.get("stop_reason"))
        self.assertEqual(self.ndrivers,6)
        self.assertEqual(sum(c["rows"] for c in result["cases"]),4500)
        self.assertEqual(len(result["comparison"]["nrg_vs_cantera"]),6)
        self.assertEqual(base._file_hash(self.export/"Stage2_Evidence.tar.gz"),result["evidence"]["sha256"])

    def test_unexpected_output_stops_and_preserves_evidence(self):
        def unsafe(*args,**kwargs):
            self.fake_bounded(*args,**kwargs)
            if self.ndrivers:
                kwargs["cwd"].joinpath("unexpected-output.txt").write_text("unapproved")
        result=stage2.execute(self.root,"a"*64,unsafe)
        self.assertEqual(result["status"],"STOPPED")
        self.assertEqual(self.ndrivers,1)
        self.assertTrue((self.export/"Stage2_Evidence.tar.gz").is_file())

if __name__=="__main__":unittest.main()
