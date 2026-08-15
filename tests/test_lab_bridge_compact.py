from pathlib import Path
import unittest

import agent_workspace.lab_bridge as lb


class CompactPreparationContractTests(unittest.TestCase):
    def test_source_keeps_full_records_on_disk_but_not_in_tool_summary(self):
        source = Path(lb.__file__).read_text(encoding="utf-8")
        self.assertIn('"records": records,', source)
        self.assertIn('write_json(summary_path, audit_summary)', source)
        self.assertIn('"record_counts": dict(sorted(record_counts.items()))', source)
        self.assertIn('"problem_cases": problem_records', source)
        tool_block = source.split('tool_summary = {', 1)[1].split('return emit(tool_summary', 1)[0]
        self.assertNotIn('"records": records', tool_block)


if __name__ == "__main__":
    unittest.main()
