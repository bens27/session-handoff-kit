"""#194: resuming another terminal's checkpoint while that terminal is live.

Liveness signal: the owner terminal's hook pointer (context-watch-terminal-*.json),
refreshed by the owner's own hooks. A fresh pointer means the owner tab is live.
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'))
from test_dispatch import Base  # noqa: E402
import handoff_identity  # noqa: E402


class ForeignLiveOwnerTests(Base):
    def setUp(self):
        super().setUp()
        self.record_hook_session('sess-a', terminal='term-a')
        self.foreign = self.save(topic='chief-work', session='sess-b', terminal='term-b')

    def age_owner_pointer(self, seconds):
        old = dict(os.environ)
        os.environ['TMPDIR'] = self.env['TMPDIR']
        try:
            path = handoff_identity.terminal_pointer_path(str(self.root), 'term-b', self.env['TMPDIR'])
        finally:
            os.environ.clear()
            os.environ.update(old)
        with open(path, 'w') as f:
            json.dump(dict(session_id='sess-b', host_session_id='', ts=time.time() - seconds), f)

    def test_live_owner_blocks_exact_resume(self):
        self.age_owner_pointer(5)
        r = self.dispatch(operation='resume', target=self.foreign)
        self.assertEqual((r['outcome'], r['code']), ('ask', 'owner_live'), r)
        self.assertFalse(r['authorized_to_execute'])
        self.assertEqual(r['target']['owner']['terminal'], 'term-b')
        self.assertTrue(r['target']['owner']['live'])

    def test_explicit_confirmation_proceeds_with_warning(self):
        self.age_owner_pointer(5)
        r = self.dispatch(operation='resume', target=self.foreign, confirm_not_owner=True)
        self.assertEqual(r['outcome'], 'proceed', r)
        self.assertEqual(r['target']['owner']['terminal'], 'term-b')
        self.assertTrue(any(w.startswith('owner_live_confirmed') for w in r.get('warnings', [])), r)

    def test_dead_owner_resume_unchanged(self):
        self.age_owner_pointer(7200)
        r = self.dispatch(operation='resume', target=self.foreign)
        self.assertEqual(r['outcome'], 'proceed', r)
        self.assertNotIn('owner', r['target'])

    def test_owner_without_pointer_resume_unchanged(self):
        r = self.dispatch(operation='resume', target=self.foreign)
        self.assertEqual(r['outcome'], 'proceed', r)

    def test_own_checkpoint_resume_unchanged(self):
        mine = self.save(topic='my-work', session='sess-a', terminal='term-a')
        r = self.dispatch(operation='resume', target=mine)
        self.assertEqual(r['outcome'], 'proceed', r)
        self.assertNotIn('owner', r['target'])

    def test_retrieve_of_live_owner_checkpoint_is_not_blocked(self):
        self.age_owner_pointer(5)
        r = self.dispatch(operation='retrieve', target=self.foreign)
        self.assertNotEqual(r.get('code'), 'owner_live', r)


if __name__ == '__main__':
    unittest.main()
