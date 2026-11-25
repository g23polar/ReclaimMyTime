"""Tests for the factory/Orca integration: issue claims, PR adoption, branch
ownership, and the shared resource lock (factory/bin/locked).

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"


def load(name):
    loader = importlib.machinery.SourceFileLoader(f"factory_{name.replace('-', '_')}", str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def issue(n, title="t"):
    return {"number": n, "title": title}


class QueueRowsTest(unittest.TestCase):
    def setUp(self):
        self.mod = load("job")
        self.mod.CONCURRENCY = 1

    def rows(self, jobs=(), factory=(), orca=(), prs=()):
        return self.mod.queue_rows({j["issue"]: j for j in jobs}, list(factory), list(orca), list(prs))

    def test_orca_issue_with_ready_pr_is_adopted(self):
        rows = self.rows(orca=[issue(8)], prs=[{"number": 11, "isDraft": False, "issues": [8]}])
        self.assertEqual(rows, [("adopt", 8, "-", "-", "PR #11")])

    def test_draft_pr_is_not_adopted(self):
        rows = self.rows(orca=[issue(8)], prs=[{"number": 11, "isDraft": True, "issues": [8]}])
        self.assertEqual(rows[0][0], "skip")
        self.assertIn("no ready PR", rows[0][4])

    def test_pr_for_unclaimed_issue_is_ignored(self):
        rows = self.rows(factory=[issue(3)], prs=[{"number": 20, "isDraft": False, "issues": [3]}])
        self.assertEqual(rows, [("new", 3, "-", "-", "t")])

    def test_pr_closing_two_issues_is_skipped_with_reason(self):
        rows = self.rows(orca=[issue(8), issue(9)], prs=[{"number": 20, "isDraft": False, "issues": [8, 9]}])
        self.assertEqual([r[0] for r in rows], ["skip", "skip"])
        self.assertIn("exactly one", rows[0][4])

    def test_fork_pr_is_skipped_with_reason(self):
        rows = self.rows(orca=[issue(8)],
                         prs=[{"number": 20, "isDraft": False, "isCrossRepository": True, "issues": [8]}])
        self.assertEqual(rows[0][0], "skip")
        self.assertIn("fork", rows[0][4])

    def test_issue_labeled_both_is_skipped_not_built(self):
        rows = self.rows(factory=[issue(8)], orca=[issue(8)],
                         prs=[{"number": 11, "isDraft": False, "issues": [8]}])
        self.assertEqual(rows[0][0], "skip")
        self.assertIn("remove one", rows[0][4])

    def test_adopt_rows_share_concurrency_slots_with_new(self):
        rows = self.rows(factory=[issue(3)], orca=[issue(8)],
                         prs=[{"number": 11, "isDraft": False, "issues": [8]}])
        self.assertEqual([r[0] for r in rows], ["new", "hold"])

    def test_running_orca_job_resumes_and_holds_the_slot(self):
        job = {"id": "002-x", "issue": 8, "stage": "build", "round": 2, "origin": "orca", "pr": 11}
        rows = self.rows(jobs=[job], factory=[issue(3)], orca=[issue(8)],
                         prs=[{"number": 11, "isDraft": False, "issues": [8]}])
        self.assertEqual(rows[0], ("resume", 8, "002-x", "build", "round 2 (orca)"))
        self.assertEqual(rows[1][0], "hold")

    def test_orca_job_whose_claim_was_removed_is_skipped(self):
        job = {"id": "002-x", "issue": 8, "stage": "review", "round": 1, "origin": "orca", "pr": 11}
        rows = self.rows(jobs=[job])
        self.assertEqual(rows[0][0], "skip")
        self.assertIn("no longer labeled orca", rows[0][4])


class AdoptRefusalTest(unittest.TestCase):
    def setUp(self):
        self.mod = load("job")

    def pr(self, **kw):
        return {"number": 11, "state": "OPEN", "isDraft": False,
                "closingIssuesReferences": [{"number": 8}], **kw}

    def issue(self, labels=("orca",), state="OPEN"):
        return {"number": 8, "state": state, "labels": [{"name": l} for l in labels]}

    def test_ready_pr_on_claimed_issue_is_accepted(self):
        self.assertIsNone(self.mod.adopt_refusal(self.pr(), self.issue(), None))

    def test_refusals(self):
        cases = {
            "merged": (self.pr(state="MERGED"), self.issue(), None),
            "draft": (self.pr(isDraft=True), self.issue(), None),
            "exactly one issue": (self.pr(closingIssuesReferences=[]), {}, None),
            "fork": (self.pr(isCrossRepository=True), self.issue(), None),
            "closed": (self.pr(), self.issue(state="CLOSED"), None),
            "isn't labeled orca": (self.pr(), self.issue(labels=()), None),
            "both factory and orca": (self.pr(), self.issue(labels=("orca", "factory")), None),
            "already has job": (self.pr(), self.issue(), {"id": "002-x", "stage": "review"}),
        }
        for needle, args in cases.items():
            with self.subTest(needle):
                self.assertIn(needle, self.mod.adopt_refusal(*args) or "")


class BranchOwnershipTest(unittest.TestCase):
    """An adopted job never deletes or force-moves the Orca agent's branch."""

    def setUp(self):
        self.mod = load("job")
        self.calls = []
        self.mod.run = lambda cmd, cwd=None, check=True: self.record(cmd)
        self.mod.git = lambda job, *args, check=True: self.record(["git", *args])
        self.mod.gh = lambda *args: self.record(["gh", *args]).stdout
        self.mod.label = lambda *a, **k: self.calls.append(("label", a, k))
        self.mod.comment = lambda *a, **k: None
        self.mod.pr_state = lambda job: "MERGED"

    def record(self, cmd):
        self.calls.append(tuple(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def job(self, origin, tmp):
        return {"id": "002-x", "issue": 8, "stage": "review", "round": 1, "origin": origin,
                "pr": 11, "branch": "g23polar/todos-a", "worktree": tmp}

    def test_merged_cleanup_keeps_orca_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.advance(self.job("orca", tmp), "merged")
        self.assertTrue(any(c[:3] == ("git", "worktree", "remove") for c in self.calls))
        self.assertFalse(any(c[:3] == ("git", "branch", "-D") for c in self.calls))

    def test_merged_cleanup_still_deletes_factory_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.mod.advance(self.job("factory", tmp), "merged")
        self.assertTrue(any(c[:3] == ("git", "branch", "-D") for c in self.calls))


class LockedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {**os.environ, "RMT_LOCK_DIR": self.dir, "RMT_LOCK_WAIT": "1"}

    def locked(self, *args):
        return subprocess.run([str(BIN / "locked"), *args], env=self.env, capture_output=True, text=True)

    def test_runs_command_and_returns_its_exit_code(self):
        res = self.locked("pg", sys.executable, "-c", "print('hi'); raise SystemExit(3)")
        self.assertEqual((res.returncode, res.stdout.strip()), (3, "hi"))

    def test_busy_resource_exits_75_without_running(self):
        holder = subprocess.Popen([str(BIN / "locked"), "sim", "sleep", "5"], env=self.env)
        try:
            time.sleep(0.5)
            res = self.locked("sim", "echo", "ran")
            self.assertEqual(res.returncode, 75)
            self.assertNotIn("ran", res.stdout)
            self.assertIn("sim busy", res.stderr)
            self.assertIn("sleep 5", res.stderr)  # says who holds it
        finally:
            holder.kill()
            holder.wait()

    def test_resources_are_independent(self):
        holder = subprocess.Popen([str(BIN / "locked"), "sim", "sleep", "5"], env=self.env)
        try:
            time.sleep(0.5)
            self.assertEqual(self.locked("pg", "true").returncode, 0)
        finally:
            holder.kill()
            holder.wait()

    def test_lock_is_released_when_the_holder_dies(self):
        holder = subprocess.Popen([str(BIN / "locked"), "pg", "sleep", "30"], env=self.env)
        time.sleep(0.5)
        holder.kill()
        holder.wait()
        self.assertEqual(self.locked("pg", "true").returncode, 0)

    def test_max_kills_the_whole_process_group(self):
        marker = Path(self.dir) / "grandchild-survived"
        script = f"(sleep 3; touch {marker}) & sleep 30"
        start = time.monotonic()
        res = self.locked("--max", "1", "pg", "bash", "-c", script)
        self.assertEqual(res.returncode, 124)
        self.assertLess(time.monotonic() - start, 10)
        time.sleep(3)
        self.assertFalse(marker.exists())  # the backgrounded grandchild died too
        self.assertEqual(self.locked("pg", "true").returncode, 0)

    def test_lingering_grandchild_does_not_keep_the_lock(self):
        res = self.locked("pg", "bash", "-c", "nohup sleep 20 >/dev/null 2>&1 & disown; exit 0")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.locked("pg", "true").returncode, 0)

    def test_unknown_resource_is_usage_error(self):
        self.assertEqual(self.locked("db", "true").returncode, 2)


class OrcaAgentTest(unittest.TestCase):
    def setUp(self):
        self.mod = load("orca-agent")

    def test_terminal_matched_by_pane_tab(self):
        agent = {"paneKey": "tab1:leaf1"}
        terminals = [{"handle": "t-shell", "tabId": "tab2"}, {"handle": "t-agent", "tabId": "tab1"}]
        self.assertEqual(self.mod.pick_terminal(agent, terminals), "t-agent")

    def test_terminal_falls_back_to_only_agent_terminal(self):
        agent = {"paneKey": "x:y"}
        terminals = [{"handle": "t-shell", "tabId": "a"}, {"handle": "t-agent", "tabId": "b", "agentIdentity": "claude"}]
        self.assertEqual(self.mod.pick_terminal(agent, terminals), "t-agent")

    def test_ambiguous_terminal_is_none(self):
        terminals = [{"handle": "a", "agentIdentity": "claude"}, {"handle": "b", "agentIdentity": "claude"}]
        self.assertIsNone(self.mod.pick_terminal({"paneKey": ""}, terminals))

    def test_verdict_reads_the_last_line(self):
        self.assertEqual(self.mod.verdict("Pushed abc.\n\nREADY"), "READY")
        self.assertEqual(self.mod.verdict("stuck\n**BLOCKED**: no fixture"), "BLOCKED")
        self.assertEqual(self.mod.verdict("READY for review.\n\nPR #13 is updated."), "-")
        self.assertEqual(self.mod.verdict("READY? Not yet.\n\nTests still fail; working on it."), "-")
        self.assertEqual(self.mod.verdict("Pushed abc.\n\nThe PR is not merged."), "-")
        self.assertEqual(self.mod.verdict("READY/BLOCKED: BLOCKED. Waiting on #8."), "-")
        self.assertEqual(self.mod.verdict(None), "-")


if __name__ == "__main__":
    unittest.main()
