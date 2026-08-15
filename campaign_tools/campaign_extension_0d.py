#!/usr/bin/env python3
"""Create a small 0D extension campaign from exact cases of a base campaign."""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import sha256_file
from campaign_tools.campaign_generator_0d import (
    NAMELIST_GROUPS, GROUP_KEYS, compute_case_fingerprint, render_namelist, sanitize_token,
)

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Python 3.11+ is required") from exc

EXTENSION_SCHEMA_VERSION = 1
IDENTITY_GROUPS = ("reactor_config", "mixture_config", "physics_config", "output_config")


def _load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"manifest has no header: {path}")
        return list(reader.fieldnames), list(reader)


def _resolve_base_cases(value: str, lab: Laboratory) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = lab.campaign_root / path
    return path.resolve()


def read_extension(path: Path, lab: Laboratory) -> dict[str, Any]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    meta = data.get("extension")
    if not isinstance(meta, dict):
        raise ValueError("extension TOML must contain [extension]")
    for required in ("name", "base_cases", "base_case_ids"):
        if required not in meta:
            raise ValueError(f"[extension].{required} is required")

    ids = [str(x).strip() for x in meta["base_case_ids"]]
    if not ids or any(not x for x in ids):
        raise ValueError("base_case_ids must contain exact non-empty IDs")
    if len(ids) != len(set(ids)):
        raise ValueError("base_case_ids must be unique")
    if len(ids) > int(meta.get("max_cases", 50)):
        raise ValueError("extension exceeds reviewed max_cases")

    overrides = data.get("overrides", {})
    if set(overrides) - {"run_control_config"}:
        raise ValueError("extension may override only run_control_config")
    run_overrides = dict(overrides.get("run_control_config", {}))
    unknown = set(run_overrides) - GROUP_KEYS["run_control_config"]
    if unknown:
        raise ValueError("unsupported run-control override(s): " + ", ".join(sorted(unknown)))

    data["_base_cases_path"] = _resolve_base_cases(str(meta["base_cases"]), lab)
    data["_base_case_ids"] = ids
    return data


