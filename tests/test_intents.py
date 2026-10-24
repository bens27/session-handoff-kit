"""The restart-intent store: one owner of layout, TTLs, states and the adopt command."""
import ast
import hashlib
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_intents as intents
import handoff_dispatch as dispatch
import restart_preflight as restart


def imported_modules(name):
    """Every module a hook file imports, at any depth (lazy imports included)."""
    tree = ast.parse((HOOKS / (name + '.py')).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


class ImportGraph(unittest.TestCase):
    def test_preflight_and_dispatch_do_not_import_each_other(self):
        self.assertNotIn('handoff_dispatch', imported_modules('restart_preflight'))
        self.assertNotIn('restart_preflight', imported_modules('handoff_dispatch'))

    def test_store_and_pointers_import_neither_caller(self):
        for name in ('handoff_intents', 'handoff_holds', 'handoff_pointers'):
            with self.subTest(name=name):
                self.assertFalse({'restart_preflight', 'handoff_dispatch'} & imported_modules(name))


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = str(Path(tmp.name).resolve())

    def test_layout_is_sha256_of_request_id_under_handoffs_restart(self):
        location = intents.location(self.root, 'req-1')
        self.assertEqual(location, os.path.join(self.root, '.handoffs', 'restart',
                                                hashlib.sha256(b'req-1').hexdigest() + '.json'))
        self.assertFalse(os.path.exists(os.path.dirname(location)))  # reads never create
        self.assertEqual(intents.location(self.root, 'req-1', create=True), location)
        self.assertTrue(os.path.isdir(os.path.dirname(location)))
        self.assertEqual(restart._location(self.root, 'req-1'), location)
        for bad in ('', '  ', None, 3):
            with self.assertRaisesRegex(ValueError, 'A nonempty restart request ID is required.'):
                intents.location(self.root, bad)

    def test_canonical_directory_for_an_aliased_root(self):
        alias = self.root + '-alias'
        os.symlink(self.root, alias)
        self.assertEqual(intents.location(alias, 'r'), intents.location(self.root, 'r'))

    def test_one_ttl_table(self):
        self.assertEqual(intents.TTL, dict(pending=7 * 86400, settled=30 * 86400, delivery_receipt=7200))
        self.assertEqual((restart.INTENT_MAX_AGE, restart.SETTLED_RETENTION),
                         (intents.TTL['pending'], intents.TTL['settled']))
        for name in ('restart_preflight', 'handoff_dispatch'):
            with self.subTest(name=name):
                self.assertNotIn('86400', (HOOKS / (name + '.py')).read_text())

    def test_expiry_applies_to_pending_only(self):
        now = 1_000_000_000
        pending = dict(state='pending', intent_at=now - intents.TTL['pending'])
        self.assertFalse(intents.expired(pending, now))
        self.assertTrue(intents.expired(dict(pending, intent_at=now - intents.TTL['pending'] - 1), now))
        self.assertTrue(intents.expired(dict(pending, intent_at='x'), now))
        self.assertTrue(intents.expired(dict(pending, intent_at=float('nan')), now))
        self.assertFalse(intents.expired(dict(state='retrieved', intent_at=0), now))
        self.assertIs(restart.intent_expired, intents.expired)

    def test_state_machine(self):
        self.assertEqual(intents.STATES, ('pending', 'refused', 'retrieved'))
        record = dict(state='pending', host_confirmed=False)
        self.assertEqual(intents.settle(dict(record), 'refused', refusal='x')['state'], 'refused')
        self.assertEqual(intents.settle(dict(record), 'retrieved', successor_session='s')['state'], 'retrieved')
        self.assertEqual(intents.settle(dict(state='refused', refusal='a'), 'refused', refusal='b')['refusal'], 'b')
        for start, end in (('refused', 'retrieved'), ('retrieved', 'refused'), ('retrieved', 'pending'),
                           ('refused', 'pending'), ('pending', 'pending'), ('pending', 'gone')):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                intents.settle(dict(state=start), end)

    def test_adopt_command_names_the_preflight_script(self):
        script = os.path.realpath(HOOKS / 'restart_preflight.py')
        expected = 'python3 %s adopt --root %s --request-id %s' % (
            shlex.quote(script), shlex.quote(self.root), shlex.quote('a b'))
        self.assertEqual(intents.adopt_command(self.root, 'a b'), expected)
        self.assertEqual(restart.adopt_command(self.root, 'a b'), expected)
        record = dict(root=self.root, request_id='a b', session='old')
        self.assertIn(expected, dispatch.successor_action(self.root, record, 'new')['command'])
        self.assertEqual(intents.handle_command('h-' + '0' * 24),
                         'python3 %s adopt h-%s' % (shlex.quote(script), '0' * 24))

    def test_entries_list_without_creating_the_directory(self):
        self.assertEqual(intents.entries(self.root), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, '.handoffs', 'restart')))
        location = intents.location(self.root, 'r', create=True)
        Path(location).write_text('{}')
        Path(location + '.lock').write_text('')
        self.assertEqual(intents.entries(self.root), [location])


if __name__ == '__main__':
    unittest.main()
