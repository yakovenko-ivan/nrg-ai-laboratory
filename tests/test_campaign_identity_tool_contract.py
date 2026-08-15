from pathlib import Path
import unittest


class CampaignIdentityToolContractTests(unittest.TestCase):
    def test_pi_exposes_reset_identity_and_append_tools(self):
        root = Path(__file__).resolve().parents[1]
        text = (
            root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
        ).read_text(encoding="utf-8")
        for name in (
            'name: "nrg_campaign_identity_inspect"',
            'name: "nrg_campaign_reset_cases"',
            'name: "nrg_campaign_append_preview"',
            'name: "nrg_campaign_append"',
        ):
            self.assertIn(name, text)
        self.assertIn("ctx.ui.confirm", text)
        self.assertIn("attempt-tunable", text)
        self.assertIn("logical case", text)

    def test_runtime_identity_modules_are_installed(self):
        root = Path(__file__).resolve().parents[1]
        for rel in (
            "campaign_tools/campaign_identity.py",
            "campaign_tools/campaign_attempts.py",
            "campaign_tools/campaign_append_0d.py",
        ):
            self.assertTrue((root / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
