#!/usr/bin/env python3
"""Template for an agent-authored NRG study.

Put question-specific scientific logic here and reuse ``nrg_analysis`` for
parsing, validation and common calculations.

For large studies, develop the per-case extractor under trusted pilot execution
before full production analysis.  Keep per-case extraction separate from
campaign aggregation and use ``NRG_STUDY_CASE_CACHE_DIR`` for reproducible
intermediate records when appropriate.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tomllib

from nrg_analysis import Campaign
from nrg_analysis.serialization import write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True)
    parser.add_argument("--case-root", required=False, default=None)
    parser.add_argument("--workspace-root", required=False, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    config = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    case_root = args.case_root if args.case_root is not None else args.workspace_root
    campaign = Campaign.load(args.cases, case_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    study_mode = os.environ.get("NRG_STUDY_MODE", "full")
    case_cache_dir = Path(
        os.environ.get("NRG_STUDY_CASE_CACHE_DIR", str(output_dir / "_case_cache"))
    )

    # Recommended large-study architecture:
    #
    #   for case in campaign:
    #       load/recompute one validated structured per-case record
    #       -> case_cache_dir / f"{case.case_id}.json"
    #
    #   aggregate the resulting records into campaign-level tables/figures
    #
    # Catch and record individual per-case analysis failures when continuing is
    # scientifically safe, but do not silently omit failed required cases from
    # the final study conclusion.
    #
    # Replace this template block with the actual study-specific analysis.
    payload = {
        "study": config.get("study", {}),
        "execution_mode": study_mode,
        "case_count": len(campaign),
        "case_cache_dir": str(case_cache_dir),
        "status": "template_only",
    }
    write_json(output_dir / "study_summary.json", payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
