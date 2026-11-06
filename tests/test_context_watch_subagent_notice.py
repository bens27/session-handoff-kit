#!/usr/bin/env python3
"""Agent-tool subagents get their own stop-and-report notice (pullboard #1).

A PostToolUse event carrying `agent_id` is measured from the subagent's own
transcript (.../<session_id>/subagents/agent-<agent_id>.jsonl). At or past the
`subagent_at` config key (default 100000) the hook tells the subagent once to
stop, commit WIP and hand back a short resumable report; about 20k later it
repeats once, more firmly; then it stays quiet. It never types /clear, never
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


class SubagentNotice(base.HookCase):
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

    def test_notice_once_then_firmer_repeat_then_silence(self):
        self.write_sub([20000, 99999])
        self.assertEqual(self.run_hook(), "")  # below the default 100000
        self.write_sub([20000, 100000])
        first = self.context(self.run_hook())
        self.assertIn("[context-watch]", first)
        for field in ("stop", "commit", "item id", "worktree", "lane", "claim state", "branch",
                      "last commit", "clean", "done", "remaining", "test command", "background",
                      "shout", "next command"):
            self.assertIn(field, first.lower(), field)
        self.assertNotIn("/clear", first)
        self.assertNotIn("session-handoff", first)
        self.write_sub([20000, 105000])
        self.assertEqual(self.run_hook(), "")  # once only until the repeat point
        self.write_sub([20000, 120000])
        second = self.context(self.run_hook())
        self.assertIn("stop now", second.lower())
        self.assertIn("commit", second.lower())
        self.assertNotEqual(first, second)
        self.write_sub([20000, 150000])
        self.assertEqual(self.run_hook(), "")  # no third notice, ever
        self.write_sub([20000, 190000])
        self.assertEqual(self.run_hook(), "")

    def test_state_keyed_by_agent_id_and_parent_untouched(self):
        self.write_sub([20000, 100000])
        self.assertIn("[context-watch]", self.context(self.run_hook()))
        self.assertTrue(any(AGENT in f for f in self.state_files()), self.state_files())
        self.assertEqual(self.parent_latches(), [])  # no parent latch, no parent checkpoint
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "handoffs")))
        # A second subagent in the same session gets its own notice.
        other = "f0e1d2c3b4a596877"
        other_path = os.path.join(os.path.dirname(self.sub), "agent-%s.jsonl" % other)
        with open(other_path, "w") as f:
            f.write(json.dumps(sidechain_call(100000)) + "\n")
        self.assertIn("[context-watch]", self.context(self.run_hook(evt_extra={"agent_id": other})))
        self.assertTrue(any(other in f for f in self.state_files()), self.state_files())
        # The parent's own event stays exactly as before: well under its threshold, silent.
        self.assertEqual(base.HookCase.run_hook(self, extra=NOTE), "")

    def test_subagent_at_config_key_project_over_user(self):
        os.makedirs(os.path.join(self.tmp, ".context-watch"))
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
        self.write_sub([20000, 100000])
        self.assertIn("[context-watch]", self.context(self.run_hook(transcript=self.sub)))

    def test_missing_subagent_transcript_stays_silent(self):
        os.remove(self.sub) if os.path.exists(self.sub) else None
        self.assertEqual(self.run_hook(), "")
        self.assertEqual(self.parent_latches(), [])


if __name__ == "__main__":
    unittest.main()
