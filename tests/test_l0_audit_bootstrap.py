"""Security tests for the G1-S1a hardened trusted L0 audit bootstrap.

Run after installing the reviewed files:

    python -m pytest -q tests/test_l0_audit_bootstrap.py tests/test_e0_bridge.py

The first 60 tests exercise the *pure* audit core with a fake host: no git, no
subprocess, no laboratory, no bridge import.  The last 6 tests are structural
checks that DO import ``agent_workspace.lab_bridge`` and therefore require the
protected bridge to be installed.

The fake runner models the documented host contract: it is aware of canonical
cases, positive fixtures and negative (refusal) fixtures, it writes the trusted
``# result:`` log header, and it raises structured child failures carrying
``returncode`` / ``timed_out`` / ``cancelled``.  It does not sniff message text.

EXECUTION STATUS: **not executed**.  The active read-only shell policy refuses
interpreter execution ("command 'python3' is not in the read-only inspection
allowlist"), so no test result is claimed by the agent.  See
``G1S1A_TEST_EVIDENCE.md``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest

from agent_workspace import l0_audit as l0

COMMIT = "f" * 40
SOURCE_SHA = "a" * 64
EXE_SHA = "b" * 64
MECH_SHA = "c" * 64
THERMO_SHA = "d" * 64
TRANS_SHA = "e" * 64

# Second, independent record of the frozen fixture identities.  These literals
# come from the packaged artifacts (evidence/fixtures/*.txt, `sha256sum`), not
# from FIXTURES.  A drift in either the module table or the fixture text fails.
FROZEN = {
    "P1_MOLECULES": "a20080a6bc23141a71d05f44da5e2b6fb09734d608e2b9cdb8903349e7fe31e3",
    "P1_MOLES": "e85a25201121b859c07ca3203f7262a380b41673e94c8d8ea7d76b00fc5af4cd",
    "N1_PLOG": "ccdf5adefd90cf299523b8e230ad614d207a5ecd8df13e12d6bb3c1838b8fb19",
    "N2_CHEB": "b949f9815c689a84af8d247b7a18c44a3056b3ea553d6d8f64bac7da8daa2241",
    "N3_SRI": "d13912dd872a72b607705505e3bd274f6b830eaf25ccbd9088098890b98afc68",
    "N4_REV": "96fa9bbc7eee068025d8cdd23826888124ed293ea78a506c74e29490a8a01ae5",
    "N5_FRACTIONAL": "95b640149537cac2b8d6c470e89748f08b5c8dfce3d38ab63d4375daa6579f73",
}


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _case(cid="M1", mode="tp", **over):
    case = {
        "id": cid,
        "mode": mode,
        "mechanism": "hydrogen/KEROMNES.txt",
        "thermo": "KEROMNES_THERMO.txt",
        "transport": "KEROMNES_TRANSDATA.txt",
        "mechanism_sha256": MECH_SHA,
        "thermo_sha256": THERMO_SHA,
        "transport_sha256": TRANS_SHA,
        "components": [["H2", 2.0], ["O2", 1.0], ["N2", 3.762]],
        "tp_temperature": 1500.0,
        "initial_temperature": 300.0,
        "pressure": 101325.0,
    }
    case.update(over)
    return case


def _record(cases=None, **audit_over):
    audit = {
        "executable_sha256": EXE_SHA,
        "build_commit": COMMIT,
        "timeout_seconds": 60,
        "total_timeout_seconds": 900,
        "fixtures_enabled": False,
        "case": cases if cases is not None else [_case()],
    }
    audit.update(audit_over)
    return {"commit": COMMIT, "source_sha256": SOURCE_SHA, "audit": audit}


def _auth(cases=None, **audit_over):
    return l0.load_audit_authorization(_record(cases, **audit_over))


class ChildFailure(RuntimeError):
    """Mirror of the patched bridge's structured child failure."""

    def __init__(self, message, *, returncode=None, timed_out=False, cancelled=False, outcome=None):
        super().__init__(message)
        self.returncode = returncode
        self.timed_out = timed_out
        self.cancelled = cancelled
        self.outcome = outcome


def _write_log(log_path, result, stdout="", stderr=""):
    Path(log_path).write_text(
        f"# l0\n# command: fixed\n# result: {result}\n# cancellation_signals: 0\n"
        f"\n== STDOUT ==\n{stdout}\n\n== STDERR ==\n{stderr}\n"
    )


def _fixture_message(name):
    return l0.FIXTURES[name]["expect"][0]


class FakeTime:
    """Deterministic clock so the wall-time budget is testable."""

    def __init__(self, start=1000.0):
        self.t = start

    def monotonic(self):
        return self.t


