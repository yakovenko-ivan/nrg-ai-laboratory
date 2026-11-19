"""Bounded G2-L1 Stage-2 reactivity screening and Cantera comparison.

This module is operator-installed TRUSTED infrastructure, never a study-written runner.
No arbitrary commands, executable paths or mechanism choices are passed from Pi.
The source/binary/Gate-B pins are inherited from g2_l1_convergence (Stage-1).
The fixed Cantera signal gate selects the shorter of 5 or 20 microseconds at which
reference O2 consumption >=1%; otherwise the NRG grid is *not* executed.
This remains an isothermal constant-volume CHEMISTRY test, not an ignition test.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_workspace import g2_l1_convergence as base

SCHEMA = "nrg-g2-l1-reactivity/1"
AUTH_REL = "config/g2_l1_reactivity_authorization.toml"
CANDIDATES = (5e-6, 20e-6)
TARGET_O2_CONSUMPTION = 0.01
DT_GRID = (1e-8, 5e-9)
EPS_GRID = (1e-7, 1e-9, 1e-11)
REF_LADDER = ((1e-10, 1e-18), (1e-11, 1e-20), (1e-12, 1e-22))
SPECIES = ("H2", "O2", "N2", "O", "H", "OH", "H2O", "HO2", "H2O2", "AR", "HE")
CAN_VERSION = "3.2.0"
MAX_STEPS_PER_CASE = 4000
MAX_EVIDENCE_BYTES = 80 * 1024 * 1024


def require(cond: bool, message: str) -> None:
    if not cond:
        raise RuntimeError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_auth(path: Path, table: str, keys: set[str]) -> tuple[dict[str, Any], str]:
    require(path.is_file() and not path.is_symlink() and path.stat().st_size < 16 * 1024,
            f"authorization absent or unsafe: {path}")
    raw = path.read_bytes()
    doc = tomllib.loads(raw.decode("utf-8"))
    require(set(doc) == {table} and isinstance(doc[table], dict) and
            set(doc[table]) == keys, "authorization structure inconsistent")
    return doc[table], sha(raw)


def case_grid(horizon: float) -> list[dict[str, Any]]:
    require(horizon in CANDIDATES, "unapproved reaction horizon")
    cases = []
    for eps in EPS_GRID:
        for dt in DT_GRID:
            steps = round(horizon / dt)
            require(steps <= MAX_STEPS_PER_CASE and
                    math.isclose(steps * dt, horizon, rel_tol=1e-12),
                    "invalid step count / horizon")
            cases.append({"name": f"R{round(horizon*1e6)}_E{round(-math.log10(eps))}_T{round(dt*1e9)}NS",
                          "eps": eps, "dt": dt, "n_steps": steps})
    return cases


def select_horizon(signals: dict[str, float]) -> float | None:
    """Reference-only design rule; never infer success from a tiny signal."""
    for horizon in CANDIDATES:
        val = signals.get(f"{horizon:.9e}")
        require(val is not None and math.isfinite(val) and val >= 0,
                "missing/nonfinite Cantera O2 screening measurement")
        if val >= TARGET_O2_CONSUMPTION:
            return horizon
    return None


def check_path(value: str, *, root: Path) -> Path:
    path = base._path(value, must_exist=False)
    require(path.resolve().is_relative_to(root.resolve()) and path != root,
            "Stage-2 output destination escapes authorized root")
    return path


def _cantera_version() -> str:
    try:
        import cantera as ct
    except ImportError as exc:
        raise RuntimeError("Cantera unavailable in the Pi bridge Python environment") from exc
    return str(ct.__version__)


def _context(root: Path) -> dict[str, Any]:
    require(root.is_dir(), "laboratory checkout absent")
    base_auth, base_digest = read_auth(root / base.AUTH_REL, "g2_l1", {
        "schema_version", "nrg_source_root", "binary_path", "gate_b_archive",
        "run_root", "export_dir", "per_case_timeout_s"})
    require(base_auth["schema_version"] == 1, "Stage-1 authorization version changed")
    stage2, stage2_digest = read_auth(root / AUTH_REL, "g2_l1_stage2", {
        "schema_version", "run_root", "export_dir", "case_timeout_s", "reference_timeout_s"})
    require(type(stage2["schema_version"]) is int and stage2["schema_version"] == 1,
            "Stage-2 authorization version changed")
    require(type(stage2["case_timeout_s"]) is int and 1 <= stage2["case_timeout_s"] <= 25,
            "G2 case wall cap must be 1..25 s")
    require(type(stage2["reference_timeout_s"]) is int and 1 <= stage2["reference_timeout_s"] <= 120,
            "Cantera reference wall cap must be 1..120 s")
    run_root = check_path(stage2["run_root"], root=root / ".local" / "g2_l1_runs")
    export = check_path(stage2["export_dir"], root=root / "agent_workspace" / "studies")
    require(os.environ.get("OMP_NUM_THREADS") == "1", "Pi environment requires OMP_NUM_THREADS=1")
    require(os.environ.get("OMP_DYNAMIC", "FALSE").upper() in ("FALSE", "0"),
            "Pi environment requires OMP_DYNAMIC=FALSE")
    nrg = Path(base_auth["nrg_source_root"])
    require(nrg.is_absolute() and nrg.is_dir() and ".." not in nrg.parts and
            all(not p.is_symlink() for p in (nrg, *nrg.parents)), "NRG source checkout unsafe")
    binary = base._path(base_auth["binary_path"], must_exist=True)
    archive = base._path(base_auth["gate_b_archive"], must_exist=True)
    require(base._file_hash(binary) == base.BINARY_SHA256, "unapproved B2 binary")
    base._check_dynamic_loader(binary)
    base._git_source_hashes(nrg)
    fixture, resources = base._load_fixture(archive)
    require(Path(sys.prefix).resolve() == (root / ".venv").resolve(),
            "Cantera reference requires the laboratory's .venv interpreter")
    require(_cantera_version() == CAN_VERSION, "Cantera 3.2.0 required")
    script = Path(__file__).resolve()
    source = Path(base.__file__).resolve()
    require(script.is_relative_to(root.resolve()) and source.is_relative_to(root.resolve()),
            "trusted runner source outside laboratory checkout")
    policy = {
        "schema": SCHEMA, "base_auth_sha256": base_digest, "stage2_auth_sha256": stage2_digest,
        "source_commit": base.NRG_COMMIT, "source_sha256": base.SOURCE_HASHES,
        "binary_sha256": base.BINARY_SHA256, "gate_b_archive_sha256": base.GATE_B_SHA256,
        "binary": str(binary), "run_root": str(run_root), "export_dir": str(export),
        "runner_sha256": base._file_hash(script), "stage1_runner_sha256": base._file_hash(source),
        "python_executable": sys.executable, "cantera_version": CAN_VERSION,
        "candidates_s": CANDIDATES, "minimum_o2_consumption": TARGET_O2_CONSUMPTION,
        "epsilons": EPS_GRID, "timesteps_s": DT_GRID,
        "maximum_cases": 6, "maximum_steps_per_case": MAX_STEPS_PER_CASE,
        "case_timeout_s": stage2["case_timeout_s"],
        "reference_timeout_s": stage2["reference_timeout_s"],
        "reference_tolerances": REF_LADDER,
        "model": "isothermal, isochoric, stoichiometric H2/air, KEROMNES; not ignition"
    }
    policy["plan_sha256"] = sha(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode())
    return {"plan": policy, "binary": binary, "fixture": fixture, "resources": resources,
            "run_root": run_root, "export": export}


def plan(root: Path) -> dict[str, Any]:
    try:
        return {"can_run": True, **_context(root)["plan"]}
    except Exception as exc:
        return {"can_run": False, "error": f"{type(exc).__name__}: {exc}"}


def _run_log_ok(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    require(re.search(r"^# result: EXIT_0\s*$", "\n".join(text.splitlines()[:5]), re.M) is not None,
            "trusted subprocess log is not EXIT_0")


def _read_reference(root: Path, selected: float) -> tuple[list[dict[str, str]], dict[str, Any]]:
    selection = json.loads((root / "reference_selection.json").read_text())
    require(selection["schema"] == SCHEMA and selection["cantera_version"] == CAN_VERSION,
            "reference record identity mismatch")
    horizon = select_horizon(selection["signals"])
    require(horizon == selected, "reference horizon selection tampered")
    summaries = selection["reference_ladder"]
    require([tuple(x["tolerances"]) for x in summaries] == list(REF_LADDER),
            "reference ladder metadata changed")
    for j, rung in enumerate(summaries, 1):
        require(base._file_hash(root / f"reference_rung_{j}.csv") == rung["file_sha256"],
                "reference CSV digest does not match trusted selection record")
    fine = root / "reference_rung_3.csv"
    with fine.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    require(len(rows) == round(selected/min(DT_GRID)), "reference sample count mismatch")
    for i, r in enumerate(rows, 1):
        require(math.isclose(float(r["t_s"]), i*min(DT_GRID), rel_tol=1e-11, abs_tol=1e-18),
                "reference time grid mismatch")
        for sp in SPECIES:
            require(math.isfinite(float(r[f"c_molm3_{sp}"])) and
                    float(r[f"c_molm3_{sp}"]) >= -1e-12, "reference concentration invalid")
    return rows, selection


def _compare(root: Path, cases: list[dict[str, Any]], refrows: list[dict[str, str]],
             selection: dict[str, Any]) -> dict[str, Any]:
    data = {}
    for case in cases:
        with (root / case["name"] / "g2_l1_points.csv").open(newline="") as fh:
            data[(case["eps"], case["dt"])] = list(csv.DictReader(fh))
    max_ref_step = min(DT_GRID)
    signal = float(selection["reaction_signal_max_molm3"])
    require(math.isfinite(signal) and signal > 0, "reference reaction signal invalid")
    out: dict[str, Any] = {"reference_ladder": selection["reference_ladder"],
                           "cantera_sensitivity": selection["reference_spread_molm3"],
                           "reaction_signal_max_molm3": signal,
                           "o2_consumption_reference": selection["signals"][f"{selection['chosen_horizon_s']:.9e}"],
                           "nrg_vs_cantera": [], "nrg_internal": [],
                           "scope": "ISOTHERMAL_CV_REACTIVITY_PILOT_NOT_IGNITION"}
    for case in cases:
        rows = data[(case["eps"], case["dt"])]
        factor = round(case["dt"]/max_ref_step)
        aligned = refrows[factor-1::factor]
        require(len(rows) == len(aligned), "NRG/Cantera grids not aligned")
        species_err = {s: max(abs(float(a[f"c_molm3_{s}"])-float(b[f"c_molm3_{s}"]))
                              for a,b in zip(rows, aligned)) for s in SPECIES}
        peak_sp = max(species_err, key=species_err.get)
        out["nrg_vs_cantera"].append({"case": case["name"], "max_traj_abs_molm3": species_err[peak_sp],
                                      "error_over_reaction_signal": species_err[peak_sp]/signal,
                                      "argmax_species": peak_sp, "final_species_abs_molm3": {
                                          s: abs(float(rows[-1][f"c_molm3_{s}"])-float(aligned[-1][f"c_molm3_{s}"]))
                                          for s in SPECIES}})
    for dt in DT_GRID:
        tight = data[(min(EPS_GRID),dt)]
        for eps in EPS_GRID:
            out["nrg_internal"].append({"eps": eps,"dt": dt,"linf_vs_tightest_molm3":
                                        base._linf(data[(eps,dt)],tight)})
    for eps in EPS_GRID:
        coarse,fine=DT_GRID
        out["nrg_internal"].append({"eps":eps,"comparison":"coarse_vs_fine",
                                    "linf_molm3": base._linf(data[(eps,coarse)],data[(eps,fine)],2)})
    out["reference_spread_over_reaction_signal"] = (
        selection["reference_spread_molm3"]["rung2_vs_rung3"] / signal)
    out["pilot_screen"] = (
        "PROVISIONAL_CONCORDANCE" if
        max(x["error_over_reaction_signal"] for x in out["nrg_vs_cantera"]) <= 0.05 and
        out["reference_spread_over_reaction_signal"] <= 0.01
        else "NOT_DEMONSTRATED")
    out["scientific_acceptance"] = "NOT_ASSESSED_FULL_STAGE_P"
    return out


def _archive(root: Path) -> dict[str, Any]:
    paths = list(root.rglob("*"))
    require(len(paths) <= 300 and all(not p.is_symlink() for p in paths),
            "evidence path inventory unsafe")
    files = sorted(x for x in paths if x.is_file() and x.name not in (
        "Stage2_Evidence.tar.gz", "Stage2_Evidence.tar.gz.sha256", "SHA256SUMS.txt"))
    require(sum(x.stat().st_size for x in files) <= MAX_EVIDENCE_BYTES, "evidence exceeds 80 MiB")
    sums = "".join(f"{base._file_hash(p)}  {p.relative_to(root).as_posix()}\n" for p in files)
    (root / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
    target = root / "Stage2_Evidence.tar.gz"
    import tarfile
    with tarfile.open(target, "w:gz") as tf:
        for file in files + [root / "SHA256SUMS.txt"]:
            tf.add(file, arcname=file.relative_to(root).as_posix(), recursive=False)
    digest = base._file_hash(target)
    (root / "Stage2_Evidence.tar.gz.sha256").write_text(digest + "  Stage2_Evidence.tar.gz\n")
    return {"sha256": digest, "path": str(target), "member_count": len(files)+1}


def execute(root: Path, expected_plan_sha256: str, run_bounded: Callable[..., str]) -> dict[str, Any]:
    p = _context(root)
    plan_data = p["plan"]
    require(expected_plan_sha256 == plan_data["plan_sha256"] and
            re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256 or "") is not None,
            "Stage-2 plan-to-execute hash mismatch")
    work = p["run_root"]
    work.mkdir(exist_ok=False)
    state: dict[str, Any] = {"schema": SCHEMA,"status":"STARTED", "plan":plan_data,
                            "started_utc":datetime.now(timezone.utc).isoformat(), "cases":[]}
    try:
        # The trusted bridge executes this module in reference-only mode via
        # the same bounded, process-group-aware runner used by Stage-1.
        ref = work / "reference"
        ref.mkdir(exist_ok=False)
        (ref / "g2_l1_driver_input.nml").write_bytes(p["fixture"])
        # The approved Gate-B header is the only source of Y0/W/rho validation.
        archive = Path(read_auth(root / base.AUTH_REL, "g2_l1", {
            "schema_version","nrg_source_root","binary_path","gate_b_archive",
            "run_root","export_dir","per_case_timeout_s"})[0]["gate_b_archive"])
        import tarfile
        with tarfile.open(archive, "r:gz") as tf:
            header = base._archive_member(tf,"primary_100_steps/g2_l1_run_header.txt")
        (ref / "g2_l1_run_header.txt").write_bytes(header)
        for rel, content in p["resources"].items():
            dest = ref / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        run_bounded([sys.executable, "-m", "agent_workspace.g2_l1_reactivity",
                     "--reference", str(ref)],
                    "G2 Stage-2 Cantera reference", plan_data["reference_timeout_s"],
                    ref / "run.log", cwd=root)
        _run_log_ok(ref / "run.log")
        selection = json.loads((ref/"reference_selection.json").read_text())
        chosen = select_horizon(selection["signals"])
        state["reference_selection"] = selection
        if chosen is None:
            state["status"] = "INSUFFICIENT_REFERENCE_REACTIVITY"
        else:
            ref_rows, selection = _read_reference(ref, chosen)
            state["selected_horizon_s"] = chosen
            state["cases_planned"] = cases = case_grid(chosen)
            for rel, raw in p["resources"].items():
                dst = work/rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(raw)
                require(base._file_hash(dst) == base.RESOURCES[rel], "shared resource staging mismatch")
            for case in cases:
                case_dir=work/case["name"]
                case_dir.mkdir(exist_ok=False)
                (case_dir/"task_setup").mkdir(exist_ok=False)
                for rel, raw in p["resources"].items():
                    dest=case_dir/"task_setup"/rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(raw)
                    require(base._file_hash(dest)==base.RESOURCES[rel],"parser resource staging mismatch")
                    require((case_dir/"task_setup"/rel.split("/")[0]/".."/rel).is_file(),
                            "production parser path unresolved")
                    require(base._file_hash(work/rel)==base.RESOURCES[rel],"shared resource changed")
                input_bytes = base._nml(p["fixture"],case)
                (case_dir/"g2_l1_driver_input.nml").write_bytes(input_bytes)
                require(base._file_hash(p["binary"]) == base.BINARY_SHA256,
                        "binary modified during execution")
                state["cases"].append({"case":case,"input_sha256":sha(input_bytes),"status":"LAUNCHED"})
                (work/"EXECUTION_RECORD.json").write_text(json.dumps(state, indent=2,sort_keys=True)+"\n")
                run_bounded([str(p["binary"]),"g2_l1_driver_input.nml"],
                            "G2 Stage-2 "+case["name"], plan_data["case_timeout_s"],
                            case_dir/"run.log",cwd=case_dir)
                state["cases"][-1].update(base._validate_run(case,case_dir))
                require(all(base._file_hash(work/rel)==digest for rel,digest in base.RESOURCES.items()),
                        "shared resource tamper detected after case")
                state["cases"][-1]["status"] = "COMPLETE"
            state["comparison"] = _compare(work, cases, ref_rows, selection)
            state["status"] = "COMPLETED_REVIEW_REQUIRED"
    except BaseException as exc:
        state["status"] = "STOPPED"
        state["stop_reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["finished_utc"]=datetime.now(timezone.utc).isoformat()
        (work/"EXECUTION_RECORD.json").write_text(json.dumps(state, indent=2, sort_keys=True)+"\n")
        state["evidence"] = _archive(work)
        exp=p["export"]
        exp.mkdir(exist_ok=False)
        target=exp/"Stage2_Evidence.tar.gz"
        shutil.copyfile(state["evidence"]["path"],target)
        require(base._file_hash(target)==state["evidence"]["sha256"],
                "evidence export digest mismatch")
        (exp/"Stage2_Evidence.tar.gz.sha256").write_text(
            state["evidence"]["sha256"]+"  Stage2_Evidence.tar.gz\n",encoding="utf-8")
        state["exported_to"]=str(target)
    return state


def _parse_initial(ref: Path) -> tuple[dict[str, float], list[float], float]:
    nml=(ref/"g2_l1_driver_input.nml").read_text()
    names={int(i):s for i,s in re.findall(r"composition\((\d+)\)%name\s*=\s*'([^']+)'",nml,re.I)}
    values={int(i):float(v.replace("D","E").replace("d","e")) for i,v in
            re.findall(r"composition\((\d+)\)%mole_fraction\s*=\s*([\d.+EeDd-]+)",nml,re.I)}
    require(set(names)==set(values)=={1,2,3},"unexpected Cantera input composition")
    X={names[k]:values[k] for k in names}
    require(set(X)=={"H2","O2","N2"} and abs(sum(X.values())-1)<1e-9,
            "unexpected initial mole fractions")
    header=(ref/"g2_l1_run_header.txt").read_text()
    found=re.findall(r"^\s*\d+\s+([A-Z0-9]+)\s+W_kg_per_mol=\s*(\S+)\s+Y0=\s*(\S+)",header,re.M)
    require(tuple(t[0] for t in found)==SPECIES,"initial NRG species order differs")
    Ys=[float(t[2]) for t in found]
    density=re.search(r"^initial_density_kgm3:\s*(\S+)",header,re.M)
    require(density is not None,"NRG initial density absent")
    return X,Ys,float(density.group(1))


def _reference_child(ref: Path) -> None:
    """Runs only in a bounded subprocess, never during plan or Pi analysis."""
    import cantera as ct
    from cantera import ck2yaml
    require(ct.__version__==CAN_VERSION,"Cantera version drift")
    for rel,digest in base.RESOURCES.items():
        require(base._file_hash(ref/rel)==digest,"Cantera input resource mismatch")
    X,Y0,rho0=_parse_initial(ref)
    yaml_path=ref/"KEROMNES_CANTERA.yaml"
    ck2yaml.convert(input_file=str(ref/"chemical_mechanisms/hydrogen/KEROMNES.txt"),
                    thermo_file=str(ref/"thermophysical_data/KEROMNES_THERMO.txt"),
                    out_name=str(yaml_path),quiet=True)
    def initialize(rtol: float, atol: float):
        phase=ct.Solution(str(yaml_path))
        require(tuple(phase.species_names)==SPECIES and phase.n_reactions==24,
                "converted chemical mechanism differs from NRG")
        phase.TPX=1500.0,101325.0,X
        require(abs(phase.density-rho0)/rho0<1e-10,"initial Cantera/NRG density mismatch")
        require(all(abs(float(phase.Y[k])-Y0[k])<1e-11 for k in range(len(SPECIES))),
                "initial Cantera/NRG mass fractions mismatch")
        reactor=ct.IdealGasReactor(phase,energy="off",clone=False)
        reactor.volume=1e-9
        net=ct.ReactorNet([reactor]);net.rtol=rtol;net.atol=atol;net.max_time_step=1e-8
        return reactor,net
    reactor,net=initialize(*REF_LADDER[-1])
    c0 = {s: float(reactor.phase.concentrations[k])*1000 for k,s in enumerate(SPECIES)}
    initial_o2=rho0*Y0[SPECIES.index("O2")]/0.031998
    require(initial_o2>0,"initial O2 absent")
    signals={}
    for horizon in CANDIDATES:
        net.advance(horizon)
        consumed=max(0.0,(initial_o2-float(reactor.phase.concentrations[SPECIES.index("O2")])*1000)/initial_o2)
        signals[f"{horizon:.9e}"]=consumed
    chosen=select_horizon(signals)
    summary: dict[str,Any]={"schema":SCHEMA,"cantera_version":ct.__version__,
            "mechanism_yaml_sha256":base._file_hash(yaml_path),
            "signals":signals,"chosen_horizon_s":chosen,
            "reference_ladder":[],"reference_spread_molm3":None,
            "scientific_status":"NOT_ASSESSED_FULL_STAGE_P"}
    if chosen is not None:
        records=[]
        N=round(chosen/min(DT_GRID))
        for j,(rtol,atol) in enumerate(REF_LADDER,1):
            reactor,net=initialize(rtol,atol)
            rows=[]
            for i in range(1,N+1):
                t=i*min(DT_GRID)
                net.advance(t)
                phase=reactor.phase
                require(abs(phase.T-1500.0)<1e-7 and abs(reactor.volume-1e-9)<1e-18,
                        "reference state not isothermal/isochoric")
                row={"t_s":t,"T_K":float(phase.T),"p_Pa":float(phase.P),
                     "rho_kgm3":float(phase.density)}
                row.update({f"c_molm3_{s}":float(phase.concentrations[k])*1000
                            for k,s in enumerate(SPECIES)})
                row.update({f"Y_{s}":float(phase.Y[k]) for k,s in enumerate(SPECIES)})
                rows.append(row)
            with (ref/f"reference_rung_{j}.csv").open("w",newline="") as f:
                writer=csv.DictWriter(f,list(rows[0]));writer.writeheader();writer.writerows(rows)
            summary["reference_ladder"].append({"tolerances":[rtol,atol],"rows":N,
                                                 "file_sha256":base._file_hash(ref/f"reference_rung_{j}.csv")})
            records.append(rows)
        summary["reference_spread_molm3"]={"rung1_vs_rung2":base._linf(records[0],records[1]),
                                            "rung2_vs_rung3":base._linf(records[1],records[2])}
        summary["reaction_signal_max_molm3"] = max(
            abs(records[-1][-1][f"c_molm3_{s}"] - c0[s]) for s in SPECIES)
    (ref/"reference_selection.json").write_text(json.dumps(summary,indent=2,sort_keys=True,allow_nan=False)+"\n")


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--reference",type=Path,required=True)
    args=parser.parse_args()
    allowed = (Path.cwd() / ".local" / "g2_l1_runs").resolve()
    reference = args.reference.resolve()
    if args.reference.is_symlink() or reference.name != "reference" or not reference.is_relative_to(allowed):
        raise SystemExit("reference path outside authorized laboratory run root")
    try:
        _reference_child(args.reference)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise SystemExit(2) from exc
