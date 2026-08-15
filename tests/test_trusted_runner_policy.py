from pathlib import Path
import unittest

import agent_workspace.lab_bridge as lb


class TrustedRunnerPolicyTests(unittest.TestCase):
    def test_pi_campaign_start_does_not_expose_run_config(self):
        root = Path(lb.__file__).resolve().parent.parent
        source = (root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts").read_text(encoding="utf-8")
        block = source.split('name: "nrg_campaign_start"', 1)[1].split('name: "nrg_campaign_job_status"', 1)[0]
        self.assertNotIn("params.run_config", block)
        self.assertNotIn('run_config: Type.String', block)

    def test_pi_campaign_status_does_not_expose_run_config(self):
        root = Path(lb.__file__).resolve().parent.parent
        source = (root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts").read_text(encoding="utf-8")
        block = source.split('name: "nrg_campaign_status"', 1)[1].split('name: "nrg_case_inspect"', 1)[0]
        self.assertNotIn("params.run_config", block)
        self.assertNotIn("run_config:", block)

    def test_canonical_runner_policy_exists(self):
        root = Path(lb.__file__).resolve().parent.parent
        self.assertTrue((root / "config" / "campaign_runner.json").is_file())


if __name__ == "__main__":
    unittest.main()
