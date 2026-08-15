from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
AGENTS = ROOT / "AGENTS.md"


class ToolActivationHotfixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = INDEX.read_text(encoding="utf-8")
        cls.agents = AGENTS.read_text(encoding="utf-8")

    def test_physical_start_tools_are_registered(self):
        self.assertIn('name: "nrg_campaign_start_to_quasistationary"', self.index)
        self.assertIn('name: "nrg_campaign_run_cases_to_quasistationary"', self.index)

    def test_session_start_explicitly_activates_all_nrg_tools(self):
        self.assertIn('filter((name) => name.startsWith("nrg_"))', self.index)
        self.assertIn('for (const name of registeredNrgTools) active.add(name);', self.index)
        self.assertIn('pi.setActiveTools([...active]);', self.index)
        self.assertIn('missingNrgTools', self.index)

    def test_diagnostic_command_is_registered(self):
        self.assertIn('pi.registerCommand("nrg-tools"', self.index)
        for name in (
            "nrg_campaign_start_to_quasistationary",
            "nrg_campaign_run_cases_to_quasistationary",
            "nrg_campaign_stop",
            "nrg_case_stop",
        ):
            self.assertIn(f'"{name}"', self.index)

    def test_generic_start_is_not_physical_fallback(self):
        self.assertIn(
            "Never use nrg_campaign_start as a fallback when the requested execution requires a trusted physical termination profile.",
            self.index,
        )
        self.assertIn(
            "If the required physical-start tool is unavailable, report the missing capability and do not launch CFD through the generic start path.",
            self.index,
        )

    def test_agents_policy_matches_runtime_semantics(self):
        self.assertIn("Never substitute `nrg_campaign_start`", self.agents)
        self.assertIn("`/nrg-tools`", self.agents)
        self.assertIn("explicitly activates every registered `nrg_*` tool", self.agents)


if __name__ == "__main__":
    unittest.main()