class Runner:
    """Contract-aware fake runner (canonical cases + positive/negative fixtures)."""

    def __init__(self, *, default="auto", overrides=None, clock=None, advance=0.0):
        self.default = default
        self.overrides = overrides or {}
        self.clock = clock
        self.advance = advance
        self.calls = []

    def __call__(self, cmd, label, timeout, log_path, cwd=None):
        assert cwd is not None, "audit must run with an explicit cwd"
        namelist = Path(cmd[1]).read_text()
        prefix = re.search(r"output_prefix = '([a-z0-9_]+)'", namelist).group(1)
        mode = re.search(r"mode = '([a-z]+)'", namelist).group(1)
        case_id = prefix.upper()
        is_fixture = case_id in l0.FIXTURES
        action = self.overrides.get(case_id)
        if action is None:
            action = self.overrides.get("FIXTURE" if is_fixture else "CANONICAL", self.default)
        if action == "auto":
            action = "refuse" if is_fixture and l0.FIXTURES[case_id]["kind"] == "refusal" else "ok"
        self.calls.append({"id": case_id, "timeout": timeout, "fixture": is_fixture, "action": action})
        if self.clock is not None:
            self.clock.t += self.advance

        if action in ("ok", "extra", "scratch", "extra_dir", "no_log", "symlink_output", "symlink_extra"):
            for suffix in l0.MODE_OUTPUTS[mode]:
                (Path(cwd) / f"{prefix}{suffix}").write_text("x")
            if action == "extra":
                (Path(cwd) / "surprise.dat").write_text("unexpected\n")
            if action == "scratch":
                (Path(cwd) / "chemical_data.inf").write_text("inf\n")
                (Path(cwd) / "thermophysical_setup.inf").write_text("inf\n")
            if action == "extra_dir":
                (Path(cwd) / "leftover").mkdir()
            if action == "symlink_output":
                outside = Path(cwd).parent.parent / "outside_output.csv"
                outside.write_text("outside\n")
                produced = Path(cwd) / f"{prefix}_species.csv"
                produced.unlink()
                produced.symlink_to(outside)
            if action == "symlink_extra":
                outside = Path(cwd).parent.parent / "outside_extra.txt"
                outside.write_text("outside\n")
                (Path(cwd) / "notes.txt").symlink_to(outside)
            if action == "no_log":
                return ""
            _write_log(log_path, "EXIT_0", stdout="ok")
            return ""
        if action == "refuse":
            message = _fixture_message(case_id)
            _write_log(log_path, "EXIT_2", stderr=f"ERROR STOP {message}\n")
            raise ChildFailure(f"{label} exited 2", returncode=2)
        if action == "refuse_wrong_message":
            _write_log(log_path, "EXIT_2", stderr="ERROR STOP something else\n")
            raise ChildFailure(f"{label} exited 2", returncode=2)
        if action == "refuse_no_exit":
            _write_log(log_path, "FAILED", stderr=f"ERROR STOP {_fixture_message(case_id)}\n")
            raise ChildFailure(f"{label} failed without an exit status")
        if action == "refuse_header_exit0":
            _write_log(log_path, "EXIT_0", stderr=f"ERROR STOP {_fixture_message(case_id)}\n")
            raise ChildFailure(f"{label} exited 2", returncode=2)
        if action == "timeout_with_message":
            _write_log(log_path, "TIMEOUT", stderr=f"ERROR STOP {_fixture_message(case_id)}\n")
            raise ChildFailure(f"{label} timed out after {timeout}s", timed_out=True)
        if action == "timeout":
            _write_log(log_path, "TIMEOUT", stderr="still running\n")
            raise ChildFailure(f"{label} timed out after {timeout}s", timed_out=True)
        if action == "cancel":
            _write_log(log_path, "CANCELLED")
            raise ChildFailure(f"{label} cancelled by external SIGTERM", cancelled=True)
        if action == "fail":
            _write_log(log_path, "EXIT_2", stderr="boom\n")
            raise ChildFailure(f"{label} exited 2", returncode=2)
        raise AssertionError(f"unknown action {action}")


