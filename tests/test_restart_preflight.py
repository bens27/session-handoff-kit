"""Restart safety through the public lifecycle seam with real saved checkpoints."""
import json
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

    def test_native_host_acceptance_survives_successor_receipt(self):
        ready = self.preflight()
        event = {'timestamp': '2026-10-21T17:01:28Z', 'type': 'event_msg',
                 'payload': {'type': 'item_completed', 'thread_id': 'old',
                    'completed_at_ms': __import__('time').time() * 1000,
                    'item': {'type': 'McpToolCall', 'id': 'native-call-1',
                        'server': 'AgentsRoom-MCP', 'tool': 'agents_restart',
                        'arguments': {'prompt': ready['restart_prompt']}, 'status': 'completed',
                        'result': {'isError': False, 'content': [{'type': 'text', 'text': json.dumps({
                            'ok': True, 'scheduled': True, 'agent': {'id': 'term'}})}]}}}}
        transcript = Path(self.root) / '.agentsroom/codex/profile/sessions/rollout-old.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(event) + '\n')
        accepted = restart.record_acceptance(self.root, 'restart-1', str(transcript))
        self.assertTrue(accepted['host_confirmed'])
        self.assertEqual(accepted['state'], 'pending')
        import copy
        for proof in (dict(accepted['host_acceptance'], transcript='/nonexistent/anything'),
                      dict(accepted['host_acceptance'], native_line_sha256=None),
                      {'source': 'codex-native-transcript', 'request_id': 'restart-1'},
                      dict(accepted['host_acceptance'], event_sha256='0' * 64)):
            tampered = dict(accepted, host_acceptance=proof)
            protocol.write_json(restart._location(self.root, 'restart-1'), tampered)
            with self.assertRaises(ValueError):
                restart.status(self.root, 'restart-1')
        protocol.write_json(restart._location(self.root, 'restart-1'), accepted)
        with self.assertRaises(ValueError):
            restart.record_refusal(self.root, 'restart-1', 'cannot erase host scheduling')
        receipt = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        successor = restart.confirm_successor(self.root, 'restart-1', 'new', 'term', receipt)
        self.assertTrue(successor['host_confirmed'])
        self.assertEqual(restart.status(self.root, 'restart-1')['state'], 'retrieved')
        import copy
        for field, value in (('thread_id', 'foreign'), ('prompt', 'different prompt'),
                             ('terminal', 'foreign'), ('completed_at_ms', 0), ('server', 'other')):
            altered = copy.deepcopy(event)
            if field in ('thread_id', 'completed_at_ms'):
                altered['payload'][field] = value
            elif field == 'prompt':
                altered['payload']['item']['arguments']['prompt'] = value
            elif field == 'terminal':
                altered['payload']['item']['result']['content'][0]['text'] = json.dumps({
                    'ok': True, 'scheduled': True, 'agent': {'id': value}})
            else:
                altered['payload']['item'][field] = value
            transcript.write_text(json.dumps(altered) + '\n')
            with self.subTest(field=field), self.assertRaises(ValueError):
                restart.record_acceptance(self.root, 'restart-1', str(transcript))
        event['payload']['item']['result']['content'][0]['text'] = json.dumps({'ok': True, 'scheduled': False})
        transcript.write_text(json.dumps(event) + '\n')
        with self.assertRaises(ValueError):
            restart.record_acceptance(self.root, 'restart-1', str(transcript))
        self.assertTrue(restart.status(self.root, 'restart-1')['host_confirmed'])

    def test_acceptance_rejects_unconfined_symlink_fifo_and_expired_evidence(self):
        import time
        ready = self.preflight()
        event = dict(type='event_msg', payload=dict(type='item_completed', thread_id='old',
            completed_at_ms=time.time() * 1000, item=dict(type='McpToolCall',
            server='AgentsRoom-MCP', tool='agents_restart', status='completed',
            arguments=dict(prompt=ready['restart_prompt']), result=dict(isError=False,
            content=[dict(type='text', text=json.dumps(dict(ok=True, scheduled=True,
                                                         agent=dict(id='term'))))]))))
        outside = Path(self.root) / 'rollout-old.jsonl'
        outside.write_text(json.dumps(event) + '\n')
        root = Path(self.root) / '.agentsroom/codex/profile/sessions'
        root.mkdir(parents=True)
        linked = root / 'linked-old.jsonl'
        linked.symlink_to(outside)
        fifo = root / 'fifo-old.jsonl'
        os.mkfifo(fifo)
        wrong = root / 'foreign.jsonl'
        wrong.write_text(outside.read_text())
        for path in (outside, linked, fifo, wrong):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                restart.record_acceptance(self.root, 'restart-1', path)
        large = root / 'large-old.jsonl'
        with large.open('wb') as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            restart.record_acceptance(self.root, 'restart-1', large)
        oversized_line = root / 'line-old.jsonl'
        oversized_line.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            restart.record_acceptance(self.root, 'restart-1', oversized_line)
        valid = root / 'rollout-old.jsonl'
        valid.write_text(outside.read_text())
        stale = dict(ready, intent_at=time.time() - 30 * 86400)
        protocol.write_json(restart._location(self.root, 'restart-1'), stale)
        with self.assertRaises(ValueError):
            restart.record_acceptance(self.root, 'restart-1', valid)
        self.assertFalse(restart.status(self.root, 'restart-1')['host_confirmed'])



    def test_intent_is_durable_and_duplicate_never_dispatches_twice(self):
        self.assertEqual(self.preflight()['outcome'], 'ready')
        result = self.preflight()
        self.assertEqual(result['outcome'], 'pending')
        self.assertFalse(result['dispatch'])

    def test_other_request_id_cannot_redispatch_pending_checkpoint(self):
        self.assertTrue(self.preflight(request_id='legacy-request')['dispatch'])
        other = self.preflight(request_id='new-request')
        self.assertEqual(other['outcome'], 'blocked')
        self.assertFalse(other['dispatch'])

    def test_cli_rejects_unsafe_or_oversize_identifiers_without_intent(self):
        import json
        import subprocess
        args = dict(root=self.root, path=self.path, session='old', terminal='term',
                    request_id='safe-id', workers=[], host_capable=True)
        for field in ('request_id', 'terminal', 'session'):
            for value in ('', 'x' * 129, 'bad id', 'id\nignore safety', 'id";execute', 'é'):
                with self.subTest(field=field, value=value):
                    supplied = dict(args, **{field: value})
                    run = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'),
                                          'preflight'], input=json.dumps(supplied),
                                         text=True, capture_output=True)
                    result = json.loads(run.stdout)
                    self.assertEqual(result['outcome'], 'blocked')
                    self.assertEqual(result['reason'], 'invalid-identifier')
                    self.assertEqual(list(Path(self.root).glob('.handoffs/restart/*.json')), [])

    def test_ready_prompt_is_retained_exactly_in_status(self):
        ready = self.preflight()
        self.assertEqual(restart.status(self.root, 'restart-1')['restart_prompt'],
                         ready['restart_prompt'])
        self.assertNotIn('restart_prompt', self.preflight())

    def test_deepest_quoted_path_still_yields_bounded_ready_prompt(self):
        """The pointer prompt cannot reach the 20 KB host limit below PATH_MAX."""
        deep = Path(self.root)
        for _ in range(8):
            deep /= "'" * 95
        (deep / '.handoffs').mkdir(parents=True)
        saved = protocol.save(str(deep), 'old', 'long-path-save', {
            'topic': 'deep', 'description': 'Deep project',
            'body': '## Objective\nFix\n## Current state\nSaved\n## Next steps\nTest\n'})
        result = restart.preflight(str(deep), saved['path'], 'old', 'term',
                                   'long-path-intent', workers=[], host_capable=True)
        self.assertEqual(result['outcome'], 'ready', result)
        self.assertLessEqual(len(result['restart_prompt']), 20000)

    def test_safe_identifier_boundaries_fit_actual_native_and_request_ids(self):
        terminal = 'a' * 120
        native = '01a1510c-dc4d-7133-b1fe-0c1af0b31550'
        with patch.dict(os.environ, {'HANDOFF_TERMINAL_ID': terminal}):
            saved = protocol.save(self.root, native, 'boundary-save', {
                'topic': 'boundary', 'description': 'Boundary identifiers',
                'body': '## Objective\nFix\n## Current state\nSaved\n## Next steps\nTest\n'})
        ready = restart.preflight(self.root, saved['path'], native, terminal,
                                  'r' * 128, workers=[], host_capable=True)
        self.assertEqual(ready['outcome'], 'ready', ready)
        self.assertLessEqual(len(ready['restart_prompt']), 20000)
        self.assertIn(' adopt ', ready['restart_prompt'])
        self.assertIn('"request_id":"%s"' % ('r' * 128), ready['restart_prompt'])

    def test_other_pending_or_invalid_legacy_intent_blocks_new_checkpoint(self):
        import hashlib
        self.preflight(request_id='legacy-unstructured-id')
        saved = protocol.save(self.root, 'old', 'save-other', {
            'topic': 'other', 'description': 'Other checkpoint',
            'body': '## Objective\nOther\n## Current state\nSaved\n## Next steps\nTest\n'})
        result = self.preflight(path=saved['path'], request_id='next-id')
        self.assertEqual(result['reason'], 'conflicting-restart-intent')
        location = Path(self.root) / '.handoffs/restart' / (
            hashlib.sha256(b'legacy-unstructured-id').hexdigest() + '.json')
        location.write_text('{"state":"pending"}')
        result = self.preflight(path=saved['path'], request_id='next-id')
        self.assertEqual(result['reason'], 'invalid-persisted-intent')
        self.assertEqual(location.read_text(), '{"state":"pending"}')

    def test_retrieved_checkpoint_cannot_dispatch_under_new_id(self):
        self.preflight(request_id='legacy-retrieved')
        delivery = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        restart.confirm_successor(self.root, 'legacy-retrieved', 'new', 'term', delivery)
        self.assertEqual(self.preflight(request_id='another-id')['reason'],
                         'conflicting-restart-intent')

    def test_definitive_refusal_requires_new_checkpoint_not_new_request_name(self):
        self.preflight()
        restart.record_refusal(self.root, 'restart-1', 'Host explicitly refused; no restart scheduled')
        self.assertEqual(self.preflight(request_id='bypass')['reason'],
                         'conflicting-restart-intent')
        saved = protocol.save(self.root, 'old', 'after-refusal', {
            'topic': 'refusal-recovery', 'description': 'Explicit recovery after refusal',
            'body': '## Objective\nRecover\n## Current state\nDefinitive refusal\n## Next steps\nTest\n'})
        self.assertTrue(self.preflight(path=saved['path'], request_id='new-checkpoint')['dispatch'])

    def test_cross_checkpoint_race_with_distinct_tmpdirs_dispatches_once(self):
        import json
        import subprocess
        other = protocol.save(self.root, 'old', 'race-save', {
            'topic': 'race-other', 'description': 'Concurrent checkpoint',
            'body': '## Objective\nRace\n## Current state\nSaved\n## Next steps\nTest\n'})['path']
        gate = Path(self.root) / 'start-gate'
        script = """import pathlib, runpy, sys, time
print('READY', flush=True)
while not pathlib.Path(sys.argv[1]).exists():
    time.sleep(0.005)
sys.path.insert(0, sys.argv[2])
sys.argv = [sys.argv[2] + '/restart_preflight.py', 'preflight', '--input', sys.argv[3]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        children = []
        try:
            for index in range(6):
                tmp = Path(self.root) / ('tmp-' + str(index))
                tmp.mkdir()
                data = Path(self.root) / ('input-' + str(index) + '.json')
                data.write_text(json.dumps(dict(root=self.root, path=(self.path if index % 2 else other),
                    session='old', terminal='term', request_id='race-' + str(index),
                    workers=[], host_capable=True)))
                child = subprocess.Popen([sys.executable, '-B', '-c', script, str(gate),
                    str(HOOKS), str(data)], env=dict(os.environ, TMPDIR=str(tmp)),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(child)
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), 'READY')
            gate.touch()
            results = []
            for child in children:
                out, err = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, err + out)
                results.append(json.loads(out))
            self.assertEqual(sum(result['dispatch'] for result in results), 1, results)
            self.assertEqual(sum(result['outcome'] == 'blocked' for result in results), 5)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_pre_prompt_legacy_pending_state_cannot_be_bypassed(self):
        import hashlib
        import json
        self.preflight()
        record = restart.status(self.root, 'restart-1')
        record.pop('restart_prompt', None)
        record['request_id'] = 'legacy ID before validation'
        directory = Path(self.root) / '.handoffs/restart'
        (directory / (hashlib.sha256(b'restart-1').hexdigest() + '.json')).unlink()
        (directory / (hashlib.sha256(record['request_id'].encode()).hexdigest() + '.json')).write_text(json.dumps(record))
        self.assertEqual(self.preflight(request_id='modern-id')['reason'],
                         'conflicting-restart-intent')

    def test_expired_intent_on_this_terminal_does_not_block_a_new_checkpoint(self):
        """F1c: an intent past INTENT_MAX_AGE can never be adopted, so it must not jam the
        terminal; it still blocks its own checkpoint and stays preserved."""
        import json, time
        self.assertEqual(self.preflight()['outcome'], 'ready')
        location = restart._location(self.root, 'restart-1')
        record = json.loads(Path(location).read_text())
        record['intent_at'] = time.time() - 30 * 86400
        protocol.write_json(location, record)
        self.assertEqual(self.preflight(request_id='restart-2')['reason'], 'conflicting-restart-intent')
        fresh = protocol.save(self.root, 'old', 'save-2', {
            'topic': 'other', 'description': 'Continue other',
            'body': '## Objective\nOther\n## Current state\nSaved\n## Next steps\nGo\n'})['path']
        ready = self.preflight(path=fresh, request_id='restart-2')
        self.assertEqual(ready['outcome'], 'ready', ready)
        self.assertEqual(restart.status(self.root, 'restart-1')['state'], 'pending')

    def test_settled_intents_are_pruned_only_when_old_and_their_checkpoint_is_done(self):
        """310304b8 item 1: retrieved/refused intents are retained while they still guard an
        open checkpoint or are recent, and pruned by the next preflight once both are over."""
        import time
        self.preflight()
        prepared = protocol.prepare(self.path, self.root, 'new')
        restart.confirm_successor(self.root, 'restart-1', 'new', 'term', prepared['delivery_receipt'])
        location = Path(restart._location(self.root, 'restart-1'))

        def age(days):
            record = json.loads(location.read_text())
            record['intent_at'] = time.time() - days * 86400
            protocol.write_json(str(location), record)

        age(restart.SETTLED_RETENTION / 86400 + 1)
        # Old, but its checkpoint is still open and authoritative: kept, still guards it
        self.assertEqual(self.preflight(request_id='restart-2')['reason'], 'conflicting-restart-intent')
        self.assertTrue(location.exists())
        successor = protocol.save(self.root, 'old', 'save-2', {
            'topic': 'work', 'description': 'Continue work', 'predecessor': self.path,
            'body': '## Objective\nFix it\n## Current state\nLater\n## Next steps\nGo\n'})['path']
        age(1)  # checkpoint superseded, but the intent is recent: kept
        self.assertEqual(self.preflight(path=successor, request_id='restart-2')['outcome'], 'ready')
        self.assertTrue(location.exists())
        age(restart.SETTLED_RETENTION / 86400 + 1)
        self.assertEqual(self.preflight(path=successor, request_id='restart-2')['outcome'], 'pending')  # replay
        self.assertFalse(location.exists())
        self.assertIsNone(restart.status(self.root, 'restart-1'))
        self.assertEqual(restart.status(self.root, 'restart-2')['state'], 'pending')  # pending never pruned

    def _settle_and_age(self, request_id='restart-1'):
        import time
        self.preflight(request_id=request_id)
        prepared = protocol.prepare(self.path, self.root, 'new')
        restart.confirm_successor(self.root, request_id, 'new', 'term', prepared['delivery_receipt'])
        location = Path(restart._location(self.root, request_id))
        record = json.loads(location.read_text())
        record['intent_at'] = time.time() - restart.SETTLED_RETENTION - 86400
        protocol.write_json(str(location), record)
        return location

    def test_symlinked_root_keeps_the_intent_of_an_open_checkpoint(self):
        """Lane E M1: a root reached through a symlink still sees its open checkpoint as live,
        so an old settled intent keeps guarding it instead of being pruned."""
        location = self._settle_and_age()
        link = Path(self.tmp.name).resolve().parent / ('link-' + Path(self.root).name)
        os.symlink(self.root, link)
        self.addCleanup(os.unlink, link)
        self.assertTrue(restart._checkpoint_live(str(link), self.path))
        self.assertEqual(restart.prune_settled(str(link)), [])
        self.assertTrue(location.exists())
        self.assertEqual(self.preflight(root=str(link), request_id='restart-2')['reason'],
                         'conflicting-restart-intent')

    def test_preflight_never_prunes_the_intent_it_is_replaying(self):
        """Lane E L2: the location preflight holds is skipped, even when it is old and done."""
        location = self._settle_and_age()
        protocol.save(self.root, 'old', 'save-2', {
            'topic': 'work', 'description': 'Continue work', 'predecessor': self.path,
            'body': '## Objective\nFix it\n## Current state\nLater\n## Next steps\nGo\n'})
        self.assertFalse(restart._checkpoint_live(self.root, self.path))
        self.assertEqual(self.preflight()['outcome'], 'retrieved')  # replayed, not re-created
        self.assertTrue(location.exists())
        self.assertEqual(restart.prune_settled(self.root), ['restart-1'])
        self.assertFalse(location.exists())
        self.assertFalse(Path(str(location) + '.lock').exists())  # L3: no orphan sidecar

    def test_prune_skips_a_busy_record_without_waiting(self):
        """Lane E L1: prune runs under the registry lock, so it never waits on a record lock."""
        import fcntl
        import time
        location = self._settle_and_age()
        protocol.save(self.root, 'old', 'save-2', {
            'topic': 'work', 'description': 'Continue work', 'predecessor': self.path,
            'body': '## Objective\nFix it\n## Current state\nLater\n## Next steps\nGo\n'})
        with open(str(location) + '.lock', 'a') as busy:
            fcntl.flock(busy, fcntl.LOCK_EX)
            started = time.monotonic()
            self.assertEqual(restart.prune_settled(self.root), [])
            self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(location.exists())
        self.assertEqual(restart.prune_settled(self.root), ['restart-1'])

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

    def test_prompt_over_host_limit_blocks_without_persisting_intent(self):
        with patch.object(restart, 'binding_command', return_value='x' * 20001):
            self.assertEqual(self.preflight()['reason'], 'restart-prompt-too-large')
        self.assertIsNone(restart.status(self.root, 'restart-1'))

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

    def test_confirmed_delivery_receipt_stays_verifiable_after_resume_rewrites_it(self):
        """310304b8 item 2: the receipt proven at confirm time is kept on the intent, so a
        later resume (which rewrites the live .delivery file) does not erase the evidence."""
        import hashlib
        self.preflight()
        prepared = protocol.prepare(self.path, self.root, 'new')
        token = prepared['delivery_receipt']
        restart.confirm_successor(self.root, 'restart-1', 'new', 'term', token)
        resumed = protocol.resume(self.root, 'new', self.path)
        self.assertEqual(resumed['outcome'], 'resumed', resumed)
        live = protocol.load_state_json(protocol.receipt_path(self.root, 'new') + '.delivery')
        self.assertNotEqual(live['token'], token)  # the live receipt moved on
        kept = restart.status(self.root, 'restart-1')['retrieval_receipt']
        self.assertEqual(kept['token_sha256'], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(kept['path'], self.path)
        self.assertNotIn(token, json.dumps(restart.status(self.root, 'restart-1')))

    def test_successor_terminal_comes_from_the_environment_not_stdin(self):
        """H1: a caller claiming the intent's terminal from elsewhere is refused."""
        self.preflight()
        result = protocol.prepare(self.path, self.root, 'new')
        for env in ({'HANDOFF_TERMINAL_ID': 'other'}, {'HANDOFF_TERMINAL_ID': ''}):
            with patch.dict(os.environ, env), self.assertRaises(ValueError):
                restart.confirm_successor(self.root, 'restart-1', 'new', 'term', result['delivery_receipt'])
        with self.assertRaises(ValueError):
            restart.confirm_successor(self.root, 'restart-1', 'new', 'other', result['delivery_receipt'])
        self.assertEqual(restart.status(self.root, 'restart-1')['state'], 'pending')
        self.assertEqual(restart.confirm_successor(self.root, 'restart-1', 'new', None, result['delivery_receipt'])['state'],
                         'retrieved')

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

    def test_cli_ready_delivers_exact_successor_prompt_but_pending_cannot_redispatch(self):
        import json
        import subprocess
        args = dict(root=self.root, path=self.path, session='old', terminal='term',
                    request_id='cli-prompt', workers=[], host_capable=True)
        command = [sys.executable, str(HOOKS / 'restart_preflight.py'), 'preflight']
        ready = subprocess.run(command, input=json.dumps(args), text=True, capture_output=True)
        self.assertEqual(ready.returncode, 0, ready.stderr)
        result = json.loads(ready.stdout)
        self.assertTrue(result['dispatch'])
        prompt = result['restart_prompt']
        for required in (self.path, self.root, result['checkpoint_id'], '"cli-prompt"', '"old"', '"term"',
                         ' adopt \'{'):
            self.assertIn(required, prompt)
        pending = subprocess.run(command, input=json.dumps(args), text=True, capture_output=True)
        repeat = json.loads(pending.stdout)
        self.assertEqual(repeat['outcome'], 'pending')
        self.assertFalse(repeat['dispatch'])
        self.assertNotIn('restart_prompt', repeat)

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


