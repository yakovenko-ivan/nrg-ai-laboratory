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
        bundle_root / "quasistationary_core.py",
        bundle_root / "termination_profiles.json",
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
# Runtime linkage and termination policy may be adjusted on the compute host.
# Every selected termination registry/profile is hashed into execution provenance.

[runtime]
computing_module = "CHANGE_ME"
# Source-machine binary SHA-256 recorded in bundle: {source_comment}
require_exact_binary = false

[execution]
backend = "local"
threads = 8
poll_seconds = 0.25
graceful_pause_timeout_seconds = 120.0
# Production defaults (Phase-1 hardening): a failed case is preserved (not
# converted to success) while later runnable cases continue, and an ordinary
# resume skips already-failed cases. These two keys are independent controls:
#   continue_on_failure = false  -> restore strict fail-fast behavior
#   rerun_failed = true          -> allow previously failed cases to rerun on resume
rerun_failed = false
continue_on_failure = true
limit_library_threads = true

[termination]
# Leave profile_name empty to disable physical-condition monitoring.
profile_name = "0d_cv_post_ignition_quasistationary_v2"
# The bundled registry is the reviewed default. For a deliberate remote override,
# copy/edit another registry file and point this field to it; the exact file and
# selected profile are fingerprinted in executor_job.json and run_status.json.
profile_registry = "termination_profiles.json"
'''


def run_readme() -> str:
    return '''# Portable NRG execution bundle

This directory is a prepared scientific campaign plus a standalone Python 3.11+
execution client. No AI agent and no NRG package-interface executable are
required on the compute host.

## Configure

Edit `execution.local.toml`. Set the local `computing_module` path, thread count,
and (if desired) the physical termination profile. The bundle contains the
reviewed termination registry and canonical quasistationarity evaluator.

The default is:

    [termination]
    profile_name = "0d_cv_post_ignition_quasistationary_v2"
    profile_registry = "termination_profiles.json"

A deliberate external-machine termination-policy change is supported: create a
separate registry file, select it in `execution.local.toml`, and validate again.
The selected registry SHA-256, profile SHA-256, and evaluator SHA-256 are recorded
in execution provenance. Do not edit `quasistationary_core.py`; its bundle hash
is verified before it is imported.

## Validate

    python portable_executor.py validate --config execution.local.toml

## Run / resume

    python portable_executor.py run --config execution.local.toml

The same command resumes a clean wall-time restart or pause. A quasistationary
condition writes `run_control.stop`; `condition_met` is accepted only if NRG then
exits through the expected graceful `external_stop_request` contract.

## Status and control

    python portable_executor.py status
    python portable_executor.py pause --mode now
    python portable_executor.py pause --mode after-current
    python portable_executor.py stop

## Return results

    python portable_executor.py pack-results --output results.zip

