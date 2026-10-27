"""Review observations at the pure reader and checkpoint CLI seams."""
import sys
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_review as review


class VerdictTests(unittest.TestCase):
    def test_offline_tags_require_one_exact_verdict(self):
        for tags, expected in [([], 'none'), (['review:accept'], 'accept'),
                               (['review:reject'], 'reject'),
                               (['other', 'review:accept', 'review:accept'], 'accept'),
                               (['review:accept', 'review:reject'], 'none'),
                               (['review:accepted', 'Review:accept'], 'none')]:
            with self.subTest(tags=tags):
                self.assertEqual(review.resolve_review(tags=tags)['reviewed'], expected)

    def test_explicit_native_observation_wins_even_when_pending(self):
        for verdict in ('accept', 'reject', 'none'):
            with self.subTest(verdict=verdict):
                result = review.resolve_review(native_review=verdict, tags=['review:accept'])
                self.assertEqual(result, {'reviewed': verdict, 'source': 'native'})

    def test_invalid_observations_are_not_silently_treated_as_approval(self):
        for value in (True, {}, 'done', 'approved'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                review.resolve_review(native_review=value)
        for tags in ('review:accept', {'review:accept': True}, [None]):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                review.resolve_review(tags=tags)


class TicketReviewTests(unittest.TestCase):
    def test_native_transition_precedes_ticket_tags(self):
        before = dict(id='ticket-1', status='in_review')
        after = dict(id='ticket-1', status='done', tags=['review:reject'])
        self.assertEqual(review.ticket_review(after, previous=before),
                         {'reviewed': 'accept', 'source': 'native'})
        after['status'] = 'in_progress'
        after['tags'] = ['review:accept']
        self.assertEqual(review.ticket_review(after, previous=before, reviewer_feedback_added=True),
                         {'reviewed': 'reject', 'source': 'native'})
        self.assertEqual(review.ticket_review(after, previous=before)['reviewed'], 'accept')

    def test_status_and_ids_alone_are_not_verdicts(self):
        for status in ('done', 'todo', 'in_progress', 'in_review'):
            ticket = dict(id='ticket-1', status=status, reviewerAgentId='reviewer', reviewSubmittedBy='author')
            self.assertEqual(review.ticket_review(ticket)['reviewed'], 'none')
        self.assertEqual(review.ticket_review(None, tags=['review:reject'])['reviewed'], 'reject')
        self.assertEqual(review.ticket_review(dict(id='ticket-1', status='done', tags=['review:accept']))['reviewed'], 'accept')

    def test_cross_ticket_transition_is_refused(self):
        with self.assertRaises(ValueError):
            review.ticket_review(dict(id='two', status='done'), previous=dict(id='one', status='in_review'))


class CheckpointReviewTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch
        import handoff_protocol as protocol
        self.protocol = protocol
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.handoffs').mkdir()
        env = patch.dict(os.environ, {'HOME': str(self.root), 'HANDOFF_TERMINAL_ID': '',
                                    'AGENTSROOM_AGENT_ID': '', 'CONTEXT_WATCH_ORIGIN': 'test'})
        env.start()
        self.addCleanup(env.stop)
        self.path = Path(protocol.save(str(self.root), 'author', 'save-1', {
            'topic': 'work', 'description': 'Review this work',
            'body': '## Objective\nFix it\n## Current state\nReady\n## Next steps\nContinue\n'})['path'])
        self.fingerprint = protocol.snapshot(str(self.path))

    def record(self, **observation):
        return review.record_review(self.path, fingerprint=self.fingerprint, **observation)

    def test_record_is_separate_from_checkpoint_and_survives_resume(self):
        original = self.path.read_bytes()
        self.assertEqual(review.read_review(self.path)['reviewed'], 'none')
        self.record(tags=['review:accept'], ticket_id='ticket-1')
        self.assertEqual(self.path.read_bytes(), original)
        read = review.read_review(self.path)
        self.assertEqual((read['reviewed'], read['source'], read['ticket_id']), ('accept', 'tag', 'ticket-1'))
        resumed = self.protocol.resume(str(self.root), 'successor', str(self.path))
        self.assertEqual(resumed['outcome'], 'resumed')
        self.assertEqual(resumed['reviewed'], 'accept')
        self.assertEqual(review.read_review(self.path)['reviewed'], 'accept')

    def test_reject_and_explicit_none_replace_earlier_observations(self):
        self.record(native_review='accept')
        self.record(native_review='reject', tags=['review:accept'])
        self.assertEqual(review.read_review(self.path)['reviewed'], 'reject')
        self.record(native_review='none', tags=['review:accept'])
        self.assertEqual(review.read_review(self.path)['reviewed'], 'none')

    def test_changed_checkpoint_or_corrupt_sidecar_never_reuses_verdict(self):
        self.record(tags=['review:accept'])
        original = self.path.read_bytes()
        self.path.write_bytes(original + b'Changed work\n')
        self.assertEqual(review.read_review(self.path)['reviewed'], 'none')
        with self.assertRaises(ValueError):
            self.record(native_review='accept')
        self.path.write_bytes(original)
        Path(str(self.path) + '.review').write_text('{bad JSON')
        self.assertEqual(review.read_review(self.path)['reviewed'], 'none')

    def test_read_only_preparation_exposes_verdict_and_projection_preserves_it(self):
        from handoff_contract import project_output
        self.record(tags=['review:reject'])
        retrieved = self.protocol.prepare(str(self.path), str(self.root), 'reader')
        self.assertEqual(retrieved['reviewed'], 'reject')
        self.assertEqual(project_output(retrieved, 'path,body')['reviewed'], 'reject')

    def test_replay_refreshes_review_snapshot(self):
        self.record(tags=['review:accept'])
        self.protocol.resume(str(self.root), 'successor', str(self.path))
        self.record(native_review='reject')
        replay = self.protocol.resume(str(self.root), 'successor', str(self.path))
        self.assertEqual(replay['reviewed'], 'reject')

    def test_cli_records_explicit_input_and_reads_without_mutation(self):
        import json
        import subprocess
        command = [sys.executable, str(HOOKS / 'handoff_ledger.py'), 'review', str(self.path)]
        observed = {'fingerprint': self.fingerprint, 'tags': ['review:reject'], 'ticket_id': 'ticket-1'}
        run = subprocess.run(command + ['--input', '-'], input=json.dumps(observed),
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertEqual(json.loads(run.stdout)['reviewed'], 'reject')
        sidecar = Path(str(self.path) + '.review')
        before = sidecar.read_bytes(), sidecar.stat().st_mtime_ns
        read = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout)['reviewed'], 'reject')
        self.assertEqual((sidecar.read_bytes(), sidecar.stat().st_mtime_ns), before)
        bad = subprocess.run(command + ['--input', '-'], input='{"native_review":"accept"}',
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(json.loads(bad.stdout)['outcome'], 'invalid')
        self.assertEqual(review.read_review(self.path)['reviewed'], 'reject')

    def test_malformed_or_oversized_review_metadata_is_not_an_acceptance(self):
        import json
        self.record(tags=['review:accept'])
        sidecar = Path(str(self.path) + '.review')
        valid = json.loads(sidecar.read_text())
        for changed in [dict(valid, source='none'), dict(valid, source='conflicting-tags'),
                        dict(valid, version=True), dict(valid, recorded_at='not-a-date'),
                        dict(valid, ticket_id='x' * 201), dict(valid, checkpoint_id='another')]:
            with self.subTest(changed=changed):
                sidecar.write_text(json.dumps(changed))
                self.assertEqual(review.read_review(self.path)['reviewed'], 'none')
        sidecar.write_text(json.dumps(valid) + ' ' * 17000)
        self.assertEqual(review.read_review(self.path)['reviewed'], 'none')

    def test_wrong_fingerprint_cannot_overwrite_valid_review(self):
        self.record(tags=['review:reject'])
        with self.assertRaises(ValueError):
            review.record_review(self.path, fingerprint='wrong', native_review='accept')
        self.assertEqual(review.read_review(self.path)['reviewed'], 'reject')