class Host:
    def __init__(self, tmp_path, *, exe_sha=EXE_SHA, commit=COMMIT, source=SOURCE_SHA,
                 blobs=None, worktree=None, run_root=None, runner=None,
                 manifest_over=None, poison=()):
        self.build_root = tmp_path / "build"
        (self.build_root / "bin" / "Release").mkdir(parents=True)
        self.run_root = Path(run_root) if run_root else tmp_path / "run"
        self.repo_root = tmp_path / "nrg"
        self.commit = commit
        self.source_sha256 = source
        self.executable_name = "package_interface_0D_chemical_equilibrium_validation"
        self.build_type = "release"
        self.exe = self.build_root / "bin" / "Release" / self.executable_name
        self.exe.write_bytes(b"#!/bin/true\n")
        manifest = {
            "pinned_commit": COMMIT,
            "pinned_source_sha256": SOURCE_SHA,
            "cmake_target": "package_interface",
            "executable_sha256": EXE_SHA,
        }
        manifest.update(manifest_over or {})
        (self.build_root / "build_manifest.json").write_text(json.dumps(manifest))
        self._hashes = {str(self.exe): exe_sha}
        self._poison = set(poison)
        self._blobs = blobs if blobs is not None else {
            "chemical_mechanisms/hydrogen/KEROMNES.txt": MECH_SHA,
            "thermophysical_data/KEROMNES_THERMO.txt": THERMO_SHA,
            "thermophysical_data/KEROMNES_TRANSDATA.txt": TRANS_SHA,
        }
        self._worktree = worktree
        self._runner = runner or Runner()

    def sha256_file(self, path):
        path = Path(path)
        key = str(path)
        if key in self._hashes:
            return self._hashes[key]
        if path.name in self._poison:
            raise RuntimeError(f"cannot hash {path.name}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def commit_blob_sha256(self, rel):
        if rel not in self._blobs:
            raise RuntimeError(f"no blob: {rel}")
        return self._blobs[rel]

    def stage_task_setup(self, dest):
        dest = Path(dest)
        ts = dest / "package_interface" / "task_setup"
        (ts / "chemical_mechanisms" / "hydrogen").mkdir(parents=True)
        (ts / "thermophysical_data").mkdir(parents=True)
        (ts / "chemical_mechanisms" / "hydrogen" / "KEROMNES.txt").write_text("x")
        (ts / "thermophysical_data" / "KEROMNES_THERMO.txt").write_text("x")
        (ts / "thermophysical_data" / "KEROMNES_TRANSDATA.txt").write_text("x")
        (dest / "staged_input_manifest.json").write_text("{}")
        return {"staged_input_manifest": str(dest / "staged_input_manifest.json"),
                "staged_input_manifest_sha256": "0" * 64, "staged_files": {}}

    def run_bounded(self, cmd, label, timeout, log_path, cwd=None):
        return self._runner(cmd, label, timeout, log_path, cwd=cwd)

    def verify_worktree(self):
        if isinstance(self._worktree, Exception):
            raise self._worktree
        return self._worktree or {"commit": COMMIT, "source_sha256": SOURCE_SHA}

    def utc_now(self):
        return "2026-01-01T00:00:00Z"

    def host(self):
        return l0.AuditHost(
            repo_root=self.repo_root,
            build_root=self.build_root,
            run_root=self.run_root,
            commit=self.commit,
            source_sha256=self.source_sha256,
            executable_name=self.executable_name,
            build_type=self.build_type,
            sha256_file=self.sha256_file,
            commit_blob_sha256=self.commit_blob_sha256,
            stage_task_setup=self.stage_task_setup,
            run_bounded=self.run_bounded,
            verify_worktree=self.verify_worktree,
            utc_now=self.utc_now,
        )


def _binding(auth):
    return {
        "executable_sha256": EXE_SHA,
        "commit": COMMIT,
        "source_sha256": SOURCE_SHA,
        "cases_sha256": auth["cases_sha256"],
    }


# --------------------------------------------------------------------------- #
# 1. good path
# --------------------------------------------------------------------------- #
def test_plan_good_path(tmp_path):
    h = Host(tmp_path)
    out = l0.plan(h.host(), _auth())
    assert out["can_run"] is True
    assert out["executable_sha256"] == EXE_SHA
    assert out["cases"][0]["output_prefix"] == "m1"
    policy = out["unexpected_output_policy"]
    assert policy["on_unexpected_entry"] == "UNEXPECTED_OUTPUT"
    assert policy["on_unsafe_entry"] == "UNSAFE_OUTPUT"
    assert policy["allowed_subdirectory"] == "task_setup"
    assert not h.run_root.exists(), "plan must not create the run root"


def test_execute_good_path(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    assert out["complete"] is True
    assert out["status_counts"] == {"COMPLETE": 1}
    manifest = json.loads(Path(out["manifest"]).read_text())
    assert manifest["executable_sha256"] == EXE_SHA
    assert manifest["cases_sha256"] == auth["cases_sha256"]
    assert manifest["results"][0]["output_count"] == len(l0.MODE_OUTPUTS["tp"])


# --------------------------------------------------------------------------- #
# 2. authorization / case validation
# --------------------------------------------------------------------------- #
def test_reject_unapproved_case_id():
    with pytest.raises(l0.AuditError):
        _auth([_case(id="m1")])


def test_reject_absent_authorization():
    with pytest.raises(l0.AuditError):
        l0.load_audit_authorization({"commit": COMMIT, "source_sha256": SOURCE_SHA})


def test_reject_missing_audit_scope():
    record = _record()
    del record["audit"]["executable_sha256"]
    with pytest.raises(l0.AuditError):
        l0.load_audit_authorization(record)


def test_reject_missing_required_case_keys():
    # Correction 7: the documented key set is enforced, so a template that omits
    # thermo/transport/tp_temperature/initial_temperature is rejected up front.
    for key in ("thermo", "transport", "thermo_sha256", "transport_sha256",
                "tp_temperature", "initial_temperature", "pressure", "components"):
        case = _case()
        del case[key]
        with pytest.raises(l0.AuditError):
            _auth([case])


def test_reject_fixture_id_collision():
    with pytest.raises(l0.AuditError):
        _auth([_case(id="N1_PLOG")])


def test_reject_mechanism_hash_mismatch(tmp_path):
    h = Host(tmp_path, blobs={
        "chemical_mechanisms/hydrogen/KEROMNES.txt": "9" * 64,
        "thermophysical_data/KEROMNES_THERMO.txt": THERMO_SHA,
        "thermophysical_data/KEROMNES_TRANSDATA.txt": TRANS_SHA,
    })
    with pytest.raises(l0.AuditError):
        l0.plan(h.host(), _auth())


def test_reject_traversal_mechanism():
    with pytest.raises(l0.AuditError):
        _auth([_case(mechanism="hydrogen/../../etc/passwd.txt")])
    with pytest.raises(l0.AuditError):
        _auth([_case(mechanism="/etc/hosts.txt")])


def test_reject_forbidden_mode():
    for mode in ("all", "crossover"):
        with pytest.raises(l0.AuditError):
            _auth([_case(mode=mode)])


def test_reject_bad_prefix():
    for cid in ("M/1", "ABCDEFGHIJ", "m1"):
        with pytest.raises(l0.AuditError):
            _auth([_case(id=cid)])


# --------------------------------------------------------------------------- #
# 3. executable / provenance / collision
# --------------------------------------------------------------------------- #
def test_missing_executable(tmp_path):
    h = Host(tmp_path)
    h.exe.unlink()
    with pytest.raises(l0.AuditError):
        l0.plan(h.host(), _auth())


def test_reject_exe_hash_mismatch(tmp_path):
    h = Host(tmp_path, exe_sha="9" * 64)
    with pytest.raises(l0.AuditError):
        l0.plan(h.host(), _auth())


def test_reject_build_manifest_identity(tmp_path):
    h = Host(tmp_path, manifest_over={"cmake_target": "computing_module"})
    with pytest.raises(l0.AuditError):
        l0.plan(h.host(), _auth())


def test_reject_wrong_commit(tmp_path):
    h = Host(tmp_path, commit="1" * 40)
    with pytest.raises(l0.AuditError):
        l0.plan(h.host(), _auth())


def test_reject_dirty_checkout(tmp_path):
    h = Host(tmp_path, worktree=RuntimeError("NRG checkout is not clean"))
    with pytest.raises(RuntimeError):
        l0.plan(h.host(), _auth())


def test_reject_source_sha(tmp_path):
    h = Host(tmp_path, worktree={"commit": COMMIT, "source_sha256": "9" * 64})
    with pytest.raises(l0.AuditError):
        l0.plan(h.host(), _auth())


def test_reject_output_collision(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "leftover.txt").write_text("x")
    h = Host(tmp_path, run_root=run)
    with pytest.raises(l0.AuditError):
        l0.plan(h.host(), _auth())


def test_reject_existing_case_dir(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    case = auth["cases"][0]
    (h.run_root / "scratch" / case["id"]).mkdir(parents=True)
    with pytest.raises(l0.AuditError):
        l0._run_one(h.host(), auth, case, None, h.run_root / "scratch" / "input",
                    is_fixture=False, timeout_seconds=10)


def test_reject_symlinked_case_dir(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    case = auth["cases"][0]
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = h.run_root / "scratch" / case["id"]
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(l0.AuditError):
        l0._run_one(h.host(), auth, case, None, h.run_root / "scratch" / "input",
                    is_fixture=False, timeout_seconds=10)
    assert target.is_dir()


# --------------------------------------------------------------------------- #
# 4. composition / namelist
# --------------------------------------------------------------------------- #
def test_reject_bad_composition():
    for comps in (
        [["H2", 0.0]],
        [["H2", -1.0]],
        [["H2", 1.0], ["H2", 1.0]],
        [["H2'", 1.0]],
        [["H2", 1.0e9]],
    ):
        with pytest.raises(l0.AuditError):
            _auth([_case(components=comps)])


def test_reject_out_of_range_state():
    with pytest.raises(l0.AuditError):
        _auth([_case(pressure=1.0e12)])
    with pytest.raises(l0.AuditError):
        _auth([_case(tp_temperature=50.0)])


def test_namelist_is_deterministic_and_safe():
    auth = _auth()
    text = l0.render_namelist(auth["cases"][0])
    assert text == l0.render_namelist(auth["cases"][0])
    assert "output_prefix = 'm1'" in text
    assert "\n/\n" in text
    assert "/" not in re.search(r"output_prefix = '([^']*)'", text).group(1)


def test_cases_digest_tracks_content():
    a = _auth([_case()])
    b = _auth([_case()])
    c = _auth([_case(tp_temperature=1501.0)])
    assert a["cases_sha256"] == b["cases_sha256"]
    assert a["cases_sha256"] != c["cases_sha256"]


def test_case_composition_keeps_air_ratio():
    # The canonical example composition is H2:O2:N2 = 2:1:3.762 (authorization
    # example, docs/e0_authorization.toml.example).  The loader must preserve the
    # ratio exactly and must not renormalize it on the way to the namelist.
    auth = _auth()
    comps = auth["cases"][0]["components"]
    assert comps == [["H2", 2.0], ["O2", 1.0], ["N2", 3.762]]
    text = l0.render_namelist(auth["cases"][0])
    assert "2.0000000000000000e+00" in text
    assert "1.0000000000000000e+00" in text
    assert "3.7620000000000000e+00" in text


# --------------------------------------------------------------------------- #
# 5. failure classification (correction 3)
# --------------------------------------------------------------------------- #
def test_timeout_classified(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "timeout"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    assert out["results"][0]["status"] == "TIMEOUT"
    assert out["complete"] is False


def test_failed_run_classified(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "fail"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    assert out["results"][0]["status"] == "FAILED"
    assert out["complete"] is False


def test_cancelled_is_not_a_refusal(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"N1_PLOG": "cancel"}))
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "CANCELLED"


# --------------------------------------------------------------------------- #
# 6. grammar fixtures (corrections 2, 3, 5)
# --------------------------------------------------------------------------- #
def test_positive_fixture_runs(tmp_path):
    h = Host(tmp_path, runner=Runner())
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["M1"]["status"] == "COMPLETE"
    assert by_id["P1_MOLECULES"]["status"] == "COMPLETE"
    assert by_id["P1_MOLES"]["status"] == "COMPLETE"
    assert by_id["P1_MOLES"]["fixture_kind"] == "positive"
    assert out["complete"] is True


def test_negative_fixture_matches_expected_diagnostic(tmp_path):
    h = Host(tmp_path, runner=Runner())
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "REFUSED_AS_EXPECTED"
    # G1-S1a correction 1: the accepted diagnostic is a string, and the full
    # candidate list is carried separately.
    assert by_id["N1_PLOG"]["message_matched"] == "unsupported kinetics directive: PLOG"
    assert by_id["N1_PLOG"]["messages_matched"] == ["unsupported kinetics directive: PLOG"]
    assert by_id["N1_PLOG"]["returncode"] == 2
    assert by_id["N1_PLOG"]["log_result"] == "EXIT_2"
    assert out["complete"] is True


def test_negative_fixture_wrong_diagnostic(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"N1_PLOG": "refuse_wrong_message"}))
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "REFUSED_WITHOUT_MESSAGE"
    assert by_id["N1_PLOG"]["message_matched"] is None
    assert by_id["N1_PLOG"]["messages_matched"] == []
    assert out["complete"] is False


def test_negative_fixture_unexpected_success(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"FIXTURE": "ok"}))
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "UNEXPECTED_SUCCESS"
    assert out["complete"] is False


