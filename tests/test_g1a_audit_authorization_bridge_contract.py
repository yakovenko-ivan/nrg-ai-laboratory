"""Regression tests for the G1-A bridge/core nested authorization contract.

Run in the NRG AI Laboratory test suite. All privileged functions are stubbed;
no compiler, NRG executable, Git process, or scientific audit is launched.
"""

from argparse import Namespace
from pathlib import Path

from agent_workspace import lab_bridge as lb
from agent_workspace import l0_audit


def _scoped_auth():
    return {
        "commit": "a" * 40,
        "source_sha256": "b" * 64,
        "audit": {
            "build_commit": "a" * 40,
            "executable_sha256": "c" * 64,
            "cases": [
                {"id": "M2", "mechanism": "ammonia/OKAFOR.txt"},
                {"id": "M1", "mechanism": "hydrogen/KEROMNES.txt"},
            ],
            "cases_sha256": "d" * 64,
        },
    }


def test_l0_host_derives_required_mechanisms_from_nested_audit(monkeypatch, tmp_path):
    auth = _scoped_auth()
    cfg = {"equilibrium_executable": "audit_executable", "build_type": "release"}
    lab = object()
    monkeypatch.setattr(lb, "_e0_nrg_root", lambda *_: tmp_path)
    monkeypatch.setattr(lb, "_e0_git_objects", lambda *_: {})
    monkeypatch.setattr(lb, "_e0_path", lambda _cfg, name, _lab: tmp_path / name)
    monkeypatch.setattr(lb, "_e0_audit_run_root", lambda *_: tmp_path / "audit_run")
    observed = {}

    def fake_stage(_cfg, scoped_auth, _lab, dest, *, required_mechanisms):
        observed["auth"] = scoped_auth
        observed["destination"] = dest
        observed["required_mechanisms"] = required_mechanisms
        return {"staged_files": {}}

    monkeypatch.setattr(lb, "_e0_stage_task_setup", fake_stage)
    host = lb._l0_audit_host(cfg, auth, lab)
    target = tmp_path / "staging"
    host.stage_task_setup(target)
    assert observed["auth"] is auth
    assert observed["destination"] == target
    assert observed["required_mechanisms"] == (
        "ammonia/OKAFOR.txt", "hydrogen/KEROMNES.txt"
    )


def test_l0_plan_passes_normalized_audit_not_outer_scoped_auth(monkeypatch):
    auth = _scoped_auth()
    monkeypatch.setattr(lb, "_e0_config", lambda *_args, **_kw: {})
    monkeypatch.setattr(lb, "_e0_authorization_scoped", lambda *_: auth)
    host = object()
    monkeypatch.setattr(lb, "_l0_audit_host", lambda *_: host)
    calls = []

    def fake_plan(given_host, given_auth):
        calls.append((given_host, given_auth))
        return {"can_run": True}

    monkeypatch.setattr(l0_audit, "plan", fake_plan)
    monkeypatch.setattr(lb, "emit", lambda payload, status=0: status)
    assert lb.cmd_l0_audit_plan(Namespace(), object()) == 0
    assert calls == [(host, auth["audit"])]


def test_l0_execute_passes_normalized_audit_and_exact_bindings(monkeypatch):
    auth = _scoped_auth()
    monkeypatch.setattr(lb, "_e0_config", lambda *_args, **_kw: {})
    monkeypatch.setattr(lb, "_e0_authorization_scoped", lambda *_: auth)
    host = object()
    monkeypatch.setattr(lb, "_l0_audit_host", lambda *_: host)
    calls = []

    def fake_execute(given_host, given_auth, expected, *, continue_on_failure):
        calls.append((given_host, given_auth, expected, continue_on_failure))
        return {"complete": True}

    monkeypatch.setattr(l0_audit, "execute", fake_execute)
    monkeypatch.setattr(lb, "emit", lambda payload, status=0: status)
    args = Namespace(
        expected_executable_sha256="c" * 64,
        expected_commit="a" * 40,
        expected_source_sha256="b" * 64,
        expected_cases_sha256="d" * 64,
        continue_on_failure=False,
    )
    assert lb.cmd_l0_audit_execute(args, object()) == 0
    assert calls == [
        (
            host,
            auth["audit"],
            {
                "executable_sha256": "c" * 64,
                "commit": "a" * 40,
                "source_sha256": "b" * 64,
                "cases_sha256": "d" * 64,
            },
            False,
        )
    ]
