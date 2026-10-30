#!/usr/bin/env python3
"""#180: a turn must not end after a context-watch notice (advisory or threshold)
while this session has published no checkpoint. #208: after the advisory only the
user warning fires; reminders and blocks start at the threshold notice. Per +10k-token band past the
notice the agent is reminded once and the first Stop blocks once; every Stop
while unsaved warns the user, in every mode (#186). Codex: reminder only.

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
        self.fire()  # #208: blocks are threshold-only
        self.occupancy(150000)
        self.assertWarns(self.stop())
        self.occupancy(161000)
        self.assertWarns(self.continued_stop(), blocks=False)
        self.assertWarns(self.stop())  # N1: the continued stop claimed nothing
        self.assertWarns(self.stop(), blocks=False)  # now that band's block is spent

    # ---- #204 / decision #208: the advisory arms the user warning only

    def test_204_advisory_only_warns_never_blocks_or_reminds(self):
        self.advise()
        for tokens in (103137, 114000, 124000):
            self.occupancy(tokens)
            self.assertWarns(self.stop(), blocks=False)
        self.write([claude_call(20000), claude_call(124000)])
        self.assertEqual(self.context(self.run_hook(extra={"CONTEXT_WATCH_THINKING": "0"})), "")

    # ---- Stop: one block per band, a warning to the user on every unsaved stop (#186)

    def test_advisory_without_checkpoint_warns_without_block(self):
        self.advise()
        msg = self.assertWarns(self.stop(), blocks=False)  # #208
        self.assertIn('clear', msg)
        self.assertWarns(self.stop(), blocks=False)

    def test_threshold_without_checkpoint_warns_in_automatic_mode(self):
        self.fire()
        self.assertWarns(self.stop(HANDOFF_AUTO='1'))

    def test_advisory_without_checkpoint_warns_in_automatic_mode(self):
        self.advise()
        self.assertWarns(self.stop(HANDOFF_AUTO='1'), blocks=False)

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
        self.assertWarns(self.stop(), blocks=False)  # #208: warning, no block

    def test_stop_blocks_once_per_10k_band(self):
        # B1 kept: one Stop block per +10k band past the notice; later stops in the band only warn.
        self.fire()  # #208: blocks are threshold-only
        self.occupancy(150000)
        self.assertWarns(self.stop())
        self.occupancy(157000)
        self.assertWarns(self.stop(), blocks=False)
        self.occupancy(161000)
        self.assertWarns(self.stop())
        self.assertWarns(self.stop(), blocks=False)

    def test_threshold_stop_warning_rearms_per_band(self):
        self.fire()
        self.occupancy(150000)
        self.assertWarns(self.stop())
        self.occupancy(161000)
        self.assertWarns(self.stop())

    def test_S1_every_unsaved_stop_warns_user(self):
        # S1: every Stop warns while nothing was saved since the notice.
        self.fire()  # #208: blocks are threshold-only
        self.occupancy(150000)
        self.assertWarns(self.stop())
        self.occupancy(152000)
        self.assertWarns(self.stop(), blocks=False)

    def test_N1_continued_stop_keeps_next_band_block(self):
        # N1: a stop_hook_active stop never claims a band's block.
        self.fire()  # #208: blocks are threshold-only
        self.occupancy(150000)
        self.assertWarns(self.stop())
        self.occupancy(161000)
        self.assertWarns(self.continued_stop(), blocks=False)
        self.occupancy(162000)
        self.assertWarns(self.stop())

    def test_no_notice_never_warns(self):
        self.assertEqual(self.stop(), "")

    # ---- the agent: one additionalContext reminder per +10k band (#186)

    EXTRA = {"CONTEXT_WATCH_THINKING": "0"}

    def call(self, tokens):
        self.write([claude_call(tokens)], "a")
        return self.context(self.run_hook(extra=self.EXTRA))

    def test_f9997a4a_replay_warns_only_below_threshold(self):
        # #208 retarget: f9997a4a promised a handoff at ~106k, below the 130k threshold. The
        # advisory arms only the user warning (every unsaved Stop); no save reminder, no block.
        self.write([claude_call(20000)])
        self.run_hook(extra=self.EXTRA)
        self.assertIn("Early warning", self.call(105000))
        for tokens in (106000, 116000, 126000):
            self.assertEqual(self.call(tokens), "")
        self.assertWarns(self.stop(**self.EXTRA), blocks=False)
        self.assertWarns(self.stop(**self.EXTRA), blocks=False)

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
        self.assertEqual(self.call(116000), "")  # #208
        self.assertWarns(self.stop(**self.EXTRA), blocks=False)
        self.call(20000)  # compaction
        self.assertIn("Early warning", self.call(105000))
        self.assertEqual(self.call(116000), "")  # #208
        self.assertWarns(self.stop(**self.EXTRA), blocks=False)

    def test_codex_gets_the_reminder_on_post_tool_use(self):
        self.codex = True
        self.fire()  # #208
        self.write([codex_count(161000, 161000)])
        self.assertIn("Save a checkpoint before", self.context(self.run_hook(extra=self.EXTRA)))
        self.assertEqual(self.context(self.run_hook(extra=self.EXTRA)), "")

    def test_reminder_source_has_no_block(self):
        import inspect
        sys.path.insert(0, os.path.join(HERE, "..", "skills", "session-handoff", "hooks"))
        import context_watch
        self.assertNotIn('"block"', inspect.getsource(context_watch.remind_unwritten_handoff))


if __name__ == "__main__":
    unittest.main()
