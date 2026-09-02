#!/usr/bin/env python3
"""Standalone execution client for an exported NRG campaign bundle.

The executor is intentionally agent-independent and Python-stdlib-only.  A bundle
may therefore be generated/audited on one machine and executed on another with
only Python 3.11+ and a compatible NRG computing_module binary.

Commands:
    validate       verify bundle contract and local NRG runtime
    run/resume     run all currently runnable cases sequentially
    status         inspect current execution state
    pause          pause now (checkpoint) or after the current case
    stop           terminally stop the active case/campaign
    pack-results   create a portable result archive for import by the laboratory
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import zipfile
from typing import Any, Iterable

BUNDLE_SCHEMA_VERSION = 1
EXECUTOR_SCHEMA_VERSION = 1
RESULTS_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def safe_relative(value: str) -> Path:
    pure = PurePosixPath(str(value).replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"bundle path must be relative and traversal-free: {value!r}")
    return Path(*pure.parts)


def load_cases(bundle_root: Path) -> list[dict[str, str]]:
    path = bundle_root / "cases.csv"
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("bundle cases.csv contains no cases")
    ids = [str(row.get("case_id", "")).strip() for row in rows]
    if any(not cid for cid in ids) or len(set(ids)) != len(ids):
        raise ValueError("bundle cases.csv has empty or duplicate case_id values")
    for row in rows:
        safe_relative(row.get("case_path") or row.get("case_relpath") or "")
    return rows


def case_dir(bundle_root: Path, row: dict[str, str]) -> Path:
    rel = safe_relative(row.get("case_relpath") or row["case_path"])
    return (bundle_root / "runs" / rel).resolve()


def parse_nrg_status(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    import re

    reason_match = re.search(r"termination_reason\s*=\s*['\"]([^'\"]+)['\"]", text, re.I)
    restart_match = re.search(
        r"restart_checkpoint_required\s*=\s*\.(true|false)\.", text, re.I
    )
    final_match = re.search(
        r"final_simulation_time\s*=\s*([+\-0-9.eEdD]+)", text, re.I
    )
    wall_match = re.search(r"elapsed_wall_time\s*=\s*([+\-0-9.eEdD]+)", text, re.I)

    def number(match: Any) -> float | None:
        if not match:
            return None
        try:
            return float(match.group(1).replace("D", "E").replace("d", "e"))
        except ValueError:
            return None

    return {
        "termination_reason": reason_match.group(1).strip() if reason_match else None,
        "restart_required": (
            restart_match.group(1).lower() == "true" if restart_match else None
        ),
        "final_simulation_time_s": number(final_match),
        "elapsed_wall_time_s": number(wall_match),
    }


def setup_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    import re

    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\bcase_fingerprint\s*=\s*['\"]([^'\"]+)['\"]", text, re.I)
    return match.group(1).strip() if match else None


def read_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    runtime = data.get("runtime", {})
    execution = data.get("execution", {})
    termination = data.get("termination", {})
    if not isinstance(runtime, dict) or not isinstance(execution, dict):
        raise ValueError("execution config requires [runtime] and [execution] tables")
    if not isinstance(termination, dict):
        raise ValueError("[termination] must be a TOML table")

    raw_exe = str(runtime.get("computing_module", "")).strip()
    if not raw_exe or raw_exe == "CHANGE_ME":
        raise ValueError("set [runtime].computing_module in execution.local.toml")
    exe = Path(os.path.expandvars(raw_exe)).expanduser()
    if not exe.is_absolute():
        exe = (path.parent / exe).resolve()
    else:
        exe = exe.resolve()
    if not exe.is_file():
        raise FileNotFoundError(f"computing_module not found: {exe}")

    threads = int(execution.get("threads", 1))
    if threads <= 0:
        raise ValueError("execution.threads must be positive")
    backend = str(execution.get("backend", "local")).strip().lower()
    if backend != "local":
        raise ValueError("portable executor v1 currently supports backend='local' only")

    profile_name = str(termination.get("profile_name", "")).strip()
    registry_path: Path | None = None
    if profile_name:
        raw_registry = str(termination.get("profile_registry", "termination_profiles.json")).strip()
        if not raw_registry:
            raise ValueError("termination.profile_registry must not be empty when profile_name is set")
        registry_path = Path(os.path.expandvars(raw_registry)).expanduser()
        if not registry_path.is_absolute():
            registry_path = (path.parent / registry_path).resolve()
        else:
            registry_path = registry_path.resolve()
        if not registry_path.is_file():
            raise FileNotFoundError(f"termination profile registry not found: {registry_path}")

    return {
        "path": path.resolve(),
        "computing_module": exe,
        "threads": threads,
        "backend": backend,
        "poll_seconds": max(0.05, float(execution.get("poll_seconds", 0.25))),
        "graceful_pause_timeout_seconds": max(
            1.0, float(execution.get("graceful_pause_timeout_seconds", 120.0))
        ),
        "rerun_failed": bool(execution.get("rerun_failed", False)),
        "continue_on_failure": bool(execution.get("continue_on_failure", True)),
        "limit_library_threads": bool(execution.get("limit_library_threads", True)),
        "require_exact_binary": bool(runtime.get("require_exact_binary", False)),
        "termination_profile_name": profile_name or None,
        "termination_profile_registry": registry_path,
    }


def verify_checksum_file(bundle_root: Path, checksum_name: str) -> list[str]:
    checksum_path = bundle_root / checksum_name
    if not checksum_path.is_file():
        return [f"{checksum_name} is missing"]
    problems: list[str] = []
    for raw in checksum_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            expected, rel = raw.split("  ", 1)
        except ValueError:
            problems.append(f"malformed checksum line: {raw}")
            continue
        target = bundle_root / safe_relative(rel)
        if not target.is_file():
            problems.append(f"missing static file: {rel}")
            continue
        observed = sha256_file(target)
        if observed != expected:
            problems.append(f"checksum mismatch: {rel}")
    return problems


def verify_static_checksums(bundle_root: Path) -> list[str]:
    return verify_checksum_file(bundle_root, "STATIC_CHECKSUMS.sha256")


def verify_export_snapshot(bundle_root: Path) -> list[str]:
    return verify_checksum_file(bundle_root, "EXPORT_SNAPSHOT_CHECKSUMS.sha256")


# Conservative classic-Windows path-length guard.  The nominal MAX_PATH limit on
# classic Windows is 260 characters; we enforce 240 to leave deliberate headroom
# for runtime-created suffixes (restart files, output segments, temporary names)
# and for the external NRG/Fortran runtime's own path handling.  Python's own
# long-path support does NOT imply the external executable/runtime is safe.
# Phase-1 does not inspect or modify Windows registry/group-policy long-path
# settings.
WINDOWS_PATH_SAFE_LIMIT = 240


def detected_platform() -> str:
    """Return a normalized platform name for path-budget enforcement."""
    return "Windows" if os.name == "nt" else platform.system()


def collect_runtime_paths(bundle_root: Path, computing_module: Any = None) -> list[Path]:
    """Collect actual resolved compute-host runtime paths for the bundle.

    The scan is recursive and structure-agnostic so current and future nested
    content (case directories, ``task_setup`` trees, setup/input files, mechanism
    /thermo/transport resources, initial/restart ``data_output``, executor
    metadata/configuration, nested results already present during resume) all
    participate naturally without a hard-coded filename list.  Generation-host
    provenance paths are deliberately not evaluated.
    """
    resolved_root = bundle_root.resolve()
    paths: list[Path] = [resolved_root]
    for path in resolved_root.rglob("*"):
        paths.append(path.resolve())
    if computing_module is not None:
        cm = Path(str(computing_module))
        if cm.exists():
            paths.append(cm.resolve())
    return paths


def audit_path_budget(
    bundle_root: Path,
    computing_module: Any,
    platform_name: str,
    safe_limit: int,
) -> dict[str, Any]:
    """Measure actual resolved compute-host path lengths against a host budget.

    This is a pure helper accepting an explicit platform indicator and safe limit
    so the Windows policy can be exercised deterministically on Linux.  On
    non-Windows hosts the Windows limit is reported as a diagnostic only and
    never as a hard error.
    """
    entries = [
        {"path": str(p), "length": len(str(p))}
        for p in collect_runtime_paths(bundle_root, computing_module)
    ]
    # Longest first, with a stable secondary ordering by path string.
    entries.sort(key=lambda item: (-item["length"], item["path"]))
    max_observed = entries[0]["length"] if entries else 0
    offending = [item for item in entries if item["length"] > safe_limit]
    enforced = platform_name == "Windows"
    ok = (not enforced) or (max_observed <= safe_limit)
    payload: dict[str, Any] = {
        "platform": platform_name,
        "enforced": enforced,
        "safe_limit": safe_limit,
        "max_observed_length": max_observed,
        "ok": ok,
        "offending_paths": offending,
    }
    if enforced and not ok:
        payload["recommendation"] = (
            "Extract or move the portable bundle to a shorter root path."
        )
    return payload


def _has_initial_data_output(case_path: Path) -> bool:
    """Return True when data_output contains at least one file at any depth."""
    data_output = case_path / "data_output"
    return data_output.is_dir() and any(
        path.is_file() for path in data_output.rglob("*")
    )

def validate_bundle(bundle_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    manifest_path = bundle_root / "bundle_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"bundle manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    if int(manifest.get("bundle_schema_version", -1)) != BUNDLE_SCHEMA_VERSION:
        raise ValueError("unsupported bundle schema version")
    rows = load_cases(bundle_root)
    if int(manifest.get("case_count", -1)) != len(rows):
        raise ValueError("bundle manifest case_count disagrees with cases.csv")

    static_problems = verify_static_checksums(bundle_root)
    first_execution = not (bundle_root / "executor_job.json").is_file()
    snapshot_problems = verify_export_snapshot(bundle_root) if first_execution else []
    case_problems: list[str] = []
    expected = manifest.get("cases", {}) if isinstance(manifest.get("cases"), dict) else {}
    for row in rows:
        cid = row["case_id"]
        cp = case_dir(bundle_root, row)
        if not cp.is_dir():
            case_problems.append(f"{cid}: case directory missing: {cp}")
            continue
        fp = str(row.get("case_fingerprint", "")).strip()
        expected_fp = str((expected.get(cid) or {}).get("case_fingerprint", "")).strip()
        if not fp or fp != expected_fp:
            case_problems.append(f"{cid}: fingerprint mismatch between cases.csv and bundle manifest")
        observed_setup_fp = setup_fingerprint(cp / "setup_input.nml")
        if observed_setup_fp and observed_setup_fp.lower() != fp.lower():
            case_problems.append(f"{cid}: setup_input.nml fingerprint mismatch")
        if not (cp / "task_setup").is_dir():
            case_problems.append(f"{cid}: task_setup directory missing")
        data_output = cp / "data_output"
        if not _has_initial_data_output(cp):
            case_problems.append(f"{cid}: initial/restart data_output state missing")

    termination_problems: list[str] = []
    termination_meta: dict[str, Any] | None = None
    if config.get("termination_profile_name"):
        try:
            selected = resolve_termination(bundle_root, config, manifest)
            assert selected is not None
            termination_meta = {
                key: value for key, value in selected.items()
                if key not in {"module", "profile"}
            }
        except Exception as exc:
            termination_problems.append(str(exc))

    exe_sha = sha256_file(config["computing_module"])
    expected_sha = str(
        ((manifest.get("source_runtime") or {}).get("computing_module_sha256") or "")
    ).strip()
    binary_match = bool(expected_sha) and exe_sha == expected_sha
    if config["require_exact_binary"] and expected_sha and not binary_match:
        raise ValueError(
            "local computing_module SHA-256 differs from the source runtime and "
            "runtime.require_exact_binary=true"
        )

    path_budget = audit_path_budget(
        bundle_root, config["computing_module"], detected_platform(), WINDOWS_PATH_SAFE_LIMIT
    )
    path_budget_problems: list[str] = []
    if path_budget["enforced"] and not path_budget["ok"]:
        path_budget_problems.append(
            "host runtime path budget exceeded: max_observed_length="
            f"{path_budget['max_observed_length']} > safe_limit={path_budget['safe_limit']}"
        )

    valid = (
        not static_problems
        and not snapshot_problems
        and not case_problems
        and not termination_problems
        and not path_budget_problems
    )
    return {
        "valid": valid,
        "bundle_id": manifest.get("bundle_id"),
        "campaign_contract_sha256": manifest.get("campaign_contract_sha256"),
        "case_count": len(rows),
        "static_integrity_problems": static_problems,
        "export_snapshot_checked": first_execution,
        "export_snapshot_integrity_problems": snapshot_problems,
        "case_contract_problems": case_problems,
        "termination_integrity_problems": termination_problems,
        "termination": termination_meta,
        "path_budget": path_budget,
        "path_budget_problems": path_budget_problems,
        "runtime": {
            "computing_module": str(config["computing_module"]),
            "computing_module_sha256": exe_sha,
            "source_computing_module_sha256": expected_sha or None,
            "exact_binary_match": binary_match if expected_sha else None,
            "exact_binary_required": config["require_exact_binary"],
            "threads": config["threads"],
            "backend": config["backend"],
        },
    }


def _load_verified_qs_module(bundle_root: Path, manifest: dict[str, Any]):
    """Verify the bundled evaluator before importing/executing it."""
    import importlib.util

    info = manifest.get("termination") or {}
    expected = str(info.get("quasistationary_core_sha256") or "").strip()
    core_path = bundle_root / "quasistationary_core.py"
    if not expected:
        raise ValueError("bundle manifest does not record quasistationary_core_sha256")
    if not core_path.is_file():
        raise FileNotFoundError(f"bundled quasistationarity evaluator missing: {core_path}")
    observed = sha256_file(core_path)
    if observed != expected:
        raise ValueError(
            "bundled quasistationarity evaluator checksum mismatch: "
            f"expected={expected}, observed={observed}"
        )
    spec = importlib.util.spec_from_file_location("nrg_portable_quasistationary_core", core_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load bundled quasistationarity evaluator: {core_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, observed


def resolve_termination(
    bundle_root: Path,
    config: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the selected execution profile and record override provenance.

    The bundle carries a reviewed default registry.  execution.local.toml may
    deliberately select another profile or another registry; the exact selected
    registry and profile are hashed and recorded in each run status.
    """
    name = config.get("termination_profile_name")
    if not name:
        return None
    registry = config.get("termination_profile_registry")
    if not isinstance(registry, Path) or not registry.is_file():
        raise FileNotFoundError(f"selected termination registry not found: {registry}")

    module, core_sha = _load_verified_qs_module(bundle_root, manifest)
    profile = module.load_profile(registry, str(name))
    registry_sha = sha256_file(registry)
    profile_sha = module.profile_sha256(profile)
    bundled = manifest.get("termination") or {}
    default_name = str(bundled.get("default_profile") or "").strip() or None
    default_registry_sha = str(bundled.get("profile_registry_sha256") or "").strip() or None
    override = bool(
        (default_name and str(name) != default_name)
        or (default_registry_sha and registry_sha != default_registry_sha)
    )
    return {
        "module": module,
        "profile": profile,
        "profile_name": str(name),
        "profile_sha256": profile_sha,
        "profile_parameters": profile.to_registry_mapping(),
        "profile_registry": str(registry),
        "profile_registry_sha256": registry_sha,
        "quasistationary_core_sha256": core_sha,
        "bundle_default_profile": default_name,
        "override_applied": override,
    }


