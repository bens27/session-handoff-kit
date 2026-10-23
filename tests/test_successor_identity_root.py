"""14752baa (A+B): adopt finds the successor under its own root.

Incident 9fe919eb: the predecessor cd'd into a linked worktree and restarted with
--root <worktree>; the successor launched at the live root, so its hooks recorded
it under the live-root key while adopt read the worktree key, still naming the
predecessor (its own PostToolUse of the restart call refreshed it AFTER dispatch).
The fixture uses a real `git worktree add`: a scratch `git init` directory would
share nothing with the live key and would not reproduce the split.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))

import context_watch  # noqa: E402
import handoff_intents as intents  # noqa: E402
import handoff_identity  # noqa: E402
import handoff_protocol as protocol  # noqa: E402
import restart_preflight as restart  # noqa: E402

BODY = '## Objective\nFix it\n## Current state\nSaved MARKER\n## Next steps\nTest it\n'
GIT = ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']


class SuccessorIdentityRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        home, self.state = base / 'home', base / 'state'
        home.mkdir(); self.state.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('AGENTSROOM_AGENT_ID', 'CODEX_THREAD_ID', 'HANDOFF_SESSION_ID',
                                 'CONTEXT_WATCH_DISABLE')}
        self.env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(self.state),
                        HANDOFF_STATE_DIR=str(self.state), HANDOFF_TERMINAL_ID='term',
                        GIT_CONFIG_NOSYSTEM='1', HANDOFF_AUTO='0', CONTEXT_WATCH_LOG='0',
                        CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_JEV='0', PYTHONDONTWRITEBYTECODE='1')
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, 'tempdir', None)
        self.live = base / 'checkout'
        git = lambda *a: subprocess.run(['git', *a], env=self.env, check=True, capture_output=True)
        git('init', '-q', str(self.live))
        git('-C', str(self.live), *GIT, 'commit', '-qm', 'Fixture', '--allow-empty')
        self.worktree = self.live / '.worktrees/lane'
        git('-C', str(self.live), 'worktree', 'add', '-q', '--detach', str(self.worktree))
        for root in (self.live, self.worktree):
            (root / '.handoffs').mkdir()
        self.assertNotEqual(handoff_identity.canonical_root(str(self.live)),
                            handoff_identity.canonical_root(str(self.worktree)))
        # Predecessor works inside the worktree and restarts with --root <worktree>.
        context_watch.write_terminal_pointer(str(self.worktree), 'old')
        self.path = protocol.save(str(self.worktree), 'old', 'save-1', {
            'topic': 'work', 'description': 'Continue work', 'body': BODY})['path']
        ready = restart.preflight(str(self.worktree), self.path, 'old', 'term', 'req-1',
                                  workers=[], host_capable=True)
        self.assertEqual(ready['outcome'], 'ready', ready)
        self.prompt = ready['restart_prompt']
        self.dispatched_at = restart.status(str(self.worktree), 'req-1')['dispatch_started_at']
        # The predecessor's own PostToolUse for agents_restart refreshes its
        # worktree pointer after dispatch (incident: 23:52:09.7Z > 23:52:03.5Z).
        context_watch.write_terminal_pointer(str(self.worktree), 'old')
        self.assertGreater(self.pointer_ts(self.worktree), self.dispatched_at)

    def pointer_ts(self, root):
        path = handoff_identity.terminal_pointer_path(str(root), 'term')
        return json.loads(Path(path).read_text())['ts']

    def session_start(self, cwd, session='new'):
        evt = dict(hook_event_name='SessionStart', source='startup', session_id=session, cwd=str(cwd),
                   transcript_path=str(Path(self.tmp.name) / 'absent.jsonl'))
        subprocess.run([sys.executable, str(HOOKS / 'context_watch.py')], input=json.dumps(evt).encode(),
                       env=self.env, cwd=str(cwd), capture_output=True, timeout=60, check=False)

    def adopt(self, cwd):
        # The successor has not cd'd: adopt runs from the directory it launched in.
        argv = shlex.split(self.prompt[len('Continue the handoff: run exactly '):])
        run = subprocess.run([sys.executable] + argv[1:], env=self.env, cwd=str(cwd),
                             text=True, capture_output=True, timeout=120)
        return json.loads(run.stdout)

    def test_successor_at_live_root_adopts_worktree_bound_intent(self):
        self.session_start(self.live)
        self.assertEqual(protocol.session_info(str(self.live))['session_id'], 'new')
        self.assertEqual(protocol.session_info(str(self.worktree))['session_id'], 'old')
        result = self.adopt(self.live)
        self.assertEqual(result['outcome'], 'resumed', result)
        self.assertEqual(restart.status(str(self.worktree), 'req-1')['successor_session'], 'new')

    def test_successor_launched_inside_worktree_still_adopts(self):
        self.session_start(self.worktree)
        result = self.adopt(self.worktree)
        self.assertEqual(result['outcome'], 'resumed', result)

    def test_no_successor_hook_blocks_with_precise_reason(self):
        result = self.adopt(self.live)
        self.assertEqual((result['outcome'], result['stage'], result['reason']),
                         ('blocked', 'identity', 'successor-identity-mismatch'), result)
        self.assertIn('no successor observation at', result['detail'])
        self.assertIn('pointer still names the predecessor', result['detail'])
        self.assertIn(str(self.worktree), result['detail'])
        self.assertEqual(restart.status(str(self.worktree), 'req-1')['state'], 'pending')

    def test_live_root_pointer_naming_predecessor_falls_back_to_binding_root(self):
        context_watch.write_terminal_pointer(str(self.live), 'old')
        context_watch.write_terminal_pointer(str(self.worktree), 'new')
        result = self.adopt(self.live)
        self.assertEqual(result['outcome'], 'resumed', result)

    def backdate(self, root, session):
        context_watch.write_terminal_pointer(str(root), session)
        path = Path(handoff_identity.terminal_pointer_path(str(root), 'term'))
        data = json.loads(path.read_text())
        data['ts'] = self.dispatched_at - 3600
        path.write_text(json.dumps(data))

    def assert_stale_blocked(self, result, root):
        self.assertEqual((result['outcome'], result['stage'], result['reason']),
                         ('blocked', 'identity', 'successor-identity-mismatch'), result)
        self.assertIn('pointer at %s predates dispatch' % root, result['detail'])
        self.assertEqual(restart.status(str(self.worktree), 'req-1')['state'], 'pending')
        self.assertNotIn('successor_session', restart.status(str(self.worktree), 'req-1'))

    def test_live_root_pointer_older_than_dispatch_is_not_the_successor(self):
        # Reviewer M1: a third session of this terminal, recorded before the restart.
        self.backdate(self.live, 'stale')
        self.assert_stale_blocked(self.adopt(self.live), self.live)

    def test_binding_root_pointer_older_than_dispatch_is_not_the_successor(self):
        context_watch.write_terminal_pointer(str(self.live), 'old')
        self.backdate(self.worktree, 'stale')
        self.assert_stale_blocked(self.adopt(self.live), self.worktree)

    def test_missing_dispatch_marker_fails_closed(self):
        # Reviewer N1: an intent that crashed before its dispatch marker gives no
        # time to judge a pointer by, so no hook-recorded candidate may adopt it.
        location = intents.location(str(self.worktree), 'req-1')
        record = json.loads(Path(location).read_text())
        record.pop('dispatch_started_at')
        intents.write(location, record)
        self.session_start(self.live)
        self.assert_stale_blocked(self.adopt(self.live), self.live)

    def test_roots_checked_lists_every_root(self):
        result = self.adopt(self.live)
        self.assertEqual(result['identity']['observed']['roots_checked'], [str(self.live), str(self.worktree)])


if __name__ == '__main__':
    unittest.main()
