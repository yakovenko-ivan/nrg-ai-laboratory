"""Short, structured bridge between Pi tools and trusted NRG laboratory scripts.

The Pi extension calls this module instead of constructing arbitrary shell commands.
All output is JSON on stdout.  Long CFD campaigns are launched as detached runner
jobs; analysis studies are executed synchronously through the existing wrapper.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import tomllib
from typing import Any

from nrg_analysis import __version__ as PROJECT_VERSION
from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import sha256_file
from nrg_analysis.namelist import string_value
from nrg_analysis.execution_lock import probe_runner_lock
from nrg_analysis.campaign import Campaign
from campaign_tools.campaign_identity import (
    apply_overrides,
    get_value as identity_get_value,
    identity_fingerprint,
    load_policy_for_generated,
    validate_overrides,
)
from campaign_tools.campaign_attempts import (
    effective_field_value,
    inventory_case,
    load_base_groups,
    load_override_record,
    materialize_effective_setup,
    reset_case_to_generated,
    write_case_attempt_config,
)
from nrg_analysis.quasistationary import (
    evaluate_case as evaluate_quasistationary_case,
    load_profile as load_quasistationary_profile,
    profile_file_sha256,
)

from agent_workspace.dev_transactions import (
    transaction_plan as dev_transaction_plan,
    transaction_start as dev_transaction_start,
    transaction_status as dev_transaction_status,
    transaction_list as dev_transaction_list,
    read_file as dev_read_file,
    apply_patch_plan as dev_apply_patch_plan,
    apply_patch_execute as dev_apply_patch_execute,
    write_plan as dev_write_plan,
    write_execute as dev_write_execute,
    edit_plan as dev_edit_plan,
    edit_execute as dev_edit_execute,
    diff as dev_diff,
    test_plan as dev_test_plan,
    test_execute as dev_test_execute,
    commit_plan as dev_commit_plan,
    commit_execute as dev_commit_execute,
    discard_plan as dev_discard_plan,
    discard_execute as dev_discard_execute,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any], rc: int = 0) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return rc


def resolve(path: str | Path, base: Path | None = None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and base is not None:
        p = base / p
    return p.resolve()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def case_path(row: dict[str, str], lab: Laboratory) -> Path:
    raw = Path(row["case_path"]).expanduser()
    return raw.resolve() if raw.is_absolute() else (lab.runs_root / raw).resolve()


def load_cases(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"campaign manifest contains no cases: {path}")
    return rows


def campaign_status_payload(
    cases_csv: Path,
    lab: Laboratory,
    run_config: Path | None = None,
    *,
    include_runnable_case_ids: bool = False,
) -> dict[str, Any]:
    rows = load_cases(cases_csv)
    counts: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    skip_statuses: set[str] = set()
    rerun_failed = True
    cfg: dict[str, Any] = {}
    if run_config is not None and run_config.is_file():
        cfg = read_json(run_config)
        skip_statuses = {str(x).lower() for x in cfg.get("skip_statuses", [])}
        rerun_failed = bool(cfg.get("rerun_failed", True))

    runnable = 0
    runnable_case_ids: list[str] = []
    skipped_counts: Counter[str] = Counter()
    runnable_counts: Counter[str] = Counter()
    for row in rows:
        cp = case_path(row, lab)
        if not cp.is_dir():
            status = "missing_case"
        else:
            sp = cp / "run_status.json"
            if not sp.is_file():
                status = "not_started"
            else:
                inspected = inspect_json_file(sp)
                if inspected.get("valid_json"):
                    status = str(inspected["data"].get("status", "invalid_status")).lower()
                else:
                    status = "invalid_status"
        counts[status] += 1
        skip = status in skip_statuses
        if status == "failed" and rerun_failed:
            skip = False
        if skip:
            skipped_counts[status] += 1
        if status not in {"missing_case", "running"} and not skip:
            runnable += 1
            runnable_case_ids.append(str(row.get("case_id", "")).strip())
            runnable_counts[status] += 1
        detail = {"case_id": row.get("case_id", ""), "status": status, "case_path": str(cp)}
        if status == "invalid_status":
            sp = cp / "run_status.json"
            inspected = inspect_json_file(sp)
            detail["run_status"] = {
                k: inspected.get(k)
                for k in ("path", "exists", "size_bytes", "empty", "whitespace_only", "valid_json", "parse_error")
            }
        details.append(detail)

    runner_lock = probe_runner_lock(lab.campaign_root)
    raw_running = counts.get("running", 0)
    active_running = raw_running if runner_lock.get("active") else 0
    stale_running = raw_running if not runner_lock.get("active") else 0
    # A stale running status is recoverable by runner v5 and therefore counts as
    # runnable for launch planning, although the status file is not mutated here.
    runnable += stale_running
    if stale_running:
        runnable_counts["stale_running"] += stale_running
        runnable_case_ids.extend(
            str(d.get("case_id", "")).strip()
            for d in details
            if d.get("status") == "running" and str(d.get("case_id", "")).strip()
        )

    execution_policy = {
        "run_config": str(run_config) if run_config is not None else None,
        "run_config_sha256": sha256_file(run_config) if run_config is not None and run_config.is_file() else None,
        "threads": int(cfg.get("threads", lab.default_threads)) if cfg else lab.default_threads,
        "max_concurrent_cases": int(cfg.get("max_concurrent_cases", 1)) if cfg else 1,
        "limit_library_threads": bool(cfg.get("limit_library_threads", True)) if cfg else True,
        "skip_statuses": sorted(skip_statuses),
        "rerun_failed": rerun_failed,
        "max_runtime_seconds": cfg.get("max_runtime_seconds") if cfg else None,
    }

    payload = {
        "cases_csv": str(cases_csv),
        "total_cases": len(rows),
        "counts": dict(sorted(counts.items())),
        "execution_policy": execution_policy,
        "skipped_by_policy": dict(sorted(skipped_counts.items())),
        "runnable_by_status": dict(sorted(runnable_counts.items())),
        "runnable_cases": runnable,
        "missing_cases": counts.get("missing_case", 0),
        "running_cases": active_running,
        "stale_running_cases": stale_running,
        "status_file_running_cases": raw_running,
        "laboratory_runner": runner_lock,
        "completed_like_cases": sum(counts.get(k, 0) for k in ("finished", "condition_met")),
        "invalid_status_cases": [d for d in details if d["status"] == "invalid_status"][:10],
        "sample": details[:12],
    }
    if include_runnable_case_ids:
        # Full IDs are intentionally opt-in: ordinary campaign status stays compact,
        # while launch planning can schedule every runnable case regardless of campaign size.
        payload["runnable_case_ids"] = runnable_case_ids
    return payload


def cmd_case_inspect(args: argparse.Namespace, lab: Laboratory) -> int:
    """Inspect one campaign case using structured filesystem/process metadata."""
    cases_csv = resolve(args.cases)
    rows = load_cases(cases_csv)
    matches = [row for row in rows if str(row.get("case_id", "")) == args.case_id]
    if not matches:
        return emit({"error": f"case_id not found in manifest: {args.case_id}", "cases_csv": str(cases_csv)}, 2)
    if len(matches) != 1:
        return emit({"error": f"case_id is not unique in manifest: {args.case_id}", "matches": len(matches)}, 2)

    row = matches[0]
    cp = case_path(row, lab)
    status_path = cp / "run_status.json"
    status_info = inspect_json_file(status_path)

    parsed: dict[str, Any] | None = status_info.get("data") if status_info.get("valid_json") else None
    pid = 0
    if parsed is not None:
        try:
            pid = int(parsed.get("process_pid") or 0)
        except (TypeError, ValueError):
            pid = 0

    pid_alive = process_alive(pid) if pid > 0 else False
    pid_matches = process_matches_executable(pid, lab.computing_module) if pid > 0 and pid_alive else False

    marker_names = (
        "run_finished.done",
        "run_condition_met.done",
        "run_failed.done",
        "run_timeout.done",
        "run_restart_required.done",
        "run_external_stop.done",
        "run_interrupted.done",
        "run_paused.done",
        "run_pause_failed.done",
        "run_control.stop",
        "run_control.pause",
        "run_control_status.inf",
    )
    markers = [name for name in marker_names if (cp / name).exists()]

    selected_status: dict[str, Any] | None = None
    if parsed is not None:
        selected_status = {
            key: parsed.get(key)
            for key in (
                "status",
                "start_time",
                "end_time",
                "duration_s",
                "exit_code",
                "termination_condition",
                "termination_message",
                "nrg_termination_reason",
                "nrg_restart_required",
                "nrg_final_simulation_time_s",
                "nrg_elapsed_wall_time_s",
                "attempt_id",
                "attempt_fingerprint",
                "runner_job_id",
                "selective_rerun_job_id",
                "termination_profile",
                "physical_condition_met",
                "physical_condition_status",
                "process_pid",
                "requested_openmp_threads",
                "max_concurrent_cases",
            )
            if key in parsed
        }

    payload = {
        "cases_csv": str(cases_csv),
        "case_id": args.case_id,
        "case_fingerprint": row.get("case_fingerprint", ""),
        "case_path": str(cp),
        "case_directory_exists": cp.is_dir(),
        "task_setup_exists": (cp / "task_setup").is_dir(),
        "run_status_file": {
            k: status_info.get(k)
            for k in ("path", "exists", "size_bytes", "empty", "whitespace_only", "valid_json", "parse_error")
        },
        "run_status": selected_status,
        "recorded_process": {
            "pid": pid if pid > 0 else None,
            "alive": pid_alive,
            "matches_trusted_computing_module": pid_matches,
            "trusted_executable": str(lab.computing_module),
        },
        "present_markers": markers,
        "identity_policy": (
            load_policy_for_generated(cases_csv)
            if (cases_csv.parent / "campaign.toml").is_file()
            else None
        ),
        "attempt_override": load_override_record(cases_csv, args.case_id),
        "attempt_config": (
            inspect_json_file(cp / "attempt_config.json").get("data")
            if inspect_json_file(cp / "attempt_config.json").get("valid_json")
            else None
        ),
        "identifier_semantics": {
            "attempt_id": (
                "recalculation/configuration lineage created by reset/attempt preparation; "
                "not a runner job identifier and may span multiple runner jobs"
            ),
            "runner_job_id": (
                "background runner invocation for the current run_status; legacy v0.5 "
                "selective reruns may expose this as selective_rerun_job_id"
            ),
        },
        "laboratory_runner": probe_runner_lock(lab.campaign_root),
    }
    return emit(payload)


def jobs_root(lab: Laboratory) -> Path:
    return (lab.campaign_root / "_jobs").resolve()


def job_path(lab: Laboratory, job_id: str) -> Path:
    return jobs_root(lab) / f"{job_id}.json"


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_matches_executable(pid: int, executable: Path) -> bool:
    """Best-effort guard against PID reuse when inspecting a recorded child PID."""
    if not process_alive(pid):
        return False
    if os.name != "nt":
        proc_exe = Path(f"/proc/{pid}/exe")
        try:
            return proc_exe.resolve(strict=True) == executable.resolve()
        except OSError:
            return False
    return True


def job_control_path(lab: Laboratory, job_id: str) -> Path:
    return jobs_root(lab) / f"{job_id}.control.json"


def resolve_job_record(lab: Laboratory, job_ref: str | None = None) -> tuple[Path, dict[str, Any]]:
    if job_ref:
        jp = resolve(job_ref) if ("/" in job_ref or job_ref.endswith(".json")) else job_path(lab, job_ref)
        if not jp.is_file():
            raise FileNotFoundError(f"job not found: {jp}")
        return jp, read_json(jp)

    live: list[tuple[Path, dict[str, Any]]] = []
    root = jobs_root(lab)
    if root.is_dir():
        for jp in sorted(root.glob("*.json")):
            if jp.name.endswith(".control.json"):
                continue
            try:
                record = read_json(jp)
            except Exception:
                continue
            pid = int(record.get("runner_pid") or 0)
            if process_alive(pid) and str(record.get("state", "")).lower() in {"running", "launching"}:
                live.append((jp, record))
    if not live:
        raise RuntimeError("no live campaign runner job found")
    if len(live) != 1:
        raise RuntimeError(f"expected one live campaign runner job, found {len(live)}")
    return live[0]


def active_case_for_job(record: dict[str, Any], lab: Laboratory) -> dict[str, Any] | None:
    manifest_text = str(record.get("manifest") or "").strip()
    if not manifest_text:
        return None
    manifest = Path(manifest_text).expanduser().resolve()
    if not manifest.is_file():
        return None
    job_id = str(record.get("job_id") or "").strip()
    for row in load_cases(manifest):
        cp = case_path(row, lab)
        sp = cp / "run_status.json"
        if not sp.is_file():
            continue
        try:
            status = read_json(sp)
        except Exception:
            continue
        if str(status.get("status", "")).lower() != "running":
            continue
        status_job = str(status.get("runner_job_id") or status.get("selective_rerun_job_id") or "").strip()
        if job_id and status_job and status_job != job_id:
            continue
        pid = int(status.get("process_pid") or 0)
        return {
            "case_id": str(row.get("case_id", "")),
            "case_path": str(cp),
            "process_pid": pid or None,
            "process_alive": process_alive(pid),
            "process_matches_computing_module": process_matches_executable(pid, lab.computing_module) if pid else False,
            "run_status": status,
        }
    return None


def read_operator_control(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def operator_stop_plan(
    lab: Laboratory,
    *,
    action: str,
    job_ref: str | None = None,
    cases: Path | None = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    try:
        jp, record = resolve_job_record(lab, job_ref)
    except Exception as exc:
        return {"can_stop": False, "error": str(exc)}
    runner_pid = int(record.get("runner_pid") or 0)
    runner_alive = process_alive(runner_pid)
    active_case = active_case_for_job(record, lab)
    blockers: list[dict[str, Any]] = []
    if not runner_alive:
        blockers.append({"reason": "runner_not_alive", "runner_pid": runner_pid or None})
    if action == "stop_case":
        if cases is None or case_id is None:
            blockers.append({"reason": "case_target_required"})
        else:
            manifest = Path(str(record.get("manifest") or "")).expanduser().resolve()
            if manifest != cases.resolve():
                blockers.append({
                    "reason": "case_manifest_does_not_match_active_runner",
                    "active_manifest": str(manifest),
                    "requested_manifest": str(cases.resolve()),
                })
            if active_case is None:
                blockers.append({"reason": "no_active_case"})
            elif active_case.get("case_id") != case_id:
                blockers.append({
                    "reason": "requested_case_is_not_active",
                    "active_case_id": active_case.get("case_id"),
                    "requested_case_id": case_id,
                })
            elif not active_case.get("process_matches_computing_module"):
                blockers.append({
                    "reason": "active_case_process_not_verified",
                    "process_pid": active_case.get("process_pid"),
                })

    job_id = str(record.get("job_id") or jp.stem)
    command = record.get("command") if isinstance(record.get("command"), list) else []
    control_supported = bool(record.get("control_file")) and "--control-file" in [str(x) for x in command]
    if not control_supported:
        blockers.append({
            "reason": "runner_job_predates_operator_control",
            "message": "This runner job was not launched with the v0.5.8 trusted control channel.",
        })
    control_path = Path(str(record.get("control_file") or job_control_path(lab, job_id))).expanduser().resolve()
    existing = read_operator_control(control_path)
    superseded_control: dict[str, Any] | None = None
    if existing and str(existing.get("state", "")).lower() in {"requested", "acknowledged"}:
        existing_action = str(existing.get("action", "")).strip().lower()
        can_supersede = (
            existing_action == "pause_after_current"
            and action in {"pause_campaign", "stop_campaign"}
        )
        if can_supersede:
            superseded_control = existing
        else:
            blockers.append({"reason": "operator_control_already_pending", "control": existing})

    return {
        "can_stop": len(blockers) == 0,
        "action": action,
        "job_id": job_id,
        "job_file": str(jp),
        "runner_pid": runner_pid or None,
        "runner_alive": runner_alive,
        "job_type": record.get("job_type") or "campaign",
        "manifest": record.get("manifest"),
        "termination_profile": record.get("termination_profile"),
        "active_case": active_case,
        "requested_case_id": case_id,
        "control_file": str(control_path),
        "control_supported": control_supported,
        "superseded_control": superseded_control,
        "laboratory_runner": probe_runner_lock(lab.campaign_root),
        "blockers": blockers,
        "semantics": {
            "stop_campaign": "terminally stops the active case through run_control.stop and prevents later case launch",
            "stop_case": "terminally stops only the active case through run_control.stop; later cases may run",
            "pause_campaign": "requests run_control.pause for the active case, verifies a restart checkpoint, and stops the runner",
            "pause_after_current": "lets the active case finish normally and stops the runner before the next case; no checkpoint is requested",
        }.get(action, "unknown operator-control action"),
    }


def issue_operator_stop(
    lab: Laboratory,
    plan: dict[str, Any],
    *,
    reason: str | None = None,
    wait_seconds: float = 45.0,
) -> dict[str, Any]:
    if not plan.get("can_stop"):
        return {"error": "operator stop preflight failed", "plan": plan}
    control_path = Path(str(plan["control_file"])).expanduser().resolve()
    request_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    request = {
        "schema_version": 1,
        "state": "requested",
        "request_id": request_id,
        "action": plan["action"],
        "runner_job_id": plan["job_id"],
        "case_id": plan.get("requested_case_id"),
        "requested_at_utc": utc_now(),
        "reason": reason or "explicit_user_request",
        "source": "trusted_lab_bridge",
    }
    superseded = plan.get("superseded_control")
    if isinstance(superseded, dict):
        request["supersedes"] = {
            "request_id": superseded.get("request_id"),
            "action": superseded.get("action"),
            "state": superseded.get("state"),
        }
    write_json(control_path, request)

    jp = Path(str(plan["job_file"])).expanduser().resolve()
    try:
        job = read_json(jp)
    except Exception:
        job = {}
    job["operator_control_last_request"] = request
    write_json(jp, job)

    deadline = time.monotonic() + max(0.0, wait_seconds)
    observed = request
    while time.monotonic() <= deadline:
        current = read_operator_control(control_path)
        if current is not None:
            observed = current
            state = str(current.get("state", "")).lower()
            if state in {"handled", "rejected"}:
                break
            if plan["action"] == "pause_after_current" and state == "acknowledged":
                break
        if plan["action"] == "stop_campaign":
            try:
                live_job = read_json(jp)
                pid = int(live_job.get("runner_pid") or 0)
                if not process_alive(pid) and str(live_job.get("state", "")).lower() == "stopped":
                    break
            except Exception:
                pass
        time.sleep(0.2)

    final_job = read_json(jp) if jp.is_file() else {}
    final_active_case = active_case_for_job(final_job, lab) if final_job else None
    return {
        "request": request,
        "control": observed,
        "completed": str(observed.get("state", "")).lower() == "handled",
        "accepted": str(observed.get("state", "")).lower() in {"acknowledged", "handled"},
        "rejected": str(observed.get("state", "")).lower() == "rejected",
        "job": final_job,
        "active_case": final_active_case,
        "laboratory_runner": probe_runner_lock(lab.campaign_root),
        "note": (
            "If completed=false and rejected=false, the request remains pending; inspect the job with nrg_campaign_job_status."
        ),
    }


def inspect_json_file(path: Path) -> dict[str, Any]:
    """Inspect a JSON file without making the caller infer emptiness from read output."""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": None,
        "empty": None,
        "whitespace_only": None,
        "valid_json": False,
        "parse_error": None,
    }
    if not path.is_file():
        return result

    try:
        raw = path.read_bytes()
    except OSError as exc:
        result["parse_error"] = f"read_error: {exc}"
        return result

    result["size_bytes"] = len(raw)
    result["empty"] = len(raw) == 0
    stripped = raw.strip()
    result["whitespace_only"] = len(raw) > 0 and len(stripped) == 0
    if len(stripped) == 0:
        result["parse_error"] = "empty file" if len(raw) == 0 else "whitespace-only file"
        return result

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["valid_json"] = isinstance(payload, dict)
    if isinstance(payload, dict):
        result["data"] = payload
    else:
        result["parse_error"] = f"top-level JSON is {type(payload).__name__}, expected object"
    return result



def child_env() -> dict[str, str]:
    env = os.environ.copy()
    project_root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = project_root + (os.pathsep + existing if existing else "")
    return env



def campaign_definitions_root(lab: Laboratory) -> Path:
    return (lab.campaign_root / "definitions").resolve()


def resolve_campaign_definition(value: str | Path, lab: Laboratory) -> Path:
    """Resolve a campaign definition without making the agent guess absolute paths.

    New-style relative names are resolved below campaigns/definitions first.  For
    backward compatibility, paths relative to research_root are accepted as a
    fallback.
    """
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()

    definitions_candidate = (campaign_definitions_root(lab) / raw).resolve()
    if definitions_candidate.is_file():
        return definitions_candidate

    research_candidate = (lab.research_root / raw).resolve()
    if research_candidate.is_file():
        return research_candidate

    # Return the canonical definitions candidate so errors point to the expected
    # scientific workspace rather than the process cwd.
    return definitions_candidate


def cmd_campaign_list(args: argparse.Namespace, lab: Laboratory) -> int:
    root = campaign_definitions_root(lab)
    if not root.is_dir():
        return emit({"definitions_root": str(root), "campaigns": [], "count": 0})

    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.toml")):
        if not path.is_file():
            continue
        record: dict[str, Any] = {
            "file": path.name,
            "relative_path": str(path.relative_to(root)),
            "path": str(path.resolve()),
        }
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            campaign = data.get("campaign", {}) if isinstance(data, dict) else {}
            record.update({
                "name": campaign.get("name"),
                "generator_output_directory": campaign.get("generator_output_directory"),
                "case_id_prefix": campaign.get("case_id_prefix"),
            })
            sweep = data.get("sweep", {}) if isinstance(data, dict) else {}
            if isinstance(sweep, dict):
                record["sweep_sections"] = sorted(str(k) for k in sweep.keys())
        except Exception as exc:
            record["parse_error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)

    return emit({
        "definitions_root": str(root),
        "count": len(records),
        "campaigns": records,
    })


def cmd_study_list(args: argparse.Namespace, lab: Laboratory) -> int:
    root = lab.studies_root.resolve()
    records: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.name.startswith("_"):
                continue
            record: dict[str, Any] = {"name": path.name, "path": str(path.resolve())}
            manifest = path / "study_manifest.json"
            if manifest.is_file():
                try:
                    data = read_json(manifest)
                    record["status"] = data.get("status")
                    record["scientific_request"] = data.get("scientific_request")
                    record["cases_csv"] = data.get("cases_csv")
                except Exception as exc:
                    record["manifest_error"] = f"{type(exc).__name__}: {exc}"
            records.append(record)
    return emit({"studies_root": str(root), "count": len(records), "studies": records})

def _component_state(path: Path) -> dict[str, Any]:
    exists = path.is_file()
    executable = bool(exists and (os.name == "nt" or os.access(path, os.X_OK)))
    digest: str | None = None
    read_error: str | None = None
    if exists:
        try:
            digest = sha256_file(path)
        except OSError as exc:
            read_error = f"{type(exc).__name__}: {exc}"
    return {
        "path": str(path),
        "exists": bool(exists),
        "executable": executable,
        "readable": digest is not None,
        "sha256": digest,
        "read_error": read_error,
    }


def _component_ready(component: dict[str, Any]) -> bool:
    # Ready means present, executable, and hashable.  A file that exists and is
    # executable but cannot be read has no verified provenance and must not be
    # reported as a usable runtime component.
    return bool(component.get("executable") and component.get("readable"))


def _execution_runtime_error(lab: Laboratory) -> dict[str, Any] | None:
    try:
        lab.validate_execution_runtime()
        if not _component_ready(_component_state(lab.computing_module)):
            raise PermissionError(
                "computing_module exists and is executable but cannot be read for "
                f"provenance hashing: {lab.computing_module}"
            )
    except Exception as exc:
        return {
            "error": f"execution runtime unavailable: {type(exc).__name__}: {exc}",
            "runtime_ready": False,
        }
    return None


def _preparation_runtime_error(
    lab: Laboratory, interface: Path
) -> dict[str, Any] | None:
    try:
        if lab.task_setup_template.name != "task_setup":
            raise ValueError(
                "task_setup_template must point to a directory named 'task_setup'"
            )
        if not lab.task_setup_template.is_dir():
            raise FileNotFoundError(
                f"task_setup_template not found: {lab.task_setup_template}"
            )
        state = _component_state(interface)
        if not state["exists"]:
            raise FileNotFoundError(f"package_interface not found: {interface}")
        if not state["executable"]:
            raise PermissionError(f"package_interface is not executable: {interface}")
        if not state["readable"]:
            raise PermissionError(
                f"package_interface cannot be read for provenance hashing: {interface}"
            )
    except Exception as exc:
        return {
            "error": f"preparation runtime unavailable: {type(exc).__name__}: {exc}",
            "runtime_ready": False,
        }
    return None


def cmd_lab_info(args: argparse.Namespace, lab: Laboratory) -> int:
    computing_module = _component_state(lab.computing_module)
    task_setup = {
        "path": str(lab.task_setup_template),
        "exists": lab.task_setup_template.is_dir(),
        "named_task_setup": lab.task_setup_template.name == "task_setup",
    }
    package_interfaces = {
        name: _component_state(path)
        for name, path in sorted(lab.package_interfaces.items())
    }
    preparation_ready = {
        name: bool(
            task_setup["exists"]
            and task_setup["named_task_setup"]
            and _component_ready(component)
        )
        for name, component in package_interfaces.items()
    }
    preparation_ready.setdefault("0d", False)
    preparation_ready.setdefault("1d", False)
    unavailable: list[str] = []
    unreadable: list[str] = []
    if not _component_ready(computing_module):
        unavailable.append("computing_module")
    if (
        computing_module["exists"]
        and computing_module["executable"]
        and not computing_module["readable"]
    ):
        unreadable.append("computing_module")
    if not task_setup["exists"]:
        unavailable.append("task_setup")
    elif not task_setup["named_task_setup"]:
        unavailable.append("task_setup_name")
    for name, component in package_interfaces.items():
        if not _component_ready(component):
            unavailable.append(f"package_interface_{name}")
        if component["exists"] and component["executable"] and not component["readable"]:
            unreadable.append(f"package_interface_{name}")

    runtime = {
        "computing_module_sha256": computing_module["sha256"],
        "package_interface_0d_sha256": package_interfaces.get("0d", {}).get("sha256"),
        "computing_module": computing_module,
        "task_setup": task_setup,
        "package_interfaces": package_interfaces,
        "execution_ready": _component_ready(computing_module),
        "preparation_ready": preparation_ready,
        "preparation_ready_0d": preparation_ready["0d"],
        "preparation_ready_1d": preparation_ready["1d"],
        # runtime_available means at least one runtime capability is present.
        # It is NOT a statement that CFD execution is ready; use execution_ready
        # or preparation_ready_* for operational decisions.
        "runtime_available": bool(
            _component_ready(computing_module) or any(preparation_ready.values())
        ),
        "unavailable": unavailable,
        "unreadable": unreadable,
    }
    return emit({"laboratory": lab.to_dict(), "runtime": runtime, "bridge_version": PROJECT_VERSION})



# NRG compact bridge output schema v1
#
# Rich forensic records remain on disk.  These helpers expose only the
# operational state needed by a caller or agent.  --full restores the previous
# exhaustive payload for diagnostics.


def _compact_runner(runner: dict[str, Any] | None) -> dict[str, Any]:
    data = runner or {}
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    result: dict[str, Any] = {"active": bool(data.get("active"))}
    if owner:
        if owner.get("runner_pid") is not None:
            result["pid"] = owner.get("runner_pid")
        if owner.get("state") is not None:
            result["state"] = owner.get("state")
    return result


def _compact_active_case(active: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(active, dict):
        return None
    status = active.get("run_status") if isinstance(active.get("run_status"), dict) else {}
    result: dict[str, Any] = {
        "case_id": active.get("case_id") or status.get("case_id"),
        "status": status.get("status"),
    }
    pid = active.get("process_pid")
    if pid is None:
        pid = status.get("process_pid")
    if pid is not None:
        result["pid"] = pid
    if active.get("process_alive") is not None:
        result["process_alive"] = bool(active.get("process_alive"))
    if active.get("process_matches_computing_module") is not None:
        result["trusted_process"] = bool(active.get("process_matches_computing_module"))
    if active.get("case_path"):
        result["case_path"] = active.get("case_path")
    return {k: v for k, v in result.items() if v is not None}


def compact_campaign_status(payload: dict[str, Any]) -> dict[str, Any]:
    invalid = payload.get("invalid_status_cases")
    problems = {
        "missing_cases": int(payload.get("missing_cases", 0) or 0),
        "invalid_status_cases": len(invalid) if isinstance(invalid, list) else int(invalid or 0),
        "stale_running_cases": int(payload.get("stale_running_cases", 0) or 0),
    }
    result: dict[str, Any] = {
        "ok": "error" not in payload,
        "cases_csv": payload.get("cases_csv"),
        "total_cases": int(payload.get("total_cases", 0) or 0),
        "counts": payload.get("counts", {}),
        "runnable_cases": int(payload.get("runnable_cases", 0) or 0),
        "completed_like_cases": int(payload.get("completed_like_cases", 0) or 0),
        "runner": _compact_runner(payload.get("laboratory_runner")),
    }
    if any(problems.values()):
        result["problems"] = problems
    if payload.get("error"):
        result["error"] = payload.get("error")
    return result


def compact_campaign_start(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    return {
        "ok": "error" not in payload,
        "job_id": job.get("job_id"),
        "state": job.get("state"),
        "runner_pid": job.get("runner_pid"),
        "cases_csv": job.get("manifest") or status.get("cases_csv"),
        "cases": {
            "total": int(status.get("total_cases", 0) or 0),
            "runnable": int(status.get("runnable_cases", 0) or 0),
            "counts": status.get("counts", {}),
        },
        "references": {
            "control_file": job.get("control_file"),
            "stdout_log": job.get("stdout_log"),
            "stderr_log": job.get("stderr_log"),
        },
    }


def compact_job_status(payload: dict[str, Any], *, job_file: str | None = None) -> dict[str, Any]:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    campaign = payload.get("campaign_status") if isinstance(payload.get("campaign_status"), dict) else {}
    control = payload.get("operator_control") if isinstance(payload.get("operator_control"), dict) else None
    active = payload.get("active_case") if isinstance(payload.get("active_case"), dict) else None
    result: dict[str, Any] = {
        "ok": "error" not in payload,
        "job_id": job.get("job_id"),
        "state": job.get("state_observed") or job.get("state"),
        "runner_active": bool((payload.get("laboratory_runner") or {}).get("active")),
        "runner_pid": job.get("runner_pid"),
        "active_case": _compact_active_case(active),
        "campaign": {
            "total": int(campaign.get("total_cases", 0) or 0),
            "counts": campaign.get("counts", {}),
            "runnable": int(campaign.get("runnable_cases", 0) or 0),
        },
        "references": {
            "job_file": job_file,
            "stdout_log": job.get("stdout_log"),
            "stderr_log": job.get("stderr_log"),
        },
    }
    if control:
        result["operator_control"] = {
            k: control.get(k)
            for k in ("action", "state", "case_id", "outcome", "request_id")
            if control.get(k) is not None
        }
    return result


def compact_operator_plan(
    plan: dict[str, Any], *, pause: bool = False, mode: str | None = None
) -> dict[str, Any]:
    allowed = bool(plan.get("can_stop"))
    result: dict[str, Any] = {
        "ok": "error" not in plan,
        "action": plan.get("action"),
        "job_id": plan.get("job_id"),
        "runner_active": bool((plan.get("laboratory_runner") or {}).get("active")),
        "active_case": _compact_active_case(plan.get("active_case")),
        "blockers": plan.get("blockers", []),
        "semantics": plan.get("semantics"),
    }
    superseded = plan.get("superseded_control")
    if isinstance(superseded, dict):
        result["supersedes"] = {
            "request_id": superseded.get("request_id"),
            "action": superseded.get("action"),
            "state": superseded.get("state"),
        }
    if pause:
        result["mode"] = mode
        result["can_pause"] = allowed
    else:
        result["can_stop"] = allowed
    if plan.get("error"):
        result["error"] = plan.get("error")
    return result


def compact_operator_result(result: dict[str, Any]) -> dict[str, Any]:
    control = result.get("control") if isinstance(result.get("control"), dict) else {}
    request = result.get("request") if isinstance(result.get("request"), dict) else {}
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    runner = result.get("laboratory_runner") if isinstance(result.get("laboratory_runner"), dict) else {}
    out: dict[str, Any] = {
        "ok": "error" not in result,
        "accepted": bool(result.get("accepted", result.get("completed", False))),
        "completed": bool(result.get("completed")),
        "rejected": bool(result.get("rejected")),
        "job_id": job.get("job_id") or control.get("runner_job_id") or request.get("runner_job_id"),
        "state": job.get("state"),
        "runner_active": bool(runner.get("active")),
        "action": control.get("action") or request.get("action"),
        "control_state": control.get("state") or request.get("state"),
        "case_id": control.get("case_id"),
        "outcome": control.get("outcome"),
    }
    nrg = {
        "reason": control.get("nrg_termination_reason"),
        "restart_required": control.get("nrg_restart_required"),
        "exit_code": control.get("exit_code"),
        "graceful": control.get("graceful_stop_completed"),
        "force_kill_used": control.get("force_kill_used"),
    }
    if any(v is not None for v in nrg.values()):
        out["nrg"] = nrg
    if result.get("error"):
        out["error"] = result.get("error")
    if not out["completed"] and not out["rejected"]:
        out["pending"] = True
    return out


def cmd_campaign_status(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    # Pi uses the reviewed laboratory runner policy. `--run-config` remains
    # only as a private/manual CLI override for trusted diagnostics.
    config = resolve(args.run_config) if args.run_config else lab.runner_config
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    if not config.is_file():
        return emit({"error": f"trusted runner configuration not found: {config}"}, 2)
    payload = campaign_status_payload(cases, lab, config)
    return emit(payload if getattr(args, "full", False) else compact_campaign_status(payload))


def parse_override_items(items: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw in items:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"field", "value"}:
            raise ValueError("each override must contain exactly field and value")
        field = str(payload["field"]).strip()
        if not field:
            raise ValueError("override field may not be empty")
        if field in result:
            raise ValueError(f"duplicate override field: {field}")
        result[field] = payload["value"]
    return result


def cmd_campaign_identity_inspect(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    try:
        policy = load_policy_for_generated(cases)
        rows = load_cases(cases)
        samples = []
        for row in rows[: min(len(rows), 8)]:
            cid = str(row.get("case_id", ""))
            groups = load_base_groups(cases, cid)
            samples.append({
                "case_id": cid,
                "manifest_case_fingerprint": row.get("case_fingerprint"),
                "logical_identity_fingerprint": identity_fingerprint(groups, policy),
                "identity_values": {
                    field: identity_get_value(groups, field)
                    for field in policy.get("identity_fields", [])
                },
            })
    except Exception as exc:
        return emit({"error": str(exc)}, 2)

    manifest_path = cases.parent / "campaign_manifest.json"
    generator_schema = None
    manifest_scheme = None
    if manifest_path.is_file():
        try:
            manifest = read_json(manifest_path)
            generator_schema = manifest.get("generator_schema_version")
            manifest_scheme = manifest.get("case_fingerprint_scheme")
        except Exception:
            pass
    return emit({
        "cases_csv": str(cases),
        "case_count": len(rows),
        "identity_policy": policy,
        "generator_schema_version": generator_schema,
        "manifest_case_fingerprint_scheme": manifest_scheme,
        "legacy_compatibility_note": (
            "For a campaign generated before v0.5.0, existing case_fingerprint values "
            "remain unchanged for runtime compatibility. logical_identity_fingerprint "
            "is authoritative for campaign-design identity."
        ) if not manifest_scheme else None,
        "sample": samples,
    })


def reset_plan(
    cases: Path,
    case_ids: list[str],
    overrides: dict[str, Any],
    lab: Laboratory,
) -> dict[str, Any]:
    requested = [str(x).strip() for x in case_ids if str(x).strip()]
    if not requested:
        return {"error": "at least one exact case_id is required", "can_reset": False}
    if len(requested) > 50:
        return {"error": "reset is limited to 50 explicitly named cases", "can_reset": False}
    if len(requested) != len(set(requested)):
        return {"error": "duplicate case_id values requested", "can_reset": False}
    if not cases.is_file():
        return {"error": f"cases.csv not found: {cases}", "can_reset": False}

    rows = load_cases(cases)
    by_id = {str(row.get("case_id", "")).strip(): row for row in rows}
    unknown = [cid for cid in requested if cid not in by_id]
    if unknown:
        return {
            "error": "requested case IDs are absent from manifest",
            "unknown_case_ids": unknown,
            "can_reset": False,
        }

    try:
        policy = load_policy_for_generated(cases)
        accepted, rejected = validate_overrides(overrides, policy)
    except Exception as exc:
        return {"error": str(exc), "can_reset": False}

    blockers: list[dict[str, Any]] = []
    if rejected:
        blockers.append({
            "reason": "attempt_override_policy_violation",
            "rejected_overrides": rejected,
        })

    lock = probe_runner_lock(lab.campaign_root)
    if lock.get("active"):
        blockers.append({"reason": "laboratory_runner_active", "laboratory_runner": lock})

    selected = []
    total_delete_bytes = 0
    total_delete_files = 0
    for cid in requested:
        row = by_id[cid]
        cp = case_path(row, lab)
        if lab.runs_root.resolve() != cp and lab.runs_root.resolve() not in cp.parents:
            blockers.append({"case_id": cid, "reason": "case_path_outside_runs_root"})

        status, status_payload = _case_current_status(cp)
        pid = 0
        if isinstance(status_payload, dict):
            try:
                pid = int(status_payload.get("process_pid") or 0)
            except (TypeError, ValueError):
                pid = 0
        alive = process_alive(pid) if pid > 0 else False
        matches = process_matches_executable(pid, lab.computing_module) if alive else False
        if matches:
            blockers.append({
                "case_id": cid,
                "reason": "trusted_computing_module_still_alive",
                "pid": pid,
            })

        if not cp.is_dir():
            blockers.append({"case_id": cid, "reason": "case_directory_missing"})
            inv = {"file_count": 0, "total_bytes": 0}
        elif cp.is_symlink():
            blockers.append({"case_id": cid, "reason": "case_directory_is_symlink"})
            inv = {"file_count": 0, "total_bytes": 0}
        else:
            try:
                inv = inventory_case(cp)
            except Exception as exc:
                blockers.append({
                    "case_id": cid,
                    "reason": "inventory_failed",
                    "error": str(exc),
                })
                inv = {"file_count": 0, "total_bytes": 0}

        total_delete_bytes += int(inv.get("total_bytes") or 0)
        total_delete_files += int(inv.get("file_count") or 0)
        previous_override = load_override_record(cases, cid)
        groups = load_base_groups(cases, cid)
        try:
            apply_overrides(groups, accepted, policy)
        except Exception as exc:
            blockers.append({
                "case_id": cid,
                "reason": "attempt_override_type_or_value_error",
                "error": str(exc),
            })
        selected.append({
            "case_id": cid,
            "case_path": str(cp),
            "current_status": status,
            "case_fingerprint": row.get("case_fingerprint"),
            "logical_identity_fingerprint": identity_fingerprint(groups, policy),
            "recorded_process_pid": pid if pid > 0 else None,
            "recorded_process_alive": alive,
            "recorded_process_matches_trusted_computing_module": matches,
            "delete_file_count": int(inv.get("file_count") or 0),
            "delete_bytes": int(inv.get("total_bytes") or 0),
            "previous_attempt_overrides": (
                previous_override.get("overrides", {})
                if isinstance(previous_override, dict)
                else {}
            ),
        })

    return {
        "cases_csv": str(cases),
        "selected_count": len(selected),
        "selected_cases": selected,
        "other_cases_untouched": len(rows) - len(selected),
        "identity_policy": policy,
        "requested_attempt_overrides": accepted,
        "rejected_attempt_overrides": rejected,
        "cleanup": {
            "archive_mode": "metadata_only",
            "raw_execution_products_preserved": False,
            "files_to_remove": total_delete_files,
            "bytes_to_remove": total_delete_bytes,
            "attempt_history_root": str(cases.parent / "_attempt_history"),
        },
        "target_state": "generated",
        "laboratory_runner": lock,
        "blockers": blockers,
        "can_reset": len(blockers) == 0,
    }


def cmd_campaign_reset_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    try:
        overrides = parse_override_items(list(args.override))
    except Exception as exc:
        return emit({"error": f"invalid overrides: {exc}", "can_reset": False}, 2)
    payload = reset_plan(cases, list(args.case_id), overrides, lab)
    return emit(payload, 0 if payload.get("can_reset") else 3)


def cmd_campaign_reset_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    try:
        overrides = parse_override_items(list(args.override))
    except Exception as exc:
        return emit({"error": f"invalid overrides: {exc}"}, 2)
    plan = reset_plan(cases, list(args.case_id), overrides, lab)
    if not plan.get("can_reset"):
        return emit({"error": "reset preflight failed", "plan": plan}, 3)

    rows = load_cases(cases)
    by_id = {str(row.get("case_id", "")).strip(): row for row in rows}
    attempt_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    results = []
    for cid in list(args.case_id):
        row = by_id[cid]
        results.append(reset_case_to_generated(
            cases_csv=cases,
            case_id=cid,
            case_fingerprint=str(row.get("case_fingerprint", "")),
            case_path=case_path(row, lab),
            overrides=overrides,
            attempt_id=attempt_id,
            reason=str(args.reason or "selected_case_recalculation"),
        ))

    return emit({
        "attempt_id": attempt_id,
        "cases_csv": str(cases),
        "reset_count": len(results),
        "target_state": "generated",
        "results": results,
        "next_step": "prepare the same campaign; completed matching cases remain untouched",
    })


def cmd_campaign_append_preview(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    campaign = resolve_campaign_definition(args.campaign, lab)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    if not campaign.is_file():
        return emit({"error": f"campaign definition not found: {campaign}"}, 2)
    script = lab.research_root / "campaign_tools" / "campaign_append_0d.py"
    cmd = [
        sys.executable, str(script),
        "--cases", str(cases),
        "--campaign", str(campaign),
        "--laboratory", str(lab.config_path),
        "--preview",
    ]
    cp = subprocess.run(
        cmd, text=True, capture_output=True, cwd=str(lab.research_root),
        env=child_env(), check=False,
    )
    payload = {
        "command": cmd,
        "exit_code": cp.returncode,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
    }
    if cp.returncode == 0:
        try:
            payload["plan"] = json.loads(cp.stdout)
        except json.JSONDecodeError:
            pass
    return emit(payload, 0 if cp.returncode == 0 else 2)


def cmd_campaign_append_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    campaign = resolve_campaign_definition(args.campaign, lab)
    script = lab.research_root / "campaign_tools" / "campaign_append_0d.py"
    cmd = [
        sys.executable, str(script),
        "--cases", str(cases),
        "--campaign", str(campaign),
        "--laboratory", str(lab.config_path),
        "--append",
    ]
    cp = subprocess.run(
        cmd, text=True, capture_output=True, cwd=str(lab.research_root),
        env=child_env(), check=False,
    )
    payload = {
        "command": cmd,
        "exit_code": cp.returncode,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
    }
    if cp.returncode == 0:
        try:
            payload["result"] = json.loads(cp.stdout)
        except json.JSONDecodeError:
            pass
    return emit(payload, 0 if cp.returncode == 0 else 2)


def resolve_extension_definition(value: str, lab: Laboratory) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    direct = (Path.cwd() / candidate).resolve()
    if direct.is_file():
        return direct
    return (campaign_definitions_root(lab) / candidate).resolve()


def cmd_extension_preview(args: argparse.Namespace, lab: Laboratory) -> int:
    definition = resolve_extension_definition(args.extension, lab)
    if not definition.is_file():
        return emit({"error": f"extension definition not found: {definition}"}, 2)
    script = lab.research_root / "campaign_tools" / "campaign_extension_0d.py"
    cmd = [sys.executable, str(script), str(definition), "--laboratory", str(lab.config_path), "--preview"]
    cp = subprocess.run(cmd, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    return emit({"command": cmd, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}, 0 if cp.returncode == 0 else 2)


def cmd_extension_generate(args: argparse.Namespace, lab: Laboratory) -> int:
    definition = resolve_extension_definition(args.extension, lab)
    if not definition.is_file():
        return emit({"error": f"extension definition not found: {definition}"}, 2)
    script = lab.research_root / "campaign_tools" / "campaign_extension_0d.py"
    cmd = [sys.executable, str(script), str(definition), "--laboratory", str(lab.config_path), "--create"]
    if args.overwrite:
        cmd.append("--overwrite")
    cp = subprocess.run(cmd, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    return emit({"command": cmd, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}, 0 if cp.returncode == 0 else 2)


def cmd_composite_create(args: argparse.Namespace, lab: Laboratory) -> int:
    base = resolve(args.base_cases)
    extension = resolve(args.extension_cases)
    if not base.is_file():
        return emit({"error": f"base cases.csv not found: {base}"}, 2)
    if not extension.is_file():
        return emit({"error": f"extension cases.csv not found: {extension}"}, 2)
    script = lab.research_root / "campaign_tools" / "campaign_composite.py"
    cmd = [
        sys.executable, str(script),
        "--base-cases", str(base),
        "--extension-cases", str(extension),
        "--output-name", args.output_name,
        "--laboratory", str(lab.config_path),
    ]
    if args.overwrite:
        cmd.append("--overwrite")
    cp = subprocess.run(cmd, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    payload = {"command": cmd, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}
    if cp.returncode == 0:
        try:
            payload["composite"] = json.loads(cp.stdout)
        except json.JSONDecodeError:
            pass
    return emit(payload, 0 if cp.returncode == 0 else 2)



def _campaign_problem_kind(campaign: Path) -> str:
    data = tomllib.loads(campaign.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {}) if isinstance(data, dict) else {}
    if not isinstance(defaults, dict):
        raise ValueError(f"campaign defaults are invalid: {campaign}")
    if "geometry_config" in defaults or "ignition_config" in defaults:
        return "1d"
    if "reactor_config" in defaults:
        return "0d"
    raise ValueError(
        "cannot determine campaign problem kind from defaults; expected reactor_config "
        "for 0D or geometry_config/ignition_config for 1D"
    )


def campaign_generator_for_definition(campaign: Path, lab: Laboratory) -> Path:
    kind = _campaign_problem_kind(campaign)
    name = "campaign_generator_1d_flame.py" if kind == "1d" else "campaign_generator_0d.py"
    generator = lab.research_root / "campaign_tools" / name
    if not generator.is_file():
        raise FileNotFoundError(f"campaign generator not found for {kind}: {generator}")
    return generator


def package_interface_for_generated(cases: Path, lab: Laboratory) -> Path:
    campaign = cases.parent / "campaign.toml"
    if not campaign.is_file():
        raise FileNotFoundError(f"generated campaign definition not found: {campaign}")
    kind = _campaign_problem_kind(campaign)
    key = "1d" if kind == "1d" else "0d"
    try:
        return lab.package_interfaces[key]
    except KeyError as exc:
        raise ValueError(
            f"laboratory runtime has no package_interface_{key}; add it to the local runtime configuration"
        ) from exc

def cmd_campaign_preview(args: argparse.Namespace, lab: Laboratory) -> int:
    campaign = resolve_campaign_definition(args.campaign, lab)
    if not campaign.is_file():
        return emit({"error": f"campaign definition not found: {campaign}", "definitions_root": str(campaign_definitions_root(lab))}, 2)
    generator = campaign_generator_for_definition(campaign, lab)
    cmd = [sys.executable, str(generator), str(campaign), "--laboratory", str(lab.config_path), "--preview", "--limit", str(args.limit)]
    cp = subprocess.run(cmd, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    payload: dict[str, Any] = {"command": cmd, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}
    for line in cp.stdout.splitlines():
        if line.startswith("Final cases:"):
            try:
                payload["final_cases"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return emit(payload, 0 if cp.returncode == 0 else 2)


def cmd_campaign_generate(args: argparse.Namespace, lab: Laboratory) -> int:
    campaign = resolve_campaign_definition(args.campaign, lab)
    if not campaign.is_file():
        return emit({"error": f"campaign definition not found: {campaign}", "definitions_root": str(campaign_definitions_root(lab))}, 2)
    generator = campaign_generator_for_definition(campaign, lab)
    mode = "--run-interface" if args.run_interface else "--create"
    cmd = [sys.executable, str(generator), str(campaign), "--laboratory", str(lab.config_path), mode]
    if args.overwrite:
        cmd.append("--overwrite")
    cp = subprocess.run(cmd, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    return emit({"command": cmd, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}, 0 if cp.returncode == 0 else 2)



def cmd_campaign_prepare(args: argparse.Namespace, lab: Laboratory) -> int:
    """Invoke the trusted package interface only for missing case directories.

    Existing cases are skipped only when their setup_input.nml fingerprint matches
    cases.csv.  A conflicting existing directory is never overwritten.
    """

    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    rows = load_cases(cases)
    setups_dir = cases.parent / "_setups"
    if not setups_dir.is_dir():
        return emit({"error": f"setup directory not found: {setups_dir}; generate campaign files first"}, 2)

    try:
        interface = package_interface_for_generated(cases, lab)
    except Exception as exc:
        return emit(
            {
                "error": f"preparation runtime unavailable: {type(exc).__name__}: {exc}",
                "runtime_ready": False,
            },
            2,
        )

    runtime_error = _preparation_runtime_error(lab, interface)
    if runtime_error is not None:
        return emit(runtime_error, 2)

    prep_dir = cases.parent / "_preparation"
    prep_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    generated = 0
    skipped = 0
    failed = 0

    for row in rows:
        cid = str(row.get("case_id", ""))
        expected_fp = str(row.get("case_fingerprint", "")).strip().lower()
        cp = case_path(row, lab)
        try:
            setup, attempt_metadata = materialize_effective_setup(cases, cid)
            setup = setup.resolve()
        except Exception as exc:
            record = {
                "case_id": cid,
                "case_path": str(cp),
                "case_fingerprint": expected_fp,
                "status": "attempt_configuration_error",
                "error": str(exc),
            }
            records.append(record)
            failed += 1
            break
        record: dict[str, Any] = {
            "case_id": cid,
            "case_path": str(cp),
            "setup_file": str(setup),
            "case_fingerprint": expected_fp,
            "case_identity_fingerprint": attempt_metadata.get("case_identity_fingerprint"),
            "attempt_fingerprint": attempt_metadata.get("attempt_fingerprint"),
            "attempt_overrides": attempt_metadata.get("overrides", {}),
        }

        if cp.exists():
            local_setup = cp / "setup_input.nml"
            observed_fp = None
            if local_setup.is_file():
                try:
                    observed_fp = string_value(local_setup.read_text(encoding="utf-8", errors="replace"), "case_fingerprint")
                except OSError:
                    pass
            if observed_fp and observed_fp.strip().lower() == expected_fp:
                existing_attempt = inspect_json_file(cp / "attempt_config.json")
                record.update({
                    "status": "skipped_existing_matching",
                    "observed_fingerprint": observed_fp,
                    "attempt_config_present": bool(existing_attempt.get("valid_json")),
                })
                records.append(record)
                skipped += 1
                continue
            record.update({
                "status": "conflict_existing_case",
                "observed_fingerprint": observed_fp,
                "error": "existing case directory does not contain the expected fingerprint; not overwritten",
            })
            records.append(record)
            failed += 1
            break

        if not setup.is_file():
            record.update({"status": "missing_setup", "error": f"setup file not found: {setup}"})
            records.append(record)
            failed += 1
            break

        stdout_path = prep_dir / f"{cid}.stdout.log"
        stderr_path = prep_dir / f"{cid}.stderr.log"
        started = utc_now()
        cp_run = subprocess.run(
            [str(interface), str(setup)],
            cwd=str(lab.package_interface_workdir),
            text=True,
            capture_output=True,
            check=False,
            env=child_env(),
        )
        stdout_path.write_text(cp_run.stdout, encoding="utf-8")
        stderr_path.write_text(cp_run.stderr, encoding="utf-8")
        if cp_run.returncode == 0 and cp.is_dir():
            status = "generated"
            generated += 1
            write_case_attempt_config(cp, attempt_metadata)
        else:
            status = "failed"
            failed += 1
        record.update({
            "status": status,
            "exit_code": cp_run.returncode,
            "started_at_utc": started,
            "finished_at_utc": utc_now(),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        })
        records.append(record)
        if status == "failed":
            break

    post_status = campaign_status_payload(cases, lab)
    summary_path = cases.parent / "preparation_summary.json"

    # Preserve the complete per-case audit trail on disk, but do not emit all
    # records into the Pi tool result.  Large campaigns would otherwise place
    # hundreds of repetitive path/timestamp records into the LLM context.
    audit_summary = {
        "cases_csv": str(cases),
        "total_cases": len(rows),
        "generated_now": generated,
        "skipped_existing_matching": skipped,
        "failed": failed,
        "records": records,
        "post_status": post_status,
    }
    write_json(summary_path, audit_summary)

    record_counts = Counter(str(r.get("status", "unknown")) for r in records)
    problem_records = [
        {
            k: r.get(k)
            for k in (
                "case_id",
                "status",
                "case_path",
                "observed_fingerprint",
                "error",
                "exit_code",
                "stdout_log",
                "stderr_log",
            )
            if r.get(k) is not None
        }
        for r in records
        if str(r.get("status", "")) not in {"generated", "skipped_existing_matching"}
    ][:10]

    compact_post_status = {
        k: post_status[k]
        for k in (
            "cases_csv",
            "total_cases",
            "counts",
            "runnable_cases",
            "missing_cases",
            "running_cases",
            "completed_like_cases",
        )
        if k in post_status
    }

    tool_summary = {
        "cases_csv": str(cases),
        "total_cases": len(rows),
        "generated_now": generated,
        "skipped_existing_matching": skipped,
        "failed": failed,
        "record_counts": dict(sorted(record_counts.items())),
        "problem_cases": problem_records,
        "preparation_summary": str(summary_path),
        "post_status": compact_post_status,
    }
    return emit(tool_summary, 0 if failed == 0 else 3)

def _case_current_status(case_dir: Path) -> tuple[str, dict[str, Any] | None]:
    if not case_dir.is_dir():
        return "missing_case", None
    info = inspect_json_file(case_dir / "run_status.json")
    if not info.get("exists"):
        return "not_started", None
    if not info.get("valid_json"):
        return "invalid_status", None
    payload = info.get("data")
    assert isinstance(payload, dict)
    return str(payload.get("status", "invalid_status")).lower(), payload


def selective_rerun_plan(
    cases: Path,
    case_ids: list[str],
    lab: Laboratory,
    config: Path,
) -> dict[str, Any]:
    requested = [str(x).strip() for x in case_ids if str(x).strip()]
    if not requested:
        return {"error": "at least one exact case_id is required", "can_start": False}
    if len(requested) > 50:
        return {
            "error": "selective rerun is limited to at most 50 explicitly named cases per job",
            "can_start": False,
            "requested_count": len(requested),
        }
    if len(requested) != len(set(requested)):
        return {"error": "duplicate case_id values were requested", "can_start": False}

    rows = load_cases(cases)
    by_id: dict[str, dict[str, str]] = {}
    duplicate_manifest_ids: list[str] = []
    for row in rows:
        cid = str(row.get("case_id", "")).strip()
        if cid in by_id:
            duplicate_manifest_ids.append(cid)
        by_id[cid] = row
    if duplicate_manifest_ids:
        return {
            "error": "campaign manifest contains duplicate case_id values",
            "duplicate_case_ids": sorted(set(duplicate_manifest_ids)),
            "can_start": False,
        }

    unknown = [cid for cid in requested if cid not in by_id]
    if unknown:
        return {
            "error": "requested case_id values were not found in the campaign manifest",
            "unknown_case_ids": unknown,
            "can_start": False,
        }

    if not config.is_file():
        return {
            "error": f"trusted runner configuration not found: {config}",
            "can_start": False,
        }
    cfg = read_json(config)
    skip_statuses = {str(x).lower() for x in cfg.get("skip_statuses", [])}
    runner_lock = probe_runner_lock(lab.campaign_root)

    selected: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    bypass_count = 0
    for cid in requested:
        row = by_id[cid]
        cp = case_path(row, lab)
        status, status_payload = _case_current_status(cp)
        expected_fp = str(row.get("case_fingerprint", "")).strip()
        setup_path = cp / "setup_input.nml"
        observed_fp = ""
        if setup_path.is_file():
            try:
                observed_fp = string_value(
                    setup_path.read_text(encoding="utf-8", errors="replace"),
                    "case_fingerprint",
                ) or ""
            except OSError:
                observed_fp = ""
        fingerprint_matches = bool(
            expected_fp and observed_fp and expected_fp.lower() == observed_fp.lower()
        )

        pid = 0
        if isinstance(status_payload, dict):
            try:
                pid = int(status_payload.get("process_pid") or 0)
            except (TypeError, ValueError):
                pid = 0
        pid_alive = process_alive(pid) if pid > 0 else False
        pid_matches = (
            process_matches_executable(pid, lab.computing_module)
            if pid > 0 and pid_alive
            else False
        )

        item = {
            "case_id": cid,
            "case_path": str(cp),
            "current_status": status,
            "policy_would_skip": status in skip_statuses,
            "case_directory_exists": cp.is_dir(),
            "task_setup_exists": (cp / "task_setup").is_dir(),
            "fingerprint_matches": fingerprint_matches,
            "expected_fingerprint": expected_fp or None,
            "observed_fingerprint": observed_fp or None,
            "recorded_process_pid": pid if pid > 0 else None,
            "recorded_process_alive": pid_alive,
            "recorded_process_matches_trusted_computing_module": pid_matches,
            "reactor_history": {
                "path": str(cp / "reactor_history.dat"),
                "exists": (cp / "reactor_history.dat").is_file(),
                "size_bytes": (
                    (cp / "reactor_history.dat").stat().st_size
                    if (cp / "reactor_history.dat").is_file()
                    else None
                ),
            },
        }
        selected.append(item)

        if item["policy_would_skip"]:
            bypass_count += 1
        if not cp.is_dir():
            blockers.append({"case_id": cid, "reason": "missing_case_directory"})
        elif not (cp / "task_setup").is_dir():
            blockers.append({"case_id": cid, "reason": "missing_task_setup"})
        elif not fingerprint_matches:
            blockers.append({"case_id": cid, "reason": "fingerprint_mismatch_or_missing"})
        elif pid_matches:
            blockers.append({"case_id": cid, "reason": "trusted_computing_module_still_alive", "pid": pid})

    if runner_lock.get("active"):
        blockers.append({
            "reason": "laboratory_runner_active",
            "laboratory_runner": runner_lock,
        })

    return {
        "cases_csv": str(cases),
        "selected_count": len(selected),
        "selected_cases": selected,
        "policy_bypass_count": bypass_count,
        "other_cases_untouched": len(rows) - len(selected),
        "archive_previous_artifacts": True,
        "archive_scope": (
            "per selected case: run metadata and compact top-level histories/logs are copied "
            "under _rerun_archive/<job_id>/ before the new run_status.json is written"
        ),
        "execution_policy": {
            "run_config": str(config),
            "run_config_sha256": sha256_file(config),
            "threads": int(cfg.get("threads", lab.default_threads)),
            "max_concurrent_cases": int(cfg.get("max_concurrent_cases", 1)),
            "limit_library_threads": bool(cfg.get("limit_library_threads", True)),
            "skip_statuses": sorted(skip_statuses),
            "rerun_failed": bool(cfg.get("rerun_failed", True)),
            "max_runtime_seconds": cfg.get("max_runtime_seconds"),
        },
        "laboratory_runner": runner_lock,
        "blockers": blockers,
        "can_start": len(blockers) == 0,
    }


def cmd_campaign_rerun_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    config = lab.runner_config
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}", "can_start": False}, 2)
    plan = selective_rerun_plan(cases, list(args.case_id), lab, config)
    return emit(plan, 0 if plan.get("can_start") else 3)


def cmd_campaign_rerun_start(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    config = lab.runner_config
    if not cases.is_file() or not config.is_file():
        return emit({
            "error": "cases.csv or trusted runner configuration not found",
            "cases": str(cases),
            "run_config": str(config),
        }, 2)

    runtime_error = _execution_runtime_error(lab)
    if runtime_error is not None:
        return emit(runtime_error, 2)

    plan = selective_rerun_plan(cases, list(args.case_id), lab, config)
    if not plan.get("can_start"):
        return emit({"error": "selective rerun preflight failed", "plan": plan}, 3)

    jobs_root(lab).mkdir(parents=True, exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    jp = job_path(lab, job_id)
    stdout_path = jobs_root(lab) / f"{job_id}.stdout.log"
    stderr_path = jobs_root(lab) / f"{job_id}.stderr.log"
    control_path = job_control_path(lab, job_id)
    runner = lab.research_root / "campaign_tools" / "campaign_runner.py"
    if not runner.is_file():
        return emit({"error": f"campaign runner not found: {runner}"}, 2)

    selected_ids = [str(x).strip() for x in args.case_id if str(x).strip()]
    record = {
        "job_id": job_id,
        "job_type": "selective_rerun",
        "state": "launching",
        "created_at_utc": utc_now(),
        "manifest": str(cases),
        "run_config": str(config),
        "laboratory_config": str(lab.config_path),
        "runner": str(runner),
        "selected_case_ids": selected_ids,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "control_file": str(control_path),
        "prelaunch_plan": plan,
    }
    write_json(jp, record)

    command = [
        sys.executable,
        str(runner),
        str(cases),
        str(config),
        "--laboratory",
        str(lab.config_path),
        "--job-file",
        str(jp),
        "--control-file",
        str(control_path),
        "--rerun-job-id",
        job_id,
    ]
    for cid in selected_ids:
        command += ["--selective-rerun-case-id", cid]

    with stdout_path.open("ab", buffering=0) as out, stderr_path.open("ab", buffering=0) as err:
        proc = subprocess.Popen(
            command,
            cwd=str(lab.research_root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            close_fds=True,
            env=child_env(),
        )
    record.update({
        "state": "running",
        "runner_pid": proc.pid,
        "command": command,
        "launched_at_utc": utc_now(),
    })
    write_json(jp, record)
    return emit({"job": record, "plan": plan})


def physical_profile_payload(lab: Laboratory, profile_name: str) -> tuple[Any, dict[str, Any]]:
    registry = lab.termination_profiles
    if not registry.is_file():
        raise FileNotFoundError(f"trusted termination profile registry not found: {registry}")
    profile = load_quasistationary_profile(registry, profile_name)
    return profile, {
        "name": profile.name,
        "description": profile.description,
        "registry": str(registry),
        "registry_sha256": profile_file_sha256(registry),
        "required_run_control_modes": list(profile.required_run_control_modes),
        "window_duration_s": profile.window_duration_s,
        "min_window_points": profile.min_window_points,
        "minimum_temperature_rise_K": profile.min_temperature_rise_K,
        "fuel_species": profile.fuel_species,
        "minimum_fuel_consumed_fraction": profile.min_fuel_consumed_fraction,
        "relative_temperature_span": profile.relative_temperature_span,
        "relative_pressure_span": profile.relative_pressure_span,
        "relative_density_span": profile.relative_density_span,
        "max_species_mass_fraction_span": profile.max_species_mass_fraction_span,
        "max_sumY_error": profile.max_sumY_error,
    }


def summarize_current_execution_provenance(campaign: Campaign) -> dict[str, Any]:
    """Summarize current execution provenance independently of physical classification."""
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    condition_counts: Counter[str] = Counter()
    physical_status_counts: Counter[str] = Counter()
    termination_profile_counts: Counter[str] = Counter()
    physical_condition_met_counts: Counter[str] = Counter()
    attempt_ids: set[str] = set()
    runner_job_ids: set[str] = set()
    cases_with_attempt_id = 0
    cases_with_runner_job_id = 0
    invalid_or_missing: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []

    for case in campaign:
        info = inspect_json_file(case.case_path / "run_status.json")
        if not info.get("valid_json"):
            status_counts["missing_or_invalid_run_status"] += 1
            if len(invalid_or_missing) < 20:
                invalid_or_missing.append({
                    "case_id": case.case_id,
                    "run_status_file": {
                        k: info.get(k)
                        for k in (
                            "path", "exists", "size_bytes", "empty",
                            "whitespace_only", "parse_error"
                        )
                    },
                })
            continue

        st = info.get("data") or {}
        status = str(st.get("status") or "not_reported")
        reason = str(st.get("nrg_termination_reason") or "not_reported")
        condition = str(st.get("termination_condition") or "not_reported")
        physical_status = str(st.get("physical_condition_status") or "not_reported")
        termination_profile = str(st.get("termination_profile") or "not_reported")
        physical_condition_met_raw = st.get("physical_condition_met")
        if physical_condition_met_raw is True:
            physical_condition_met = "true"
        elif physical_condition_met_raw is False:
            physical_condition_met = "false"
        else:
            physical_condition_met = "not_reported"
        status_counts[status] += 1
        reason_counts[reason] += 1
        condition_counts[condition] += 1
        physical_status_counts[physical_status] += 1
        termination_profile_counts[termination_profile] += 1
        physical_condition_met_counts[physical_condition_met] += 1

        attempt_id = str(st.get("attempt_id") or "").strip()
        if attempt_id:
            cases_with_attempt_id += 1
            attempt_ids.add(attempt_id)

        runner_job_id = str(
            st.get("runner_job_id") or st.get("selective_rerun_job_id") or ""
        ).strip()
        if runner_job_id:
            cases_with_runner_job_id += 1
            runner_job_ids.add(runner_job_id)

        if len(examples) < 12 and (attempt_id or reason == "external_stop_request"):
            examples.append({
                "case_id": case.case_id,
                "status": status,
                "nrg_termination_reason": reason,
                "termination_condition": condition,
                "attempt_id": attempt_id or None,
                "attempt_fingerprint": st.get("attempt_fingerprint"),
                "runner_job_id": runner_job_id or None,
                "runner_job_id_source": (
                    "runner_job_id" if st.get("runner_job_id")
                    else "selective_rerun_job_id_legacy_fallback"
                    if st.get("selective_rerun_job_id")
                    else None
                ),
                "physical_condition_met": st.get("physical_condition_met"),
                "physical_condition_status": st.get("physical_condition_status"),
                "termination_profile": st.get("termination_profile"),
            })

    return {
        "source": "current per-case run_status.json",
        "affects_quasistationarity_classification": False,
        "status_counts": dict(sorted(status_counts.items())),
        "nrg_termination_reason_counts": dict(sorted(reason_counts.items())),
        "termination_condition_counts": dict(sorted(condition_counts.items())),
        "physical_condition_status_counts": dict(sorted(physical_status_counts.items())),
        "termination_profile_counts": dict(sorted(termination_profile_counts.items())),
        "physical_condition_met_counts": dict(sorted(physical_condition_met_counts.items())),
        "cases_with_attempt_id": cases_with_attempt_id,
        "unique_attempt_ids": sorted(attempt_ids),
        "cases_with_runner_job_id": cases_with_runner_job_id,
        "unique_runner_job_ids": sorted(runner_job_ids),
        "identifier_semantics": {
            "attempt_id": (
                "recalculation/configuration lineage created by reset/attempt preparation; "
                "one attempt_id may legitimately be executed by multiple runner jobs"
            ),
            "runner_job_id": (
                "background runner invocation that produced the current run_status; "
                "legacy v0.5 selective reruns are recovered from selective_rerun_job_id"
            ),
        },
        "external_stop_semantics": (
            "The external laboratory physical-condition controller writes run_control.stop; "
            "NRG detects that request and terminates with nrg_termination_reason="
            "external_stop_request. NRG does not originate the stop request."
        ),
        "examples_limited_to": 12,
        "examples": examples,
        "missing_or_invalid_run_status_limited_to": 20,
        "missing_or_invalid_run_status": invalid_or_missing,
    }


def cmd_campaign_execution_summary(args: argparse.Namespace, lab: Laboratory) -> int:
    # Fast execution-only campaign summary; does not read reactor histories.
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    try:
        campaign = Campaign.load(cases, lab.runs_root)
        execution = summarize_current_execution_provenance(campaign)
    except Exception as exc:
        return emit({"error": str(exc)}, 2)

    return emit({
        "cases_csv": str(cases),
        "case_count": len(campaign),
        "summary_semantics": {
            "basis": "current per-case run_status.json only",
            "reactor_histories_read": False,
            "establishes_offline_quasistationarity": False,
            "online_physical_condition_note": (
                "physical_condition_met/status and termination_profile are runtime metadata "
                "recorded by the trusted physical-condition execution path; they are not an "
                "independent re-evaluation of reactor_history.dat"
            ),
        },
        **execution,
    })


def cmd_campaign_physical_audit(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    try:
        profile, profile_info = physical_profile_payload(lab, args.profile)
    except Exception as exc:
        return emit({"error": str(exc)}, 2)

    campaign = Campaign.load(cases, lab.runs_root)
    counts: Counter[str] = Counter()
    needs: list[str] = []
    anomalies: list[dict[str, Any]] = []
    reached_examples: list[dict[str, Any]] = []

    for case in campaign:
        try:
            result = evaluate_quasistationary_case(case.case_path, profile)
            payload = result.to_dict()
            counts[result.status] += 1
            if not result.reached:
                needs.append(case.case_id)
                if len(anomalies) < 30:
                    anomalies.append({
                        "case_id": case.case_id,
                        "case_path": str(case.case_path),
                        "status": result.status,
                        "history_end_time_s": result.history_end_time_s,
                        "temperature_rise_K": result.temperature_rise_K,
                        "fuel_consumed_fraction": result.fuel_consumed_fraction,
                        "relative_temperature_span": result.relative_temperature_span,
                        "relative_pressure_span": result.relative_pressure_span,
                        "relative_density_span": result.relative_density_span,
                        "max_species_mass_fraction_span": result.max_species_mass_fraction_span,
                        "species_with_max_span": result.species_with_max_span,
                        "reason": result.reason,
                    })
            elif len(reached_examples) < 5:
                reached_examples.append({
                    "case_id": case.case_id,
                    "history_end_time_s": result.history_end_time_s,
                    "product_temperature_K": result.product_temperature_K,
                    "product_pressure_Pa": result.product_pressure_Pa,
                    "product_density_kg_m3": result.product_density_kg_m3,
                })
        except Exception as exc:
            counts["history_error"] += 1
            needs.append(case.case_id)
            if len(anomalies) < 30:
                anomalies.append({
                    "case_id": case.case_id,
                    "case_path": str(case.case_path),
                    "status": "history_error",
                    "error": str(exc),
                })

    execution_provenance = summarize_current_execution_provenance(campaign)

    return emit({
        "cases_csv": str(cases),
        "case_count": len(campaign),
        "profile": profile_info,
        "audit_semantics": {
            "quasistationarity_classification_basis": "reactor_history.dat only",
            "execution_provenance_basis": "current run_status.json, reported separately",
            "execution_provenance_affects_classification": False,
            "do_not_infer": [
                "Do not infer termination reason from quasistationarity status.",
                "Do not infer runner job identity from attempt_id.",
                "Do not infer that wall_time mode means the wall-time limit was reached.",
            ],
        },
        "counts": dict(sorted(counts.items())),
        "quasistationary_count": counts.get("quasistationary", 0),
        "needs_recalculation_count": len(needs),
        "needs_recalculation_case_ids": needs,
        "anomaly_details_limited_to": 30,
        "anomalies": anomalies,
        "reached_examples_limited_to": 5,
        "reached_examples": reached_examples,
        "execution_provenance": execution_provenance,
    })


def physical_launch_plan(
    cases: Path,
    selected_ids: list[str] | None,
    profile_name: str,
    lab: Laboratory,
) -> dict[str, Any]:
    try:
        profile, profile_info = physical_profile_payload(lab, profile_name)
    except Exception as exc:
        return {"error": str(exc), "can_start": False}

    config = lab.runner_config
    if not config.is_file():
        return {"error": f"trusted runner configuration not found: {config}", "can_start": False}

    runtime_error = _execution_runtime_error(lab)

    rows = load_cases(cases)
    by_id = {str(row.get("case_id", "")).strip(): row for row in rows}
    if selected_ids is None:
        status = campaign_status_payload(
            cases, lab, config, include_runnable_case_ids=True
        )
        ids = [
            str(x).strip()
            for x in status.get("runnable_case_ids", [])
            if str(x).strip()
        ]
        expected_count = int(status.get("runnable_cases", 0) or 0)
        if len(ids) != expected_count:
            return {
                "error": (
                    "campaign runnable-case enumeration is inconsistent with "
                    "the trusted status count"
                ),
                "runnable_count": expected_count,
                "enumerated_case_ids": len(ids),
                "can_start": False,
            }
    else:
        ids = [str(x).strip() for x in selected_ids if str(x).strip()]
        if not ids:
            return {"error": "at least one exact case_id is required", "can_start": False}
        if len(ids) != len(set(ids)):
            return {"error": "duplicate case_id values were requested", "can_start": False}
        unknown = [cid for cid in ids if cid not in by_id]
        if unknown:
            return {
                "error": "requested case_id values were not found in the campaign manifest",
                "unknown_case_ids": unknown,
                "can_start": False,
            }

    incompatible = []
    for cid in ids:
        row = by_id[cid]
        try:
            mode = str(
                effective_field_value(
                    cases, cid, "run_control_config.termination_mode"
                )
            ).strip().lower()
        except Exception:
            mode = str(row.get("run_control_config.termination_mode", "")).strip().lower()
        if mode not in set(profile.required_run_control_modes):
            incompatible.append({
                "case_id": cid,
                "run_control_mode": mode or None,
                "required_modes": list(profile.required_run_control_modes),
            })

    lock = probe_runner_lock(lab.campaign_root)
    blockers: list[dict[str, Any]] = []
    if runtime_error is not None:
        blockers.append({
            "reason": "execution_runtime_unavailable",
            "error": runtime_error["error"],
        })
    if lock.get("active"):
        blockers.append({"reason": "laboratory_runner_active", "laboratory_runner": lock})
    if incompatible:
        blockers.append({
            "reason": "incompatible_run_control_mode",
            "cases": incompatible[:30],
            "count": len(incompatible),
            "message": (
                "This physical profile must be the successful termination authority. "
                "Generate/prepare a campaign using a compatible run-control mode "
                "(for v1: wall_time) rather than a finite simulation-time ceiling."
            ),
        })

    cfg = read_json(config)
    selection_mode = "all_runnable" if selected_ids is None else "explicit_case_ids"
    selected_ids_limit = 50
    selected_ids_for_report = (
        ids if selection_mode == "explicit_case_ids" or len(ids) <= selected_ids_limit
        else ids[:selected_ids_limit]
    )
    return {
        "cases_csv": str(cases),
        "selection_mode": selection_mode,
        "selected_count": len(ids),
        "selected_case_ids": selected_ids_for_report,
        "selected_case_ids_truncated": len(selected_ids_for_report) < len(ids),
        "selected_case_ids_limited_to": selected_ids_limit,
        "other_cases_not_scheduled": len(rows) - len(ids),
        "profile": profile_info,
        "execution_policy": {
            "run_config": str(config),
            "run_config_sha256": sha256_file(config),
            "threads": int(cfg.get("threads", lab.default_threads)),
            "max_concurrent_cases": int(cfg.get("max_concurrent_cases", 1)),
            "max_runtime_seconds": cfg.get("max_runtime_seconds"),
            "skip_statuses": cfg.get("skip_statuses", []),
        },
        "laboratory_runner": lock,
        "blockers": blockers,
        "can_start": len(blockers) == 0,
    }


def launch_physical_job(
    cases: Path,
    selected_ids: list[str] | None,
    profile_name: str,
    lab: Laboratory,
) -> dict[str, Any]:
    plan = physical_launch_plan(cases, selected_ids, profile_name, lab)
    if not plan.get("can_start"):
        return {"error": "physical-run preflight failed", "plan": plan}

    jobs_root(lab).mkdir(parents=True, exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    jp = job_path(lab, job_id)
    stdout_path = jobs_root(lab) / f"{job_id}.stdout.log"
    stderr_path = jobs_root(lab) / f"{job_id}.stderr.log"
    control_path = job_control_path(lab, job_id)
    runner = lab.research_root / "campaign_tools" / "campaign_runner.py"
    if not runner.is_file():
        return {"error": f"campaign runner not found: {runner}"}

    record = {
        "job_id": job_id,
        "job_type": "physical_condition_run",
        "state": "launching",
        "created_at_utc": utc_now(),
        "manifest": str(cases),
        "run_config": str(lab.runner_config),
        "laboratory_config": str(lab.config_path),
        "runner": str(runner),
        "selected_case_ids": selected_ids,
        "termination_profile": profile_name,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "control_file": str(control_path),
        "prelaunch_plan": plan,
    }
    write_json(jp, record)

    command = [
        sys.executable,
        str(runner),
        str(cases),
        str(lab.runner_config),
        "--laboratory",
        str(lab.config_path),
        "--job-file",
        str(jp),
        "--control-file",
        str(control_path),
        "--termination-profile",
        profile_name,
        "--rerun-job-id",
        job_id,
    ]
    if selected_ids is not None:
        for cid in selected_ids:
            command += ["--selective-rerun-case-id", cid]

    with stdout_path.open("ab", buffering=0) as out, stderr_path.open("ab", buffering=0) as err:
        proc = subprocess.Popen(
            command,
            cwd=str(lab.research_root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            close_fds=True,
            env=child_env(),
        )
    record.update({
        "state": "running",
        "runner_pid": proc.pid,
        "command": command,
        "launched_at_utc": utc_now(),
    })
    write_json(jp, record)
    return {"job": record, "plan": plan}


def cmd_campaign_physical_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}", "can_start": False}, 2)
    plan = physical_launch_plan(cases, None, args.profile, lab)
    return emit(plan, 0 if plan.get("can_start") else 3)


def cmd_campaign_physical_start(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    result = launch_physical_job(cases, None, args.profile, lab)
    return emit(result, 0 if not result.get("error") else 3)


def cmd_campaign_physical_cases_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}", "can_start": False}, 2)
    plan = physical_launch_plan(cases, list(args.case_id), args.profile, lab)
    return emit(plan, 0 if plan.get("can_start") else 3)


def cmd_campaign_physical_cases_start(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    result = launch_physical_job(cases, list(args.case_id), args.profile, lab)
    return emit(result, 0 if not result.get("error") else 3)


def cmd_campaign_start(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    # Execution policy is trusted infrastructure and is not authored ad hoc by
    # the LLM. `--run-config` is a private/manual override only.
    config = resolve(args.run_config) if args.run_config else lab.runner_config
    if not cases.is_file() or not config.is_file():
        return emit({"error": "cases.csv or trusted runner configuration not found", "cases": str(cases), "run_config": str(config)}, 2)
    runtime_error = _execution_runtime_error(lab)
    if runtime_error is not None:
        return emit(runtime_error, 2)
    status = campaign_status_payload(cases, lab, config)
    if status["missing_cases"]:
        return emit({"error": "campaign contains missing case directories; generate setups before running", "status": status}, 3)
    if status["laboratory_runner"].get("active"):
        return emit({
            "error": "another campaign runner already owns the laboratory execution lock",
            "status": status,
            "laboratory_runner": status["laboratory_runner"],
        }, 4)
    # Stale per-case `running` records are intentionally allowed here. Runner v5
    # acquires the global lock and converts them to `interrupted` before executing.

    jobs_root(lab).mkdir(parents=True, exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
    jp = job_path(lab, job_id)
    stdout_path = jobs_root(lab) / f"{job_id}.stdout.log"
    stderr_path = jobs_root(lab) / f"{job_id}.stderr.log"
    control_path = job_control_path(lab, job_id)
    runner = lab.research_root / "campaign_tools" / "campaign_runner.py"
    if not runner.is_file():
        return emit({"error": f"campaign runner not found: {runner}"}, 2)

    record = {
        "job_id": job_id,
        "state": "launching",
        "created_at_utc": utc_now(),
        "manifest": str(cases),
        "run_config": str(config),
        "laboratory_config": str(lab.config_path),
        "runner": str(runner),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "control_file": str(control_path),
        "prelaunch_status": status,
    }
    write_json(jp, record)

    command = [
        sys.executable, str(runner), str(cases), str(config),
        "--laboratory", str(lab.config_path),
        "--job-file", str(jp),
        "--control-file", str(control_path),
    ]
    with stdout_path.open("ab", buffering=0) as out, stderr_path.open("ab", buffering=0) as err:
        proc = subprocess.Popen(
            command,
            cwd=str(lab.research_root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            close_fds=True,
            env=child_env(),
        )
    record.update({"state": "running", "runner_pid": proc.pid, "command": command, "launched_at_utc": utc_now()})
    write_json(jp, record)
    payload = {"job": record, "status": status}
    return emit(payload if getattr(args, "full", False) else compact_campaign_start(payload))


def cmd_campaign_job_status(args: argparse.Namespace, lab: Laboratory) -> int:
    jp = resolve(args.job) if ("/" in args.job or args.job.endswith(".json")) else job_path(lab, args.job)
    if not jp.is_file():
        return emit({"error": f"job not found: {jp}"}, 2)
    record = read_json(jp)
    pid = int(record.get("runner_pid") or 0)
    if record.get("state") == "running" and not process_alive(pid):
        record["state_observed"] = "not_alive"

    # This is the authoritative *current* laboratory lock observation. The
    # `job.runner_lock` field in older job files may only be an acquisition-time
    # snapshot and can legitimately say active=true after the runner has exited.
    live_runner = probe_runner_lock(lab.campaign_root)

    manifest = Path(record.get("manifest", ""))
    config = Path(record.get("run_config", ""))
    status = campaign_status_payload(manifest, lab, config if config.is_file() else None) if manifest.is_file() else None
    control_file = Path(str(record.get("control_file") or job_control_path(lab, str(record.get("job_id") or jp.stem)))).expanduser().resolve()
    operator_control = read_operator_control(control_file)
    active_case = active_case_for_job(record, lab)
    payload = {
        "job": record,
        "operator_control": operator_control,
        "laboratory_runner": live_runner,
        "runner_lock_semantics": {
            "authoritative_current_field": "laboratory_runner",
            "job_runner_lock_is_persisted_snapshot": True,
        },
        "campaign_status": status,
        "active_case": active_case,
    }
    return emit(
        payload
        if getattr(args, "full", False)
        else compact_job_status(payload, job_file=str(jp))
    )


def cmd_campaign_stop_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    plan = operator_stop_plan(lab, action="stop_campaign", job_ref=args.job)
    payload = plan if getattr(args, "full", False) else compact_operator_plan(plan)
    return emit(payload, 0 if plan.get("can_stop") else 3)


def cmd_campaign_stop_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    plan = operator_stop_plan(lab, action="stop_campaign", job_ref=args.job)
    result = issue_operator_stop(lab, plan, reason=args.reason, wait_seconds=args.wait_seconds)
    payload = result if getattr(args, "full", False) else compact_operator_result(result)
    return emit(payload, 0 if not result.get("error") else 3)



def cmd_campaign_pause_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    action = "pause_campaign" if args.mode == "now" else "pause_after_current"
    plan = operator_stop_plan(lab, action=action, job_ref=args.job)
    payload = plan if getattr(args, "full", False) else compact_operator_plan(
        plan, pause=True, mode=args.mode
    )
    return emit(payload, 0 if plan.get("can_stop") else 3)


def cmd_campaign_pause_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    action = "pause_campaign" if args.mode == "now" else "pause_after_current"
    plan = operator_stop_plan(lab, action=action, job_ref=args.job)
    wait_seconds = args.wait_seconds if args.mode == "now" else min(args.wait_seconds, 5.0)
    result = issue_operator_stop(
        lab,
        plan,
        reason=args.reason,
        wait_seconds=wait_seconds,
    )
    payload = result if getattr(args, "full", False) else compact_operator_result(result)
    return emit(payload, 0 if not result.get("error") else 3)


def _portable_bundle_bridge(
    lab: Laboratory,
    command: str,
    *,
    cases: str,
    results: str | None = None,
    output: str | None = None,
) -> tuple[dict[str, Any], int]:
    module = lab.research_root / "campaign_tools" / "portable_bundle.py"
    if not module.is_file():
        return {"error": f"portable bundle tool not found: {module}"}, 2
    cmd = [
        sys.executable,
        str(module),
        "--laboratory",
        str(lab.config_path),
        command,
        "--cases",
        str(resolve(cases)),
    ]
    if results is not None:
        cmd += ["--results", str(resolve(results))]
    if output is not None:
        cmd += ["--output", str(resolve(output))]
    cp = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        cwd=str(lab.research_root),
        env=child_env(),
        check=False,
    )
    try:
        payload = json.loads(cp.stdout) if cp.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"stdout": cp.stdout}
    if cp.returncode != 0 and "error" not in payload:
        payload["error"] = f"portable bundle command exited with code {cp.returncode}"
    if cp.stderr.strip():
        payload["stderr"] = cp.stderr
    payload["command_exit_code"] = cp.returncode
    return payload, cp.returncode


def cmd_campaign_export_bundle_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    payload, rc = _portable_bundle_bridge(
        lab, "export-plan", cases=args.cases, output=args.output
    )
    return emit(payload, 0 if rc == 0 and payload.get("can_export") else 3)


def cmd_campaign_export_bundle_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    payload, rc = _portable_bundle_bridge(
        lab, "export", cases=args.cases, output=args.output
    )
    return emit(payload, 0 if rc == 0 else 3)


def cmd_campaign_import_results_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    payload, rc = _portable_bundle_bridge(
        lab, "import-plan", cases=args.cases, results=args.results
    )
    return emit(payload, 0 if rc == 0 and payload.get("can_import") else 3)


def cmd_campaign_import_results_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    payload, rc = _portable_bundle_bridge(
        lab, "import", cases=args.cases, results=args.results
    )
    return emit(payload, 0 if rc == 0 else 3)

def cmd_case_stop_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"can_stop": False, "error": f"cases.csv not found: {cases}"}, 2)
    plan = operator_stop_plan(
        lab, action="stop_case", cases=cases, case_id=args.case_id
    )
    payload = plan if getattr(args, "full", False) else compact_operator_plan(plan)
    return emit(payload, 0 if plan.get("can_stop") else 3)


def cmd_case_stop_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    plan = operator_stop_plan(
        lab, action="stop_case", cases=cases, case_id=args.case_id
    )
    result = issue_operator_stop(lab, plan, reason=args.reason, wait_seconds=args.wait_seconds)
    payload = result if getattr(args, "full", False) else compact_operator_result(result)
    return emit(payload, 0 if not result.get("error") else 3)


def _pilot_numeric(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _study_manifest_and_cases(study: Path) -> tuple[dict[str, Any], Path, list[dict[str, str]]]:
    manifest_path = study / "study_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"study manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    cases_raw = manifest.get("cases_csv")
    if not cases_raw:
        raise ValueError(f"study manifest has no cases_csv: {manifest_path}")
    cases = resolve(cases_raw)
    if not cases.is_file():
        raise ValueError(f"cases.csv not found: {cases}")
    return manifest, cases, load_cases(cases)


def _representative_pilot_selection(cases: Path, rows: list[dict[str, str]], max_cases: int) -> dict[str, Any]:
    """Deterministic maximin subset over campaign logical-identity axes.

    The selection is intended for analysis-code validation, not statistical
    sampling.  It favors broad coverage of the campaign identity space and
    avoids the pathological 'first N cases' development pattern.
    """

    if len(rows) <= 1:
        return {"can_pilot": False, "reason": "campaign has fewer than two cases", "selected_case_ids": []}
    target = min(max(5, int(max_cases)), 50, len(rows) - 1)
    if target < 5:
        return {"can_pilot": False, "reason": "pilot requires at least five cases", "selected_case_ids": []}

    policy = load_policy_for_generated(cases)
    identity_fields = [str(x) for x in policy.get("identity_fields", []) if str(x).strip()]
    if not identity_fields:
        raise ValueError("campaign identity policy exposes no identity_fields")

    values_by_row: list[dict[str, Any]] = []
    groups_cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = str(row.get("case_id", "")).strip()
        if not cid:
            raise ValueError("cases.csv contains an empty case_id")
        item: dict[str, Any] = {"case_id": cid}
        for field in identity_fields:
            raw = row.get(field)
            if raw is not None and str(raw).strip() != "":
                item[field] = raw
                continue
            if cid not in groups_cache:
                groups_cache[cid] = load_base_groups(cases, cid)
            item[field] = identity_get_value(groups_cache[cid], field)
        values_by_row.append(item)

    axis_specs: list[dict[str, Any]] = []
    for field in identity_fields:
        raw_values = [item.get(field) for item in values_by_row]
        unique_text = sorted({str(v) for v in raw_values})
        if len(unique_text) <= 1:
            continue
        numeric_values = [_pilot_numeric(v) for v in raw_values]
        is_numeric = all(v is not None for v in numeric_values)
        if is_numeric:
            numbers = [float(v) for v in numeric_values if v is not None]
            lo, hi = min(numbers), max(numbers)
            if hi == lo:
                continue
            axis_specs.append({"field": field, "kind": "numeric", "min": lo, "max": hi, "levels": unique_text})
        else:
            counts = Counter(str(v) for v in raw_values)
            axis_specs.append({"field": field, "kind": "categorical", "counts": counts, "levels": unique_text})

    if not axis_specs:
        selected = [str(row.get("case_id")) for row in rows[:target]]
        return {
            "can_pilot": True,
            "selection_method": "deterministic_source_order_no_varying_identity_axes",
            "selected_case_ids": selected,
            "selected_count": len(selected),
            "identity_axes": [],
        }

    def axis_distance(a: dict[str, Any], b: dict[str, Any], spec: dict[str, Any]) -> float:
        field = spec["field"]
        if spec["kind"] == "numeric":
            av = _pilot_numeric(a.get(field))
            bv = _pilot_numeric(b.get(field))
            if av is None or bv is None:
                return 0.0
            return abs(av - bv) / (spec["max"] - spec["min"])
        return 0.0 if str(a.get(field)) == str(b.get(field)) else 1.0

    def distance(a: dict[str, Any], b: dict[str, Any]) -> float:
        return sum(axis_distance(a, b, spec) for spec in axis_specs) / len(axis_specs)

    # Seed at an identity-space edge/rare category, then greedily maximize the
    # minimum distance to the already selected subset.  Case ID breaks ties.
    def seed_score(item: dict[str, Any]) -> float:
        total = 0.0
        for spec in axis_specs:
            field = spec["field"]
            if spec["kind"] == "numeric":
                val = _pilot_numeric(item.get(field))
                if val is None:
                    continue
                norm = (val - spec["min"]) / (spec["max"] - spec["min"])
                total += abs(norm - 0.5) * 2.0
            else:
                count = spec["counts"].get(str(item.get(field)), len(rows))
                total += 1.0 - count / len(rows)
        return total / len(axis_specs)

    ordered = sorted(values_by_row, key=lambda x: str(x["case_id"]))
    order_index = {str(item["case_id"]): index for index, item in enumerate(ordered)}

    # First cover representative levels of every varying identity axis. For
    # modest discrete sweeps (<=12 levels) this targets every level. For finer
    # numeric axes it targets five quantile-like levels; for large categorical
    # axes it targets the twelve least frequent levels first.
    desired_tokens: set[tuple[str, str]] = set()
    for spec in axis_specs:
        field = spec["field"]
        levels = spec["levels"]
        if spec["kind"] == "numeric" and len(levels) > 12:
            numeric_levels = sorted((float(level), level) for level in levels)
            indices = sorted({0, len(numeric_levels) // 4, len(numeric_levels) // 2, (3 * len(numeric_levels)) // 4, len(numeric_levels) - 1})
            wanted = [numeric_levels[index][1] for index in indices]
        elif spec["kind"] == "categorical" and len(levels) > 12:
            wanted = [
                level for level, _count in sorted(
                    spec["counts"].items(), key=lambda item: (item[1], item[0])
                )[:12]
            ]
        else:
            wanted = levels
        desired_tokens.update((field, str(level)) for level in wanted)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    uncovered = set(desired_tokens)
    while uncovered and len(selected) < target:
        candidates = [item for item in ordered if str(item["case_id"]) not in selected_ids]
        def coverage_key(item: dict[str, Any]) -> tuple[float, float, float, int]:
            covered = sum((spec["field"], str(item.get(spec["field"]))) in uncovered for spec in axis_specs)
            spread = min((distance(item, chosen) for chosen in selected), default=seed_score(item))
            return (float(covered), spread, seed_score(item), -order_index[str(item["case_id"])])
        best = max(candidates, key=coverage_key)
        best_tokens = {(spec["field"], str(best.get(spec["field"]))) for spec in axis_specs}
        if not (best_tokens & uncovered):
            break
        selected.append(best)
        selected_ids.add(str(best["case_id"]))
        uncovered.difference_update(best_tokens)

    # Fill the remainder by maximin distance to broaden combination coverage.
    if not selected:
        seed = max(ordered, key=lambda item: (seed_score(item), -order_index[str(item["case_id"])]))
        selected = [seed]
        selected_ids = {str(seed["case_id"])}
    while len(selected) < target:
        candidates = [item for item in ordered if str(item["case_id"]) not in selected_ids]
        best = max(
            candidates,
            key=lambda item: (min(distance(item, chosen) for chosen in selected), -order_index[str(item["case_id"])]),
        )
        selected.append(best)
        selected_ids.add(str(best["case_id"]))

    coverage: list[dict[str, Any]] = []
    for spec in axis_specs:
        field = spec["field"]
        all_levels = spec["levels"]
        selected_levels = sorted({str(item.get(field)) for item in selected})
        coverage.append({
            "field": field,
            "kind": spec["kind"],
            "campaign_unique_count": len(all_levels),
            "selected_unique_count": len(selected_levels),
            "all_levels": all_levels[:20],
            "all_levels_truncated": len(all_levels) > 20,
            "selected_levels": selected_levels[:20],
            "selected_levels_truncated": len(selected_levels) > 20,
        })

    return {
        "can_pilot": True,
        "selection_method": "deterministic_identity_space_maximin_v1",
        "selected_count": len(selected),
        "selected_case_ids": [str(item["case_id"]) for item in selected],
        "selected_identity_values": [
            {"case_id": str(item["case_id"]), **{spec["field"]: item.get(spec["field"]) for spec in axis_specs}}
            for item in selected
        ],
        "identity_axes": coverage,
        "guidance": (
            "This is a deterministic analysis-development subset chosen for broad identity-space coverage, "
            "not a statistical sample. Inspect axis coverage before accepting it; add exact difficult cases if needed."
        ),
    }


def cmd_study_pilot_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    study = resolve(args.study, lab.studies_root)
    try:
        manifest, cases, rows = _study_manifest_and_cases(study)
        plan = _representative_pilot_selection(cases, rows, args.max_cases)
    except Exception as exc:
        return emit({"can_pilot": False, "error": str(exc), "study": str(study)}, 2)
    return emit({
        "study": str(study),
        "scientific_request": manifest.get("scientific_request", ""),
        "cases_csv": str(cases),
        "campaign_case_count": len(rows),
        **plan,
    }, 0 if plan.get("can_pilot") else 3)


def cmd_run_study_pilot(args: argparse.Namespace, lab: Laboratory) -> int:
    study = resolve(args.study, lab.studies_root)
    command = [
        sys.executable, "-m", "agent_workspace.run_study", str(study),
        "--laboratory", str(lab.config_path), "--mode", "pilot",
    ]
    for case_id in args.case_id:
        command.extend(["--case-id", case_id])
    cp = subprocess.run(command, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    payload: dict[str, Any] = {
        "command": command, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr,
        "study": str(study), "execution_mode": "pilot",
    }
    for key, rel in (("pilot_provenance", "pilot/provenance.json"), ("pilot_validation", "pilot_validation.json")):
        path = study / rel
        if path.is_file():
            try:
                payload[key] = read_json(path)
            except Exception:
                pass
    return emit(payload, 0 if cp.returncode == 0 else 2)

def cmd_create_study(args: argparse.Namespace, lab: Laboratory) -> int:
    command = [sys.executable, "-m", "agent_workspace.create_study", "--slug", args.slug, "--request", args.request, "--cases", str(resolve(args.cases)), "--laboratory", str(lab.config_path)]
    if args.force:
        command.append("--force")
    cp = subprocess.run(command, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    return emit({"command": command, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}, 0 if cp.returncode == 0 else 2)


def cmd_run_study(args: argparse.Namespace, lab: Laboratory) -> int:
    study = resolve(args.study, lab.studies_root)
    command = [sys.executable, "-m", "agent_workspace.run_study", str(study), "--laboratory", str(lab.config_path), "--mode", "full"]
    cp = subprocess.run(command, text=True, capture_output=True, cwd=str(lab.research_root), env=child_env(), check=False)
    payload: dict[str, Any] = {"command": command, "exit_code": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr, "study": str(study)}
    prov = study / "provenance.json"
    if prov.is_file():
        try:
            payload["provenance"] = read_json(prov)
        except Exception:
            pass
    return emit(payload, 0 if cp.returncode == 0 else 2)


def cmd_study_summary(args: argparse.Namespace, lab: Laboratory) -> int:
    study = resolve(args.study, lab.studies_root)
    payload: dict[str, Any] = {"study": str(study)}
    for name, rel in (
        ("manifest", "study_manifest.json"),
        ("pilot_validation", "pilot_validation.json"),
        ("pilot_provenance", "pilot/provenance.json"),
        ("provenance", "provenance.json"),
        ("summary", "results/study_summary.json"),
    ):
        path = study / rel
        if path.is_file():
            try:
                payload[name] = read_json(path)
            except Exception as exc:
                payload[name] = {"error": str(exc), "path": str(path)}
    results = study / "results"
    if results.is_dir():
        payload["result_files"] = [str(p.relative_to(study)) for p in sorted(results.rglob("*")) if p.is_file()][:100]
    return emit(payload)


def cmd_dev_transaction_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_transaction_plan(lab, args.task, args.rationale, args.allowed_path))


def cmd_dev_transaction_start(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_transaction_start(lab, args.transaction_id, args.task, args.rationale,
                                      args.allowed_path, args.baseline_sha))


def cmd_dev_transaction_status(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_transaction_status(lab, args.transaction_id))


def cmd_dev_transaction_list(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_transaction_list(lab))


def cmd_dev_read(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_read_file(lab, args.transaction_id, args.path, args.offset, args.limit))


def cmd_dev_apply_patch_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_apply_patch_plan(lab, args.transaction_id, args.patch))


def cmd_dev_apply_patch_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_apply_patch_execute(lab, args.transaction_id, args.patch,
                                        args.expected_patch_sha256, args.expected_head_sha,
                                        args.expected_candidate_tree_sha))


def cmd_dev_write_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_write_plan(lab, args.transaction_id, args.path, args.content, args.mode))


def cmd_dev_write_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_write_execute(
        lab, args.transaction_id, args.path, args.content, args.mode,
        args.expected_semantic_spec_sha256, args.expected_head_sha,
        args.expected_candidate_tree_sha, args.expected_branch,
        args.expected_target_sha256,
    ))


def cmd_dev_edit_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_edit_plan(
        lab, args.transaction_id, args.path, args.old_text, args.new_text,
        args.expected_occurrences,
    ))


def cmd_dev_edit_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_edit_execute(
        lab, args.transaction_id, args.path, args.old_text, args.new_text,
        args.expected_occurrences, args.expected_semantic_spec_sha256,
        args.expected_head_sha, args.expected_candidate_tree_sha,
        args.expected_branch, args.expected_target_sha256,
    ))


def cmd_dev_diff(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_diff(lab, args.transaction_id))


def cmd_dev_test_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_test_plan(lab, args.transaction_id, args.profile, args.test_file,
                              bool(args.require_nrg)))


def cmd_dev_test_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_test_execute(lab, args.transaction_id, args.profile, args.test_file,
                                 bool(args.require_nrg), args.expected_candidate_tree_sha,
                                 args.expected_head_sha, args.expected_branch))


def cmd_dev_commit_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_commit_plan(lab, args.transaction_id, args.message))


def cmd_dev_commit_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_commit_execute(lab, args.transaction_id, args.message,
                                   args.expected_candidate_tree_sha,
                                   args.expected_head_sha, args.expected_branch))


def cmd_dev_discard_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_discard_plan(lab, args.transaction_id))


def cmd_dev_discard_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    return emit(dev_discard_execute(lab, args.transaction_id,
                                    args.expected_head_sha,
                                    args.expected_candidate_tree_sha,
                                    args.expected_branch,
                                    args.expected_staged_path or None,
                                    args.expected_unexpected_change or None))


# ---------------------------------------------------------------------------
# E0 Mode 3 (Route B) -- R3-corrected.  Authoritative pins (commit, source/
# harness/validation-config SHA-256, cmake target/selector, mechanism selector)
# and the fixed researcher-approved acceptance thresholds live ONLY in the
# protected authorization record read by _e0_authorization(); the ordinary [e0]
# table holds operational paths/settings and is never authoritative (R2).  Bounded
# subprocesses use a fixed argv, a minimal env allowlist, and whole-process-tree
# kill on timeout (R3).  Mandatory finite result.json checks and precise
# recomputed-vs-inspected evidence claims are enforced (R4/R5).  validation_config.json
# is authenticated at plan+execute via validation_config_sha256, and
# _e0_parse_results requires exactly one KEROMNES record with complete TP/HP
# blocks (validation-config round).  A subprocess return code of zero is never
# treated as a scientific PASS.
# ---------------------------------------------------------------------------

import signal

# Operation-scoped operational key sets (optional-Cantera contract):
#   build      -> NRG source + isolated build root + executable name only
#   validation -> additionally the isolated run root, the pinned harness, and
#                 an explicitly configured Cantera interpreter launcher
_E0_BASE_REQUIRED_KEYS = (
    "nrg_source_root", "build_root", "equilibrium_executable",
)
_E0_VALIDATION_REQUIRED_KEYS = ("run_root", "harness", "cantera_python")
_E0_REQUIRED_KEYS = _E0_BASE_REQUIRED_KEYS + _E0_VALIDATION_REQUIRED_KEYS
_E0_BASE_PATH_KEYS = ("nrg_source_root", "build_root")
_E0_PATH_KEYS = _E0_BASE_PATH_KEYS + _E0_VALIDATION_REQUIRED_KEYS
_E0_OPERATIONS = ("build", "validation")
# Optional independent reference: only these keys may be absent without making
# the generic laboratory or the NRG package-interface build unready.
_E0_OPTIONAL_REFERENCE_KEYS = frozenset({"cantera_python"})

# Optional-reference verification statuses.  These describe the independent
# reference only; they are never generic laboratory readiness and never a
# scientific PASS or FAIL of NRG itself.
_E0_REFERENCE_NOT_CONFIGURED = "REFERENCE_NOT_CONFIGURED"
_E0_REFERENCE_UNVERIFIED = "REFERENCE_UNVERIFIED"
_E0_REFERENCE_AVAILABLE = "REFERENCE_AVAILABLE"
_E0_CANTERA_UNAVAILABLE = "CANTERA_UNAVAILABLE"
_E0_CANTERA_PROBE = "import cantera; print(cantera.__version__); print(cantera.__file__)"
_E0_REFERENCE_PROBE_TIMEOUT_S = 120
_E0_SOURCE_REL = "package_interface/src/tests/classic_tests/0D_chemical_equilibrium_validation.f90"
_E0_CMAKE_SELECTOR = "src/tests/classic_tests/0D_chemical_equilibrium_validation.f90"
_E0_CMAKE_TARGET = "package_interface"
_E0_TASK_SETUP_REL = "package_interface/task_setup"
_E0_CONFIG_REL = "validation_config.json"
_V3_MARKER = "NRG_CHEMKIN_VALIDATION_AUDIT_V3"
_E0_MECHANISM_SELECTOR = "hydrogen/KEROMNES.txt"
_E0_SIX_MECHANISMS = (
    "hydrogen/KEROMNES.txt",
    "hydrogen/KONNOV.txt",
    "hydrogen/TEREZA.txt",
    "hydrogen/ZHANG.txt",
    "ammonia/OKAFOR.txt",
    "acetylene/VARATHARAJAN.txt",
)
# Expected reference pressure (Pa) for these atmospheric equilibrium cases
# (matches the historical archive and the harness's fixed problem definition).
_E0_EXPECTED_PRESSURE_PA = 101325.0

# Protected authorization record (R2): researcher-installed and self-protected
# OUT-OF-BAND.  This is where the authoritative pins live; the ordinary [e0]
# table must never carry authoritative pins.  Fail closed if absent/malformed.
_E0_AUTH_REL = "config/e0_authorization.toml"
_E0_AUTH_PINS = (
    "commit", "source_sha256", "harness_sha256", "validation_config_sha256",
    "source_relative", "cmake_selector", "cmake_target", "mechanism_selector",
)
# Exactly the researcher-approved acceptance keys (R6).  The fixed threshold
# VALUES live in the protected authorization record; [e0] cannot broaden them.
# Missing or
# extra keys fail closed; no default threshold is ever silently inserted.
_E0_ACCEPTANCE_KEYS = (
    "normalization_tolerance",
    "nonnegativity_tolerance",
    "element_conservation_tolerance",
    "mass_conservation_tolerance",
    "enthalpy_residual_tolerance",
    "mole_fraction_tolerance",
    "temperature_tolerance_K",
)
_E0_ACCEPTANCE_MAX = {
    "normalization_tolerance": 1.0,
    "nonnegativity_tolerance": 1.0,
    "element_conservation_tolerance": 1.0,
    "mass_conservation_tolerance": 1.0,
    "enthalpy_residual_tolerance": 1.0,
    "mole_fraction_tolerance": 1.0,
    "temperature_tolerance_K": 1000.0,
}
# NRG-reported residual diagnostics (R4/R5).  All five are present in both TP and
# HP archived CSVs and in result.json's nrg_diagnostics; missing/duplicate keys
# are rejected, never defaulted.  The first three map to an acceptance tolerance;
# mole_fraction_sum_residual/stationarity_residual are validated for
# presence/finiteness/nonnegativity but have no independent tolerance.
_E0_RESIDUAL_KEYS = (
    "max_element_relative_residual",
    "mass_relative_residual",
    "mole_fraction_sum_residual",
    "stationarity_residual",
    "enthalpy_relative_residual",
)
_E0_RESIDUAL_TOL = {
    "max_element_relative_residual": "element_conservation_tolerance",
    "mass_relative_residual": "mass_conservation_tolerance",
    "enthalpy_relative_residual": "enthalpy_residual_tolerance",
}
_E0_DIAGNOSTICS_KEYS = ("converged", "temperature_K", "pressure_Pa") + _E0_RESIDUAL_KEYS

# Minimal explicit environment allowlist (R3/R8).  Only a fixed set of
# runtime-relevant variables is inherited.  Git configuration, proxies,
# LD_PRELOAD, shell init (BASH_ENV/ENV), PYTHONSTARTUP, and every other user
# variable are DROPPED so a malicious environment variable cannot reach a
# subprocess.  Build-tool overrides (FC/CC/CXX/F77/F90) and include/library
# path overrides (CPATH/C_INCLUDE_PATH/CPLUS_INCLUDE_PATH/LIBRARY_PATH) are
# deliberately EXCLUDED (R8): the Fortran compiler is discovered and pinned
# explicitly via -DCMAKE_Fortran_COMPILER, so an env-var compiler redirect
# cannot steer the build.  (PYTHONPATH is excluded; Cantera must be found via
# the cantera_python interpreter's own site-packages.)  PATH/HOME/
# LD_LIBRARY_PATH remain a documented residual (NOT sandboxed) risk -- see
# security_delta.md; this is an env allowlist, not an OS sandbox.
_E0_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
)

# Bridge/Pi timeout relationship (R4-deadline).  The index.ts bridge uses
# E0_TIMEOUT_MS = 30 min = 1800 s for the WHOLE build and the WHOLE validate
# command.  [e0].timeout_seconds is reinterpreted as a TOTAL per-command
# deadline that must leave a cleanup margin so that, on timeout or external
# cancellation, the whole-process-group SIGKILL + reap + log write completes
# BEFORE the bridge's own timeout fires.
_E0_BRIDGE_TIMEOUT_S = 1800
_E0_CLEANUP_MARGIN_S = 120


def _isfinite(x: float) -> bool:
    return x == x and abs(x) != float("inf")


def _is_sha256(s: str) -> bool:
    s = (s or "").strip().lower()
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def _sanitized_env() -> dict[str, str]:
    """Minimal explicit environment allowlist (R3)."""
    env: dict[str, str] = {}
    for k in _E0_ENV_ALLOWLIST:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    return env


def _e0_authorization(lab: Laboratory) -> dict[str, Any]:
    """Read the protected, researcher-installed E0 authorization record (R2).

    Authoritative pins (commit, source/harness SHA-256, cmake target/selector,
    mechanism selector) and the fixed researcher-approved acceptance thresholds
    live ONLY here.  The record is self-protected and installed OUT-OF-BAND; the
    ordinary [e0] table never carries authoritative pins.  Fail closed if the
    record is absent, a symlink, malformed, or inconsistent with the fixed
    constants.
    """
    auth_path = Path(lab.research_root).resolve() / _E0_AUTH_REL
    if auth_path.is_symlink():
        raise RuntimeError(f"authorization record is a symlink (refusing): {auth_path}")
    if not auth_path.is_file():
        raise RuntimeError(
            f"E0 authorization record missing: {auth_path}; researcher installation "
            "is out-of-band (see trust_policy_proposal.md in this study)."
        )
    try:
        with auth_path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001 -- fail closed on any read/parse error
        raise RuntimeError(f"E0 authorization record unreadable: {exc}")
    auth = data.get("e0_authorization")
    if not isinstance(auth, dict):
        raise RuntimeError("[e0_authorization] table missing in authorization record")
    missing = [k for k in _E0_AUTH_PINS if k not in auth]
    if missing:
        raise RuntimeError(f"[e0_authorization] missing pins: {sorted(missing)}")
    for k in ("source_sha256", "harness_sha256", "validation_config_sha256"):
        if not _is_sha256(str(auth[k])):
            raise RuntimeError(f"[e0_authorization].{k} must be a 64-char hex SHA-256")
    commit = str(auth["commit"]).strip().lower()
    if not (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)):
        raise RuntimeError("[e0_authorization].commit must be a full 40-hex git commit")
    auth["commit"] = commit
    for k, want in (
        ("source_relative", _E0_SOURCE_REL),
        ("cmake_selector", _E0_CMAKE_SELECTOR),
        ("cmake_target", _E0_CMAKE_TARGET),
        ("mechanism_selector", _E0_MECHANISM_SELECTOR),
    ):
        if str(auth[k]) != want:
            raise RuntimeError(f"[e0_authorization].{k} must equal {want!r}")
    acc = auth.get("acceptance")
    if not isinstance(acc, dict):
        raise RuntimeError("[e0_authorization.acceptance] must be a table")
    unknown = set(acc) - set(_E0_ACCEPTANCE_KEYS)
    if unknown:
        raise RuntimeError(f"[e0_authorization.acceptance] contains unapproved keys: {sorted(unknown)}")
    missing_acc = set(_E0_ACCEPTANCE_KEYS) - set(acc)
    if missing_acc:
        raise RuntimeError(f"[e0_authorization.acceptance] missing required keys: {sorted(missing_acc)}")
    checked: dict[str, float] = {}
    for k in _E0_ACCEPTANCE_KEYS:
        try:
            v = float(acc[k])
        except (TypeError, ValueError):
            raise RuntimeError(f"[e0_authorization.acceptance].{k} must be numeric")
        if not _isfinite(v):
            raise RuntimeError(f"[e0_authorization.acceptance].{k} must be finite (NaN/Inf rejected)")
        if v < 0.0:
            raise RuntimeError(f"[e0_authorization.acceptance].{k} must be nonnegative")
        if v > _E0_ACCEPTANCE_MAX[k]:
            raise RuntimeError(f"[e0_authorization.acceptance].{k} exceeds permissible range (>{_E0_ACCEPTANCE_MAX[k]})")
        checked[k] = v
    auth["acceptance"] = checked
    return auth


def _e0_config(lab: Laboratory, operation: str = "validation") -> dict[str, Any]:
    """Load and type-check OPERATIONAL [e0] settings only (R2).

    Authoritative pins and acceptance thresholds are NOT read here; they come
    from the protected authorization record via _e0_authorization().  [e0]
    (config_path + local_config_path) holds paths and operational knobs only,
    and any pin-like value it carries is at most a cross-check, never an
    authority.

    Required keys are scoped by operation (optional-Cantera contract):
      * operation="build"      - nrg_source_root, build_root, and the
        equilibrium executable name.  A Cantera interpreter, a validation run
        root, and a validation harness are NOT required, so the NRG build path
        never depends on the optional Cantera reference.
      * operation="validation" - additionally run_root, harness, and
        cantera_python.  Absence of only cantera_python raises
        _E0ReferenceNotConfigured (an optional-reference status) instead of a
        generic laboratory failure.
    """
    if operation not in _E0_OPERATIONS:
        raise RuntimeError(f"[e0] unknown config operation {operation!r}; expected one of {list(_E0_OPERATIONS)}")
    required = _E0_BASE_REQUIRED_KEYS if operation == "build" else _E0_REQUIRED_KEYS
    tables: list[dict[str, Any]] = []
    for src in (lab.config_path, lab.local_config_path):
        if src is None or not src.is_file():
            continue
        with src.open("rb") as fh:
            data = tomllib.load(fh)
        e0 = data.get("e0")
        if isinstance(e0, dict):
            tables.append(e0)
    merged: dict[str, Any] = {}
    for t in tables:  # local config wins (merged last)
        merged.update(t)
    missing = [k for k in required if k not in merged]
    if missing:
        if operation == "validation" and set(missing) <= _E0_OPTIONAL_REFERENCE_KEYS:
            raise _E0ReferenceNotConfigured(
                f"[e0] config missing required keys: {sorted(missing)} "
                "(optional Cantera reference not configured; generic laboratory and NRG build remain usable)"
            )
        raise RuntimeError(f"[e0] config missing required keys: {sorted(missing)}")
    for key in required:
        if not isinstance(merged[key], str) or not merged[key].strip():
            if key in _E0_OPTIONAL_REFERENCE_KEYS:
                raise _E0ReferenceNotConfigured(
                    f"[e0] {key} must be a non-empty string (optional Cantera reference not configured)"
                )
            raise RuntimeError(f"[e0] {key} must be a non-empty string")
    try:
        jobs = int(merged["parallel_jobs"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("[e0] parallel_jobs must be an int")
    if not 1 <= jobs <= 64:
        raise RuntimeError("[e0] parallel_jobs must be an int in 1..64")
    merged["parallel_jobs"] = jobs
    try:
        timeout = int(merged["timeout_seconds"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("[e0] timeout_seconds must be an int")
    if timeout < 1:
        raise RuntimeError("[e0] timeout_seconds must be positive")
    merged["timeout_seconds"] = timeout
    btype = str(merged.get("build_type", "release")).strip().lower()
    if btype not in ("release", "debug"):
        raise RuntimeError("[e0] build_type must be 'release' or 'debug'")
    merged["build_type"] = btype
    return merged


def _e0_raw_path(cfg: dict[str, Any], key: str, lab: Laboratory) -> Path:
    """Join the RAW config value against research_root WITHOUT resolving symlinks
    or collapsing '..' (R1).  This pre-resolution form is validated before
    canonicalization so a symlink or '..' cannot be hidden by .resolve()."""
    raw = Path(str(cfg[key])).expanduser()
    if not raw.is_absolute():
        raw = Path(lab.research_root).resolve() / raw
    return raw


def _e0_path(cfg: dict[str, Any], key: str, lab: Laboratory) -> Path:
    """Validate raw path components BEFORE resolving, then return the canonical
    resolved path (R1)."""
    raw = _e0_raw_path(cfg, key, lab)
    _reject_raw_symlink_or_dotdot(raw)
    return raw.resolve()


def _e0_nrg_root(cfg: dict[str, Any], lab: Laboratory) -> Path:
    """Canonical nrg_source_root resolved ONCE against research_root (correction 5)."""
    return _e0_path(cfg, "nrg_source_root", lab)


def _git(root: Path, *args: str, timeout: int = 30) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "--no-pager", "--no-optional-locks", *args],
            capture_output=True, text=True, timeout=timeout, env=_sanitized_env(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _e0_discover_fortran() -> tuple[str, str]:
    """Return (compiler, first version line); fail if a present compiler's --version fails."""
    for cand in ("gfortran", "gfortran-13", "ifx", "ifort"):
        try:
            proc = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=10, env=_sanitized_env())
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Fortran compiler {cand} --version timed out")
        if proc.returncode != 0:
            raise RuntimeError(f"Fortran compiler {cand} found but '--version' exited {proc.returncode}")
        first = (proc.stdout or proc.stderr or "").strip().splitlines()
        if not first:
            raise RuntimeError(f"Fortran compiler {cand} '--version' produced no output")
        return cand, first[0]
    raise RuntimeError("no supported Fortran compiler found (gfortran/gfortran-13/ifx/ifort)")


