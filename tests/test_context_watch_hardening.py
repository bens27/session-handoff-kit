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
from test_context_watch_regressions import CLEAN, HookCase, codex_count, claude_call, HOOK  # noqa: E402


def load_hook(tmp):
    hooks = os.path.dirname(os.path.abspath(HOOK))
    sys.path.insert(0, hooks)
    spec = importlib.util.spec_from_file_location("cw_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tempfile.tempdir = tmp  # gettempdir() -> the isolated dir
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
        self.addCleanup(setattr, tempfile, "tempdir", None)

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
        import gc, warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(self.cw, "handoff_written_since", lambda *a, **k: True), \
                    mock.patch.object(self.cw.subprocess, "Popen"), \
                    self.assertRaises(SystemExit):
                self.cw.handle_stop({"session_id": "sym-count", "cwd": cwd})
            gc.collect()
        self.assertVictimIntact()
        # 310304b8 item 5: reading the counter leaves no file handle open
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_stop_types_clear_and_resume_into_the_herdr_pane(self):
        open(self.cw.latch_path("herdr-stop"), "w").close()
        env = {"HANDOFF_AUTO": "1", "HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p3", "HERDR_BIN_PATH": "/opt/herdr",
               "TMUX_PANE": "%inherited", "AGENTSROOM_AGENT_ID": "", "HANDOFF_AUTO_RUNNER": "",
               "HANDOFF_TERMINAL_ID": ""}
        moved = {"pane_id": "w7:p1", "terminal_id": "term_abc"}  # the pane moved workspaces since launch
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.cw.handoff_identity, "herdr_pane_info", lambda pane: dict(moved)), \
                mock.patch.object(self.cw, "handoff_written_since", lambda *a, **k: True), \
                mock.patch.object(self.cw.subprocess, "Popen") as popen, \
                self.assertRaises(SystemExit):
            self.cw.handle_stop({"session_id": "herdr-stop", "cwd": self.tmp})
        (argv,), kwargs = popen.call_args
        self.assertEqual((kwargs["env"]["P"], kwargs["env"]["H"]), ("w7:p1", "/opt/herdr"))
        self.assertIn('"$H" pane send-keys "$P" Escape; "$H" pane send-text "$P" "$1"', argv[2])
        self.assertIn("send /clear", argv[2])
        self.assertIn("send resume", argv[2])
        self.assertNotIn("tmux", argv[2])

    def test_auto_count_path_is_collision_free_sha1(self):
        a = os.path.join(self.tmp, "a_b")
        b = os.path.join(self.tmp, "a", "b")
        os.makedirs(a)
        os.makedirs(b)
        self.assertNotEqual(self.cw.auto_count_path(a), self.cw.auto_count_path(b))
        digest = hashlib.sha1(os.path.realpath(self.cw._ledger().project_root(a)).encode()).hexdigest()[:20]
        self.assertTrue(self.cw.auto_count_path(a).endswith("context-watch-auto-%s.count" % digest))


