#!/usr/bin/env python3
"""#180: a turn must not end after a context-watch notice (advisory or threshold)
while this session has published no checkpoint. Per +10k-token band past the
notice the agent is reminded once and the user is warned once at Stop, in every
mode; nothing ever blocks (#184).

Run: python3 -m pytest tests/test_promise_guard.py"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_context_watch_regressions import HookCase, claude_call, codex_count  # noqa: E402
from test_dispatch import BODY, LEDGER  # noqa: E402

sys.path.insert(0, os.path.join(HERE, "..", "skills", "session-handoff", "hooks"))
import handoff_identity  # noqa: E402


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

    def occupancy(self, tokens):
        """Leave the session note the Stop hook reads, as write_session_note does."""
        import time
        path = handoff_identity.session_note_path(self.tmp, self.sid, self.tmp)
        with open(path, "w") as f:
            json.dump({"session_id": self.sid, "cwd": self.tmp, "ts": time.time(),
                       "observed_occupancy_tokens": tokens}, f)

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

    def assertWarns(self, out, blocks=True):
        """#186: the user warning on every guarded stop; the block on the first stop of a band."""
        out = json.loads(out)
        self.assertIn('no checkpoint', out['systemMessage'])
        if not blocks:
            self.assertNotIn('decision', out)
            return out['systemMessage']
        self.assertEqual(out['decision'], 'block')
        self.assertIn('nothing was saved and why', out['reason'])
        self.assertIn('only a saved checkpoint does', out['reason'])
        return out['systemMessage']

    def continued_stop(self):
        """A Stop whose turn a block already continued (stop_hook_active)."""
        from test_context_watch_regressions import CLEAN, HOOK
        env = {k: v for k, v in os.environ.items() if k not in CLEAN}
        env.update(HOME=self.tmp, TMPDIR=self.tmp, CONTEXT_WATCH_LOG="0", CONTEXT_WATCH_JEV="0",
                   CONTEXT_WATCH_AGENT="claude")
        evt = {"hook_event_name": "Stop", "session_id": self.sid, "cwd": self.tmp,
               "transcript_path": self.transcript, "stop_hook_active": True}
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(evt), env=env,
                           capture_output=True, text=True, timeout=30)
        return p.stdout.strip()

    def test_continued_stop_warns_without_blocking(self):
        # A band first reached on a stop a block already continued: warn only, never loop.
        self.advise()
        self.occupancy(103137)
        self.assertWarns(self.stop())
        self.occupancy(114000)
        self.assertWarns(self.continued_stop(), blocks=False)
        self.assertEqual(self.stop(), "")  # that band is spent

    # ---- Stop: a non-blocking warning to the user, never a block (#184)

    def test_advisory_without_checkpoint_blocks_and_warns_once(self):
        self.advise()
        msg = self.assertWarns(self.stop())
        self.assertIn('clear', msg)
        self.assertEqual(self.stop(), "")

    def test_threshold_without_checkpoint_warns_in_automatic_mode(self):
        self.fire()
        self.assertWarns(self.stop(HANDOFF_AUTO='1'))

    def test_advisory_without_checkpoint_warns_in_automatic_mode(self):
        self.advise()
        self.assertWarns(self.stop(HANDOFF_AUTO='1'))

    def test_session_that_saved_after_advisory_is_not_warned(self):
        self.advise()
        self.save()
        self.assertEqual(self.stop(), "")

    def test_checkpoint_saved_before_the_advisory_does_not_count(self):
        # N1: only a checkpoint published since the notice satisfies the guard.
        self.save()
        old = os.path.getmtime(next(p for p in (os.path.join(self.tmp, '.handoffs', f)
                                                  for f in os.listdir(os.path.join(self.tmp, '.handoffs')))
                                      if p.endswith('.md')))
        self.advise()
        os.utime(self.base + ".advised", (old + 60, old + 60))
        self.assertWarns(self.stop())

    def test_stop_warning_once_per_10k_band(self):
        # B1 kept: one Stop warning per +10k band past the notice, never twice in a band.
        self.advise()
        self.occupancy(103137)
        self.assertWarns(self.stop())
        self.occupancy(110000)
        self.assertEqual(self.stop(), "")
        self.occupancy(114000)
        self.assertWarns(self.stop())
        self.assertEqual(self.stop(), "")

    def test_threshold_stop_warning_rearms_per_band(self):
        self.fire()
        self.occupancy(150000)
        self.assertWarns(self.stop())
        self.occupancy(161000)
        self.assertWarns(self.stop())

    def test_no_notice_never_warns(self):
        self.assertEqual(self.stop(), "")

    # ---- the agent: one additionalContext reminder per +10k band (#184)

    EXTRA = {"CONTEXT_WATCH_THINKING": "0"}

    def call(self, tokens):
        self.write([claude_call(tokens)], "a")
        return self.context(self.run_hook(extra=self.EXTRA))

    def test_f9997a4a_replay_reminds_and_blocks_per_band(self):
        self.write([claude_call(20000)])
        self.run_hook(extra=self.EXTRA)
        self.assertIn("Early warning", self.call(105000))
        self.assertEqual(self.call(106000), "")  # band 0: the notice itself
        first = self.call(116000)
        self.assertIn("save a checkpoint before", first.lower())
        self.assertIn("never", first.lower())
        self.assertEqual(self.call(117000), "")  # same band: once
        self.assertIn("checkpoint", self.call(126000))  # next band
        self.assertWarns(self.stop(**self.EXTRA))  # band 2: blocks again, with the warning
        self.assertEqual(self.stop(**self.EXTRA), "")

    def test_save_silences_reminders_and_warning(self):
        self.write([claude_call(20000)])
        self.run_hook(extra=self.EXTRA)
        self.assertIn("Early warning", self.call(105000))
        self.save()
        for tokens in (116000, 126000):
            self.assertEqual(self.call(tokens), "")
        self.assertEqual(self.stop(**self.EXTRA), "")

    def test_reminders_rearm_after_compaction(self):
        # B2 kept: compaction re-arms the advisory and every spent band marker.
        self.write([claude_call(20000)])
        self.run_hook(extra=self.EXTRA)
        self.assertIn("Early warning", self.call(105000))
        self.assertIn("checkpoint", self.call(116000))
        self.assertWarns(self.stop(**self.EXTRA))
        self.call(20000)  # compaction
        self.assertIn("Early warning", self.call(105000))
        self.assertIn("checkpoint", self.call(116000))
        self.assertWarns(self.stop(**self.EXTRA))

    def test_codex_gets_the_reminder_on_post_tool_use(self):
        self.codex = True
        self.advise()
        self.write([codex_count(114000, 114000)])
        self.assertIn("Save a checkpoint before", self.context(self.run_hook(extra=self.EXTRA)))
        self.assertEqual(self.context(self.run_hook(extra=self.EXTRA)), "")

    def test_reminder_source_has_no_block(self):
        import inspect
        sys.path.insert(0, os.path.join(HERE, "..", "skills", "session-handoff", "hooks"))
        import context_watch
        self.assertNotIn('"block"', inspect.getsource(context_watch.remind_unwritten_handoff))


if __name__ == "__main__":
    unittest.main()
