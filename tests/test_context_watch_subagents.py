#!/usr/bin/env python3
"""Subagent-shaped hook events must not receive the parent session's handoff
notices. Claude Code runs the same hooks inside Agent-tool subagents, with the
parent's session_id/transcript_path plus `agent_id`/`agent_type`.

Run: python3 tests/test_context_watch_subagents.py"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_context_watch_regressions as base  # noqa: E402

NOTE = {"CONTEXT_WATCH_THINKING": "0"}

# A real PostToolUse event from an Agent-tool subagent (`claude -p`, haiku,
# 2026-10-02; tool_input/tool_response trimmed). session_id and transcript_path
# are the PARENT's; only agent_id/agent_type mark the subagent. The same run
# fired no UserPromptSubmit or Stop inside the subagent, and SubagentStop
# carried the subagent's own transcript as `agent_transcript_path`
# (.../<session_id>/subagents/agent-<agent_id>.jsonl).
CAPTURED_SUBAGENT_POST_TOOL_USE = {
    "session_id": "853d82ae-a9f0-48fd-a092-7f05baf0ccc1",
    "transcript_path": "/Users/u/.claude/projects/-private-tmp-capture/"
                       "853d82ae-a9f0-48fd-a092-7f05baf0ccc1.jsonl",
    "cwd": "/private/tmp/capture",
    "prompt_id": "10f3989d-3d72-4cd8-8830-f106eece976e",
    "permission_mode": "bypassPermissions",
    "agent_id": "a1b2e6d85a702a7d3",
    "agent_type": "general-purpose",
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hi", "description": "Run echo command"},
    "tool_response": {"stdout": "hi", "stderr": "", "interrupted": False},
    "tool_use_id": "toolu_01A6ovvkTwRxozBe9Egs4qcp",
    "duration_ms": 2019,
}


class SubagentEvents(base.HookCase):
    def run_hook(self, event="PostToolUse", transcript=None, extra=None, evt_extra=None):
        # Base run_hook cannot add event fields (agent_id), so build the event here.
        env = {k: v for k, v in os.environ.items() if k not in base.CLEAN}
        env.update(HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0",
                   CONTEXT_WATCH_AGENT="claude")
        env.update(extra or {})
        evt = {"hook_event_name": event, "session_id": self.sid, "cwd": self.tmp,
               "transcript_path": self.transcript if transcript is None else transcript}
        evt.update(evt_extra or {})
        p = subprocess.run([sys.executable, base.HOOK], input=json.dumps(evt), env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def test_subagent_event_gets_no_parent_notice_and_leaves_parent_latch(self):
        self.write([base.claude_call(20000), base.claude_call(140000)])
        sub = {"agent_id": "agent-a1b2c3", "agent_type": "general-purpose"}
        self.assertEqual(self.run_hook(extra=NOTE, evt_extra=sub), "")
        self.assertEqual(self.run_hook("UserPromptSubmit", extra=NOTE, evt_extra=sub), "")
        # The parent's own event still fires its notice: the subagent did not eat the latch.
        self.assertIn("reason: context-pressure", self.context(self.run_hook(extra=NOTE)))

    def test_captured_subagent_payload_gets_no_parent_notice(self):
        self.write([base.claude_call(20000), base.claude_call(140000)])
        captured = dict(CAPTURED_SUBAGENT_POST_TOOL_USE)
        self.assertNotIn("subagents", captured["transcript_path"])  # parent's transcript
        # Point the payload at this test's parent session: only agent_id can tell them apart.
        captured.update(session_id=self.sid, transcript_path=self.transcript, cwd=self.tmp)
        self.assertEqual(self.run_hook(extra=NOTE, evt_extra=captured), "")
        self.assertIn("reason: context-pressure", self.context(self.run_hook(extra=NOTE)))

    def test_subagent_transcript_path_alone_is_detected(self):
        sub_path = os.path.join(self.tmp, "proj", self.sid, "subagents", "agent-a1b2c3.jsonl")
        os.makedirs(os.path.dirname(sub_path))
        with open(sub_path, "w") as f:
            for tokens in (20000, 140000):
                f.write(json.dumps(base.claude_call(tokens)) + "\n")
        self.assertEqual(self.run_hook(transcript=sub_path, extra=NOTE), "")

    def test_main_thread_event_is_unaffected(self):
        self.write([base.claude_call(20000), base.claude_call(140000)])
        self.assertIn("reason: context-pressure", self.context(self.run_hook(extra=NOTE)))


if __name__ == "__main__":
    unittest.main()
