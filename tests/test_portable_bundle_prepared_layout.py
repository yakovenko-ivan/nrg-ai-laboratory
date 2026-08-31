from pathlib import Path
import tempfile
import unittest

from campaign_tools.portable_bundle import _has_initial_data_output


class PortableBundlePreparedLayoutTests(unittest.TestCase):
    def test_accepts_nrg_nested_initial_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "R000001"
            initial = case / "data_output" / "000"
            initial.mkdir(parents=True)
            (initial / "mesh.dat").write_text("mesh", encoding="utf-8")
            self.assertTrue(_has_initial_data_output(case))

    def test_accepts_direct_file_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "R000001"
            data_output = case / "data_output"
            data_output.mkdir(parents=True)
            (data_output / "initial.dat").write_text("state", encoding="utf-8")
            self.assertTrue(_has_initial_data_output(case))

    def test_rejects_empty_nested_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "R000001"
            (case / "data_output" / "000").mkdir(parents=True)
            self.assertFalse(_has_initial_data_output(case))

    def test_rejects_missing_data_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "R000001"
            case.mkdir(parents=True)
            self.assertFalse(_has_initial_data_output(case))


if __name__ == "__main__":
    unittest.main()
