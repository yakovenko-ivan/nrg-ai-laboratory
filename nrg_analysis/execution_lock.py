"""Single-runner execution lock for the NRG laboratory.

The lock is OS-managed and therefore released automatically when the runner
process exits or the VM is powered off.  A companion JSON file is metadata only;
it is never used as the locking primitive.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

LOCK_FILE_NAME = "laboratory_runner.lock"
LOCK_STATE_NAME = "laboratory_runner_state.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_root(campaign_root: Path) -> Path:
    return (Path(campaign_root) / "_jobs").resolve()


def runner_lock_path(campaign_root: Path) -> Path:
    return _jobs_root(campaign_root) / LOCK_FILE_NAME


def runner_lock_state_path(campaign_root: Path) -> Path:
    return _jobs_root(campaign_root) / LOCK_STATE_NAME


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"state_file_error": f"cannot parse {path}"}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ensure_lock_byte(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)


def _try_lock(handle) -> bool:
    """Try to acquire the process lock without blocking."""
    if os.name == "nt":
        import msvcrt
        _ensure_lock_byte(handle)
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def probe_runner_lock(campaign_root: Path) -> dict[str, Any]:
    """Return whether another laboratory runner currently owns the lock."""
    lock_path = runner_lock_path(campaign_root)
    state_path = runner_lock_state_path(campaign_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        acquired = _try_lock(handle)
        if acquired:
            _unlock(handle)
    state = _read_state(state_path)
    return {
        "active": not acquired,
        "lock_file": str(lock_path),
        "state_file": str(state_path),
        "owner": state if not acquired else None,
        "last_owner": state if acquired and state else None,
    }


class RunnerAlreadyActive(RuntimeError):
    pass


@dataclass
class LaboratoryRunnerLock:
    campaign_root: Path
    metadata: dict[str, Any]
    _handle: Any = None

    def acquire(self) -> "LaboratoryRunnerLock":
        lock_path = runner_lock_path(self.campaign_root)
        state_path = runner_lock_state_path(self.campaign_root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = lock_path.open("a+b")
        if not _try_lock(self._handle):
            owner = _read_state(state_path)
            self._handle.close()
            self._handle = None
            raise RunnerAlreadyActive(
                "another NRG campaign runner already owns the laboratory execution lock"
                + (f": {owner}" if owner else "")
            )
        state = {
            **self.metadata,
            "state": "running",
            "runner_pid": os.getpid(),
            "acquired_at_utc": utc_now(),
        }
        _write_state(state_path, state)
        return self

    def update(self, **values: Any) -> None:
        state_path = runner_lock_state_path(self.campaign_root)
        state = _read_state(state_path)
        state.update(values)
        _write_state(state_path, state)

    def release(self) -> None:
        if self._handle is None:
            return
        state_path = runner_lock_state_path(self.campaign_root)
        state = _read_state(state_path)
        state.update({"state": "released", "released_at_utc": utc_now()})
        _write_state(state_path, state)
        _unlock(self._handle)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "LaboratoryRunnerLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
