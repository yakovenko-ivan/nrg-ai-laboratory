from __future__ import annotations

import unittest

from agent_workspace.lab_bridge import (
    build_parser,
    compact_campaign_start,
    compact_campaign_status,
    compact_job_status,
    compact_operator_plan,
    compact_operator_result,
)


class CompactBridgeOutputTests(unittest.TestCase):
    def test_campaign_status_drops_forensic_bulk(self):
        full = {
            "cases_csv": "/tmp/cases.csv",
            "total_cases": 2,
            "counts": {"running": 1, "not_started": 1},
            "runnable_cases": 1,
            "completed_like_cases": 0,
            "missing_cases": 0,
            "invalid_status_cases": [],
            "stale_running_cases": 0,
            "execution_policy": {"run_config_sha256": "x" * 64},
            "sample": [{"case_id": "R000001"}],
            "laboratory_runner": {"active": True, "owner": {"runner_pid": 123}},
        }
        compact = compact_campaign_status(full)
        self.assertTrue(compact["ok"])
        self.assertEqual(compact["counts"], {"running": 1, "not_started": 1})
        self.assertEqual(compact["runner"]["pid"], 123)
        self.assertNotIn("execution_policy", compact)
        self.assertNotIn("sample", compact)

    def test_campaign_start_is_compact(self):
        full = {
            "job": {
                "job_id": "J1",
                "state": "running",
                "runner_pid": 42,
                "manifest": "/tmp/cases.csv",
                "control_file": "/tmp/J1.control.json",
                "prelaunch_status": {"very": "large"},
            },
            "status": {
                "total_cases": 2,
                "runnable_cases": 2,
                "counts": {"not_started": 2},
            },
        }
        compact = compact_campaign_start(full)
        self.assertEqual(compact["job_id"], "J1")
        self.assertEqual(compact["cases"]["runnable"], 2)
        self.assertNotIn("prelaunch_status", compact)

    def test_pause_plan_uses_can_pause(self):
        plan = {
            "action": "pause_campaign",
            "can_stop": True,
            "job_id": "J1",
            "blockers": [],
            "laboratory_runner": {"active": True},
            "active_case": {
                "case_id": "R000001",
                "process_pid": 99,
                "process_alive": True,
                "process_matches_computing_module": True,
                "run_status": {"status": "running"},
            },
        }
        compact = compact_operator_plan(plan, pause=True, mode="now")
        self.assertTrue(compact["can_pause"])
        self.assertNotIn("can_stop", compact)
        self.assertEqual(compact["active_case"]["case_id"], "R000001")

    def test_pause_result_keeps_decisive_handshake(self):
        result = {
            "accepted": True,
            "completed": True,
            "rejected": False,
            "control": {
                "action": "pause_campaign",
                "state": "handled",
                "case_id": "R000001",
                "outcome": "pause_verified",
                "nrg_termination_reason": "external_pause_request",
                "nrg_restart_required": True,
                "exit_code": 0,
                "graceful_stop_completed": True,
                "force_kill_used": False,
            },
            "job": {"job_id": "J1", "state": "paused", "prelaunch_status": {"large": True}},
            "laboratory_runner": {"active": False, "last_owner": {"large": True}},
        }
        compact = compact_operator_result(result)
        self.assertTrue(compact["accepted"])
        self.assertTrue(compact["completed"])
        self.assertEqual(compact["state"], "paused")
        self.assertEqual(compact["nrg"]["reason"], "external_pause_request")
        self.assertTrue(compact["nrg"]["restart_required"])
        self.assertFalse(compact["nrg"]["force_kill_used"])
        self.assertNotIn("job", compact)
        self.assertNotIn("laboratory_runner", compact)

    def test_after_current_acknowledgement_is_pending_not_failure(self):
        result = {
            "accepted": True,
            "completed": False,
            "rejected": False,
            "control": {
                "action": "pause_after_current",
                "state": "acknowledged",
                "case_id": "R000001",
                "outcome": "pause_queued_after_current",
            },
            "job": {"job_id": "J1", "state": "running"},
            "laboratory_runner": {"active": True},
        }
        compact = compact_operator_result(result)
        self.assertTrue(compact["accepted"])
        self.assertTrue(compact["pending"])
        self.assertEqual(compact["control_state"], "acknowledged")

    def test_job_status_retains_references_not_full_history(self):
        full = {
            "job": {
                "job_id": "J1",
                "state": "paused",
                "runner_pid": 11,
                "stdout_log": "/tmp/out.log",
                "stderr_log": "/tmp/err.log",
                "prelaunch_status": {"large": True},
            },
            "campaign_status": {
                "total_cases": 2,
                "runnable_cases": 2,
                "counts": {"paused": 1, "not_started": 1},
            },
            "laboratory_runner": {"active": False},
            "active_case": None,
            "operator_control": {"action": "pause_campaign", "state": "handled"},
        }
        compact = compact_job_status(full, job_file="/tmp/J1.json")
        self.assertEqual(compact["campaign"]["counts"]["paused"], 1)
        self.assertEqual(compact["references"]["job_file"], "/tmp/J1.json")
        self.assertNotIn("prelaunch_status", compact)

    def test_full_flag_parses_on_operational_commands(self):
        parser = build_parser()
        commands = [
            ["campaign-status", "--cases", "x.csv", "--full"],
            ["campaign-start", "--cases", "x.csv", "--full"],
            ["campaign-job-status", "--job", "J1", "--full"],
            ["campaign-stop-plan", "--job", "J1", "--full"],
            ["campaign-stop-execute", "--job", "J1", "--full"],
            ["campaign-pause-plan", "--job", "J1", "--mode", "now", "--full"],
            ["campaign-pause-execute", "--job", "J1", "--mode", "now", "--full"],
        ]
        for argv in commands:
            with self.subTest(argv=argv):
                ns = parser.parse_args(argv)
                self.assertTrue(ns.full)


if __name__ == "__main__":
    unittest.main()
