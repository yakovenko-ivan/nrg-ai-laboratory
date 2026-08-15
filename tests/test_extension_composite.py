import csv
import json
from pathlib import Path
import tempfile
import unittest
import shutil

from campaign_tools.campaign_extension_0d import read_extension, build_cases, create_files
from campaign_tools.campaign_composite import build as build_composite
from campaign_tools.campaign_runner import CampaignRunner
from nrg_analysis.laboratory import Laboratory
from campaign_tools.campaign_generator_0d import compute_case_fingerprint


def make_lab(root: Path) -> Laboratory:
    for p in (
        root/"campaigns", root/"runs", root/"studies",
        root/"resources"/"task_setup", root/"bin", root/"config",
    ):
        p.mkdir(parents=True, exist_ok=True)
    exe=root/"bin"/"computing_module"; interface=root/"bin"/"package_interface"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interface.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755); interface.chmod(0o755)
    rcfg=root/"config"/"campaign_runner.json"
    rcfg.write_text(json.dumps({"threads":1,"max_concurrent_cases":1,"skip_statuses":["finished","condition_met"]}), encoding="utf-8")
    (root/"config"/"termination_profiles.json").write_text(json.dumps({"schema_version":1,"profiles":{}}), encoding="utf-8")
    lab=root/"config"/"laboratory.toml"
    lab.write_text(f"""
[paths]
research_root = "{root}"
campaign_root = "{root/'campaigns'}"
runs_root = "{root/'runs'}"
studies_root = "{root/'studies'}"
task_setup_template = "{root/'resources'/'task_setup'}"

[runtime]
computing_module = "{exe}"
package_interface_0d = "{interface}"

[execution]
default_threads = 1
runner_config = "{rcfg}"
""", encoding="utf-8")
    return Laboratory.load(lab)


def make_base(root: Path) -> Path:
    out=root/"campaigns"/"base"; setups=out/"_setups"; setups.mkdir(parents=True)
    rows=[]
    for n in range(1,4):
        cid=f"R{n:06d}"
        groups={
            "case_config":{"case_id":cid,"case_fingerprint":"","case_directory":cid,"case_label":cid,
                           "campaign_id":"base","results_root":str(root/"runs"),"numerical_variant":"default"},
            "reactor_config":{"reactor_type":"constant_volume","cells_number_x":2,"cell_length_x":1e-4},
            "mixture_config":{"hydrogen_mole_percent":20.0,"n2_o2_molar_ratio":3.762,
                              "initial_temperature":900.0+100*n,"initial_pressure":101325.0},
            "physics_config":{"mechanism_id":"konnov","solver_id":"cpm","initial_time_step":1e-8,
                              "cfl_enabled":False,"cfl_coefficient":0.25},
            "run_control_config":{"termination_mode":"either","final_time_ms":2.0,
                                  "wall_time_limit_s":3600.0,"wall_time_reserve_s":60.0},
            "output_config":{"postprocess_interval_us":0.1,"field_save_interval_us":1000.0,
                             "checkpoint_interval_us":250.0,"save_spatial_fields":False},
        }
        fp=compute_case_fingerprint(groups); groups["case_config"]["case_fingerprint"]=fp
        case_path=root/"runs"/"base"/cid
        row={"case_id":cid,"case_fingerprint":fp,"case_directory":cid,"case_path":str(case_path),
             "label":cid,"numerical_variant":"default"}
        for g,vals in groups.items():
            for k,v in vals.items(): row[f"{g}.{k}"]=v
        rows.append(row)
        (setups/f"{cid}.json").write_text(json.dumps({"namelists":groups}), encoding="utf-8")
    fields=[]
    for r in rows:
        for k in r:
            if k not in fields: fields.append(k)
    path=out/"cases.csv"
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    return path


class ExtensionCompositeTests(unittest.TestCase):
    def test_extension_preserves_identity_and_is_small(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); lab=make_lab(root); base=make_base(root)
            definition=root/"extension.toml"
            definition.write_text(f"""
[extension]
name = "ext"
base_cases = "{base}"
base_case_ids = ["R000002"]

[overrides.run_control_config]
termination_mode = "wall_time"
final_time_ms = 1000.0
wall_time_limit_s = 3600.0
wall_time_reserve_s = 60.0
""", encoding="utf-8")
            data=read_extension(definition,lab); cases=build_cases(data,lab)
            self.assertEqual(len(cases),1)
            self.assertEqual(cases[0]["parent_case_id"],"R000002")
            self.assertEqual(cases[0]["parent_identity_sha256"],cases[0]["identity_sha256"])
            out=create_files(data,definition,cases,lab,False)
            with (out/"cases.csv").open(newline="", encoding="utf-8") as f:
                row=next(csv.DictReader(f))
            self.assertEqual(row["extension_parent_case_id"],"R000002")
            self.assertEqual(row["run_control_config.termination_mode"],"wall_time")

    def test_composite_uses_extension_only_for_linked_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); lab=make_lab(root); base=make_base(root)
            definition=root/"extension.toml"
            definition.write_text(f"""
[extension]
name = "ext"
base_cases = "{base}"
base_case_ids = ["R000002"]

[overrides.run_control_config]
termination_mode = "wall_time"
final_time_ms = 1000.0
wall_time_limit_s = 3600.0
wall_time_reserve_s = 60.0
""", encoding="utf-8")
            data=read_extension(definition,lab); cases=build_cases(data,lab)
            ext=create_files(data,definition,cases,lab,False)
            with (ext/"cases.csv").open(newline="", encoding="utf-8") as f:
                erow=next(csv.DictReader(f))
            cp=Path(erow["case_path"]); cp.mkdir(parents=True)
            (cp/"run_status.json").write_text(json.dumps({"status":"condition_met","physical_condition_met":True}),encoding="utf-8")
            comp=root/"campaigns"/"_composites"/"c"
            build_composite(base,ext/"cases.csv",comp,lab,False)
            with (comp/"cases.csv").open(newline="", encoding="utf-8") as f:
                rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),3)
            byid={r["case_id"]:r for r in rows}
            self.assertEqual(byid["R000001"]["composite_source_role"],"base")
            self.assertEqual(byid["R000002"]["composite_source_role"],"extension")
            self.assertEqual(byid["R000002"]["composite_source_case_id"],"E000001")

    def test_runner_rejects_composite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); lab=make_lab(root); base=make_base(root)
            comp=root/"campaigns"/"_composites"/"blocked"; comp.mkdir(parents=True)
            shutil.copy2(base,comp/"cases.csv")
            (comp/"composite_manifest.json").write_text(json.dumps({"analysis_only":True}),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"analysis-only composite"):
                CampaignRunner(comp/"cases.csv",lab.runner_config,lab.config_path)


if __name__=="__main__":
    unittest.main()
