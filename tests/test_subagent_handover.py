#!/usr/bin/env python3
"""Subagent limit and planned handover (G23, G24).

A subagent gets its own limit, subagent_at, 160000 by default and above the
parent's default threshold. At it the subagent is told to hand over: save its
own subagent-scoped checkpoint (agent_id, parent session id, objective, done,
next), never the parent's; commit WIP unless its brief forbids commits; end
with a short report naming the checkpoint path and the exact resume command,
as a planned handover, not a failure. Origin: a subagent that stopped at 81k
in 2026-11-22 and reported a context-limit failure.

Run: python3 -m pytest -q tests/test_subagent_handover.py"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_context_watch_subagent_notice as notice  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(ROOT, "skills", "session-handoff", "hooks")
sys.path.insert(0, HOOKS)
import context_watch  # noqa: E402


def load_thresholds():
    spec = importlib.util.spec_from_file_location(
        "herdr_thresholds", os.path.join(ROOT, "herdr-plugin", "thresholds.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SubagentLimit(unittest.TestCase):
    def test_default_is_160k_and_above_parent_default(self):
        self.assertEqual(context_watch.SUBAGENT_AT_DEFAULT, 160000)
        self.assertGreater(context_watch.SUBAGENT_AT_DEFAULT, context_watch.DEFAULT_TOKENS)

    def test_herdr_plugin_default_follows_hook(self):
        self.assertEqual(load_thresholds().DEFAULTS["subagent_at"], 160000)


class SubagentHandover(notice.SubagentCase):
    def checkpoint(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            return context_watch.subagent_checkpoint_path(notice.AGENT, self.sid)

    def test_silent_below_160k_then_handover_at_it(self):
        self.write_sub([20000, 159999])
        self.assertEqual(self.run_hook(), "")
        self.write_sub([20000, 160000])
        msg = self.context(self.run_hook())
        low = msg.lower()
        self.assertIn("planned handover", low)
        self.assertIn("not a failure", low)
        # its own checkpoint, keyed by agent_id, naming the parent session
        self.assertIn(notice.AGENT, msg)
        self.assertIn(self.sid, msg)
        self.assertIn(self.checkpoint(), msg)
        for field in ("agent_id", "parent_session_id", "objective", "done", "next"):
            self.assertIn(field, low, field)
        self.assertIn("never claim, supersede or edit the parent", low)
        self.assertIn("commit", low)
        self.assertIn("unless your brief forbids commits", low)
        self.assertIn("resume command", low)
        self.assertNotIn("do not save a checkpoint", low)
        self.assertNotIn("/clear", msg)
        # the hook itself writes nothing into the parent's ledger or latch
        self.assertEqual(self.parent_latches(), [])
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, ".handoffs")))

    def test_firmer_repeat_keeps_the_handover(self):
        self.write_sub([20000, 160000])
        self.context(self.run_hook())
        self.write_sub([20000, 180000])
        second = self.context(self.run_hook())
        self.assertIn("stop now", second.lower())
        self.assertIn("planned handover", second.lower())
        self.assertIn(self.checkpoint(), second)

    def test_checkpoint_path_scoped_to_agent_not_parent(self):
        a = context_watch.subagent_checkpoint_path("agent-x1", "parent-s")
        b = context_watch.subagent_checkpoint_path("agent-x2", "parent-s")
        self.assertNotEqual(a, b)
        self.assertIn("agent-x1", a)
        self.assertIn("parent-s", a)
        self.assertNotIn(os.sep + ".handoffs" + os.sep, a)


if __name__ == "__main__":
    unittest.main()