class StopTyperEscapesSubagentView(unittest.TestCase):
    """pullboard #2: the detached Stop typer presses Escape before each typed
    command (a focused subagent view in Claude's TUI would otherwise swallow
    /clear or resume), waits for the successor's resume event in the context-watch
    ledger, retries Escape+resume once, and leaves a visible notice if it never comes."""

    FAST = "0,0,0.05,4"  # clear delay, resume delay, poll interval, polls (test-only knob)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.log = os.path.join(self.tmp, "resumes.jsonl")
        self.keys = os.path.join(self.tmp, "keys.txt")
        patcher = mock.patch.dict(os.environ, {
            "HOME": self.tmp, "TMPDIR": self.tmp, "CONTEXT_WATCH_LOG": "0", "CONTEXT_WATCH_JEV": "0",
            "CONTEXT_WATCH_RESUME_LOG": self.log, "CONTEXT_WATCH_TYPER_TIMING": self.FAST,
            "HANDOFF_AUTO": "1", "AGENTSROOM_AGENT_ID": "", "HANDOFF_AUTO_RUNNER": "",
            "HANDOFF_TERMINAL_ID": "", "HERDR_ENV": "", "HERDR_PANE_ID": "", "TMUX_PANE": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cw = load_hook(self.tmp)
        self.addCleanup(setattr, tempfile, "tempdir", None)

    def fake(self, name, resume_succeeds, terminal=None):
        """A recording herdr/tmux stand-in; optionally logs a resume event when 'resume' is typed.
        `terminal` adds the terminal_id the resume telemetry records; None is an old-format event."""
        path = os.path.join(self.tmp, name)
        record = ""
        if resume_succeeds:
            event = {"operation": "resume", "project_id": self.cw.resume_project_id(self.tmp)}
            if terminal is not None:
                event["terminal_id"] = terminal
            record = ("case \"$*\" in *resume*) printf '%%s\\n' '%s' >> \"%s\";; esac\n"
                      % (json.dumps(event), self.log))
        with open(path, "w") as f:
            f.write("#!/bin/sh\nprintf '%%s\\n' \"$*\" >> \"%s\"\n%s" % (self.keys, record))
        os.chmod(path, 0o755)
        return path

    def typer(self, env):
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.cw.handoff_identity, "herdr_pane_info", lambda pane: {}), \
                mock.patch.object(self.cw, "handoff_written_since", lambda *a, **k: True), \
                mock.patch.object(self.cw.subprocess, "Popen") as popen, \
                self.assertRaises(SystemExit):
            open(self.cw.latch_path("typer"), "w").close()
            self.cw.handle_stop({"session_id": "typer", "cwd": self.tmp})
        (argv,), kwargs = popen.call_args
        return argv, kwargs["env"]

    def run_typer(self, argv, env, path_dir=None):
        if path_dir:
            env = dict(env, PATH=path_dir + os.pathsep + env.get("PATH", ""))
        subprocess.run(argv, env=env, timeout=30, check=True)
        with open(self.keys) as f:
            return f.read().splitlines()

    def test_herdr_escape_before_each_command_and_no_retry_when_resume_recorded(self):
        herdr = self.fake("herdr", resume_succeeds=True)
        argv, env = self.typer({"HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p3", "HERDR_BIN_PATH": herdr})
        keys = self.run_typer(argv, env)
        self.assertEqual(keys, [
            "pane send-keys w2:p3 Escape", "pane send-text w2:p3 /clear", "pane send-keys w2:p3 Enter",
            "pane send-keys w2:p3 Escape", "pane send-text w2:p3 resume", "pane send-keys w2:p3 Enter"])
        self.assertFalse(os.path.exists(self.cw.resume_missed_path(self.tmp)))

    def test_herdr_retries_escape_resume_once_then_leaves_a_notice(self):
        herdr = self.fake("herdr", resume_succeeds=False)
        argv, env = self.typer({"HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p3", "HERDR_BIN_PATH": herdr})
        keys = self.run_typer(argv, env)
        self.assertEqual(keys, [
            "pane send-keys w2:p3 Escape", "pane send-text w2:p3 /clear", "pane send-keys w2:p3 Enter",
            "pane send-keys w2:p3 Escape", "pane send-text w2:p3 resume", "pane send-keys w2:p3 Enter",
            "pane send-keys w2:p3 Escape", "pane send-text w2:p3 resume", "pane send-keys w2:p3 Enter"])
        notice = self.cw.resume_missed_path(self.tmp)
        self.assertTrue(os.path.exists(notice))
        # The next prompt in this project shows the notice once, from any session.
        evt = {"hook_event_name": "UserPromptSubmit", "session_id": "successor", "cwd": self.tmp,
               "transcript_path": os.path.join(self.tmp, "none.jsonl"), "prompt": "hello"}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=dict(os.environ, PYTHONPATH=""),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("resume", p.stdout)
        self.assertIn("Escape", p.stdout)
        self.assertFalse(os.path.exists(notice))

    def test_resume_by_another_terminal_in_the_project_is_not_success(self):
        """pullboard #3: a resume from another terminal in this project inside the
        wait window must not stop the retry or suppress the notice."""
        herdr = self.fake("herdr", resume_succeeds=True, terminal="term-B")
        argv, env = self.typer({"HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p3", "HERDR_BIN_PATH": herdr,
                                "HANDOFF_TERMINAL_ID": "term-A"})
        keys = self.run_typer(argv, env)
        self.assertEqual(keys.count("pane send-text w2:p3 resume"), 2)
        self.assertTrue(os.path.exists(self.cw.resume_missed_path(self.tmp)))

    def test_resume_by_this_terminal_is_success(self):
        herdr = self.fake("herdr", resume_succeeds=True, terminal="term-A")
        argv, env = self.typer({"HERDR_ENV": "1", "HERDR_PANE_ID": "w2:p3", "HERDR_BIN_PATH": herdr,
                                "HANDOFF_TERMINAL_ID": "term-A"})
        keys = self.run_typer(argv, env)
        self.assertEqual(keys.count("pane send-text w2:p3 resume"), 1)
        self.assertFalse(os.path.exists(self.cw.resume_missed_path(self.tmp)))

    def test_resume_telemetry_records_the_terminal_id(self):
        protocol = importlib.import_module("handoff_protocol")
        with mock.patch.dict(os.environ, {"HANDOFF_TERMINAL_ID": "term-A", "CONTEXT_WATCH_LOG": ""}):
            protocol.telemetry("resume", self.tmp, "s1", {"outcome": "resumed"})
        with open(self.log) as f:
            record = json.loads(f.read().splitlines()[-1])
        self.assertEqual(record["terminal_id"], "term-A")
        self.assertEqual(record["project_id"], self.cw.resume_project_id(self.tmp))

    def test_tmux_escape_before_each_command(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.fake(os.path.join("bin", "tmux"), resume_succeeds=True)
        argv, env = self.typer({"TMUX_PANE": "%5"})
        keys = self.run_typer(argv, env, path_dir=bindir)
        self.assertEqual(keys, [
            "send-keys -t %5 Escape", "send-keys -t %5 -l /clear", "send-keys -t %5 Enter",
            "send-keys -t %5 Escape", "send-keys -t %5 -l resume", "send-keys -t %5 Enter"])


class RootCheckpointCase(HookCase):
    """A git project under the sandbox where the hook fires and checkpoints save."""

    QUIET = {"HERDR_ENV": "", "HERDR_PANE_ID": "", "HANDOFF_TERMINAL_ID": "", "CONTEXT_WATCH_THINKING": "0"}

    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(self.tmp)
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.root, ".handoffs"))
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", self.root]
        env = dict(os.environ, HOME=self.tmp, GIT_CONFIG_NOSYSTEM="1")
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(git + args, env=env, check=True, capture_output=True, timeout=30)

    def env(self):
        # A Codex shell's CODEX_THREAD_ID (or HANDOFF_SESSION_ID) makes the save record
        # under that host session, not this test's; HANDOFF_STATE_DIR would leave the sandbox.
        env = {k: v for k, v in os.environ.items()
               if k not in CLEAN and k not in ("CODEX_THREAD_ID", "HANDOFF_SESSION_ID", "HANDOFF_STATE_DIR")}
        env.update(self.QUIET, HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG="0")
        return env

    def hook(self, event, cwd):
        env = self.env()
        env.update(CONTEXT_WATCH_JEV="0", CONTEXT_WATCH_AGENT="claude")
        evt = {"hook_event_name": event, "session_id": self.sid, "cwd": cwd,
               "transcript_path": self.transcript}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env, cwd=cwd,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def save(self):
        env = self.env()
        doc = {"topic": "drift", "description": "fixture",
               "body": "## Objective\nContinue.\n## Current state\nReady.\n## Next steps\nTest.\n"}
        ledger = os.path.join(os.path.dirname(HOOK), "handoff_ledger.py")
        p = subprocess.run([sys.executable, ledger, "save", self.root, "--session", self.sid,
                            "--request-id", "fixture"], input=json.dumps(doc), env=env, cwd=self.root,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["outcome"], "saved")



