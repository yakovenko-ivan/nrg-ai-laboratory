#!/usr/bin/env python3
"""Build an analysis-only composite campaign from base and extension manifests."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any

from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import sha256_file


def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"manifest has no header: {path}")
        return list(reader.fieldnames), list(reader)


def read_status(case_path: Path) -> dict[str, Any]:
    status = case_path / "run_status.json"
    if not status.is_file():
        raise ValueError(f"extension case missing run_status.json: {case_path}")
    payload = json.loads(status.read_text(encoding="utf-8"))
    if payload.get("status") != "condition_met":
        raise ValueError(
            f"extension case has not reached condition_met: {case_path.name}: "
            f"{payload.get('status')!r}"
        )
    if payload.get("physical_condition_met") is not True:
        raise ValueError(f"extension case lacks physical_condition_met=true: {case_path.name}")
    return payload


def validate_extension_row(base: dict[str, str], ext: dict[str, str]) -> None:
    parent = ext.get("extension_parent_case_id", "")
    if parent != base.get("case_id", ""):
        raise ValueError(f"extension-parent mismatch: {parent} != {base.get('case_id')}")
    if ext.get("extension_parent_case_fingerprint", "") != base.get("case_fingerprint", ""):
        raise ValueError(f"base fingerprint mismatch for parent {parent}")
    if ext.get("extension_parent_identity_sha256", "") != ext.get("extension_identity_sha256", ""):
        raise ValueError(f"identity differs between base and extension for {parent}")

    for prefix in ("reactor_config.", "mixture_config.", "physics_config.", "output_config."):
        keys = {k for k in base if k.startswith(prefix)} | {k for k in ext if k.startswith(prefix)}
        for key in keys:
            if str(base.get(key, "")) != str(ext.get(key, "")):
                raise ValueError(
                    f"identity mismatch for {parent}: {key}: "
                    f"base={base.get(key)!r}, extension={ext.get(key)!r}"
                )


def build(base_cases: Path, extension_cases: Path, output_dir: Path,
          lab: Laboratory, overwrite: bool) -> Path:
    _bf, base_rows = load_csv(base_cases)
    _ef, ext_rows = load_csv(extension_cases)
    base_by_id = {r.get("case_id", ""): r for r in base_rows}
    if len(base_by_id) != len(base_rows):
        raise ValueError("base campaign contains duplicate IDs")

    ext_by_parent: dict[str, dict[str, str]] = {}
    for ext in ext_rows:
        parent = ext.get("extension_parent_case_id", "")
        if not parent:
            raise ValueError(f"extension row lacks parent link: {ext.get('case_id')}")
        if parent in ext_by_parent:
            raise ValueError(f"multiple extension rows target {parent}")
        if parent not in base_by_id:
            raise ValueError(f"extension targets unknown base case {parent}")
        validate_extension_row(base_by_id[parent], ext)
        read_status(Path(ext["case_path"]).expanduser().resolve())
        ext_by_parent[parent] = ext

    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{output_dir} exists and is not empty; use --overwrite")
    if output_dir.exists() and overwrite:
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for base in base_rows:
        logical_id = base["case_id"]
        source = ext_by_parent.get(logical_id)
        chosen = dict(source if source is not None else base)
        source_case_id = chosen["case_id"]
        chosen["case_id"] = logical_id
        chosen["composite_logical_case_id"] = logical_id
        chosen["composite_source_role"] = "extension" if source is not None else "base"
        chosen["composite_source_case_id"] = source_case_id
        chosen["composite_source_case_path"] = chosen.get("case_path", "")
        chosen["composite_source_case_fingerprint"] = chosen.get("case_fingerprint", "")
        chosen["composite_base_case_id"] = logical_id
        chosen["composite_base_case_fingerprint"] = base.get("case_fingerprint", "")
        chosen["composite_selection_reason"] = (
            "physical_extension_replacement" if source is not None else "base_history_retained"
        )
        rows.append(chosen)

    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with (output_dir / "cases.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    (output_dir / "composite_manifest.json").write_text(json.dumps({
        "composite_schema_version": 1,
        "manifest_kind": "analysis_only_composite",
        "analysis_only": True,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_cases": str(base_cases),
        "base_cases_sha256": sha256_file(base_cases),
        "extension_cases": str(extension_cases),
        "extension_cases_sha256": sha256_file(extension_cases),
        "logical_case_count": len(rows),
        "base_source_count": len(rows) - len(ext_by_parent),
        "extension_source_count": len(ext_by_parent),
        "replacement_mapping": {
            parent: ext["case_id"] for parent, ext in sorted(ext_by_parent.items())
        },
        "raw_data_copied": False,
        "execution_forbidden": True,
    }, indent=2) + "\n", encoding="utf-8")
    return output_dir


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-cases", type=Path, required=True)
    p.add_argument("--extension-cases", type=Path, required=True)
    p.add_argument("--output-name", required=True)
    p.add_argument("--laboratory", type=Path, default=None)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()

    lab = Laboratory.load(args.laboratory)
    out = build(
        args.base_cases.expanduser().resolve(),
        args.extension_cases.expanduser().resolve(),
        (lab.campaign_root / "_composites" / args.output_name).resolve(),
        lab,
        args.overwrite,
    )
    payload = json.loads((out / "composite_manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"output_directory": str(out), "cases_csv": str(out / "cases.csv"), **payload}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
