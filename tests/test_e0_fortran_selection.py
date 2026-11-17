"""E0 Fortran compiler selection tests (G1-C).

Run after installing the G1-C bridge patch:

    python -m pytest -q tests/test_e0_fortran_selection.py tests/test_e0_bridge.py

These tests never execute a compiler.  ``subprocess.run`` is replaced with a
scripted fake, and the sanitized environment is inspected rather than trusted.
The last group is structural: it pins the *call sites* so plan, execute and the
provenance manifest cannot drift apart, and pins the env allowlist so this change
cannot quietly widen the build sandbox.

EXECUTION STATUS: prepared by the agent, **not executed** (the active read-only
shell policy refuses interpreter execution).  See the package README.
"""

from __future__ import annotations

import inspect
import subprocess

import pytest

from agent_workspace import lab_bridge as lb

IFX_VERSION = "ifx (IFX) 2026.2.0 20260101"
GFORTRAN_VERSION = "GNU Fortran (Ubuntu 13.3.0-6ubuntu2~24.04) 13.3.0"
FROZEN_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
)


class FakeCompiler:
    """Scripted replacement for ``subprocess.run`` in the discovery path."""

    def __init__(self, versions, *, fail=(), timeout=(), empty=()):
        self.versions = dict(versions)
        self.fail = set(fail)
        self.timeout = set(timeout)
        self.empty = set(empty)
        self.calls = []

    def __call__(self, argv, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv, "argv must be a non-empty list"
        name = argv[0]
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        if name not in self.versions:
            raise FileNotFoundError(2, "No such file or directory", name)
        if name in self.timeout:
            raise subprocess.TimeoutExpired(list(argv), 10)
        if name in self.fail:
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="internal error")
        if name in self.empty:
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")
        return subprocess.CompletedProcess(list(argv), 0, stdout=self.versions[name] + "\n", stderr="")

    @property
    def probed(self):
        return [call["argv"][0] for call in self.calls]


def _install(monkeypatch, fake):
    monkeypatch.setattr(lb.subprocess, "run", fake)
    return fake


# --------------------------------------------------------------------------- #
# 1. the allowlist itself
# --------------------------------------------------------------------------- #
def test_compiler_allowlist_is_the_frozen_discovery_order():
    assert lb._E0_FORTRAN_COMPILERS == ("gfortran", "gfortran-13", "ifx", "ifort")


# --------------------------------------------------------------------------- #
# 2-4. explicit selection
# --------------------------------------------------------------------------- #
def test_explicit_ifx_is_selected_and_nothing_else_is_probed(monkeypatch):
    fake = _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION, "gfortran": GFORTRAN_VERSION}))
    selection = lb._e0_select_fortran({"fortran_compiler": "ifx"})
    assert selection["mode"] == "explicit"
    assert selection["identifier"] == "ifx"
    assert selection["compiler"] == "ifx"
    assert selection["version"].startswith("ifx (IFX) 2026.2.0")
    assert fake.probed == ["ifx"], "an explicit selection must not probe other compilers"


def test_explicit_ifx_probe_uses_the_build_sanitized_environment(monkeypatch):
    fake = _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION}))
    lb._e0_select_fortran({"fortran_compiler": "ifx"})
    assert len(fake.calls) == 1
    kwargs = fake.calls[0]["kwargs"]
    assert kwargs["env"] == lb._sanitized_env(), "the probe must use _sanitized_env()"
    assert set(kwargs["env"]) <= set(lb._E0_ENV_ALLOWLIST)
    assert "ONEAPI_ROOT" not in kwargs["env"]


def test_explicit_gfortran_wins_over_the_automatic_order(monkeypatch):
    fake = _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION, "gfortran": GFORTRAN_VERSION}))
    selection = lb._e0_select_fortran({"fortran_compiler": "gfortran"})
    assert selection["identifier"] == "gfortran"
    assert selection["mode"] == "explicit"
    assert selection["version"].startswith("GNU Fortran")
    assert fake.probed == ["gfortran"]


# --------------------------------------------------------------------------- #
# 5. identifier normalization / rejection
# --------------------------------------------------------------------------- #
def test_selection_identifier_is_normalized():
    assert lb._e0_fortran_selection("ifx") == "ifx"
    assert lb._e0_fortran_selection("IFX") == "ifx"
    assert lb._e0_fortran_selection("  Gfortran-13  ") == "gfortran-13"


@pytest.mark.parametrize(
    "value",
    ["/usr/bin/ifx", "ifx-2026", "ifort64", "", "   ", "gcc", 42, 3.5, True, ["ifx"]],
)
def test_invalid_selection_is_rejected(value):
    with pytest.raises(RuntimeError):
        lb._e0_fortran_selection(value)