def _e0_checkout_preflight(cfg: dict[str, Any], auth: dict[str, Any], lab: Laboratory) -> dict[str, Any]:
    root = _e0_nrg_root(cfg, lab)
    if not (root / ".git").exists():
        raise RuntimeError(f"nrg_source_root is not a git checkout: {root}")
    head = _git(root, "rev-parse", "HEAD").strip()
    if head != auth["commit"]:
        raise RuntimeError(f"NRG checkout HEAD {head[:12]} != authorized commit {auth['commit'][:12]}; researcher source review required.")
    status = _git(root, "status", "--porcelain")
    if status.strip():
        raise RuntimeError(f"NRG checkout is not clean; researcher source review required:\n{status[:2000]}")
    src = root / _E0_SOURCE_REL
    if not src.is_file():
        raise RuntimeError(f"NRG equilibrium source missing: {src}")
    if _V3_MARKER not in src.read_text(encoding="utf-8"):
        raise RuntimeError(f"NRG equilibrium source lacks {_V3_MARKER}; source review required.")
    src_sha = sha256_file(src)
    if auth["source_sha256"] != src_sha:
        raise RuntimeError(f"NRG source SHA-256 {src_sha[:12]} != authorized source_sha256 {auth['source_sha256'][:12]}; source review required.")
    return {"commit": head, "source_sha256": src_sha, "source_path": str(src)}


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise RuntimeError(f"path escapes root: {path} not under {root}")


