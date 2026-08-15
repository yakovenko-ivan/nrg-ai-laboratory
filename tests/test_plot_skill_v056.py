from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".pi" / "skills" / "nrg-plot-data" / "SKILL.md"


class PlotSkillV056Tests(unittest.TestCase):
    def test_filename_is_not_sufficient_semantic_context(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("filename is useful navigation metadata, but it is not sufficient scientific metadata", text)
        self.assertIn('TITLE = "Ignition delay tau_dTdt vs inverse temperature, P0=1atm"', text)
        self.assertIn('ZONE T = "tau_dTdt, P0=1atm"', text)

    def test_bare_mechanisms_allowed_only_with_metric_context(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("compact bare mechanism names are acceptable", text)
        self.assertIn("Generic text such as `Ignition delay` is not sufficient", text)
        self.assertIn("validate_semantic_context()", text)


if __name__ == "__main__":
    unittest.main()