Restart checkpoints are raw NRG binary state and should only be moved between
binary-compatible NRG runtimes.
'''
DEFAULT_TERMINATION_PROFILE = "0d_cv_post_ignition_quasistationary_v2"


def _portable_termination_sources(lab: Laboratory) -> tuple[Path, Path]:
    core = (lab.research_root / "nrg_analysis" / "quasistationary_core.py").resolve()
    registry = lab.termination_profiles.resolve()
    return core, registry


def _validate_default_profile(registry: Path) -> None:
    data = read_json(registry)
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict) or DEFAULT_TERMINATION_PROFILE not in profiles:
        raise ValueError(
            f"default portable termination profile {DEFAULT_TERMINATION_PROFILE!r} "
            f"not found in {registry}"
        )


def _source_resource_contract(cases: Path) -> dict[str, Any] | None:
    manifest = cases.parent / "campaign_manifest.json"
    if not manifest.is_file():
        return None
    payload = read_json(manifest)
    contract = payload.get("task_setup_resources")
    return contract if isinstance(contract, dict) else None


def _verify_prepared_resource_contract(case_path: Path, contract: dict[str, Any] | None) -> list[str]:
    if not contract:
        return []
    problems: list[str] = []
    task_setup = case_path / "task_setup"
    entries: list[tuple[str, dict[str, Any]]] = []
    for group in ("kinetic_mechanisms", "thermophysical_data"):
        table = contract.get(group, {})
        if isinstance(table, dict):
            for key, value in table.items():
                if isinstance(value, dict):
                    entries.append((f"{group}.{key}", value))
    for label, item in entries:
        rel = str(item.get("relative_path") or "").strip()
        expected = str(item.get("sha256") or "").strip()
        if not rel or not expected:
            problems.append(f"{label}: incomplete resource provenance entry")
            continue
        target = task_setup / Path(*PurePosixPath(rel.replace("\\", "/")).parts)
        if not target.is_file():
            problems.append(f"{label}: prepared resource missing: {rel}")
            continue
        observed = sha256_file(target)
        if observed != expected:
            problems.append(f"{label}: prepared resource checksum mismatch: {rel}")
    return problems


# ---------------------------------------------------------------------------
# Managed portable artifact roots and transport-artifact cleanup
#
# ``runs/`` is the single authoritative heavy scientific record.  Portable
# export bundles and returned result archives are *transient transport
# artifacts* that live under ``<research_root>/.local/portable/``.  A
# successful verified import writes a durable lightweight receipt under
# ``<campaign>/_imports/`` and, by default, reclaims the matching managed
# transport artifacts.  Legacy laboratory-managed locations
# (``<campaign_root>/exports/`` and
# ``<research_root>/agent_workspace/portable_returns/``) are recognised for
# cleanup only; no new artifacts are written there.
#
# Deletion safety is provenance-based (managed-root containment + manifest /
# receipt proof), never filename-based.  ``runs/`` is never a cleanup
# candidate.  No intra-campaign file deduplication, hardlinks, reflinks, or
# compression is performed.
# ---------------------------------------------------------------------------


def managed_portable_root(lab: Laboratory) -> Path:
    """Transient managed transport-artifact root for portable bundles/returns."""
    return (lab.research_root / ".local" / "portable").resolve()


def managed_exports_root(lab: Laboratory) -> Path:
    """Default destination for new portable export bundles (transient)."""
    return (managed_portable_root(lab) / "exports").resolve()


def managed_returns_root(lab: Laboratory) -> Path:
    """Default managed location for returned result archives (transient)."""
    return (managed_portable_root(lab) / "returns").resolve()


def legacy_exports_root(lab: Laboratory) -> Path:
    """Historical laboratory-managed export location; cleanup-discoverable only."""
    return (lab.campaign_root / "exports").resolve()


def legacy_returns_root(lab: Laboratory) -> Path:
    """Historical laboratory-managed return location; cleanup-discoverable only."""
    return (lab.research_root / "agent_workspace" / "portable_returns").resolve()


def _export_roots(lab: Laboratory) -> list[tuple[Path, str]]:
    return [
        (managed_exports_root(lab), "managed_exports"),
        (legacy_exports_root(lab), "legacy_exports"),
    ]


def _return_roots(lab: Laboratory) -> list[tuple[Path, str]]:
    return [
        (managed_returns_root(lab), "managed_returns"),
        (legacy_returns_root(lab), "legacy_returns"),
    ]


def _strictly_inside(path: Path, root: Path) -> bool:
    """Return True only when *path* resolves to a strict descendant of *root*.

    Rejects ``..`` traversal, symlink escapes, the root itself, and arbitrary
    external paths.  Symlink resolution follows links so an in-root symlink
    pointing outside is correctly rejected.
    """
    try:
        resolved = path.resolve(strict=False)
        base = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if resolved == base:
        return False
    try:
        resolved.relative_to(base)
    except ValueError:
        return False
    return True


def _recursive_bytes(path: Path) -> int:
    """Recursive byte size of a file or directory tree (0 on error/missing)."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    if path.is_dir():
        total = 0
        for sub in path.rglob("*"):
            if sub.is_file():
                try:
                    total += sub.stat().st_size
                except OSError:
                    pass
        return total
    return 0


def _discover_import_receipts(lab: Laboratory) -> list[dict[str, Any]]:
    """Discover durable ``_imports/*.json`` receipts beneath the campaigns root.

    Each receipt is proof of a scientifically successful import: receipts are
    only written after verified results have been overlaid onto canonical
    ``runs/``.
    """
    receipts: list[dict[str, Any]] = []
    root = lab.campaign_root.resolve()
    if not root.is_dir():
        return receipts
    for imports_dir in sorted(root.rglob("_imports")):
        if not imports_dir.is_dir():
            continue
        for receipt_path in sorted(imports_dir.glob("*.json")):
            try:
                data = read_json(receipt_path)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            receipts.append({"path": str(receipt_path.resolve()), "data": data})
    return receipts


def _archive_bundle_id(path: Path) -> str | None:
    """Read ``bundle_id`` from a returned results ZIP manifest without extracting."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            _safe_zip_members(zf)
            manifest = _zip_json(zf, "results_manifest.json")
            bid = str(manifest.get("bundle_id") or "")
            return bid or None
    except Exception:
        return None


def _parse_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 UTC-aware timestamp.

    Returns ``None`` for missing/malformed/naive values so that temporal proof
    fails closed rather than authorizing deletion by lexicographic guesswork.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt


def _classify_returned_archive(
    path: Path, lab: Laboratory, receipts: list[dict[str, Any]]
) -> dict[str, Any]:
    """Classify a returned result ZIP for cleanup safety (fail closed).

    Deletable only when ALL are proven: strict managed/legacy return-root
    containment; a valid readable results ZIP; a non-empty archive ``bundle_id``;
    a non-empty receipt ``bundle_id``; exact archive/receipt ``bundle_id``
    equality; exact current archive SHA-256 equality with the receipt's
    ``results_archive_sha256``; and a matching successful import receipt.
    Hash equality alone is insufficient.
    """
    root_type: str | None = None
    for root, rtype in _return_roots(lab):
        if _strictly_inside(path, root):
            root_type = rtype
            break
    base: dict[str, Any] = {
        "kind": "returned_archive",
        "path": str(path),
        "root_type": root_type,
        "bundle_id": None,
        "campaign_contract_sha256": None,
        "bytes": _recursive_bytes(path),
        "classification": "external",
        "can_delete": False,
        "reason": "archive outside managed/legacy return roots; never auto-deleted",
    }
    if root_type is None:
        return base
    archive_bid = _archive_bundle_id(path)
    base["bundle_id"] = archive_bid
    if not archive_bid:
        base.update({"classification": "unreadable_or_missing_identity",
                     "reason": "archive missing/unreadable results manifest or bundle_id"})
        return base
    try:
        current_hash = sha256_file(path)
    except OSError:
        base.update({"classification": "unreadable", "reason": "could not hash archive"})
        return base
    bid_match = False
    for receipt in receipts:
        rdata = receipt["data"]
        r_bid = str(rdata.get("bundle_id") or "")
        r_hash = str(rdata.get("results_archive_sha256") or "")
        if r_bid and r_bid == archive_bid:
            bid_match = True
            if r_hash and r_hash == current_hash:
                base["campaign_contract_sha256"] = (
                    str(rdata.get("campaign_contract_sha256") or "") or None
                )
                base.update({"classification": "safe_imported", "can_delete": True,
                             "reason": "matching successful import receipt, exact bundle_id and hash proof"})
                return base
    if bid_match:
        base.update({"classification": "hash_mismatch",
                     "reason": "receipt bundle_id matches but archive hash differs"})
    else:
        base.update({"classification": "no_receipt",
                     "reason": "no matching import receipt with agreeing non-empty bundle_id"})
    return base


def _classify_export(
    path: Path,
    lab: Laboratory,
    receipts: list[dict[str, Any]],
    include_unimported: bool = False,
) -> dict[str, Any]:
    """Classify an expanded export directory for cleanup safety (fail closed).

    Deletable with a matching receipt only when: strict managed/legacy export
    containment; ``bundle_manifest.json`` parses; non-empty ``bundle_id`` and
    ``campaign_contract_sha256``; no executor lock; and parsed-UTC temporal proof
    that the matching import occurred at or after export creation.  In advanced
    ``include_unimported`` mode an abandoned export is deletable only with
    complete identity and no lock.  Missing/malformed/incomparable timestamps
    fail closed.
    """
    root_type: str | None = None
    for root, rtype in _export_roots(lab):
        if _strictly_inside(path, root):
            root_type = rtype
            break
    base: dict[str, Any] = {
        "kind": "expanded_export",
        "path": str(path),
        "root_type": root_type,
        "bundle_id": None,
        "campaign_contract_sha256": None,
        "bytes": _recursive_bytes(path),
        "classification": "external",
        "can_delete": False,
        "reason": "export outside managed/legacy export roots",
    }
    if root_type is None:
        return base
    manifest_path = path / "bundle_manifest.json"
    if not manifest_path.is_file():
        base.update({"classification": "manual_review",
                     "reason": "bundle_manifest.json missing"})
        return base
    try:
        manifest = read_json(manifest_path)
    except Exception:
        base.update({"classification": "manual_review",
                     "reason": "bundle_manifest.json did not parse"})
        return base
    bid = str(manifest.get("bundle_id") or "")
    contract_sha = str(manifest.get("campaign_contract_sha256") or "")
    created_at_raw = str(manifest.get("created_at_utc") or "")
    base["bundle_id"] = bid or None
    base["campaign_contract_sha256"] = contract_sha or None
    # An executor lock means the export may still be in active use.
    if (path / ".executor.lock" / "owner.json").is_file():
        base.update({"classification": "locked", "can_delete": False,
                     "reason": "executor lock present; not auto-deleted"})
        return base
    matching = [
        r for r in receipts
        if bid and contract_sha
        and str(r["data"].get("bundle_id") or "") == bid
        and str(r["data"].get("campaign_contract_sha256") or "") == contract_sha
    ]
    if matching:
        created_dt = _parse_utc(created_at_raw)
        import_dts = [
            d for d in (
                _parse_utc(str(r["data"].get("imported_at_utc") or "")) for r in matching
            )
            if d is not None
        ]
        if created_dt is not None and import_dts and max(import_dts) >= created_dt:
            base.update({"classification": "safe_imported", "can_delete": True,
                         "reason": "matching successful import receipt with temporal proof"})
        else:
            base.update({"classification": "temporal_mismatch", "can_delete": False,
                         "reason": "export created after latest matching import, or timestamps missing/malformed"})
        return base
    if include_unimported:
        if bid and contract_sha:
            base.update({"classification": "unimported_export_abandoned", "can_delete": True,
                         "reason": "explicit include_unimported_exports; abandoned unlocked export with complete identity"})
        else:
            base.update({"classification": "incomplete_identity", "can_delete": False,
                         "reason": "unimported export missing bundle_id or campaign_contract_sha256"})
    else:
        base.update({"classification": "no_receipt", "can_delete": False,
                     "reason": "no matching successful import receipt"})
    return base


def cleanup_plan(lab: Laboratory, include_unimported_exports: bool = False) -> dict[str, Any]:
    """Read-only cleanup plan for managed/legacy portable transport artifacts.

    Discovers all current and legacy managed portable artifacts, classifies each
    by provenance, and reports which are safe to delete.  ``runs/`` is never a
    candidate.  Unimported returned archives are never deletable; abandoned
    unimported exports are deletable only with ``include_unimported_exports``.
    """
    receipts = _discover_import_receipts(lab)
    artifacts: list[dict[str, Any]] = []
    for root, _rtype in _return_roots(lab):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_file() and entry.suffix.lower() == ".zip":
                artifacts.append(_classify_returned_archive(entry, lab, receipts))
    for root, _rtype in _export_roots(lab):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / "bundle_manifest.json").is_file():
                artifacts.append(
                    _classify_export(entry, lab, receipts, include_unimported=include_unimported_exports)
                )
    artifacts.sort(key=lambda a: (a["kind"], a["path"]))
    safe = [a for a in artifacts if a["can_delete"]]
    review = [a for a in artifacts if a["classification"] in ("manual_review", "temporal_mismatch")]
    return {
        "artifacts": artifacts,
        "reclaimable_bytes": sum(a["bytes"] for a in safe),
        "artifact_count": len(artifacts),
        "safe_delete_count": len(safe),
        "manual_review_count": len(review),
        "managed_roots": {
            "exports": str(managed_exports_root(lab)),
            "returns": str(managed_returns_root(lab)),
            "legacy_exports": str(legacy_exports_root(lab)),
            "legacy_returns": str(legacy_returns_root(lab)),
        },
        "include_unimported_exports": include_unimported_exports,
        "semantics": (
            "Read-only cleanup plan. Only managed/legacy portable transport artifacts with "
            "proven successful-import provenance are classified as safe_imported. "
            "runs/ is never a candidate."
        ),
    }


def cleanup_execute(lab: Laboratory, include_unimported_exports: bool = False) -> dict[str, Any]:
    """Delete only artifacts proven safe by a fresh cleanup plan.

    Acts only on artifacts returned by a freshly built plan.  Immediately before
    every deletion it re-resolves strict containment, reclassifies the artifact
    from current on-disk state under the same mode, requires it still be
    deletable, and requires current identity/provenance to agree with the
    fresh-plan artifact.  If anything changed between plan and deletion, the
    artifact is skipped and a revalidation failure is reported.  Scientific data
    in ``runs/`` is never touched.
    """
    plan = cleanup_plan(lab, include_unimported_exports=include_unimported_exports)
    receipts = _discover_import_receipts(lab)
    deleted: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for artifact in plan["artifacts"]:
        if not artifact.get("can_delete"):
            continue
        path = Path(artifact["path"])
        roots = (
            _return_roots(lab) if artifact["kind"] == "returned_archive" else _export_roots(lab)
        )
        if not any(_strictly_inside(path, root) for root, _ in roots):
            failures.append({"kind": artifact["kind"], "path": str(path),
                             "reason": "containment_revalidation_failed"})
            continue
        if not path.exists():
            failures.append({"kind": artifact["kind"], "path": str(path),
                             "reason": "already_absent"})
            continue
        # Reclassify from current on-disk state under the same mode.
        if artifact["kind"] == "returned_archive":
            fresh = _classify_returned_archive(path, lab, receipts)
        else:
            fresh = _classify_export(path, lab, receipts, include_unimported=include_unimported_exports)
        if not fresh.get("can_delete"):
            failures.append({"kind": artifact["kind"], "path": str(path),
                             "reason": "revalidation_not_deletable",
                             "classification": fresh.get("classification")})
            continue
        # Require current identity/provenance to agree with the fresh-plan artifact.
        if (
            fresh.get("bundle_id") != artifact.get("bundle_id")
            or fresh.get("campaign_contract_sha256") != artifact.get("campaign_contract_sha256")
        ):
            failures.append({"kind": artifact["kind"], "path": str(path),
                             "reason": "revalidation_identity_changed"})
            continue
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                failures.append({"kind": artifact["kind"], "path": str(path),
                                 "reason": "not_file_or_dir"})
                continue
            deleted.append({"kind": artifact["kind"], "path": str(path), "bytes": artifact["bytes"]})
        except OSError as exc:
            failures.append({"kind": artifact["kind"], "path": str(path), "reason": str(exc)})
    return {
        "deleted": deleted,
        "deleted_count": len(deleted),
        "reclaimed_bytes": sum(d["bytes"] for d in deleted),
        "failures": failures,
        "failure_count": len(failures),
        "include_unimported_exports": include_unimported_exports,
    }


def _post_import_cleanup_preview(
    lab: Laboratory,
    results_resolved: Path,
    contract_sha: str,
    bundle_id: str | None,
    keep_portable_artifacts: bool,
) -> dict[str, Any]:
    """Preview post-import cleanup *candidates* without deleting anything.

    The durable receipt does not exist yet at preview time (it is written after
    a successful import), so full deletion safety (exact hash proof for the
    return archive, temporal proof for exports) cannot be established here.
    Results are therefore reported as **candidates**, not guaranteed deletions.
    Candidate screening uses the same containment + identity + lock rules as
    deletion; the receipt-based proof is applied only during actual cleanup.
    """
    if keep_portable_artifacts:
        return {
            "enabled": False,
            "reason": "keep_portable_artifacts requested",
            "managed_return_candidate": False,
            "matching_export_candidate_count": 0,
        }
    archive_root_type: str | None = None
    for root, rtype in _return_roots(lab):
        if _strictly_inside(results_resolved, root):
            archive_root_type = rtype
            break
    archive_managed = archive_root_type is not None
    candidate_exports = 0
    for root, _rtype in _export_roots(lab):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not (entry / "bundle_manifest.json").is_file():
                continue
            if (entry / ".executor.lock" / "owner.json").is_file():
                continue
            try:
                m = read_json(entry / "bundle_manifest.json")
            except Exception:
                continue
            e_bid = str(m.get("bundle_id") or "")
            e_contract = str(m.get("campaign_contract_sha256") or "")
            if not e_bid or not e_contract:
                continue
            if e_bid != str(bundle_id or "") or e_contract != str(contract_sha or ""):
                continue
            candidate_exports += 1
    return {
        "enabled": True,
        "results_archive_managed": archive_managed,
        "results_archive_root_type": archive_root_type,
        "managed_return_candidate": archive_managed,
        "matching_export_candidate_count": candidate_exports,
        "note": (
            "Candidates only; final deletion requires the durable receipt written "
            "after a successful import to prove exact hash (return archive) and "
            "temporal (export) safety."
        ),
    }


def _cleanup_for_import(lab: Laboratory, receipt: dict[str, Any]) -> dict[str, Any]:
    """Reclaim managed transport artifacts proven safe by a just-written receipt.

    The durable receipt must already be written.  Deletion reclassifies each
    artifact from current on-disk state immediately before unlink/rmtree and
    requires the result to remain deletable with identity agreeing with the
    receipt.  Only the matching returned archive (managed/legacy return root,
    exact non-empty bundle_id equality and exact hash proof) and matching
    expanded exports (contract/temporal/lock proof) are deleted.  A cleanup
    failure does not roll back the already-imported scientific data.
    """
    receipts = _discover_import_receipts(lab)
    receipt_bid = str(receipt.get("bundle_id") or "")
    receipt_contract = str(receipt.get("campaign_contract_sha256") or "")
    deleted: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    archive_path = Path(str(receipt.get("results_archive") or "")).expanduser().resolve()
    if archive_path.is_file():
        cls = _classify_returned_archive(archive_path, lab, receipts)
        if (
            cls["can_delete"]
            and cls.get("bundle_id") == receipt_bid
            and (
                not receipt_contract
                or cls.get("campaign_contract_sha256") == receipt_contract
            )
        ):
            try:
                archive_path.unlink()
                deleted.append({"kind": "returned_archive", "path": str(archive_path),
                                "bytes": cls["bytes"]})
            except OSError as exc:
                failures.append({"kind": "returned_archive", "path": str(archive_path),
                                 "reason": str(exc)})
        elif cls["can_delete"]:
            failures.append({"kind": "returned_archive", "path": str(archive_path),
                             "reason": "identity_does_not_match_receipt"})
    for root, _rtype in _export_roots(lab):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not (entry / "bundle_manifest.json").is_file():
                continue
            cls = _classify_export(entry, lab, receipts, include_unimported=False)
            if not cls["can_delete"]:
                continue
            if cls.get("bundle_id") != receipt_bid or cls.get("campaign_contract_sha256") != receipt_contract:
                continue
            try:
                shutil.rmtree(entry)
                deleted.append({"kind": "expanded_export", "path": str(entry),
                                "bytes": cls["bytes"]})
            except OSError as exc:
                failures.append({"kind": "expanded_export", "path": str(entry),
                                 "reason": str(exc)})
    return {
        "imported": True,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "reclaimed_bytes": sum(d["bytes"] for d in deleted),
        "failures": failures,
        "failure_count": len(failures),
    }


def _has_initial_data_output(case_path: Path) -> bool:
    """Return True when data_output contains at least one file at any depth."""
    data_output = case_path / "data_output"
    return data_output.is_dir() and any(
        path.is_file() for path in data_output.rglob("*")
    )

def export_plan(cases: Path, lab: Laboratory, output: Path | None = None) -> dict[str, Any]:
    cases = cases.expanduser().resolve()
    _fields, rows = load_rows(cases)
    contract = campaign_contract_payload(cases, rows)
    contract_sha = canonical_sha256(contract)
    executor_source = (lab.research_root / "campaign_tools" / "portable_executor.py").resolve()
    core_source, profile_registry = _portable_termination_sources(lab)
    problems: list[dict[str, Any]] = []

    if not executor_source.is_file():
        problems.append({"reason": "portable_executor_not_installed", "path": str(executor_source)})
        executor_sha = "missing"
    else:
        executor_sha = sha256_file(executor_source)
    if not core_source.is_file():
        problems.append({"reason": "quasistationary_core_not_installed", "path": str(core_source)})
        core_sha = "missing"
    else:
        core_sha = sha256_file(core_source)
    if not profile_registry.is_file():
        problems.append({"reason": "termination_profiles_missing", "path": str(profile_registry)})
        registry_sha = "missing"
    else:
        registry_sha = sha256_file(profile_registry)
        try:
            _validate_default_profile(profile_registry)
        except Exception as exc:
            problems.append({"reason": "default_termination_profile_invalid", "error": str(exc)})

    bundle_id = hashlib.sha256(
        f"nrg-bundle-v1:{contract_sha}:{executor_sha}:{core_sha}:{registry_sha}:"
        f"{DEFAULT_TERMINATION_PROFILE}".encode("utf-8")
    ).hexdigest()[:20]
    if output is None:
        output = managed_exports_root(lab) / f"{cases.parent.name}__{bundle_id}"
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        problems.append({"reason": "output_exists_nonempty", "path": str(output)})

    resource_contract = _source_resource_contract(cases)
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
            and _has_initial_data_output(src)
        )
        if not ready:
            problems.append({"case_id": cid, "reason": "case_not_prepared", "path": str(src)})
        else:
            resource_problems = _verify_prepared_resource_contract(src, resource_contract)
            for problem in resource_problems:
                problems.append({"case_id": cid, "reason": "scientific_resource_integrity", "error": problem})
            status_path = src / "run_status.json"
            if status_path.is_file():
                try:
                    current_state = str(read_json(status_path).get("status", "")).lower()
                except Exception:
                    current_state = "invalid_status"
                if current_state in {"running", "pause_failed", "invalid_status"}:
                    problems.append({
                        "case_id": cid, "reason": "case_not_safely_exportable",
                        "status": current_state, "path": str(src),
                    })
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
        "termination": {
            "default_profile": DEFAULT_TERMINATION_PROFILE,
            "profile_registry_sha256": None if registry_sha == "missing" else registry_sha,
            "quasistationary_core_sha256": None if core_sha == "missing" else core_sha,
        },
        "scientific_resource_contract_present": resource_contract is not None,
        "problems": problems[:50],
        "problem_count": len(problems),
        "sample": sample,
        "semantics": (
            "Export copies prepared cases into a self-contained execution bundle, verifies "
            "available scientific-resource provenance, and rewrites only execution locators "
            "to portable relative paths."
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
    core_source, profile_registry = _portable_termination_sources(lab)
    executor_sha = sha256_file(executor_source)
    core_sha = sha256_file(core_source)
    registry_sha = sha256_file(profile_registry)
    _validate_default_profile(profile_registry)
    bundle_id = hashlib.sha256(
        f"nrg-bundle-v1:{contract_sha}:{executor_sha}:{core_sha}:{registry_sha}:"
        f"{DEFAULT_TERMINATION_PROFILE}".encode("utf-8")
    ).hexdigest()[:20]
    if output is None:
        output = managed_exports_root(lab) / f"{cases.parent.name}__{bundle_id}"
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"bundle output already exists and is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "runs").mkdir(exist_ok=True)
    (output / "provenance").mkdir(exist_ok=True)

    resource_contract = _source_resource_contract(cases)
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
            or not _has_initial_data_output(src)
        ):
            raise ValueError(
                f"case {cid} is not execution-ready: prepared task_setup, setup_input.nml "
                "and initial data_output state are required"
            )
        integrity = _verify_prepared_resource_contract(src, resource_contract)
        if integrity:
            raise ValueError(f"case {cid} scientific-resource integrity failed: {'; '.join(integrity)}")
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
        shutil.copy2(source_runtime_manifest, output / "provenance" / "source_runtime_manifest.json")
    shutil.copy2(cases, output / "provenance" / "source_cases.csv")
    shutil.copy2(executor_source, output / "portable_executor.py")
    shutil.copy2(core_source, output / "quasistationary_core.py")
    shutil.copy2(profile_registry, output / "termination_profiles.json")
    (output / "README_RUN.md").write_text(run_readme(), encoding="utf-8")

    source_runtime = {
        "computing_module_sha256": (
            sha256_file(lab.computing_module) if lab.computing_module.is_file() else None
        ),
        "runtime_manifest_sha256": (
            sha256_file(source_runtime_manifest) if source_runtime_manifest.is_file() else None
        ),
        "generation_environment": {
            "computing_module": str(lab.computing_module),
            "runs_root": str(lab.runs_root),
            "laboratory_config": str(lab.config_path),
            "laboratory_local_config": str(lab.local_config_path) if lab.local_config_path else None,
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
        "termination": {
            "default_profile": DEFAULT_TERMINATION_PROFILE,
            "profile_registry_file": "termination_profiles.json",
            "profile_registry_sha256": registry_sha,
            "quasistationary_core_file": "quasistationary_core.py",
            "quasistationary_core_sha256": core_sha,
            "remote_profile_override_supported": True,
        },
        "scientific_resources": resource_contract,
        "source_runtime": source_runtime,
        "cases": case_manifest,
    }
    write_json(output / "bundle_manifest.json", bundle_manifest)
    (output / "execution.local.toml").write_text(
        execution_config_template(source_runtime["computing_module_sha256"]), encoding="utf-8"
    )
    write_export_snapshot_checksums(output)
    write_static_checksums(output, portable_rows)
    return {
        "bundle": str(output),
        "bundle_id": bundle_id,
        "campaign_contract_sha256": contract_sha,
        "case_count": len(rows),
        "portable_executor": str(output / "portable_executor.py"),
        "quasistationary_core": str(output / "quasistationary_core.py"),
        "termination_profiles": str(output / "termination_profiles.json"),
        "default_termination_profile": DEFAULT_TERMINATION_PROFILE,
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


def import_plan(
    cases: Path,
    results: Path,
    lab: Laboratory,
    keep_portable_artifacts: bool = False,
) -> dict[str, Any]:
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

    post_import_cleanup = _post_import_cleanup_preview(
        lab, results, local_contract_sha, result_manifest.get("bundle_id"),
        keep_portable_artifacts,
    )
    return {
        "can_import": len(problems) == 0,
        "cases_csv": str(cases),
        "results_archive": str(results),
        "bundle_id": result_manifest.get("bundle_id"),
        "campaign_contract_sha256": local_contract_sha,
        "case_count_in_results": len(cases_to_import),
        "cases": cases_to_import,
        "keep_portable_artifacts": keep_portable_artifacts,
        "post_import_cleanup": post_import_cleanup,
        "problems": problems[:50],
        "problem_count": len(problems),
        "semantics": (
            "Import overlays verified returned execution artifacts onto the same local logical cases. "
            "It does not regenerate cases or change scientific identity. By default managed transport "
            "artifacts are reclaimed after a successful verified import; keep_portable_artifacts "
            "suppresses cleanup."
        ),
    }


def import_results(
    cases: Path,
    results: Path,
    lab: Laboratory,
    keep_portable_artifacts: bool = False,
) -> dict[str, Any]:
    plan = import_plan(cases, results, lab, keep_portable_artifacts=keep_portable_artifacts)
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
        "import_status": "success",
        "keep_portable_artifacts": keep_portable_artifacts,
    }
    imports_dir = cases.expanduser().resolve().parent / "_imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y%m%dT%H%M%S") + f"__{plan.get('bundle_id','bundle')}.json"
    record_path = imports_dir / name
    # The durable receipt must be written successfully before any automatic
    # transport-artifact deletion begins.  If receipt writing fails, no
    # portable artifact is deleted.
    write_json(record_path, record)
    cleanup_result: dict[str, Any] | None = None
    if not keep_portable_artifacts:
        try:
            cleanup_result = _cleanup_for_import(lab, record)
        except Exception as exc:  # pragma: no cover - defensive guard
            cleanup_result = {
                "imported": True,
                "cleanup_error": f"{type(exc).__name__}: {exc}",
                "deleted": [],
                "deleted_count": 0,
                "reclaimed_bytes": 0,
                "failures": [{"reason": str(exc)}],
                "failure_count": 1,
            }
    # A cleanup failure does not roll back the already-imported scientific data.
    return {**plan, "imported": True, "import_record": str(record_path), "cleanup": cleanup_result}


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
    s.add_argument("--keep-portable-artifacts", action="store_true")
    s = sub.add_parser("import")
    s.add_argument("--cases", type=Path, required=True)
    s.add_argument("--results", type=Path, required=True)
    s.add_argument("--keep-portable-artifacts", action="store_true")
    s = sub.add_parser("cleanup-plan")
    s.add_argument("--include-unimported-exports", action="store_true")
    s = sub.add_parser("cleanup")
    s.add_argument("--include-unimported-exports", action="store_true")
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
        payload = import_plan(args.cases, args.results, lab, keep_portable_artifacts=args.keep_portable_artifacts)
    elif args.command == "import":
        payload = import_results(args.cases, args.results, lab, keep_portable_artifacts=args.keep_portable_artifacts)
    elif args.command == "cleanup-plan":
        payload = cleanup_plan(lab, include_unimported_exports=args.include_unimported_exports)
    elif args.command == "cleanup":
        payload = cleanup_execute(lab, include_unimported_exports=args.include_unimported_exports)
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, indent=2, sort_keys=True))
    ok = payload.get("can_import", payload.get("can_export", True))
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