class BundleLock:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".executor.lock"
        self.meta = self.path / "owner.json"

    def __enter__(self) -> "BundleLock":
        try:
            self.path.mkdir()
        except FileExistsError:
            owner = read_json(self.meta) if self.meta.is_file() else {}
            pid = int(owner.get("pid") or 0)
            if process_alive(pid):
                raise RuntimeError(f"portable executor already active with pid={pid}")
            shutil.rmtree(self.path, ignore_errors=True)
            self.path.mkdir()
        write_json(self.meta, {"pid": os.getpid(), "started_at_utc": utc_now()})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def current_status(case_path: Path) -> dict[str, Any] | None:
    path = case_path / "run_status.json"
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def child_env(limit_library_threads: bool) -> dict[str, str]:
    env = os.environ.copy()
    if limit_library_threads:
        for name in (
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "BLIS_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            env[name] = "1"
    return env


def control_file(root: Path) -> Path:
    return root / "executor.control.json"


def read_pending_control(root: Path) -> dict[str, Any] | None:
    path = control_file(root)
    if not path.is_file():
        return None
    try:
        payload = read_json(path)
    except Exception:
        return None
    if str(payload.get("state", "")).lower() != "requested":
        return None
    return payload


def update_control(root: Path, request: dict[str, Any], state: str, **updates: Any) -> None:
    payload = dict(request)
    payload.update(updates)
    payload["state"] = state
    payload["updated_at_utc"] = utc_now()
    write_json(control_file(root), payload)


def wait_for_process_stop(process: subprocess.Popen[Any], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.25)
    return process.poll() is not None


def clean_runtime_markers(cp: Path) -> None:
    for name in (
        "run_control.pause",
        "run_control.stop",
        "run_control_status.inf",
        "run_finished.done",
        "run_condition_met.done",
        "run_paused.done",
        "run_pause_failed.done",
        "run_restart_required.done",
        "run_stopped.done",
        "run_failed.done",
        "run_interrupted.done",
    ):
        try:
            (cp / name).unlink()
        except FileNotFoundError:
            pass


def recover_stale_running(rows: list[dict[str, str]], root: Path) -> None:
    for row in rows:
        cp = case_dir(root, row)
        status = current_status(cp)
        if not status or str(status.get("status", "")).lower() != "running":
            continue
        pid = int(status.get("process_pid") or 0)
        if process_alive(pid):
            raise RuntimeError(
                f"case {row['case_id']} still has a live computing_module pid={pid}; "
                "refusing duplicate execution"
            )
        status.update(
            {
                "status": "interrupted",
                "end_time": utc_now(),
                "termination_condition": "executor_interrupted",
                "termination_message": "Recovered stale running state; case is runnable again.",
            }
        )
        write_json(cp / "run_status.json", status)
        (cp / "run_interrupted.done").write_text(
            "Recovered stale portable-executor running state.\n", encoding="utf-8"
        )


def run_case(
    root: Path,
    row: dict[str, str],
    config: dict[str, Any],
    bundle_manifest: dict[str, Any],
) -> tuple[str, bool]:
    cid = row["case_id"]
    cp = case_dir(root, row)
    previous = current_status(cp)
    previous_status = str((previous or {}).get("status", "not_started")).lower()
    clean_runtime_markers(cp)

    termination = config.get("_termination_runtime")
    profile = termination.get("profile") if isinstance(termination, dict) else None
    qs_module = termination.get("module") if isinstance(termination, dict) else None

    started = utc_now()
    exe_sha = sha256_file(config["computing_module"])
    status: dict[str, Any] = {
        "case_id": cid,
        "case_fingerprint": row.get("case_fingerprint"),
        "case_identity_fingerprint": row.get("case_identity_fingerprint"),
        "attempt_fingerprint": row.get("generated_attempt_fingerprint") or row.get("attempt_fingerprint"),
        "status": "running",
        "start_time": started,
        "end_time": None,
        "process_pid": None,
        "execution_origin": "portable_bundle_v1",
        "bundle_id": bundle_manifest.get("bundle_id"),
        "campaign_contract_sha256": bundle_manifest.get("campaign_contract_sha256"),
        "execution_segment_previous_status": previous_status,
        "computing_module": str(config["computing_module"]),
        "computing_module_sha256": exe_sha,
        "threads": config["threads"],
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "nrg_termination_reason": None,
        "nrg_restart_required": None,
        "nrg_final_simulation_time_s": None,
        "termination_condition": None,
        "termination_message": None,
        "termination_profile": termination.get("profile_name") if termination else None,
        "termination_profile_sha256": termination.get("profile_sha256") if termination else None,
        "termination_profile_parameters": termination.get("profile_parameters") if termination else None,
        "termination_profile_registry": termination.get("profile_registry") if termination else None,
        "termination_profile_registry_sha256": termination.get("profile_registry_sha256") if termination else None,
        "quasistationary_core_sha256": termination.get("quasistationary_core_sha256") if termination else None,
        "termination_override_applied": termination.get("override_applied") if termination else None,
        "physical_condition_met": False if termination else None,
        "physical_condition_status": None,
        "physical_condition": None,
        "product_state": None,
    }
    write_json(cp / "run_status.json", status)

    command = [str(config["computing_module"]), f"--num_threads={config['threads']}"]
    stdout_path = cp / "computing_module.stdout.log"
    stderr_path = cp / "computing_module.stderr.log"
    with stdout_path.open("ab", buffering=0) as out, stderr_path.open("ab", buffering=0) as err:
        marker = f"\n=== PORTABLE EXECUTION SEGMENT {started} previous={previous_status} ===\n".encode()
        out.write(marker)
        err.write(marker)
        process = subprocess.Popen(
            command,
            cwd=str(cp),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=child_env(config["limit_library_threads"]),
        )
        status["process_pid"] = process.pid
        write_json(cp / "run_status.json", status)

        pause_action: str | None = None
        pause_request: dict[str, Any] | None = None
        campaign_pause_after_current = False
        terminal_stop = False
        physical_triggered = False
        physical_result: dict[str, Any] | None = None
        physical_stop_requested_at: str | None = None
        last_physical_check = -1.0e300

        while process.poll() is None:
            time.sleep(config["poll_seconds"])
            request = read_pending_control(root)
            if request is not None:
                action = str(request.get("action", "")).lower()
                if action == "pause_after_current":
                    campaign_pause_after_current = True
                    pause_request = request
                    update_control(
                        root, request, "acknowledged", active_case_id=cid,
                        outcome="pause_queued_after_current",
                    )
                elif action == "pause_now":
                    pause_action = action
                    pause_request = request
                    update_control(root, request, "acknowledged", active_case_id=cid)
                    (cp / "run_control.pause").write_text(
                        f"reason=portable_executor_pause\nrequested_at={utc_now()}\n",
                        encoding="utf-8",
                    )
                    stopped = wait_for_process_stop(process, config["graceful_pause_timeout_seconds"])
                    if not stopped:
                        update_control(
                            root, request, "rejected",
                            reason="nrg_did_not_honor_pause_before_timeout",
                            active_case_id=cid,
                        )
                        raise RuntimeError(
                            f"NRG did not honor pause for {cid} within "
                            f"{config['graceful_pause_timeout_seconds']} s; process left running"
                        )
                    break
                elif action == "stop":
                    terminal_stop = True
                    pause_request = request
                    update_control(root, request, "acknowledged", active_case_id=cid)
                    (cp / "run_control.stop").write_text(
                        f"reason=portable_executor_stop\nrequested_at={utc_now()}\n",
                        encoding="utf-8",
                    )
                    stopped = wait_for_process_stop(process, config["graceful_pause_timeout_seconds"])
                    if not stopped:
                        update_control(root, request, "rejected", reason="nrg_did_not_stop_before_timeout")
                        raise RuntimeError(f"NRG did not honor terminal stop for {cid}")
                    break
                else:
                    update_control(root, request, "rejected", reason="unsupported_action")

            if process.poll() is not None or profile is None or qs_module is None:
                continue
            now_wall = time.monotonic()
            if now_wall - last_physical_check < profile.check_wall_interval_s:
                continue
            last_physical_check = now_wall
            try:
                qs = qs_module.evaluate_case(cp, profile)
            except FileNotFoundError:
                # Reactor history may not exist during initial solver startup.
                continue
            except qs_module.TransientHistoryError:
                # A concurrently appended final record may be incomplete; retry.
                continue
            except Exception as exc:
                raise RuntimeError(f"physical termination monitor failed for {cid}: {exc}") from exc

            physical_result = qs.to_dict()
            write_json(
                cp / "quasistationary_status.json",
                {
                    "observed_at_utc": utc_now(),
                    "profile": termination["profile_name"],
                    "profile_sha256": termination["profile_sha256"],
                    "profile_registry_sha256": termination["profile_registry_sha256"],
                    "quasistationary_core_sha256": termination["quasistationary_core_sha256"],
                    **physical_result,
                },
            )
            if not qs.reached:
                continue

            physical_triggered = True
            physical_stop_requested_at = utc_now()
            (cp / "run_control.stop").write_text(
                f"reason=physical:{termination['profile_name']}\n"
                f"requested_at={physical_stop_requested_at}\n"
                f"condition={termination['profile_name']}\n"
                f"simulation_time_s={qs.history_end_time_s}\n",
                encoding="utf-8",
            )
            stopped = wait_for_process_stop(process, config["graceful_pause_timeout_seconds"])
            if not stopped:
                raise RuntimeError(
                    f"NRG did not honor physical stop for {cid} within "
                    f"{config['graceful_pause_timeout_seconds']} s"
                )
            break

        exit_code = process.wait()

    core = parse_nrg_status(cp / "run_control_status.inf") or {}
    reason = core.get("termination_reason")
    restart = core.get("restart_required")
    final_sim = core.get("final_simulation_time_s")
    end = utc_now()
    status.update(
        {
            "end_time": end,
            "exit_code": exit_code,
            "process_pid": None,
            "nrg_termination_reason": reason,
            "nrg_restart_required": restart,
            "nrg_final_simulation_time_s": final_sim,
            "nrg_elapsed_wall_time_s": core.get("elapsed_wall_time_s"),
            "physical_condition_met": physical_triggered if termination else None,
            "physical_condition_status": physical_result.get("status") if physical_result else None,
            "physical_condition": physical_result,
        }
    )
    if physical_triggered and physical_result:
        status["product_state"] = {
            "temperature_K": physical_result.get("product_temperature_K"),
            "pressure_Pa": physical_result.get("product_pressure_Pa"),
            "density_kg_m3": physical_result.get("product_density_kg_m3"),
            "species_mass_fractions": physical_result.get("product_species_mass_fractions", {}),
            "averaging_window_start_s": physical_result.get("window_start_time_s"),
            "averaging_window_end_s": physical_result.get("window_end_time_s"),
        }

    must_pause_runner = False
    if physical_triggered:
        graceful = exit_code == 0 and reason == "external_stop_request"
        if graceful:
            status.update(
                status="condition_met",
                termination_condition=termination["profile_name"],
                termination_message="Post-ignition quasistationary product-state condition met.",
            )
            (cp / "run_condition_met.done").write_text(
                f"Condition: {termination['profile_name']}\n"
                f"Requested at: {physical_stop_requested_at}\n"
                f"Simulation time [s]: {physical_result.get('history_end_time_s')}\n",
                encoding="utf-8",
            )
        else:
            status.update(
                status="failed",
                termination_condition="physical_condition_met_but_stop_unverified",
                termination_message=(
                    "Physical condition was reached, but NRG did not complete the expected "
                    f"graceful external-stop contract (reason={reason}, exit_code={exit_code})."
                ),
            )
            (cp / "run_failed.done").write_text(status["termination_message"] + "\n", encoding="utf-8")
            must_pause_runner = not config["continue_on_failure"]
    elif pause_action == "pause_now":
        verified = exit_code == 0 and reason == "external_pause_request" and restart is True
        if verified:
            status.update(
                status="paused",
                termination_condition="operator_campaign_pause",
                termination_message="Portable execution paused with a verified NRG restart checkpoint.",
            )
            (cp / "run_paused.done").write_text(
                f"Paused at {end}; simulation_time_s={final_sim}.\n", encoding="utf-8"
            )
            outcome = "pause_verified"
        else:
            status.update(
                status="pause_failed",
                termination_condition="operator_campaign_pause_unverified",
                termination_message="Pause did not produce the required NRG restart contract.",
            )
            (cp / "run_pause_failed.done").write_text(
                f"reason={reason}; restart_required={restart}; exit_code={exit_code}.\n",
                encoding="utf-8",
            )
            outcome = "pause_failed"
        if pause_request is not None:
            update_control(
                root, pause_request, "handled", active_case_id=cid, outcome=outcome,
                nrg_termination_reason=reason, nrg_restart_required=restart,
            )
        must_pause_runner = True
    elif terminal_stop:
        status.update(
            status="stopped",
            termination_condition="operator_campaign_stop",
            termination_message="Portable execution terminally stopped by operator request.",
        )
        (cp / "run_stopped.done").write_text(f"Stopped at {end}.\n", encoding="utf-8")
        if pause_request is not None:
            update_control(root, pause_request, "handled", active_case_id=cid, outcome="case_stopped")
        must_pause_runner = True
    elif exit_code == 0 and reason == "wall_time_limit" and restart is True:
        status.update(
            status="restart_required",
            termination_condition="wall_time_limit",
            termination_message="NRG reached wall-time limit and wrote a restart checkpoint.",
        )
        (cp / "run_restart_required.done").write_text(
            f"Restart required at {end}; simulation_time_s={final_sim}.\n", encoding="utf-8"
        )
        must_pause_runner = True
    elif exit_code == 0 and reason == "final_simulation_time":
        status.update(
            status="finished",
            termination_condition="final_simulation_time",
            termination_message="NRG reached configured final simulation time.",
        )
        (cp / "run_finished.done").write_text(f"Finished at {end}.\n", encoding="utf-8")
    elif exit_code == 0 and reason == "external_stop_request":
        status.update(
            status="external_stop",
            termination_condition="external_stop_request",
            termination_message="NRG observed a terminal external stop not originated by this executor command.",
        )
        must_pause_runner = not config["continue_on_failure"]
    elif exit_code == 0 and restart is True:
        status.update(
            status="restart_required",
            termination_condition=reason or "restart_required",
            termination_message="NRG ended cleanly and requested restart.",
        )
        must_pause_runner = True
    elif exit_code == 0:
        status.update(
            status="finished",
            termination_condition=reason or "accepted_exit",
            termination_message="NRG exited successfully.",
        )
        (cp / "run_finished.done").write_text(f"Finished at {end}; reason={reason}.\n", encoding="utf-8")
    else:
        status.update(
            status="failed",
            termination_condition="process_exit",
            termination_message=f"computing_module exited with code {exit_code}",
        )
        (cp / "run_failed.done").write_text(status["termination_message"] + "\n", encoding="utf-8")
        must_pause_runner = not config["continue_on_failure"]

    write_json(cp / "run_status.json", status)

    if campaign_pause_after_current:
        if pause_request is not None:
            update_control(
                root, pause_request, "handled", active_case_id=cid,
                outcome="current_case_completed_runner_paused", case_status=status["status"],
            )
        must_pause_runner = True
    return str(status["status"]), must_pause_runner


def collect_failed_case_ids(rows: list[dict[str, str]], root: Path) -> list[str]:
    """Return failed case IDs in cases.csv order from final per-case states."""
    failed: list[str] = []
    for row in rows:
        st = current_status(case_dir(root, row)) or {}
        if str(st.get("status", "")).lower() == "failed":
            failed.append(row["case_id"])
    return failed


def status_payload(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "bundle_manifest.json")
    rows = load_cases(root)
    counts: dict[str, int] = {}
    active: list[dict[str, Any]] = []
    sample: list[dict[str, Any]] = []
    for row in rows:
        cp = case_dir(root, row)
        st = current_status(cp)
        state = str((st or {}).get("status", "not_started")).lower()
        counts[state] = counts.get(state, 0) + 1
        if state == "running":
            active.append({"case_id": row["case_id"], "process_pid": (st or {}).get("process_pid")})
        if len(sample) < 12:
            sample.append({
                "case_id": row["case_id"],
                "status": state,
                "nrg_termination_reason": (st or {}).get("nrg_termination_reason"),
                "nrg_final_simulation_time_s": (st or {}).get("nrg_final_simulation_time_s"),
            })
    lock_owner = None
    owner_path = root / ".executor.lock" / "owner.json"
    if owner_path.is_file():
        try:
            lock_owner = read_json(owner_path)
        except Exception:
            lock_owner = None
    job_path = root / "executor_job.json"
    job = read_json(job_path) if job_path.is_file() else {}
    # Completion summary is completed-only metadata; do not fabricate it for
    # active/paused/stopped/failed/non-completed jobs.
    completion: dict[str, Any] = {}
    if isinstance(job, dict) and str(job.get("state", "")).lower() == "completed":
        completion = {
            "completion_outcome": job.get("completion_outcome"),
            "failed_case_count": job.get("failed_case_count", 0),
            "failed_case_ids": job.get("failed_case_ids", []),
        }
    return {
        "bundle_id": manifest.get("bundle_id"),
        "campaign_contract_sha256": manifest.get("campaign_contract_sha256"),
        "case_count": len(rows),
        "counts": dict(sorted(counts.items())),
        "active_cases": active,
        "executor_lock": lock_owner,
        "executor_job": job,
        "completion": completion,
        "control": read_json(control_file(root)) if control_file(root).is_file() else None,
        "sample": sample,
    }


def handle_control_between_cases(root: Path) -> bool:
    request = read_pending_control(root)
    if request is None:
        return False
    action = str(request.get("action", "")).lower()
    if action in {"pause_now", "pause_after_current", "stop"}:
        update_control(
            root,
            request,
            "handled",
            active_case_id=None,
            outcome="runner_paused_between_cases" if action != "stop" else "runner_stopped_between_cases",
        )
        return True
    update_control(root, request, "rejected", reason="unsupported_action")
    return False


def run_bundle(root: Path, config_path: Path) -> int:
    config = read_config(config_path)
    validation = validate_bundle(root, config)
    if not validation["valid"]:
        print(json.dumps(validation, indent=2))
        return 3
    rows = load_cases(root)
    manifest = read_json(root / "bundle_manifest.json")
    config["_termination_runtime"] = resolve_termination(root, config, manifest)
    with BundleLock(root):
        recover_stale_running(rows, root)
        ctl = control_file(root)
        if ctl.is_file():
            try:
                old = read_json(ctl)
            except Exception:
                old = {}
            if str(old.get("state", "")).lower() != "requested":
                ctl.unlink(missing_ok=True)

        term = config.get("_termination_runtime")
        job = {
            "schema_version": EXECUTOR_SCHEMA_VERSION,
            "state": "running",
            "pid": os.getpid(),
            "started_at_utc": utc_now(),
            "bundle_id": manifest.get("bundle_id"),
            "computing_module": str(config["computing_module"]),
            "computing_module_sha256": sha256_file(config["computing_module"]),
            "threads": config["threads"],
            "termination_profile": term.get("profile_name") if term else None,
            "termination_profile_sha256": term.get("profile_sha256") if term else None,
            "termination_profile_parameters": term.get("profile_parameters") if term else None,
            "termination_profile_registry_sha256": term.get("profile_registry_sha256") if term else None,
            "quasistationary_core_sha256": term.get("quasistationary_core_sha256") if term else None,
            "termination_override_applied": term.get("override_applied") if term else None,
        }
        write_json(root / "executor_job.json", job)
        stop_runner = False
        last_case_state: str | None = None
        for row in rows:
            if handle_control_between_cases(root):
                stop_runner = True
                break
            cp = case_dir(root, row)
            st = current_status(cp)
            state = str((st or {}).get("status", "not_started")).lower()
            if state in {"finished", "condition_met"}:
                continue
            if state == "failed" and not config["rerun_failed"]:
                continue
            if state == "pause_failed":
                raise RuntimeError(
                    f"case {row['case_id']} is pause_failed; inspect/recover it before resume"
                )
            job["active_case_id"] = row["case_id"]
            write_json(root / "executor_job.json", job)
            case_state, stop_runner = run_case(root, row, config, manifest)
            last_case_state = case_state
            if stop_runner:
                break
        if stop_runner:
            control = read_json(ctl) if ctl.is_file() else {}
            control_outcome = str(control.get("outcome", ""))
            if last_case_state == "restart_required":
                job_state = "restart_required"
            elif last_case_state == "paused":
                job_state = "paused"
            elif last_case_state == "stopped":
                job_state = "stopped"
            elif last_case_state in {"failed", "pause_failed", "external_stop"}:
                job_state = "failed"
            elif "paused" in control_outcome or control_outcome == "current_case_completed_runner_paused":
                job_state = "paused"
            elif "stopped" in control_outcome:
                job_state = "stopped"
            else:
                job_state = "paused"
        else:
            job_state = "completed"
        # Completion outcome metadata is completed-only: it is attached solely
        # when the terminal executor state is "completed", never for fail-fast,
        # stopped, paused, restart-required, or otherwise non-completed jobs.
        completion_payload: dict[str, Any] = {}
        if job_state == "completed":
            failed_case_ids = collect_failed_case_ids(rows, root)
            completion_payload = {
                "completion_outcome": (
                    "completed_with_failures" if failed_case_ids else "completed_cleanly"
                ),
                "failed_case_count": len(failed_case_ids),
                "failed_case_ids": failed_case_ids,
            }
        job.update(
            state=job_state,
            active_case_id=None,
            finished_at_utc=utc_now(),
            **completion_payload,
        )
        write_json(root / "executor_job.json", job)
    return 0


def issue_control(root: Path, action: str, wait_seconds: float) -> dict[str, Any]:
    owner_path = root / ".executor.lock" / "owner.json"
    if not owner_path.is_file():
        return {"accepted": False, "error": "no active portable executor lock found"}
    owner = read_json(owner_path)
    pid = int(owner.get("pid") or 0)
    if not process_alive(pid):
        return {"accepted": False, "error": "executor lock is stale", "owner": owner}
    path = control_file(root)
    if path.is_file():
        current = read_json(path)
        if str(current.get("state", "")).lower() in {"requested", "acknowledged"}:
            return {"accepted": False, "error": "another control request is already pending", "control": current}
    request = {
        "schema_version": 1,
        "state": "requested",
        "request_id": datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}",
        "action": action,
        "requested_at_utc": utc_now(),
    }
    write_json(path, request)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    observed = request
    while time.monotonic() <= deadline:
        time.sleep(0.2)
        if path.is_file():
            observed = read_json(path)
        state = str(observed.get("state", "")).lower()
        if state in {"handled", "rejected"}:
            break
        if action == "pause_after_current" and state == "acknowledged":
            break
    return {
        "accepted": str(observed.get("state", "")).lower() in {"acknowledged", "handled"},
        "completed": str(observed.get("state", "")).lower() == "handled",
        "control": observed,
    }


