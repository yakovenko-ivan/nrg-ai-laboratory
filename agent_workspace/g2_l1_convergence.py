"""G2-L1 trusted six-case SLATEC convergence pilot (no arbitrary executable input).

Operator-only installation. The research agent may plan and request execution,
never choose arbitrary paths, binaries, chemistry mechanisms, or broader cases.
The privileged bridge supplies a bounded subprocess runner with outcome logging.
This is *not* full independent scientific validation against Cantera.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = "nrg-g2-l1-agent-pilot/1"
AUTH_REL = "config/g2_l1_authorization.toml"
NRG_COMMIT = "6013e120fe289826cff82d073fe3bc232d86e27b"
BINARY_SHA256 = "95757e66d339b7559aa1a40d1b5ddcc025b4e8d13c4f335e3303c4a4afb6e903"
GATE_B_SHA256 = "b3a5d1417b96836795aec9266c01225f01c25ce282dfa06aab43dd195b65135f"
SOURCE_HASHES = {
    "computing_module/src/tests/g2_l1_single_cell_driver.f90": "59d23a796c6764cb29a5a46e3615ff7999245beefdb14e3a3d2b067b74cccc97",
    "computing_module/src/current_build/chemical_kinetics_solver.f90": "09223e9e694cddef16eaa5ab4b565314d31c34c1bbefe9f9cfffb7abb92a622f",
}
RESOURCES = {
    "chemical_mechanisms/hydrogen/KEROMNES.txt": "b4bc17409791acd4aa7f72c9569015c96fc0c36990d7a333cc2798e946c75ca7",
    "thermophysical_data/KEROMNES_THERMO.txt": "8079c0749b9c2527d41f0dad83210a8bef6e4ddce71b39545cfbfeb51ff58fc6",
    "thermophysical_data/KEROMNES_TRANSDATA.txt": "bae04a9d2f72eaa5522ece7bc2f12b93396fc8345640dfd53a2c17fcfb13b57b",
}
TIMESTEPS = (1e-8, 5e-9)
EPSILONS = (1e-7, 1e-9, 1e-11)
FINAL_TIME = 1e-6
MAX_CASES = len(TIMESTEPS) * len(EPSILONS)
FILE_LIMIT = 4 * 1024 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise RuntimeError(message)


def _path(value: str, *, must_exist: bool) -> Path:
    p = Path(value)
    _require(p.is_absolute() and ".." not in p.parts and "." not in p.parts,
             "authorization path must be absolute without dot components")
    # A symlink in a parent would bypass path/containment reasoning.
    for part in (p, *p.parents):
        _require(not part.is_symlink(), f"authorization path contains symlink: {part}")
    if must_exist:
        _require(p.is_file(), f"required file absent: {p}")
    else:
        _require(not p.exists() and p.parent.is_dir(), "run_root must be absent with existing parent")
    return p


def _git_source_hashes(root: Path) -> None:
    for rel, expected in SOURCE_HASHES.items():
        proc = subprocess.run(
            ["git", "-C", str(root), "--no-optional-locks", "show", f"{NRG_COMMIT}:{rel}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
            env={k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")},
        )
        _require(proc.returncode == 0, f"pinned NRG commit/source unavailable: {rel}")
        _require(_sha(proc.stdout) == expected, f"pinned NRG source hash mismatch: {rel}")


def _check_dynamic_loader(binary: Path) -> None:
    # The B2 ifx binary needs Intel's OpenMP runtime.  Refuse instead of
    # discovering a missing libiomp5.so after a run directory is created.
    proc = subprocess.run(["ldd", str(binary)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=10, check=False)
    _require(proc.returncode == 0 and b"not found" not in proc.stdout and
             b"libiomp5.so" in proc.stdout and b"libiomp5.so =>" in proc.stdout,
             "G2 driver dynamic libraries unresolved; initialize oneAPI in Pi environment")


def _archive_member(tf: tarfile.TarFile, name: str) -> bytes:
    try:
        member = tf.getmember(name)
    except KeyError as exc:
        raise RuntimeError(f"Gate-B archive missing {name}") from exc
    _require(member.isfile() and 0 < member.size <= FILE_LIMIT, f"unsafe/oversized archive member: {name}")
    stream = tf.extractfile(member)
    _require(stream is not None, f"unreadable archive member {name}")
    return stream.read()


def _load_fixture(archive: Path) -> tuple[bytes, dict[str, bytes]]:
    _require(archive.stat().st_size <= 10 * 1024 * 1024,
             "Gate-B archive unexpectedly large")
    _require(_file_hash(archive) == GATE_B_SHA256, "frozen Gate-B archive hash mismatch")
    with tarfile.open(archive, "r:gz") as tf:
        prefix = "primary_100_steps/"
        nml = _archive_member(tf, prefix + "g2_l1_driver_input.nml")
        resources = {}
        for rel, digest in RESOURCES.items():
            raw = _archive_member(tf, prefix + "task_setup/" + rel)
            _require(_sha(raw) == digest, f"Gate-B resource hash mismatch: {rel}")
            resources[rel] = raw
    source = nml.decode("utf-8")
    checks = {
        "backend": r"(?im)^\s*backend\s*=\s*'slatec'",
        "eps": r"(?im)^\s*eps\s*=\s*1\.0e-9\s*$",
        "ewt": r"(?im)^\s*ewt\s*=\s*1\.0e-4\s*$",
        "dt": r"(?im)^\s*delta_t_s\s*=\s*1\.0e-8\s*$",
        "steps": r"(?im)^\s*n_steps\s*=\s*100\s*$",
        "state": r"(?im)^\s*state_id\s*=\s*'M1_S1'\s*$",
        "neg": r"(?im)^\s*neg_test\s*=\s*'none'",
        "temp": r"(?im)^\s*initial_temperature_K\s*=\s*1500\.0\s*$",
    }
    for name, expr in checks.items():
        _require(len(re.findall(expr, source)) == 1, f"unsupported fixture {name}")
    for key, rel in (("mechanism_file", "chemical_mechanisms/hydrogen/KEROMNES.txt"),
                     ("thermo_file", "thermophysical_data/KEROMNES_THERMO.txt"),
                     ("transport_file", "thermophysical_data/KEROMNES_TRANSDATA.txt")):
        expr = rf"(?im)^\s*{key}\s*=\s*'\.\./{re.escape(rel)}'\s*$"
        _require(len(re.findall(expr, source)) == 1, f"unexpected production path in fixture: {key}")
    return nml, resources


def _cases() -> list[dict[str, Any]]:
    return [dict(name=f"E{int(round(-math.log10(eps)))}_T{round(dt*1e9)}NS",
                 eps=eps, dt=dt, n_steps=round(FINAL_TIME / dt))
            for eps in EPSILONS for dt in TIMESTEPS]


def _nml(template: bytes, case: dict[str, Any]) -> bytes:
    src = template.decode("utf-8")
    for key, repl in (("eps", f"eps = {case['eps']:.1e}"),
                      ("delta_t_s", f"delta_t_s = {case['dt']:.1e}"),
                      ("n_steps", f"n_steps = {case['n_steps']}"),
                      ("state_id", f"state_id = '{case['name']}'")):
        expr = rf"(?m)^\s*{key}\s*=\s*[^\n]+"
        src, count = re.subn(expr, "    " + repl, src)
        _require(count == 1, f"bad fixture field replacement {key}")
    return src.encode("utf-8")


def _auth(lab_root: Path) -> dict[str, Any]:
    _require(lab_root.is_dir(), "laboratory root absent")
    auth_path = lab_root / AUTH_REL
    _require(not auth_path.is_symlink() and auth_path.is_file(), "operator authorization missing")
    _require(auth_path.stat().st_size <= 16 * 1024, "authorization oversized")
    content = auth_path.read_bytes()
    cfg = tomllib.loads(content.decode("utf-8"))
    _require(set(cfg) == {"g2_l1"}, "unexpected authorization tables")
    val = cfg["g2_l1"]
    expected = {"schema_version", "nrg_source_root", "binary_path", "gate_b_archive", "run_root", "export_dir", "per_case_timeout_s"}
    _require(set(val) == expected, "missing/unexpected G2 authorization keys")
    _require(type(val["schema_version"]) is int and val["schema_version"] == 1,
             "authorization schema mismatch")
    timeout = val["per_case_timeout_s"]
    _require(type(timeout) is int and 1 <= timeout <= 25, "per-case timeout outside fixed bound")
    src = Path(val["nrg_source_root"])
    _require(src.is_dir() and src.is_absolute() and ".." not in src.parts,
             "NRG source checkout invalid")
    for parent in (src, *src.parents):
        _require(not parent.is_symlink(), "NRG source checkout symlink")
    binary = _path(val["binary_path"], must_exist=True)
    archive = _path(val["gate_b_archive"], must_exist=True)
    out = _path(val["run_root"], must_exist=False)
    trusted_run_base = (lab_root / ".local" / "g2_l1_runs").resolve()
    _require(out.resolve().is_relative_to(trusted_run_base), "run_root outside trusted .local/g2_l1_runs")
    export = _path(val["export_dir"], must_exist=False)
    study_root = (lab_root / "agent_workspace" / "studies").resolve()
    _require(export.resolve().is_relative_to(study_root), "evidence export outside Pi-readable studies")
    _require(_file_hash(binary) == BINARY_SHA256, "unapproved G2 driver binary")
    _require(os.environ.get("OMP_NUM_THREADS") == "1",
             "Pi's trusted execution environment must set OMP_NUM_THREADS=1")
    _check_dynamic_loader(binary)
    _git_source_hashes(src)
    fixture, resource = _load_fixture(archive)
    plan_data = {
        "schema": SCHEMA, "auth_sha256": _sha(content), "source_commit": NRG_COMMIT,
        "source_sha256": SOURCE_HASHES, "binary": str(binary), "binary_sha256": BINARY_SHA256,
        "gate_b_archive_sha256": GATE_B_SHA256, "run_root": str(out), "export_dir": str(export),
        "case_timeout_s": timeout, "cases": _cases(), "reference": "NOT_EXECUTED",
    }
    stable = json.dumps(plan_data, sort_keys=True, separators=(",", ":")).encode()
    plan_data["plan_sha256"] = _sha(stable)
    return dict(plan=plan_data, fixture=fixture, resources=resource, binary=binary,
                run_root=out, export_dir=export, timeout=timeout)


def plan(lab_root: Path) -> dict[str, Any]:
    try:
        verified = _auth(lab_root)
        return {"can_run": True, "plan": verified["plan"], **verified["plan"]}
    except Exception as exc:
        return {"can_run": False, "error": f"{type(exc).__name__}: {exc}"}


def _validate_run(case: dict[str, Any], root: Path) -> dict[str, Any]:
    log = (root / "run.log").read_text(encoding="utf-8")
    _require(re.search(r"^# result: EXIT_0\s*$", "\n".join(log.splitlines()[:5]), flags=re.M) is not None,
             "trusted process log lacks EXIT_0")
    expected = {"g2_l1_driver_input.nml", "run.log", "g2_l1_run_header.txt",
                "g2_l1_points.csv", "g2_l1_summary.csv", "task_setup"}
    entries = list(root.iterdir())
    _require({p.name for p in entries} == expected, "unexpected/missing case-root output")
    _require(not any(p.is_symlink() for p in root.rglob("*")), "case contains symlinks")
    points = root / "g2_l1_points.csv"
    with points.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    _require(len(rows) == case["n_steps"], "incorrect number of recorded steps")
    for k, row in enumerate(rows, start=1):
        _require(row["schema_version"].strip() == "g2-l1-driver/2" and
                 row["state_id"].strip() == case["name"] and
                 row["backend"].strip() == "slatec", "driver row identity mismatch")
        _require(math.isclose(float(row["eps"]), case["eps"], rel_tol=1e-12) and
                 math.isclose(float(row["delta_t_s"]), case["dt"], rel_tol=1e-12),
                 "driver returned unexpected controls")
        _require(math.isclose(float(row["T_K"]), 1500.0, rel_tol=0, abs_tol=1e-8),
                 "driver not isothermal at 1500 K")
        _require(int(row["step_index"]) == k, "unexpected step index")
        t = float(row["t_s"])
        _require(math.isfinite(t) and math.isclose(t, k * case["dt"], rel_tol=1e-10, abs_tol=1e-18),
                 "wrong time grid")
        concentration_keys = [key for key in row if key.startswith("c_molm3_")]
        y_keys = [key for key in row if key.startswith("Y_")]
        _require(len(concentration_keys) == 11 and len(y_keys) == 11, "unexpected mechanism species")
        yy = [float(row[key]) for key in y_keys]
        cc = [float(row[key]) for key in concentration_keys]
        _require(all(math.isfinite(x) and x >= -1e-13 for x in yy + cc), "non-finite or negative species")
        _require(abs(sum(yy) - 1) <= 1e-9, "species mass closure failure")
        closure = abs(float(row["mass_closure_defect"]))
        bound = float(row["mass_closure_tolerance"])
        _require(math.isfinite(closure) and math.isfinite(bound) and
                 bound >= 0 and closure <= bound, "driver mass closure gate exceeded")
        _require(row["failure_code"].strip() in ("OK", "GATE_APPLIED"),
                 "G2 driver row status not OK")
    with (root / "g2_l1_summary.csv").open("r", encoding="utf-8", newline="") as f:
        summary = list(csv.DictReader(f))
    _require(len(summary) == 1 and summary[0]["state_id"].strip() == case["name"] and
             summary[0]["verdict"].strip() == "G2_A1_NOT_ASSESSED_STAGE_0",
             "summary identity or scientific verdict mismatch")
    return {"case": case, "rows": len(rows), "final_time_s": float(rows[-1]["t_s"]),
            "points_sha256": _file_hash(points), "summary_sha256": _file_hash(root / "g2_l1_summary.csv")}


def _linf(a: list[dict[str, str]], b: list[dict[str, str]], stride: int = 1) -> float:
    species = [key for key in a[0] if key.startswith("c_molm3_")]
    _require(len(species) == 11, "invalid species columns")
    rb = b[stride - 1::stride]
    _require(len(a) == len(rb), "unaligned comparison grids")
    return max(abs(float(p[s]) - float(q[s])) for p, q in zip(a, rb) for s in species)


def _comparisons(root: Path) -> dict[str, Any]:
    rows = {}
    for case in _cases():
        with (root / case["name"] / "g2_l1_points.csv").open(newline="") as f:
            rows[(case["eps"], case["dt"])] = list(csv.DictReader(f))
    out = {"reference_status": "NOT_ASSESSED_CANTERA", "stage_p_scientific_acceptance": "NOT_ASSESSED",
           "same_dt_vs_tightest_eps_linf_molm3": [], "same_eps_coarse_vs_fine_dt_linf_molm3": []}
    fine_eps = min(EPSILONS)
    for dt in TIMESTEPS:
        for eps in EPSILONS:
            out["same_dt_vs_tightest_eps_linf_molm3"].append({"eps": eps, "dt": dt,
                "linf_molm3": _linf(rows[(eps, dt)], rows[(fine_eps, dt)])})
    coarse, fine = max(TIMESTEPS), min(TIMESTEPS)
    factor = round(coarse / fine)
    for eps in EPSILONS:
        out["same_eps_coarse_vs_fine_dt_linf_molm3"].append({"eps": eps,
            "coarse_dt": coarse, "fine_dt": fine, "linf_molm3":
            _linf(rows[(eps, coarse)], rows[(eps, fine)], factor)})
    return out


def _archive_evidence(root: Path) -> dict[str, str]:
    entries = list(root.rglob("*"))
    _require(len(entries) <= 300 and all(not p.is_symlink() for p in entries),
             "evidence tree too large or contains symlink")
    files = sorted(p for p in entries if p.is_file() and p.name not in (
        "G2_L1_Evidence.tar.gz", "G2_L1_Evidence.tar.gz.sha256", "SHA256SUMS.txt"))
    _require(sum(p.stat().st_size for p in files) <= 50 * 1024 * 1024,
             "G2 evidence volume exceeds bounded limit")
    manifest = "".join(f"{_file_hash(p)}  {p.relative_to(root).as_posix()}\n" for p in files)
    (root / "SHA256SUMS.txt").write_text(manifest, encoding="utf-8")
    dest = root / "G2_L1_Evidence.tar.gz"
    with tarfile.open(dest, "w:gz") as tf:
        for p in files + [root / "SHA256SUMS.txt"]:
            tf.add(p, arcname=p.relative_to(root).as_posix(), recursive=False)
    digest = _file_hash(dest)
    (root / "G2_L1_Evidence.tar.gz.sha256").write_text(digest + "  G2_L1_Evidence.tar.gz\n")
    return {"path": str(dest), "sha256": digest, "member_count": len(files) + 1}


def execute(lab_root: Path, expected_plan_sha256: str,
            run_bounded: Callable[..., str]) -> dict[str, Any]:
    checked = _auth(lab_root)
    plan_data = checked["plan"]
    _require(re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256 or "") is not None and
             expected_plan_sha256 == plan_data["plan_sha256"], "plan-to-execute identity mismatch")
    root = checked["run_root"]
    root.mkdir(exist_ok=False)
    state: dict[str, Any] = {"schema": SCHEMA, "plan": plan_data,
                             "started_utc": datetime.now(timezone.utc).isoformat(),
                             "status": "STARTED", "cases": []}
    record = root / "EXECUTION_RECORD.json"
    try:
        # Direct driver references ../chemical_mechanisms and ../thermophysical_data
        # from each case CWD. The parent directories are shared, read-only in
        # scientific intent and rehashed before/after every case. The production
        # parser additionally requires copies beneath each case/task_setup.
        for rel, raw in checked["resources"].items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(raw)
            _require(_file_hash(dst) == RESOURCES[rel], f"shared resource staging mismatch {rel}")
        for case in plan_data["cases"]:
            p = root / case["name"]
            p.mkdir(exist_ok=False)
            (p / "task_setup").mkdir(exist_ok=False)
            for rel, raw in checked["resources"].items():
                # The direct CWD/../ path and production parser's task_setup/../
                # path must BOTH resolve, exactly as in Gate B r3.
                dst = p / "task_setup" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(raw)
                _require(_file_hash(dst) == RESOURCES[rel], f"staging mismatch {rel}")
                # The parser's '/../' path requires the intermediate component.
                _require((p / "task_setup" / rel.split("/")[0] / ".." / rel).is_file(),
                         f"production parser path not resolvable: {rel}")
                _require(_file_hash(root / rel) == RESOURCES[rel], f"shared resource mutated: {rel}")
            nml = _nml(checked["fixture"], case)
            (p / "g2_l1_driver_input.nml").write_bytes(nml)
            _require(_file_hash(checked["binary"]) == BINARY_SHA256, "driver binary changed during pilot")
            state["cases"].append({"case": case, "input_sha256": _sha(nml), "status": "LAUNCHED"})
            record.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
            run_bounded([str(checked["binary"]), "g2_l1_driver_input.nml"],
                        "G2-L1 " + case["name"], checked["timeout"], p / "run.log", cwd=p)
            state["cases"][-1].update(_validate_run(case, p))
            for rel, digest in RESOURCES.items():
                _require(_file_hash(root / rel) == digest, f"shared resource changed by driver: {rel}")
            state["cases"][-1]["status"] = "COMPLETE"
        state["comparison"] = _comparisons(root)
        state["status"] = "COMPLETED_REVIEW_REQUIRED"
    except BaseException as exc:
        state["status"] = "STOPPED"
        state["stop_reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["finished_utc"] = datetime.now(timezone.utc).isoformat()
        record.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        state["evidence"] = _archive_evidence(root)
        # The authoritative data stays under trusted .local; export only a
        # hash-verified copy to the agent-readable study path.
        exp = checked["export_dir"]
        exp.mkdir(exist_ok=False)
        target = exp / "G2_L1_Evidence.tar.gz"
        shutil.copyfile(state["evidence"]["path"], target)
        _require(_file_hash(target) == state["evidence"]["sha256"],
                 "Pi-readable evidence transfer SHA-256 mismatch")
        (exp / "G2_L1_Evidence.tar.gz.sha256").write_text(
            state["evidence"]["sha256"] + "  G2_L1_Evidence.tar.gz\n", encoding="utf-8")
        state["exported_to"] = str(target)
    return state
