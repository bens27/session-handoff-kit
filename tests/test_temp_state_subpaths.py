"""887c009c: every .handoffs/<sub> state path honours the temp-base trust rule.

At an exact temp base an untrusted .handoffs (symlink, foreign-owned, group/other
writable) is never read or written by dispatch identities, restart intents, holds
or pointers; adopt blocks with a precise reason; save creates a private directory
and refuses one planted between its check and its write.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'skills', 'session-handoff', 'hooks')
sys.path.insert(0, HOOKS)

import handoff_dispatch  # noqa: E402
import handoff_holds  # noqa: E402
import handoff_identity as identity  # noqa: E402
import handoff_intents as intents  # noqa: E402
import handoff_pointers as pointers  # noqa: E402
import handoff_protocol as protocol  # noqa: E402
import restart_preflight as restart  # noqa: E402

BODY = '## Objective\nFix it\n## Current state\nState\n## Next steps\nTest it\n'


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = os.path.realpath(self._tmp.name)
        self.base = os.path.join(tmp, 'tmpbase')
        os.makedirs(self.base)
        self.hdir = os.path.join(self.base, '.handoffs')
        state = os.path.join(tmp, 'state')
        os.makedirs(state)
        env = patch.dict(os.environ, {'HOME': tmp, 'HANDOFF_STATE_DIR': state,
                                      'HANDOFF_TERMINAL_ID': 'term-1', 'AGENTSROOM_AGENT_ID': '',
                                      'CODEX_THREAD_ID': '', 'HANDOFF_SESSION_ID': ''})
        env.start()
        self.addCleanup(env.stop)
        bases = patch.object(identity, 'temp_bases', return_value={self.base})
        bases.start()
        self.addCleanup(bases.stop)

    def untrusted(self):
        os.mkdir(self.hdir)
        os.chmod(self.hdir, 0o777)

    def trusted(self):
        os.mkdir(self.hdir, 0o700)


class LocalStateDir(Case):
    def test_untrusted_raises_and_creates_nothing(self):
        self.untrusted()
        with self.assertRaises(identity.UntrustedStateDir):
            identity.local_state_dir(self.base, 'restart', create=True)
        self.assertEqual(os.listdir(self.hdir), [])

    def test_missing_is_created_private(self):
        path = identity.local_state_dir(self.base, 'restart', create=True)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(os.stat(self.hdir).st_mode & 0o077, 0)

    def test_foreign_owner_raises(self):
        self.trusted()
        with patch.object(identity.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaises(identity.UntrustedStateDir):
                identity.local_state_dir(self.base, 'holds')

    def test_conforming_directory_works(self):
        self.trusted()
        self.assertEqual(identity.local_state_dir(self.base, 'holds'), os.path.join(self.hdir, 'holds'))


class DispatchIdentity(Case):
    def test_planted_identity_ignored_and_none_written(self):
        self.untrusted()
        planted = handoff_dispatch._identity_file(self.base, 'term-1')
        os.makedirs(os.path.dirname(planted))
        with open(planted, 'w') as f:
            json.dump(dict(session='synthetic-planted'), f)
        self.assertEqual(handoff_dispatch.synthetic_identity(self.base), '')
        self.assertEqual(os.listdir(os.path.dirname(planted)), [os.path.basename(planted)])

    def test_conforming_directory_persists(self):
        self.trusted()
        first = handoff_dispatch.synthetic_identity(self.base)
        self.assertTrue(first.startswith('synthetic-'))
        self.assertEqual(handoff_dispatch.synthetic_identity(self.base), first)


class RestartIntents(Case):
    def test_untrusted_intent_directory_refused(self):
        self.untrusted()
        os.mkdir(os.path.join(self.hdir, 'restart'))
        with open(os.path.join(self.hdir, 'restart', 'x.json'), 'w') as f:
            f.write('{}')
        with self.assertRaises(identity.UntrustedStateDir):
            intents.entries(self.base)
        with self.assertRaises(identity.UntrustedStateDir):
            intents.location(self.base, 'req-1')

    def test_preflight_blocks_with_reason(self):
        self.untrusted()
        result = restart.preflight(self.base, os.path.join(self.hdir, 'x.md'), 'old', 'term-1', 'req-1',
                                   workers=[], host_capable=True)
        self.assertEqual(result['outcome'], 'blocked', result)
        self.assertEqual(result['reason'], 'untrusted-temp-handoffs')
        self.assertFalse(os.path.exists(os.path.join(self.hdir, 'restart')))

    def test_adopt_blocks_at_intent_stage(self):
        self.untrusted()
        info = dict(identity_source='hook', session_id='new', host_session_id='')
        with patch.object(restart.protocol, 'session_info', return_value=info):
            result = restart.adopt(self.base, 'req-1')
        self.assertEqual(result['outcome'], 'blocked', result)
        self.assertEqual(result['stage'], 'intent', result)
        self.assertTrue(result['reason'].startswith('untrusted-temp-handoffs'), result)
        self.assertIn('Preserve state', result['action'])
        self.assertFalse(os.path.exists(os.path.join(self.hdir, 'restart')))

    def test_reads_create_nothing(self):
        intents.location(self.base, 'req-1')
        self.assertIsNone(intents.status(self.base, 'req-1'))
        self.assertEqual(intents.entries(self.base), [])
        self.assertFalse(os.path.lexists(self.hdir))


class Holds(Case):
    def test_planted_hold_ignored_and_set_refused(self):
        self.untrusted()
        holds = os.path.join(self.hdir, 'holds')
        os.mkdir(holds)
        with open(os.path.join(holds, 'p.json'), 'w') as f:
            json.dump(dict(scope='project', reason='planted'), f)
        self.assertEqual(handoff_holds.read_holds(self.base), [])
        with self.assertRaises(identity.UntrustedStateDir):
            handoff_holds.set_hold(self.base, 'project', 'x')

    def test_conforming_hold_read(self):
        self.trusted()
        handoff_holds.set_hold(self.base, 'project', 'mine')
        self.assertEqual([h['reason'] for h in handoff_holds.read_holds(self.base)], ['mine'])


class Pointers(Case):
    def test_group_writable_handoffs_refused_at_temp_base(self):
        os.mkdir(self.hdir)
        os.chmod(self.hdir, 0o770)
        os.mkdir(os.path.join(self.hdir, 'pointers'), 0o700)
        with self.assertRaises(ValueError):
            with pointers._check_dirs(self.base):
                pass
        self.assertEqual(pointers.scan(self.base)['diagnostics'][0]['reason'], 'untrusted-temp-handoffs')

    def test_conforming_directory_works(self):
        self.trusted()
        os.mkdir(os.path.join(self.hdir, 'pointers'), 0o700)
        with pointers._check_dirs(self.base):
            pass


class SaveTemporal(Case):
    """The fallback is made unwritable so the temp-base .handoffs destination is used."""

    def setUp(self):
        super().setUp()
        blocker = os.path.join(self.base, 'not-a-dir')
        open(blocker, 'w').close()
        fallback = patch.object(protocol.ledger, '_fallback_dir', return_value=os.path.join(blocker, 'x'))
        fallback.start()
        self.addCleanup(fallback.stop)

    def save(self):
        return protocol.save(self.base, 'old', 'save-1', {
            'topic': 'work', 'description': 'Continue work', 'body': BODY})

    def test_creates_private_handoffs(self):
        result = self.save()
        self.assertEqual(result['outcome'], 'saved', result)
        self.assertEqual(os.path.dirname(result['path']), self.hdir)
        self.assertEqual(os.stat(self.hdir).st_mode & 0o077, 0)

    def test_planted_after_check_is_not_written(self):
        real = protocol.write_json
        planted = []

        def plant_once(path, data):
            if not planted and not os.path.lexists(self.hdir):
                planted.append(1)
                os.mkdir(self.hdir)
                os.chmod(self.hdir, 0o777)
            return real(path, data)
        with patch.object(protocol, 'write_json', side_effect=plant_once):
            self.save()
        self.assertTrue(planted)
        self.assertEqual([n for n in os.listdir(self.hdir) if n.endswith('.md')], [])

    def test_planted_symlink_after_check_is_not_written(self):
        real, elsewhere = protocol.write_json, os.path.join(self.base, 'elsewhere')
        os.mkdir(elsewhere, 0o700)

        def plant_once(path, data):
            if not os.path.lexists(self.hdir):
                os.symlink(elsewhere, self.hdir)
            return real(path, data)
        with patch.object(protocol, 'write_json', side_effect=plant_once):
            self.save()
        self.assertEqual([n for n in os.listdir(elsewhere) if n.endswith('.md')], [])


if __name__ == '__main__':
    unittest.main()