def touched_result_rows(
    root: Path,
    rows: Iterable[dict[str, str]],
    bundle_id: str,
) -> list[dict[str, str]]:
    """Return cases actually executed/updated inside this portable bundle.

    Prepared bundles may already contain completed local cases. Those cases are
    skipped by the portable executor and need not be copied back to the source
    laboratory, where they already exist.
    """
    selected: list[dict[str, str]] = []
    for row in rows:
        st = current_status(case_dir(root, row)) or {}
        if (
            str(st.get("execution_origin", "")) == "portable_bundle_v1"
            and str(st.get("bundle_id", "")) == bundle_id
        ):
            selected.append(row)
    return selected


def iter_result_files(root: Path, rows: Iterable[dict[str, str]]) -> Iterable[tuple[Path, str]]:
    # Include portable contract files needed for import verification.
    for name in ("bundle_manifest.json", "cases.csv"):
        path = root / name
        if path.is_file():
            yield path, name
    for row in rows:
        cp = case_dir(root, row)
        rel_case = safe_relative(row.get("case_relpath") or row["case_path"])
        for path in sorted(cp.rglob("*")):
            if not path.is_file():
                continue
            # Transient control markers are not scientific results.
            if path.name in {"run_control.pause", "run_control.stop"}:
                continue
            rel = Path("runs") / rel_case / path.relative_to(cp)
            yield path, rel.as_posix()


