"""9452e1fe: a context-pressure restart blocked by a hold is offered again.

Incident: context-watch fired once; the agent saved a checkpoint and ran
`restart_preflight.py restart --hold` while an ask_user card was open (blocked,
human-hold, correct). The hold cleared minutes later and nothing re-offered the
restart: the second notice stays quiet once a checkpoint is published, so the
session ran ~30 more turns past 250k tokens in silence.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))

import context_watch  # noqa: E402
import handoff_holds as holds  # noqa: E402
import handoff_protocol as protocol  # noqa: E402
import restart_preflight as restart  # noqa: E402

BODY = '## Objective\nFix it\n## Current state\nSaved\n## Next steps\nTest it\n'
SESSION = 's-9452e1fe'


class RestartBlockedReoffer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root, state, home = base / 'project', base / 'state', base / 'home'
        for d in (self.root / '.handoffs', state, home):
            d.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('CONTEXT_WATCH_', 'HANDOFF_')) and k not in (
                        'AGENTSROOM_AGENT_ID', 'CODEX_THREAD_ID', 'AUTORESUME', 'TMUX_PANE')}
        self.env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(state),
                        HANDOFF_STATE_DIR=str(state), HANDOFF_TERMINAL_ID='term', HANDOFF_AT='100000',
                        HANDOFF_AUTO='0', CONTEXT_WATCH_AGENT='claude', CONTEXT_WATCH_LOG='0',
                        CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_JEV='0', PYTHONDONTWRITEBYTECODE='1')
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, 'tempdir', None)
        self.transcript = base / 'transcript.jsonl'
        self.calls = 0

    def call(self, tokens):
        """One more main-chain model call: a new usage entry is a new model turn."""
        self.calls += 1
        entry = {'message': {'model': 'claude-sonnet-5-5', 'id': 'm%d' % self.calls,
                             'usage': {'input_tokens': 2000, 'cache_read_input_tokens': tokens - 2000}}}
        with open(self.transcript, 'a') as fh:
            fh.write(json.dumps(entry) + '\n')

    def hook(self, event, prompt='go on'):
        evt = dict(hook_event_name=event, session_id=SESSION, cwd=str(self.root),
                   transcript_path=str(self.transcript))
        if event == 'UserPromptSubmit':
            evt['prompt'] = prompt
        run = subprocess.run([sys.executable, str(HOOKS / 'context_watch.py')], input=json.dumps(evt),
                             env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = run.stdout.strip()
        return json.loads(out)['hookSpecificOutput']['additionalContext'] if out else ''

    def turn(self, tokens=131000, event='UserPromptSubmit', prompt='go on'):
        self.call(tokens)
        return self.hook(event, prompt)

    def marker(self):
        return Path(restart.restart_block_path(SESSION))

    def start(self):
        self.assertEqual(self.turn(20000), '')  # startup context well under the threshold

    def first_notice_and_save(self):
        self.start()
        notice = self.turn(130000)
        self.assertIn('over the 100,000-token handoff threshold', notice)
        saved = protocol.save(str(self.root), SESSION, 'save-1', {
            'topic': 'work', 'description': 'Continue work', 'body': BODY})
        self.assertEqual(saved['outcome'], 'saved', saved)
        self.path = saved['path']

    def restart(self, **kw):
        args = dict(workers=[], host_capable=True)
        args.update(kw)
        return restart.restart(str(self.root), **args)

    # -- 1. hold blocked -> hold cleared -> notice ---------------------------

    def test_caller_hold_cleared_by_next_prompt_reoffers_restart(self):
        self.first_notice_and_save()
        self.call(131000)
        blocked = self.restart(hold=True)
        self.assertEqual((blocked['outcome'], blocked['reason']), ('blocked', 'human-hold'), blocked)
        # Same turn: the ask_user card is still open; nothing to offer yet.
        self.assertEqual(self.hook('PostToolUse'), '')
        reoffer = self.turn()  # the human answered: a new prompt
        # The hook cannot see the card: it must not claim the hold cleared.
        self.assertNotIn('hold has cleared', reoffer)
        self.assertIn('keep waiting (or pass --hold)', reoffer)
        self.assertIn('restart_preflight.py restart --root %s --path %s' % (self.root, self.path), reoffer)
        self.assertIn('keep waiting (or pass --hold). Otherwise, finish the action in progress', reoffer)
        self.assertIn('--workers', reoffer)
        self.assertIn('--host-capable', reoffer)
        self.assertIn(self.path, reoffer)
        self.assertIn('cache-read', reoffer)
        self.assertEqual(self.turn(), '')  # offered once, not every turn

    def test_mailbox_notification_prompt_does_not_claim_the_card_was_answered(self):
        self.first_notice_and_save()
        self.call(131000)
        self.assertEqual(self.restart(hold=True)['reason'], 'human-hold')
        mail = ('[AgentsRoom] Notification: mail from chief-of-staff\nSubject: status?\n'
                'Read it in full with agents_read_inbox.')
        notice = self.turn(prompt=mail)
        self.assertNotIn('hold has cleared', notice)
        self.assertIn('If a question to the user is still unanswered, keep waiting', notice)

    def test_early_blocked_restart_removes_a_stale_marker(self):
        self.first_notice_and_save()
        self.assertEqual(self.restart(hold=True)['reason'], 'human-hold')
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.restart(path=str(self.root / 'missing.md'))['reason'], 'checkpoint-unreadable')
        self.assertFalse(self.marker().exists())

    def test_malformed_marker_is_treated_as_missing(self):
        self.first_notice_and_save()
        self.restart(workers=None)
        value = json.loads(self.marker().read_text())
        value['turns'] = 'x'
        self.marker().write_text(json.dumps(value))
        self.assertEqual(self.turn(), '')  # no "Context-watch failed"
        self.assertFalse(self.marker().exists())

    def test_durable_hold_still_in_force_stays_quiet_then_reoffers_once_released(self):
        self.first_notice_and_save()
        holds.set_hold(str(self.root), 'project', 'review pending')
        blocked = self.restart()
        self.assertEqual((blocked['outcome'], blocked['reason']), ('blocked', 'human-hold'), blocked)
        for _ in range(3):
            self.assertEqual(self.turn(), '')
        holds.set_hold(str(self.root), 'project', None)
        self.assertIn('hold has cleared', self.turn(event='PostToolUse'))

    def test_ready_restart_clears_the_block(self):
        self.first_notice_and_save()
        self.assertEqual(self.restart(hold=True)['reason'], 'human-hold')
        self.assertEqual(self.restart()['outcome'], 'ready')
        for _ in range(6):
            self.assertEqual(self.turn(), '')

    # -- 2. acted on + restart blocked + flat occupancy -> reminder within N --

    def test_blocked_restart_reminds_every_n_turns_while_over_threshold(self):
        self.first_notice_and_save()
        blocked = self.restart(workers=None)
        self.assertEqual(blocked['outcome'], 'blocked', blocked)
        notices = [self.turn() for _ in range(context_watch.RESTART_REMIND_TURNS * 2)]
        fired = [i for i, n in enumerate(notices) if n]
        self.assertEqual(fired, [context_watch.RESTART_REMIND_TURNS - 1,
                                 context_watch.RESTART_REMIND_TURNS * 2 - 1], notices)
        self.assertIn('restart is still blocked', notices[fired[0]])
        self.assertIn(blocked['reason'], notices[fired[0]])

    def test_reminder_stops_once_checkpoint_closes(self):
        self.first_notice_and_save()
        self.restart(workers=None)
        text = Path(self.path).read_text().replace('status: open', 'status: closed', 1)
        Path(self.path).write_text(text)
        for _ in range(context_watch.RESTART_REMIND_TURNS + 1):
            self.assertEqual(self.turn(), '')

    # -- 3. plain case still fires once ---------------------------------------

    def test_plain_case_fires_once_at_flat_occupancy(self):
        self.start()
        notices = [self.turn(130000)] + [self.turn() for _ in range(context_watch.RESTART_REMIND_TURNS * 2)]
        self.assertEqual(sum(1 for n in notices if n), 1, notices)


if __name__ == '__main__':
    unittest.main()
