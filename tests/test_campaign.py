import csv
from pathlib import Path
import tempfile
import unittest

from nrg_analysis.campaign import Campaign


class CampaignTests(unittest.TestCase):
    def _write_manifest(self, path: Path, case_path: str):
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["case_id", "case_path"])
            writer.writeheader()
            writer.writerow({"case_id": "R000001", "case_path": case_path})

    def test_absolute_case_path_ignores_case_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "runs" / "R000001"
            case.mkdir(parents=True)
            manifest = root / "cases.csv"
            self._write_manifest(manifest, str(case))
            campaign = Campaign.load(manifest, root / "wrong")
            self.assertEqual(campaign.case("R000001").case_path, case.resolve())

    def test_relative_case_path_uses_case_root_and_legacy_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_root = root / "legacy_root"
            case = case_root / "campaign" / "R000001"
            case.mkdir(parents=True)
            manifest = root / "cases.csv"
            self._write_manifest(manifest, "campaign/R000001")
            campaign = Campaign.load(manifest, workspace_root=case_root)
            self.assertEqual(campaign.case("R000001").case_path, case.resolve())
            self.assertEqual(campaign.workspace_root, case_root.resolve())


if __name__ == "__main__":
    unittest.main()