class AdoptTests(unittest.TestCase):
    """Item 1+2: one successor command retrieves, records receipt and resumes."""
    BODY = '## Objective\nFix it\n## Current state\nSaved UNIQUE-BODY-MARKER\n## Next steps\nTest it\n'

    def setUp(self):
        import json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name).resolve())
        (Path(self.root) / '.handoffs').mkdir()
        self.env = dict(os.environ, HOME=self.root, USERPROFILE=self.root, TMPDIR=self.root,
                        HANDOFF_TERMINAL_ID='term', AGENTSROOM_AGENT_ID='', CODEX_THREAD_ID='',
                        HANDOFF_SESSION_ID='')
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, 'tempdir', None)
        self.path = protocol.save(self.root, 'old', 'save-1', {
            'topic': 'work', 'description': 'Continue work', 'body': self.BODY})['path']
        ready = restart.preflight(self.root, self.path, 'old', 'term', 'req-1',
                                  workers=[], host_capable=True)
        self.assertEqual(ready['outcome'], 'ready', ready)
        self.prompt = ready['restart_prompt']
        self.json = json

    def pointer(self, session, terminal='term'):
        import hashlib, time
        key = self.root + '\0' + terminal
        name = 'context-watch-terminal-%s.json' % hashlib.sha1(key.encode()).hexdigest()[:20]
        (Path(self.root) / name).write_text(self.json.dumps(dict(session_id=session, ts=time.time())))

    def adopt(self, request_id='req-1', **env):
        import subprocess
        run = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'adopt',
                              '--root', self.root, '--request-id', request_id],
                             env=dict(self.env, **env), text=True, capture_output=True)
        return run.returncode, run.stdout, self.json.loads(run.stdout)




    def test_adopt_cli_automatically_imports_native_acceptance(self):
        import subprocess, time
        reply = {'ok': True, 'scheduled': True, 'agent': {'id': 'term'}}
        event = {'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'thread_id': 'old', 'completed_at_ms': time.time() * 1000,
            'item': {'type': 'McpToolCall', 'server': 'AgentsRoom-MCP', 'tool': 'agents_restart',
                     'status': 'completed', 'arguments': {'prompt': self.prompt},
                     'result': {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(reply)}]}}}}
        transcript = Path(self.root) / '.agentsroom/codex/profile/sessions/2026/rollout-old.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(event) + '\n')
        self.pointer('new')
        _, _, adopted = self.adopt()
        self.assertEqual(adopted['outcome'], 'resumed')
        self.assertTrue(adopted['restart']['host_confirmed'])
        self.assertIn('acceptance/scheduling', adopted['report'])

    def test_adopt_automatically_imports_claude_tool_pair(self):
        import time
        from datetime import datetime, timezone
        call = dict(type='assistant', sessionId='old', message=dict(role='assistant', content=[
            dict(type='tool_use', id='restart-call', name='mcp__AgentsRoom-MCP__agents_restart',
                 input=dict(prompt=self.prompt))]))
        reply = dict(type='user', sessionId='old',
            timestamp=datetime.now(timezone.utc).isoformat(),
            message=dict(role='user', content=[dict(type='tool_result', tool_use_id='restart-call',
                content=[dict(type='text', text=json.dumps(dict(ok=True, scheduled=True,
                            agent=dict(id='term'))))])]))
        transcript = Path(self.root) / '.claude/projects/project/old.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(call) + '\n' + json.dumps(reply) + '\n')
        self.pointer('new')
        _, _, adopted = self.adopt()
        self.assertEqual(adopted['outcome'], 'resumed')
        self.assertTrue(adopted['restart']['host_confirmed'])
        self.assertEqual(restart.status(self.root, 'req-1')['host_acceptance']['source'],
                         'claude-native-transcript')

    def test_adopt_delivers_body_once_and_records_retrieval_without_prompt_echo(self):
        self.pointer('new')
        code, raw, out = self.adopt()
        self.assertEqual((code, out['outcome']), (0, 'resumed'), out)
        self.assertEqual(raw.count('UNIQUE-BODY-MARKER'), 1)
        self.assertNotIn(self.prompt, raw)
        self.assertNotIn('restart_prompt', out['restart'])
        self.assertEqual(out['restart'], dict(
            request_id='req-1', predecessor_session='old', successor_session='new',
            terminal='term', state='retrieved', host_confirmed=False))
        self.assertEqual(restart.status(self.root, 'req-1')['successor_session'], 'new')

    def test_adopt_refuses_unrecorded_foreign_or_predecessor_identity_and_preserves_state(self):
        cases = [
            ('environment-only session', None, dict(HANDOFF_SESSION_ID='new')),
            ('predecessor session', 'old', {}),
            ('other terminal', 'new', dict(HANDOFF_TERMINAL_ID='elsewhere')),
        ]
        for label, session, env in cases:
            with self.subTest(label):
                for leftover in Path(self.root).glob('context-watch-terminal-*.json'):
                    leftover.unlink()
                if session:
                    self.pointer(session, env.get('HANDOFF_TERMINAL_ID', 'term'))
                code, raw, out = self.adopt(**env)
                self.assertEqual((code, out['outcome']), (1, 'blocked'), out)
                self.assertNotIn('UNIQUE-BODY-MARKER', raw)
                self.assertEqual(restart.status(self.root, 'req-1')['state'], 'pending')
                self.assertNotIn('claim_owner', Path(self.path).read_text())

    def test_adopt_rerun_by_same_successor_is_idempotent_but_another_successor_is_refused(self):
        self.pointer('new')
        self.assertEqual(self.adopt()[2]['outcome'], 'resumed')
        code, raw, again = self.adopt()
        self.assertEqual((code, again['outcome'], again['restart']['state']), (0, 'resumed', 'retrieved'), again)
        self.assertEqual(raw.count('UNIQUE-BODY-MARKER'), 1)
        self.pointer('third')
        code, raw, other = self.adopt()
        self.assertEqual((code, other['outcome'], other['stage']), (1, 'blocked', 'successor'), other)
        self.assertNotIn('UNIQUE-BODY-MARKER', raw)
        self.assertEqual(restart.status(self.root, 'req-1')['successor_session'], 'new')

    def test_adopt_reports_resume_failure_as_blocked_with_stage_and_restart(self):
        self.pointer('new')
        self.assertEqual(self.adopt()[2]['outcome'], 'resumed')
        moved = Path(self.root) / 'moved.md'
        Path(self.path).rename(moved)
        code, raw, out = self.adopt()
        self.assertEqual((code, out['outcome'], out['stage']), (1, 'blocked', 'resume'), out)
        self.assertEqual(out['restart']['successor_session'], 'new')
        self.assertFalse(out['host_confirmed'])

    def test_adopt_honours_dispatcher_hold_after_recording_retrieval(self):
        import handoff_dispatch as dispatch
        dispatch.set_hold(self.root, 'project', 'Ben asked to wait')
        self.pointer('new')
        code, raw, out = self.adopt()
        self.assertEqual((code, out['outcome'], out['stage']), (1, 'held', 'hold'), out)
        self.assertNotIn('UNIQUE-BODY-MARKER', raw)
        self.assertEqual(restart.status(self.root, 'req-1')['state'], 'retrieved')
        self.assertNotIn('claim_owner', Path(self.path).read_text())
        dispatch.set_hold(self.root, 'project')
        code, raw, out = self.adopt()
        self.assertEqual((code, out['outcome']), (0, 'resumed'), out)

    def test_adopt_refuses_expired_pending_intent_and_preserves_it(self):
        import time
        location = restart._location(self.root, 'req-1')
        record = self.json.loads(Path(location).read_text())
        record['intent_at'] = time.time() - restart.INTENT_MAX_AGE - 60
        protocol.write_json(location, record)
        self.pointer('new')
        code, raw, out = self.adopt()
        self.assertEqual((code, out['outcome'], out['stage'], out['reason']),
                         (1, 'blocked', 'intent', 'restart-intent-expired'), out)
        self.assertNotIn('UNIQUE-BODY-MARKER', raw)
        self.assertEqual(restart.status(self.root, 'req-1')['state'], 'pending')

    def test_stepwise_successor_refuses_expired_pending_intent_like_adopt(self):
        """F1: the stepwise prepare -> successor path honours the same intent age as adopt."""
        import time
        location = restart._location(self.root, 'req-1')
        record = self.json.loads(Path(location).read_text())
        record['intent_at'] = time.time() - 30 * 86400
        protocol.write_json(location, record)
        prepared = protocol.prepare(self.path, self.root, 'new')
        self.assertEqual(prepared['outcome'], 'retrieved', prepared)
        with self.assertRaisesRegex(ValueError, 'expired'):
            restart.confirm_successor(self.root, 'req-1', 'new', 'term', prepared['delivery_receipt'])
        self.assertEqual(restart.status(self.root, 'req-1')['state'], 'pending')
        self.pointer('new')
        code, raw, out = self.adopt()
        self.assertEqual((code, out['outcome'], out['stage'], out['reason']),
                         (1, 'blocked', 'intent', 'restart-intent-expired'), out)
        self.assertEqual(restart.status(self.root, 'req-1')['state'], 'pending')

    def test_restart_prompt_is_one_sentence_whose_command_carries_the_binding(self):
        import shlex
        record = restart.status(self.root, 'req-1')
        binding = {k: record[k] for k in ('checkpoint_id', 'path', 'request_id', 'root', 'session', 'terminal')}
        compact = self.json.dumps(binding, sort_keys=True, separators=(',', ':'))
        command = 'python3 %s adopt %s' % (
            shlex.quote(os.path.realpath(HOOKS / 'restart_preflight.py')), shlex.quote(compact))
        self.assertEqual(self.prompt, 'Continue the handoff: run exactly ' + command)
        self.assertEqual(self.prompt.count(self.path), 1)
        self.assertEqual(self.prompt.count('python3 '), 1)

    def test_adopt_by_binding_resumes_and_reports_identity_apart_from_host_acceptance(self):
        import shlex, subprocess
        self.pointer('new')
        argv = shlex.split(self.prompt[len('Continue the handoff: run exactly '):])
        argv[0] = sys.executable
        run = subprocess.run(argv, env=self.env, text=True, capture_output=True)
        out = self.json.loads(run.stdout)
        self.assertEqual((run.returncode, out['outcome']), (0, 'resumed'), out)
        self.assertEqual(run.stdout.count('UNIQUE-BODY-MARKER'), 1)
        self.assertIn('old -> new', out['report'])
        self.assertIn('host_confirmed=false', out['report'])

    def test_adopt_refuses_binding_that_differs_from_recorded_intent(self):
        import subprocess
        self.pointer('new')
        record = restart.status(self.root, 'req-1')
        binding = {k: record[k] for k in ('checkpoint_id', 'path', 'request_id', 'root', 'session', 'terminal')}
        for field, value in (('checkpoint_id', 'f' * 64), ('path', self.path + '.other'),
                             ('session', 'someone'), ('terminal', 'elsewhere')):
            with self.subTest(field):
                bad = dict(binding, **{field: value})
                run = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'adopt',
                                      self.json.dumps(bad)], env=self.env, text=True, capture_output=True)
                out = self.json.loads(run.stdout)
                self.assertEqual((run.returncode, out['outcome'], out['stage']), (1, 'blocked', 'binding'), out)
                self.assertNotIn('UNIQUE-BODY-MARKER', run.stdout)
                self.assertEqual(restart.status(self.root, 'req-1')['state'], 'pending')


