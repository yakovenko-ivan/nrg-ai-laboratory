from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_workspace import run_study
from agent_workspace import lab_bridge


class StagedStudyAnalysisTests(unittest.TestCase):
    def test_filtered_cases_csv_preserves_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "cases.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["case_id", "x"])
                writer.writeheader()
                for case_id, x in (("R1", 1), ("R2", 2), ("R3", 3), ("R4", 4), ("R5", 5)):
                    writer.writerow({"case_id": case_id, "x": x})
            dest = root / "pilot" / "selected_cases.csv"
            selected = run_study._write_filtered_cases_csv(source, dest, ["R5", "R2"])
            self.assertEqual(selected, ["R2", "R5"])
            with dest.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["case_id"] for row in rows], ["R2", "R5"])

    def test_pilot_marker_is_invalidated_by_analysis_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            analyze = root / "analyze.py"
            config = root / "analysis_config.toml"
            cases = root / "cases.csv"
            marker = root / "pilot_validation.json"
            analyze.write_text("print('v1')\n", encoding="utf-8")
            config.write_text("[study]\nname='x'\n", encoding="utf-8")
            cases.write_text("case_id\nR1\nR2\nR3\nR4\nR5\nR6\n", encoding="utf-8")
            hashes = run_study._analysis_hashes(analyze, config, cases)
            marker.write_text(json.dumps({
                "schema_version": run_study.PILOT_MARKER_SCHEMA,
                "status": "validated",
                "campaign_case_count": 6,
                "selected_count": 5,
                **hashes,
            }), encoding="utf-8")
            current = run_study._pilot_marker_status(
                marker,
                analyze_path=analyze,
                config_path=config,
                cases_csv=cases,
                campaign_case_count=6,
            )
            self.assertTrue(current["valid"])
            analyze.write_text("print('v2')\n", encoding="utf-8")
            stale = run_study._pilot_marker_status(
                marker,
                analyze_path=analyze,
                config_path=config,
                cases_csv=cases,
                campaign_case_count=6,
            )
            self.assertFalse(stale["valid"])
            self.assertIn("analysis_script_sha256_changed", stale["reason"])

    def test_representative_plan_uses_identity_axes_not_first_n(self) -> None:
        rows = []
        case_number = 1
        for x in (0.0, 0.5, 1.0):
            for mechanism in ("A", "B", "C", "D"):
                rows.append({
                    "case_id": f"R{case_number:03d}",
                    "axis.x": str(x),
                    "axis.mechanism": mechanism,
                })
                case_number += 1
        policy = {"identity_fields": ["axis.x", "axis.mechanism"]}
        with patch.object(lab_bridge, "load_policy_for_generated", return_value=policy):
            plan = lab_bridge._representative_pilot_selection(Path("cases.csv"), rows, 8)
        self.assertTrue(plan["can_pilot"])
        self.assertEqual(plan["selected_count"], 8)
        selected = plan["selected_identity_values"]
        self.assertEqual({row["axis.mechanism"] for row in selected}, {"A", "B", "C", "D"})
        self.assertEqual({str(row["axis.x"]) for row in selected}, {"0.0", "0.5", "1.0"})
        self.assertNotEqual(plan["selected_case_ids"], [f"R{i:03d}" for i in range(1, 9)])

    def test_720_like_plan_covers_all_discrete_axis_levels(self) -> None:
        rows = []
        case_number = 1
        for h2 in (10, 20, 30, 40, 50, 60):
            for temp in (1000, 1100, 1200, 1300, 1400, 1500):
                for pressure in (1, 2, 3, 4, 5):
                    for mechanism in ("KONNOV", "KEROMNES", "TEREZA", "ZHANG"):
                        rows.append({
                            "case_id": f"R{case_number:06d}",
                            "mixture.H2": str(h2),
                            "mixture.T": str(temp),
                            "mixture.P": str(pressure),
                            "physics.mechanism": mechanism,
                        })
                        case_number += 1
        policy = {"identity_fields": ["mixture.H2", "mixture.T", "mixture.P", "physics.mechanism"]}
        with patch.object(lab_bridge, "load_policy_for_generated", return_value=policy):
            plan = lab_bridge._representative_pilot_selection(Path("cases.csv"), rows, 20)
        self.assertEqual(plan["selected_count"], 20)
        coverage = {axis["field"]: axis for axis in plan["identity_axes"]}
        for field in policy["identity_fields"]:
            self.assertEqual(coverage[field]["selected_unique_count"], coverage[field]["campaign_unique_count"])



if __name__ == "__main__":
    unittest.main()