def pack_results(root: Path, output: Path) -> Path:
    if (root / ".executor.lock" / "owner.json").is_file():
        owner = read_json(root / ".executor.lock" / "owner.json")
        if process_alive(int(owner.get("pid") or 0)):
            raise RuntimeError("cannot pack results while portable executor is active")
    rows = load_cases(root)
    manifest = read_json(root / "bundle_manifest.json")
    bundle_id = str(manifest.get("bundle_id", ""))
    returned_rows = touched_result_rows(root, rows, bundle_id)
    files = list(iter_result_files(root, returned_rows))
    inventory = {rel: sha256_file(path) for path, rel in files}
    statuses = {}
    for row in returned_rows:
        st = current_status(case_dir(root, row)) or {"status": "not_started"}
        statuses[row["case_id"]] = {
            "status": st.get("status"),
            "case_fingerprint": row.get("case_fingerprint"),
            "case_identity_fingerprint": row.get("case_identity_fingerprint") or row.get("case_fingerprint"),
            "attempt_fingerprint": row.get("generated_attempt_fingerprint") or row.get("attempt_fingerprint"),
            "nrg_termination_reason": st.get("nrg_termination_reason"),
            "nrg_restart_required": st.get("nrg_restart_required"),
            "nrg_final_simulation_time_s": st.get("nrg_final_simulation_time_s"),
            "computing_module_sha256": st.get("computing_module_sha256"),
            "physical_condition_met": st.get("physical_condition_met"),
            "termination_profile": st.get("termination_profile"),
            "termination_profile_sha256": st.get("termination_profile_sha256"),
            "termination_override_applied": st.get("termination_override_applied"),
        }
    job = read_json(root / "executor_job.json") if (root / "executor_job.json").is_file() else {}
    results_manifest = {
        "results_schema_version": RESULTS_SCHEMA_VERSION,
        "created_at_utc": utc_now(),
        "bundle_id": manifest.get("bundle_id"),
        "campaign_contract_sha256": manifest.get("campaign_contract_sha256"),
        "case_count": len(returned_rows),
        "campaign_case_count": len(rows),
        "returned_case_ids": [row["case_id"] for row in returned_rows],
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "executor_job": job,
        "cases": statuses,
        "files_sha256": inventory,
    }
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        zf.writestr("results_manifest.json", json.dumps(results_manifest, indent=2, sort_keys=True) + "\n")
        for path, rel in files:
            zf.write(path, rel)
    return output


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("validate", "run", "resume"):
        s = sub.add_parser(name)
        if name in {"validate", "run", "resume"}:
            s.add_argument("--config", type=Path, default=Path("execution.local.toml"))
    sub.add_parser("status")
    s = sub.add_parser("pause")
    s.add_argument("--mode", choices=("now", "after-current"), default="now")
    s.add_argument("--wait-seconds", type=float, default=45.0)
    s = sub.add_parser("stop")
    s.add_argument("--wait-seconds", type=float, default=45.0)
    s = sub.add_parser("pack-results")
    s.add_argument("--output", type=Path, required=True)
    return p


def main() -> int:
    args = build_parser().parse_args()
    root = args.bundle.expanduser().resolve()
    if not (root / "bundle_manifest.json").is_file():
        raise SystemExit(f"not an NRG execution bundle: {root}")
    if args.command == "status":
        print(json.dumps(status_payload(root), indent=2, sort_keys=True))
        return 0
    if args.command == "pause":
        action = "pause_now" if args.mode == "now" else "pause_after_current"
        payload = issue_control(root, action, args.wait_seconds)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("accepted") else 3
    if args.command == "stop":
        payload = issue_control(root, "stop", args.wait_seconds)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("accepted") else 3
    if args.command == "pack-results":
        output = pack_results(root, args.output)
        print(json.dumps({"results_archive": str(output)}, indent=2))
        return 0

    config_path = args.config.expanduser()
    if not config_path.is_absolute():
        config_path = (root / config_path).resolve()
    config = read_config(config_path)
    if args.command == "validate":
        payload = validate_bundle(root, config)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 3
    if args.command in {"run", "resume"}:
        return run_bundle(root, config_path)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