class RestartCommandTests(unittest.TestCase):
    """Item 5: one predecessor command observes, persists intent and returns the prompt."""

    def setUp(self):
        import json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name).resolve())
        (Path(self.root) / '.handoffs').mkdir()
        self.env = dict(os.environ, HOME=self.root, USERPROFILE=self.root, TMPDIR=self.root,
                        HANDOFF_TERMINAL_ID='term', AGENTSROOM_AGENT_ID='', CODEX_THREAD_ID='',
                        HANDOFF_SESSION_ID='')
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.json = json
        self.path = protocol.save(self.root, 'old', 'save-1', {
            'topic': 'work', 'description': 'Continue work', 'body': self.BODY})['path']
        self.pointer('old')

    BODY = AdoptTests.BODY
    pointer = AdoptTests.pointer

    def restart_cli(self, *args, **env):
        import subprocess
        run = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'restart',
                              '--root', self.root] + list(args),
                             env=dict(self.env, **env), text=True, capture_output=True)
        return run.returncode, self.json.loads(run.stdout)

    def test_restart_persists_observations_and_returns_prompt_once(self):
        import hashlib
        checkpoint = protocol.ledger._read_fm(self.path)['checkpoint_id']
        expected_id = 'agentsroom:' + hashlib.sha256(('term\0' + checkpoint).encode()).hexdigest()
        code, out = self.restart_cli('--workers', '[]', '--host-capable')
        self.assertEqual((code, out['outcome'], out['dispatch']), (0, 'ready', True), out)
        self.assertEqual((out['request_id'], out['call'], out['agent']), (expected_id, 'agents_restart', 'term'))
        self.assertIn('"request_id":"%s"' % expected_id, out['restart_prompt'])
        self.assertIn(' adopt ', out['restart_prompt'])
        observations = list(Path(self.root).glob('.handoffs/restart/*.observations'))
        self.assertEqual(len(observations), 1)
        saved = self.json.loads(observations[0].read_text())
        self.assertEqual((saved['session'], saved['identity_source'], saved['workers']), ('old', 'hook', []))
        code, again = self.restart_cli('--workers', '[]', '--host-capable')
        self.assertEqual((code, again['outcome'], again['dispatch']), (1, 'pending', False), again)
        self.assertNotIn('restart_prompt', again)

    def test_observations_are_written_only_with_a_new_ready_intent(self):
        import hashlib
        report = Path(self.root) / 'w2.txt'
        report.write_text('w2 result')
        w2 = self.json.dumps([{'id': 'w2', 'report': {'path': str(report),
                                'sha256': hashlib.sha256(report.read_bytes()).hexdigest()}}])
        observations = lambda: list(Path(self.root).glob('.handoffs/restart/*.observations'))
        self.assertEqual(self.restart_cli('--workers', '[]')[1]['reason'], 'host-capability-absent')
        self.assertEqual(observations(), [])
        self.assertEqual(self.restart_cli('--workers', '[]', '--host-capable')[1]['outcome'], 'ready')
        self.assertEqual(self.restart_cli('--workers', w2, '--host-capable')[1]['outcome'], 'pending')
        self.assertEqual(self.json.loads(observations()[0].read_text())['workers'], [])

    def test_rerun_after_host_refusal_says_a_retry_needs_a_new_save(self):
        code, out = self.restart_cli('--workers', '[]', '--host-capable')
        restart.record_refusal(self.root, out['request_id'], 'Host refused; no restart scheduled')
        code, again = self.restart_cli('--workers', '[]', '--host-capable')
        self.assertEqual((code, again['outcome'], again['dispatch']), (1, 'refused', False), again)
        self.assertIn('new save', again['action'])

    def test_restart_blocks_without_intent_on_unknown_inventory_hold_ambiguity_or_weak_identity(self):
        import handoff_dispatch as dispatch
        intents = lambda: list(Path(self.root).glob('.handoffs/restart/*.json'))
        code, out = self.restart_cli('--host-capable')
        self.assertEqual((code, out['reason']), (1, 'worker-inventory-unknown'), out)
        code, out = self.restart_cli('--workers', 'not json', '--host-capable')
        self.assertEqual((code, out['outcome']), (1, 'blocked'), out)
        code, out = self.restart_cli('--workers', '[]')
        self.assertEqual(out['reason'], 'host-capability-absent', out)
        dispatch.set_hold(self.root, 'project', 'Ben asked to wait')
        code, out = self.restart_cli('--workers', '[]', '--host-capable')
        self.assertEqual(out['reason'], 'human-hold', out)
        dispatch.set_hold(self.root, 'project')
        for leftover in Path(self.root).glob('context-watch-terminal-*.json'):
            leftover.unlink()
        code, out = self.restart_cli('--workers', '[]', '--host-capable', HANDOFF_SESSION_ID='old')
        self.assertEqual(out['reason'], 'identity-not-recorded-by-hook-or-host', out)
        self.pointer('old')
        protocol.save(self.root, 'old', 'save-2', {
            'topic': 'second', 'description': 'Second open work', 'body': self.BODY})
        code, out = self.restart_cli('--workers', '[]', '--host-capable')
        self.assertEqual(out['reason'], 'ambiguous-checkpoint', out)
        self.assertEqual(intents(), [])
        code, out = self.restart_cli('--workers', '[]', '--host-capable', '--path', self.path)
        self.assertEqual(out['outcome'], 'ready', out)