# --------------------------------------------------------------------------- #
# 6-8. absent configuration keeps the historical behaviour
# --------------------------------------------------------------------------- #
def test_absent_configuration_keeps_automatic_discovery(monkeypatch):
    fake = _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION, "gfortran": GFORTRAN_VERSION}))
    selection = lb._e0_select_fortran({})
    assert selection["mode"] == "auto"
    assert selection["identifier"] == "gfortran", "auto mode must still prefer gfortran"
    assert selection["version"].startswith("GNU Fortran")
    assert fake.probed == ["gfortran"]


def test_absent_configuration_auto_skips_absent_candidates(monkeypatch):
    fake = _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION}))
    selection = lb._e0_select_fortran({})
    assert selection["mode"] == "auto"
    assert selection["identifier"] == "ifx"
    assert fake.probed == ["gfortran", "gfortran-13", "ifx"]


def test_absent_configuration_reports_no_compiler(monkeypatch):
    _install(monkeypatch, FakeCompiler({}))
    with pytest.raises(RuntimeError) as excinfo:
        lb._e0_select_fortran({})
    assert "no supported Fortran compiler found" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 9-12. explicit selection fails closed
# --------------------------------------------------------------------------- #
def test_selected_compiler_unavailable_fails_without_falling_back(monkeypatch):
    fake = _install(monkeypatch, FakeCompiler({"gfortran": GFORTRAN_VERSION}))
    with pytest.raises(RuntimeError) as excinfo:
        lb._e0_select_fortran({"fortran_compiler": "ifx"})
    message = str(excinfo.value)
    assert "ifx" in message and "not found on PATH" in message
    assert fake.probed == ["ifx"], "must not fall back to gfortran"


def test_selected_compiler_version_failure_is_fatal(monkeypatch):
    fake = _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION}, fail=("ifx",)))
    with pytest.raises(RuntimeError) as excinfo:
        lb._e0_select_fortran({"fortran_compiler": "ifx"})
    assert "exited 1" in str(excinfo.value)
    assert fake.probed == ["ifx"]


def test_selected_compiler_timeout_is_fatal(monkeypatch):
    _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION}, timeout=("ifx",)))
    with pytest.raises(RuntimeError) as excinfo:
        lb._e0_select_fortran({"fortran_compiler": "ifx"})
    assert "timed out" in str(excinfo.value)


def test_selected_compiler_without_version_output_is_fatal(monkeypatch):
    _install(monkeypatch, FakeCompiler({"ifx": IFX_VERSION}, empty=("ifx",)))
    with pytest.raises(RuntimeError) as excinfo:
        lb._e0_select_fortran({"fortran_compiler": "ifx"})
    assert "produced no output" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 13-16. structural: plan / execute / manifest agree, nothing was weakened
# --------------------------------------------------------------------------- #
def test_plan_and_execute_use_the_shared_selector():
    for command in (lb.cmd_e0_build_plan, lb.cmd_e0_build_execute):
        source = inspect.getsource(command)
        assert "_e0_select_fortran(cfg)" in source, command.__name__
        assert "_e0_discover_fortran()" not in source, command.__name__
        assert "fortran_selection" in source, command.__name__
        assert "fortran_identifier" in source, command.__name__


def test_build_manifest_records_the_selection():
    source = inspect.getsource(lb.cmd_e0_build_execute)
    manifest_block = source.split("manifest = {", 1)[1]
    assert '"fortran_compiler": compiler' in manifest_block
    assert '"fortran_version": cver' in manifest_block
    assert '"fortran_selection": selection["mode"]' in manifest_block
    assert '"fortran_identifier": selection["identifier"]' in manifest_block


def test_config_validates_the_new_key():
    source = inspect.getsource(lb._e0_config)
    assert "_e0_fortran_selection(" in source, "_e0_config must validate [e0].fortran_compiler"


def test_environment_allowlist_is_not_weakened():
    assert lb._E0_ENV_ALLOWLIST == FROZEN_ALLOWLIST
    assert "ONEAPI_ROOT" not in lb._E0_ENV_ALLOWLIST
    assert "CPATH" not in lb._E0_ENV_ALLOWLIST
    assert "LIBRARY_PATH" not in lb._E0_ENV_ALLOWLIST


def test_automatic_discovery_diagnostics_are_unchanged():
    source = inspect.getsource(lb._e0_discover_fortran)
    assert "for cand in _E0_FORTRAN_COMPILERS:" in source
    assert "--version timed out" in source
    assert "found but '--version' exited" in source
    assert "'--version' produced no output" in source
    assert "no supported Fortran compiler found" in source
