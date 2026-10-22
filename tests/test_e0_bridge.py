"""G2 static/unit/mock tests for the E0 Mode 3 bridge commands (corrections 1-8 + R3 R1-R6).

These tests exercise only pure helpers and mocked subprocess boundaries.  They
never build NRG, never import Cantera, and never run CFD.  They import the
bridge module and call the *_plan/*_execute helpers with a mocked Laboratory and
mocked subprocess, so no real compiler, git checkout, cmake, or harness run is
performed.

Run from the repository root (in G2, after the candidate patch is applied to
agent_workspace/lab_bridge.py):

    python -m pytest -q tests/test_e0_bridge.py

The bridge module is imported as ``agent_workspace.lab_bridge`` (the trusted
module the candidate patch extends).  In G2 the patch is applied first, so the
E0 helpers (_e0_config, _e0_authorization, _e0_checkout_preflight, ...) are
present.  The tests use ``mock`` to patch ``subprocess.run`` / ``_run_bounded``
and ``sha256_file`` and pass a ``FakeLab`` stand-in for ``Laboratory``, so they
are independent of any real NRG checkout or Cantera installation.

R3 notes reflected here:
  * R2 — authoritative pins and acceptance thresholds live in the protected
    ``config/e0_authorization.toml`` record; ``[e0]`` is operational only.
  * R3 — environment is built from an explicit allowlist; the harness runs
    through ``_run_bounded`` (Popen + start_new_session + killpg on timeout).
  * R4 — mandatory finite JSON checks (nrg_temperature_K, nrg_diagnostics,
    full comparison error fields) fail closed on missing/NaN/Inf/mismatch.
  * R6 — acceptance thresholds are fixed in the authorization record and are
    not broadenable via ``[e0]``.

Validation-config/data-completeness round (new):
  * ``validation_config.json`` is pinned via ``validation_config_sha256`` in the
    protected record and authenticated in both plan and execute.
  * ``_e0_parse_results`` requires exactly one KEROMNES summary record and
    complete TP/HP comparison blocks (duplicates/extras/malformed rejected).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import types
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Constants shared with the bridge (literal, so no fixture scope leaks).
# ---------------------------------------------------------------------------

_V3_MARKER = "NRG_CHEMKIN_VALIDATION_AUDIT_V3"
_SOURCE_REL = "package_interface/src/tests/classic_tests/0D_chemical_equilibrium_validation.f90"
_CMAKE_SELECTOR = "src/tests/classic_tests/0D_chemical_equilibrium_validation.f90"
_CMAKE_TARGET = "package_interface"
_MECHANISM_SELECTOR = "hydrogen/KEROMNES.txt"
_COMMIT = "f198fa8ad0085bf995feb0dce06b6f79481db7b4"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "keromnes"


# ---------------------------------------------------------------------------
# Fixtures: a synthetic Laboratory, an operational [e0] config, and a protected
# authorization record (R2).
# ---------------------------------------------------------------------------


class FakeLab:
    """Minimal stand-in for nrg_analysis.laboratory.Laboratory."""

    def __init__(self, config_path: Path, local_config_path: Path | None, research_root: Path):
        self.config_path = config_path
        self.local_config_path = local_config_path
        self.research_root = research_root


@pytest.fixture
def e0_cfg(tmp_path: Path) -> dict:
    """OPERATIONAL [e0] settings only (R2).  No pins, no acceptance, no selectors."""
    return {
        "nrg_source_root": str(tmp_path / "NRG"),
        "build_root": str(tmp_path / "build"),
        "run_root": str(tmp_path / "run"),
        "harness": str(tmp_path / "harness" / "tools" / "run_validation.py"),
        "cantera_python": str(tmp_path / "python"),
        "equilibrium_executable": "package_interface_0D_chemical_equilibrium_validation",
        "build_type": "release",
        "parallel_jobs": 2,
        "timeout_seconds": 30,
    }


def _default_auth() -> dict:
    """A valid protected authorization record (R2) with all 8 pins + 7 acceptance keys."""
    return {
        "commit": _COMMIT,
        "source_sha256": "a" * 64,
        "harness_sha256": "b" * 64,
        "validation_config_sha256": "c" * 64,
        "source_relative": _SOURCE_REL,
        "cmake_selector": _CMAKE_SELECTOR,
        "cmake_target": _CMAKE_TARGET,
        "mechanism_selector": _MECHANISM_SELECTOR,
        "acceptance": {
            "normalization_tolerance": 1e-6,
            "nonnegativity_tolerance": 1e-12,
            "element_conservation_tolerance": 1e-7,
            "mass_conservation_tolerance": 1e-7,
            "enthalpy_residual_tolerance": 1e-9,
            "mole_fraction_tolerance": 5e-4,
            "temperature_tolerance_K": 2.0,
        },
    }


@pytest.fixture
def e0_auth() -> dict:
    return _default_auth()


def _scalar(v) -> str:
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


def _write_toml(path: Path, table_name: str, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"[{table_name}]"]
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"[{table_name}.{k}]")
            for kk, vv in v.items():
                lines.append(f"{kk} = {_scalar(vv)}")
        else:
            lines.append(f"{k} = {_scalar(v)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _auth_path(lab: FakeLab) -> Path:
    return Path(lab.research_root).resolve() / "config" / "e0_authorization.toml"


def _make_lab(tmp_path: Path, portable: dict, local: dict | None = None,
              auth: dict | None = None, write_auth: bool = True,
              research_root: Path | None = None) -> FakeLab:
    cfg_path = tmp_path / "config" / "laboratory.toml"
    _write_toml(cfg_path, "e0", portable)
    local_path = None
    if local is not None:
        local_path = tmp_path / "config" / "laboratory.local.toml"
        _write_toml(local_path, "e0", local)
    root = research_root if research_root is not None else (tmp_path / "research")
    lab = FakeLab(cfg_path, local_path, root)
    if write_auth:
        _write_toml(_auth_path(lab), "e0_authorization",
                    auth if auth is not None else _default_auth())
    return lab


# ---------------------------------------------------------------------------
# Import the bridge module (trusted source extended by the candidate patch).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bridge_module():
    import agent_workspace.lab_bridge as mod

    return mod


# ---------------------------------------------------------------------------
# R2: operational [e0] config merge + type checking (no pins/acceptance here).
# ---------------------------------------------------------------------------


def test_e0_config_merges_portable_and_local(tmp_path, bridge_module, e0_cfg):
    local = {"nrg_source_root": "/tmp/specific/NRG"}
    lab = _make_lab(tmp_path, e0_cfg, local)
    merged = bridge_module._e0_config(lab)
    assert merged["nrg_source_root"] == "/tmp/specific/NRG"
    assert merged["equilibrium_executable"] == e0_cfg["equilibrium_executable"]


def test_e0_config_missing_key_raises(tmp_path, bridge_module, e0_cfg):
    e0_cfg.pop("cantera_python")
    lab = _make_lab(tmp_path, e0_cfg)
    with pytest.raises(RuntimeError, match="missing required keys"):
        bridge_module._e0_config(lab)


def test_e0_config_bad_parallel_jobs_raises(tmp_path, bridge_module, e0_cfg):
    e0_cfg["parallel_jobs"] = 0
    lab = _make_lab(tmp_path, e0_cfg)
    with pytest.raises(RuntimeError, match="parallel_jobs"):
        bridge_module._e0_config(lab)


def test_e0_config_bad_build_type_raises(tmp_path, bridge_module, e0_cfg):
    e0_cfg["build_type"] = "RelWithDebInfo"
    lab = _make_lab(tmp_path, e0_cfg)
    with pytest.raises(RuntimeError, match="build_type"):
        bridge_module._e0_config(lab)


def test_e0_config_ignores_pin_like_keys(tmp_path, bridge_module, e0_cfg):
    # [e0] may carry a pin-like key as a cross-check, but it is NEVER validated
    # as an authority here (R2); the operational config passes regardless.
    e0_cfg["pinned_commit"] = "deadbeef"
    lab = _make_lab(tmp_path, e0_cfg)
    merged = bridge_module._e0_config(lab)
    assert merged["pinned_commit"] == "deadbeef"  # retained, but not authoritative


# ---------------------------------------------------------------------------
# R2: protected authorization record load/validation (authoritative pins).
# ---------------------------------------------------------------------------


def test_e0_authorization_loads_valid_record(tmp_path, bridge_module, e0_cfg, e0_auth):
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    auth = bridge_module._e0_authorization(lab)
    assert auth["commit"] == _COMMIT
    assert auth["source_sha256"] == "a" * 64
    assert auth["acceptance"]["temperature_tolerance_K"] == 2.0


def test_e0_authorization_missing_record(tmp_path, bridge_module, e0_cfg):
    lab = _make_lab(tmp_path, e0_cfg, write_auth=False)
    with pytest.raises(RuntimeError, match="missing"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_symlink(tmp_path, bridge_module, e0_cfg):
    lab = _make_lab(tmp_path, e0_cfg, write_auth=False)
    ap = _auth_path(lab)
    ap.parent.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "real_auth.toml"
    real.write_text("[e0_authorization]\ncommit = \"deadbeef\"\n", encoding="utf-8")
    ap.symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_missing_pin(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth.pop("source_sha256")
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="missing pins"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_non_hex_source_sha(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["source_sha256"] = "not-a-real-sha"
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="64-char hex SHA-256"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_missing_config_sha(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth.pop("validation_config_sha256")
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="missing pins"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_non_hex_config_sha(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["validation_config_sha256"] = "not-a-sha"
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="64-char hex SHA-256"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_bad_commit(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["commit"] = "deadbeef"
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="40-hex git commit"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_wrong_selector(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["mechanism_selector"] = "hydrogen/OTHER.txt"
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="must equal"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_requires_acceptance_table(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth.pop("acceptance")
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="must be a table"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_missing_tolerance(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["acceptance"].pop("enthalpy_residual_tolerance")
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="missing required keys"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_unknown_tolerance(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["acceptance"]["made_up_tolerance"] = 1e-3
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="unapproved keys"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_nan_tolerance(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["acceptance"]["normalization_tolerance"] = "nan"
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="finite"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_negative_tolerance(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["acceptance"]["temperature_tolerance_K"] = -1.0
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="nonnegative"):
        bridge_module._e0_authorization(lab)


def test_e0_authorization_rejects_overlarge_tolerance(tmp_path, bridge_module, e0_cfg, e0_auth):
    e0_auth["acceptance"]["temperature_tolerance_K"] = 1e9
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)
    with pytest.raises(RuntimeError, match="exceeds permissible range"):
        bridge_module._e0_authorization(lab)


# ---------------------------------------------------------------------------
# R1: pre-resolution path safety (_e0_raw_path preserves raw components).
# ---------------------------------------------------------------------------


def test_e0_path_resolves_relative_against_research_root(tmp_path, bridge_module):
    lab = FakeLab(tmp_path / "cfg", None, tmp_path)
    cfg = {"build_root": "rel/build"}
    p = bridge_module._e0_path(cfg, "build_root", lab)
    assert p == (tmp_path / "rel" / "build").resolve()


def test_e0_raw_path_does_not_collapse(tmp_path, bridge_module):
    lab = FakeLab(tmp_path / "cfg", None, tmp_path)
    cfg = {"build_root": "rel/build"}
    raw = bridge_module._e0_raw_path(cfg, "build_root", lab)
    assert raw == (tmp_path / "rel" / "build")


def test_e0_path_rejects_dotdot_before_resolve(tmp_path, bridge_module):
    lab = FakeLab(tmp_path / "cfg", None, tmp_path)
    cfg = {"build_root": "rel/../escape"}
    with pytest.raises(RuntimeError, match="'..'"):
        bridge_module._e0_path(cfg, "build_root", lab)


# ---------------------------------------------------------------------------
# CMake selector/target pinning + expected bin/<CONFIG>/ layout.
# ---------------------------------------------------------------------------


def test_cmake_selector_and_target_are_pinned(bridge_module):
    assert bridge_module._E0_CMAKE_SELECTOR == _CMAKE_SELECTOR
    assert bridge_module._E0_CMAKE_TARGET == _CMAKE_TARGET


def test_exe_path_uses_bin_config_subdir(tmp_path, bridge_module, e0_cfg):
    lab = _make_lab(tmp_path, e0_cfg)
    exe = bridge_module._e0_exe_path(e0_cfg, lab)
    assert exe == (Path(e0_cfg["build_root"]) / "bin" / "Release" / e0_cfg["equilibrium_executable"]).resolve()
    e0_cfg["build_type"] = "debug"
    exe = bridge_module._e0_exe_path(e0_cfg, lab)
    assert exe == (Path(e0_cfg["build_root"]) / "bin" / "Debug" / e0_cfg["equilibrium_executable"]).resolve()


# ---------------------------------------------------------------------------
# Path containment + symlink/overlap rejection + boundaries (R1).
# ---------------------------------------------------------------------------


def test_ensure_within_rejects_escape(tmp_path, bridge_module):
    with pytest.raises(RuntimeError, match="escapes root"):
        bridge_module._ensure_within(tmp_path / "a" / ".." / "b", tmp_path / "a")


def test_reject_symlink_or_overlap_rejects_overlap(tmp_path, bridge_module):
    with pytest.raises(RuntimeError, match="overlap"):
        bridge_module._reject_symlink_or_overlap(tmp_path / "a", tmp_path / "a" / "b")


def test_reject_raw_symlink_or_dotdot_rejects_symlink(tmp_path, bridge_module):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        bridge_module._reject_raw_symlink_or_dotdot(link)


def test_reject_raw_symlink_or_dotdot_rejects_dotdot(tmp_path, bridge_module):
    with pytest.raises(RuntimeError, match="'..'"):
        bridge_module._reject_raw_symlink_or_dotdot(tmp_path / "a" / ".." / "b")


def test_boundaries_returns_derived_output_and_scratch(tmp_path, bridge_module, e0_cfg):
    lab = _make_lab(tmp_path, e0_cfg)
    b = bridge_module._e0_boundaries(e0_cfg, lab)
    assert set(b) == {"build_root", "run_root", "output_root", "scratch"}
    assert b["output_root"] == str((Path(e0_cfg["run_root"]) / "output").resolve())
    assert b["scratch"] == str((Path(e0_cfg["run_root"]) / "scratch").resolve())


def test_boundaries_reject_build_overlapping_research_root(tmp_path, bridge_module, e0_cfg):
    e0_cfg["build_root"] = str(tmp_path / "research" / "build")
    lab = _make_lab(tmp_path, e0_cfg)
    with pytest.raises(RuntimeError, match="overlaps trusted research_root"):
        bridge_module._e0_boundaries(e0_cfg, lab)


def test_boundaries_reject_build_overlapping_run(tmp_path, bridge_module, e0_cfg):
    e0_cfg["build_root"] = str(tmp_path / "same")
    e0_cfg["run_root"] = str(tmp_path / "same")
    lab = _make_lab(tmp_path, e0_cfg)
    with pytest.raises(RuntimeError, match="overlap"):
        bridge_module._e0_boundaries(e0_cfg, lab)


def test_boundaries_reject_symlink_run_root(tmp_path, bridge_module, e0_cfg):
    real = tmp_path / "real_run"
    real.mkdir()
    link = tmp_path / "run_link"
    link.symlink_to(real)
    e0_cfg["run_root"] = str(link)
    lab = _make_lab(tmp_path, e0_cfg)
    with pytest.raises(RuntimeError, match="symlink"):
        bridge_module._e0_boundaries(e0_cfg, lab)


# ---------------------------------------------------------------------------
# dirty/unpinned checkout fail-closed (mocked git).
# ---------------------------------------------------------------------------


def _prepare_nrg_checkout(e0_cfg):
    nrg = Path(e0_cfg["nrg_source_root"])
    (nrg / ".git").mkdir(parents=True)
    src = nrg / _SOURCE_REL
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(f"contains {_V3_MARKER}\n", encoding="utf-8")
    return nrg


def test_checkout_preflight_rejects_dirty(tmp_path, bridge_module, e0_cfg, e0_auth):
    _prepare_nrg_checkout(e0_cfg)

    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=e0_auth["commit"], stderr="")
        return types.SimpleNamespace(returncode=0, stdout=" M dirty.f90\n", stderr="")

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with mock.patch("agent_workspace.lab_bridge.sha256_file", return_value="a" * 64):
            with pytest.raises(RuntimeError, match="not clean"):
                bridge_module._e0_checkout_preflight(e0_cfg, e0_auth, _make_lab(tmp_path, e0_cfg))


def test_checkout_preflight_rejects_wrong_commit(tmp_path, bridge_module, e0_cfg, e0_auth):
    _prepare_nrg_checkout(e0_cfg)

    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="deadbeef", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="authorized commit"):
            bridge_module._e0_checkout_preflight(e0_cfg, e0_auth, _make_lab(tmp_path, e0_cfg))


def test_checkout_preflight_rejects_missing_v3_marker(tmp_path, bridge_module, e0_cfg, e0_auth):
    nrg = Path(e0_cfg["nrg_source_root"])
    (nrg / ".git").mkdir(parents=True)
    src = nrg / _SOURCE_REL
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("no marker here\n", encoding="utf-8")

    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=e0_auth["commit"], stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="V3"):
            bridge_module._e0_checkout_preflight(e0_cfg, e0_auth, _make_lab(tmp_path, e0_cfg))


def test_checkout_preflight_rejects_wrong_source_sha(tmp_path, bridge_module, e0_cfg, e0_auth):
    _prepare_nrg_checkout(e0_cfg)

    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=e0_auth["commit"], stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with mock.patch("agent_workspace.lab_bridge.sha256_file", return_value="d" * 64):
            with pytest.raises(RuntimeError, match="source_sha256"):
                bridge_module._e0_checkout_preflight(e0_cfg, e0_auth, _make_lab(tmp_path, e0_cfg))


# ---------------------------------------------------------------------------
# compiler discovery (fail on --version failure).
# ---------------------------------------------------------------------------


def test_discover_fortran_finds_gfortran(bridge_module):
    with mock.patch("agent_workspace.lab_bridge.subprocess.run") as run:
        run.return_value = types.SimpleNamespace(returncode=0, stdout="GNU Fortran 13.2.0\n", stderr="")
        cand, ver = bridge_module._e0_discover_fortran()
        assert cand == "gfortran"
        assert "13.2.0" in ver


def test_discover_fortran_skips_missing(bridge_module):
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd[0])
        if cmd[0] == "gfortran":
            raise FileNotFoundError
        return types.SimpleNamespace(returncode=0, stdout="GNU Fortran 13.2.0\n", stderr="")

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        cand, _ = bridge_module._e0_discover_fortran()
        assert cand == "gfortran-13"


def test_discover_fortran_fails_on_bad_version(bridge_module):
    def fake_run(cmd, *a, **k):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="compiler broke")

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="--version"):
            bridge_module._e0_discover_fortran()


def test_discover_fortran_no_compiler_raises(bridge_module):
    def fake_run(cmd, *a, **k):
        raise FileNotFoundError

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="no supported Fortran compiler"):
            bridge_module._e0_discover_fortran()


# ---------------------------------------------------------------------------
# staging byte-equivalence + six-mechanism manifest.
# ---------------------------------------------------------------------------


def _make_nrg_task_setup(tmp_path, e0_cfg):
    nrg = Path(e0_cfg["nrg_source_root"])
    task = nrg / "package_interface" / "task_setup"
    mech = task / "chemical_mechanisms"
    thermo = task / "thermophysical_data"
    thermo.mkdir(parents=True, exist_ok=True)
    for rel in (
        "hydrogen/KEROMNES.txt", "hydrogen/KONNOV.txt", "hydrogen/TEREZA.txt",
        "hydrogen/ZHANG.txt", "ammonia/OKAFOR.txt", "acetylene/VARATHARAJAN.txt",
    ):
        f = mech / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"MECH {rel}\n", encoding="utf-8")
    for name in ("KEROMNES_THERMO", "KONNOV_THERMO", "TEREZA_THERMO", "ZHANG_THERMO",
                 "OKAFOR_THERMO", "VARATHARAJAN_THERMO", "KEROMNES_TRANSDATA",
                 "KONNOV_TRANSDATA", "TEREZA_TRANSDATA", "ZHANG_TRANSDATA",
                 "OKAFOR_TRANSDATA", "VARATHARAJAN_TRANSDATA"):
        (thermo / name).write_text(f"DATA {name}\n", encoding="utf-8")
    src = nrg / "package_interface" / "src" / "tests" / "classic_tests" / "0D_chemical_equilibrium_validation.f90"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(f"! {_V3_MARKER}\n", encoding="utf-8")
    return nrg


def _build_commit_tree_records(nrg: Path, *, symlink=(), gitlink=(), omit=(), extra=()):
    """Build (mode, oid, path) records + blob map for the on-disk task_setup and
    the single V3 source, standing in for the PINNED COMMIT tree (authority =
    commit, not the index or worktree).  'extra' entries are (mode, oid, path)
    tuples appended verbatim; symlink/gitlink are names to mark 120000/160000."""
    task_rel = "package_interface/task_setup"
    records: list[tuple[str, str, str]] = []
    blob_by_oid: dict[str, bytes] = {}
    for f in sorted(nrg.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(nrg).as_posix()
        if not (rel.startswith(task_rel + "/") or rel == _SOURCE_REL):
            continue
        if rel in omit or Path(rel).name in omit:
            continue
        data = f.read_bytes()
        oid = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        mode = ("120000" if (rel in symlink or Path(rel).name in symlink)
                else "160000" if (rel in gitlink or Path(rel).name in gitlink)
                else "100644")
        records.append((mode, oid, rel))
        blob_by_oid[oid] = data
    for mode, oid, path in extra:
        records.append((mode, oid, path))
        blob_by_oid.setdefault(oid, b"EXTRA")
    return records, blob_by_oid


def _commit_git_runner(nrg: Path, commit: str, records, blob_by_oid, *, cat_fail=()):
    """subprocess.run side_effect for 'git ls-tree -r -z <commit> -- <rel>' and
    'git cat-file blob <oid>' (plus rev-parse/status pass-through for preflight)."""

    def fake_run(cmd, *a, **k):
        args = list(cmd)
        if "ls-tree" in args:
            sep = args.index("--")
            pathspecs = [spec.rstrip("/") for spec in args[sep + 1:]]
            out = []
            for mode, oid, path in records:
                if any(path == spec or path.startswith(spec + "/") for spec in pathspecs):
                    out.append(f"{mode} blob {oid}\t{path}".encode())
            return types.SimpleNamespace(returncode=0, stdout=b"\0".join(out) + b"\0", stderr=b"")
        if "cat-file" in args:
            oid = args[-1]
            if oid in cat_fail:
                return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"missing object")
            blob = blob_by_oid.get(oid, b"")
            if "-s" in args:
                return types.SimpleNamespace(returncode=0, stdout=f"{len(blob)}\n".encode(), stderr=b"")
            return types.SimpleNamespace(returncode=0, stdout=blob, stderr=b"")
        if "rev-parse" in args:
            return types.SimpleNamespace(returncode=0, stdout=commit, stderr="")
        if "status" in args:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def test_stage_task_setup_completes_six_mechanisms(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg)
    auth = _default_auth()
    auth["source_sha256"] = hashlib.sha256((nrg / _SOURCE_REL).read_bytes()).hexdigest()
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        staged = bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)
    files = staged["staged_files"]
    assert _SOURCE_REL in files
    mechanism_relpaths = {
        "hydrogen/KEROMNES.txt",
        "hydrogen/KONNOV.txt",
        "hydrogen/TEREZA.txt",
        "hydrogen/ZHANG.txt",
        "ammonia/OKAFOR.txt",
        "acetylene/VARATHARAJAN.txt",
    }
    mech_prefix = "package_interface/task_setup/chemical_mechanisms/"
    assert {k[len(mech_prefix):] for k in files if k.startswith(mech_prefix)} == mechanism_relpaths
    assert staged["staged_input_manifest_sha256"]
    manifest = json.loads((scratch / "staged_input_manifest.json").read_text())
    assert manifest["approved_commit"] == auth["commit"]
    assert len(manifest["files"]) == len(files)
    manifest_path = scratch / "staged_input_manifest.json"
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == staged["staged_input_manifest_sha256"]


def test_stage_task_setup_uses_commit_bytes_not_worktree(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg)  # captures commit bytes
    # Mutate the working tree AFTER the commit tree was captured.
    mech = nrg / "package_interface" / "task_setup" / "chemical_mechanisms" / "hydrogen" / "KEROMNES.txt"
    mech.write_text("TAMPERED WORKTREE\n", encoding="utf-8")
    auth = _default_auth()
    auth["source_sha256"] = hashlib.sha256((nrg / _SOURCE_REL).read_bytes()).hexdigest()
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        staged = bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)
    staged_mech = scratch / "package_interface" / "task_setup" / "chemical_mechanisms" / "hydrogen" / "KEROMNES.txt"
    assert staged_mech.read_text() == "MECH hydrogen/KEROMNES.txt\n"


def test_stage_task_setup_ignores_changed_index_and_extra_worktree(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg)  # commit tree is fixed
    # Files that would be in a changed index / dirty worktree but NOT the commit.
    (nrg / "package_interface" / "task_setup" / "chemical_mechanisms" / "hydrogen" / "INDEXED_EXTRA.txt").write_text("x\n", encoding="utf-8")
    (nrg / "package_interface" / "task_setup" / "UNTRACKED.txt").write_text("y\n", encoding="utf-8")
    auth = _default_auth()
    auth["source_sha256"] = hashlib.sha256((nrg / _SOURCE_REL).read_bytes()).hexdigest()
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        staged = bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)
    files = staged["staged_files"]
    assert not any("INDEXED_EXTRA.txt" in k or "UNTRACKED.txt" in k for k in files)


def test_stage_task_setup_manifest_is_deterministic(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    auth = _default_auth()
    auth["source_sha256"] = hashlib.sha256((nrg / _SOURCE_REL).read_bytes()).hexdigest()
    records, blobs = _build_commit_tree_records(nrg)
    digests = []
    for i in (0, 1):
        scratch = tmp_path / "run" / f"scratch{i}"
        with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                        side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
            staged = bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)
        digests.append(staged["staged_input_manifest_sha256"])
    assert digests[0] == digests[1]


def test_stage_task_setup_rejects_incomplete_manifest(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(
        nrg, omit=("KONNOV.txt", "TEREZA.txt", "ZHANG.txt", "OKAFOR.txt", "VARATHARAJAN.txt"))
    auth = _default_auth()
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        with pytest.raises(RuntimeError, match="manifest incomplete"):
            bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


# ---------------------------------------------------------------------------
# independent checks against the real CSV contract (R4/R5/R6).
# ---------------------------------------------------------------------------


def _default_residuals(mode: str) -> dict[str, float]:
    return {
        "max_element_relative_residual": 1e-15,
        "mass_relative_residual": 0.0,
        "mole_fraction_sum_residual": 1e-16,
        "stationarity_residual": 0.0,
        "enthalpy_relative_residual": 0.0 if mode == "tp" else 9.644228646507566e-11,
    }


def _write_state(case_dir: Path, mode: str, species: list[str], mole_fractions: list[float]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["species", "mole_number", "mole_fraction", "mass_fraction", "molar_mass_kg_per_mol"])
    for sp, x in zip(species, mole_fractions):
        w.writerow([sp, f"{x:E}", f"{x:E}", f"{x:E}", "1.0E-3"])
    (case_dir / f"nrg_{mode}_{mode}_state.csv").write_text(buf.getvalue(), encoding="utf-8")


def _write_diagnostics(case_dir: Path, mode: str, converged: bool = True, **residuals: float) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["key", "value"])
    w.writerow(["converged", "T" if converged else "F"])
    w.writerow(["iterations", "6"])
    w.writerow(["temperature_K", "1500.0" if mode == "tp" else "2387.6248666734637"])
    w.writerow(["pressure_Pa", "101325.0"])
    w.writerow(["total_moles", "0.86"])
    defaults = _default_residuals(mode)
    defaults.update(residuals)
    for k in ("max_element_relative_residual", "mass_relative_residual",
              "mole_fraction_sum_residual", "stationarity_residual", "enthalpy_relative_residual"):
        w.writerow([k, repr(defaults[k])])
    (case_dir / f"nrg_{mode}_{mode}_diagnostics.csv").write_text(buf.getvalue(), encoding="utf-8")


def _write_case(tmp_path, case_dir, status="PASS", mode="full", converged=True,
                mole_fractions=(0.5, 0.5), residuals=None, errors=None):
    case_dir.mkdir(parents=True, exist_ok=True)
    species = [f"S{i}" for i in range(len(mole_fractions))]
    for m in ("tp", "hp"):
        _write_state(case_dir, m, species, mole_fractions)
        _write_diagnostics(case_dir, m, converged=converged, **(residuals or {}))
    eq = {}
    for m in ("tp", "hp"):
        r = _default_residuals(m)
        r.update(residuals or {})
        eq[m] = {
            "converged": converged,
            "max_absolute_mole_fraction_error": (errors or {}).get("mole", 0.0),
            "temperature_error_K": (errors or {}).get("temp", 0.0),
            "nrg_temperature_K": 1500.0 if m == "tp" else 2387.6248666734637,
            "nrg_diagnostics": r,
        }
    rec = {"mechanism": "hydrogen/KEROMNES.txt", "validation_mode": mode, "status": status,
           "reason": "ok" if status == "PASS" else "failed", "comparisons": {"equilibrium": eq}}
    (case_dir / "result.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


_ACCEPTANCE = {"normalization_tolerance": 1e-6, "nonnegativity_tolerance": 1e-12,
               "element_conservation_tolerance": 1e-7, "mass_conservation_tolerance": 1e-7,
               "enthalpy_residual_tolerance": 1e-9, "mole_fraction_tolerance": 5e-4,
               "temperature_tolerance_K": 2.0}


def test_independent_checks_pass_on_clean_result(tmp_path, bridge_module):
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir)
    assert bridge_module._e0_independent_checks(rec, _ACCEPTANCE, case_dir) == []


def test_independent_checks_catch_non_normalized(tmp_path, bridge_module):
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir, mole_fractions=(0.4, 0.4))
    issues = bridge_module._e0_independent_checks(rec, _ACCEPTANCE, case_dir)
    assert any("not normalized" in i for i in issues)


def test_independent_checks_catch_negative_mole_fraction(tmp_path, bridge_module):
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir, mole_fractions=(1.0, -1e-3))
    issues = bridge_module._e0_independent_checks(rec, _ACCEPTANCE, case_dir)
    assert any("negative" in i for i in issues)


def test_independent_checks_reject_not_full_mode(tmp_path, bridge_module):
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir, mode="input")
    issues = bridge_module._e0_independent_checks(rec, {}, case_dir)
    assert issues == ["not full validation mode"]


def test_independent_checks_reject_unconverged(tmp_path, bridge_module):
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir, converged=False)
    issues = bridge_module._e0_independent_checks(rec, _ACCEPTANCE, case_dir)
    assert any("did not converge" in i for i in issues)


def test_independent_checks_catch_corrupted_hp_enthalpy(tmp_path, bridge_module):
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir, residuals={"enthalpy_relative_residual": 1.0e-3})
    issues = bridge_module._e0_independent_checks(rec, _ACCEPTANCE, case_dir)
    assert any("enthalpy" in i and "residual" in i for i in issues)


def test_independent_checks_reject_nan_mole_fraction(tmp_path, bridge_module):
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir, mole_fractions=(float("nan"), 0.5))
    with pytest.raises(RuntimeError, match="non-finite"):
        bridge_module._e0_independent_checks(rec, _ACCEPTANCE, case_dir)


def test_independent_checks_reject_missing_nrg_temperature(tmp_path, bridge_module):
    # R4: missing nrg_temperature_K must fail closed, not default.
    case_dir = tmp_path / "out" / "01_KEROMNES"
    rec = _write_case(tmp_path, case_dir)
    rec["comparisons"]["equilibrium"]["tp"].pop("nrg_temperature_K")
    issues = bridge_module._e0_independent_checks(rec, _ACCEPTANCE, case_dir)
    assert any("missing nrg_temperature_K" in i for i in issues)


# --- Negative tests: negative conservation residual / duplicate CSV key ------


def test_read_diagnostics_rejects_negative_residual(tmp_path, bridge_module):
    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True)
    _write_diagnostics(case_dir, "hp", max_element_relative_residual=-1.0e-5)
    with pytest.raises(RuntimeError, match="negative"):
        bridge_module._e0_read_diagnostics(case_dir, "hp")


def test_read_diagnostics_rejects_duplicate_key(tmp_path, bridge_module):
    case_dir = tmp_path / "case"
    case_dir.mkdir(parents=True)
    lines = [
        "key,value",
        "converged,T",
        "iterations,6",
        "temperature_K,2387.6248666734637",
        "pressure_Pa,101325.0",
        "total_moles,0.86",
        "max_element_relative_residual,1.0E-15",
        "mass_relative_residual,0.0",
        "mole_fraction_sum_residual,1.0E-16",
        "stationarity_residual,0.0",
        "enthalpy_relative_residual,9.644228646507566E-11",
        "enthalpy_relative_residual,9.644228646507566E-11",  # duplicate
    ]
    (case_dir / "nrg_hp_hp_diagnostics.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="duplicate"):
        bridge_module._e0_read_diagnostics(case_dir, "hp")


# ---------------------------------------------------------------------------
# build identity cross-check rejects an unapproved manifest (R2 signature).
# ---------------------------------------------------------------------------


def test_verify_build_identity_rejects_wrong_target(tmp_path, bridge_module, e0_cfg, e0_auth):
    build = Path(e0_cfg["build_root"])
    build.mkdir(parents=True, exist_ok=True)
    (build / "build_manifest.json").write_text(json.dumps({
        "pinned_commit": e0_auth["commit"],
        "pinned_source_sha256": "a" * 64,
        "cmake_selector": _CMAKE_SELECTOR,
        "cmake_target": "WRONG_TARGET",
        "source_relative": _SOURCE_REL,
        "build_type": "release",
        "executable_sha256": "exe",
    }), encoding="utf-8")
    lab = _make_lab(tmp_path, e0_cfg)
    preflight = {"source_sha256": "a" * 64}
    exe = build / "bin" / "Release" / e0_cfg["equilibrium_executable"]
    with pytest.raises(RuntimeError, match="cmake_target"):
        bridge_module._e0_verify_build_identity(e0_cfg, e0_auth, lab, preflight, exe, "exe")


# ---------------------------------------------------------------------------
# archived fixtures: independent checks on the real KEROMNES CSV/JSON contract.
# ---------------------------------------------------------------------------


def test_independent_checks_pass_on_archived_keromnes(bridge_module):
    result = json.loads((_FIXTURES / "result.json").read_text(encoding="utf-8"))
    issues = bridge_module._e0_independent_checks(result, _ACCEPTANCE, _FIXTURES)
    assert issues == []


def test_independent_checks_fail_on_corrupted_archived_hp_enthalpy(tmp_path, bridge_module):
    case_dir = tmp_path / "keromnes_corrupt"
    case_dir.mkdir(parents=True)
    for name in ("nrg_tp_tp_state.csv", "nrg_hp_hp_state.csv",
                 "nrg_tp_tp_diagnostics.csv", "nrg_hp_hp_diagnostics.csv", "result.json"):
        (case_dir / name).write_text((_FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    hp_diag = (case_dir / "nrg_hp_hp_diagnostics.csv").read_text(encoding="utf-8")
    hp_diag = hp_diag.replace("enthalpy_relative_residual, 9.6442286465075663E-011",
                              "enthalpy_relative_residual, 1.0000000000000000E-003")
    (case_dir / "nrg_hp_hp_diagnostics.csv").write_text(hp_diag, encoding="utf-8")
    result = json.loads((case_dir / "result.json").read_text(encoding="utf-8"))
    issues = bridge_module._e0_independent_checks(result, _ACCEPTANCE, case_dir)
    assert any("enthalpy" in i and "residual" in i for i in issues)


# ---------------------------------------------------------------------------
# validation_config.json pinning (validation-config round): resolve + authenticate.
# ---------------------------------------------------------------------------


def _write_validation_config(e0_cfg: dict) -> Path:
    harness = Path(e0_cfg["harness"])
    config = harness.parent.parent / "validation_config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"mixture": "H2/O2/Ar", "pressure_Pa": 101325.0}\n', encoding="utf-8")
    return config


def test_validation_config_resolves_and_verifies(tmp_path, bridge_module, e0_cfg, e0_auth):
    config = _write_validation_config(e0_cfg)
    harness = Path(e0_cfg["harness"])
    with mock.patch("agent_workspace.lab_bridge.sha256_file", return_value="c" * 64):
        resolved = bridge_module._e0_validation_config(harness, e0_auth)
    assert resolved == config


def test_validation_config_rejects_absent(tmp_path, bridge_module, e0_cfg, e0_auth):
    harness = Path(e0_cfg["harness"])
    with pytest.raises(RuntimeError, match="missing"):
        bridge_module._e0_validation_config(harness, e0_auth)


def test_validation_config_rejects_symlink(tmp_path, bridge_module, e0_cfg, e0_auth):
    harness = Path(e0_cfg["harness"])
    config = harness.parent.parent / "validation_config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "real_config.json"
    real.write_text("{}\n", encoding="utf-8")
    config.symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        bridge_module._e0_validation_config(harness, e0_auth)


def test_validation_config_rejects_hash_mismatch(tmp_path, bridge_module, e0_cfg, e0_auth):
    _write_validation_config(e0_cfg)
    harness = Path(e0_cfg["harness"])
    with mock.patch("agent_workspace.lab_bridge.sha256_file", return_value="d" * 64):
        with pytest.raises(RuntimeError, match="does not match"):
            bridge_module._e0_validation_config(harness, e0_auth)


# ---------------------------------------------------------------------------
# _e0_parse_results strictness: exactly one KEROMNES record + complete TP/HP.
# ---------------------------------------------------------------------------


def _write_summary(out: Path, records: list) -> None:
    (out / "summary.json").write_text(json.dumps(records), encoding="utf-8")


def test_parse_results_accepts_single_record(tmp_path, bridge_module):
    out = tmp_path / "out"
    case = out / "01_KEROMNES"
    rec = _write_case(tmp_path, case)
    _write_summary(out, [{"mechanism": "hydrogen/KEROMNES.txt", "status": rec["status"]}])
    parsed = bridge_module._e0_parse_results(out, "hydrogen/KEROMNES.txt")
    assert parsed["summary_entry"]["mechanism"] == "hydrogen/KEROMNES.txt"


def test_parse_results_rejects_duplicate_record(tmp_path, bridge_module):
    out = tmp_path / "out"
    case = out / "01_KEROMNES"
    rec = _write_case(tmp_path, case)
    _write_summary(out, [
        {"mechanism": "hydrogen/KEROMNES.txt", "status": rec["status"]},
        {"mechanism": "hydrogen/KEROMNES.txt", "status": rec["status"]},
    ])
    with pytest.raises(RuntimeError, match="duplicate"):
        bridge_module._e0_parse_results(out, "hydrogen/KEROMNES.txt")


def test_parse_results_rejects_extra_mechanism(tmp_path, bridge_module):
    out = tmp_path / "out"
    case = out / "01_KEROMNES"
    rec = _write_case(tmp_path, case)
    _write_summary(out, [
        {"mechanism": "hydrogen/KEROMNES.txt", "status": rec["status"]},
        {"mechanism": "hydrogen/KONNOV.txt", "status": "PASS"},
    ])
    with pytest.raises(RuntimeError, match="unexpected mechanism"):
        bridge_module._e0_parse_results(out, "hydrogen/KEROMNES.txt")


def test_parse_results_rejects_missing_tp(tmp_path, bridge_module):
    out = tmp_path / "out"
    case = out / "01_KEROMNES"
    rec = _write_case(tmp_path, case)
    rec["comparisons"]["equilibrium"].pop("tp")
    (case / "result.json").write_text(json.dumps(rec), encoding="utf-8")
    _write_summary(out, [{"mechanism": "hydrogen/KEROMNES.txt", "status": rec["status"]}])
    with pytest.raises(RuntimeError, match="equilibrium.tp"):
        bridge_module._e0_parse_results(out, "hydrogen/KEROMNES.txt")


def test_parse_results_rejects_missing_hp(tmp_path, bridge_module):
    out = tmp_path / "out"
    case = out / "01_KEROMNES"
    rec = _write_case(tmp_path, case)
    rec["comparisons"]["equilibrium"].pop("hp")
    (case / "result.json").write_text(json.dumps(rec), encoding="utf-8")
    _write_summary(out, [{"mechanism": "hydrogen/KEROMNES.txt", "status": rec["status"]}])
    with pytest.raises(RuntimeError, match="equilibrium.hp"):
        bridge_module._e0_parse_results(out, "hydrogen/KEROMNES.txt")


def test_parse_results_rejects_status_mismatch(tmp_path, bridge_module):
    out = tmp_path / "out"
    case = out / "01_KEROMNES"
    rec = _write_case(tmp_path, case, status="PASS")
    _write_summary(out, [{"mechanism": "hydrogen/KEROMNES.txt", "status": "FAIL"}])
    with pytest.raises(RuntimeError, match="status mismatch"):
        bridge_module._e0_parse_results(out, "hydrogen/KEROMNES.txt")


# ---------------------------------------------------------------------------
# validate_execute fails closed on harness non-PASS despite zero return code.
# ---------------------------------------------------------------------------


def test_validate_execute_rejects_zero_rc_with_fail_status(tmp_path, bridge_module, e0_cfg, e0_auth):
    _make_nrg_task_setup(tmp_path, e0_cfg)
    _prepare_nrg_checkout(e0_cfg)
    build = Path(e0_cfg["build_root"])
    exe = build / "bin" / "Release" / e0_cfg["equilibrium_executable"]
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"\x00")
    (build / "build_manifest.json").write_text(json.dumps({
        "pinned_commit": e0_auth["commit"],
        "pinned_source_sha256": "a" * 64,
        "source_relative": _SOURCE_REL,
        "cmake_selector": _CMAKE_SELECTOR,
        "cmake_target": _CMAKE_TARGET,
        "build_type": "release",
        "executable_sha256": "exe",
    }), encoding="utf-8")
    harness = Path(e0_cfg["harness"])
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text("# harness\n", encoding="utf-8")
    config = harness.parent.parent / "validation_config.json"
    config.write_text('{"mixture": "H2/O2/Ar"}\n', encoding="utf-8")
    python = Path(e0_cfg["cantera_python"])
    python.write_bytes(b"#!/bin/sh\n")
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)

    def fake_git(cmd, *a, **k):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=e0_auth["commit"], stderr="")
        if "status" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run_bounded(cmd, label, timeout, log_path):
        # Zero return code but the harness writes a FAIL screening verdict.
        out = Path(cmd[cmd.index("--output") + 1])
        case = out / "01_KEROMNES"
        case.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps([
            {"mechanism": "hydrogen/KEROMNES.txt", "status": "FAIL", "reason": "screening failed"},
        ]), encoding="utf-8")
        (case / "result.json").write_text(json.dumps({
            "mechanism": "hydrogen/KEROMNES.txt", "validation_mode": "full",
            "status": "FAIL", "reason": "screening failed",
        }), encoding="utf-8")
        return ""

    def fake_sha(p):
        p = Path(p)
        if p.name == e0_cfg["equilibrium_executable"]:
            return "exe"
        if p.name == "run_validation.py":
            return "b" * 64
        if p.name == "0D_chemical_equilibrium_validation.f90":
            return "a" * 64
        if p.name == "validation_config.json":
            return "c" * 64
        if p.name == "validation_config.snapshot.json":
            return "c" * 64
        if p.name == "python":
            return "py"
        return "x"

    args = types.SimpleNamespace(
        expected_executable_sha256="exe", expected_harness_sha256="b" * 64,
        expected_validation_config_sha256="c" * 64, expected_cantera_python_sha256="py",
    )

    with mock.patch("agent_workspace.lab_bridge.sha256_file", side_effect=fake_sha):
        with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_git):
            with mock.patch("agent_workspace.lab_bridge._e0_stage_task_setup",
                            return_value={"staged_files": {}, "staged_input_manifest_sha256": "d" * 64}):
                with mock.patch("agent_workspace.lab_bridge._run_bounded", side_effect=fake_run_bounded):
                    rc = bridge_module.cmd_e0_validate_execute(args, lab)
    assert rc == 2  # emitted scientific_pass: False


def test_validate_execute_rejects_missing_plan_binding(tmp_path, bridge_module, e0_cfg, e0_auth):
    # Correction 6: plan->execute binding is mandatory; a missing identity field
    # must fail closed (rc 2) before the harness is launched.
    _make_nrg_task_setup(tmp_path, e0_cfg)
    _prepare_nrg_checkout(e0_cfg)
    build = Path(e0_cfg["build_root"])
    exe = build / "bin" / "Release" / e0_cfg["equilibrium_executable"]
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"\x00")
    harness = Path(e0_cfg["harness"])
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text("# harness\n", encoding="utf-8")
    lab = _make_lab(tmp_path, e0_cfg, auth=e0_auth)

    def fake_git(cmd, *a, **k):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=e0_auth["commit"], stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_sha(p):
        p = Path(p)
        if p.name == "run_validation.py":
            return "b" * 64
        if p.name == "0D_chemical_equilibrium_validation.f90":
            return "a" * 64
        return "exe"

    args = types.SimpleNamespace(expected_executable_sha256=None, expected_harness_sha256=None)
    with mock.patch("agent_workspace.lab_bridge.sha256_file", side_effect=fake_sha):
        with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_git):
            with mock.patch("agent_workspace.lab_bridge._run_bounded") as bounded:
                rc = bridge_module.cmd_e0_validate_execute(args, lab)
    bounded.assert_not_called()
    assert rc == 2


# ---------------------------------------------------------------------------
# Correction 5: non-dictionary summary.json records are rejected (not skipped).
# ---------------------------------------------------------------------------


def test_parse_results_rejects_non_dict_record(tmp_path, bridge_module):
    out = tmp_path / "out"
    case = out / "01_KEROMNES"
    rec = _write_case(tmp_path, case)
    _write_summary(out, [
        {"mechanism": "hydrogen/KEROMNES.txt", "status": rec["status"]},
        "not-a-dict",
    ])
    with pytest.raises(RuntimeError, match="non-dictionary"):
        bridge_module._e0_parse_results(out, "hydrogen/KEROMNES.txt")


# ---------------------------------------------------------------------------
# Correction C6-A: commit-pinned input staging rejects symlinks/gitlinks/
# traversal paths/absent objects and stages only the approved commit tree.
# ---------------------------------------------------------------------------


def test_stage_task_setup_rejects_symlink(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg, symlink=("KEROMNES.txt",))
    auth = _default_auth()
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        with pytest.raises(RuntimeError, match="symlink"):
            bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


def test_stage_task_setup_rejects_gitlink(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg, gitlink=("KEROMNES.txt",))
    auth = _default_auth()
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        with pytest.raises(RuntimeError, match="gitlink"):
            bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


def test_stage_task_setup_rejects_traversal_entry(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    auth = _default_auth()

    def fake_run(cmd, *a, **k):
        args = list(cmd)
        if "ls-tree" in args:
            rec = b"100644 blob 0000000000000000000000000000000000000000\t../evil.txt\0"
            return types.SimpleNamespace(returncode=0, stdout=rec, stderr=b"")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="unsafe Git-tree path"):
            bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


def test_stage_task_setup_rejects_extra_source_entry(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg)
    auth = _default_auth()
    base = _commit_git_runner(nrg, auth["commit"], records, blobs)

    def fake_run(cmd, *a, **k):
        args = list(cmd)
        if "ls-tree" in args and args[-1] == _SOURCE_REL:
            rec = (f"100644 blob {'0' * 40}\t{_SOURCE_REL}\0"
                   f"100644 blob {'1' * 40}\t{_SOURCE_REL}.evil\0")
            return types.SimpleNamespace(returncode=0, stdout=rec.encode(), stderr=b"")
        return base(cmd, *a, **k)

    with mock.patch("agent_workspace.lab_bridge.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="unexpected approved-commit path"):
            bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


def test_stage_task_setup_rejects_blob_data_mismatch(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg)
    auth = _default_auth()
    # Pinned V3 source verification hashes the fetched blob directly, so the
    # source pin must be genuine here or it fails before staging integrity.
    auth["source_sha256"] = hashlib.sha256((nrg / _SOURCE_REL).read_bytes()).hexdigest()
    real_sha256_file = bridge_module.sha256_file
    target_suffix = "/chemical_mechanisms/hydrogen/KEROMNES.txt"

    def selective_sha(path):
        if str(path).endswith(target_suffix):
            return "0" * 64
        return real_sha256_file(path)

    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        # Corrupt only the KEROMNES mechanism re-hash; the pinned V3 source
        # check and all other staged resources remain genuine.
        with mock.patch("agent_workspace.lab_bridge.sha256_file", side_effect=selective_sha):
            with pytest.raises(RuntimeError, match="staging mismatch"):
                bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


def test_stage_task_setup_rejects_source_sha_mismatch(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg)
    auth = _default_auth()  # source_sha256 = "a"*64 (deliberately wrong)
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs)):
        with pytest.raises(RuntimeError, match="source SHA-256 mismatch"):
            bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


def test_stage_task_setup_rejects_absent_object(tmp_path, bridge_module, e0_cfg):
    nrg = _make_nrg_task_setup(tmp_path, e0_cfg)
    lab = _make_lab(tmp_path, e0_cfg)
    scratch = tmp_path / "run" / "scratch"
    records, blobs = _build_commit_tree_records(nrg)
    auth = _default_auth()
    first_oid = records[0][1]
    with mock.patch("agent_workspace.lab_bridge.subprocess.run",
                    side_effect=_commit_git_runner(nrg, auth["commit"], records, blobs, cat_fail=(first_oid,))):
        with pytest.raises(RuntimeError, match="unavailable"):
            bridge_module._e0_stage_task_setup(e0_cfg, auth, lab, scratch)


# ---------------------------------------------------------------------------
# Correction 4: total deadline + cleanup margin; inert process-tree teardown.
# ---------------------------------------------------------------------------


def test_deadline_rejects_insufficient_margin(bridge_module):
    with pytest.raises(RuntimeError, match="cleanup margin"):
        bridge_module._e0_deadline({"timeout_seconds": 1800})


def test_remaining_rejects_exceeded_deadline(bridge_module):
    with pytest.raises(RuntimeError, match="deadline exceeded"):
        bridge_module._e0_remaining(-1.0)


def test_run_bounded_kills_inert_process_tree_on_timeout(tmp_path, bridge_module):
    # Inert tree: child + grandchild write their PIDs, then sleep.  No NRG.
    import json
    import sys
    import time
    pidfile = tmp_path / "inert_tree.json"
    child = (
        "import json, os, pathlib, subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pidfile)!r}).write_text("
        f"json.dumps({{'child': os.getpid(), 'grandchild': g.pid}}))\n"
        "time.sleep(60)\n"
    )
    log = tmp_path / "inert_tree.log"
    with pytest.raises(RuntimeError, match="timed out"):
        bridge_module._run_bounded([sys.executable, "-c", child], "inert-tree", 1, log)
    text = log.read_text()
    assert "# result: TIMEOUT" in text
    assert "# cancellation_signals: 0" in text
    pids = json.loads(pidfile.read_text())

    def alive(pid: int) -> bool:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return False
        return stat.rsplit(") ", 1)[1].split()[0] != "Z"

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in pids.values()):
            return
        time.sleep(0.05)
    pytest.fail(f"inert process tree survived timeout: {pids}")


# ---------------------------------------------------------------------------
# Correction C6-B: external SIGTERM cancellation kills the whole process tree
# and records CANCELLED (never a PASS); the handler only sets a flag.
# ---------------------------------------------------------------------------


def test_run_bounded_prelaunch_sigterm_never_launches_child(tmp_path, bridge_module):
    # Inject SIGTERM at handler registration; no real process or signal.
    import signal
    installed = {"count": 0}

    def fake_signal(signum, handler):
        if callable(handler) and installed["count"] == 0:
            installed["count"] += 1
            handler(signum, None)
        return signal.SIG_DFL

    with mock.patch.object(bridge_module.signal, "signal", side_effect=fake_signal):
        with mock.patch.object(bridge_module.subprocess, "Popen") as spawn:
            with pytest.raises(RuntimeError, match="cancelled before launch"):
                bridge_module._run_bounded(["inert"], "prelaunch", 2, tmp_path / "prelaunch.log")
    spawn.assert_not_called()
    assert "CANCELLED" in (tmp_path / "prelaunch.log").read_text()


def test_run_bounded_repeated_sigterm_is_idempotent_and_kills_group(tmp_path, bridge_module):
    # Two handler invocations during mocked Popen: one group kill, CANCELLED log.
    import signal
    installed = {"handler": None}

    def fake_signal(signum, handler):
        if callable(handler):
            installed["handler"] = handler
        return signal.SIG_DFL

    fakeproc = mock.Mock(pid=123450)
    fakeproc.communicate.return_value = ("", "")
    fakeproc.returncode = 0

    def spawn_with_cancel(*args, **kwargs):
        installed["handler"](signal.SIGTERM, None)
        installed["handler"](signal.SIGTERM, None)
        return fakeproc

    with mock.patch.object(bridge_module.signal, "signal", side_effect=fake_signal):
        with mock.patch.object(bridge_module.subprocess, "Popen", side_effect=spawn_with_cancel):
            with mock.patch.object(bridge_module, "_kill_process_group") as killer:
                with pytest.raises(RuntimeError, match="external SIGTERM"):
                    bridge_module._run_bounded(["inert"], "repeat-sigterm", 2, tmp_path / "repeat.log")
    killer.assert_called_once_with(fakeproc)
    log = (tmp_path / "repeat.log").read_text()
    assert "# result: CANCELLED" in log
    assert "cancellation_signals: 2" in log


def test_run_bounded_external_sigterm_cancels_and_kills_tree(tmp_path, bridge_module):
    # Integration: a separate bridge process runs an inert child tree under
    # _run_bounded (which installs the SIGTERM handler).  Signaling that bridge
    # PID with SIGTERM must cancel the operation, kill the child tree, record
    # CANCELLED, and exit non-zero.  Never launches NRG, cmake, or Cantera.
    import os
    import signal as _signal
    import subprocess as _sp
    import sys
    import time
    repo_root = Path(__file__).resolve().parents[1]
    log = tmp_path / "sigterm.log"
    pidfile = tmp_path / "tree.pid"
    inner = (
        "import json, os, pathlib, subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pidfile)!r}).write_text("
        f"json.dumps({{'child': os.getpid(), 'grandchild': g.pid}}))\n"
        "time.sleep(60)"
    )
    script = (
        "import agent_workspace.lab_bridge as b, pathlib, sys\n"
        f"log = pathlib.Path({str(log)!r})\n"
        f"inner = {inner!r}\n"
        "cmd = [sys.executable, '-c', inner]\n"
        "try:\n"
        "    b._run_bounded(cmd, 'sigterm-inert', 60, log)\n"
        "    sys.exit(0)\n"
        "except RuntimeError:\n"
        "    sys.exit(3)\n"
    )
    proc = _sp.Popen([sys.executable, "-c", script], stdout=_sp.PIPE, stderr=_sp.PIPE, cwd=str(repo_root))
    try:
        for _ in range(100):
            if pidfile.exists():
                break
            time.sleep(0.1)
        assert pidfile.exists(), "inert child never started"
        child_pids = json.loads(pidfile.read_text())
        proc.send_signal(_signal.SIGTERM)
        proc.communicate(timeout=15)
        assert proc.returncode != 0
        assert "CANCELLED" in log.read_text()

        def alive(pid: int) -> bool:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except FileNotFoundError:
                return False
            return stat.rsplit(") ", 1)[1].split()[0] != "Z"

        for _ in range(50):
            if not any(alive(pid) for pid in child_pids.values()):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"inert process tree survived SIGTERM cancellation: {child_pids}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


# ---------------------------------------------------------------------------
# Correction 2: run-owned validation_config snapshot; correction 3: interpreter.
# ---------------------------------------------------------------------------


def test_validation_config_snapshot_hash_verified(tmp_path, bridge_module, e0_cfg, e0_auth):
    config = _write_validation_config(e0_cfg)
    harness = Path(e0_cfg["harness"])
    scratch = tmp_path / "run" / "scratch"
    with mock.patch("agent_workspace.lab_bridge.sha256_file", return_value="c" * 64):
        snap = bridge_module._e0_validation_config_snapshot(harness, e0_auth, scratch)
    assert snap == scratch / "validation_config.snapshot.json"
    assert snap.read_text() == config.read_text()


def test_python_identity_returns_resolved_and_sha(tmp_path, bridge_module, e0_cfg):
    python = Path(e0_cfg["cantera_python"])
    python.write_bytes(b"#!/bin/sh\n")
    lab = _make_lab(tmp_path, e0_cfg)
    with mock.patch("agent_workspace.lab_bridge.sha256_file", return_value="py"):
        resolved, sha = bridge_module._e0_python_identity(e0_cfg, lab)
    assert resolved == python
    assert sha == "py"


def test_python_identity_rejects_missing(tmp_path, bridge_module, e0_cfg):
    lab = _make_lab(tmp_path, e0_cfg)
    with pytest.raises(RuntimeError, match="cantera_python missing"):
        bridge_module._e0_python_identity(e0_cfg, lab)
