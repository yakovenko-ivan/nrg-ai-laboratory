"""Create a new agent-editable study from the controlled template."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil

from nrg_analysis.laboratory import Laboratory


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    value = value.strip("_")
    if not value:
        raise ValueError("empty study slug")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument(
        "--laboratory",
        help="path to laboratory.toml; defaults to config/laboratory.toml or NRG_LABORATORY_CONFIG",
    )
    parser.add_argument(
        "--case-root",
        help="fallback root for legacy cases.csv files with relative case_path values; defaults to laboratory runs_root",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing study directory")
    args = parser.parse_args()

    lab = Laboratory.load(args.laboratory)
    lab.ensure_output_roots()

    module_root = Path(__file__).resolve().parent
    template = module_root / "studies" / "_template"
    if not template.is_dir():
        raise SystemExit(f"controlled study template not found: {template}")

    destination = lab.studies_root / slugify(args.slug)
    if destination.exists():
        if not args.force:
            raise SystemExit(f"study already exists: {destination}")
        shutil.rmtree(destination)
    shutil.copytree(template, destination)

    cases = Path(args.cases).expanduser().resolve()
    if not cases.is_file():
        raise SystemExit(f"cases.csv not found: {cases}")
    case_root = (
        Path(args.case_root).expanduser().resolve()
        if args.case_root is not None
        else lab.runs_root
    )

    manifest = {
        "study_schema_version": 2,
        "study_id": destination.name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scientific_request": args.request,
        "laboratory_config": str(lab.config_path),
        "laboratory_local_config": (str(lab.local_config_path) if lab.local_config_path else None),
        "cases_csv": str(cases),
        "case_root": str(case_root),
        "status": "draft",
    }
    (destination / "study_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    study_md = destination / "STUDY.md"
    text = study_md.read_text(encoding="utf-8")
    replacements = {
        "{{STUDY_ID}}": destination.name,
        "{{SCIENTIFIC_REQUEST}}": args.request,
        "{{CASES_CSV}}": str(cases),
        "{{CASE_ROOT}}": str(case_root),
        "{{WORKSPACE_ROOT}}": str(case_root),  # compatibility with the v1 template
        "{{LABORATORY_CONFIG}}": str(lab.config_path),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    study_md.write_text(text, encoding="utf-8")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
