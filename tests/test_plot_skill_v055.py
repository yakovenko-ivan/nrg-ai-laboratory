from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".pi" / "skills" / "nrg-plot-data" / "SKILL.md"


class PlotSkillV055Tests(unittest.TestCase):
    def test_mechanism_variables_are_self_describing(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn('"tau_dTdt_Konnov"', text)
        self.assertIn('"tau_dTdt_Keromnes"', text)
        self.assertIn('"tau_dTdt_Tereza"', text)
        self.assertIn('"tau_dTdt_Zhang"', text)
        self.assertIn("Preferred self-describing variable names", text)

    def test_metric_comparison_splits_hidden_mechanism_dimension(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("split the output by mechanism", text)
        self.assertIn("ignition_metrics_vs_T0_Konnov_P5atm.dat", text)
        self.assertIn("Do not emit repeated x values whose distinction depends on row order", text)

    def test_interpretive_ranking_filename_is_discouraged(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("tau_dTdt_vs_pressure_T1500K.dat", text)
        self.assertIn("mechanism_ranking_T1500K.dat", text)
        self.assertIn("Ranking itself belongs in structured tables", text)


if __name__ == "__main__":
    unittest.main()