class StopAfterCwdDriftIntoWorktree(RootCheckpointCase):
    """pullboard #6: the notice fires and the checkpoint is saved at the project
    root, then a Bash cd moves the session cwd into a linked worktree of it. The
    Stop hook must still find that checkpoint instead of blocking."""

    def setUp(self):
        super().setUp()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", self.root]
        env = dict(os.environ, HOME=self.tmp, GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(git + ["worktree", "add", "-q", os.path.join(".claude", "worktrees", "x")],
                       env=env, check=True, capture_output=True, timeout=30)
        self.worktree = os.path.join(self.root, ".claude", "worktrees", "x")

    def test_stop_from_worktree_finds_the_root_checkpoint(self):
        self.write([claude_call(20000), claude_call(140000)])
        self.assertIn("context-pressure", self.hook("PostToolUse", self.root))
        # Guard can fail: before the save, Stop from the drifted cwd blocks.
        self.assertIn("No checkpoint has been published", self.hook("Stop", self.worktree))
        self.save()
        self.assertNotIn("No checkpoint has been published", self.hook("Stop", self.worktree))
        self.assertNotIn("has published no checkpoint", self.hook("Stop", self.worktree))

    def test_stop_from_a_subdirectory_finds_the_root_checkpoint(self):
        sub = os.path.join(self.root, "src", "deep")
        os.makedirs(sub)
        self.write([claude_call(20000), claude_call(140000)])
        self.assertIn("context-pressure", self.hook("PostToolUse", self.root))
        self.save()
        self.assertNotIn("has published no checkpoint", self.hook("Stop", sub))


class AutoHandoffSummaryReshownAfterResume(RootCheckpointCase):
    """pullboard #11: auto-handoff shows its summary, then the detached typer clears
    and resumes seconds later, so the person cannot read it. The Stop hook saves
    that summary with the checkpoint; the first hook output after resume carries
    it verbatim, once."""

    SUMMARY = "Handoff saved.\nDone: parser fixed (abc1234).\nNext: wire the CLI flag."

    def stop(self):
        env = self.env()
        env.update(CONTEXT_WATCH_JEV="0", CONTEXT_WATCH_AGENT="claude", HANDOFF_AUTO="1",
                   HERDR_ENV="1", HERDR_PANE_ID="w1:p1", HERDR_BIN_PATH="/usr/bin/true",
                   AGENTSROOM_AGENT_ID="", HANDOFF_AUTO_RUNNER="",
                   CONTEXT_WATCH_TYPER_TIMING="0,0,0,0")
        evt = {"hook_event_name": "Stop", "session_id": self.sid, "cwd": self.root,
               "transcript_path": self.transcript, "last_assistant_message": self.SUMMARY}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env, cwd=self.root,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)

    def resume(self, sid):
        env = self.env()
        env.update(CONTEXT_WATCH_JEV="0", CONTEXT_WATCH_AGENT="claude")
        evt = {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": self.root,
               "transcript_path": os.path.join(self.tmp, sid + ".jsonl"), "prompt": "resume"}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env, cwd=self.root,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_summary_is_saved_with_the_checkpoint_and_reshown_once(self):
        self.write([claude_call(20000), claude_call(140000)])
        self.assertIn("context-pressure", self.hook("PostToolUse", self.root))
        self.save()
        self.stop()
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "TMPDIR": self.tmp}):
            cw = load_hook(self.tmp)
            self.addCleanup(setattr, tempfile, "tempdir", None)
            with open(cw.auto_summary_path(self.root), encoding="utf-8") as f:
                saved = json.load(f)
            checkpoint = [r for r in cw._ledger()._records(self.root)
                          if r["fm"].get("session_id") == self.sid][-1]
        self.assertEqual(saved["summary"], self.SUMMARY)
        self.assertEqual(saved["checkpoint_id"], checkpoint["fm"]["checkpoint_id"])
        self.assertEqual(os.path.realpath(saved["checkpoint"]), os.path.realpath(checkpoint["path"]))
        first = self.resume(self.sid + "-successor")
        self.assertTrue(first.strip(), "the first hook output after resume is empty")
        shown = json.loads(first)
        self.assertIn(self.SUMMARY, shown.get("systemMessage", ""))
        self.assertIn(self.SUMMARY, shown["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn(self.SUMMARY, self.resume(self.sid + "-again"))


class AutoHandoffSummaryInHerdrPane(RootCheckpointCase):
    """pullboard #12 (G4): inside Herdr the Stop typer opens the summary in an unfocused
    plugin pane beside the agent pane before it types /clear. The pane reads its own
    copy, so the replay after resume (#11) does not take it away. Outside Herdr
    nothing changes."""

    SUMMARY = "Handoff saved.\nDone: parser fixed (abc1234).\nNext: wire the CLI flag."
    PLUGIN = os.path.join(os.path.dirname(HERE), "herdr-plugin")

    def setUp(self):
        super().setUp()
        self.calls = os.path.join(self.tmp, "calls.txt")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name in ("herdr", "tmux"):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\nprintf '%%s\\n' \"%s $*\" >> \"%s\"\n" % (name, self.calls))
            os.chmod(path, 0o755)

    def host(self, **extra):
        env = self.env()
        env.update(CONTEXT_WATCH_JEV="0", CONTEXT_WATCH_AGENT="claude", HANDOFF_AUTO="1",
                   AGENTSROOM_AGENT_ID="", HANDOFF_AUTO_RUNNER="", TMUX_PANE="",
                   CONTEXT_WATCH_TYPER_TIMING="0,0,0,0",
                   PATH=self.bin + os.pathsep + env.get("PATH", ""))
        env.update(extra)
        return env

    def stop(self, env):
        """Run the Stop hook, wait for its detached typer, return what it called."""
        self.write([claude_call(20000), claude_call(140000)])
        self.assertIn("context-pressure", self.hook("PostToolUse", self.root))
        self.save()
        evt = {"hook_event_name": "Stop", "session_id": self.sid, "cwd": self.root,
               "transcript_path": self.transcript, "last_assistant_message": self.SUMMARY}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env, cwd=self.root,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        import time
        deadline = time.time() + 15
        while time.time() < deadline:
            calls = open(self.calls).read().splitlines() if os.path.exists(self.calls) else []
            if any("resume" in c for c in calls) and calls[-1].endswith("Enter"):
                return calls
            time.sleep(0.05)
        self.fail("the typer never typed resume: %r" % calls)

    def resume(self):
        env = self.env()
        env.update(CONTEXT_WATCH_JEV="0", CONTEXT_WATCH_AGENT="claude")
        evt = {"hook_event_name": "UserPromptSubmit", "session_id": self.sid + "-successor",
               "cwd": self.root, "transcript_path": os.path.join(self.tmp, "successor.jsonl"),
               "prompt": "resume"}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env, cwd=self.root,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def render(self, path):
        p = subprocess.run([sys.executable, os.path.join(self.PLUGIN, "summary.py")],
                           env=dict(self.env(), HANDOFF_SUMMARY_FILE=path), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_herdr_opens_the_summary_pane_before_the_clear_and_it_survives_the_replay(self):
        calls = self.stop(self.host(HERDR_ENV="1", HERDR_PANE_ID="w1:p1",
                                    HERDR_BIN_PATH=os.path.join(self.bin, "herdr")))
        opened = [c for c in calls if c.startswith("herdr plugin pane open ")]
        self.assertEqual(len(opened), 1, calls)
        for part in ("--plugin session-handoff.thresholds", "--entrypoint summary", "--placement split",
                     "--direction right", "--target-pane w1:p1", "--no-focus"):
            self.assertIn(part, opened[0])
        self.assertNotIn("--focus ", opened[0] + " ")
        self.assertLess(calls.index(opened[0]), calls.index("herdr pane send-text w1:p1 /clear"))
        copy = opened[0].split("--env HANDOFF_SUMMARY_FILE=", 1)[1].split(" --")[0]
        self.assertTrue(os.path.realpath(copy).startswith(self.tmp + os.sep), copy)
        self.assertIn(self.SUMMARY, self.render(copy))
        replay = json.loads(self.resume())  # the #11 replay consumes its own file
        self.assertIn(self.SUMMARY, replay.get("systemMessage", ""))
        self.assertIn(self.SUMMARY, self.render(copy))

    def test_manifest_declares_the_summary_pane(self):
        import tomllib
        with open(os.path.join(self.PLUGIN, "herdr-plugin.toml"), "rb") as f:
            panes = {p["id"]: p for p in tomllib.load(f)["panes"]}
        self.assertEqual(panes["summary"]["title"], "Handoff summary")
        self.assertEqual(panes["summary"]["command"], ["python3", "summary.py"])

    def test_outside_herdr_no_pane_is_opened(self):
        calls = self.stop(self.host(TMUX_PANE="%7"))
        self.assertEqual([c for c in calls if "plugin" in c or c.startswith("herdr")], [], calls)
        self.assertIn("tmux send-keys -t %7 -l /clear", calls)

    def test_a_live_summary_pane_is_reused_instead_of_stacking_another(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "TMPDIR": self.tmp}):
            cw = load_hook(self.tmp)
            self.addCleanup(setattr, tempfile, "tempdir", None)
            script = cw.typer_script("herdr")
        copy = os.path.join(self.tmp, "copy.json")
        env = dict(self.env(), P="w1:p1", H=os.path.join(self.bin, "herdr"), S=copy, R="", D="", N="",
                   K="", M="", T1="0", T2="0", T3="0", T4="0")

        def opens():
            if os.path.exists(self.calls):
                os.remove(self.calls)
            subprocess.run(["sh", "-c", script], env=env, check=True, timeout=30)
            return [c for c in open(self.calls).read().splitlines() if " plugin pane open " in c]

        with open(copy + ".pid", "w") as f:
            f.write("%d\n" % os.getpid())  # a pane process that is alive
        self.assertEqual(opens(), [])
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        with open(copy + ".pid", "w") as f:
            f.write("%d\n" % gone.pid)  # the pane was closed
        self.assertEqual(len(opens()), 1)


class ResumeMissedNoticeUnremovable(unittest.TestCase):
    """pullboard #4: a notice that was read but cannot be removed is still shown,
    and the same notice is not shown again on the next prompt."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp, "TMPDIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cw = load_hook(self.tmp)
        self.addCleanup(setattr, tempfile, "tempdir", None)
        self.path = self.cw.resume_missed_path(self.tmp)
        with open(self.path, "w") as f:
            f.write("missed notice\n")

    def refuse(self, *a, **k):
        raise PermissionError("refused")

    def test_shown_once_when_remove_fails(self):
        with mock.patch.object(self.cw.os, "remove", self.refuse):
            self.assertEqual(self.cw.take_resume_missed_notice(self.tmp), "missed notice")
            self.assertEqual(self.cw.take_resume_missed_notice(self.tmp), "")

    def test_shown_once_when_remove_and_truncate_fail(self):
        with mock.patch.object(self.cw.os, "remove", self.refuse), \
                mock.patch.object(self.cw, "empty_file", self.refuse):
            self.assertEqual(self.cw.take_resume_missed_notice(self.tmp), "missed notice")
            self.assertEqual(self.cw.take_resume_missed_notice(self.tmp), "")

    def test_a_rewritten_notice_shows_again(self):
        with mock.patch.object(self.cw.os, "remove", self.refuse), \
                mock.patch.object(self.cw, "empty_file", self.refuse):
            self.assertEqual(self.cw.take_resume_missed_notice(self.tmp), "missed notice")
            st = os.stat(self.path)
            with open(self.path, "w") as f:  # the typer misses again later
                f.write("missed notice\n")
            os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
            self.assertEqual(self.cw.take_resume_missed_notice(self.tmp), "missed notice")
            self.assertEqual(self.cw.take_resume_missed_notice(self.tmp), "")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_shown_once_when_every_save_of_shown_fails(self):
        # Real modes, no mocks: the notice is read-only and its state dir (also HOME
        # and TMPDIR) is too, so remove, truncate and the .shown sidecar all fail.
        # Each call is its own process, and a third process reads the notice in
        # between, as any ordinary reader may (which resets the file's atime).
        fallback = tempfile.TemporaryDirectory()
        self.addCleanup(fallback.cleanup)
        state = os.path.dirname(self.path)
        os.chmod(self.path, 0o400)
        os.chmod(state, 0o500)
        self.addCleanup(os.chmod, self.path, 0o600)
        self.addCleanup(os.chmod, state, 0o700)
        # gettempdir() skips an unwritable TMPDIR, so name the state dir outright.
        env = dict(os.environ, HOME=self.tmp, TMPDIR=self.tmp, HANDOFF_STATE_DIR=state,
                   HANDOFF_SHOWN_FALLBACK_DIR=fallback.name)
        hooks = os.path.dirname(os.path.abspath(HOOK))
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import context_watch as cw; "
                "sys.stdout.write(cw.take_resume_missed_notice(sys.argv[2]))")

        def take():
            p = subprocess.run([sys.executable, "-c", code, hooks, self.tmp], env=env, cwd=self.tmp,
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout

        def read():
            p = subprocess.run([sys.executable, "-c", "import sys; open(sys.argv[1]).read()", self.path],
                               env=env, cwd=self.tmp, capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)

        first = take()
        read()
        second = take()
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".shown"))
        self.assertEqual(first, "missed notice")
        self.assertEqual(second, "")

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
    def test_startup_is_quiet_even_with_autoresume_and_untrusted_checkpoint(self):
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
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "")


if __name__ == "__main__":
    unittest.main()


class LoneHookFailsVisible(unittest.TestCase):
    """A context_watch.py copied without its sibling modules must still say the
    handoff service is unavailable instead of exiting silently."""

    def run_lone(self, event):
        with tempfile.TemporaryDirectory() as tmp:
            hook = os.path.join(tmp, "context_watch.py")
            with open(HOOK) as src, open(hook, "w") as dst:
                dst.write(src.read())
            env = dict(os.environ, HOME=tmp, TMPDIR=tmp, PYTHONPATH="",
                       CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0")
            evt = dict(event, cwd=tmp, session_id="lone-hook")
            return subprocess.run([sys.executable, "-I", hook], input=json.dumps(evt),
                                  capture_output=True, text=True, cwd=tmp, env=env, timeout=30)

    def test_session_start_compact_reports_service_unavailable(self):
        p = self.run_lone({"hook_event_name": "SessionStart", "source": "compact"})
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("Handoff service unavailable (ModuleNotFoundError)", out["additionalContext"])
        self.assertIn("do not infer that no handoff exists", out["additionalContext"])

    def test_session_start_startup_stays_quiet(self):
        # Matches 031292b and verify-watcher: plain startup announces nothing.
        p = self.run_lone({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""), p.stderr)

    def test_user_prompt_submit_reports_context_watch_failed(self):
        p = self.run_lone({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn("Context-watch failed (ModuleNotFoundError)", out["additionalContext"])

    def test_stop_reports_context_watch_failed(self):
        p = self.run_lone({"hook_event_name": "Stop"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Context-watch failed (ModuleNotFoundError)", json.loads(p.stdout)["systemMessage"])


class LoneHookNoticeOncePerSession(unittest.TestCase):
    """Missing install: one notice per session, not one per tool call or turn."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.hook = os.path.join(self.tmp, "context_watch.py")
        with open(HOOK) as src, open(self.hook, "w") as dst:
            dst.write(src.read())

    def run_lone(self, event, tmpdir=None):
        env = dict(os.environ, HOME=self.tmp, TMPDIR=tmpdir or self.tmp, PYTHONPATH="",
                   CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0")
        return subprocess.run([sys.executable, "-I", self.hook], input=json.dumps(event),
                              capture_output=True, text=True, cwd=self.tmp, env=env, timeout=30)

    def test_second_event_of_a_session_is_quiet(self):
        for name in ("UserPromptSubmit", "PostToolUse", "Stop"):
            evt = {"hook_event_name": name, "session_id": "once-" + name, "cwd": self.tmp,
                   "transcript_path": os.path.join(self.tmp, "t.jsonl"), "prompt": "x"}
            first, second = self.run_lone(evt), self.run_lone(evt)
            self.assertEqual((first.returncode, second.returncode), (0, 0), name)
            self.assertIn("Context-watch failed", first.stdout, name)
            self.assertEqual(second.stdout.strip(), "", name)

    def test_one_claim_covers_every_event_of_the_session(self):
        base = {"session_id": "shared", "cwd": self.tmp, "prompt": "x"}
        self.assertIn("Context-watch failed", self.run_lone(dict(base, hook_event_name="UserPromptSubmit")).stdout)
        self.assertEqual(self.run_lone(dict(base, hook_event_name="Stop")).stdout.strip(), "")

    def test_other_session_still_hears_it(self):
        self.run_lone({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "x"})
        p = self.run_lone({"hook_event_name": "UserPromptSubmit", "session_id": "s2", "prompt": "x"})
        self.assertIn("Context-watch failed", p.stdout)

    def test_planted_symlink_is_not_followed(self):
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w") as f:
            f.write("KEEP")
        key = hashlib.sha1(b"planted").hexdigest()[:20]
        os.symlink(victim, os.path.join(self.tmp, "context-watch-lone-%s.error" % key))
        p = self.run_lone({"hook_event_name": "UserPromptSubmit", "session_id": "planted", "prompt": "x"})
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(victim) as f:
            self.assertEqual(f.read(), "KEEP")

    def test_unclaimable_marker_falls_back_to_every_event(self):
        cw = load_hook(self.tmp)
        self.addCleanup(setattr, tempfile, "tempdir", None)
        with mock.patch.object(cw.os, "open", side_effect=PermissionError):
            self.assertTrue(cw._claim_lone_notice({"session_id": "x"}))
            self.assertTrue(cw._claim_lone_notice({"session_id": "x"}))
        self.assertTrue(cw._claim_lone_notice({"session_id": "y"}))
        self.assertFalse(cw._claim_lone_notice({"session_id": "y"}))


class PaneOverrideAutoSettings(unittest.TestCase):
    """The Herdr pane override file carries auto and auto_max with the same
    precedence as its "at": below the environment, above the config files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        env = {k: v for k, v in os.environ.items()
               if k not in ("HANDOFF_AUTO", "HANDOFF_AUTO_MAX", "AGENTSROOM_AGENT_ID", "HERDR_ENV")}
        env.update(HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0",
                   HANDOFF_TERMINAL_ID="term-override")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cw = load_hook(self.tmp)
        self.addCleanup(setattr, tempfile, "tempdir", None)
        self.cw._EVT = {"cwd": self.tmp}
        os.makedirs(os.path.join(self.tmp, ".context-watch"))
        self.user({"auto": False, "auto_max": 8})

    def user(self, data):
        with open(os.path.join(self.tmp, ".context-watch", "thresholds.json"), "w") as f:
            json.dump(data, f)

    def pane(self, data):
        path = self.cw.handoff_identity.terminal_settings_path("term-override")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

    def project(self, data):
        with open(os.path.join(self.tmp, ".context-watch.json"), "w") as f:
            json.dump(data, f)

    def test_pane_auto_and_auto_max_sit_between_env_and_files(self):
        self.assertFalse(self.cw.auto_mode_on())
        self.assertEqual(self.cw.auto_max(self.tmp), 8)
        self.pane({"at": 90000, "auto": True, "auto_max": 2})
        self.assertTrue(self.cw.auto_mode_on())
        self.assertEqual(self.cw.auto_max(self.tmp), 2)
        self.project({"auto": False, "auto_max": 5})  # the pane is the terminal's own say, like HANDOFF_AUTO=1
        self.assertTrue(self.cw.auto_mode_on())
        self.assertEqual(self.cw.auto_max(self.tmp), 2)
        with mock.patch.dict(os.environ, {"HANDOFF_AUTO": "0", "HANDOFF_AUTO_MAX": "6"}):
            self.assertFalse(self.cw.auto_mode_on())
            self.assertEqual(self.cw.auto_max(self.tmp), 6)

    def test_pane_auto_off_wins_over_the_user_file(self):
        self.user({"auto": True})
        self.assertTrue(self.cw.auto_mode_on())
        self.pane({"auto": False})
        self.assertFalse(self.cw.auto_mode_on())

    def test_wrong_types_in_the_pane_file_are_ignored(self):
        self.pane({"auto": "yes", "auto_max": "3"})
        self.assertFalse(self.cw.auto_mode_on())
        self.assertEqual(self.cw.auto_max(self.tmp), 8)