def _reject_raw_symlink_or_dotdot(path: Path, allow_final_symlink: bool = False) -> None:
    """Reject '..' or symlink components in the RAW (pre-canonicalization) path.

    A symlink whose target does not exist is still rejected (Path.is_symlink()
    is True for a broken link).  This closes the TOCTOU gap where .resolve()
    would silently follow the link before the overlap check runs.

    allow_final_symlink=True is a narrowly scoped exception for the FINAL path
    component only.  It exists for a virtual-environment interpreter launcher
    (.venv/bin/python -> base interpreter), which is normally a symlink.  Every
    directory ancestor, any '..' component, and every other symlink component
    are still refused, so this exception cannot escape confinement or hide a
    traversal."""
    raw = Path(str(path))
    if ".." in raw.parts:
        raise RuntimeError(f"path contains '..' component: {path}")
    last = len(raw.parts)
    for i in range(1, last + 1):
        if allow_final_symlink and i == last:
            continue
        prefix = Path(*raw.parts[:i])
        try:
            if prefix.is_symlink():
                raise RuntimeError(f"symlink component in path (target may be empty): {prefix}")
        except OSError:
            pass
    return None


def _reject_symlink_or_overlap(a: Path, b: Path) -> None:
    ar, br = a.resolve(), b.resolve()
    if ar == br or ar in br.parents or br in ar.parents:
        raise RuntimeError(f"paths overlap: {a} and {b}")