def row_groups(row: dict[str, str]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {g: {} for g in NAMELIST_GROUPS}
    for group in NAMELIST_GROUPS:
        prefix = group + "."
        for key, raw in row.items():
            if not key.startswith(prefix) or raw == "":
                continue
            name = key[len(prefix):]
            low = raw.strip().lower()
            if low in {"true", "false"}:
                value: Any = low == "true"
            else:
                try:
                    value = int(raw)
                except ValueError:
                    try:
                        value = float(raw)
                    except ValueError:
                        value = raw
            groups[group][name] = value
    return groups


def load_base_groups(base_cases: Path, row: dict[str, str]) -> dict[str, dict[str, Any]]:
    meta = base_cases.parent / "_setups" / f"{row['case_id']}.json"
    if meta.is_file():
        payload = json.loads(meta.read_text(encoding="utf-8"))
        groups = payload.get("namelists")
        if isinstance(groups, dict):
            return copy.deepcopy(groups)
    return row_groups(row)


def identity_payload(groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {g: copy.deepcopy(groups[g]) for g in IDENTITY_GROUPS}


def canonical_identity_sha256(groups: dict[str, dict[str, Any]]) -> str:
    raw = json.dumps(
        identity_payload(groups), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def extension_output_dir(data: dict[str, Any], lab: Laboratory) -> Path:
    meta = data["extension"]
    setting = meta.get("generator_output_directory")
    if setting:
        path = Path(str(setting)).expanduser()
        if not path.is_absolute():
            path = lab.campaign_root / path
        return path.resolve()
    return (lab.campaign_root / str(meta["name"])).resolve()


def build_cases(data: dict[str, Any], lab: Laboratory) -> list[dict[str, Any]]:
    base_cases: Path = data["_base_cases_path"]
    if not base_cases.is_file():
        raise FileNotFoundError(f"base cases.csv not found: {base_cases}")
    _fields, rows = _load_csv(base_cases)
    by_id = {str(r.get("case_id", "")).strip(): r for r in rows}
    unknown = [cid for cid in data["_base_case_ids"] if cid not in by_id]
    if unknown:
        raise ValueError("base IDs not found: " + ", ".join(unknown))

    meta = data["extension"]
    prefix = str(meta.get("case_id_prefix", "E"))
    width = int(meta.get("case_id_width", 6))
    campaign_id = str(meta["name"])
    run_overrides = dict(data.get("overrides", {}).get("run_control_config", {}))

    result = []
    for n, parent_id in enumerate(data["_base_case_ids"], start=1):
        parent = by_id[parent_id]
        parent_groups = load_base_groups(base_cases, parent)
        groups = copy.deepcopy(parent_groups)
        for key, value in run_overrides.items():
            groups["run_control_config"][key] = value

        extension_id = f"{prefix}{n:0{width}d}"
        parent_dir = str(parent.get("case_directory", parent_id))
        descriptor = parent_dir.split("__", 1)[1] if "__" in parent_dir else parent_id
        case_directory = f"{extension_id}__from_{parent_id}__{sanitize_token(descriptor)}"
        label = f"Extension of {parent_id} | {parent.get('label','')} | product-state completion"

        fp = compute_case_fingerprint(groups)
        groups["case_config"] = copy.deepcopy(groups.get("case_config", {}))
        groups["case_config"].update({
            "case_id": extension_id,
            "case_fingerprint": fp,
            "case_directory": case_directory,
            "case_label": label,
            "campaign_id": campaign_id,
            "results_root": str(lab.runs_root),
            "numerical_variant": parent.get("numerical_variant", "default"),
        })

        result.append({
            "case_id": extension_id,
            "parent_case_id": parent_id,
            "parent_case_fingerprint": parent.get("case_fingerprint", ""),
            "parent_case_path": parent.get("case_path", ""),
            "parent_identity_sha256": canonical_identity_sha256(parent_groups),
            "identity_sha256": canonical_identity_sha256(groups),
            "case_directory": case_directory,
            "label": label,
            "case_fingerprint": fp,
            "groups": groups,
            "numerical_variant": parent.get("numerical_variant", "default"),
        })
    return result


def flatten(item: dict[str, Any], lab: Laboratory, campaign_id: str, base_cases: Path) -> dict[str, Any]:
    case_path = lab.runs_root / campaign_id / item["case_directory"]
    row: dict[str, Any] = {
        "case_id": item["case_id"],
        "case_fingerprint": item["case_fingerprint"],
        "case_directory": item["case_directory"],
        "label": item["label"],
        "case_path": case_path.as_posix(),
        "data_save_path": (case_path / "data_save").as_posix(),
        "data_output_path": (case_path / "data_output").as_posix(),
        "numerical_variant": item["numerical_variant"],
        "extension_parent_case_id": item["parent_case_id"],
        "extension_parent_case_fingerprint": item["parent_case_fingerprint"],
        "extension_parent_case_path": item["parent_case_path"],
        "extension_parent_identity_sha256": item["parent_identity_sha256"],
        "extension_identity_sha256": item["identity_sha256"],
        "extension_base_cases": str(base_cases),
        "extension_base_cases_sha256": sha256_file(base_cases),
        "extension_relation": "supersedes_base_for_product_state",
    }
    for group in NAMELIST_GROUPS:
        for key, value in item["groups"][group].items():
            row[f"{group}.{key}"] = value
    return row


def create_files(data: dict[str, Any], source_toml: Path, cases: list[dict[str, Any]],
                 lab: Laboratory, overwrite: bool) -> Path:
    out_dir = extension_output_dir(data, lab)
    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{out_dir} already exists and is not empty; use --overwrite")
    if out_dir.exists() and overwrite:
        shutil.rmtree(out_dir)
    setups = out_dir / "_setups"
    setups.mkdir(parents=True, exist_ok=True)

    base_cases: Path = data["_base_cases_path"]
    campaign_id = str(data["extension"]["name"])
    (out_dir / "extension.toml").write_text(source_toml.read_text(encoding="utf-8"), encoding="utf-8")

    rows = []
    for item in cases:
        cid = item["case_id"]
        (setups / f"{cid}.nml").write_text(render_namelist(item["groups"]), encoding="utf-8")
        rows.append(flatten(item, lab, campaign_id, base_cases))
        (setups / f"{cid}.json").write_text(json.dumps({
            "case_id": cid,
            "case_fingerprint": item["case_fingerprint"],
            "extension_parent_case_id": item["parent_case_id"],
            "extension_parent_case_fingerprint": item["parent_case_fingerprint"],
            "extension_base_cases": str(base_cases),
            "identity_sha256": item["identity_sha256"],
            "namelists": item["groups"],
        }, indent=2) + "\n", encoding="utf-8")

    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with (out_dir / "cases.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    (out_dir / "extension_manifest.json").write_text(json.dumps({
        "extension_schema_version": EXTENSION_SCHEMA_VERSION,
        "manifest_kind": "execution_extension",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "extension_name": campaign_id,
        "extension_source": str(source_toml),
        "extension_source_sha256": sha256_file(source_toml),
        "base_cases": str(base_cases),
        "base_cases_sha256": sha256_file(base_cases),
        "case_count": len(cases),
        "mapping": {item["case_id"]: item["parent_case_id"] for item in cases},
        "allowed_difference": "run_control_config only",
        "identity_groups": list(IDENTITY_GROUPS),
    }, indent=2) + "\n", encoding="utf-8")
    return out_dir


def preview(data: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    print(f"Extension cases: {len(cases)}")
    print(f"Base cases: {data['_base_cases_path']}")
    print("Mapping:")
    for item in cases:
        print(f"  {item['case_id']} <- {item['parent_case_id']} [{item['identity_sha256'][:12]}]")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("extension", type=Path)
    p.add_argument("--laboratory", type=Path, default=None)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--create", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()

    lab = Laboratory.load(args.laboratory)
    lab.ensure_output_roots()
    source = args.extension.expanduser().resolve()
    data = read_extension(source, lab)
    cases = build_cases(data, lab)
    if args.preview:
        preview(data, cases)
        return 0
    out = create_files(data, source, cases, lab, args.overwrite)
    print(f"Generated extension cases: {len(cases)}")
    print(f"Output directory: {out}")
    print(f"Manifest: {out / 'cases.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
