"""Execute an agent-authored study while preserving provenance and raw-data checks.

v0.5.9 adds staged study execution:

* ``pilot`` mode executes the current analysis implementation on an explicit,
  bounded subset of cases and records a validation marker tied to the exact
  analysis/config/campaign hashes;
* ``full`` mode refuses to launch a large study until a current pilot has
  succeeded;
* pilot outputs are isolated from canonical production outputs;
* both modes expose a shared per-case cache directory through environment
  variables so large-study analyzers can reuse validated per-case products.

The wrapper does not prescribe scientific metrics.  It protects raw data and
prevents expensive first-pass execution of unvalidated analysis code over a
large campaign.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
from typing import Any, Iterable

from nrg_analysis.campaign import Campaign
from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import environment_snapshot, git_state, sha256_file, write_json


LARGE_STUDY_THRESHOLD = 50
MIN_PILOT_CASES = 5
MAX_PILOT_CASES = 50
PILOT_MARKER_SCHEMA = 1


def raw_snapshot(campaign: Campaign) -> dict[str, dict[str, int]]:
    """Cheap integrity snapshot; filesystem permissions remain the primary protection."""

    snapshot: dict[str, dict[str, int]] = {}
    for case in campaign:
        candidates = [
            case.case_path / "reactor_history.dat",
            case.case_path / "setup_input.nml",
            case.case_path / "run_control_status.inf",
        ]
        task_setup = case.case_path / "task_setup"
        if task_setup.exists():
            candidates.extend(sorted(task_setup.glob("post_processor*.inf")))
        for path in candidates:
            if path.exists() and path.is_file():
                stat = path.stat()
                snapshot[str(path.resolve())] = {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
    return snapshot


def changed_raw_files(
    before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]
) -> list[str]:
    changed: list[str] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            changed.append(path)
    return changed


def _runtime_provenance(lab: Laboratory) -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "computing_module": {
            "path": str(lab.computing_module),
            "sha256": sha256_file(lab.computing_module),
        },
        "package_interfaces": {},
    }
    for name, path in lab.package_interfaces.items():
        runtime["package_interfaces"][name] = {
            "path": str(path),
            "sha256": sha256_file(path),
        }
    if lab.runtime_manifest.is_file():
        runtime["runtime_manifest"] = {
            "path": str(lab.runtime_manifest),
            "sha256": sha256_file(lab.runtime_manifest),
        }
        try:
            runtime["runtime_manifest_content"] = json.loads(
                lab.runtime_manifest.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            runtime["runtime_manifest_content"] = None
    return runtime


def _read_case_ids(cases_csv: Path) -> list[str]:
    with cases_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "case_id" not in reader.fieldnames:
            raise ValueError(f"cases.csv has no case_id column: {cases_csv}")
        ids = [str(row.get("case_id", "")).strip() for row in reader]
    if any(not case_id for case_id in ids):
        raise ValueError(f"cases.csv contains an empty case_id: {cases_csv}")
    duplicates = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"cases.csv contains duplicate case IDs: {duplicates[:10]}")
    return ids


def _normalize_requested_case_ids(case_ids: Iterable[str]) -> list[str]:
    requested: list[str] = []
    seen: set[str] = set()
    for raw in case_ids:
        case_id = str(raw).strip()
        if not case_id:
            continue
        if case_id in seen:
            raise ValueError(f"duplicate pilot case ID: {case_id}")
        seen.add(case_id)
        requested.append(case_id)
    return requested


def _write_filtered_cases_csv(
    source: Path, destination: Path, selected_case_ids: list[str]
) -> list[str]:
    """Write a source-order-preserving cases.csv containing only selected IDs."""

    requested = _normalize_requested_case_ids(selected_case_ids)
    selected_set = set(requested)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with source.open("r", encoding="utf-8-sig", newline="") as src:
        reader = csv.DictReader(src)
        if not reader.fieldnames or "case_id" not in reader.fieldnames:
            raise ValueError(f"cases.csv has no case_id column: {source}")
        rows = [row for row in reader if str(row.get("case_id", "")).strip() in selected_set]
        found = {str(row.get("case_id", "")).strip() for row in rows}
        unknown = sorted(selected_set - found)
        if unknown:
            raise ValueError(f"unknown pilot case IDs: {unknown}")
        with destination.open("w", encoding="utf-8", newline="") as dst:
            writer = csv.DictWriter(dst, fieldnames=reader.fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return [str(row.get("case_id", "")).strip() for row in rows]


def _copy_campaign_metadata(source_cases_csv: Path, pilot_dir: Path) -> list[str]:
    """Copy small generated-campaign metadata beside the pilot cases.csv.

    Some study code legitimately resolves campaign identity/configuration from
    files adjacent to cases.csv.  Pilot execution uses a filtered cases.csv in
    the study workspace, so mirror only the small declarative metadata files
    needed for read-only interpretation.  Raw case data remain in their
    original locations.
    """

    copied: list[str] = []
    for name in ("campaign.toml", "campaign_manifest.json", "case_identity_policy.json"):
        source = source_cases_csv.parent / name
        destination = pilot_dir / name
        if destination.exists():
            destination.unlink()
        if source.is_file():
            shutil.copy2(source, destination)
            copied.append(str(destination))
    return copied


def _analysis_hashes(analyze_path: Path, config_path: Path, cases_csv: Path) -> dict[str, str]:
    return {
        "analysis_script_sha256": sha256_file(analyze_path),
        "analysis_config_sha256": sha256_file(config_path),
        "cases_csv_sha256": sha256_file(cases_csv),
    }


def _load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pilot_marker_status(
    marker_path: Path,
    *,
    analyze_path: Path,
    config_path: Path,
    cases_csv: Path,
    campaign_case_count: int,
) -> dict[str, Any]:
    marker = _load_json_object(marker_path)
    expected_hashes = _analysis_hashes(analyze_path, config_path, cases_csv)
    if marker is None:
        return {
            "valid": False,
            "reason": "pilot_validation_missing",
            "marker": str(marker_path),
            "expected_hashes": expected_hashes,
        }
    reasons: list[str] = []
    if marker.get("schema_version") != PILOT_MARKER_SCHEMA:
        reasons.append("schema_version_mismatch")
    if marker.get("status") != "validated":
        reasons.append("pilot_status_not_validated")
    if marker.get("campaign_case_count") != campaign_case_count:
        reasons.append("campaign_case_count_changed")
    for key, expected in expected_hashes.items():
        if marker.get(key) != expected:
            reasons.append(f"{key}_changed")
    selected_count = marker.get("selected_count")
    if not isinstance(selected_count, int) or selected_count < MIN_PILOT_CASES:
        reasons.append("pilot_subset_too_small")
    return {
        "valid": not reasons,
        "reason": "current" if not reasons else ",".join(reasons),
        "marker": str(marker_path),
        "marker_payload": marker,
        "expected_hashes": expected_hashes,
    }


def _pilot_output_check(results_dir: Path) -> dict[str, Any]:
    """Perform generic structural checks without guessing study-specific metrics."""

    summary_path = results_dir / "study_summary.json"
    summary = _load_json_object(summary_path)
    if summary is None:
        return {
            "ok": False,
            "reason": "pilot study_summary.json missing or invalid",
            "summary_path": str(summary_path),
        }
    status = str(summary.get("status", "")).strip().lower()
    blocked_statuses = {"", "template_only", "draft", "failed", "error", "incomplete", "partial"}
    if status in blocked_statuses:
        return {
            "ok": False,
            "reason": f"pilot summary status is not production-ready: {status or '<missing>'}",
            "summary_path": str(summary_path),
            "summary": summary,
        }
    return {
        "ok": True,
        "reason": "structured pilot summary present",
        "summary_path": str(summary_path),
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("study_dir")
    parser.add_argument(
        "--laboratory",
        help="override laboratory.toml recorded in the study manifest",
    )
    parser.add_argument("--mode", choices=("pilot", "full"), default="full")
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="exact pilot case ID; valid only in --mode pilot and may be repeated",
    )
    args = parser.parse_args()

    study = Path(args.study_dir).expanduser().resolve()
    manifest_path = study / "study_manifest.json"
    analyze_path = study / "analyze.py"
    config_path = study / "analysis_config.toml"
    for path in (manifest_path, analyze_path, config_path):
        if not path.exists():
            raise SystemExit(f"required study file missing: {path}")

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    lab_path = args.laboratory or manifest.get("laboratory_config")
    lab = Laboratory.load(lab_path)

    studies_root = lab.studies_root.resolve()
    try:
        study.relative_to(studies_root)
    except ValueError as exc:
        raise SystemExit(f"study must be inside configured studies_root: {studies_root}") from exc
    if study.name == "_template":
        raise SystemExit("the template itself cannot be executed")

    case_root = manifest.get("case_root", manifest.get("workspace_root", str(lab.runs_root)))
    full_cases_csv = Path(manifest["cases_csv"]).expanduser().resolve()
    full_campaign = Campaign.load(full_cases_csv, case_root)
    campaign_case_count = len(full_campaign)
    pilot_marker_path = study / "pilot_validation.json"

    if args.mode == "pilot":
        requested = _normalize_requested_case_ids(args.case_id)
        if not (MIN_PILOT_CASES <= len(requested) <= MAX_PILOT_CASES):
            raise SystemExit(
                f"pilot mode requires {MIN_PILOT_CASES}-{MAX_PILOT_CASES} exact case IDs; "
                f"received {len(requested)}"
            )
        if len(requested) >= campaign_case_count and campaign_case_count > MIN_PILOT_CASES:
            raise SystemExit(
                "pilot subset must be smaller than the full campaign; use representative cases, not all cases"
            )
        pilot_root = study / "pilot"
        pilot_cases_csv = pilot_root / "selected_cases.csv"
        selected_ids = _write_filtered_cases_csv(full_cases_csv, pilot_cases_csv, requested)
        pilot_metadata_files = _copy_campaign_metadata(full_cases_csv, pilot_root)
        campaign = Campaign.load(pilot_cases_csv, case_root)
        results_dir = pilot_root / "results"
        if results_dir.exists():
            shutil.rmtree(results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = pilot_root / "study_stdout.log"
        stderr_path = pilot_root / "study_stderr.log"
        provenance_path = pilot_root / "provenance.json"
        # A current failed pilot must not leave an older validation marker usable.
        if pilot_marker_path.exists():
            pilot_marker_path.unlink()
        cases_for_analyzer = pilot_cases_csv
    else:
        pilot_metadata_files: list[str] = []
        if args.case_id:
            raise SystemExit("--case-id is valid only with --mode pilot")
        selected_ids = _read_case_ids(full_cases_csv)
        campaign = full_campaign
        results_dir = study / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = study / "study_stdout.log"
        stderr_path = study / "study_stderr.log"
        provenance_path = study / "provenance.json"
        cases_for_analyzer = full_cases_csv
        if campaign_case_count > LARGE_STUDY_THRESHOLD:
            pilot_status = _pilot_marker_status(
                pilot_marker_path,
                analyze_path=analyze_path,
                config_path=config_path,
                cases_csv=full_cases_csv,
                campaign_case_count=campaign_case_count,
            )
            if not pilot_status["valid"]:
                print(
                    json.dumps(
                        {
                            "error": "large_study_requires_current_pilot_validation",
                            "campaign_case_count": campaign_case_count,
                            "large_study_threshold": LARGE_STUDY_THRESHOLD,
                            "pilot_status": pilot_status,
                            "next_step": (
                                "Use nrg_study_pilot_plan, then nrg_run_study_pilot on a "
                                "representative subset. Re-run the pilot after any change to "
                                "analyze.py, analysis_config.toml, or cases.csv."
                            ),
                        },
                        indent=2,
                    ),
                    file=sys.stderr,
                )
                return 42

    before = raw_snapshot(campaign)

    command = [
        sys.executable,
        str(analyze_path),
        "--cases",
        str(cases_for_analyzer),
        "--case-root",
        str(campaign.case_root),
        "--config",
        str(config_path),
        "--output-dir",
        str(results_dir),
    ]
    started = datetime.now(timezone.utc)
    env = os.environ.copy()
    module_root = Path(__file__).resolve().parent
    project_root = str(module_root.parent)
    env["PYTHONPATH"] = project_root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["NRG_LABORATORY_CONFIG"] = str(lab.config_path)
    env["NRG_STUDY_MODE"] = args.mode
    env["NRG_STUDY_CAMPAIGN_CASE_COUNT"] = str(campaign_case_count)
    env["NRG_STUDY_SELECTED_CASE_COUNT"] = str(len(selected_ids))
    env["NRG_STUDY_CASE_CACHE_DIR"] = str(study / "results" / "_case_cache")
    hashes = _analysis_hashes(analyze_path, config_path, full_cases_csv)
    env["NRG_STUDY_ANALYSIS_SHA256"] = hashes["analysis_script_sha256"]
    env["NRG_STUDY_CONFIG_SHA256"] = hashes["analysis_config_sha256"]
    env["NRG_STUDY_CASES_SHA256"] = hashes["cases_csv_sha256"]

    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        cwd=str(study),
        env=env,
        check=False,
    )
    ended = datetime.now(timezone.utc)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    after = raw_snapshot(campaign)
    changed = changed_raw_files(before, after)
    provenance = {
        "study_id": manifest.get("study_id", study.name),
        "scientific_request": manifest.get("scientific_request", ""),
        "execution_mode": args.mode,
        "campaign_case_count": campaign_case_count,
        "selected_case_count": len(selected_ids),
        "selected_case_ids": selected_ids if args.mode == "pilot" else None,
        "started_at_utc": started.isoformat(),
        "ended_at_utc": ended.isoformat(),
        "exit_code": completed.returncode,
        "command": command,
        "laboratory_config": str(lab.config_path),
        "laboratory_config_sha256": sha256_file(lab.config_path),
        "laboratory_local_config": (str(lab.local_config_path) if lab.local_config_path else None),
        "laboratory_local_config_sha256": (
            sha256_file(lab.local_config_path) if lab.local_config_path else None
        ),
        "laboratory": lab.to_dict(),
        "runtime": _runtime_provenance(lab),
        "cases_csv": str(full_cases_csv),
        "cases_csv_sha256": hashes["cases_csv_sha256"],
        "analyzer_cases_csv": str(cases_for_analyzer),
        "pilot_metadata_files": pilot_metadata_files if args.mode == "pilot" else [],
        "case_root": str(campaign.case_root),
        "analysis_script": str(analyze_path),
        "analysis_script_sha256": hashes["analysis_script_sha256"],
        "analysis_config": str(config_path),
        "analysis_config_sha256": hashes["analysis_config_sha256"],
        "case_cache_dir": env["NRG_STUDY_CASE_CACHE_DIR"],
        "environment": environment_snapshot(),
        "research_git": git_state(lab.research_root),
        "raw_files_snapshot_count": len(before),
        "raw_files_changed": changed,
        "raw_data_integrity_ok": not changed,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    write_json(provenance_path, provenance)

    if changed:
        print("ERROR: study changed raw input files:", file=sys.stderr)
        for path in changed:
            print(f"  {path}", file=sys.stderr)
        return 90
    if completed.returncode != 0:
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        return completed.returncode

    if args.mode == "pilot":
        output_check = _pilot_output_check(results_dir)
        if not output_check["ok"]:
            print(json.dumps({"error": "pilot_output_validation_failed", **output_check}, indent=2), file=sys.stderr)
            return 43
        marker = {
            "schema_version": PILOT_MARKER_SCHEMA,
            "status": "validated",
            "validated_at_utc": ended.isoformat(),
            "campaign_case_count": campaign_case_count,
            "selected_count": len(selected_ids),
            "selected_case_ids": selected_ids,
            **hashes,
            "raw_data_integrity_ok": True,
            "pilot_results_dir": str(results_dir),
            "pilot_provenance": str(provenance_path),
            "pilot_summary": output_check.get("summary"),
            "note": (
                "This marker validates successful execution and a structured pilot summary for the exact "
                "analysis/config/campaign hashes. Scientific acceptance of the pilot outputs remains part "
                "of the nrg-study-analysis workflow."
            ),
        }
        write_json(pilot_marker_path, marker)
        print(pilot_marker_path)
    else:
        print(results_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
