import ast
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "agent_workspace" / "lab_bridge.py"
INDEX = ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
AGENTS = ROOT / "AGENTS.md"


def load_function(name, namespace):
    source = BRIDGE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(BRIDGE), "exec"), namespace)
    return namespace[name]


class PhysicalCampaignResumeRegressionTests(unittest.TestCase):
    def test_status_payload_has_opt_in_runnable_id_enumeration(self):
        text = BRIDGE.read_text(encoding="utf-8")
        self.assertIn("include_runnable_case_ids: bool = False", text)
        self.assertIn('payload["runnable_case_ids"] = runnable_case_ids', text)
        self.assertNotIn('list(status.get("runnable_cases", []))', text)

    def test_campaign_wide_plan_handles_719_runnable_cases(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            config = td / "runner.json"
            config.write_text('{"threads":1,"max_concurrent_cases":1,"skip_statuses":["finished","condition_met"]}', encoding="utf-8")
            rows = [
                {
                    "case_id": f"R{i:06d}",
                    "run_control_config.termination_mode": "wall_time",
                }
                for i in range(1, 721)
            ]
            runnable_ids = [f"R{i:06d}" for i in range(2, 721)]

            ns = {
                "Path": Path,
                "Laboratory": object,
                "Any": object,
                "physical_profile_payload": lambda lab, name: (
                    SimpleNamespace(required_run_control_modes=["wall_time"]),
                    {"name": name},
                ),
                "load_cases": lambda cases: rows,
                "campaign_status_payload": lambda cases, lab, cfg, include_runnable_case_ids=False: {
                    "runnable_cases": 719,
                    "runnable_case_ids": runnable_ids if include_runnable_case_ids else None,
                },
                "effective_field_value": lambda cases, cid, field: "wall_time",
                "probe_runner_lock": lambda root: {"active": False},
                "read_json": lambda p: {"threads": 1, "max_concurrent_cases": 1, "skip_statuses": ["finished", "condition_met"]},
                "sha256_file": lambda p: "dummysha",
                "set": set,
                "str": str,
                "len": len,
                "list": list,
                "int": int,
            }
            fn = load_function("physical_launch_plan", ns)
            lab = SimpleNamespace(runner_config=config, campaign_root=td, default_threads=1)
            plan = fn(td / "cases.csv", None, "0d_cv_post_ignition_quasistationary_v1", lab)
            self.assertTrue(plan["can_start"])
            self.assertEqual(plan["selection_mode"], "all_runnable")
            self.assertEqual(plan["selected_count"], 719)
            self.assertTrue(plan["selected_case_ids_truncated"])
            self.assertEqual(len(plan["selected_case_ids"]), 50)
            self.assertEqual(plan["other_cases_not_scheduled"], 1)

    def test_tool_guidance_forbids_batching_full_campaign(self):
        text = INDEX.read_text(encoding="utf-8")
        self.assertIn("campaign-wide start/resume tool", text)
        self.assertIn("Do not partition a full campaign into selected-case batches", text)
        self.assertIn("Never use repeated calls to partition a full campaign", text)

    def test_agents_documents_campaign_wide_resume_semantics(self):
        text = AGENTS.read_text(encoding="utf-8")
        self.assertIn("campaign-wide start/resume operation", text)
        self.assertIn("must not be called repeatedly to partition a full campaign into batches", text)


if __name__ == "__main__":
    unittest.main()
