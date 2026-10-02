#!/usr/bin/env python3
"""skills/session-handoff/lane.py: subprocess seam against temporary git repos.

Each test builds a repo whose `main` holds README.md (two sections) and a
code file, writes a lanes.json manifest into the git common dir, and runs the
CLI the way a worker or the coordinator would.

Run: python3 tests/test_lane.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCRIPT = os.path.join(REPO, "skills", "session-handoff", "lane.py")

README = """# Kit

intro

## Alpha

alpha one
alpha two

## Beta

beta one
beta two
beta three
beta four
"""


class LaneTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "repo")
        gitconfig = os.path.join(self._tmp.name, "gitconfig")
        with open(gitconfig, "w") as f:
            f.write("[user]\n\tname = T\n\temail = t@example.com\n"
                    "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=gitconfig, GIT_CONFIG_NOSYSTEM="1")
        os.makedirs(self.root)
        self.git("init", "-q")
        self.write("README.md", README)
        self.write("src/a.py", "a = 1\n")
        self.write("src/b.py", "b = 1\n")
        self.commit("init")
        self.base = self.sha()

    # -- helpers -----------------------------------------------------------
    def git(self, *args, check=True):
        p = subprocess.run(["git"] + list(args), cwd=self.root, env=self.env,
                           capture_output=True, text=True, timeout=30)
        if check and p.returncode:
            raise AssertionError("git %s: %s" % (args, p.stderr))
        return p.stdout.strip()

    def sha(self, ref="HEAD"):
        return self.git("rev-parse", ref)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def manifest(self, **extra):
        data = {"base": self.base, "target": "main", "order": ["A", "B"], "checks": [],
                "lanes": {"A": {"branch": "lane-a", "paths": ["src/a.py"],
                                "sections": ["README.md#Alpha"]},
                          "B": {"branch": "lane-b", "paths": ["src/b.py"],
                                "sections": ["README.md#Beta"]}}}
        data.update(extra)
        common = self.git("rev-parse", "--git-common-dir")
        with open(os.path.join(self.root, common, "lanes.json"), "w") as f:
            json.dump(data, f)

    def lane(self, *args, expect=None):
        p = subprocess.run([sys.executable, SCRIPT] + list(args), cwd=self.root, env=self.env,
                           capture_output=True, text=True, timeout=60)
        lines = p.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, p.stdout + p.stderr)
        out = json.loads(lines[0])
        if expect is not None:
            self.assertEqual(p.returncode, expect, out)
        return p.returncode, out

    # -- start -------------------------------------------------------------
    def test_start_resets_a_branch_created_from_an_older_commit(self):
        old = self.base
        self.write("src/a.py", "a = 2\n")
        self.commit("main moves on")
        self.base = self.sha()
        self.git("checkout", "-q", "-b", "lane-a", old)  # like agents_spawn from origin/main
        self.manifest()
        _, out = self.lane("start", "A", expect=0)
        self.assertEqual(out["outcome"], "reset")
        self.assertEqual(self.sha(), self.base)

    def test_start_at_base_or_ahead_is_ok(self):
        self.manifest()
        self.git("checkout", "-q", "-b", "lane-a")
        _, out = self.lane("start", "A", expect=0)
        self.assertEqual(out["outcome"], "at-base")
        self.write("src/a.py", "a = 3\n")
        self.commit("lane work")
        _, out = self.lane("start", "A", expect=0)
        self.assertEqual(out["outcome"], "started")

    def test_start_refuses_diverged_branch_and_wrong_branch(self):
        self.git("checkout", "-q", "-b", "lane-a")
        self.write("src/a.py", "a = 4\n")
        self.commit("lane work")
        self.git("checkout", "-q", "main")
        self.write("src/b.py", "b = 4\n")
        self.commit("main moves on")
        self.base = self.sha()
        self.manifest()
        _, out = self.lane("start", "A", expect=1)
        self.assertEqual(out["outcome"], "wrong-branch")
        self.git("checkout", "-q", "lane-a")
        head = self.sha()
        _, out = self.lane("start", "A", expect=1)
        self.assertEqual(out["outcome"], "diverged")
        self.assertEqual(self.sha(), head)
        self.assertTrue(out.get("next"))

    # -- check -------------------------------------------------------------
    def on_lane_a(self):
        self.manifest()
        self.git("checkout", "-q", "-b", "lane-a")

    def test_check_passes_when_only_owned_files_and_sections_change(self):
        self.on_lane_a()
        self.write("src/a.py", "a = 5\n")
        self.write("README.md", README.replace("alpha two", "alpha TWO\nalpha three"))
        self.commit("owned work")
        _, out = self.lane("check", "A", expect=0)
        self.assertEqual(out["outcome"], "clean")
        self.assertEqual(out["not_owned"], [])
        self.assertEqual(sorted(h["file"] for h in out["owned"]), ["README.md", "src/a.py"])

    def test_check_flags_other_lanes_and_unassigned_changes(self):
        self.on_lane_a()
        self.write("README.md", README.replace("alpha one", "alpha 1").replace("beta two", "beta 2"))
        self.write("src/b.py", "b = 5\n")
        self.write("src/new.py", "n = 1\n")  # uncommitted edits count too
        self.git("add", "src/new.py")
        _, out = self.lane("check", "A", expect=1)
        self.assertEqual(out["outcome"], "not-owned")
        flagged = {(h["file"], h.get("section"), h["owner"]) for h in out["not_owned"]}
        self.assertEqual(flagged, {("README.md", "Beta", "B"), ("src/b.py", None, "B"),
                                   ("src/new.py", None, None)})
        self.assertEqual([(h["file"], h["section"]) for h in out["owned"]], [("README.md", "Alpha")])
        self.assertTrue(out.get("next"))

    # -- split -------------------------------------------------------------
    def test_split_moves_not_owned_changes_to_side_branches(self):
        self.on_lane_a()
        lane_readme = README.replace("alpha one", "alpha 1")
        self.write("README.md", lane_readme.replace("beta two", "beta 2"))
        self.write("src/a.py", "a = 6\n")
        self.write("src/b.py", "b = 6\n")
        self.commit("mixed work")
        _, out = self.lane("split", "A", expect=0)
        self.assertEqual(out["outcome"], "split")
        sides = {s["branch"]: s for s in out["side_branches"]}
        self.assertEqual(set(sides), {"lane-a-shared-readme-md", "lane-a-shared-src-b-py"})
        # The lane keeps only what it owns ...
        self.assertEqual(self.read("README.md"), lane_readme)
        self.assertEqual(self.read("src/b.py"), "b = 1\n")
        self.assertEqual(self.read("src/a.py"), "a = 6\n")
        self.assertEqual(self.lane("check", "A")[1]["outcome"], "clean")
        # ... and each side branch holds one file's foreign change on top of base.
        for branch in sides:
            self.assertEqual(self.git("rev-parse", branch + "~1"), self.base)
        self.assertEqual(self.git("show", "lane-a-shared-readme-md:README.md") + "\n",
                         README.replace("beta two", "beta 2"))
        self.assertEqual(self.git("show", "lane-a-shared-src-b-py:src/b.py"), "b = 6")
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

    def test_split_refuses_spanning_hunk_and_dirty_tree(self):
        self.on_lane_a()
        self.write("README.md", README.replace("alpha two\n\n## Beta\n\nbeta one", "merged"))
        _, out = self.lane("split", "A", expect=1)
        self.assertEqual(out["outcome"], "dirty")
        self.commit("spanning edit")
        head = self.sha()
        _, out = self.lane("split", "A", expect=1)
        self.assertEqual(out["outcome"], "spans-sections")
        self.assertEqual(out["hunks"][0]["file"], "README.md")
        self.assertEqual(self.sha(), head)
        self.assertEqual(self.git("branch", "--list", "lane-a-shared-*"), "")

    # -- sync --------------------------------------------------------------
    def diverge(self, lane_text, main_text):
        """lane-a and main each rewrite README.md; returns lane-a's head."""
        self.on_lane_a()
        self.write("README.md", lane_text)
        self.commit("lane edit")
        head = self.sha()
        self.git("checkout", "-q", "main")
        self.write("README.md", main_text)
        self.commit("main edit")
        self.git("checkout", "-q", "lane-a")
        return head

    def merging(self):
        return self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) != ""

    def test_sync_merges_cleanly(self):
        self.diverge(README.replace("alpha one", "alpha 1"), README.replace("beta one", "beta 1"))
        _, out = self.lane("sync", "A", expect=0)
        self.assertEqual(out["outcome"], "merged")
        self.assertIn("alpha 1", self.read("README.md"))
        self.assertIn("beta 1", self.read("README.md"))

    def test_sync_aborts_on_conflict_outside_the_lane(self):
        head = self.diverge(README.replace("beta one", "beta lane"), README.replace("beta one", "beta main"))
        _, out = self.lane("sync", "A", expect=1)
        self.assertEqual(out["outcome"], "aborted")
        self.assertEqual([(c["file"], c["section"], c["owner"]) for c in out["conflicts"]],
                         [("README.md", "Beta", "B")])
        self.assertFalse(self.merging())
        self.assertEqual(self.sha(), head)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_sync_leaves_owned_conflicts_for_the_worker(self):
        self.diverge(README.replace("alpha one", "alpha lane"), README.replace("alpha one", "alpha main"))
        _, out = self.lane("sync", "A", expect=1)
        self.assertEqual(out["outcome"], "conflicts")
        self.assertEqual([(c["file"], c["section"], c["owner"]) for c in out["conflicts"]],
                         [("README.md", "Alpha", "A")])
        self.assertTrue(self.merging())

    # -- report ------------------------------------------------------------
    def test_report_lists_branches_and_the_starting_point_line(self):
        self.on_lane_a()
        self.write("README.md", README.replace("alpha one", "alpha 1").replace("beta two", "beta 2"))
        self.commit("mixed work")
        self.lane("split", "A", expect=0)
        _, out = self.lane("report", "A", expect=0)
        side = self.sha("lane-a-shared-readme-md")
        self.assertEqual(out["line"], "Starting point: %s (expected %s); shared-file branches: "
                         "lane-a-shared-readme-md: README.md#Beta" % (self.base, self.base))
        self.assertEqual(out["branch"], {"name": "lane-a", "sha": self.sha()})
        self.assertEqual(out["side_branches"], [{"name": "lane-a-shared-readme-md", "sha": side,
                                                  "changes": ["README.md#Beta"]}])

    def test_report_flags_a_wrong_starting_point(self):
        old = self.base
        self.write("src/b.py", "b = 7\n")
        self.commit("main moves on")
        self.base = self.sha()
        self.manifest()
        self.git("checkout", "-q", "-b", "lane-a", old)
        _, out = self.lane("report", "A", expect=1)
        self.assertEqual(out["line"], "Starting point: %s (expected %s); shared-file branches: none"
                         % (old, self.base))

    # -- integrate ---------------------------------------------------------
    def two_lanes(self, side_text, b_text):
        """lane-a owns Alpha and splits a Beta edit off; lane-b edits Beta itself."""
        self.manifest(checks=["test -f src/a.py"])
        self.git("checkout", "-q", "-b", "lane-a")
        self.write("src/a.py", "a = 8\n")
        self.write("README.md", README.replace("alpha one", "alpha A").replace("beta one", side_text))
        self.commit("lane a")
        self.lane("split", "A", expect=0)
        self.git("checkout", "-q", "-b", "lane-b", self.base)
        self.write("README.md", README.replace("beta four", b_text))
        self.commit("lane b")
        self.git("checkout", "-q", "main")

    def test_integrate_merges_lanes_then_side_branches_and_reruns_safely(self):
        self.two_lanes("beta side", "beta B")
        _, out = self.lane("integrate", expect=0)
        self.assertEqual(out["outcome"], "integrated")
        self.assertEqual(out["merged"], ["lane-a", "lane-b", "lane-a-shared-readme-md"])
        self.assertEqual(out["checks"], [{"cmd": "test -f src/a.py", "ok": True}])
        text = self.read("README.md")
        for piece in ("alpha A", "beta side", "beta B"):
            self.assertIn(piece, text)
        _, out = self.lane("integrate", expect=0)
        self.assertEqual(out["merged"], [])
        self.assertEqual(out["skipped"], ["lane-a", "lane-b", "lane-a-shared-readme-md"])

    def test_integrate_stops_at_the_first_conflict(self):
        self.two_lanes("beta side", "beta 4")
        self.git("checkout", "-q", "lane-b")
        self.write("README.md", README.replace("beta four", "beta 4").replace("beta one", "beta B"))
        self.commit("lane b touches the same line")
        self.git("checkout", "-q", "main")
        _, out = self.lane("integrate", expect=1)
        self.assertEqual(out["outcome"], "conflict")
        self.assertEqual(out["branch"], "lane-a-shared-readme-md")
        self.assertEqual(out["merged"], ["lane-a", "lane-b"])
        self.assertEqual([(c["file"], c["section"], c["owner"]) for c in out["conflicts"]],
                         [("README.md", "Beta", "B")])
        self.assertTrue(self.merging())

    def test_integrate_reports_a_failing_check_and_needs_the_target(self):
        self.two_lanes("beta side", "beta B")
        self.manifest(checks=["true", "exit 3"])
        self.git("checkout", "-q", "lane-a")
        _, out = self.lane("integrate", expect=1)
        self.assertEqual(out["outcome"], "wrong-branch")
        self.git("checkout", "-q", "main")
        _, out = self.lane("integrate", expect=1)
        self.assertEqual(out["outcome"], "check-failed")
        self.assertEqual(out["checks"][-1], {"cmd": "exit 3", "ok": False, "exit": 3})

    def test_usage_errors_exit_2(self):
        self.lane("start", "A", expect=2)  # no manifest yet
        self.manifest()
        self.lane("start", "Z", expect=2)
        self.lane("bogus", expect=2)


if __name__ == "__main__":
    unittest.main()
