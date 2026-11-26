"""pullboard #27: Codex seats saved at context pressure but never cleared, because the
automatic clear runs from the Stop hook and the kit registered no Stop hook for Codex.
Ben chose (decision #249) to have the kit's Codex install add one; he trusts it in Codex."""
import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_context_watch_regressions import HOOK, codex_count  # noqa: E402
from test_context_watch_hardening import RootCheckpointCase  # noqa: E402

INSTALL = os.path.join(os.path.dirname(os.path.dirname(HOOK)), "install.py")


class CodexStopHook(RootCheckpointCase):
    def setUp(self):
        super().setUp()
        self.calls = os.path.join(self.tmp, "calls.txt")
        self.herdr = os.path.join(self.tmp, "herdr")
        with open(self.herdr, "w") as f:
            f.write("#!/bin/sh\nprintf '%%s\\n' \"herdr $*\" >> \"%s\"\n" % self.calls)
        os.chmod(self.herdr, 0o755)

    def codex_env(self, **extra):
        env = self.env()
        env.update(CONTEXT_WATCH_JEV="0", CONTEXT_WATCH_AGENT="codex", HANDOFF_AUTO="1",
                   AGENTSROOM_AGENT_ID="", HANDOFF_AUTO_RUNNER="", TMUX_PANE="",
                   CONTEXT_WATCH_TYPER_TIMING="0,0,0,0")
        env.update(extra)
        return env

    def run_event(self, evt, env):
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env, cwd=self.root,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_codex_install_registers_the_stop_hook(self):
        codex_home = os.path.join(self.tmp, "codex")
        env = dict(self.env(), CODEX_HOME=codex_home)
        p = subprocess.run([sys.executable, INSTALL, "codex"], env=env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(codex_home, "hooks.json")) as f:
            groups = json.load(f)["hooks"].get("Stop", [])
        commands = [h["command"] for g in groups for h in g["hooks"]]
        self.assertTrue(any("context_watch.py" in c for c in commands), commands)

    def test_codex_stop_after_a_published_checkpoint_spawns_the_typer(self):
        env = self.codex_env(HERDR_ENV="1", HERDR_PANE_ID="w1:p1", HERDR_BIN_PATH=self.herdr)
        self.write([codex_count(20000, 20000), codex_count(150000, 170000)])
        evt = {"hook_event_name": "PostToolUse", "session_id": self.sid, "cwd": self.root,
               "transcript_path": self.transcript}
        self.assertIn("context-pressure", self.run_event(evt, env))
        self.save()
        self.run_event(dict(evt, hook_event_name="Stop", last_assistant_message="saved"), env)
        deadline = time.time() + 15
        calls = []
        while time.time() < deadline:
            calls = open(self.calls).read().splitlines() if os.path.exists(self.calls) else []
            if "herdr pane send-text w1:p1 resume" in calls:
                break
            time.sleep(0.05)
        self.assertIn("herdr pane send-text w1:p1 /clear", calls)
        self.assertIn("herdr pane send-text w1:p1 resume", calls)


if __name__ == "__main__":
    unittest.main()
