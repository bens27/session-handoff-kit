#!/usr/bin/env python3
"""Regression tests for context_watch.py hook-review tickets (floor suppression,
Codex compaction, event-correct telemetry errors). Subprocess seam: hook stdin ->
stdout, with a throwaway HOME/TMPDIR and synthetic transcripts only.

Run: python3 tests/test_context_watch_regressions.py"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "skills", "session-handoff", "hooks", "context_watch.py")
CLEAN = ("HANDOFF_AT", "AUTORESUME", "HANDOFF_AUTO", "HANDOFF_AUTO_RUNNER", "TMUX_PANE",
         "AGENTSROOM_AGENT_ID", "CONTEXT_WATCH_AUTORESUME", "CONTEXT_WATCH_TOKENS",
         "CONTEXT_WATCH_TOKENS_MAP", "CONTEXT_WATCH_PERCENT", "CONTEXT_WATCH_MODE",
         "CONTEXT_WATCH_WINDOW", "CONTEXT_WATCH_RESERVE", "CONTEXT_WATCH_THINKING")


def codex_count(last, total, window=258400):
    """token_count the way Codex writes it: last_token_usage + running totals."""
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"total_tokens": last},
        "total_token_usage": {"total_tokens": total},
        "model_context_window": window}}}


COMPACTED = {"type": "compacted", "payload": {"message": "", "replacement_history": []}}


def claude_call(tokens):
    return {"message": {"model": "claude-sonnet-5-5", "usage": {"input_tokens": tokens}}}


class HookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.sid = "regress-" + os.path.basename(self.tmp)
        self.transcript = os.path.join(self.tmp, "transcript.jsonl")
        self.codex = False

    def write(self, entries, mode="w"):
        with open(self.transcript, mode) as f:
            for e in entries:
                f.write((json.dumps(e) if not isinstance(e, str) else e) + "\n")

    def run_hook(self, event="PostToolUse", transcript=None, extra=None):
        env = {k: v for k, v in os.environ.items() if k not in CLEAN}
        env.update(HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0",
                   CONTEXT_WATCH_AGENT="codex" if self.codex else "claude")
        env.update(extra or {})
        evt = {"hook_event_name": event, "session_id": self.sid, "cwd": self.tmp,
               "transcript_path": self.transcript if transcript is None else transcript}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def context(self, out):
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


class FloorPolicy(HookCase):
    def setUp(self):
        super().setUp()
        self.codex = True

    def test_startup_floor_note_does_not_silence_later_pressure(self):
        self.write([codex_count(125000, 125000)])
        self.assertEqual(self.run_hook(), "")  # below threshold
        self.write([codex_count(140000, 140000)], "a")
        floor = self.context(self.run_hook())
        self.assertIn("startup context", floor)
        self.assertNotIn("reason: context-pressure", floor)
        self.write([codex_count(150000, 150000)], "a")
        self.assertEqual(self.run_hook(), "")  # floor note is once, no handoff yet
        self.write([codex_count(240000, 240000)], "a")  # window 258400, 18k left
        warn = self.context(self.run_hook())
        self.assertIn("reason: context-pressure", warn)

    def test_jump_past_effective_limit_warns_directly(self):
        self.write([codex_count(125000, 125000), codex_count(245000, 245000)])
        self.assertIn("reason: context-pressure", self.context(self.run_hook()))


class NormalPolicyUnchanged(HookCase):
    def test_second_notice_and_compaction_rearm(self):
        self.write([claude_call(20000), claude_call(140000)])
        first = self.context(self.run_hook(extra={"CONTEXT_WATCH_THINKING": "0"}))
        self.assertIn("reason: context-pressure", first)
        self.assertNotIn("SECOND NOTICE", first)
        self.write([claude_call(175000)], "a")
        second = self.context(self.run_hook(extra={"CONTEXT_WATCH_THINKING": "0"}))
        self.assertIn("SECOND NOTICE", second)
        self.write([claude_call(20000)], "a")  # compaction
        self.assertEqual(self.run_hook(extra={"CONTEXT_WATCH_THINKING": "0"}), "")
        self.write([claude_call(140000)], "a")
        self.assertIn("reason: context-pressure",
                      self.context(self.run_hook(extra={"CONTEXT_WATCH_THINKING": "0"})))


class AgentsRoomRestart(HookCase):
    def trigger(self, extra):
        self.write([claude_call(20000), claude_call(140000)])
        return self.context(self.run_hook(extra=dict(extra, CONTEXT_WATCH_THINKING="0")))

    def test_auto_agentsroom_trigger_asks_agent_to_restart_itself(self):
        msg = self.trigger({"HANDOFF_AUTO": "1", "AGENTSROOM_AGENT_ID": "agent-7"})
        self.assertIn("agents_restart", msg)
        self.assertIn("resume", msg.split("agents_restart", 1)[1])

    def test_no_restart_instruction_in_tmux_or_without_auto(self):
        tmux = self.trigger({"HANDOFF_AUTO": "1", "AGENTSROOM_AGENT_ID": "agent-7",
                             "TMUX_PANE": "%1"})
        self.assertIn("reason: context-pressure", tmux)
        self.assertNotIn("agents_restart", tmux)
        self.setUp()
        self.assertNotIn("agents_restart", self.trigger({"AGENTSROOM_AGENT_ID": "agent-7"}))


class CodexCompaction(HookCase):
    def setUp(self):
        super().setUp()
        self.codex = True

    def test_compacted_record_invalidates_prior_usage(self):
        # Codex replays the pre-compaction token_count right after `compacted`.
        self.write([codex_count(50000, 50000), codex_count(140000, 190000), COMPACTED])
        self.assertEqual(self.run_hook(), "")
        self.write([codex_count(140000, 190000),
                    {"type": "event_msg", "payload": {"type": "context_compacted"}}], "a")
        self.assertEqual(self.run_hook(), "")

    def test_fresh_measurements_and_rearming_after_compaction(self):
        self.write([codex_count(50000, 50000), codex_count(140000, 190000)])
        self.assertIn("reason: context-pressure", self.context(self.run_hook()))
        self.write([COMPACTED, codex_count(140000, 190000), codex_count(30000, 220000)], "a")
        self.assertEqual(self.run_hook(), "")  # fresh low usage re-arms
        self.write([codex_count(150000, 370000)], "a")
        self.assertIn("reason: context-pressure", self.context(self.run_hook()))


class TelemetryErrors(HookCase):
    def assert_event_diag(self, out, event):
        env = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(env["hookEventName"], event)
        self.assertIn("telemetry", env["additionalContext"].lower())
        self.assertLess(len(env["additionalContext"]), 400)

    def test_malformed_transcript_reports_under_triggering_event_once(self):
        self.write([[]])  # valid JSON, not a transcript entry
        self.assert_event_diag(self.run_hook("PostToolUse"), "PostToolUse")
        self.assertEqual(self.run_hook("PostToolUse"), "")

    def test_garbage_lines_report_under_user_prompt_submit(self):
        self.write(["not json", "{broken"])
        self.assert_event_diag(self.run_hook("UserPromptSubmit"), "UserPromptSubmit")

    def test_missing_transcript_reports_on_post_tool_use(self):
        self.assert_event_diag(self.run_hook("PostToolUse", transcript=os.path.join(self.tmp, "nope")),
                               "PostToolUse")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_unreadable_transcript_reports(self):
        self.write([claude_call(1000)])
        os.chmod(self.transcript, 0)
        self.assert_event_diag(self.run_hook("PostToolUse"), "PostToolUse")

    def test_valid_sessions_are_silent(self):
        self.write([])  # legitimately empty: no usage yet
        self.assertEqual(self.run_hook("PostToolUse"), "")
        self.write([{"type": "user", "message": {"content": "hi"}}])  # valid, no usage yet
        self.assertEqual(self.run_hook("PostToolUse"), "")
        self.write([claude_call(1000)])
        self.assertEqual(self.run_hook("PostToolUse"), "")

    def test_missing_transcript_on_first_prompt_is_not_an_error(self):
        out = self.run_hook("UserPromptSubmit", transcript=os.path.join(self.tmp, "nope"))
        self.assertNotIn("telemetry", out.lower())  # only the opening-prompt router may speak

    def test_unexpected_exception_keeps_event_and_is_bounded(self):
        spec = importlib.util.spec_from_file_location("context_watch_under_test", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        os.environ["TMPDIR"] = self.tmp
        self.addCleanup(os.environ.pop, "TMPDIR", None)
        import tempfile as tf
        tf.tempdir = self.tmp
        self.addCleanup(setattr, tf, "tempdir", None)
        evt = {"hook_event_name": "PostToolUse", "session_id": self.sid}
        outs = []
        for _ in range(2):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                mod.fail_open(RuntimeError("boom"), evt)
            outs.append(buf.getvalue().strip())
        self.assert_event_diag_any(outs[0], "PostToolUse")
        self.assertEqual(outs[1], "")

    def assert_event_diag_any(self, out, event):
        env = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(env["hookEventName"], event)
        self.assertIn("RuntimeError", env["additionalContext"])
        self.assertNotIn("boom", env["additionalContext"])

    def test_session_start_failure_keeps_legacy_envelope(self):
        spec = importlib.util.spec_from_file_location("context_watch_under_test2", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            mod.fail_open(RuntimeError("x"), {"hook_event_name": "SessionStart"})
        env = json.loads(buf.getvalue())["hookSpecificOutput"]
        self.assertEqual(env["hookEventName"], "SessionStart")
        self.assertIn("Handoff service unavailable", env["additionalContext"])


if __name__ == "__main__":
    unittest.main()
