#!/usr/bin/env python3
"""Export/import portable NRG execution bundles.

This module belongs to trusted laboratory infrastructure.  It separates a
scientific campaign contract from machine-local execution linkage:

  agent/lab -> export prepared bundle -> remote portable_executor.py ->
  pack-results -> import/verify in agent/lab

The exported bundle contains relative case locations only.  Absolute paths from
the generation machine are retained solely under provenance/ and are never used
as execution locators.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile
from typing import Any

from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import sha256_file

BUNDLE_SCHEMA_VERSION = 1
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


def safe_rel_posix(value: str) -> str:
    pure = PurePosixPath(str(value).replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError(f"invalid portable relative path: {value!r}")
    return pure.as_posix()


def load_rows(cases: Path) -> tuple[list[str], list[dict[str, str]]]:
    with cases.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError(f"manifest has no header: {cases}")
        rows = list(reader)
        fields = list(reader.fieldnames)
    if not rows:
        raise ValueError(f"campaign contains no cases: {cases}")
    ids = [str(row.get("case_id", "")).strip() for row in rows]
    if any(not cid for cid in ids) or len(set(ids)) != len(ids):
        raise ValueError("cases.csv has empty or duplicate case_id values")
    return fields, rows


def source_case_path(row: dict[str, str], lab: Laboratory) -> Path:
    raw = Path(str(row.get("case_path", ""))).expanduser()
    if not str(raw):
        raise ValueError(f"case {row.get('case_id')}: missing case_path")
    return raw.resolve() if raw.is_absolute() else (lab.runs_root / raw).resolve()


def case_relpath(row: dict[str, str], lab: Laboratory) -> str:
    explicit = str(row.get("case_relpath", "")).strip()
    if explicit:
        return safe_rel_posix(explicit)
    raw = Path(str(row.get("case_path", ""))).expanduser()
    if not raw.is_absolute():
        return safe_rel_posix(raw.as_posix())
    resolved = raw.resolve()
    try:
        rel = resolved.relative_to(lab.runs_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"case {row.get('case_id')}: absolute case_path lies outside runs_root; "
            "cannot derive a portable relative locator"
        ) from exc
    return safe_rel_posix(rel.as_posix())


def campaign_contract_payload(cases: Path, rows: list[dict[str, str]]) -> dict[str, Any]:
    campaign_toml = cases.parent / "campaign.toml"
    return {
        "campaign_toml_sha256": sha256_file(campaign_toml) if campaign_toml.is_file() else None,
        "cases": [
            {
                "case_id": str(row.get("case_id", "")).strip(),
                "case_fingerprint": str(row.get("case_fingerprint", "")).strip(),
                "case_identity_fingerprint": str(
                    row.get("case_identity_fingerprint", row.get("case_fingerprint", ""))
                ).strip(),
                "attempt_fingerprint": str(
                    row.get("generated_attempt_fingerprint", row.get("attempt_fingerprint", ""))
                ).strip(),
            }
            for row in rows
        ],
    }


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def portable_row(row: dict[str, str], rel: str) -> dict[str, str]:
    out = dict(row)
    out["case_relpath"] = rel
    out["case_path"] = rel
    out["data_save_path"] = (PurePosixPath(rel) / "data_save").as_posix()
    out["data_output_path"] = (PurePosixPath(rel) / "data_output").as_posix()
    # case_config.results_root belongs to package-interface preparation and may
    # contain the source-machine path.  Keep it for provenance in _setups, but
    # it is not an execution locator in the portable cases.csv contract.
    return out


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    fields = list(fieldnames)
    for extra in ("case_relpath",):
        if extra not in fields:
            fields.append(extra)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_export_snapshot_checksums(bundle_root: Path) -> None:
    """Hash the complete prepared bundle snapshot before any remote execution.

    execution.local.toml is deliberately excluded because it is the one
    machine-local editable file. Checksum files are also excluded to avoid
    recursion.
    """
    excluded = {
        "execution.local.toml",
        "STATIC_CHECKSUMS.sha256",
        "EXPORT_SNAPSHOT_CHECKSUMS.sha256",
    }
    lines: list[str] = []
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_root).as_posix()
        if rel in excluded:
            continue
        lines.append(f"{sha256_file(path)}  {rel}")
    (bundle_root / "EXPORT_SNAPSHOT_CHECKSUMS.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def immutable_files_for_checksum(bundle_root: Path, portable_rows: list[dict[str, str]]) -> list[Path]:
    files = [
        bundle_root / "bundle_manifest.json",
        bundle_root / "cases.csv",
        bundle_root / "campaign.toml",
        bundle_root / "portable_executor.py",
        bundle_root / "README_RUN.md",
        bundle_root / "EXPORT_SNAPSHOT_CHECKSUMS.sha256",
    ]
    for row in portable_rows:
        cp = bundle_root / "runs" / Path(*PurePosixPath(row["case_relpath"]).parts)
        setup = cp / "setup_input.nml"
        if setup.is_file():
            files.append(setup)
    return [p for p in files if p.is_file()]


def write_static_checksums(bundle_root: Path, portable_rows: list[dict[str, str]]) -> None:
    lines = []
    for path in sorted(immutable_files_for_checksum(bundle_root, portable_rows)):
        rel = path.relative_to(bundle_root).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}")
    (bundle_root / "STATIC_CHECKSUMS.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def execution_config_template(source_sha: str | None) -> str:
    source_comment = source_sha or "not-recorded"
    return f'''# Machine-local configuration for this exported NRG execution bundle.
# Edit ONLY this file on the compute host. It is execution provenance, not a
# scientific campaign definition.

[runtime]
computing_module = "CHANGE_ME"
# Source-machine binary SHA-256 recorded in bundle: {source_comment}
# Set true only when the exact same binary is intentionally required.
require_exact_binary = false

[execution]
backend = "local"
threads = 8
poll_seconds = 0.25
graceful_pause_timeout_seconds = 120.0
rerun_failed = true
continue_on_failure = false
limit_library_threads = true
'''


def run_readme() -> str:
    return '''# Portable NRG execution bundle

This directory is a prepared scientific campaign plus a standalone execution
client.  No AI agent and no NRG package-interface executable are required on the
compute host.

## 1. Configure the compute host

Edit `execution.local.toml` and set the absolute path to a compatible NRG
`computing_module` binary and the desired thread count.

## 2. Validate before first execution

    python portable_executor.py validate --config execution.local.toml

A differing computing-module SHA-256 is reported as provenance. Set
`require_exact_binary = true` if the exact source binary is mandatory.

## 3. Run / resume

    python portable_executor.py run --config execution.local.toml

The same command is used after a clean pause or wall-time restart. Completed
cases are skipped; a paused/restart-required case is continued in place.

## 4. Status

    python portable_executor.py status

## 5. Pause

Immediate restartable pause (NRG writes a checkpoint):

    python portable_executor.py pause --mode now

Pause after the current case finishes naturally:

    python portable_executor.py pause --mode after-current

Do not kill `computing_module` manually when a pause is available.

## 6. Return results

After the executor is not running:

    python portable_executor.py pack-results --output results.zip

Copy `results.zip` back to the agentic/scientific environment and import it with
the trusted NRG Laboratory result-import tool.

## Portability boundary

Campaign/result transfer is portable. Restart checkpoints are raw NRG binary
state and should only be moved between binary-compatible NRG runtimes (same data
kinds/endianness/restart schema; preferably the same source revision/build).
'''


def export_plan(cases: Path, lab: Laboratory, output: Path | None = None) -> dict[str, Any]:
    cases = cases.expanduser().resolve()
    _fields, rows = load_rows(cases)
    contract = campaign_contract_payload(cases, rows)
    contract_sha = canonical_sha256(contract)
    executor_source = (lab.research_root / "campaign_tools" / "portable_executor.py").resolve()
    problems: list[dict[str, Any]] = []
    if not executor_source.is_file():
        problems.append({"reason": "portable_executor_not_installed", "path": str(executor_source)})
        executor_sha = "missing"
    else:
        executor_sha = sha256_file(executor_source)
    bundle_id = hashlib.sha256(
        f"nrg-bundle-v1:{contract_sha}:{executor_sha}".encode("utf-8")
    ).hexdigest()[:20]
    if output is None:
        output = lab.campaign_root / "exports" / f"{cases.parent.name}__{bundle_id}"
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        problems.append({"reason": "output_exists_nonempty", "path": str(output)})
    total_bytes = 0
    prepared = 0
    sample: list[dict[str, Any]] = []
    for row in rows:
        cid = str(row["case_id"]).strip()
        try:
            rel = case_relpath(row, lab)
            src = source_case_path(row, lab)
        except Exception as exc:
            problems.append({"case_id": cid, "reason": "path_resolution", "error": str(exc)})
            continue
        ready = (
            src.is_dir()
            and (src / "task_setup").is_dir()
            and (src / "setup_input.nml").is_file()
            and (src / "data_output").is_dir()
            and any(path.is_file() for path in (src / "data_output").iterdir())
        )
        if not ready:
            problems.append({"case_id": cid, "reason": "case_not_prepared", "path": str(src)})
        else:
            status_path = src / "run_status.json"
            if status_path.is_file():
                try:
                    current_state = str(read_json(status_path).get("status", "")).lower()
                except Exception:
                    current_state = "invalid_status"
                if current_state in {"running", "pause_failed", "invalid_status"}:
                    problems.append(
                        {
                            "case_id": cid,
                            "reason": "case_not_safely_exportable",
                            "status": current_state,
                            "path": str(src),
                        }
                    )
            prepared += 1
            for path in src.rglob("*"):
                if path.is_file():
                    try:
                        total_bytes += path.stat().st_size
                    except OSError:
                        pass
        if len(sample) < 8:
            sample.append({"case_id": cid, "case_relpath": rel, "source_path": str(src), "prepared": ready})
    return {
        "can_export": len(problems) == 0,
        "cases_csv": str(cases),
        "bundle": str(output),
        "bundle_id": bundle_id,
        "campaign_contract_sha256": contract_sha,
        "case_count": len(rows),
        "prepared_case_count": prepared,
        "estimated_case_bytes": total_bytes,
        "problems": problems[:50],
        "problem_count": len(problems),
        "sample": sample,
        "semantics": (
            "Export copies prepared cases into a self-contained execution bundle and rewrites only "
            "execution locators to portable relative paths. Source campaign/results remain untouched."
        ),
    }


def export_bundle(
    cases: Path,
    lab: Laboratory,
    output: Path | None = None,
) -> dict[str, Any]:
    preflight = export_plan(cases, lab, output)
    if not preflight["can_export"]:
        raise ValueError("bundle export preflight failed; inspect export-plan output")
    cases = cases.expanduser().resolve()
    fields, rows = load_rows(cases)
    contract = campaign_contract_payload(cases, rows)
    contract_sha = canonical_sha256(contract)
    executor_source = (lab.research_root / "campaign_tools" / "portable_executor.py").resolve()
    if not executor_source.is_file():
        raise FileNotFoundError(
            f"portable executor not installed in laboratory: {executor_source}"
        )
    executor_sha = sha256_file(executor_source)
    bundle_id = hashlib.sha256(
        f"nrg-bundle-v1:{contract_sha}:{executor_sha}".encode("utf-8")
    ).hexdigest()[:20]
    if output is None:
        output = lab.campaign_root / "exports" / f"{cases.parent.name}__{bundle_id}"
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"bundle output already exists and is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "runs").mkdir(exist_ok=True)
    (output / "provenance").mkdir(exist_ok=True)

    portable_rows: list[dict[str, str]] = []
    case_manifest: dict[str, Any] = {}
    for row in rows:
        cid = str(row["case_id"]).strip()
        rel = case_relpath(row, lab)
        src = source_case_path(row, lab)
        if not src.is_dir():
            raise FileNotFoundError(f"case {cid} is not prepared: {src}")
        if (
            not (src / "task_setup").is_dir()
            or not (src / "setup_input.nml").is_file()
            or not (src / "data_output").is_dir()
            or not any(path.is_file() for path in (src / "data_output").iterdir())
        ):
            raise ValueError(
                f"case {cid} is not execution-ready: prepared task_setup, setup_input.nml "
                "and initial data_output state are required"
            )
        dst = output / "runs" / Path(*PurePosixPath(rel).parts)
        if dst.exists():
            raise ValueError(f"duplicate portable case path: {rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, copy_function=shutil.copy2)
        prow = portable_row(row, rel)
        portable_rows.append(prow)
        case_manifest[cid] = {
            "case_relpath": rel,
            "case_fingerprint": row.get("case_fingerprint"),
            "case_identity_fingerprint": row.get(
                "case_identity_fingerprint", row.get("case_fingerprint")
            ),
            "attempt_fingerprint": row.get(
                "generated_attempt_fingerprint", row.get("attempt_fingerprint")
            ),
        }

    write_csv(output / "cases.csv", fields, portable_rows)
    campaign_toml = cases.parent / "campaign.toml"
    if campaign_toml.is_file():
        shutil.copy2(campaign_toml, output / "campaign.toml")
    else:
        (output / "campaign.toml").write_text(
            "# Source campaign.toml was unavailable at export time.\n", encoding="utf-8"
        )
    setup_dir = cases.parent / "_setups"
    if setup_dir.is_dir():
        shutil.copytree(setup_dir, output / "_setups", copy_function=shutil.copy2)

    source_manifest = cases.parent / "campaign_manifest.json"
    if source_manifest.is_file():
        shutil.copy2(source_manifest, output / "provenance" / "source_campaign_manifest.json")
    source_runtime_manifest = lab.runtime_manifest
    if source_runtime_manifest.is_file():
        shutil.copy2(
            source_runtime_manifest,
            output / "provenance" / "source_runtime_manifest.json",
        )
    shutil.copy2(cases, output / "provenance" / "source_cases.csv")
    shutil.copy2(executor_source, output / "portable_executor.py")
    (output / "README_RUN.md").write_text(run_readme(), encoding="utf-8")

    source_runtime = {
        "computing_module_sha256": (
            sha256_file(lab.computing_module) if lab.computing_module.is_file() else None
        ),
        "runtime_manifest_sha256": (
            sha256_file(source_runtime_manifest) if source_runtime_manifest.is_file() else None
        ),
        # Absolute source paths are provenance only, never execution locators.
        "generation_environment": {
            "computing_module": str(lab.computing_module),
            "runs_root": str(lab.runs_root),
            "laboratory_config": str(lab.config_path),
            "laboratory_local_config": (
                str(lab.local_config_path) if lab.local_config_path else None
            ),
        },
    }
    bundle_manifest = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "created_at_utc": utc_now(),
        "campaign_contract_sha256": contract_sha,
        "campaign_contract": contract,
        "case_count": len(rows),
        "path_contract": {
            "case_locator": "case_relpath relative to bundle/runs",
            "absolute_source_paths_authoritative": False,
        },
        "executor_sha256": executor_sha,
        "source_runtime": source_runtime,
        "cases": case_manifest,
    }
    write_json(output / "bundle_manifest.json", bundle_manifest)
    (output / "execution.local.toml").write_text(
        execution_config_template(source_runtime["computing_module_sha256"]),
        encoding="utf-8",
    )
    write_export_snapshot_checksums(output)
    write_static_checksums(output, portable_rows)
    return {
        "bundle": str(output),
        "bundle_id": bundle_id,
        "campaign_contract_sha256": contract_sha,
        "case_count": len(rows),
        "portable_executor": str(output / "portable_executor.py"),
        "local_execution_config": str(output / "execution.local.toml"),
    }


def _zip_json(zf: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        return json.loads(zf.read(name).decode("utf-8-sig"))
    except KeyError as exc:
        raise ValueError(f"results archive missing {name}") from exc


def _zip_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    import io

    text = zf.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _safe_zip_members(zf: zipfile.ZipFile) -> None:
    for info in zf.infolist():
        pure = PurePosixPath(info.filename)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"unsafe archive member path: {info.filename}")


def import_plan(cases: Path, results: Path, lab: Laboratory) -> dict[str, Any]:
    cases = cases.expanduser().resolve()
    results = results.expanduser().resolve()
    _, local_rows = load_rows(cases)
    local_by_id = {row["case_id"]: row for row in local_rows}
    local_contract_sha = canonical_sha256(campaign_contract_payload(cases, local_rows))
    problems: list[dict[str, Any]] = []
    cases_to_import: list[dict[str, Any]] = []

    with zipfile.ZipFile(results, "r") as zf:
        _safe_zip_members(zf)
        result_manifest = _zip_json(zf, "results_manifest.json")
        bundle_manifest = _zip_json(zf, "bundle_manifest.json")
        portable_rows = _zip_csv(zf, "cases.csv")
        if int(result_manifest.get("results_schema_version", -1)) != RESULTS_SCHEMA_VERSION:
            problems.append({"reason": "unsupported_results_schema"})
        contract_sha = str(result_manifest.get("campaign_contract_sha256", ""))
        if contract_sha != local_contract_sha:
            problems.append(
                {
                    "reason": "campaign_contract_mismatch",
                    "local": local_contract_sha,
                    "results": contract_sha,
                }
            )
        if str(bundle_manifest.get("campaign_contract_sha256", "")) != contract_sha:
            problems.append({"reason": "bundle_results_contract_disagreement"})

        returned_by_id = {row.get("case_id", ""): row for row in portable_rows}
        result_cases = result_manifest.get("cases", {})
        if not isinstance(result_cases, dict):
            problems.append({"reason": "invalid_result_case_table"})
            result_cases = {}
        for cid, remote in result_cases.items():
            local = local_by_id.get(cid)
            portable = returned_by_id.get(cid)
            if local is None or portable is None:
                problems.append({"case_id": cid, "reason": "unknown_returned_case"})
                continue
            local_fp = str(local.get("case_fingerprint", "")).strip()
            remote_fp = str(remote.get("case_fingerprint", "")).strip()
            if local_fp != remote_fp:
                problems.append({"case_id": cid, "reason": "case_fingerprint_mismatch"})
                continue
            local_identity_fp = str(
                local.get("case_identity_fingerprint", local.get("case_fingerprint", ""))
            ).strip()
            remote_identity_fp = str(
                remote.get("case_identity_fingerprint", remote.get("case_fingerprint", ""))
            ).strip()
            if local_identity_fp != remote_identity_fp:
                problems.append({"case_id": cid, "reason": "case_identity_fingerprint_mismatch"})
                continue
            local_attempt_fp = str(
                local.get("generated_attempt_fingerprint", local.get("attempt_fingerprint", ""))
            ).strip()
            remote_attempt_fp = str(remote.get("attempt_fingerprint", "")).strip()
            if local_attempt_fp != remote_attempt_fp:
                problems.append({"case_id": cid, "reason": "attempt_fingerprint_mismatch"})
                continue
            rel = safe_rel_posix(portable.get("case_relpath") or portable.get("case_path") or "")
            target = source_case_path(local, lab)
            local_status = read_json(target / "run_status.json") if (target / "run_status.json").is_file() else None
            local_bundle_id = str((local_status or {}).get("bundle_id", ""))
            result_bundle_id = str(result_manifest.get("bundle_id", ""))
            conflict = False
            if local_status is not None and local_bundle_id != result_bundle_id:
                conflict = True
                problems.append(
                    {
                        "case_id": cid,
                        "reason": "existing_local_execution_conflict",
                        "existing_status": local_status.get("status"),
                        "local_bundle_id": local_bundle_id or None,
                        "result_bundle_id": result_bundle_id,
                        "message": (
                            "Local case already contains execution metadata not proven to belong "
                            "to this returned bundle; import will not overwrite it."
                        ),
                    }
                )
            cases_to_import.append(
                {
                    "case_id": cid,
                    "case_relpath": rel,
                    "target": str(target),
                    "returned_status": remote.get("status"),
                    "existing_status": (local_status or {}).get("status"),
                    "conflict": conflict,
                }
            )

        inventory = result_manifest.get("files_sha256", {})
        if not isinstance(inventory, dict):
            problems.append({"reason": "missing_file_inventory"})
        else:
            names = set(zf.namelist())
            for rel, expected in inventory.items():
                if rel not in names:
                    problems.append({"reason": "missing_archived_file", "file": rel})
                    continue
                digest = hashlib.sha256()
                with zf.open(rel, "r") as stream:
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                observed = digest.hexdigest()
                if observed != expected:
                    problems.append({"reason": "archived_file_checksum_mismatch", "file": rel})

    return {
        "can_import": len(problems) == 0,
        "cases_csv": str(cases),
        "results_archive": str(results),
        "bundle_id": result_manifest.get("bundle_id"),
        "campaign_contract_sha256": local_contract_sha,
        "case_count_in_results": len(cases_to_import),
        "cases": cases_to_import,
        "problems": problems[:50],
        "problem_count": len(problems),
        "semantics": (
            "Import overlays verified returned execution artifacts onto the same local logical cases. "
            "It does not regenerate cases or change scientific identity."
        ),
    }


def import_results(cases: Path, results: Path, lab: Laboratory) -> dict[str, Any]:
    plan = import_plan(cases, results, lab)
    if not plan["can_import"]:
        raise ValueError("result import preflight failed; inspect import plan")
    with tempfile.TemporaryDirectory(prefix="nrg-results-import-") as td:
        stage = Path(td)
        with zipfile.ZipFile(results, "r") as zf:
            _safe_zip_members(zf)
            zf.extractall(stage)
        with zipfile.ZipFile(results, "r") as zf:
            portable_rows = {row["case_id"]: row for row in _zip_csv(zf, "cases.csv")}
        _, local_rows = load_rows(cases)
        local_by_id = {row["case_id"]: row for row in local_rows}
        imported = []
        for item in plan["cases"]:
            cid = item["case_id"]
            rel = safe_rel_posix(portable_rows[cid].get("case_relpath") or portable_rows[cid]["case_path"])
            src = stage / "runs" / Path(*PurePosixPath(rel).parts)
            target = source_case_path(local_by_id[cid], lab)
            target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target, dirs_exist_ok=True, copy_function=shutil.copy2)
            imported.append(cid)

    record = {
        "imported_at_utc": utc_now(),
        "results_archive": str(results.expanduser().resolve()),
        "results_archive_sha256": sha256_file(results.expanduser().resolve()),
        "bundle_id": plan.get("bundle_id"),
        "campaign_contract_sha256": plan.get("campaign_contract_sha256"),
        "imported_case_ids": imported,
    }
    imports_dir = cases.expanduser().resolve().parent / "_imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y%m%dT%H%M%S") + f"__{plan.get('bundle_id','bundle')}.json"
    record_path = imports_dir / name
    write_json(record_path, record)
    return {**plan, "imported": True, "import_record": str(record_path)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--laboratory", type=Path, default=None)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("export-plan")
    s.add_argument("--cases", type=Path, required=True)
    s.add_argument("--output", type=Path)
    s = sub.add_parser("export")
    s.add_argument("--cases", type=Path, required=True)
    s.add_argument("--output", type=Path)
    s = sub.add_parser("import-plan")
    s.add_argument("--cases", type=Path, required=True)
    s.add_argument("--results", type=Path, required=True)
    s = sub.add_parser("import")
    s.add_argument("--cases", type=Path, required=True)
    s.add_argument("--results", type=Path, required=True)
    return p


def main() -> int:
    args = build_parser().parse_args()
    # Export operates on already-prepared cases. Runtime executables are optional
    # provenance here; the compute host supplies its own machine-local binary.
    # Import likewise needs only laboratory roots and campaign identity.
    lab = Laboratory.load(args.laboratory, require_runtime=False)
    if args.command == "export-plan":
        payload = export_plan(args.cases, lab, args.output)
    elif args.command == "export":
        payload = export_bundle(args.cases, lab, args.output)
    elif args.command == "import-plan":
        payload = import_plan(args.cases, args.results, lab)
    elif args.command == "import":
        payload = import_results(args.cases, args.results, lab)
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, indent=2, sort_keys=True))
    ok = payload.get("can_import", payload.get("can_export", True))
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
