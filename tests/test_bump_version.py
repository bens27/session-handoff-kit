#!/usr/bin/env python3
"""scripts/bump-version: subprocess seam against a temporary copy of the three
files that carry the session-handoff version (via --root).

Run: python3 tests/test_bump_version.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCRIPT = os.path.join(REPO, "scripts", "bump-version")
FILES = ("plugins/session-handoff/.claude-plugin/plugin.json",
         "skills/session-handoff/SKILL.md", "SPEC.md")


class BumpVersion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        for rel in FILES:
            os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
            shutil.copy(os.path.join(REPO, rel), os.path.join(self.root, rel))

    def run_script(self, *args):
        p = subprocess.run([sys.executable, SCRIPT, "--root", self.root] + list(args),
                           capture_output=True, text=True, timeout=30)
        lines = p.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, p.stdout + p.stderr)
        return p.returncode, json.loads(lines[0])

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def snapshot(self):
        return {rel: self.read(rel) for rel in FILES}

    def current(self):
        rc, out = self.run_script("--check")
        self.assertEqual((rc, out["outcome"]), (0, "consistent"), out)
        return out["version"]

    def test_check_reports_consistent_version(self):
        self.assertRegex(self.current(), r"^\d+\.\d+\.\d+$")

    def test_bump_rewrites_exactly_the_three_occurrences(self):
        old, before = self.current(), self.snapshot()
        rc, out = self.run_script("9.8.7")
        self.assertEqual(rc, 0, out)
        self.assertEqual({k: out[k] for k in ("outcome", "from", "to", "files")},
                         {"outcome": "bumped", "from": old, "to": "9.8.7", "files": list(FILES)})
        self.assertTrue(out.get("next"))
        self.assertEqual(json.loads(self.read(FILES[0]))["version"], "9.8.7")
        self.assertIn('  version: "9.8.7"\n', self.read(FILES[1]))
        line4 = self.read("SPEC.md").splitlines()[3]
        self.assertIn("`session-handoff` skill/plugin 9.8.7", line4)
        old_line4 = before["SPEC.md"].splitlines()[3]
        self.assertEqual(line4, old_line4.replace("skill/plugin " + old, "skill/plugin 9.8.7"))
        for rel in FILES:  # nothing else changed
            self.assertEqual(self.read(rel).replace("9.8.7", old), before[rel])
        self.assertEqual(self.current(), "9.8.7")

    def test_mismatch_refuses_and_changes_nothing(self):
        old = self.current()
        text = self.read(FILES[1]).replace('version: "%s"' % old, 'version: "0.0.1"')
        with open(os.path.join(self.root, FILES[1]), "w") as f:
            f.write(text)
        before = self.snapshot()
        for args in (("1.2.3",), ("--check",)):
            rc, out = self.run_script(*args)
            self.assertEqual(rc, 1, out)
            self.assertEqual(out["outcome"], "mismatch")
            self.assertEqual(out["versions"], {FILES[0]: old, FILES[1]: "0.0.1", FILES[2]: old})
            self.assertTrue(out.get("next"))
        self.assertEqual(self.snapshot(), before)

    def test_invalid_version_refused(self):
        before = self.snapshot()
        for bad in ("1.2", "v1.2.3", "1.2.3.4", "01.2.3", "1.2.x"):
            rc, out = self.run_script(bad)
            self.assertEqual((rc, out["outcome"]), (2, "invalid"), (bad, out))
        self.assertEqual(self.snapshot(), before)

    def test_missing_occurrence_reported(self):
        with open(os.path.join(self.root, "SPEC.md"), "w") as f:
            f.write("# no version line\n")
        rc, out = self.run_script("--check")
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["outcome"], "mismatch")
        self.assertIsNone(out["versions"]["SPEC.md"])

    def test_same_version_is_noop_bump(self):
        old = self.current()
        before = self.snapshot()
        rc, out = self.run_script(old)
        self.assertEqual((rc, out["outcome"], out["from"], out["to"]), (0, "bumped", old, old))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
