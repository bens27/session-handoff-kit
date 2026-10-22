"""Checkpoint record: the one owner of 'is this checkpoint eligible to adopt'.

Records are built from fields, not .handoffs trees, except where the question
(authoritative, live) is about the chain on disk.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_ledger as ledger
from handoff_record import Record, live, seal


def build(path='/nonexistent/x.md', **fields):
    return Record(path, Record.render(**fields))


class StatusMachine(unittest.TestCase):
    def test_missing_and_unknown_status_are_open_but_not_strictly_open(self):
        for fields in ({}, {'status': 'weird'}, {'status': 'OPEN'}):
            record = build(**fields)
            self.assertTrue(record.is_open, fields)
            self.assertFalse(record.open_strict, fields)

    def test_closed_statuses(self):
        for status in ('resumed', 'superseded', 'abandoned', 'unpublished', 'Resumed'):
            self.assertFalse(build(status=status).is_open, status)
        self.assertTrue(build(status='open').open_strict)

    def test_ledger_is_open_is_the_record_rule(self):
        self.assertIs(ledger._is_open, ledger.is_open)
        self.assertEqual(ledger._is_open({'status': 'unpublished'}), build(status='unpublished').is_open)


class Holds(unittest.TestCase):
    def test_paused_and_waiting_are_holds(self):
        self.assertFalse(build(status='open').held)
        self.assertTrue(build(automatic_paused='guard').held)
        self.assertTrue(build(waiting_input='true').held)
        self.assertEqual(build(automatic_paused='guard, cap').paused, 'guard, cap')

    def test_continuity_parses_and_fails_loud(self):
        self.assertEqual(build().continuity, [])
        items = [{'id': 'a', 'status': 'open', 'text': 't'}]
        self.assertEqual(build(continuity=json.dumps(items)).continuity, items)
        with self.assertRaises(ValueError):
            build(continuity='[not json').continuity


class SealAndPublication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = patch.dict(os.environ, {'HOME': str(base / 'home'), 'USERPROFILE': str(base / 'home')})
        env.start()
        self.addCleanup(env.stop)
        self.path = str(base / 'c.md')
        Path(self.path).write_text(Record.render(checkpoint_id='c1', status='open', topic='t'))

    def publish(self, sealed=True):
        record = Record.load(self.path)
        receipt = seal('c1', record.fingerprint) if sealed else dict(checkpoint_id='c1', fingerprint=record.fingerprint)
        Path(self.path + '.published').write_text(json.dumps(receipt))

    def test_no_receipt(self):
        record = Record.load(self.path)
        self.assertFalse(record.has_receipt())
        self.assertFalse(record.published())
        self.assertFalse(record.trusted())

    def test_unsealed_receipt_is_published_not_trusted(self):
        self.publish(sealed=False)
        record = Record.load(self.path)
        self.assertTrue(record.published())
        self.assertFalse(record.trusted())

    def test_sealed_receipt_is_trusted_and_volatile_keys_do_not_break_it(self):
        self.publish()
        ledger.claim(self.path, owner='s1')
        record = Record.load(self.path)
        self.assertEqual(record.claim_owner, 's1')
        self.assertTrue(record.published() and record.trusted())
        self.assertEqual(ledger.trusted(self.path), record.trusted())

    def test_set_fields_replaces_keys_without_breaking_the_seal(self):
        self.publish()
        Record.set_fields(self.path, waiting_input='true')
        Record.set_fields(self.path, waiting_input='later')
        record = Record.load(self.path)
        self.assertEqual(record.waiting_input, 'later')
        self.assertTrue(record.held)
        self.assertEqual(record.text.count('waiting_input:'), 1)

    def test_content_edit_breaks_publication_and_trust(self):
        self.publish()
        Path(self.path).write_text(Path(self.path).read_text() + 'edited\n')
        record = Record.load(self.path)
        self.assertFalse(record.published() or record.trusted())

    def test_rotated_key_breaks_trust_only(self):
        self.publish()
        Path(self.tmp.name, 'home/.context-watch/receipt.key').write_bytes(os.urandom(32))
        record = Record.load(self.path)
        self.assertTrue(record.published())
        self.assertFalse(record.trusted())


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root = base / 'project'
        (self.root / '.handoffs').mkdir(parents=True)
        self.alias = base / 'alias'
        self.alias.symlink_to(self.root, target_is_directory=True)

    def write(self, name, **fields):
        path = self.root / '.handoffs' / name
        path.write_text(Record.render(topic='t', **fields))
        return str(path)

    def test_newest_of_topic_is_authoritative_through_a_symlinked_root(self):
        old = self.write('20260101-0000-t.md', status='open')
        new = self.write('20260102-0000-t.md', status='open')
        for root in (str(self.root), str(self.alias)):
            self.assertTrue(Record.load(new).authoritative(root), root)
            self.assertFalse(Record.load(old).authoritative(root), root)
            self.assertTrue(live(root, new))
            self.assertFalse(live(root, old))

    def test_closed_or_missing_is_not_live(self):
        closed = self.write('20260101-0000-t.md', status='resumed')
        self.assertFalse(live(str(self.root), closed))
        self.assertFalse(live(str(self.root), str(self.root / '.handoffs/missing.md')))

    def test_unreadable_but_present_keeps_the_guard(self):
        path = self.write('20260101-0000-t.md', status='open')
        with patch.object(Record, 'load', side_effect=OSError('denied')):
            self.assertTrue(live(str(self.root), path))


if __name__ == '__main__':
    unittest.main()
