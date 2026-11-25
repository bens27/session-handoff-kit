#!/usr/bin/env python3
"""A /clear successor is told how to continue its predecessor's Pullboard seat (G31).

Pullboard binds a checkout to one agent session in
<git-dir>/pullboard-checkout-session.json as {agent, digest}, where digest is
sha256 of ["CLAUDE_CODE_SESSION_ID", <session id>] in compact JSON. When a
session in this /clear lineage holds that binding and no checkpoint handed it
over, SessionStart names the seat, the checkout and `pullboard takeover`. A
checkpoint saved by the seat holder records the seat, and resuming it names the
same step. The kit never runs takeover: these tests put no pullboard on PATH.

Run: python3 -m pytest -q tests/test_clear_seat_notice.py"""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_clear_lineage as base  # noqa: E402

STEP = 'pullboard takeover'


def digest(session):
    return hashlib.sha256(json.dumps(['CLAUDE_CODE_SESSION_ID', session], separators=(',', ':')).encode()).hexdigest()


class SeatNotice(base.ClearSuccessor):
    def env(self, terminal):
        env = super().env(terminal)
        env['PATH'] = '/usr/bin:/bin'  # no pullboard: the kit must never run it
        env.pop('CLAUDE_CODE_SESSION_ID', None)
        return env

    def bind(self, agent, session):
        (self.proj / '.git' / 'pullboard-checkout-session.json').write_text(json.dumps(
            dict(version=1, agent=agent, digest=digest(session), at='2026-11-22T00:00:00.000Z')) + '\n')

    def start(self, session, predecessor, terminal='tab-after'):
        path = base.transcript(self.transcripts, session, predecessor)
        evt = dict(hook_event_name='SessionStart', source='clear', session_id=session,
                   transcript_path=str(path), cwd=str(self.proj))
        out = self.call([str(base.HOOKS / 'context_watch.py')], json.dumps(evt), terminal)
        return json.loads(out)['hookSpecificOutput']['additionalContext'] if out.strip() else ''

    def test_unsaved_seat_holder_is_named_with_the_takeover_step(self):
        self.bind('hooks-13', 'pred')
        base.transcript(self.transcripts, 'pred')
        for session, predecessor in (('succ1', 'pred'), ('succ2', 'succ1')):
            with self.subTest(session=session):
                note = self.start(session, predecessor)
                self.assertIn('held Pullboard seat hooks-13 in %s' % self.proj, note)
                self.assertIn('nothing handed it over', note)
                self.assertIn('cd %s && %s' % (self.proj, STEP), note)

    def test_no_notice_without_a_seat_or_when_this_session_holds_it(self):
        base.transcript(self.transcripts, 'pred')
        self.assertNotIn(STEP, self.start('succ', 'pred'))
        self.bind('hooks-13', 'succ2')
        self.assertNotIn(STEP, self.start('succ2', 'succ'))
        self.bind('hooks-13', 'someone-else')  # a seat outside the lineage is never ours to take
        self.assertNotIn(STEP, self.start('succ3', 'succ2'))

    def test_no_notice_on_startup_or_for_damaged_bindings(self):
        base.transcript(self.transcripts, 'pred')
        (self.proj / '.git' / 'pullboard-checkout-session.json').write_text('{"agent": "x\\nforged", "digest": 5}')
        self.assertNotIn(STEP, self.start('succ', 'pred'))
        self.bind('hooks-13', 'pred')
        path = base.transcript(self.transcripts, 'fresh')
        evt = dict(hook_event_name='SessionStart', source='startup', session_id='fresh',
                   transcript_path=str(path), cwd=str(self.proj))
        self.assertNotIn(STEP, self.call([str(base.HOOKS / 'context_watch.py')], json.dumps(evt), 'tab-after'))

    def test_open_checkpoint_from_the_lineage_handed_it_over(self):
        self.bind('hooks-13', 'pred')
        self.save('coordinator', 'pred', 'tab-before')
        base.transcript(self.transcripts, 'pred')
        self.assertNotIn(STEP, self.start('succ', 'pred'))

    def test_checkpoint_records_the_seat_and_resume_names_the_step(self):
        self.bind('hooks-13', 'pred')
        saved = self.save('coordinator', 'pred', 'tab-before')
        self.assertIn('pullboard_seat: hooks-13', Path(saved).read_text())
        other = self.save('unseated', 'someone-else', 'tab-other')
        self.assertNotIn('pullboard_seat', Path(other).read_text())
        base.transcript(self.transcripts, 'pred')
        self.start('succ', 'pred')
        r = self.dispatch('succ', 'tab-after')
        self.assertEqual(r['outcome'], 'proceed', json.dumps(r)[:600])
        out = json.loads(self.call([str(base.HOOKS / 'handoff_ledger.py')] + r['next_action']['command'].split()[2:],
                                   '', 'tab-after'))
        seat = [l for l in out.get('workspace', []) if l.startswith('pullboard_seat:')]
        self.assertEqual(len(seat), 1, out)
        self.assertIn('hooks-13', seat[0])
        self.assertIn('cd %s && %s' % (self.proj, STEP), seat[0])

    def test_resume_is_silent_once_the_seat_moved(self):
        self.bind('hooks-13', 'pred')
        saved = self.save('coordinator', 'pred', 'tab-before')
        self.bind('hooks-13', 'succ')  # already taken over
        out = json.loads(self.call([str(base.HOOKS / 'handoff_ledger.py'), 'resume', saved, '--session', 'succ'],
                                   '', 'tab-after'))
        self.assertFalse([l for l in out.get('workspace', []) if l.startswith('pullboard_seat:')], out)


if __name__ == '__main__':
    unittest.main()
