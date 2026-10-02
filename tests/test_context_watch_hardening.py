#!/usr/bin/env python3
"""Regression tests for context_watch.py temp-file hardening (symlinks planted in a
shared TMPDIR must not redirect writes) and Stop-nudge latch handling.

Run: python3 -m pytest tests/test_context_watch_hardening.py"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_context_watch_regressions import HookCase, codex_count, claude_call, HOOK  # noqa: E402


def load_hook(tmp):
    hooks = os.path.dirname(os.path.abspath(HOOK))
    sys.path.insert(0, hooks)
    spec = importlib.util.spec_from_file_location("cw_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.tempfile.tempdir = tmp  # gettempdir() -> the isolated dir
    return mod


class PlantedSymlinks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.victim = os.path.join(self.tmp, "victim.txt")
        with open(self.victim, "w") as f:
            f.write("KEEP")
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp, "TMPDIR": self.tmp,
                                               "CONTEXT_WATCH_LOG": "0", "CONTEXT_WATCH_JEV": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cw = load_hook(self.tmp)
        self.addCleanup(setattr, self.cw.tempfile, "tempdir", None)

    def plant(self, path):
        os.symlink(self.victim, path)

    def assertVictimIntact(self):
        with open(self.victim) as f:
            self.assertEqual(f.read(), "KEEP")

    def test_session_note_temp_is_not_followed(self):
        sid = "sym-note"
        note = self.cw._ledger().session_note_path(self.tmp, sid)
        os.makedirs(os.path.dirname(note), exist_ok=True)
        self.plant("%s.%d" % (note, os.getpid()))
        self.cw.write_session_note(self.tmp, {"session_id": sid}, False)
        self.assertVictimIntact()
        self.assertTrue(os.path.exists(note))

    def test_terminal_pointer_temp_is_not_followed(self):
        with mock.patch.dict(os.environ, {"HANDOFF_TERMINAL_ID": "term-1"}):
            path = self.cw.terminal_pointer_path(self.tmp, "term-1")
            self.plant("%s.%d" % (path, os.getpid()))
            self.cw.write_terminal_pointer(self.tmp, "sym-term")
        self.assertVictimIntact()
        self.assertTrue(os.path.exists(path))

    def test_auto_counter_is_not_followed(self):
        cwd = self.tmp
        self.plant(self.cw.auto_count_path(cwd))
        latch = self.cw.latch_path("sym-count")
        open(latch, "w").close()
        env = {"HANDOFF_AUTO": "1", "TMUX_PANE": "%1", "AGENTSROOM_AGENT_ID": "",
               "HANDOFF_AUTO_RUNNER": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.cw, "handoff_written_since", lambda *a, **k: True), \
                mock.patch.object(self.cw.subprocess, "Popen"), \
                self.assertRaises(SystemExit):
            self.cw.handle_stop({"session_id": "sym-count", "cwd": cwd})
        self.assertVictimIntact()

    def test_auto_count_path_is_collision_free_sha1(self):
        a = os.path.join(self.tmp, "a_b")
        b = os.path.join(self.tmp, "a", "b")
        os.makedirs(a)
        os.makedirs(b)
        self.assertNotEqual(self.cw.auto_count_path(a), self.cw.auto_count_path(b))
        digest = hashlib.sha1(os.path.realpath(self.cw._ledger().project_root(a)).encode()).hexdigest()[:20]
        self.assertTrue(self.cw.auto_count_path(a).endswith("context-watch-auto-%s.count" % digest))


class FloorLatchSymlink(HookCase):
    def test_floor_latch_is_not_followed(self):
        # A dangling symlink passes the exists() check, so the write would create its target.
        self.codex = True
        victim = os.path.join(self.tmp, "victim.txt")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.sid)
        os.symlink(victim, os.path.join(self.tmp, "context-watch-%s.fired.floor" % safe))
        self.write([codex_count(125000, 125000)])
        self.run_hook()
        self.write([codex_count(140000, 140000)], "a")
        self.run_hook()  # startup-floor path writes the .floor latch
        self.assertFalse(os.path.exists(victim))


class StopNudge(HookCase):
    def stop(self):
        return self.run_hook(event="Stop")

    def test_empty_second_latch_does_not_suppress_nudge(self):
        # A failed second-notice write can leave an empty .fired2; that is not a floor note.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.sid)
        base = os.path.join(self.tmp, "context-watch-%s.fired" % safe)
        with open(base, "w") as f:
            f.write("150000/130000/140000\n")
        open(base + "2", "w").close()
        self.assertIn('"decision": "block"', self.stop())


class StartupAutoresumeNote(unittest.TestCase):
    def test_note_says_untrusted_verify_asks_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "project")
            os.makedirs(os.path.join(root, ".handoffs"))
            with open(os.path.join(root, ".handoffs", "work.md"), "w") as f:
                f.write("---\ntopic: work\nstatus: open\n---\n## Objective\nFix it.\n"
                        "## Current state\nUnchanged.\n## Next steps\nRun checks.\n")
            env = dict(os.environ, HOME=tmp, TMPDIR=tmp, AUTORESUME="1", CONTEXT_WATCH_LOG="0",
                       CONTEXT_WATCH_JEV="0", CONTEXT_WATCH_ORIGIN="test")
            for k in ("HANDOFF_AUTO", "HANDOFF_AT", "TMUX_PANE", "AGENTSROOM_AGENT_ID"):
                env.pop(k, None)
            evt = {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1", "cwd": root}
            p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), cwd=root, env=env,
                               capture_output=True, text=True, timeout=15)
            note = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Proceed without asking", note)
            self.assertIn("An untrusted checkpoint with a verify command asks the user first.", note)


if __name__ == "__main__":
    unittest.main()
