#!/usr/bin/env python3
"""Append genuinely new design points to an existing generated 0D campaign.

Existing case IDs and runtime data are never changed.  Candidate cases are
matched using the campaign-specific logical identity policy.  Only missing
identity points are appended with monotonically increasing case IDs.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
from typing import Any

from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import sha256_file
from campaign_tools.campaign_generator_0d import (
    expand_campaign,
    compute_case_fingerprint,
    create_files as _unused_create_files,
    flatten_case,
    read_campaign,
    render_namelist,
)
from campaign_tools.campaign_identity import (
    attempt_fingerprint,
    build_policy,
    get_value,
    identity_fingerprint,
    load_policy_for_generated,
    protected_constant_fields,
)
from campaign_tools.campaign_attempts import load_base_groups


def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"manifest has no header: {path}")
        return list(reader.fieldnames), list(reader)


def case_number(case_id: str) -> int:
    match = re.search(r"(\d+)$", case_id)
    if not match:
        raise ValueError(f"case ID has no numeric suffix: {case_id}")
    return int(match.group(1))


def legacy_campaign(cases_csv: Path) -> bool:
    manifest = cases_csv.parent / "campaign_manifest.json"
    if not manifest.is_file():
        return True
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return int(payload.get("generator_schema_version", 0)) < 3


def policy_compatible(existing: dict[str, Any], candidate: dict[str, Any]) -> None:
    if list(existing.get("identity_fields", [])) != list(candidate.get("identity_fields", [])):
        raise ValueError(
            "identity schema differs from the existing campaign. "
            "Changing identity axes requires an explicit campaign migration/variant."
        )


def plan(cases_csv: Path, revised_toml: Path, lab: Laboratory) -> dict[str, Any]:
    if not cases_csv.is_file():
        raise FileNotFoundError(cases_csv)
    if not revised_toml.is_file():
        raise FileNotFoundError(revised_toml)

    existing_policy = load_policy_for_generated(cases_csv)
    data = read_campaign(revised_toml)
    candidate_policy = build_policy(data)
    policy_compatible(existing_policy, candidate_policy)
    candidates = expand_campaign(data, lab)

    _fields, existing_rows = load_csv(cases_csv)
    existing_by_identity: dict[str, dict[str, Any]] = {}
    existing_groups: dict[str, dict[str, dict[str, Any]]] = {}
    for row in existing_rows:
        cid = row["case_id"]
        groups = load_base_groups(cases_csv, cid)
        fp = identity_fingerprint(groups, existing_policy)
        if fp in existing_by_identity:
            raise ValueError(f"duplicate logical identity in existing campaign: {cid}")
        existing_by_identity[fp] = row
        existing_groups[cid] = groups

    baseline = existing_groups[existing_rows[0]["case_id"]]
    protected = protected_constant_fields(baseline, existing_policy)

    new_items = []
    already = []
    conflicts = []
    for item in candidates:
        fp = identity_fingerprint(item["groups"], existing_policy)
        if fp in existing_by_identity:
            existing_row = existing_by_identity[fp]
            existing_group = existing_groups[existing_row["case_id"]]
            changed_constants = []
            for field in protected:
                try:
                    old = get_value(existing_group, field)
                    new = get_value(item["groups"], field)
                except KeyError:
                    continue
                if old != new:
                    changed_constants.append({"field": field, "existing": old, "candidate": new})
            if changed_constants:
                conflicts.append({
                    "candidate_case_id": item["case_id"],
                    "existing_case_id": existing_row["case_id"],
                    "identity_fingerprint": fp,
                    "protected_constant_changes": changed_constants,
                })
            else:
                already.append({
                    "candidate_case_id": item["case_id"],
                    "existing_case_id": existing_row["case_id"],
                    "identity_fingerprint": fp,
                })
            continue

        changed_constants = []
        for field in protected:
            try:
                old = get_value(baseline, field)
                new = get_value(item["groups"], field)
            except KeyError:
                continue
            if old != new:
                changed_constants.append({"field": field, "existing": old, "candidate": new})
        if changed_constants:
            conflicts.append({
                "candidate_case_id": item["case_id"],
                "identity_fingerprint": fp,
                "protected_constant_changes": changed_constants,
            })
        else:
            new_items.append(item)

    return {
        "cases_csv": str(cases_csv),
        "revised_campaign": str(revised_toml),
        "identity_policy": existing_policy,
        "existing_case_count": len(existing_rows),
        "candidate_design_count": len(candidates),
        "already_present_count": len(already),
        "new_case_count": len(new_items),
        "conflict_count": len(conflicts),
        "already_present": already[:20],
        "new_identity_fingerprints": [
            identity_fingerprint(x["groups"], existing_policy) for x in new_items
        ],
        "new_case_preview": [
            {
                "candidate_label": x["label"],
                "identity": {
                    field: get_value(x["groups"], field)
                    for field in existing_policy["identity_fields"]
                },
            }
            for x in new_items[:30]
        ],
        "conflicts": conflicts[:20],
        "_new_items": new_items,
        "_candidate_data": data,
    }


def append(cases_csv: Path, revised_toml: Path, lab: Laboratory) -> dict[str, Any]:
    p = plan(cases_csv, revised_toml, lab)
    if p["conflict_count"]:
        raise ValueError("append blocked by protected campaign-constant changes")
    new_items = p.pop("_new_items")
    p.pop("_candidate_data", None)
    if not new_items:
        return {**p, "appended_case_count": 0, "message": "no new identities to append"}

    fieldnames, rows = load_csv(cases_csv)
    existing_policy = p["identity_policy"]
    is_legacy = legacy_campaign(cases_csv)

    max_number = max(case_number(row["case_id"]) for row in rows)
    prefix_match = re.match(r"^(.*?)(\d+)$", rows[0]["case_id"])
    if not prefix_match:
        raise ValueError("cannot infer case ID prefix")
    prefix = prefix_match.group(1)
    width = len(prefix_match.group(2))
    campaign_name = str(rows[0].get("case_config.campaign_id") or cases_csv.parent.name)

    added_rows = []
    setup_dir = cases_csv.parent / "_setups"
    revision_root = cases_csv.parent / "_revisions"
    revision_root.mkdir(parents=True, exist_ok=True)
    existing_revs = [
        int(m.group(1))
        for child in revision_root.iterdir()
        if child.is_dir() and (m := re.match(r"rev(\d+)$", child.name))
    ]
    rev = max(existing_revs, default=0) + 1
    rev_dir = revision_root / f"rev{rev:03d}"
    rev_dir.mkdir(parents=True)

    for offset, source_item in enumerate(new_items, start=1):
        item = copy.deepcopy(source_item)
        cid = f"{prefix}{max_number + offset:0{width}d}"
        old_candidate_id = item["case_id"]
        descriptor = item["case_directory"].split("__", 1)[1] if "__" in item["case_directory"] else item["case_directory"]
        item["case_id"] = cid
        item["case_directory"] = f"{cid}__{descriptor}"
        c = item["groups"]["case_config"]
        c["case_id"] = cid
        c["case_directory"] = item["case_directory"]
        c["campaign_id"] = campaign_name
        c["results_root"] = str(lab.runs_root)

        logical_fp = identity_fingerprint(item["groups"], existing_policy)
        # Preserve legacy per-case fingerprint semantics inside an already-generated
        # legacy campaign so runner/setup verification remains homogeneous enough for
        # old tooling. Logical identity is always explicit in the new sidecar/column.
        case_fp = (
            compute_case_fingerprint(item["groups"])
            if is_legacy
            else logical_fp
        )
        c["case_fingerprint"] = case_fp
        item["case_fingerprint"] = case_fp
        item["case_identity_fingerprint"] = logical_fp
        item["generated_attempt_fingerprint"] = attempt_fingerprint(item["groups"])
        item["fingerprint_scheme"] = (
            "legacy_full_configuration_v1"
            if is_legacy
            else "campaign_identity_v1"
        )

        (setup_dir / f"{cid}.nml").write_text(render_namelist(item["groups"]), encoding="utf-8")
        metadata = {
            "case_id": cid,
            "case_fingerprint": case_fp,
            "case_identity_fingerprint": logical_fp,
            "generated_attempt_fingerprint": item["generated_attempt_fingerprint"],
            "fingerprint_scheme": item["fingerprint_scheme"],
            "case_directory": item["case_directory"],
            "label": item["label"],
            "numerical_variant": item["numerical_variant"],
            "numerical_variant_description": item["numerical_variant_description"],
            "case_path": (lab.runs_root / campaign_name / item["case_directory"]).as_posix(),
            "namelists": item["groups"],
            "appended_in_revision": f"rev{rev:03d}",
            "candidate_case_id_before_append": old_candidate_id,
        }
        (setup_dir / f"{cid}.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

        row = flatten_case(item)
        row["case_identity_fingerprint"] = logical_fp
        row["generated_attempt_fingerprint"] = item["generated_attempt_fingerprint"]
        row["fingerprint_scheme"] = item["fingerprint_scheme"]
        row["campaign_revision"] = f"rev{rev:03d}"
        rows.append(row)
        added_rows.append(row)

    # Add identity sidecar columns for old rows without changing their case fingerprint.
    for row in rows:
        cid = row["case_id"]
        if not row.get("case_identity_fingerprint"):
            groups = load_base_groups(cases_csv, cid)
            row["case_identity_fingerprint"] = identity_fingerprint(groups, existing_policy)
            row["fingerprint_scheme"] = row.get("fingerprint_scheme") or "legacy_full_configuration_v1"
        row.setdefault("campaign_revision", "rev000")

    all_fields = list(fieldnames)
    for row in rows:
        for key in row:
            if key not in all_fields:
                all_fields.append(key)
    with cases_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    with (rev_dir / "added_cases.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(added_rows)
    shutil.copy2(revised_toml, rev_dir / "campaign_definition.toml")

    revision_record = {
        "revision": f"rev{rev:03d}",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "revised_definition": str(revised_toml),
        "revised_definition_sha256": sha256_file(revised_toml),
        "previous_case_count": p["existing_case_count"],
        "appended_case_count": len(added_rows),
        "new_case_ids": [r["case_id"] for r in added_rows],
        "identity_policy": existing_policy,
    }
    (rev_dir / "revision_manifest.json").write_text(
        json.dumps(revision_record, indent=2) + "\n", encoding="utf-8"
    )

    manifest_path = cases_csv.parent / "campaign_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    manifest["case_count"] = len(rows)
    manifest["identity_policy"] = existing_policy
    manifest["latest_revision"] = f"rev{rev:03d}"
    manifest["revisions_root"] = str(revision_root)
    manifest["case_identity_fingerprints"] = {
        row["case_id"]: row["case_identity_fingerprint"] for row in rows
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return {
        **p,
        "appended_case_count": len(added_rows),
        "new_case_ids": [r["case_id"] for r in added_rows],
        "new_total_case_count": len(rows),
        "revision": f"rev{rev:03d}",
        "revision_directory": str(rev_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--laboratory", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--append", action="store_true")
    args = parser.parse_args()

    lab = Laboratory.load(args.laboratory)
    cases = args.cases.expanduser().resolve()
    campaign = args.campaign.expanduser().resolve()
    result = plan(cases, campaign, lab) if args.preview else append(cases, campaign, lab)
    result.pop("_new_items", None)
    result.pop("_candidate_data", None)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
