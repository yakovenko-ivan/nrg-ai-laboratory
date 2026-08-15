from pathlib import Path
import json
import tempfile
import unittest

from agent_workspace.lab_bridge import inspect_json_file


class CaseInspectionTests(unittest.TestCase):
    def test_zero_byte_json_is_reported_explicitly(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "run_status.json"
            p.write_bytes(b"")
            info = inspect_json_file(p)
            self.assertTrue(info["exists"])
            self.assertEqual(info["size_bytes"], 0)
            self.assertTrue(info["empty"])
            self.assertFalse(info["valid_json"])
            self.assertEqual(info["parse_error"], "empty file")

    def test_whitespace_json_is_distinguished(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "run_status.json"
            p.write_text("  \n\t", encoding="utf-8")
            info = inspect_json_file(p)
            self.assertFalse(info["empty"])
            self.assertTrue(info["whitespace_only"])
            self.assertFalse(info["valid_json"])

    def test_valid_json_is_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "run_status.json"
            p.write_text(json.dumps({"status": "running", "process_pid": 123}), encoding="utf-8")
            info = inspect_json_file(p)
            self.assertTrue(info["valid_json"])
            self.assertEqual(info["data"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