def test_timeout_is_never_a_refusal_even_with_matching_message(tmp_path):
    # Correction 3: the matching diagnostic is present in the log, but the child
    # timed out, so the outcome must be TIMEOUT -- never REFUSED_AS_EXPECTED.
    h = Host(tmp_path, runner=Runner(overrides={"N1_PLOG": "timeout_with_message"}))
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "TIMEOUT"
    assert by_id["N1_PLOG"]["reason"].startswith("child timed out")


def test_refusal_requires_nonzero_exit(tmp_path):
    # A failure without a structured exit status is not proof of a parser refusal.
    h = Host(tmp_path, runner=Runner(overrides={"N1_PLOG": "refuse_no_exit"}))
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "EXIT_UNVERIFIED"
    assert out["complete"] is False


def test_refusal_rejects_provenance_mismatch(tmp_path):
    # Log header says EXIT_0 while the child reported exit 2: inconsistent
    # provenance is never accepted as an expected refusal.
    h = Host(tmp_path, runner=Runner(overrides={"N1_PLOG": "refuse_header_exit0"}))
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "PROVENANCE_MISMATCH"


def test_fixture_hashes_are_frozen_literals():
    # Both MOLECULES and MOLES are retained, and the module table is data, not a
    # value recomputed from the fixture strings.
    assert set(l0.FIXTURE_SHA256) == set(FROZEN)
    assert l0.FIXTURE_SHA256 == FROZEN
    assert {"P1_MOLECULES", "P1_MOLES"} <= set(l0.FIXTURES)
    for name, digest in FROZEN.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert hashlib.sha256(l0.FIXTURES[name]["text"].encode()).hexdigest() == digest


