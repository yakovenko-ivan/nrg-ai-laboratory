import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "read_only_shell_policy.json"
INDEX = ROOT / ".pi" / "extensions" / "nrg-laboratory" / "index.ts"
HELPER = ROOT / ".pi" / "extensions" / "nrg-laboratory" / "read_only_shell.ts"
AGENTS = ROOT / "AGENTS.md"


class ReadOnlyShellV057ContractTests(unittest.TestCase):
    def test_policy_is_default_narrow_and_auditable(self):
        policy = json.loads(POLICY.read_text())
        self.assertEqual(policy["schema_version"], 1)
        self.assertLessEqual(policy["max_timeout_ms"], 60_000)
        allowed = set(policy["allowed_commands"])
        for name in {"wc", "du", "stat", "file", "sha256sum", "git"}:
            self.assertIn(name, allowed)
        for name in {"rm", "mv", "cp", "mkdir", "touch", "python", "python3", "bash", "sh", "make", "cmake", "kill", "sudo", "tee", "xargs"}:
            self.assertNotIn(name, allowed)

    def test_git_is_read_only_subset(self):
        policy = json.loads(POLICY.read_text())
        subs = set(policy["git_subcommands"])
        self.assertTrue({"status", "log", "diff", "branch", "show", "rev-parse", "ls-files"}.issubset(subs))
        self.assertTrue({"commit", "add", "checkout", "switch", "reset", "merge", "rebase", "stash", "push", "pull"}.isdisjoint(subs))

    def test_extension_enables_bash_but_gates_it(self):
        text = INDEX.read_text()
        self.assertIn('for (const name of ["bash", "ls", "find", "grep"])', text)
        self.assertIn("evaluateReadOnlyShell", text)
        self.assertIn("NRG read-only shell blocked this command", text)
        self.assertNotIn("Agent bash is disabled in the NRG laboratory", text)

    def test_helper_blocks_shell_escape_and_mutating_find(self):
        text = HELPER.read_text()
        for token in ["-delete", "-exec", "-execdir", "-fprintf", "--ext-diff", "--textconv"]:
            self.assertIn(token, text)
        self.assertIn("only one leading 'cd ... && <read command>' chain is allowed", text)
        self.assertIn("shell expansion/escaping is not allowed", text)
        self.assertIn("path escapes research_root", text)

    def test_agents_policy_describes_read_only_shell_boundary(self):
        text = AGENTS.read_text()
        self.assertIn("## Read-only shell inspection", text)
        self.assertIn("restricted read-only Pi `bash` channel", text)
        self.assertIn("do not try alternate shell syntax", text.lower())
        self.assertIn("A shell-derived observation is not a substitute for structured laboratory state", text)


if __name__ == "__main__":
    unittest.main()
