"""Permanent test suite for the installed guarded-development transaction engine.

Tests run in temporary Git repositories created under tempfile — never against
production data.  They exercise the installed transaction engine and the
semantic write/edit tools: transaction start/status/read, allowlist/denylist
and self-protection, candidate-tree/HEAD/branch integrity, semantic write/edit
behavior, canonical patch generation, rollback/persistence failure behavior,
and test validation and commit/discard safeguards.

The one-time out-of-band bootstrap installer
(agent_workspace/studies/guarded_dev_bootstrap/) is intentionally not a test
fixture here; it is a gitignored local artifact and must not be required by a
fresh worktree or clone.

Run:  python -m pytest tests/test_dev_transactions.py -q
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "dev_transaction_policy.json"
INDEX = ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
WORKSPACE_POLICY = ROOT / "agent_workspace" / "workspace_policy.json"

from agent_workspace import dev_transactions as dev  # noqa: E402


class FakeLab:
    def __init__(self, research_root: Path, studies_root: Path):
        self.research_root = str(research_root)
        self.studies_root = str(studies_root)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "--no-pager", *args], cwd=str(repo), text=True,
                          capture_output=True, check=True).stdout


def _make_repo(tmpdir: str) -> Path:
    repo = Path(tmpdir)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.org"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Dev Test"], cwd=str(repo), check=True)
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.local/\nagent_workspace/studies/\n",
        encoding="utf-8")
    (repo / "config").mkdir()
    shutil.copy2(POLICY, repo / "config" / "dev_transaction_policy.json")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_noop.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# tmp\n", encoding="utf-8")
    studies = repo / "agent_workspace" / "studies" / "dev_test" / "patches"
    studies.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo), check=True)
    return repo


def _write_patch(path: Path, rel: str, content: str = "hello\n", new: bool = True) -> Path:
    lines = ["diff --git a/%s b/%s" % (rel, rel)]
    if new:
        lines += ["new file mode 100644", "--- /dev/null", "+++ b/%s" % rel,
                  "@@ -0,0 +1,%d @@" % len(content.splitlines())]
    else:
        lines += ["--- a/%s" % rel, "+++ b/%s" % rel, "@@ -1,1 +1,1 @@"]
    for ln in content.splitlines():
        lines.append("+" + ln)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class DevTransactionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = _make_repo(self._tmp.name)
        self.lab = FakeLab(self.repo, self.repo / "agent_workspace" / "studies")
        self.patches = self.repo / "agent_workspace" / "studies" / "dev_test" / "patches"

    def _start(self, allow=("tests/",)):
        plan = dev.transaction_plan(self.lab, "t", "r", list(allow))
        return dev.transaction_start(self.lab, plan["transaction_id"], "t", "r", list(allow), plan["baseline_sha"])

    def _apply(self, res, rel, content="hello\n"):
        patch = _write_patch(self.patches / f"p_{rel.replace('/', '_')}.diff", rel, content)
        plan = dev.apply_patch_plan(self.lab, res["transaction_id"], str(patch))
        return dev.apply_patch_execute(self.lab, res["transaction_id"], str(patch),
                                        plan["patch_sha256"], plan["current_head_sha"], plan["applied_against_tree_sha"])

    def _run_test(self, res, profile, test_files=None, require_nrg=False):
        plan = dev.test_plan(self.lab, res["transaction_id"], profile, test_files, require_nrg)
        return dev.test_execute(self.lab, res["transaction_id"], profile, test_files, require_nrg,
                                plan["candidate_tree_sha"], plan["current_head_sha"], plan["current_branch"])

    def _commit(self, res, message="m"):
        plan = dev.commit_plan(self.lab, res["transaction_id"], message)
        return dev.commit_execute(self.lab, res["transaction_id"], message,
                                  plan["candidate_tree_sha"], plan["current_head_sha"], plan["current_branch"])

    # --- ordinary invariants ---
    def test_ordinary_write_roots_unchanged(self):
        wp = json.loads(WORKSPACE_POLICY.read_text(encoding="utf-8"))
        self.assertEqual(wp["agent_write_roots"], ["paths.studies_root", "paths.campaign_root/definitions"])
        self.assertIn("dev_transaction_policy", wp)

    # 1. transaction-ID generation/validation
    def test_transaction_id_generation_and_validation(self):
        tid = dev._new_transaction_id()
        self.assertTrue(dev._valid_tid(tid))
        path = dev._transaction_path(self.lab, dev._load_policy(self.lab), tid)
        self.assertTrue(path.name.endswith(tid + ".json"))
        # invalid IDs are rejected
        self.assertFalse(dev._valid_tid("invalid-id"))
        self.assertFalse(dev._valid_tid("dev-20240101T120000-xyz"))  # non-hex
        self.assertFalse(dev._valid_tid(""))

    # 2. transaction start + record creation
    def test_start_creates_record(self):
        res = self._start()
        self.assertTrue(res["started"])
        rec = dev._load_transaction(self.lab, dev._load_policy(self.lab), res["transaction_id"])
        self.assertEqual(rec["final_state"], "active")

    # 3. no orphan on failed start
    def test_no_orphan_on_start_failure(self):
        plan = dev.transaction_plan(self.lab, "t", "r", ["tests/"])
        tid = plan["transaction_id"]
        branch = plan["development_branch"]
        with self.assertRaises(Exception):
            dev.transaction_start(self.lab, tid, "t", "r", ["tests/"], "0" * 40)
        self.assertNotIn(branch, _git(self.repo, "branch", "--list"))
        wt = dev._worktree_dir_path(self.lab, dev._load_policy(self.lab), tid)
        self.assertFalse(wt.exists())

    def test_no_orphan_on_record_save_failure(self):
        plan = dev.transaction_plan(self.lab, "t", "r", ["tests/"])
        tid, branch = plan["transaction_id"], plan["development_branch"]
        orig = dev._save_transaction

        def boom(lab, policy, t, payload):
            raise RuntimeError("simulated record failure")

        dev._save_transaction = boom
        try:
            with self.assertRaises(RuntimeError):
                dev.transaction_start(self.lab, tid, "t", "r", ["tests/"], plan["baseline_sha"])
        finally:
            dev._save_transaction = orig
        self.assertNotIn(branch, _git(self.repo, "branch", "--list"))
        wt = dev._worktree_dir_path(self.lab, dev._load_policy(self.lab), tid)
        self.assertFalse(wt.exists())

    # 4. directory-prefix allowlist persists through plan/start/record
    def test_directory_prefix_allowlist_persists(self):
        plan = dev.transaction_plan(self.lab, "t", "r", ["tests/"])
        self.assertEqual(plan["allowed_paths"], ["tests/"])
        start = dev.transaction_start(self.lab, plan["transaction_id"], "t", "r",
                                      plan["allowed_paths"], plan["baseline_sha"])
        self.assertEqual(start["allowed_paths"], ["tests/"])
        pol = dev._load_policy(self.lab)
        rec = dev._load_transaction(self.lab, pol, start["transaction_id"])
        self.assertEqual(rec["allowed_paths"], ["tests/"])
        self.assertTrue(dev._allow_match("tests/a.py", rec["allowed_paths"]))
        self.assertFalse(dev._allow_match("other/a.py", rec["allowed_paths"]))

    # 5. exact-file allowlist
    def test_exact_file_allowlist(self):
        res = self._start(allow=("tests/x.py",))
        # exact file allowed
        self._apply(res, "tests/x.py", "x\n")
        # sibling NOT allowed
        patch = _write_patch(self.patches / "p2.diff", "tests/y.py", "y\n")
        with self.assertRaises(dev.DevTransactionError):
            dev.apply_patch_plan(self.lab, res["transaction_id"], str(patch))

    # 6. path-aware glob direct-file positive + nested negative
    def test_path_aware_glob_positive_and_negative(self):
        res = self._start(allow=("campaign_tools/*.py",))
        # direct file: allowed
        patch1 = _write_patch(self.patches / "p1.diff", "campaign_tools/foo.py", "z\n")
        plan1 = dev.apply_patch_plan(self.lab, res["transaction_id"], str(patch1))
        self.assertTrue(plan1["can_apply"])
        dev.apply_patch_execute(self.lab, res["transaction_id"], str(patch1),
                                plan1["patch_sha256"], plan1["current_head_sha"],
                                plan1["applied_against_tree_sha"])
        # nested file: NOT allowed by campaign_tools/*.py
        res2 = self._start(allow=("campaign_tools/*.py",))
        patch2 = _write_patch(self.patches / "p2.diff", "campaign_tools/sub/foo.py", "nested\n")
        with self.assertRaises(dev.DevTransactionError):
            dev.apply_patch_plan(self.lab, res2["transaction_id"], str(patch2))

    # 7. denylist/self-protected override
    def test_denied_and_self_protected_override(self):
        res = self._start(allow=("runs/", "campaigns/", "agent_workspace/dev_transactions.py", "AGENTS.md"))
        for rel in ("runs/x.txt", "campaigns/x.txt", "agent_workspace/dev_transactions.py", "AGENTS.md"):
            patch = _write_patch(self.patches / f"d_{rel.replace('/', '_')}.diff", rel, "x\n")
            with self.assertRaises(dev.DevTransactionError, msg=rel):
                dev.apply_patch_plan(self.lab, res["transaction_id"], str(patch))

    # 8. staged whitespace detected by diff_check
    def test_diff_check_detects_staged_whitespace(self):
        res = self._start()
        self._apply(res, "tests/ws.txt", "trailing   \n")
        out = self._run_test(res, "diff_check")
        self.assertEqual(out["classification"], "fail")

    # 9. stale start plan rejected
    def test_stale_start_plan_rejected(self):
        plan = dev.transaction_plan(self.lab, "t", "r", ["tests/"])
        (self.repo / "README.md").write_text("# changed\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-q", "-am", "advance"], cwd=str(self.repo), check=True)
        with self.assertRaises(dev.DevTransactionError):
            dev.transaction_start(self.lab, plan["transaction_id"], "t", "r", ["tests/"], plan["baseline_sha"])

    # 10. changed patch SHA rejected
    def test_patch_changed_after_confirmation_rejected(self):
        res = self._start()
        patch = _write_patch(self.patches / "p.diff", "tests/a.txt", "original\n")
        plan = dev.apply_patch_plan(self.lab, res["transaction_id"], str(patch))
        _write_patch(patch, "tests/a.txt", "tampered\n")
        with self.assertRaises(dev.DevTransactionError):
            dev.apply_patch_execute(self.lab, res["transaction_id"], str(patch),
                                     plan["patch_sha256"], plan["current_head_sha"], plan["applied_against_tree_sha"])

    # 11. changed candidate after test plan rejected
    def test_candidate_changed_after_test_plan_rejected(self):
        res = self._start()
        self._apply(res, "tests/a.txt", "a\n")
        tplan = dev.test_plan(self.lab, res["transaction_id"], "diff_check")
        self._apply(res, "tests/b.txt", "b\n")
        with self.assertRaises(dev.DevTransactionError):
            dev.test_execute(self.lab, res["transaction_id"], "diff_check", None, False,
                             tplan["candidate_tree_sha"], tplan["current_head_sha"], tplan["current_branch"])

    # 12. pre-existing unexpected working-tree source change blocks test
    def test_pre_existing_unexpected_change_blocks_test(self):
        res = self._start()
        wt = Path(res["worktree"])
        # create an unstaged modification to a tracked file in the worktree
        (wt / "tests" / "test_noop.py").write_text("def test_ok():\n    assert 2 == 2\n", encoding="utf-8")
        # test_plan must report can_run=false
        plan = dev.test_plan(self.lab, res["transaction_id"], "diff_check")
        self.assertFalse(plan["can_run"])
        self.assertTrue(len(plan["unexpected_changes"]) > 0)
        # test_execute must reject
        with self.assertRaises(dev.DevTransactionError):
            dev.test_execute(self.lab, res["transaction_id"], "diff_check", None, False,
                             plan["candidate_tree_sha"], plan["current_head_sha"], plan["current_branch"])

    # 13. candidate mutation during test yields integrity_failed
    def test_candidate_changed_during_test_integrity_failed(self):
        res = self._start()
        # add a mutation test that creates an untracked source file when run
        mut = "def test_mutates():\n    open('tests/mutated_by_test.txt','w').write('x')\n"
        mut_path = self.repo / "tests" / "test_mutation.py"
        mut_path.write_text(mut, encoding="utf-8")
        # V4 defect #3: explicitly git add the untracked file before commit
        subprocess.run(["git", "add", "tests/test_mutation.py"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add mutation test"], cwd=str(self.repo), check=True)
        # re-create transaction baseline after the commit
        res = self._start()
        out = self._run_test(res, "targeted_pytest", test_files=["tests/test_mutation.py"])
        self.assertEqual(out["classification"], "integrity_failed")
        self.assertFalse(out["integrity_ok"])

    # 14. zero-validation commit rejected
    def test_commit_zero_validation_rejected(self):
        res = self._start()
        self._apply(res, "tests/a.txt", "a\n")
        plan = dev.commit_plan(self.lab, res["transaction_id"], "m")
        self.assertFalse(plan["can_commit"])
        self.assertFalse(plan["validation_ok"])

    # 15. failed-validation commit rejected
    def test_commit_failed_validation_rejected(self):
        res = self._start()
        self._apply(res, "tests/ws.txt", "trailing   \n")
        self._run_test(res, "diff_check")  # fail (current tree)
        plan = dev.commit_plan(self.lab, res["transaction_id"], "m")
        self.assertFalse(plan["can_commit"])
        self.assertTrue(plan["current_blocking_result"])

    # 16. current-tree diff_check + pytest pass accepted
    def test_commit_with_validation_accepted(self):
        res = self._start()
        self._apply(res, "tests/a.txt", "a\n")  # clean
        self._run_test(res, "diff_check")
        self._run_test(res, "targeted_pytest", test_files=["tests/test_noop.py"])
        out = self._commit(res, "add a")
        self.assertTrue(out["committed"])
        self.assertTrue(out["development_commit_sha"])

    # 17. stale HEAD before test rejected
    def test_stale_head_before_test_rejected(self):
        res = self._start()
        self._apply(res, "tests/a.txt", "a\n")
        self._run_test(res, "diff_check")
        self._commit(res, "first commit")
        # now HEAD has advanced; a second test_execute with the old head sha must fail
        plan = dev.test_plan(self.lab, res["transaction_id"], "diff_check")
        # simulate stale: pass a wrong expected_head_sha
        with self.assertRaises(dev.DevTransactionError):
            dev.test_execute(self.lab, res["transaction_id"], "diff_check", None, False,
                             plan["candidate_tree_sha"], "0" * 40, plan["current_branch"])

    # 18. stale HEAD/branch before commit rejected
    def test_stale_head_before_commit_rejected(self):
        res = self._start()
        self._apply(res, "tests/a.txt", "a\n")
        self._run_test(res, "diff_check")
        self._run_test(res, "targeted_pytest", test_files=["tests/test_noop.py"])
        plan = dev.commit_plan(self.lab, res["transaction_id"], "m")
        self.assertTrue(plan["can_commit"])
        # V4 defect #5: a stale HEAD (different from plan) must be rejected.
        # This simulates a new commit with the same tree between plan and execute:
        # the candidate tree matches but HEAD differs, so provenance changed.
        with self.assertRaises(dev.DevTransactionError):
            dev.commit_execute(self.lab, res["transaction_id"], "m",
                               plan["candidate_tree_sha"], "0" * 40, plan["current_branch"])

    # 19. discard state change after confirmation rejected
    def test_discard_state_change_after_confirmation_rejected(self):
        res = self._start()
        self._apply(res, "tests/a.txt", "a\n")
        plan = dev.discard_plan(self.lab, res["transaction_id"])
        # change the worktree state after confirmation
        wt = Path(res["worktree"])
        (wt / "tests" / "sneak.py").write_text("x\n", encoding="utf-8")
        with self.assertRaises(dev.DevTransactionError):
            dev.discard_execute(self.lab, res["transaction_id"],
                                plan["current_head_sha"], plan["candidate_tree_sha"],
                                plan["current_branch"], plan["staged_paths"],
                                plan["unexpected_changes"])

    # 20. discard cleanup failure is not reported as success
    def test_discard_cleanup_failure_not_success(self):
        res = self._start()
        pol = dev._load_policy(self.lab)
        orig_run_git = dev._run_git

        def fake_run_git(args, cwd, timeout=120):
            # Let worktree remove succeed but make branch -D fail
            if args[:2] == ["branch", "-D"]:
                return subprocess.CompletedProcess(args, 1, "", "simulated branch deletion failure")
            return orig_run_git(args, cwd, timeout)

        dev._run_git = fake_run_git
        try:
            with self.assertRaises(dev.DevTransactionError):
                dev.discard_execute(self.lab, res["transaction_id"])
        finally:
            dev._run_git = orig_run_git
        rec2 = dev._load_transaction(self.lab, pol, res["transaction_id"])
        self.assertNotEqual(rec2["final_state"], "discarded")

    # 21. real lab_config_validate command normalization
    def test_lab_config_validate_command_normalization(self):
        res = self._start()
        plan = dev.test_plan(self.lab, res["transaction_id"], "lab_config_validate")
        cmd = plan["normalized_command"]
        self.assertIn("nrg_analysis.config_cli", cmd)
        self.assertIn("validate", cmd)
        self.assertIn("--no-runtime", cmd)
        self.assertNotIn("lab-info", cmd)

    # 22. outside-study/symlink patch rejection
    def test_outside_study_patch_rejected(self):
        res = self._start()
        outside = self.repo / ".local" / "evil_patch.diff"
        outside.parent.mkdir(parents=True, exist_ok=True)
        _write_patch(outside, "tests/a.txt", "x\n")
        with self.assertRaises(dev.DevTransactionError):
            dev.apply_patch_plan(self.lab, res["transaction_id"], str(outside))

    def test_symlink_patch_escape_rejected(self):
        res = self._start()
        outside = self.repo.parent / "outside.patch"
        outside.write_text("x", encoding="utf-8")
        link = self.patches / "link.diff"
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        os.symlink(str(outside), str(link))
        with self.assertRaises(dev.DevTransactionError):
            dev.apply_patch_plan(self.lab, res["transaction_id"], str(link))

    # 23. corrupted transaction record rejected
    def test_corrupted_record_rejected(self):
        res = self._start()
        pol = dev._load_policy(self.lab)
        path = dev._transaction_path(self.lab, pol, res["transaction_id"])
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["worktree"] = str(self.repo / ".local" / "evil_wt")
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        with self.assertRaises(dev.DevTransactionError):
            dev.transaction_status(self.lab, res["transaction_id"])
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["worktree"] = res["worktree"]
        rec["denylist"] = []
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        with self.assertRaises(dev.DevTransactionError):
            dev.transaction_status(self.lab, res["transaction_id"])

    # 24. non-canonical discard rejected
    def test_discard_noncanonical_rejected(self):
        res = self._start()
        pol = dev._load_policy(self.lab)
        path = dev._transaction_path(self.lab, pol, res["transaction_id"])
        victim = self.repo / ".local" / "victim_dir"
        victim.mkdir(parents=True, exist_ok=True)
        (victim / "keep.txt").write_text("keep", encoding="utf-8")
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["worktree"] = str(victim)
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        with self.assertRaises(dev.DevTransactionError):
            dev.discard_execute(self.lab, res["transaction_id"])
        self.assertTrue((victim / "keep.txt").exists())

    # Semantic write/edit interface (trusted engine authors patch artifacts)
    def test_semantic_write_create_stages_and_audits(self):
        res = self._start(allow=("tests/",))
        content = "def test_semantic():\n    assert True\n"
        plan = dev.write_plan(self.lab, res["transaction_id"], "tests/semantic_tmp.py", content, "create")
        self.assertTrue(plan["can_write"])
        self.assertIsNone(plan["current_target_sha256"])
        out = dev.write_execute(
            self.lab, res["transaction_id"], "tests/semantic_tmp.py", content, "create",
            plan["semantic_spec_sha256"], plan["current_head_sha"],
            plan["applied_against_tree_sha"], plan["current_branch"],
            plan["current_target_sha256"],
        )
        self.assertTrue(out["applied"])
        self.assertEqual(out["kind"], "semantic_write")
        self.assertIn("tests/semantic_tmp.py", out["staged_paths"])
        artifact = Path(out["canonical_patch_artifact"])
        self.assertTrue(artifact.is_file())
        patch = artifact.read_text(encoding="utf-8")
        self.assertIn("tests/semantic_tmp.py", patch)
        self.assertIn("+def test_semantic():", patch)

    def test_semantic_write_replace_existing_text(self):
        res = self._start(allow=("tests/test_noop.py",))
        replacement = "def test_ok():\n    assert 2 == 2\n"
        plan = dev.write_plan(self.lab, res["transaction_id"], "tests/test_noop.py", replacement, "replace")
        out = dev.write_execute(
            self.lab, res["transaction_id"], "tests/test_noop.py", replacement, "replace",
            plan["semantic_spec_sha256"], plan["current_head_sha"],
            plan["applied_against_tree_sha"], plan["current_branch"],
            plan["current_target_sha256"],
        )
        wt = Path(res["worktree"])
        self.assertEqual((wt / "tests/test_noop.py").read_text(encoding="utf-8"), replacement)
        self.assertIn("tests/test_noop.py", out["staged_paths"])

    def test_semantic_edit_exact_replacement(self):
        res = self._start(allow=("tests/test_noop.py",))
        plan = dev.edit_plan(
            self.lab, res["transaction_id"], "tests/test_noop.py",
            "assert 1 == 1", "assert 3 == 3", 1,
        )
        self.assertTrue(plan["can_edit"])
        out = dev.edit_execute(
            self.lab, res["transaction_id"], "tests/test_noop.py",
            "assert 1 == 1", "assert 3 == 3", 1,
            plan["semantic_spec_sha256"], plan["current_head_sha"],
            plan["applied_against_tree_sha"], plan["current_branch"],
            plan["current_target_sha256"],
        )
        self.assertEqual(out["kind"], "semantic_edit")
        wt = Path(res["worktree"])
        self.assertIn("assert 3 == 3", (wt / "tests/test_noop.py").read_text(encoding="utf-8"))

    def test_semantic_edit_occurrence_mismatch_refuses(self):
        res = self._start(allow=("tests/test_noop.py",))
        plan = dev.edit_plan(
            self.lab, res["transaction_id"], "tests/test_noop.py",
            "assert 1 == 1", "assert True", 2,
        )
        self.assertFalse(plan["can_edit"])
        self.assertEqual(plan["actual_occurrences"], 1)

    def test_semantic_write_enforces_allowlist_and_self_protection(self):
        res = self._start(allow=("tests/", "agent_workspace/dev_transactions.py"))
        with self.assertRaises(dev.DevTransactionError):
            dev.write_plan(self.lab, res["transaction_id"], "README.md", "x\n", "replace")
        with self.assertRaises(dev.DevTransactionError):
            dev.write_plan(
                self.lab, res["transaction_id"],
                "agent_workspace/dev_transactions.py", "x\n", "create",
            )

    def test_semantic_write_spec_change_after_confirmation_rejected(self):
        res = self._start(allow=("tests/",))
        plan = dev.write_plan(self.lab, res["transaction_id"], "tests/x.py", "a\n", "create")
        with self.assertRaises(dev.DevTransactionError):
            dev.write_execute(
                self.lab, res["transaction_id"], "tests/x.py", "b\n", "create",
                plan["semantic_spec_sha256"], plan["current_head_sha"],
                plan["applied_against_tree_sha"], plan["current_branch"],
                plan["current_target_sha256"],
            )

    def test_semantic_write_stale_candidate_tree_rejected(self):
        res = self._start(allow=("tests/",))
        plan = dev.write_plan(self.lab, res["transaction_id"], "tests/x.py", "x\n", "create")
        self._apply(res, "tests/y.py", "y\n")
        with self.assertRaises(dev.DevTransactionError):
            dev.write_execute(
                self.lab, res["transaction_id"], "tests/x.py", "x\n", "create",
                plan["semantic_spec_sha256"], plan["current_head_sha"],
                plan["applied_against_tree_sha"], plan["current_branch"],
                plan["current_target_sha256"],
            )

    def test_semantic_write_unexpected_worktree_change_blocks_plan(self):
        res = self._start(allow=("tests/",))
        wt = Path(res["worktree"])
        (wt / "tests/test_noop.py").write_text("def test_ok():\n    assert False\n", encoding="utf-8")
        with self.assertRaises(dev.DevTransactionError):
            dev.write_plan(self.lab, res["transaction_id"], "tests/x.py", "x\n", "create")

    def test_semantic_write_rolls_back_if_record_persist_fails(self):
        res = self._start(allow=("tests/",))
        plan = dev.write_plan(self.lab, res["transaction_id"], "tests/rollback.py", "x = 1\n", "create")
        original = dev._save_transaction

        def fail_save(*_args, **_kwargs):
            raise RuntimeError("simulated record failure")

        dev._save_transaction = fail_save
        try:
            with self.assertRaises(RuntimeError):
                dev.write_execute(
                    self.lab, res["transaction_id"], "tests/rollback.py", "x = 1\n", "create",
                    plan["semantic_spec_sha256"], plan["current_head_sha"],
                    plan["applied_against_tree_sha"], plan["current_branch"],
                    plan["current_target_sha256"],
                )
        finally:
            dev._save_transaction = original
        wt = Path(res["worktree"])
        self.assertFalse((wt / "tests/rollback.py").exists())
        self.assertEqual(dev._write_tree(wt), plan["applied_against_tree_sha"])
        self.assertEqual(dev._unexpected_worktree_changes(wt), [])

    def test_semantic_edit_canonical_patch_is_operation_delta(self):
        res = self._start(allow=("tests/test_noop.py",))
        plan = dev.edit_plan(
            self.lab, res["transaction_id"], "tests/test_noop.py",
            "assert 1 == 1", "assert 9 == 9", 1,
        )
        out = dev.edit_execute(
            self.lab, res["transaction_id"], "tests/test_noop.py",
            "assert 1 == 1", "assert 9 == 9", 1,
            plan["semantic_spec_sha256"], plan["current_head_sha"],
            plan["applied_against_tree_sha"], plan["current_branch"],
            plan["current_target_sha256"],
        )
        patch = Path(out["canonical_patch_artifact"]).read_text(encoding="utf-8")
        self.assertIn("-    assert 1 == 1", patch)
        self.assertIn("+    assert 9 == 9", patch)

    # --- ordinary behavior unaffected ---
    def test_dev_tools_do_not_broaden_write_roots(self):
        wp = json.loads(WORKSPACE_POLICY.read_text(encoding="utf-8"))
        for root in wp["agent_write_roots"]:
            self.assertNotIn("campaign_tools", root)
            self.assertNotIn("nrg_analysis", root)
            self.assertNotIn(".pi", root)


    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup test")
    def test_run_test_process_kills_child_tree_on_timeout(self):
        import sys
        import time
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pidfile = root / "tree.json"
            child = (
                "import json, os, pathlib, subprocess, sys, time\n"
                "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(pidfile)!r}).write_text("
                f"json.dumps({{'child': os.getpid(), 'grandchild': g.pid}}))\n"
                "time.sleep(60)\n"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                dev._run_test_process([sys.executable, "-c", child], root, os.environ.copy(), 1)
            ids = json.loads(pidfile.read_text())

            def alive(pid: int) -> bool:
                try:
                    stat = Path(f"/proc/{pid}/stat").read_text()
                except FileNotFoundError:
                    return False
                return stat.rsplit(") ", 1)[1].split()[0] != "Z"

            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and any(alive(p) for p in ids.values()):
                time.sleep(0.05)
            self.assertFalse(any(alive(p) for p in ids.values()), ids)

if __name__ == "__main__":
    unittest.main()