def test_fixture_identity_check_detects_drift(monkeypatch):
    original = l0.FIXTURES["P1_MOLES"]["text"]
    monkeypatch.setitem(l0.FIXTURES["P1_MOLES"], "text", original + "\n")
    with pytest.raises(l0.AuditError):
        l0._verify_fixture_identity()
    monkeypatch.setitem(l0.FIXTURES["P1_MOLES"], "text", original)
    l0._verify_fixture_identity()


def test_fixture_hash_table_cannot_be_derived_from_text():
    source = Path(l0.__file__).read_text()
    assert "_sha256_text(spec[\"text\"]) for name, spec in FIXTURES.items()" not in source
    assert "FIXTURE_SHA256: dict[str, str] = {" in source


# --------------------------------------------------------------------------- #
# 7. output provenance (correction 4)
# --------------------------------------------------------------------------- #
def test_manifest_records_log_hash(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    manifest = json.loads(Path(out["manifest"]).read_text())
    log = manifest["results"][0]["log"]
    assert re.fullmatch(r"[0-9a-f]{64}", log["sha256"])
    assert log["bytes"] > 0


def test_failure_artifact_hashes_recorded(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "fail"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    manifest = json.loads(Path(out["manifest"]).read_text())
    result = manifest["results"][0]
    assert result["status"] == "FAILED"
    assert re.fullmatch(r"[0-9a-f]{64}", result["log"]["sha256"])
    assert result["log_result"] == "EXIT_2"


def test_unexpected_output_is_never_complete(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "extra"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "UNEXPECTED_OUTPUT"
    assert result["unexpected"] == ["surprise.dat"]
    assert len(result["unexpected_hashes"]) == 1
    assert result["unexpected_hashes"][0]["name"] == "surprise.dat"
    assert out["complete"] is False


def test_allowed_scratch_files_do_not_fail_a_case(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "scratch"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "COMPLETE"
    assert {item["name"] for item in result["scratch"]} == {
        "chemical_data.inf", "thermophysical_setup.inf"}


def test_refusal_missing_result_header_fails_closed(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("# l0\n# command: fixed\n== STDERR ==\n"
                   "unsupported kinetics directive: PLOG\n")
    launched = {"timed_out": False, "cancelled": False,
                "status": l0.STATUS_FAILED, "returncode": 2}
    status, matched, header = l0._refusal_status(l0.FIXTURES["N1_PLOG"], launched, log)
    assert status == l0.STATUS_PROVENANCE_MISMATCH
    assert matched == []
    assert header is None


def test_untrusted_stdout_cannot_spoof_result_header(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("# l0\n# command: fixed\n== STDOUT ==\n"
                   "# result: EXIT_2\nunsupported kinetics directive: PLOG\n")
    assert l0._log_result(log) is None


def test_success_requires_corresponding_result_header(tmp_path):
    class NoHeaderRunner(Runner):
        def __call__(self, cmd, label, timeout, log_path, cwd=None):
            result = super().__call__(cmd, label, timeout, log_path, cwd=cwd)
            path = Path(log_path)
            path.write_text(path.read_text().replace("# result: EXIT_0\n", ""))
            return result

    host = Host(tmp_path, runner=NoHeaderRunner())
    auth = _auth()
    out = l0.execute(host.host(), auth, _binding(auth))
    assert out["results"][0]["status"] == l0.STATUS_PROVENANCE_MISMATCH
    assert out["complete"] is False


# --------------------------------------------------------------------------- #
# 8. directory / symlink / provenance hardening (G1-S1a corrections 2, 3)
# --------------------------------------------------------------------------- #
def test_task_setup_directory_is_allowed(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert (Path(result["case_dir"]) / "task_setup").is_dir()
    assert result["status"] == "COMPLETE"
    assert result["unexpected_dirs"] == []
    assert result["unsafe"] == []


def test_unexpected_directory_is_never_complete(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "extra_dir"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "UNEXPECTED_OUTPUT"
    assert result["unexpected_dirs"] == ["leftover"]
    assert out["complete"] is False


def test_symlinked_expected_output_is_unsafe(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "symlink_output"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "UNSAFE_OUTPUT"
    assert any(item["kind"] == "symlink" and item["expected_output"] == "true"
               for item in result["unsafe"])
    assert "m1_species.csv" in result["missing"]
    assert out["complete"] is False


def test_symlinked_extra_entry_is_unsafe(tmp_path):
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "symlink_extra"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "UNSAFE_OUTPUT"
    assert any(item["name"] == "notes.txt" for item in result["unsafe"])
    assert out["complete"] is False


def test_missing_log_is_never_complete(tmp_path):
    # The runner contract requires a log; a case without one has no auditable
    # provenance and must not be reported COMPLETE.
    h = Host(tmp_path, runner=Runner(overrides={"CANONICAL": "no_log"}))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "PROVENANCE_INCOMPLETE"
    assert result["provenance_complete"] is False
    assert result["log"] is None
    assert "missing" in result["provenance_error"]
    assert out["complete"] is False


def test_unhashable_expected_output_is_never_complete(tmp_path):
    h = Host(tmp_path, poison=("m1_species.csv",))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "PROVENANCE_INCOMPLETE"
    assert any(item["name"] == "m1_species.csv" for item in result["hash_errors"])
    assert out["complete"] is False


def test_unhashable_log_is_never_complete(tmp_path):
    h = Host(tmp_path, poison=("run.log",))
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    result = out["results"][0]
    assert result["status"] == "PROVENANCE_INCOMPLETE"
    assert result["provenance_complete"] is False
    assert out["complete"] is False


def test_refusal_requires_complete_provenance(tmp_path):
    # C3: the parser refusal is real (exit 2, matching diagnostic), but its
    # trusted log cannot be hashed, so it must not be certified as OK.
    # continue_on_failure keeps the fixture block reachable although the
    # canonical case also loses its log hash.
    h = Host(tmp_path, poison=("run.log",))
    auth = _auth(fixtures_enabled=True)
    out = l0.execute(h.host(), auth, _binding(auth), continue_on_failure=True)
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["N1_PLOG"]["status"] == "PROVENANCE_INCOMPLETE"
    assert by_id["N1_PLOG"]["refusal_evidence_ok"] is True
    assert by_id["N1_PLOG"]["provenance_complete"] is False
    assert "not certified" in by_id["N1_PLOG"]["reason"]
    assert out["complete"] is False


def test_complete_case_reports_complete_provenance(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    out = l0.execute(h.host(), auth, _binding(auth))
    manifest = json.loads(Path(out["manifest"]).read_text())
    result = manifest["results"][0]
    assert result["status"] == "COMPLETE"
    assert result["provenance_complete"] is True
    assert result["provenance_error"] is None
    assert manifest["unexpected_output_policy"]["on_incomplete_provenance"] == "PROVENANCE_INCOMPLETE"


# --------------------------------------------------------------------------- #
# 9. wall-time budget (correction 6, hardened)
# --------------------------------------------------------------------------- #
def test_per_case_timeout_is_bounded_by_remaining_budget(tmp_path, monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(l0, "time", clock)
    runner = Runner(clock=clock, advance=50.0)
    h = Host(tmp_path, runner=runner)
    auth = _auth([_case("M1"), _case("M2"), _case("M3")],
                 timeout_seconds=100, total_timeout_seconds=120)
    out = l0.execute(h.host(), auth, _binding(auth))
    assert [call["timeout"] for call in runner.calls] == [100, 70, 20]
    assert out["complete"] is True


def test_budget_exhaustion_blocks_remaining_cases(tmp_path, monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(l0, "time", clock)
    runner = Runner(clock=clock, advance=70.0)
    h = Host(tmp_path, runner=runner)
    auth = _auth([_case("M1"), _case("M2")],
                 timeout_seconds=60, total_timeout_seconds=60)
    out = l0.execute(h.host(), auth, _binding(auth))
    assert out["results"][0]["status"] == "COMPLETE"
    assert out["results"][1]["status"] == "BLOCKED"
    assert "budget" in out["results"][1]["reason"]
    assert out["complete"] is False


def test_sub_second_remainder_blocks_instead_of_launching(tmp_path, monkeypatch):
    # G1-S1a correction 4: with less than one second left, a 1 s launch would
    # exceed the authoritative total limit, so the case must be BLOCKED.
    clock = FakeTime()
    monkeypatch.setattr(l0, "time", clock)
    runner = Runner(clock=clock, advance=59.5)
    h = Host(tmp_path, runner=runner)
    auth = _auth([_case("M1"), _case("M2")],
                 timeout_seconds=60, total_timeout_seconds=60)
    out = l0.execute(h.host(), auth, _binding(auth))
    assert out["results"][0]["status"] == "COMPLETE"
    assert out["results"][1]["status"] == "BLOCKED"
    assert len(runner.calls) == 1, "no second launch may occur with 0.5 s left"
    assert out["complete"] is False


def test_remaining_budget_is_floored_not_rounded_up(tmp_path, monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(l0, "time", clock)
    runner = Runner(clock=clock, advance=118.1)
    h = Host(tmp_path, runner=runner)
    auth = _auth([_case("M1"), _case("M2")],
                 timeout_seconds=100, total_timeout_seconds=120)
    out = l0.execute(h.host(), auth, _binding(auth))
    assert [call["timeout"] for call in runner.calls] == [100, 1]
    assert out["complete"] is True


# --------------------------------------------------------------------------- #
# 10. binding / containment / scope
# --------------------------------------------------------------------------- #
def test_execute_requires_binding(tmp_path):
    h = Host(tmp_path)
    with pytest.raises(l0.AuditError):
        l0.execute(h.host(), _auth(), {"commit": COMMIT})


def test_execute_stale_exe(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    binding = _binding(auth)
    h._hashes[str(h.exe)] = "9" * 64
    with pytest.raises(l0.AuditError):
        l0.execute(h.host(), auth, binding)


def test_execute_stale_cases(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    with pytest.raises(l0.AuditError):
        l0.execute(h.host(), auth, dict(_binding(auth), cases_sha256="9" * 64))


def test_no_writes_outside_run_root(tmp_path):
    h = Host(tmp_path)
    auth = _auth()
    before = sorted(p.name for p in tmp_path.iterdir())
    out = l0.execute(h.host(), auth, _binding(auth))
    assert out["complete"] is True
    created = sorted(set(p.name for p in tmp_path.iterdir()) - set(before))
    assert created == ["run"], created
    assert (h.run_root / "scratch" / "input" / "package_interface" / "task_setup"
            / "thermophysical_data" / "KEROMNES_THERMO.txt").read_text() == "x"


def test_no_computing_module_path():
    source = Path(l0.__file__).read_text()
    assert "computing_module" not in source
    assert "shell=True" not in source
    assert "os.system" not in source


# =========================================================================== #
# 11. STRUCTURAL CHECKS ON THE PROTECTED BRIDGE
#     These 6 tests import agent_workspace.lab_bridge and therefore require the
#     G1-S1 bridge patch to be installed.  They cannot run in isolation.
# =========================================================================== #
def test_build_commands_use_build_scope():
    from agent_workspace import lab_bridge as lb

    for command in (lb.cmd_e0_build_plan, lb.cmd_e0_build_execute):
        source = inspect.getsource(command)
        assert '_e0_authorization_scoped(lab, "build")' in source, command.__name__
        assert "_e0_authorization(lab)" not in source, command.__name__


def test_validation_commands_keep_validation_scope():
    from agent_workspace import lab_bridge as lb

    for command in (lb.cmd_e0_validate_plan, lb.cmd_e0_validate_execute):
        source = inspect.getsource(command)
        assert "_e0_authorization(lab)" in source, command.__name__


def test_bounded_run_error_is_structured():
    from agent_workspace import lab_bridge as lb

    assert issubclass(lb._BoundedRunError, RuntimeError)
    params = inspect.signature(lb._BoundedRunError.__init__).parameters
    for name in ("returncode", "timed_out", "cancelled", "outcome", "log_path"):
        assert name in params, name


def test_run_bounded_cwd():
    from agent_workspace import lab_bridge as lb

    sig = inspect.signature(lb._run_bounded)
    assert "cwd" in sig.parameters
    assert sig.parameters["cwd"].default is None


def test_stage_task_setup_required_param():
    from agent_workspace import lab_bridge as lb

    sig = inspect.signature(lb._e0_stage_task_setup)
    assert "required_mechanisms" in sig.parameters
    assert sig.parameters["required_mechanisms"].default is None


def test_authorization_scopes_exist():
    from agent_workspace import lab_bridge as lb

    assert hasattr(lb, "_e0_authorization_scoped")
    assert lb._E0_AUDIT_REQUIRED_KEYS == lb._E0_BASE_REQUIRED_KEYS + ("audit_run_root",)
    assert "audit" in lb._E0_OPERATIONS
    assert {"build", "validation", "audit"}.issubset(lb._E0_SCOPES)
