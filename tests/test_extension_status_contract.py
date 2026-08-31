from pathlib import Path
import unittest


class ExtensionStatusContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            Path(__file__).resolve().parents[1]
            / ".pi"
            / "extensions"
            / "nrg-laboratory"
            / "index.ts"
        ).read_text(encoding="utf-8")

    def tool_section(self, name: str) -> str:
        marker = f'name: "{name}"'
        start = self.source.index(marker)
        end = self.source.find("  pi.registerTool({", start + len(marker))
        return self.source[start:end if end >= 0 else len(self.source)]

    def test_prepare_uses_full_status_for_gating(self):
        section = self.tool_section("nrg_campaign_prepare_cases")
        self.assertIn(
            'bridge(["campaign-status", "--cases", params.cases, "--full"], signal)',
            section,
        )
        self.assertIn("status.missing_cases", section)

    def test_start_uses_full_status_for_gating(self):
        section = self.tool_section("nrg_campaign_start")
        self.assertIn(
            'bridge(["campaign-status", "--cases", params.cases, "--full"], signal)',
            section,
        )
        self.assertIn("status.missing_cases", section)
        self.assertIn("status.laboratory_runner", section)
        self.assertIn("status.running_cases", section)

    def test_public_status_remains_compact(self):
        section = self.tool_section("nrg_campaign_status")
        self.assertIn(
            'bridge(["campaign-status", "--cases", params.cases], signal)',
            section,
        )
        self.assertNotIn('"--full"', section)


if __name__ == "__main__":
    unittest.main()
