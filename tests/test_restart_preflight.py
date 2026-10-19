"""Restart safety through the public lifecycle seam with real saved checkpoints."""
import os
from unittest.mock import patch
import sys
from pathlib import Path
import tempfile
import unittest

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_protocol as protocol
import restart_preflight as restart


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name).resolve())
        (Path(self.root) / '.handoffs').mkdir()
        env = patch.dict(os.environ, {'HOME': self.root, 'USERPROFILE': self.root, 'HANDOFF_TERMINAL_ID': 'term', 'AGENTSROOM_AGENT_ID': ''})
        env.start()
        self.addCleanup(env.stop)
        self.saved = protocol.save(self.root, 'old', 'save-1', {
            'topic': 'work', 'description': 'Continue work',
            'body': '## Objective\nFix it\n## Current state\nSaved\n## Next steps\nTest it\n'})
        self.path = self.saved['path']

    def preflight(self, **kw):
        args = dict(root=self.root, path=self.path, session='old', terminal='term',
                    request_id='restart-1', workers=[], host_capable=True)
        args.update(kw)
        return restart.preflight(**args)

    def test_intent_is_durable_and_duplicate_never_dispatches_twice(self):
        self.assertEqual(self.preflight()['outcome'], 'ready')
        result = self.preflight()
        self.assertEqual(result['outcome'], 'pending')
        self.assertFalse(result['dispatch'])

    def test_failure_keeps_checkpoint_and_refuses_duplicate_dispatch(self):
        self.preflight()
        restart.record_refusal(self.root, 'restart-1', 'host unavailable')
        self.assertEqual(self.preflight()['outcome'], 'refused')
        self.assertEqual(protocol.ledger._read_fm(self.path)['status'], 'open')

    def test_interrupted_handover_is_pending_without_false_confirmation(self):
        self.preflight()
        state = restart.status(self.root, 'restart-1')
        self.assertEqual(state['state'], 'pending')
        self.assertFalse(state['host_confirmed'])
        self.assertFalse(self.preflight()['dispatch'])

    def test_active_worker_without_durable_report_blocks(self):
        self.assertEqual(self.preflight(workers=[{'id': 'worker', 'active': True}])['reason'],
                         'worker-report-not-durable')

    def test_worker_report_requires_matching_bytes(self):
        import hashlib
        report = Path(self.root) / 'worker.txt'
        report.write_text('Result and evidence')
        worker = {'id': 'worker', 'report': {'path': str(report),
                  'sha256': hashlib.sha256(report.read_bytes()).hexdigest()}}
        self.assertEqual(self.preflight(workers=[worker])['outcome'], 'ready')

    def test_absent_host_and_unknown_inventory_block(self):
        self.assertEqual(self.preflight(host_capable=False)['reason'], 'host-capability-absent')
        self.assertEqual(self.preflight(workers=None)['reason'], 'worker-inventory-unknown')

    def test_stale_identity_and_request_rebinding_block(self):
        self.assertEqual(self.preflight(session='other')['reason'], 'stale-identity')
        self.preflight()
        self.assertEqual(self.preflight(terminal='other')['reason'], 'request-identity-conflict')

    def test_hold_and_invalid_publication_block(self):
        self.assertEqual(self.preflight(hold=True)['reason'], 'human-hold')
        Path(self.path + '.published').unlink()
        self.assertEqual(self.preflight()['reason'], 'publication-not-ready')

    def test_successor_requires_actual_preparation_and_records_retrieval_only(self):
        self.preflight()
        with self.assertRaises((ValueError, OSError)):
            restart.confirm_successor(self.root, 'restart-1', 'new', 'term', 'absent')
        result = protocol.prepare(self.path, self.root, 'new')
        self.assertEqual(result['outcome'], 'retrieved', result)
        receipt = restart.confirm_successor(self.root, 'restart-1', 'new', 'term', result['delivery_receipt'])
        self.assertEqual(receipt['state'], 'retrieved')
        self.assertFalse(receipt['host_confirmed'])
        self.assertEqual(restart.confirm_successor(self.root, 'restart-1', 'new', 'term', result['delivery_receipt']), receipt)
        with self.assertRaises(ValueError):
            restart.confirm_successor(self.root, 'restart-1', 'old', 'term', result['delivery_receipt'])

    def test_waiting_input_marker_is_preserved(self):
        protocol.ledger._set_fields(self.path, [('waiting_input', 'true')], ())
        self.assertEqual(self.preflight()['reason'], 'human-hold')
        self.assertEqual(protocol.ledger._read_fm(self.path)['waiting_input'], 'true')

    def test_cli_absent_host_is_blocked_without_dispatch(self):
        import json
        import subprocess
        result = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'preflight'],
            input=json.dumps(dict(root=self.root, path=self.path, session='old', terminal='term',
                                  request_id='cli-1', workers=[])), text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout)['reason'], 'host-capability-absent')

    def test_foreign_live_claim_blocks_restart(self):
        protocol.prepare(self.path, self.root, 'foreign', execute=True)
        self.assertEqual(self.preflight()['reason'], 'checkpoint-owned-by-another-session')

    def test_delivery_before_intent_cannot_confirm_successor(self):
        delivery = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        self.preflight()
        with self.assertRaises(ValueError):
            restart.confirm_successor(self.root, 'restart-1', 'new', 'term', delivery)

    def test_malformed_worker_inventory_blocks(self):
        for workers in ({}, '', [None], [{}], [{'id': 'x', 'report': 'fake'}]):
            with self.subTest(workers=workers):
                self.assertEqual(self.preflight(workers=workers)['outcome'], 'blocked')

    def test_expired_delivery_blocks_but_confirmed_retry_remains_idempotent(self):
        self.preflight()
        with patch.object(restart.time, 'time', return_value=10000000000):
            delivery = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        with patch.object(restart.time, 'time', return_value=10000007201):
            with self.assertRaises(ValueError):
                restart.confirm_successor(self.root, 'restart-1', 'new', 'term', delivery)
        delivery = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        confirmed = restart.confirm_successor(self.root, 'restart-1', 'new', 'term', delivery)
        with patch.object(restart.time, 'time', return_value=10000007201):
            self.assertEqual(restart.confirm_successor(self.root, 'restart-1', 'new', 'term', delivery), confirmed)

    def test_cli_rejects_malformed_worker_inventory(self):
        import json
        import subprocess
        for workers in ({}, '', [None], [{'id': 'x', 'report': 'bad'}]):
            result = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'preflight'],
                input=json.dumps(dict(root=self.root, path=self.path, session='old', terminal='term',
                    request_id='cli-bad', workers=workers, host_capable=True)),
                text=True, capture_output=True)
            self.assertEqual(json.loads(result.stdout)['outcome'], 'blocked')
            self.assertFalse(json.loads(result.stdout)['dispatch'])

    def test_busy_checkpoint_or_publisher_blocks_dispatch(self):
        import json
        import subprocess
        for locked_path in (self.path, protocol.ledger.session_note_path(self.root, 'publisher')):
            with protocol.ledger.locked(locked_path):
                result = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'preflight'],
                    input=json.dumps(dict(root=self.root, path=self.path, session='old', terminal='term',
                        request_id='locked', workers=[], host_capable=True)),
                    text=True, capture_output=True, timeout=8)
            self.assertEqual(json.loads(result.stdout)['outcome'], 'blocked')
            self.assertFalse(json.loads(result.stdout)['dispatch'])

    def test_malformed_existing_intent_blocks_callable_without_replacement(self):
        import hashlib
        import json
        self.preflight()
        location = Path(self.root) / '.handoffs/restart' / (hashlib.sha256(b'restart-1').hexdigest() + '.json')
        for invalid in ({}, None, [], {'state': 'pending'}, {'state': 'bogus'}):
            with self.subTest(invalid=invalid):
                raw = json.dumps(invalid)
                location.write_text(raw)
                result = self.preflight()
                self.assertEqual(result['outcome'], 'blocked')
                self.assertFalse(result['dispatch'])
                self.assertEqual(location.read_text(), raw)
                with self.assertRaises(ValueError):
                    restart.status(self.root, 'restart-1')

    def test_malformed_existing_intent_blocks_cli_without_replacement(self):
        import hashlib
        import json
        import subprocess
        self.preflight()
        location = Path(self.root) / '.handoffs/restart' / (hashlib.sha256(b'restart-1').hexdigest() + '.json')
        for invalid in ({}, None, [], {'state': 'pending'}):
            with self.subTest(invalid=invalid):
                raw = json.dumps(invalid)
                location.write_text(raw)
                result = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'preflight'],
                    input=json.dumps(dict(root=self.root, path=self.path, session='old', terminal='term',
                        request_id='restart-1', workers=[], host_capable=True)),
                    text=True, capture_output=True)
                data = json.loads(result.stdout)
                self.assertEqual(data['outcome'], 'blocked')
                self.assertFalse(data['dispatch'])
                self.assertEqual(location.read_text(), raw)

    def test_nested_intent_uses_shared_recoverable_decoder(self):
        import hashlib
        self.preflight()
        location = Path(self.root) / '.handoffs/restart' / (hashlib.sha256(b'restart-1').hexdigest() + '.json')
        raw = '[' * 100000 + ']' * 100000
        location.write_text(raw)
        self.assertEqual(self.preflight()['reason'], 'invalid-persisted-intent')
        self.assertEqual(location.read_text(), raw)

    def test_nested_successor_cache_raises_recoverable_value_error(self):
        self.preflight()
        delivery = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        Path(protocol.receipt_path(self.root, 'new') + '.delivery').write_text('[' * 100000 + ']' * 100000)
        with self.assertRaises(ValueError):
            restart.confirm_successor(self.root, 'restart-1', 'new', 'term', delivery)

    def test_nested_cli_input_returns_structured_blocked_outcome(self):
        import json
        import subprocess
        result = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'status'],
            input='[' * 100000 + ']' * 100000, text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout)['outcome'], 'blocked')
        self.assertNotIn('Traceback', result.stderr)

    def test_malformed_successor_reference_map_is_recoverable(self):
        import json
        self.preflight()
        delivery = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        path = Path(protocol.receipt_path(self.root, 'new') + '.delivery')
        receipt = json.loads(path.read_text())
        receipt['references'] = []
        path.write_text(json.dumps(receipt))
        with self.assertRaises(ValueError):
            restart.confirm_successor(self.root, 'restart-1', 'new', 'term', delivery)
