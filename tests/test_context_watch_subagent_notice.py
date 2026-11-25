#!/usr/bin/env python3
"""Agent-tool subagents that opt in get one save-and-continue notice (G32).

A PostToolUse event carrying `agent_id` is measured from the subagent's own
transcript (.../<session_id>/subagents/agent-<agent_id>.jsonl). At or past the
`subagent_at` config key (opt-in since G32) the hook tells the subagent once to
write its findings and work in progress to its own checkpoint and keep working;
then it stays quiet. It never types /clear, never
touches the parent's checkpoint or latch, and keys its state by agent_id.

Run: python3 -m pytest -q tests/test_context_watch_subagent_notice.py"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_context_watch_regressions as base  # noqa: E402

AGENT = "a1b2e6d85a702a7d3"
NOTE = {"CONTEXT_WATCH_THINKING": "0"}


def sidechain_call(tokens):
    # Real subagent transcripts mark every entry isSidechain: true.
    return {"isSidechain": True, "type": "assistant",
            "message": {"model": "claude-sonnet-5-5", "usage": {
                "input_tokens": 2, "cache_read_input_tokens": tokens - 2,
                "cache_creation_input_tokens": 0, "output_tokens": 0}}}


def context_watch_checkpoint(home, agent_id, parent):
    sys.path.insert(0, os.path.dirname(base.HOOK))
    import context_watch
    from unittest import mock
    with mock.patch.dict(os.environ, {"HOME": home}):
        return context_watch.subagent_checkpoint_path(agent_id, parent)


class SubagentCase(base.HookCase):
    """Subagent transcript beside a calm parent; shared by the handover tests."""
    def setUp(self):
        super().setUp()
        self.transcript = os.path.join(self.tmp, "proj", self.sid + ".jsonl")
        self.sub = os.path.join(self.tmp, "proj", self.sid, "subagents", "agent-%s.jsonl" % AGENT)
        os.makedirs(os.path.dirname(self.sub))
        self.write([base.claude_call(20000)])  # the parent is far from pressure

    def write_sub(self, tokens):
        with open(self.sub, "w") as f:
            for t in tokens:
                f.write(json.dumps(sidechain_call(t)) + "\n")

    def run_hook(self, event="PostToolUse", transcript=None, extra=None, evt_extra=None):
        env = {k: v for k, v in os.environ.items() if k not in base.CLEAN}
        env.update(HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0",
                   CONTEXT_WATCH_AGENT="claude")
        env.update(NOTE)
        env.update(extra or {})
        evt = {"hook_event_name": event, "session_id": self.sid, "cwd": self.tmp,
               "transcript_path": self.transcript if transcript is None else transcript,
               "agent_id": AGENT, "agent_type": "general-purpose", "tool_name": "Bash"}
        evt.update(evt_extra or {})
        p = subprocess.run([sys.executable, base.HOOK], input=json.dumps(evt), env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def state_files(self):
        root = os.path.join(self.tmp, ".context-watch")
        return sorted(os.path.relpath(os.path.join(d, f), root)
                      for d, _, fs in os.walk(root) for f in fs)

    def parent_latches(self):
        return sorted(f for f in os.listdir(self.tmp) if f.startswith("context-watch-"))


class SubagentNotice(SubagentCase):
    def setUp(self):
        super().setUp()
        # subagent_at is opt-in (G32); these tests opt in at the old 160000.
        os.makedirs(os.path.join(self.tmp, ".context-watch"))
        with open(os.path.join(self.tmp, ".context-watch", "thresholds.json"), "w") as f:
            json.dump({"subagent_at": 160000}, f)

    def test_one_save_and_continue_notice_then_silence(self):
        self.write_sub([20000, 159999])
        self.assertEqual(self.run_hook(), "")  # below the opted-in 160000
        self.write_sub([20000, 160000])
        first = self.context(self.run_hook())
        self.assertIn("[context-watch]", first)
        low = first.lower()
        for field in ("findings", "work in progress", "keep working", "agent_id", "parent_session_id",
                      "never claim, supersede or edit the parent"):
            self.assertIn(field, low, field)
        self.assertIn(context_watch_checkpoint(self.tmp, AGENT, self.sid), first)
        for banned in ("stop", "end your turn", "context limit", "hand over", "/clear", "session-handoff"):
            self.assertNotIn(banned, low, banned)
        for tokens in (165000, 180000, 250000, 393000):
            self.write_sub([20000, tokens])
            self.assertEqual(self.run_hook(), "", tokens)  # one notice, never a repeat

    def test_checkpoint_path_scoped_to_agent_not_parent_ledger(self):
        a = context_watch_checkpoint(self.tmp, "agent-x1", "parent-s")
        self.assertNotEqual(a, context_watch_checkpoint(self.tmp, "agent-x2", "parent-s"))
        self.assertIn("agent-x1", a)
        self.assertIn("parent-s", a)
        self.assertNotIn(os.sep + ".handoffs" + os.sep, a)

    def test_state_keyed_by_agent_id_and_parent_untouched(self):
        self.write_sub([20000, 160000])
        self.assertIn("[context-watch]", self.context(self.run_hook()))
        self.assertTrue(any(AGENT in f for f in self.state_files()), self.state_files())
        self.assertEqual(self.parent_latches(), [])  # no parent latch, no parent checkpoint
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "handoffs")))
        # A second subagent in the same session gets its own notice.
        other = "f0e1d2c3b4a596877"
        other_path = os.path.join(os.path.dirname(self.sub), "agent-%s.jsonl" % other)
        with open(other_path, "w") as f:
            f.write(json.dumps(sidechain_call(160000)) + "\n")
        self.assertIn("[context-watch]", self.context(self.run_hook(evt_extra={"agent_id": other})))
        self.assertTrue(any(other in f for f in self.state_files()), self.state_files())
        # The parent's own event stays exactly as before: well under its threshold, silent.
        self.assertEqual(base.HookCase.run_hook(self, extra=NOTE), "")

    def test_subagent_at_config_key_project_over_user(self):
        with open(os.path.join(self.tmp, ".context-watch", "thresholds.json"), "w") as f:
            json.dump({"subagent_at": 60000, "default": 130000}, f)
        self.write_sub([20000, 59000])
        self.assertEqual(self.run_hook(), "")
        self.write_sub([20000, 60000])
        self.assertIn("[context-watch]", self.context(self.run_hook()))
        with open(os.path.join(self.tmp, ".context-watch.json"), "w") as f:
            json.dump({"subagent_at": 70000}, f)
        other = "0123456789abcdef0"
        with open(os.path.join(os.path.dirname(self.sub), "agent-%s.jsonl" % other), "w") as f:
            f.write(json.dumps(sidechain_call(65000)) + "\n")
        self.assertEqual(self.run_hook(evt_extra={"agent_id": other}), "")
        # subagent_at is never a model threshold for the parent: parent at 20k stays silent.
        self.assertEqual(base.HookCase.run_hook(self, extra=NOTE), "")

    def test_subagent_transcript_path_in_event_is_used_directly(self):
        self.write_sub([20000, 160000])
        self.assertIn("[context-watch]", self.context(self.run_hook(transcript=self.sub)))

    def test_missing_subagent_transcript_stays_silent(self):
        os.remove(self.sub) if os.path.exists(self.sub) else None
        self.assertEqual(self.run_hook(), "")
        self.assertEqual(self.parent_latches(), [])


if __name__ == "__main__":
    unittest.main()
