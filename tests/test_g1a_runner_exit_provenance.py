"""Regression: trusted runner log exit header must agree with structured child exit."""
import sys
from pathlib import Path
import pytest
from agent_workspace import lab_bridge as lb


def test_bounded_runner_nonzero_exit_and_log_provenance(tmp_path):
    log = tmp_path / "nonzero.log"
    with pytest.raises(lb._BoundedRunError) as exc_info:
        lb._run_bounded(
            [sys.executable, "-c", "import sys; print('unsupported kinetics directive: PLOG'); sys.exit(128)"],
            "g1a synthetic refusal", 10, log, cwd=tmp_path
        )
    exc = exc_info.value
    assert exc.returncode == 128
    assert exc.outcome == "EXIT_128"
    assert not exc.timed_out and not exc.cancelled
    data = log.read_text(encoding="utf-8")
    assert "# result: EXIT_128\n" in data
    assert "unsupported kinetics directive: PLOG" in data


def test_bounded_runner_zero_exit_and_log_provenance(tmp_path):
    log = tmp_path / "success.log"
    out = lb._run_bounded(
        [sys.executable, "-c", "print('ok')"],
        "g1a synthetic success", 10, log, cwd=tmp_path
    )
    assert out.strip() == "ok"
    assert "# result: EXIT_0\n" in log.read_text(encoding="utf-8")
