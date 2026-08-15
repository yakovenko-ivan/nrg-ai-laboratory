"""Attempt configuration and reset helpers for generated NRG campaign cases."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from campaign_tools.campaign_generator_0d import render_namelist
from campaign_tools.campaign_identity import (
    apply_overrides,
    attempt_fingerprint,
    identity_fingerprint,
    load_policy_for_generated,
    validate_overrides,
)


SMALL_HASH_LIMIT_BYTES = 2 * 1024 * 1024
IMPORTANT_METADATA_NAMES = {
    "run_status.json",
    "run_control_status.inf",
    "quasistationary_status.json",
    "attempt_config.json",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def generated_setup_metadata(cases_csv: Path, case_id: str) -> Path:
    return cases_csv.parent / "_setups" / f"{case_id}.json"


def load_base_groups(cases_csv: Path, case_id: str) -> dict[str, dict[str, Any]]:
    meta = generated_setup_metadata(cases_csv, case_id)
    if not meta.is_file():
        raise FileNotFoundError(f"generated case metadata not found: {meta}")
    payload = read_json(meta)
    groups = payload.get("namelists")
    if not isinstance(groups, dict):
        raise ValueError(f"generated case metadata has no namelists: {meta}")
    return groups


def override_path(cases_csv: Path, case_id: str) -> Path:
    return cases_csv.parent / "_attempt_overrides" / f"{case_id}.json"


def attempt_setup_path(cases_csv: Path, case_id: str) -> Path:
    return cases_csv.parent / "_attempt_setups" / f"{case_id}.nml"


def load_override_record(cases_csv: Path, case_id: str) -> dict[str, Any] | None:
    path = override_path(cases_csv, case_id)
    if not path.is_file():
        return None
    return read_json(path)


def effective_groups(
    cases_csv: Path,
    case_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, dict[str, Any]]:
    base = load_base_groups(cases_csv, case_id)
    policy = load_policy_for_generated(cases_csv)
    record = load_override_record(cases_csv, case_id)
    overrides = dict(record.get("overrides", {})) if isinstance(record, dict) else {}
    effective = apply_overrides(base, overrides, policy)
    return effective, record, policy


def effective_field_value(cases_csv: Path, case_id: str, field: str) -> Any:
    groups, _record, _policy = effective_groups(cases_csv, case_id)
    group, key = field.split(".", 1)
    return groups[group][key]


def materialize_effective_setup(
    cases_csv: Path,
    case_id: str,
) -> tuple[Path, dict[str, Any]]:
    groups, record, policy = effective_groups(cases_csv, case_id)
    base_groups = load_base_groups(cases_csv, case_id)
    logical_fp = str(groups["case_config"]["case_fingerprint"])
    logical_identity = identity_fingerprint(base_groups, policy)
    effective_attempt_fp = attempt_fingerprint(groups)
    base_attempt_fp = attempt_fingerprint(base_groups)
    overrides = dict(record.get("overrides", {})) if record else {}

    if overrides:
        path = attempt_setup_path(cases_csv, case_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_namelist(groups), encoding="utf-8")
    else:
        path = cases_csv.parent / "_setups" / f"{case_id}.nml"

    metadata = {
        "attempt_schema_version": 1,
        "case_id": case_id,
        "case_fingerprint": logical_fp,
        "case_identity_fingerprint": logical_identity,
        "identity_policy": policy,
        "base_attempt_fingerprint": base_attempt_fp,
        "attempt_fingerprint": effective_attempt_fp,
        "overrides": overrides,
        "attempt_id": record.get("attempt_id") if record else None,
        "override_created_at_utc": record.get("created_at_utc") if record else None,
        "override_record": str(override_path(cases_csv, case_id)) if record else None,
        "effective_setup_file": str(path),
    }
    return path, metadata


def write_case_attempt_config(case_path: Path, metadata: dict[str, Any]) -> Path:
    path = case_path / "attempt_config.json"
    payload = {**metadata, "prepared_at_utc": utc_now()}
    write_json(path, payload)
    return path


def create_override_record(
    cases_csv: Path,
    case_id: str,
    case_fingerprint: str,
    overrides: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    base = load_base_groups(cases_csv, case_id)
    policy = load_policy_for_generated(cases_csv)
    accepted, rejected = validate_overrides(overrides, policy)
    if rejected:
        raise ValueError("attempt overrides rejected: " + json.dumps(rejected))
    effective = apply_overrides(base, accepted, policy)
    record = {
        "attempt_override_schema_version": 1,
        "attempt_id": attempt_id,
        "created_at_utc": utc_now(),
        "case_id": case_id,
        "case_fingerprint": case_fingerprint,
        "case_identity_fingerprint": identity_fingerprint(base, policy),
        "identity_policy": policy,
        "base_attempt_fingerprint": attempt_fingerprint(base),
        "attempt_fingerprint": attempt_fingerprint(effective),
        "overrides": accepted,
    }
    write_json(override_path(cases_csv, case_id), record)
    return record


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def inventory_case(case_path: Path) -> dict[str, Any]:
    if not case_path.exists():
        return {"file_count": 0, "total_bytes": 0, "files": []}
    if case_path.is_symlink():
        raise ValueError(f"refusing to inventory symlink case directory: {case_path}")

    files = []
    total = 0
    for path in sorted(case_path.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"refusing reset because case contains symlink: {path}")
        if not path.is_file():
            continue
        st = path.stat()
        size = int(st.st_size)
        total += size
        rel = path.relative_to(case_path).as_posix()
        entry: dict[str, Any] = {
            "path": rel,
            "size_bytes": size,
            "mtime_ns": int(st.st_mtime_ns),
        }
        if size <= SMALL_HASH_LIMIT_BYTES or path.name in IMPORTANT_METADATA_NAMES:
            entry["sha256"] = sha256_file(path)
        files.append(entry)
    return {"file_count": len(files), "total_bytes": total, "files": files}


def archive_attempt_metadata(
    cases_csv: Path,
    case_id: str,
    case_path: Path,
    attempt_id: str,
    reason: str,
) -> Path:
    archive = cases_csv.parent / "_attempt_history" / case_id / attempt_id
    archive.mkdir(parents=True, exist_ok=False)

    inventory = inventory_case(case_path)
    write_json(archive / "artifact_inventory.json", inventory)

    for name in IMPORTANT_METADATA_NAMES:
        source = case_path / name
        if source.is_file() and source.stat().st_size <= SMALL_HASH_LIMIT_BYTES:
            shutil.copy2(source, archive / name)

    previous_override = load_override_record(cases_csv, case_id)
    if previous_override is not None:
        write_json(archive / "previous_attempt_override.json", previous_override)

    write_json(archive / "reset_record.json", {
        "reset_at_utc": utc_now(),
        "case_id": case_id,
        "case_path": str(case_path),
        "attempt_id": attempt_id,
        "reason": reason,
        "archive_mode": "metadata_only",
        "raw_execution_products_preserved": False,
        "artifact_inventory_file": "artifact_inventory.json",
    })
    return archive


def reset_case_to_generated(
    cases_csv: Path,
    case_id: str,
    case_fingerprint: str,
    case_path: Path,
    overrides: dict[str, Any],
    attempt_id: str,
    reason: str,
) -> dict[str, Any]:
    archive = archive_attempt_metadata(
        cases_csv, case_id, case_path, attempt_id, reason
    )
    if case_path.exists():
        shutil.rmtree(case_path)

    record = create_override_record(
        cases_csv, case_id, case_fingerprint, overrides, attempt_id
    )

    setup = attempt_setup_path(cases_csv, case_id)
    if setup.exists():
        setup.unlink()

    return {
        "case_id": case_id,
        "target_state": "generated",
        "case_directory_removed": True,
        "attempt_history": str(archive),
        "override_record": str(override_path(cases_csv, case_id)),
        "attempt_fingerprint": record["attempt_fingerprint"],
        "case_identity_fingerprint": record["case_identity_fingerprint"],
        "overrides": record["overrides"],
    }
