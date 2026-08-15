from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".pi" / "skills" / "nrg-plot-data" / "SKILL.md"


class PlotSkillV054Tests(unittest.TestCase):
    def test_skill_requires_dense_independent_variable_rows(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("one physical value of the independent variable corresponds to exactly one numerical row", text)
        self.assertIn("must contain exactly 6 numerical rows, not 24 rows", text)
        self.assertIn("must contain exactly 5 numerical rows, not 20 rows", text)
        self.assertIn("Never use zero as a placeholder", text)

    def test_skill_encodes_minimal_header_contract(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn('TITLE = "descriptive title"', text)
        self.assertIn('VARIABLES = "x_variable"', text)
        self.assertIn('ZONE T = "descriptive zone"', text)
        self.assertIn("Do not add any of the following", text)
        for token in ("`N=`", "`E=`", "`DATAPACKING=`", "`I=`", "`F=`"):
            self.assertIn(token, text)

    def test_skill_uses_human_readable_names(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("tau_dTdt_vs_invT_P2atm.dat", text)
        self.assertIn("P2p000atm", text)
        self.assertIn("P2.5atm", text)


if __name__ == "__main__":
    unittest.main()
