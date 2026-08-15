from pathlib import Path
import unittest

import agent_workspace.lab_bridge as lb


class SelectiveRerunToolContractTests(unittest.TestCase):
    def test_pi_tool_requires_exact_case_ids_and_hides_runner_config(self):
        root = Path(lb.__file__).resolve().parent.parent
        source = (
            root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
        ).read_text(encoding="utf-8")
        block = source.split('name: "nrg_campaign_rerun_cases"', 1)[1].split(
            'name: "nrg_campaign_job_status"', 1
        )[0]
        self.assertIn("case_ids: Type.Array", block)
        self.assertIn("campaign-rerun-plan", block)
        self.assertIn("campaign-rerun-start", block)
        self.assertNotIn("run_config:", block)
        self.assertIn("ctx.ui.confirm", block)


if __name__ == "__main__":
    unittest.main()