def _e0_boundaries(cfg: dict[str, Any], lab: Laboratory, require_run_root: bool = True) -> dict[str, str | None]:
    """Pre-resolution path confinement (R1 / optional-Cantera contract).

    Raw '..' and symlink components (including existing ancestors) are rejected
    BEFORE canonicalization.  build_root/run_root must not overlap each other,
    the NRG source, or trusted research_root.  Derived output_root/scratch must
    remain plain directories under run_root (a symlink is refused even if its
    target is empty) and must not overlap the input staging area.

    require_run_root=False is the NRG-build case: the build neither reads nor
    writes any run root, so [e0].run_root is not required and not validated
    there.  The build must work with no validation/Cantera configuration.
    """
    nrg_root = _e0_nrg_root(cfg, lab)
    build_raw = _e0_raw_path(cfg, "build_root", lab)
    _reject_raw_symlink_or_dotdot(build_raw)
    build_root = build_raw.resolve()
    trusted = lab.research_root.resolve()
    _reject_symlink_or_overlap(build_root, nrg_root)
    try:
        build_root.relative_to(trusted)
    except ValueError:
        pass
    else:
        raise RuntimeError(f"[e0].build_root overlaps trusted research_root (lab code / protected results): {build_root}")
    if not require_run_root:
        return {"build_root": str(build_root), "run_root": None, "output_root": None, "scratch": None}
    run_raw = _e0_raw_path(cfg, "run_root", lab)
    _reject_raw_symlink_or_dotdot(run_raw)
    run_root = run_raw.resolve()
    _reject_symlink_or_overlap(build_root, run_root)
    _reject_symlink_or_overlap(run_root, nrg_root)
    try:
        run_root.relative_to(trusted)
    except ValueError:
        pass
    else:
        raise RuntimeError(f"[e0].run_root overlaps trusted research_root (lab code / protected results): {run_root}")
    output_root = run_root / "output"
    scratch = run_root / "scratch"
    for derived, name in ((output_root, "output_root"), (scratch, "scratch")):
        if derived.is_symlink():
            raise RuntimeError(f"derived {name} is a symlink (refusing even if its target is empty): {derived}")
        try:
            derived.resolve().relative_to(run_root)
        except ValueError:
            raise RuntimeError(f"derived {name} escapes run_root: {derived}")
    _reject_symlink_or_overlap(output_root, scratch)
    return {
        "build_root": str(build_root),
        "run_root": str(run_root),
        "output_root": str(output_root),
        "scratch": str(scratch),
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _kill_process_group(proc: "subprocess.Popen[str]") -> None:
    """SIGKILL the entire process group (session) of proc, then reap it (R3).

    proc was started with start_new_session=True, so its pgid == its pid and all
    descendants (e.g. the NRG binary launched by the harness) inherit that pgid.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _e0_deadline(cfg: dict[str, Any]) -> float:
    """Total per-command deadline (R4).  [e0].timeout_seconds is the TOTAL budget
    for one bridge command (build OR validate), and it must leave a cleanup
    margin below the bridge timeout so process-group teardown completes before
    the bridge kills us."""
    timeout = int(cfg["timeout_seconds"])
    if timeout + _E0_CLEANUP_MARGIN_S > _E0_BRIDGE_TIMEOUT_S:
        raise RuntimeError(
            f"[e0] timeout_seconds ({timeout}) + cleanup margin ({_E0_CLEANUP_MARGIN_S}) "
            f"exceeds bridge timeout ({_E0_BRIDGE_TIMEOUT_S}); reduce timeout_seconds."
        )
    return time.monotonic() + timeout


def _e0_remaining(deadline: float) -> int:
    rem = deadline - time.monotonic()
    if rem <= 0:
        raise RuntimeError("total execution deadline exceeded")
    return int(rem)


class _E0Cancellation(RuntimeError):
    """Cancellation of an E0 child process; never a scientific PASS."""


def _run_bounded(cmd: list[str], label: str, timeout: int, log_path: Path) -> str:
    """Run fixed argv in a new process group; handle timeout and bridge SIGTERM.

    The SIGTERM handler is a *flag only*.  It is installed in the bridge's
    main thread before Popen, stays active until teardown completes, and does
    no subprocess, IO, or signal-group work itself.  communicate() is polled
    in bounded increments so signal arrival is noticed despite PEP 475
    interrupted-system-call restarts.  It covers SIGTERM before/during spawn
    and while collecting output.  SIGKILL cannot be handled here.
    """
    if timeout <= 0:
        raise RuntimeError(f"{label}: nonpositive timeout")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cancelled = {"requested": False, "count": 0}

    def request_cancel(_signum: int, _frame: Any) -> None:
        cancelled["requested"] = True
        cancelled["count"] += 1

    try:
        previous_term = signal.signal(signal.SIGTERM, request_cancel)
    except ValueError as exc:
        raise RuntimeError("E0 subprocess wrapper requires the main thread") from exc

    proc: subprocess.Popen[str] | None = None
    out, err = "", ""
    outcome = "NOT_STARTED"
    try:
        if cancelled["requested"]:
            raise _E0Cancellation(f"{label} cancelled before launch")
        # A SIGTERM arriving inside Popen only marks the flag.  Cleanup runs
        # as soon as Popen returns the new session's process-group identifier.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_sanitized_env(), start_new_session=True,
        )
        deadline = time.monotonic() + float(timeout)
        while True:
            if cancelled["requested"]:
                raise _E0Cancellation(f"{label} cancelled by external SIGTERM")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                out, err = proc.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        # Also reject a SIGTERM that arrived between completion and return.
        if cancelled["requested"]:
            raise _E0Cancellation(f"{label} cancelled by external SIGTERM")
        outcome = f"EXIT_{proc.returncode}"
        if proc.returncode != 0:
            raise RuntimeError(f"{label} exited {proc.returncode}; see {log_path}: {err[-2000:]}")
        return out
    except BaseException as exc:
        outcome = "CANCELLED" if cancelled["requested"] or isinstance(exc, _E0Cancellation) else (
            "TIMEOUT" if isinstance(exc, subprocess.TimeoutExpired) else "FAILED"
        )
        if proc is not None:
            _kill_process_group(proc)
            try:
                out, err = proc.communicate(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                # Descendants that escaped the process group cannot be
                # guaranteed reaped by Python.  No unbounded pipe read here.
                _kill_process_group(proc)
                out, err = "", ""
        if isinstance(exc, subprocess.TimeoutExpired):
            raise RuntimeError(f"{label} timed out after {timeout}s; process group terminated. see {log_path}") from exc
        raise
    finally:
        # Restore before exiting this fixed E0 operation.  A second SIGTERM
        # during cleanup is recorded by the same flag without recursion.
        signal.signal(signal.SIGTERM, previous_term)
        # Logs are written for every outcome, including cancellation before
        # Popen, never reporting scientific PASS on external cancellation.
        log_path.write_text(
            f"# {label}\n# command: {' '.join(cmd)}\n# result: {outcome}"
            f"\n# cancellation_signals: {cancelled['count']}\n"
            f"\n== STDOUT ==\n{out}\n\n== STDERR ==\n{err}\n",
            encoding="utf-8",
        )


def _e0_git_objects(root: Path, commit: str, allowed_prefix: str, source_rel: str) -> dict[str, tuple[str, str]]:
    """Return {git_path: (mode, oid)} from an *approved commit*, never index.

    Parse git ls-tree -r -z as raw bytes.  Both the requested input prefix and
    the single V3 source must appear; all paths are normalized UTF-8 and must
    remain in the approved prefixes.  No shell, checkout, or working-tree reads.
    """
    if not (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit.lower())):
        raise RuntimeError("invalid authorized commit")
    command = [
        "git", "-C", str(root), "--no-pager", "--no-optional-locks", "ls-tree",
        "-r", "-z", commit, "--", allowed_prefix + "/", source_rel,
    ]
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=False, timeout=30, env=_sanitized_env())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("approved-commit git ls-tree timed out") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"approved-commit git ls-tree failed: {proc.stderr[:500]!r}")
    raw = proc.stdout
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError("approved-commit tree inventory exceeds size limit")
    if not raw or not raw.endswith(b"\0"):
        raise RuntimeError("empty or malformed approved-commit git tree inventory")
    entries: dict[str, tuple[str, str]] = {}
    for item in raw[:-1].split(b"\0"):
        if b"\t" not in item:
            raise RuntimeError("invalid NUL-separated git tree entry")
        meta, raw_path = item.split(b"\t", 1)
        fields = meta.split(b" ")
        if len(fields) != 3 or not all(fields) or fields[1] not in (b"blob", b"commit"):
            raise RuntimeError(f"invalid Git tree metadata: {meta!r}")
        try:
            mode, oid, rel = fields[0].decode("ascii"), fields[2].decode("ascii"), raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise RuntimeError("invalid Git tree metadata/path encoding") from exc
        if (len(oid) not in (40, 64) or any(ch not in "0123456789abcdef" for ch in oid)):
            raise RuntimeError("invalid git blob OID")
        if (not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or
            any(p in ("", ".", "..") for p in rel.split("/"))):
            raise RuntimeError(f"unsafe Git-tree path: {rel!r}")
        if not (rel.startswith(allowed_prefix + "/") or rel == source_rel):
            raise RuntimeError(f"unexpected approved-commit path: {rel}")
        if rel in entries:
            raise RuntimeError(f"duplicate approved-commit tree path: {rel}")
        if mode == "120000":
            raise RuntimeError(f"symlink in approved commit: {rel}")
        if mode == "160000" or fields[1] == b"commit":
            raise RuntimeError(f"gitlink in approved commit: {rel}")
        if mode not in ("100644", "100755") or fields[1] != b"blob":
            raise RuntimeError(f"unsupported Git mode/type {mode}: {rel}")
        entries[rel] = mode, oid
    if source_rel not in entries:
        raise RuntimeError(f"approved commit is missing V3 source: {source_rel}")
    if not any(path.startswith(allowed_prefix + "/") for path in entries):
        raise RuntimeError("approved commit contains no task_setup data")
    return entries


def _e0_read_git_blob(root: Path, oid: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """Retrieve approved Git blob bytes with a bounded fixed-argv subprocess.

    Independently check Git's content-addressed blob OID (SHA-1 or SHA-256).
    This is in addition to the SHA-256 content manifest below.
    """
    # Preflight exact Git object size before allocating the binary stdout pipe.
    # Git content-addresses the blob OID; reject oversized input before read.
    try:
        size_proc = subprocess.run(
            ["git", "-C", str(root), "--no-pager", "--no-optional-locks", "cat-file", "-s", oid],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, env=_sanitized_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"approved Git blob {oid} size probe timed out") from exc
    if size_proc.returncode != 0:
        raise RuntimeError(f"approved Git blob {oid} unavailable (size probe)")
    try:
        object_size = int(size_proc.stdout.decode("ascii").strip())
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid approved Git blob size") from exc
    if object_size < 0 or object_size > max_bytes:
        raise RuntimeError(f"approved Git blob {oid} exceeds size limit")
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "--no-pager", "--no-optional-locks", "cat-file", "blob", oid],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=_sanitized_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"approved Git blob {oid} read timed out") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"approved Git blob {oid} unavailable: {proc.stderr[:500]!r}")
    data = proc.stdout
    if len(data) != object_size or len(data) > max_bytes:
        raise RuntimeError(f"approved Git blob {oid} size changed or exceeds limit")
    header = f"blob {len(data)}\0".encode("ascii")
    digest = hashlib.sha1(header + data).hexdigest() if len(oid) == 40 else hashlib.sha256(header + data).hexdigest()
    if digest != oid:
        raise RuntimeError(f"approved Git blob/OID mismatch: {oid}")
    return data


def _e0_stage_task_setup(cfg: dict[str, Any], auth: dict[str, Any], lab: Laboratory, scratch: Path) -> dict[str, Any]:
    """Stage approved-commit Git blobs into an isolated scratch task_setup tree.

    Neither the index nor the working tree can change authorized file bytes.
    The complete pinned-tree inventory is the authorized file set.  Hashes
    of staged output are independently verified and recorded with Git OIDs.
    """
    root = _e0_nrg_root(cfg, lab)
    commit = auth["commit"]
    inventory = _e0_git_objects(root, commit, _E0_TASK_SETUP_REL, _E0_SOURCE_REL)
    prefix = _E0_TASK_SETUP_REL + "/"
    task_files = {p for p in inventory if p.startswith(prefix)}
    required = {f"{prefix}chemical_mechanisms/{rel}" for rel in _E0_SIX_MECHANISMS}
    missing = sorted(required - task_files)
    if missing:
        raise RuntimeError("six-mechanism manifest incomplete in approved commit: " + "; ".join(missing))
    if not any(p.startswith(prefix + "thermophysical_data/") for p in task_files):
        raise RuntimeError("thermophysical_data missing in approved commit")
    staged: dict[str, str] = {}
    records: list[dict[str, str]] = []
    blobs: dict[str, bytes] = {}
    total = 0
    for rel, (mode, oid) in sorted(inventory.items()):
        data = _e0_read_git_blob(root, oid)
        total += len(data)
        if total > 256 * 1024 * 1024:
            raise RuntimeError("approved input tree exceeds 256 MiB staging limit")
        digest = hashlib.sha256(data).hexdigest()
        if rel == _E0_SOURCE_REL:
            if digest != auth["source_sha256"]:
                raise RuntimeError("approved V3 source SHA-256 mismatch")
            if _V3_MARKER.encode("utf-8") not in data:
                raise RuntimeError("approved V3 source marker missing")
        blobs[rel] = data
        records.append({"repository_path": rel, "git_mode": mode, "git_blob_oid": oid,
                        "sha256": digest, "staged_path": rel})
    # Build a deterministic manifest before changing scratch, then stage only
    # authenticated bytes.  All paths in records were validated above.
    manifest_data: dict[str, Any] = {"schema_version": 1, "approved_commit": commit, "files": records}
    manifest_bytes = (json.dumps(manifest_data, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if scratch.exists():
        raise RuntimeError(f"scratch staging root exists: {scratch}")
    scratch.mkdir(parents=True, exist_ok=False)
    for rel, data in sorted(blobs.items()):
        dst = scratch / rel
        _ensure_within(dst, scratch)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        if sha256_file(dst) != digest:
            raise RuntimeError(f"approved Git blob staging mismatch: {rel}")
        staged[rel] = digest
    manifest_path = scratch / "staged_input_manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    if sha256_file(manifest_path) != manifest_sha:
        raise RuntimeError("staged-input manifest write integrity failure")
    return {"staged_files": staged, "scratch": str(scratch),
            "staged_input_manifest": str(manifest_path), "staged_input_manifest_sha256": manifest_sha,
            "approved_commit": commit}


def _e0_exe_path(cfg: dict[str, Any], lab: Laboratory) -> Path:
    return _e0_path(cfg, "build_root", lab) / "bin" / cfg["build_type"].title() / cfg["equilibrium_executable"]


class _E0ReferenceNotConfigured(RuntimeError):
    """The OPTIONAL Cantera reference is not configured.

    This is an optional reference-verification status, not a generic laboratory
    failure, not an NRG build failure, and never a scientific PASS or FAIL of
    NRG itself.  It subclasses RuntimeError so existing fail-closed callers and
    tests keep working."""

    status = _E0_REFERENCE_NOT_CONFIGURED


def _e0_python_launcher_identity(cfg: dict[str, Any], lab: Laboratory) -> dict[str, Any]:
    """Fingerprint a Cantera-capable interpreter WITHOUT losing venv identity.

    The configured value is the *launcher* path (e.g. .venv/bin/python).  That
    path is preserved verbatim as the execution argv[0], because a venv
    launcher is normally a symlink to a base interpreter and canonicalizing it
    to the base interpreter path silently discards the venv (pyvenv.cfg), i.e.
    it would lose the environment Cantera was installed into.  Only the FINAL
    path component may be a symlink; ancestors, '..', and every other symlink
    component are still refused by _reject_raw_symlink_or_dotdot.

    Returns the launcher path, the resolved target, the target binary SHA-256,
    and (when present) the adjacent venv identity: venv root, pyvenv.cfg path,
    and pyvenv.cfg SHA-256.  Does NOT import or execute Cantera.
    """
    raw = _e0_raw_path(cfg, "cantera_python", lab)
    _reject_raw_symlink_or_dotdot(raw, allow_final_symlink=True)
    launcher = Path(os.path.abspath(raw))
    if not launcher.exists():
        raise _E0ReferenceNotConfigured(f"cantera_python missing: {launcher}")
    target = launcher.resolve(strict=True)
    if not target.is_file():
        raise _E0ReferenceNotConfigured(f"cantera_python launcher target is not a regular file: {target}")
    pyvenv_cfg = launcher.parent.parent / "pyvenv.cfg"
    if pyvenv_cfg.is_symlink():
        raise RuntimeError(f"pyvenv.cfg is a symlink (refusing): {pyvenv_cfg}")
    is_venv = pyvenv_cfg.is_file()
    return {
        "cantera_python": str(launcher),
        "cantera_python_resolved": str(target),
        "cantera_python_sha256": sha256_file(target),
        "cantera_python_launcher_is_symlink": launcher.is_symlink(),
        "cantera_venv_root": str(launcher.parent.parent) if is_venv else None,
        "cantera_pyvenv_cfg": str(pyvenv_cfg) if is_venv else None,
        "cantera_pyvenv_cfg_sha256": sha256_file(pyvenv_cfg) if is_venv else None,
        "cantera_is_venv": is_venv,
    }


def _e0_python_identity(cfg: dict[str, Any], lab: Laboratory) -> tuple[Path, str]:
    """Backwards-compatible (launcher_path, binary_sha256) pair.

    The launcher path is returned as-is (see _e0_python_launcher_identity); the
    hash is of the resolved interpreter binary."""
    rec = _e0_python_launcher_identity(cfg, lab)
    return Path(rec["cantera_python"]), rec["cantera_python_sha256"]


def _e0_reference_status(rec: dict[str, Any], probe: bool = False) -> dict[str, Any]:
    """Report the OPTIONAL Cantera reference status for a launcher record.

    Default (probe=False) never imports Cantera: status REFERENCE_UNVERIFIED,
    and the reviewed harness performs its own availability check when it runs.
    probe=True is an explicit, operator-authorized availability probe (import
    only, version/location printed) that yields REFERENCE_AVAILABLE or
    CANTERA_UNAVAILABLE.

    These statuses describe the optional independent reference.  They are never
    generic-laboratory readiness, never NRG build readiness, and never a
    scientific PASS or FAIL of NRG itself.
    """
    out: dict[str, Any] = {
        "optional_reference": True,
        "reference_engine": "cantera",
        "reference_status": _E0_REFERENCE_UNVERIFIED,
        "reference_detail": "availability probe not requested; no Cantera import performed",
        **rec,
    }
    if not probe:
        return out
    if not os.access(rec["cantera_python"], os.X_OK):
        out["reference_status"] = _E0_CANTERA_UNAVAILABLE
        out["reference_detail"] = f"interpreter launcher is not executable: {rec['cantera_python']}"
        return out
    try:
        proc = subprocess.run(
            [rec["cantera_python"], "-c", _E0_CANTERA_PROBE],
            capture_output=True, text=True, timeout=_E0_REFERENCE_PROBE_TIMEOUT_S, env=_sanitized_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        out["reference_status"] = _E0_CANTERA_UNAVAILABLE
        out["reference_detail"] = f"probe failed to start: {type(exc).__name__}: {exc}"
        return out
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        out["reference_status"] = _E0_CANTERA_UNAVAILABLE
        out["reference_detail"] = f"probe exited {proc.returncode}: {err[-1] if err else 'no stderr'}"
        return out
    out["reference_status"] = _E0_REFERENCE_AVAILABLE
    out["cantera_version"] = lines[0] if lines else None
    out["cantera_module"] = lines[1] if len(lines) > 1 else None
    out["reference_detail"] = "Cantera import probe succeeded"
    return out


def _e0_bind_reference_identity(interp: dict[str, Any], args: argparse.Namespace) -> None:
    """Fail closed unless the plan-time optional-reference identity is unchanged.

    Binding covers the launcher path (venv identity), the resolved interpreter
    binary, and - inside a virtual environment - the pyvenv.cfg identity.  A
    changed launcher, binary, or venv identity is refused, and so is a plan that
    promised a venv when the launcher is not inside one."""
    expected_launcher = getattr(args, "expected_cantera_python_launcher", None)
    expected_pyvenv = getattr(args, "expected_cantera_pyvenv_sha256", None)
    if not getattr(args, "expected_cantera_python_sha256", None):
        raise RuntimeError("plan->execute binding requires --expected-cantera-python-sha256")
    if not expected_launcher:
        raise RuntimeError("plan->execute binding requires --expected-cantera-python-launcher (vetted interpreter launcher path)")
    if args.expected_cantera_python_sha256 != interp["cantera_python_sha256"]:
        raise RuntimeError("cantera_python interpreter binary changed since plan")
    if expected_launcher != interp["cantera_python"]:
        raise RuntimeError("cantera_python launcher path changed since plan")
    if interp["cantera_is_venv"]:
        if not expected_pyvenv:
            raise RuntimeError("plan->execute binding requires --expected-cantera-pyvenv-sha256 when the launcher is inside a virtual environment")
        if expected_pyvenv != interp["cantera_pyvenv_cfg_sha256"]:
            raise RuntimeError("cantera virtual-environment identity (pyvenv.cfg) changed since plan")
    elif expected_pyvenv:
        raise RuntimeError("plan promised a virtual environment but the launcher is not inside one")
    return None


def _e0_read_state(case_dir: Path, mode: str) -> list[tuple[str, float]]:
    path = case_dir / f"nrg_{mode}_{mode}_state.csv"
    if not path.is_file():
        raise RuntimeError(f"missing state CSV: {path}")
    rows = _read_csv(path)
    if not rows:
        raise RuntimeError(f"empty state CSV: {path}")
    if "species" not in rows[0] or "mole_fraction" not in rows[0]:
        raise RuntimeError(f"state CSV missing species/mole_fraction columns: {path}")
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    for r in rows:
        name = (r.get("species") or "").strip()
        if not name:
            raise RuntimeError(f"state CSV has empty species name: {path}")
        if name in seen:
            raise RuntimeError(f"state CSV has duplicate species {name!r}: {path}")
        seen.add(name)
        try:
            x = float(r["mole_fraction"])
        except (KeyError, ValueError):
            raise RuntimeError(f"malformed mole_fraction for species {name!r} in {path}")
        if not _isfinite(x):
            raise RuntimeError(f"non-finite mole_fraction for species {name!r} in {path}")
        out.append((name, x))
    return out


def _e0_read_diagnostics(case_dir: Path, mode: str) -> dict[str, Any]:
    path = case_dir / f"nrg_{mode}_{mode}_diagnostics.csv"
    if not path.is_file():
        raise RuntimeError(f"missing diagnostics CSV: {path}")
    rows = _read_csv(path)
    if not rows:
        raise RuntimeError(f"empty diagnostics CSV: {path}")
    if list(rows[0].keys()) != ["key", "value"]:
        raise RuntimeError(f"diagnostics CSV must have exactly columns [key, value]: {path}")
    diag: dict[str, Any] = {}
    for r in rows:
        if set(r.keys()) != {"key", "value"}:
            raise RuntimeError(f"diagnostics CSV has duplicate/unknown columns: {path}")
        k = (r.get("key") or "").strip()
        v = (r.get("value") or "").strip()
        if not k:
            raise RuntimeError(f"malformed diagnostics record (empty key) in {path}")
        if k in diag:
            raise RuntimeError(f"duplicate diagnostics key {k!r} in {path}")
        if k == "converged":
            diag[k] = v
            continue
        try:
            val = float(v)
        except ValueError:
            raise RuntimeError(f"malformed numeric value for key {k!r} in {path}")
        if not _isfinite(val):
            raise RuntimeError(f"non-finite diagnostics value for {k!r} in {path}")
        diag[k] = val
    missing = [k for k in _E0_DIAGNOSTICS_KEYS if k not in diag]
    if missing:
        raise RuntimeError(f"diagnostics CSV missing required keys {sorted(missing)}: {path}")
    for k in _E0_RESIDUAL_KEYS:
        if k in diag and float(diag[k]) < 0.0:
            raise RuntimeError(f"negative {k} {diag[k]} in {path}")
    if float(diag["temperature_K"]) <= 0.0:
        raise RuntimeError(f"nonphysical temperature_K {diag['temperature_K']} in {path}")
    if float(diag["pressure_Pa"]) <= 0.0:
        raise RuntimeError(f"nonphysical pressure_Pa {diag['pressure_Pa']} in {path}")
    return diag


def _e0_independent_checks(result: dict[str, Any], acceptance: dict[str, float], case_dir: Path) -> list[str]:
    """Independent acceptance on raw archived CSVs, decoupled from the harness
    screening verdict.  Acceptance keys are guaranteed present and validated by
    _e0_authorization; no default threshold is inserted here.

    Evidence classes (R5):
      * RECOMPUTED -- mole-fraction normalization and nonnegativity are recomputed
        here from the raw state CSV (genuinely independent of NRG's own report).
      * INSPECTED -- element/mass/enthalpy conservation residuals and temperature/
        pressure are read back from NRG-generated diagnostics and cross-checked
        against result.json.  These are NOT an independent conservation re-
        derivation, only an inspection of NRG's reported residuals.

    Mandatory finite JSON checks (R4): nrg_temperature_K, nrg_diagnostics (each
    expected numeric residual), and the full comparison error fields (converged,
    max_absolute_mole_fraction_error, temperature_error_K) are all required;
    missing/NaN/Inf/wrong-type/mismatch/nonphysical-sign fail closed.
    """
    issues: list[str] = []
    if result.get("validation_mode") != "full":
        return ["not full validation mode"]
    eq = result.get("comparisons", {}).get("equilibrium", {})
    if not isinstance(eq, dict) or not eq:
        return ["missing comparisons.equilibrium in result.json"]
    tp_species = {s for s, _ in _e0_read_state(case_dir, "tp")}
    hp_species = {s for s, _ in _e0_read_state(case_dir, "hp")}
    if tp_species != hp_species:
        issues.append("TP/HP species set mismatch")
    for mode in ("tp", "hp"):
        m = eq.get(mode, {})
        if not isinstance(m, dict):
            issues.append(f"{mode}: missing comparison block in result.json")
            continue
        state = _e0_read_state(case_dir, mode)
        diag = _e0_read_diagnostics(case_dir, mode)
        conv = str(diag.get("converged", "F")).strip().upper() in ("T", "TRUE", ".TRUE.")
        if not conv or not m.get("converged"):
            issues.append(f"{mode}: did not converge")
            continue
        xs = [x for _, x in state]
        s = sum(xs)
        # RECOMPUTED (R5): normalization + nonnegativity from the raw state CSV.
        if abs(s - 1.0) > acceptance["normalization_tolerance"]:
            issues.append(f"{mode}: mole fractions not normalized (sum={s:.3e}) [recomputed from state CSV]")
        if min(xs) < -acceptance["nonnegativity_tolerance"]:
            issues.append(f"{mode}: negative mole fraction {min(xs):.3e} [recomputed from state CSV]")
        # INSPECTED (R5): NRG-reported residuals read back, NOT recomputed.
        for rk, tol_key in _E0_RESIDUAL_TOL.items():
            if float(diag[rk]) > acceptance[tol_key]:
                issues.append(f"{mode}: NRG-reported {rk} {diag[rk]:.3e} exceeds tolerance [inspected, not recomputed]")
        # Mandatory nrg_temperature_K (R4): finite and positive.
        nrg_T = m.get("nrg_temperature_K")
        if nrg_T is None:
            issues.append(f"{mode}: missing nrg_temperature_K in result.json")
            continue
        try:
            fT = float(nrg_T)
        except (TypeError, ValueError):
            issues.append(f"{mode}: non-numeric nrg_temperature_K in result.json")
            continue
        if not _isfinite(fT) or fT <= 0.0:
            issues.append(f"{mode}: nrg_temperature_K must be finite and positive (got {nrg_T})")
            continue
        if abs(float(diag["temperature_K"]) - fT) > max(1e-12, abs(fT) * 1e-9):
            issues.append(f"{mode}: temperature_K CSV/result.json mismatch ({diag['temperature_K']} vs {fT})")
        # Fixed 1-atm pilot constraint.
        if abs(float(diag["pressure_Pa"]) - _E0_EXPECTED_PRESSURE_PA) > _E0_EXPECTED_PRESSURE_PA * 1e-9:
            issues.append(f"{mode}: unexpected pressure {diag['pressure_Pa']} (expected {_E0_EXPECTED_PRESSURE_PA})")
        # Mandatory nrg_diagnostics (R4): require the dict and each expected
        # numeric residual, finite and nonnegative, cross-checked against the CSV.
        nrg_diag = m.get("nrg_diagnostics")
        if not isinstance(nrg_diag, dict):
            issues.append(f"{mode}: missing/invalid nrg_diagnostics in result.json")
            continue
        for rk in _E0_RESIDUAL_KEYS:
            dv = nrg_diag.get(rk)
            if dv is None:
                issues.append(f"{mode}: missing nrg_diagnostics.{rk} in result.json")
                continue
            try:
                fdv = float(dv)
            except (TypeError, ValueError):
                issues.append(f"{mode}: non-numeric nrg_diagnostics.{rk} in result.json")
                continue
            if not _isfinite(fdv) or fdv < 0.0:
                issues.append(f"{mode}: nrg_diagnostics.{rk} must be finite and nonnegative (got {dv})")
                continue
            if abs(float(diag[rk]) - fdv) > max(1e-15, abs(fdv) * 1e-9):
                issues.append(f"{mode}: diagnostics {rk} CSV/result.json mismatch ({diag[rk]:.3e} vs {fdv:.3e})")
        # Mandatory full comparison error fields (R4).
        for key, tol_key, label in (
            ("max_absolute_mole_fraction_error", "mole_fraction_tolerance", "mole-fraction delta vs Cantera"),
            ("temperature_error_K", "temperature_tolerance_K", "temperature delta vs Cantera"),
        ):
            v = m.get(key)
            if v is None:
                issues.append(f"{mode}: missing {key} in result.json")
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                issues.append(f"{mode}: non-numeric {key} in result.json")
                continue
            if not _isfinite(fv):
                issues.append(f"{mode}: {key} must be finite (NaN/Inf rejected)")
                continue
            if fv < 0.0:
                issues.append(f"{mode}: {key} must be nonnegative (got {fv})")
                continue
            if fv > acceptance[tol_key]:
                issues.append(f"{mode}: {label} exceeds tolerance ({fv:.3e} > {acceptance[tol_key]:.3e})")
    return issues


def _e0_parse_results(output_root: Path, selector: str) -> dict[str, Any]:
    summary_path = output_root / "summary.json"
    if not summary_path.is_file():
        raise RuntimeError(f"missing summary.json: {summary_path}")
    summary = read_json(summary_path)
    if not isinstance(summary, list) or not summary:
        raise RuntimeError("summary.json is empty or malformed")
    non_dict = [i for i, r in enumerate(summary) if not isinstance(r, dict)]
    if non_dict:
        raise RuntimeError(f"summary.json contains non-dictionary records at indices {non_dict}")
    matching = [r for r in summary if r.get("mechanism") == selector]
    if not matching:
        raise RuntimeError(f"selected mechanism {selector} missing from summary.json")
    if len(matching) > 1:
        raise RuntimeError(f"summary.json has {len(matching)} duplicate records for {selector} (expected exactly one)")
    extra = sorted({r.get("mechanism") for r in summary if r.get("mechanism") != selector})
    if extra:
        raise RuntimeError(f"summary.json contains unexpected mechanism records {extra} (expected only {selector})")
    target = matching[0]
    mech_stem = Path(selector).stem
    case_dirs = sorted(
        d for d in output_root.iterdir()
        if d.is_dir() and d.name.endswith(f"_{mech_stem}") and (d / "result.json").is_file()
    )
    if not case_dirs:
        raise RuntimeError(f"case result.json missing for {selector}")
    if len(case_dirs) > 1:
        raise RuntimeError(f"multiple case directories for {selector}: {[d.name for d in case_dirs]}")
    case_dir = case_dirs[0]
    result = read_json(case_dir / "result.json")
    if not isinstance(result, dict):
        raise RuntimeError(f"case result.json malformed for {selector}")
    # Reject summary/case status and mechanism mismatch (correction 7).
    if target.get("status") != result.get("status"):
        raise RuntimeError(f"summary/case status mismatch for {selector}: {target.get('status')} vs {result.get('status')}")
    if result.get("mechanism") not in (None, selector):
        raise RuntimeError(f"case result.json mechanism mismatch for {selector}: {result.get('mechanism')}")
    # Complete TP/HP results required (validation-config/data-completeness round).
    eq = result.get("comparisons", {}).get("equilibrium", {})
    for mode in ("tp", "hp"):
        if not isinstance(eq.get(mode), dict):
            raise RuntimeError(f"case result.json missing equilibrium.{mode} comparison block for {selector}")
    return {"summary_entry": target, "result": result, "case_dir": case_dir}


def _e0_verify_build_identity(cfg: dict[str, Any], auth: dict[str, Any], lab: Laboratory, preflight: dict[str, Any], exe_path: Path, exe_sha: str) -> dict[str, Any]:
    """Correction 1: reject a build-manifest/binary pair that is self-consistent
    but not associated with the researcher-approved source."""
    manifest_path = _e0_path(cfg, "build_root", lab) / "build_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"build_manifest.json missing: {manifest_path}")
    manifest = read_json(manifest_path)
    checks = (
        ("pinned_commit", manifest.get("pinned_commit"), auth["commit"]),
        ("pinned_source_sha256", manifest.get("pinned_source_sha256"), preflight["source_sha256"]),
        ("cmake_selector", manifest.get("cmake_selector"), _E0_CMAKE_SELECTOR),
        ("cmake_target", manifest.get("cmake_target"), _E0_CMAKE_TARGET),
        ("source_relative", manifest.get("source_relative"), _E0_SOURCE_REL),
        ("build_type", manifest.get("build_type"), cfg["build_type"]),
        ("executable_sha256", manifest.get("executable_sha256"), exe_sha),
    )
    for key, got, want in checks:
        if got != want:
            raise RuntimeError(f"build_manifest.json {key}={got!r} != approved {want!r}; manifest/binary not associated with approved source.")
    recorded_path = manifest.get("executable_path")
    if recorded_path is not None and Path(recorded_path).resolve() != exe_path.resolve():
        raise RuntimeError(f"build_manifest.json executable_path {recorded_path} != pinned executable {exe_path}")
    return manifest


def _e0_validation_config(harness_path: Path, auth: dict[str, Any]) -> Path:
    """Resolve and authenticate validation_config.json (validation-config pin).

    The harness (tools/run_validation.py) reads its validation_config.json one
    directory above the tools/ directory (harness_path.parent.parent).  That
    accepted location is fixed by _E0_CONFIG_REL, and the file's SHA-256 is
    pinned in the protected authorization record (validation_config_sha256).
    Both are verified here so a stray, tampered, symlinked, or hash-mismatched
    config cannot steer the run.  Reject absent/malformed/mismatch and return
    the canonical resolved path."""
    config_path = harness_path.parent.parent / _E0_CONFIG_REL
    if config_path.is_symlink():
        raise RuntimeError(f"validation_config.json is a symlink (refusing): {config_path}")
    if not config_path.is_file():
        raise RuntimeError(f"validation_config.json missing at accepted location: {config_path}")
    if sha256_file(config_path) != auth["validation_config_sha256"]:
        raise RuntimeError("validation_config.json SHA-256 does not match authorized validation_config_sha256; source review required.")
    return config_path


def _e0_validation_config_snapshot(harness_path: Path, auth: dict[str, Any], scratch: Path) -> Path:
    """Run-owned, hash-verified snapshot of validation_config.json (correction 2).

    The harness reads a COPY staged under the run-owned scratch root, not the
    live file, so a config swapped after the plan/execute hash check cannot steer
    the run.  The source is authenticated against the protected pin, and the
    snapshot is re-hashed to confirm a byte-identical write."""
    src = _e0_validation_config(harness_path, auth)
    snap = scratch / "validation_config.snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(src.read_bytes())
    if sha256_file(snap) != auth["validation_config_sha256"]:
        raise RuntimeError("validation_config.json snapshot hash mismatch (write integrity failed)")
    return snap


def cmd_e0_build_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    try:
        cfg = _e0_config(lab, operation="build")
        auth = _e0_authorization(lab)
        pre = _e0_checkout_preflight(cfg, auth, lab)
        _e0_boundaries(cfg, lab, require_run_root=False)
        compiler, version = _e0_discover_fortran()
        cmake_ver = subprocess.run(["cmake", "--version"], capture_output=True, text=True, env=_sanitized_env()).stdout.strip().splitlines()
        return emit({
            "can_build": True,
            # The NRG build never queries, hashes, or imports a Cantera
            # environment: Cantera is an optional independent reference used
            # only by the separate equilibrium-validation command.
            "cantera_required": False,
            "reference_dependency": "not_required_for_build",
            "commit": pre["commit"],
            "source_sha256": pre["source_sha256"],
            "source_path": pre["source_path"],
            "fortran_compiler": compiler,
            "fortran_version": version,
            "cmake_version": cmake_ver[0] if cmake_ver else "unknown",
            "build_type": cfg["build_type"],
            "build_root": str(_e0_path(cfg, "build_root", lab)),
            "nrg_source_root": str(_e0_nrg_root(cfg, lab)),
            "cmake_selector": _E0_CMAKE_SELECTOR,
            "cmake_target": _E0_CMAKE_TARGET,
            "executable_name": cfg["equilibrium_executable"],
        })
    except Exception as exc:
        return emit({"error": f"{type(exc).__name__}: {exc}", "can_build": False}, 2)


def cmd_e0_build_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    try:
        cfg = _e0_config(lab, operation="build")
        auth = _e0_authorization(lab)
        pre = _e0_checkout_preflight(cfg, auth, lab)
        _e0_boundaries(cfg, lab, require_run_root=False)
        # Mandatory plan->execute binding (correction 6): identity fields are
        # required, not optional.
        if not args.expected_source_sha256 or not args.expected_commit:
            raise RuntimeError("plan->execute binding requires --expected-source-sha256 and --expected-commit; refusing to build without plan identity.")
        if args.expected_source_sha256 != pre["source_sha256"]:
            raise RuntimeError("source SHA-256 changed since plan; researcher review required.")
        if args.expected_commit != pre["commit"]:
            raise RuntimeError("checkout commit changed since plan; researcher review required.")
        compiler, cver = _e0_discover_fortran()
        root = _e0_nrg_root(cfg, lab)
        build_root = _e0_path(cfg, "build_root", lab)
        if build_root.exists() and any(build_root.iterdir()):
            raise RuntimeError(f"build_root already exists and is nonempty: {build_root}")
        build_root.mkdir(parents=True, exist_ok=True)
        jobs = int(cfg["parallel_jobs"])
        deadline = _e0_deadline(cfg)
        build_type = cfg["build_type"]
        cmake_ver = subprocess.run(["cmake", "--version"], capture_output=True, text=True, env=_sanitized_env()).stdout.strip().splitlines()
        configure_cmd = [
            "cmake", "-S", str(root), "-B", str(build_root),
            f"-DPACKAGE_INTERFACE_SOURCE={_E0_CMAKE_SELECTOR}",
            f"-DCMAKE_BUILD_TYPE={build_type.title()}",
            f"-DCMAKE_Fortran_COMPILER={compiler}",
        ]
        _run_bounded(configure_cmd, "cmake configure", _e0_remaining(deadline), build_root / "cmake_configure.log")
        build_cmd = ["cmake", "--build", str(build_root), "--target", _E0_CMAKE_TARGET, "--parallel", str(jobs)]
        _run_bounded(build_cmd, "cmake build", _e0_remaining(deadline), build_root / "cmake_build.log")
        exe_path = _e0_exe_path(cfg, lab)
        if not exe_path.is_file():
            raise RuntimeError(f"build produced no executable at {exe_path}")
        exe_sha = sha256_file(exe_path)
        manifest = {
            "pinned_commit": auth["commit"],
            "pinned_source_sha256": pre["source_sha256"],
            "source_relative": _E0_SOURCE_REL,
            "cmake_selector": _E0_CMAKE_SELECTOR,
            "cmake_target": _E0_CMAKE_TARGET,
            "configure_command": configure_cmd,
            "configure_log": str(build_root / "cmake_configure.log"),
            "build_log": str(build_root / "cmake_build.log"),
            "executable_sha256": exe_sha,
            "executable_path": str(exe_path),
            "build_type": build_type,
            "fortran_compiler": compiler,
            "fortran_version": cver,
            "cmake_version": cmake_ver[0] if cmake_ver else "unknown",
            "built_at": utc_now(),
        }
        write_json(build_root / "build_manifest.json", manifest)
        return emit({"built": True, "executable_sha256": exe_sha, "build_manifest": manifest, "build_root": str(build_root)})
    except Exception as exc:
        return emit({"error": f"{type(exc).__name__}: {exc}", "built": False}, 2)


def cmd_e0_validate_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    try:
        cfg = _e0_config(lab, operation="validation")
        auth = _e0_authorization(lab)
        _e0_boundaries(cfg, lab)
        pre = _e0_checkout_preflight(cfg, auth, lab)
        harness_path = _e0_path(cfg, "harness", lab)
        if not harness_path.is_file():
            raise RuntimeError(f"harness missing: {harness_path}")
        harness_sha = sha256_file(harness_path)
        if harness_sha != auth["harness_sha256"]:
            raise RuntimeError("harness SHA-256 does not match authorized harness_sha256 (post-patch hash); source review required.")
        exe_path = _e0_exe_path(cfg, lab)
        if not exe_path.is_file():
            raise RuntimeError(f"equilibrium executable missing: {exe_path}")
        exe_sha = sha256_file(exe_path)
        _e0_verify_build_identity(cfg, auth, lab, pre, exe_path, exe_sha)
        _e0_validation_config(harness_path, auth)
        interp = _e0_python_launcher_identity(cfg, lab)
        # Optional independent reference: the probe is opt-in and performed only
        # when explicitly requested, so planning never imports Cantera.
        ref = _e0_reference_status(interp, probe=bool(getattr(args, "probe_cantera_reference", False)))
        if ref["reference_status"] == _E0_CANTERA_UNAVAILABLE:
            return emit({
                "can_run": False,
                "generic_lab_ready": True,
                "scientific_pass": False,
                "verification_mode": "full",
                "reference_required_for_this_command": True,
                "error": f"{_E0_CANTERA_UNAVAILABLE}: {ref['reference_detail']}",
                **ref,
            }, 3)
        return emit({
            "can_run": True,
            "verification_mode": "full",
            "reference_required_for_this_command": True,
            "mechanism_selector": _E0_MECHANISM_SELECTOR,
            "commit": pre["commit"],
            "source_sha256": pre["source_sha256"],
            "executable_sha256": exe_sha,
            "harness_sha256": harness_sha,
            "validation_config_sha256": auth["validation_config_sha256"],
            "cantera_python": interp["cantera_python"],
            "cantera_python_resolved": interp["cantera_python_resolved"],
            "cantera_python_sha256": interp["cantera_python_sha256"],
            "cantera_is_venv": interp["cantera_is_venv"],
            "cantera_venv_root": interp["cantera_venv_root"],
            "cantera_pyvenv_cfg_sha256": interp["cantera_pyvenv_cfg_sha256"],
            "output_root": str(_e0_path(cfg, "run_root", lab) / "output"),
            **ref,
        })
    except _E0ReferenceNotConfigured as exc:
        return emit({
            "can_run": False,
            "generic_lab_ready": True,
            "scientific_pass": False,
            "verification_mode": "full",
            "reference_required_for_this_command": True,
            "optional_reference": True,
            "reference_engine": "cantera",
            "reference_status": _E0_REFERENCE_NOT_CONFIGURED,
            "reference_detail": str(exc),
            "error": f"{_E0_REFERENCE_NOT_CONFIGURED}: {type(exc).__name__}: {exc}",
        }, 3)
    except Exception as exc:
        return emit({"error": f"{type(exc).__name__}: {exc}", "can_run": False}, 2)


def cmd_e0_validate_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    try:
        cfg = _e0_config(lab, operation="validation")
        auth = _e0_authorization(lab)
        selector = _E0_MECHANISM_SELECTOR
        _e0_boundaries(cfg, lab)
        pre = _e0_checkout_preflight(cfg, auth, lab)
        exe_path = _e0_exe_path(cfg, lab)
        if not exe_path.is_file():
            raise RuntimeError(f"equilibrium executable missing: {exe_path}")
        exe_sha = sha256_file(exe_path)
        harness_path = _e0_path(cfg, "harness", lab)
        harness_sha = sha256_file(harness_path)
        if harness_sha != auth["harness_sha256"]:
            raise RuntimeError("harness SHA-256 does not match authorized harness_sha256 (post-patch hash)")
        # Mandatory plan->execute binding (correction 6).
        if not args.expected_executable_sha256 or not args.expected_harness_sha256:
            raise RuntimeError("plan->execute binding requires --expected-executable-sha256 and --expected-harness-sha256.")
        if args.expected_executable_sha256 != exe_sha:
            raise RuntimeError("executable SHA-256 changed since plan")
        if args.expected_harness_sha256 != harness_sha:
            raise RuntimeError("harness SHA-256 changed since plan")
        _e0_verify_build_identity(cfg, auth, lab, pre, exe_path, exe_sha)
        # Correction 2: bind validation_config plan->execute, then run from a
        # run-owned snapshot (not the live file) so a config swapped after this
        # check cannot steer the run.
        if not args.expected_validation_config_sha256:
            raise RuntimeError("plan->execute binding requires --expected-validation-config-sha256")
        if args.expected_validation_config_sha256 != auth["validation_config_sha256"]:
            raise RuntimeError("validation_config SHA-256 changed since plan")
        # Correction 3 + optional-reference contract: bind the interpreter
        # LAUNCHER path (so a venv keeps its identity and site-packages), the
        # resolved interpreter binary, and the venv pyvenv.cfg identity.
        interp = _e0_python_launcher_identity(cfg, lab)
        _e0_bind_reference_identity(interp, args)
        python = Path(interp["cantera_python"])
        python_sha = interp["cantera_python_sha256"]
        deadline = _e0_deadline(cfg)
        run_root = _e0_path(cfg, "run_root", lab)
        output_root = run_root / "output"
        scratch = run_root / "scratch"
        if output_root.exists() and any(output_root.iterdir()):
            raise RuntimeError(f"output root already exists and is nonempty: {output_root}")
        if scratch.exists():
            raise RuntimeError(f"scratch already exists: {scratch}")
        staged = _e0_stage_task_setup(cfg, auth, lab, scratch)
        config_snapshot = _e0_validation_config_snapshot(harness_path, auth, scratch)
        cmd = [
            str(python), str(harness_path),
            "--nrg", str(scratch),
            "--nrg-binary", str(exe_path),
            "--config", str(config_snapshot),
            "--output", str(output_root),
            "--workdir", str(scratch / "package_interface"),
            "--build-manifest", str(_e0_path(cfg, "build_root", lab) / "build_manifest.json"),
            "--only", selector,
            "--mode", "full",
        ]
        _run_bounded(cmd, "equilibrium validation harness", _e0_remaining(deadline), output_root / "harness_run.log")
        parsed = _e0_parse_results(output_root, selector)
        rec = parsed["result"]
        if rec.get("status") != "PASS":
            raise RuntimeError(f"harness status {rec.get('status')} != PASS: {rec.get('reason')}")
        acceptance = auth["acceptance"]
        issues = _e0_independent_checks(rec, acceptance, parsed["case_dir"])
        if issues:
            raise RuntimeError("independent acceptance checks failed: " + "; ".join(issues))
        return emit({
            "scientific_pass": True,
            "verification_mode": "full",
            "reference_engine": "cantera",
            "reference_required": True,
            "mechanism": selector,
            "harness_status": rec.get("status"),
            "independent_checks": "passed",
            "acceptance_thresholds": acceptance,
            "validation_config_sha256": auth["validation_config_sha256"],
            "cantera_python": str(python),
            "cantera_python_resolved": interp["cantera_python_resolved"],
            "cantera_python_sha256": python_sha,
            "cantera_is_venv": interp["cantera_is_venv"],
            "cantera_venv_root": interp["cantera_venv_root"],
            "cantera_pyvenv_cfg_sha256": interp["cantera_pyvenv_cfg_sha256"],
            "equilibrium": rec.get("comparisons", {}).get("equilibrium", {}),
            "staged_files": len(staged.get("staged_files", {})),
            "staged_input_manifest_sha256": staged["staged_input_manifest_sha256"],
            "staged_input_manifest": staged["staged_input_manifest"],
            "staged_commit": staged["approved_commit"],
            "output_root": str(output_root),
        })
    except _E0ReferenceNotConfigured as exc:
        return emit({
            "scientific_pass": False,
            "optional_reference": True,
            "reference_engine": "cantera",
            "reference_status": _E0_REFERENCE_NOT_CONFIGURED,
            "error": f"{_E0_REFERENCE_NOT_CONFIGURED}: {type(exc).__name__}: {exc}",
        }, 3)
    except Exception as exc:
        return emit({"error": f"{type(exc).__name__}: {exc}", "scientific_pass": False}, 2)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--laboratory", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("lab-info")
    sub.add_parser("campaign-list")
    sub.add_parser("study-list")

    s = sub.add_parser("campaign-status")
    s.add_argument("--cases", required=True)
    s.add_argument("--run-config")
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("case-inspect")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", required=True)

    s = sub.add_parser("campaign-identity-inspect")
    s.add_argument("--cases", required=True)

    s = sub.add_parser("campaign-reset-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", action="append", required=True)
    s.add_argument("--override", action="append", default=[])
    s.add_argument("--reason", default=None)

    s = sub.add_parser("campaign-reset-execute")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", action="append", required=True)
    s.add_argument("--override", action="append", default=[])
    s.add_argument("--reason", default=None)

    s = sub.add_parser("campaign-append-preview")
    s.add_argument("--cases", required=True)
    s.add_argument("--campaign", required=True)

    s = sub.add_parser("campaign-append-execute")
    s.add_argument("--cases", required=True)
    s.add_argument("--campaign", required=True)

    s = sub.add_parser("extension-preview")
    s.add_argument("--extension", required=True)

    s = sub.add_parser("extension-generate")
    s.add_argument("--extension", required=True)
    s.add_argument("--overwrite", action="store_true")

    s = sub.add_parser("composite-create")
    s.add_argument("--base-cases", required=True)
    s.add_argument("--extension-cases", required=True)
    s.add_argument("--output-name", required=True)
    s.add_argument("--overwrite", action="store_true")

    s = sub.add_parser("campaign-preview")
    s.add_argument("--campaign", required=True)
    s.add_argument("--limit", type=int, default=12)

    s = sub.add_parser("campaign-generate")
    s.add_argument("--campaign", required=True)
    s.add_argument("--run-interface", action="store_true")
    s.add_argument("--overwrite", action="store_true")

    s = sub.add_parser("campaign-prepare")
    s.add_argument("--cases", required=True)

    s = sub.add_parser("campaign-start")
    s.add_argument("--cases", required=True)
    s.add_argument("--run-config")  # private/manual override; not exposed to Pi
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("campaign-execution-summary")
    s.add_argument("--cases", required=True)

    s = sub.add_parser("campaign-physical-audit")
    s.add_argument("--cases", required=True)
    s.add_argument("--profile", required=True)

    s = sub.add_parser("campaign-physical-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--profile", required=True)

    s = sub.add_parser("campaign-physical-start")
    s.add_argument("--cases", required=True)
    s.add_argument("--profile", required=True)

    s = sub.add_parser("campaign-physical-cases-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--profile", required=True)
    s.add_argument("--case-id", action="append", required=True)

    s = sub.add_parser("campaign-physical-cases-start")
    s.add_argument("--cases", required=True)
    s.add_argument("--profile", required=True)
    s.add_argument("--case-id", action="append", required=True)

    s = sub.add_parser("campaign-rerun-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", action="append", required=True)

    s = sub.add_parser("campaign-rerun-start")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", action="append", required=True)

    s = sub.add_parser("campaign-job-status")
    s.add_argument("--job", required=True)
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("campaign-stop-plan")
    s.add_argument("--job")
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("campaign-stop-execute")
    s.add_argument("--job")
    s.add_argument("--reason")
    s.add_argument("--wait-seconds", type=float, default=45.0)
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("campaign-export-bundle-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--output")

    s = sub.add_parser("campaign-export-bundle-execute")
    s.add_argument("--cases", required=True)
    s.add_argument("--output")

    s = sub.add_parser("campaign-import-results-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--results", required=True)

    s = sub.add_parser("campaign-import-results-execute")
    s.add_argument("--cases", required=True)
    s.add_argument("--results", required=True)

    s = sub.add_parser("campaign-pause-plan")
    s.add_argument("--job")
    s.add_argument("--mode", choices=("now", "after_current"), required=True)
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("campaign-pause-execute")
    s.add_argument("--job")
    s.add_argument("--mode", choices=("now", "after_current"), required=True)
    s.add_argument("--reason")
    s.add_argument("--wait-seconds", type=float, default=45.0)
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("case-stop-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", required=True)
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("case-stop-execute")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", required=True)
    s.add_argument("--reason")
    s.add_argument("--wait-seconds", type=float, default=45.0)
    s.add_argument("--full", action="store_true", help="return exhaustive diagnostic/provenance payload")

    s = sub.add_parser("create-study")
    s.add_argument("--slug", required=True)
    s.add_argument("--request", required=True)
    s.add_argument("--cases", required=True)
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("study-pilot-plan")
    s.add_argument("--study", required=True)
    s.add_argument("--max-cases", type=int, default=20)

    s = sub.add_parser("run-study-pilot")
    s.add_argument("--study", required=True)
    s.add_argument("--case-id", action="append", default=[], required=True)

    s = sub.add_parser("run-study")
    s.add_argument("--study", required=True)

    s = sub.add_parser("study-summary")
    s.add_argument("--study", required=True)
    s = sub.add_parser("dev-transaction-plan")
    s.add_argument("--task", required=True)
    s.add_argument("--rationale", required=True)
    s.add_argument("--allowed-path", action="append", required=True)

    s = sub.add_parser("dev-transaction-start")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--rationale", required=True)
    s.add_argument("--allowed-path", action="append", required=True)
    s.add_argument("--baseline-sha", required=True)

    s = sub.add_parser("dev-transaction-status")
    s.add_argument("--transaction-id", required=True)

    s = sub.add_parser("dev-transaction-list")

    s = sub.add_parser("dev-read")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--offset", type=int, default=1)
    s.add_argument("--limit", type=int, default=2000)

    s = sub.add_parser("dev-apply-patch-plan")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--patch", required=True)

    s = sub.add_parser("dev-apply-patch-execute")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--patch", required=True)
    s.add_argument("--expected-patch-sha256", required=True)
    s.add_argument("--expected-head-sha", default=None)
    s.add_argument("--expected-candidate-tree-sha", default=None)

    s = sub.add_parser("dev-write-plan")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--mode", required=True)
    s.add_argument("--content", required=True)

    s = sub.add_parser("dev-write-execute")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--mode", required=True)
    s.add_argument("--content", required=True)
    s.add_argument("--expected-semantic-spec-sha256", required=True)
    s.add_argument("--expected-head-sha", default=None)
    s.add_argument("--expected-candidate-tree-sha", default=None)
    s.add_argument("--expected-branch", default=None)
    s.add_argument("--expected-target-sha256", default=None)

    s = sub.add_parser("dev-edit-plan")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--old-text", required=True)
    s.add_argument("--new-text", required=True)
    s.add_argument("--expected-occurrences", type=int, required=True)

    s = sub.add_parser("dev-edit-execute")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--old-text", required=True)
    s.add_argument("--new-text", required=True)
    s.add_argument("--expected-occurrences", type=int, required=True)
    s.add_argument("--expected-semantic-spec-sha256", required=True)
    s.add_argument("--expected-head-sha", default=None)
    s.add_argument("--expected-candidate-tree-sha", default=None)
    s.add_argument("--expected-branch", default=None)
    s.add_argument("--expected-target-sha256", default=None)

    s = sub.add_parser("dev-diff")
    s.add_argument("--transaction-id", required=True)

    s = sub.add_parser("dev-test-plan")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--profile", required=True)
    s.add_argument("--test-file", action="append", default=[])
    s.add_argument("--require-nrg", action="store_true")

    s = sub.add_parser("dev-test-execute")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--profile", required=True)
    s.add_argument("--test-file", action="append", default=[])
    s.add_argument("--require-nrg", action="store_true")
    s.add_argument("--expected-candidate-tree-sha", default=None)
    s.add_argument("--expected-head-sha", default=None)
    s.add_argument("--expected-branch", default=None)

    s = sub.add_parser("dev-commit-plan")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--message", required=True)

    s = sub.add_parser("dev-commit-execute")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--expected-candidate-tree-sha", default=None)
    s.add_argument("--expected-head-sha", default=None)
    s.add_argument("--expected-branch", default=None)

    s = sub.add_parser("dev-discard-plan")
    s.add_argument("--transaction-id", required=True)

    s = sub.add_parser("dev-discard-execute")
    s.add_argument("--transaction-id", required=True)
    s.add_argument("--expected-head-sha", default=None)
    s.add_argument("--expected-candidate-tree-sha", default=None)
    s.add_argument("--expected-branch", default=None)
    s.add_argument("--expected-staged-path", action="append", default=[])
    s.add_argument("--expected-unexpected-change", action="append", default=[])

    s = sub.add_parser("e0-build-plan")

    s = sub.add_parser("e0-build-execute")
    s.add_argument("--expected-source-sha256", default=None)
    s.add_argument("--expected-commit", default=None)

    s = sub.add_parser("e0-validate-plan")
    # Opt-in availability probe only: importing Cantera requires explicit
    # operator authorization, so planning never imports it by default.
    s.add_argument("--probe-cantera-reference", action="store_true")

    s = sub.add_parser("e0-validate-execute")
    s.add_argument("--expected-cantera-python-launcher", default=None)
    s.add_argument("--expected-cantera-pyvenv-sha256", default=None)
    s.add_argument("--expected-executable-sha256", default=None)
    s.add_argument("--expected-harness-sha256", default=None)
    s.add_argument("--expected-validation-config-sha256", default=None)
    s.add_argument("--expected-cantera-python-sha256", default=None)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        lab = Laboratory.load(args.laboratory, require_runtime=False)
        commands = {
            "lab-info": cmd_lab_info,
            "campaign-list": cmd_campaign_list,
            "study-list": cmd_study_list,
            "campaign-status": cmd_campaign_status,
            "campaign-identity-inspect": cmd_campaign_identity_inspect,
            "campaign-reset-plan": cmd_campaign_reset_plan,
            "campaign-reset-execute": cmd_campaign_reset_execute,
            "campaign-append-preview": cmd_campaign_append_preview,
            "campaign-append-execute": cmd_campaign_append_execute,
            "extension-preview": cmd_extension_preview,
            "extension-generate": cmd_extension_generate,
            "composite-create": cmd_composite_create,
            "case-inspect": cmd_case_inspect,
            "campaign-preview": cmd_campaign_preview,
            "campaign-generate": cmd_campaign_generate,
            "campaign-prepare": cmd_campaign_prepare,
            "campaign-start": cmd_campaign_start,
            "campaign-execution-summary": cmd_campaign_execution_summary,
            "campaign-physical-audit": cmd_campaign_physical_audit,
            "campaign-physical-plan": cmd_campaign_physical_plan,
            "campaign-physical-start": cmd_campaign_physical_start,
            "campaign-physical-cases-plan": cmd_campaign_physical_cases_plan,
            "campaign-physical-cases-start": cmd_campaign_physical_cases_start,
            "campaign-rerun-plan": cmd_campaign_rerun_plan,
            "campaign-rerun-start": cmd_campaign_rerun_start,
            "campaign-job-status": cmd_campaign_job_status,
            "campaign-stop-plan": cmd_campaign_stop_plan,
            "campaign-stop-execute": cmd_campaign_stop_execute,
            "campaign-export-bundle-plan": cmd_campaign_export_bundle_plan,
            "campaign-export-bundle-execute": cmd_campaign_export_bundle_execute,
            "campaign-import-results-plan": cmd_campaign_import_results_plan,
            "campaign-import-results-execute": cmd_campaign_import_results_execute,
            "campaign-pause-plan": cmd_campaign_pause_plan,
            "campaign-pause-execute": cmd_campaign_pause_execute,
            "case-stop-plan": cmd_case_stop_plan,
            "case-stop-execute": cmd_case_stop_execute,
            "create-study": cmd_create_study,
            "study-pilot-plan": cmd_study_pilot_plan,
            "run-study-pilot": cmd_run_study_pilot,
            "run-study": cmd_run_study,
            "study-summary": cmd_study_summary,
            "dev-transaction-plan": cmd_dev_transaction_plan,
            "dev-transaction-start": cmd_dev_transaction_start,
            "dev-transaction-status": cmd_dev_transaction_status,
            "dev-transaction-list": cmd_dev_transaction_list,
            "dev-read": cmd_dev_read,
            "dev-apply-patch-plan": cmd_dev_apply_patch_plan,
            "dev-apply-patch-execute": cmd_dev_apply_patch_execute,
            "dev-write-plan": cmd_dev_write_plan,
            "dev-write-execute": cmd_dev_write_execute,
            "dev-edit-plan": cmd_dev_edit_plan,
            "dev-edit-execute": cmd_dev_edit_execute,
            "dev-diff": cmd_dev_diff,
            "dev-test-plan": cmd_dev_test_plan,
            "dev-test-execute": cmd_dev_test_execute,
            "dev-commit-plan": cmd_dev_commit_plan,
            "dev-commit-execute": cmd_dev_commit_execute,
            "dev-discard-plan": cmd_dev_discard_plan,
            "dev-discard-execute": cmd_dev_discard_execute,
            "e0-build-plan": cmd_e0_build_plan,
            "e0-build-execute": cmd_e0_build_execute,
            "e0-validate-plan": cmd_e0_validate_plan,
            "e0-validate-execute": cmd_e0_validate_execute,
        }
        return commands[args.command](args, lab)
    except Exception as exc:
        return emit({"error": f"{type(exc).__name__}: {exc}"}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
