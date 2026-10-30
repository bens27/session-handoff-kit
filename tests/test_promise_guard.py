#!/usr/bin/env python3
"""#180: a turn must not end after a context-watch notice (advisory or threshold)
while this session has published no checkpoint. The Stop hook blocks once, in
every mode, so the agent saves before telling the user anything continues later.

Run: python3 -m pytest tests/test_promise_guard.py"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_context_watch_regressions import HookCase, claude_call  # noqa: E402
from test_dispatch import BODY, LEDGER  # noqa: E402


class PromiseGuard(HookCase):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.tmp, ".handoffs"))
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.sid)
        self.base = os.path.join(self.tmp, "context-watch-%s.fired" % safe)

    def advise(self):
        with open(self.base + ".advised", "w") as f:
            f.write("103137/100000/130000\n")

    def fire(self):
        with open(self.base, "w") as f:
            f.write("150000/130000/140000\n")

    def stop(self, **extra):
        return self.run_hook(event="Stop", extra=extra)

    def save(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ('AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID', 'HANDOFF_AUTO')}
        env.update(HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0',
                   HANDOFF_SKILL_ROOTS=os.path.join(self.tmp, 'skills'))
        doc = dict(topic='work', description='Work', body=BODY)
        p = subprocess.run([sys.executable, '-B', str(LEDGER), 'save', '.', '--session', self.sid,
                            '--request-id', 'req-guard'], cwd=self.tmp, env=env, input=json.dumps(doc),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(p.stdout)['outcome'], 'saved', p.stdout + p.stderr)

    def test_advisory_without_checkpoint_blocks_once(self):
        self.advise()
        out = self.stop()
        self.assertIn('"decision": "block"', out)
        self.assertIn('continues', json.loads(out)['reason'])
        self.assertEqual(self.stop(), "")

    def test_threshold_without_checkpoint_blocks_in_automatic_mode(self):
        self.fire()
        self.assertIn('"decision": "block"', self.stop(HANDOFF_AUTO='1'))

    def test_advisory_without_checkpoint_blocks_in_automatic_mode(self):
        self.advise()
        self.assertIn('"decision": "block"', self.stop(HANDOFF_AUTO='1'))

    def test_session_that_saved_after_advisory_is_not_blocked(self):
        self.advise()
        self.save()
        self.assertNotIn('"decision": "block"', self.stop())

    def test_checkpoint_saved_before_the_advisory_does_not_count(self):
        # N1: only a checkpoint published since the notice satisfies the guard.
        self.save()
        old = os.path.getmtime(next(p for p in (os.path.join(self.tmp, '.handoffs', f)
                                                  for f in os.listdir(os.path.join(self.tmp, '.handoffs')))
                                      if p.endswith('.md')))
        self.advise()
        os.utime(self.base + ".advised", (old + 60, old + 60))
        self.assertIn('"decision": "block"', self.stop())

    def test_advisory_guard_rearms_after_compaction(self):
        # B2: compaction re-arms the advisory, so its spent block must re-arm too.
        extra = {"CONTEXT_WATCH_THINKING": "0"}
        self.write([claude_call(20000), claude_call(105000)])
        self.assertIn("Early warning", self.run_hook(extra=extra))
        self.assertIn('"decision": "block"', self.stop(**extra))
        self.assertEqual(self.stop(**extra), "")
        self.write([claude_call(20000)], "a")  # compaction
        self.run_hook(extra=extra)
        self.write([claude_call(105000)], "a")
        self.assertIn("Early warning", self.run_hook(extra=extra))
        self.assertIn('"decision": "block"', self.stop(**extra))

    def test_no_notice_never_blocks(self):
        self.assertEqual(self.stop(), "")


if __name__ == "__main__":
    unittest.main()
