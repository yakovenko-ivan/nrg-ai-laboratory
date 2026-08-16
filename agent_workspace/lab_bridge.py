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
        "run_control.stop",
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
    if existing and str(existing.get("state", "")).lower() in {"requested", "acknowledged"}:
        blockers.append({"reason": "operator_stop_already_pending", "control": existing})

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
        "laboratory_runner": probe_runner_lock(lab.campaign_root),
        "blockers": blockers,
        "semantics": (
            "stop_campaign prevents any further case launch and gracefully stops the current case if one is active"
            if action == "stop_campaign"
            else "stop_case gracefully stops only the currently active case; the runner may continue with later cases"
        ),
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
            if str(current.get("state", "")).lower() in {"handled", "rejected"}:
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

def cmd_lab_info(args: argparse.Namespace, lab: Laboratory) -> int:
    runtime = {
        "computing_module_sha256": sha256_file(lab.computing_module),
        "package_interface_0d_sha256": sha256_file(lab.package_interface_0d),
    }
    return emit({"laboratory": lab.to_dict(), "runtime": runtime, "bridge_version": PROJECT_VERSION})


def cmd_campaign_status(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    # Pi uses the reviewed laboratory runner policy. `--run-config` remains
    # only as a private/manual CLI override for trusted diagnostics.
    config = resolve(args.run_config) if args.run_config else lab.runner_config
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    if not config.is_file():
        return emit({"error": f"trusted runner configuration not found: {config}"}, 2)
    return emit(campaign_status_payload(cases, lab, config))


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


def cmd_campaign_preview(args: argparse.Namespace, lab: Laboratory) -> int:
    campaign = resolve_campaign_definition(args.campaign, lab)
    if not campaign.is_file():
        return emit({"error": f"campaign definition not found: {campaign}", "definitions_root": str(campaign_definitions_root(lab))}, 2)
    generator = lab.research_root / "campaign_tools" / "campaign_generator_0d.py"
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
    generator = lab.research_root / "campaign_tools" / "campaign_generator_0d.py"
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
            [str(lab.package_interface_0d), str(setup)],
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
    return emit({"job": record, "status": status})


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
    return emit({
        "job": record,
        "operator_control": operator_control,
        "laboratory_runner": live_runner,
        "runner_lock_semantics": {
            "authoritative_current_field": "laboratory_runner",
            "job_runner_lock_is_persisted_snapshot": True,
        },
        "campaign_status": status,
    })


def cmd_campaign_stop_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    plan = operator_stop_plan(lab, action="stop_campaign", job_ref=args.job)
    return emit(plan, 0 if plan.get("can_stop") else 3)


def cmd_campaign_stop_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    plan = operator_stop_plan(lab, action="stop_campaign", job_ref=args.job)
    result = issue_operator_stop(lab, plan, reason=args.reason, wait_seconds=args.wait_seconds)
    return emit(result, 0 if not result.get("error") else 3)


def cmd_case_stop_plan(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"can_stop": False, "error": f"cases.csv not found: {cases}"}, 2)
    plan = operator_stop_plan(
        lab, action="stop_case", cases=cases, case_id=args.case_id
    )
    return emit(plan, 0 if plan.get("can_stop") else 3)


def cmd_case_stop_execute(args: argparse.Namespace, lab: Laboratory) -> int:
    cases = resolve(args.cases)
    if not cases.is_file():
        return emit({"error": f"cases.csv not found: {cases}"}, 2)
    plan = operator_stop_plan(
        lab, action="stop_case", cases=cases, case_id=args.case_id
    )
    result = issue_operator_stop(lab, plan, reason=args.reason, wait_seconds=args.wait_seconds)
    return emit(result, 0 if not result.get("error") else 3)


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

    s = sub.add_parser("campaign-stop-plan")
    s.add_argument("--job")

    s = sub.add_parser("campaign-stop-execute")
    s.add_argument("--job")
    s.add_argument("--reason")
    s.add_argument("--wait-seconds", type=float, default=45.0)

    s = sub.add_parser("case-stop-plan")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", required=True)

    s = sub.add_parser("case-stop-execute")
    s.add_argument("--cases", required=True)
    s.add_argument("--case-id", required=True)
    s.add_argument("--reason")
    s.add_argument("--wait-seconds", type=float, default=45.0)

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
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        lab = Laboratory.load(args.laboratory)
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
            "case-stop-plan": cmd_case_stop_plan,
            "case-stop-execute": cmd_case_stop_execute,
            "create-study": cmd_create_study,
            "study-pilot-plan": cmd_study_pilot_plan,
            "run-study-pilot": cmd_run_study_pilot,
            "run-study": cmd_run_study,
            "study-summary": cmd_study_summary,
        }
        return commands[args.command](args, lab)
    except Exception as exc:
        return emit({"error": f"{type(exc).__name__}: {exc}"}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
