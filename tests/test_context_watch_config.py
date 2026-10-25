#!/usr/bin/env python3
"""context_watch.py configuration contracts: one canonical ledger path, auto-mode
settings from config files, and the per-terminal session pointer. Subprocess
seam: hook stdin -> stdout, with a throwaway HOME/TMPDIR.

Run: python3 tests/test_context_watch_config.py"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.realpath(os.path.join(HERE, "..", "skills", "session-handoff"))
HOOKS = os.path.join(SKILL, "hooks")
HOOK = os.path.join(HOOKS, "context_watch.py")
CLEAN = ("HANDOFF_AT", "AUTORESUME", "HANDOFF_AUTO", "HANDOFF_AUTO_MAX", "HANDOFF_AUTO_RUNNER",
         "TMUX_PANE", "AGENTSROOM_AGENT_ID", "HANDOFF_TERMINAL_ID", "CONTEXT_WATCH_AUTORESUME",
         "CONTEXT_WATCH_TOKENS", "CONTEXT_WATCH_TOKENS_MAP", "CONTEXT_WATCH_PERCENT",
         "CONTEXT_WATCH_MODE", "CONTEXT_WATCH_WINDOW", "CONTEXT_WATCH_RESERVE",
         "CONTEXT_WATCH_THINKING", "HANDOFF_RUN_ID")
BODY = "## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun checks.\n"


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)
        self.transcript = os.path.join(self.tmp, "transcript.jsonl")
        self.n = 0

    def env(self, extra=None):
        env = {k: v for k, v in os.environ.items() if k not in CLEAN}
        env.update(HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0",
                   CONTEXT_WATCH_AGENT="claude", CONTEXT_WATCH_PENDING="0", TYPESAFE_API_KEY="")
        env.update(extra or {})
        return env

    def run_hook(self, event, extra=None, hook=HOOK, **fields):
        self.n += 1
        evt = dict(hook_event_name=event, session_id="cfg-%d" % self.n, cwd=self.project,
                   transcript_path=self.transcript, **fields)
        p = subprocess.run([sys.executable, hook], input=json.dumps(evt), env=self.env(extra),
                           cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.strip()
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def pressure(self, tokens, extra=None, hook=HOOK):
        with open(self.transcript, "w") as f:  # small first call: no startup-floor note
            for n in (1000, tokens):
                f.write(json.dumps({"message": {"model": "claude-test", "usage": {"input_tokens": n}}}) + "\n")
        return self.run_hook("PostToolUse", extra, hook)

    def save_handoff(self):
        p = subprocess.run([sys.executable, os.path.join(HOOKS, "handoff_ledger.py"), "save", ".",
                            "--session", "old", "--request-id", "c1"],
                           input=json.dumps({"topic": "work", "description": "Fix it.", "body": BODY}),
                           env=self.env({"AGENTSROOM_AGENT_ID": "agent-path"}), cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)


def ledger_paths(text):
    return set(re.findall(r"python3 (\S+handoff_ledger\.py)", text))


class CanonicalLedgerPath(Case):
    def both_paths(self, linked_hook):
        self.save_handoff()
        outs = []
        for hook in (linked_hook, HOOK):
            notice = self.pressure(140000, hook=hook)
            status = self.run_hook("UserPromptSubmit", {"AGENTSROOM_AGENT_ID": "agent-path"}, hook, prompt="continue the handoff")
            self.assertRegex(status, " (prepare|resume) ")
            outs.append((ledger_paths(notice), ledger_paths(status)))
        (n1, s1), (n2, s2) = outs
        self.assertEqual(len(n1 | s1), 1, outs)
        self.assertEqual(n1 | s1, n2 | s2, outs)
        return next(iter(n1))

    def test_installed_symlink_and_repo_path_print_the_installed_path(self):
        skills = os.path.join(self.tmp, ".agents", "skills")
        os.makedirs(skills)
        os.symlink(SKILL, os.path.join(skills, "session-handoff"))
        installed = os.path.join(skills, "session-handoff", "hooks")
        self.assertEqual(self.both_paths(os.path.join(installed, "context_watch.py")),
                         os.path.join(installed, "handoff_ledger.py"))

    def test_without_install_both_paths_print_the_resolved_sibling(self):
        link = os.path.join(self.tmp, "linked-skill")
        os.symlink(SKILL, link)
        self.assertEqual(self.both_paths(os.path.join(link, "hooks", "context_watch.py")),
                         os.path.join(HOOKS, "handoff_ledger.py"))


class AutoSettingsFromConfig(Case):
    def project_config(self, data):
        with open(os.path.join(self.project, ".context-watch.json"), "w") as f:
            json.dump(data, f)

    def test_project_file_enables_auto_mode_at_its_threshold(self):
        self.project_config({"auto": True, "at": 70000})
        self.assertEqual(self.pressure(65000, {"CONTEXT_WATCH_WARNING_WINDOW": "0"}), "")  # no notice yet
        msg = self.pressure(75000, {"AGENTSROOM_AGENT_ID": "agent-x"})
        self.assertIn("70,000-token handoff threshold", msg)
        self.assertIn("Fully automatic mode is active", msg)
        self.assertIn("agents_restart", msg)
        self.assertIn("restart_preflight", msg)
        self.assertIn("manual fresh-session continuation", msg)

    def test_agentsroom_pressure_routes_to_supported_restart_procedure(self):
        self.project_config({"auto": True, "at": 70000})
        msg = self.pressure(75000, {"AGENTSROOM_AGENT_ID": "agent-x"})
        self.assertIn("restart_preflight.py restart --root", msg)
        self.assertNotIn("reference.md", msg)
        self.assertIn("restart_prompt", msg)
        self.assertNotIn("without a host adapter", msg)

    def test_environment_overrides_the_file(self):
        self.project_config({"auto": True, "at": 70000})
        msg = self.pressure(75000, {"AGENTSROOM_AGENT_ID": "agent-x", "HANDOFF_AUTO": "0", "HANDOFF_AT": "72000"})
        self.assertIn("72,000-token", msg)
        self.assertNotIn("Fully automatic", msg)
        self.assertNotIn("agents_restart", msg)

    def test_project_file_wins_over_user_file_and_keys_are_not_model_thresholds(self):
        os.makedirs(os.path.join(self.tmp, ".context-watch"))
        with open(os.path.join(self.tmp, ".context-watch", "thresholds.json"), "w") as f:
            json.dump({"auto": True, "at": 60000, "auto_max": 3}, f)
        self.project_config({"auto": False})
        msg = self.pressure(65000)
        self.assertIn("60,000-token handoff threshold", msg)
        self.assertIn("[config:at]", msg)
        self.assertNotIn("Fully automatic", msg)
        # "at"/"auto" are substrings of model ids; they must never match as thresholds.
        with open(self.transcript, "w") as f:
            f.write(json.dumps({"message": {"model": "auto-at-model", "usage": {"input_tokens": 1000}}}) + "\n")
        self.project_config({"auto_max": 5, "auto": True})
        self.assertEqual(self.run_hook("PostToolUse"), "")

    def test_auto_max_from_config(self):
        sys.path.insert(0, HOOKS)
        try:
            import context_watch
        finally:
            sys.path.remove(HOOKS)
        self.project_config({"auto_max": 4})
        old = {k: os.environ.pop(k, None) for k in ("HANDOFF_AUTO_MAX", "HOME")}
        os.environ["HOME"] = self.tmp
        try:
            self.assertEqual(context_watch.auto_max(self.project), 4)
            os.environ["HANDOFF_AUTO_MAX"] = "7"
            self.assertEqual(context_watch.auto_max(self.project), 7)
            os.environ["HANDOFF_AUTO_MAX"] = ""
            self.assertEqual(context_watch.auto_max(self.project), 4)
            os.remove(os.path.join(self.project, ".context-watch.json"))
            self.assertEqual(context_watch.auto_max(self.project), 10)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


class AdvisoryWindow(Case):
    """23079574: warning_window_tokens early warning before the handoff notice."""

    def at(self, tokens, extra=None):
        """One PostToolUse at tokens on a fixed session (latches persist across calls)."""
        with open(self.transcript, "w") as f:
            for n in (1000, tokens):
                f.write(json.dumps({"message": {"model": "claude-test", "usage": {"input_tokens": n}}}) + "\n")
        evt = dict(hook_event_name="PostToolUse", session_id="adv-fixed", cwd=self.project,
                   transcript_path=self.transcript)
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=self.env(extra),
                           cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.strip()
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def project_config(self, data):
        with open(os.path.join(self.project, ".context-watch.json"), "w") as f:
            json.dump(data, f)

    def test_advisory_then_notice_and_duplicate_silent(self):
        self.assertEqual(self.at(99000), "")
        first = self.at(100000)
        self.assertIn("Early warning", first)
        self.assertIn("advisory point 100,000", first)
        self.assertNotIn("Publish with", first)
        self.assertEqual(self.at(110000), "", "duplicate callback")
        self.assertIn("handoff threshold", self.at(130000))

    def test_single_jump_gives_the_notice_not_the_advisory(self):
        out = self.at(140000)
        self.assertIn("handoff threshold", out)
        self.assertNotIn("Early warning", out)

    def test_compaction_rearms(self):
        self.assertIn("Early warning", self.at(105000))
        self.assertEqual(self.at(40000), "")
        self.assertIn("Early warning", self.at(105000))

    def test_project_lead_moves_the_point(self):
        self.project_config({"warning_window_tokens": 20000})
        self.assertEqual(self.at(105000), "")
        self.assertIn("advisory point 110,000", self.at(110000))

    def test_env_zero_disables(self):
        self.assertEqual(self.at(120000, {"CONTEXT_WATCH_WARNING_WINDOW": "0"}), "")

    def test_invalid_value_refused_with_a_diag(self):
        for bad in (-1, "x", True):
            with self.subTest(bad=bad):
                for name in os.listdir(self.tmp):
                    if name.startswith("context-watch") or "adv-fixed" in name:
                        path = os.path.join(self.tmp, name)
                        if os.path.isfile(path):
                            os.remove(path)
                self.project_config({"warning_window_tokens": bad})
                out = self.at(110000)
                self.assertNotIn("Early warning", out)
                self.assertIn("warning_window_tokens", out)

    def test_key_is_not_a_model_threshold(self):
        # A model key matching would turn 20000 into a threshold; the default 130k must hold.
        self.project_config({"warning_window_tokens": 20000})
        out = self.at(115000)
        self.assertIn("before the 130,000-token handoff threshold [builtin-default]", out)
        self.assertNotIn("Publish with", out)


class TerminalPointer(Case):
    def test_session_start_writes_this_terminals_pointer(self):
        self.run_hook("SessionStart", {"AGENTSROOM_AGENT_ID": "agent-x"}, source="startup")
        sys.path.insert(0, HOOKS)
        try:
            import handoff_ledger
        finally:
            sys.path.remove(HOOKS)
        root = os.path.realpath(handoff_ledger.project_root(self.project))
        digest = hashlib.sha1((root + "\0agent-x").encode("utf-8", "replace")).hexdigest()[:20]
        with open(os.path.join(self.tmp, "context-watch-terminal-%s.json" % digest)) as f:
            pointer = json.load(f)
        self.assertEqual(pointer["session_id"], "cfg-1")
        self.assertIsInstance(pointer["ts"], float)

    def test_no_terminal_identity_writes_no_pointer(self):
        self.run_hook("SessionStart", source="startup")
        self.assertFalse([n for n in os.listdir(self.tmp) if n.startswith("context-watch-terminal-")])


if __name__ == "__main__":
    unittest.main()
