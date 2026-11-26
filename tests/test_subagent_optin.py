#!/usr/bin/env python3
"""G32: the kit never cuts a subagent off below its real context window.

Without a `subagent_at` in a thresholds file (unset, null, zero or not an int)
an Agent-tool subagent gets no stop or handover notice at any occupancy, since
Claude Code compacts subagents itself (observed to 393k). A positive int still
opts in to one save-and-continue notice that never says stop and never repeats.
No fixed token default remains in the hook, the Herdr plugin or the
README.

Run: python3 -m pytest -q tests/test_subagent_optin.py"""
import json
import os
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_context_watch_subagent_notice as sub  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills/session-handoff/hooks"))
sys.path.insert(0, str(REPO / "herdr-plugin"))
import context_watch  # noqa: E402


class OptIn(sub.SubagentCase):
    def thresholds(self, data):
        os.makedirs(os.path.join(self.tmp, ".context-watch"), exist_ok=True)
        with open(os.path.join(self.tmp, ".context-watch", "thresholds.json"), "w") as f:
            json.dump(data, f)

    def assert_silent_to(self, tokens):
        for t in tokens:
            self.write_sub([20000, t])
            self.assertEqual(self.run_hook(), "", t)
        self.assertFalse([f for f in self.state_files() if "notice" in f], self.state_files())

    def test_no_thresholds_file_means_no_notice_at_any_occupancy(self):
        self.assert_silent_to([160000, 200000, 393000, 900000])

    def test_null_zero_and_non_int_mean_off(self):
        for value in (None, 0, -5, "160000", 160000.0, True):
            with self.subTest(value=value):
                self.thresholds({"subagent_at": value, "default": 130000})
                self.assert_silent_to([160000, 393000])

    def test_project_null_turns_off_a_user_value(self):
        self.thresholds({"subagent_at": 60000})
        with open(os.path.join(self.tmp, ".context-watch.json"), "w") as f:
            json.dump({"subagent_at": None}, f)
        self.assert_silent_to([60000, 393000])

    def test_positive_int_still_opts_in(self):
        self.thresholds({"subagent_at": 60000})
        self.write_sub([20000, 60000])
        self.assertIn("[context-watch]", self.context(self.run_hook()))

    def test_opted_in_notice_never_stops_and_never_repeats(self):  # G23/G24/G28 are wont
        self.thresholds({"subagent_at": 60000})
        self.write_sub([20000, 60000])
        low = self.context(self.run_hook()).lower()
        self.assertIn("keep working", low)
        for banned in ("stop", "end your turn", "context limit", "hand over", "planned handover"):
            self.assertNotIn(banned, low, banned)
        for tokens in (80000, 160000, 393000):
            self.write_sub([20000, tokens])
            self.assertEqual(self.run_hook(), "", tokens)
        self.assertFalse(hasattr(context_watch, "SUBAGENT_REPEAT_LEAD"))

    def test_no_fixed_default_in_the_hook(self):
        from unittest import mock
        self.assertFalse(hasattr(context_watch, "SUBAGENT_AT_DEFAULT"))
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            self.assertIsNone(context_watch.subagent_threshold(self.tmp))


class Plugin(unittest.TestCase):
    def setUp(self):
        import thresholds
        self.t = thresholds

    def state(self, value, scope="global"):
        s = self.t.State.__new__(self.t.State)
        s.scope, s.message, s.sources = scope, "", {}
        s.warning, s.handoff, s.auto, s.auto_max, s.subagent_at = 85000, 90000, False, 10, value
        return s

    def test_default_is_off(self):
        self.assertIsNone(self.t.DEFAULTS["subagent_at"])
        self.assertIn(" off ", self.state(None).setting_lines()[2])

    def test_keys_opt_in_from_the_handoff_point_and_back_off(self):
        s = self.state(None)
        s.key("u")
        self.assertIsNone(s.subagent_at)
        s.key("U")
        self.assertEqual(s.subagent_at, 90000)  # starts at the parent's handoff point
        s.subagent_at = self.t.STEP
        s.key("u")
        self.assertIsNone(s.subagent_at)

    def test_saving_off_writes_null_keeping_other_keys(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "thresholds.json")
            with open(path, "w") as f:
                json.dump({"subagent_at": 60000, "claude": 180000}, f)
            with mock.patch.object(self.t, "global_path", return_value=path):
                self.t.save_global(85000, 90000, subagent_at=None)
                with open(path) as f:
                    self.assertEqual(json.load(f)["subagent_at"], 60000)  # not given: kept
                self.t.save_global(85000, 90000, subagent_at=self.t.OFF)
            with open(path) as f:
                data = json.load(f)
        self.assertIsNone(data["subagent_at"])
        self.assertEqual(data["claude"], 180000)


class Docs(unittest.TestCase):
    def test_readme_and_hook_doc_state_no_subagent_token_default(self):
        texts = {"README.md": (REPO / "README.md").read_text(),
                 "context_watch.py": (REPO / "skills/session-handoff/hooks/context_watch.py").read_text()}
        pattern = re.compile(r"subagent_at`?\"?\)?\s*\(?[^.\n]{0,30}default[^.\n]{0,10}\d", re.I)
        self.assertEqual({k: pattern.findall(v) for k, v in texts.items() if pattern.search(v)}, {})


if __name__ == "__main__":
    unittest.main()
