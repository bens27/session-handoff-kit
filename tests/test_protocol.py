"""Public CLI and hook contracts for deterministic, bounded handoffs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / 'skills/session-handoff/hooks/handoff_ledger.py'
WATCHER = LEDGER.with_name('context_watch.py')


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.hd = self.root / '.handoffs'
        self.hd.mkdir()
        self.env = dict(os.environ, HOME=self.tmp.name, CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='')

    def cli(self, *args, input=None, ok=True):
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)],
                           cwd=self.root, env=self.env, input=input,
                           capture_output=True, text=True, timeout=15)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p

    def hook(self, event, session='one', **fields):
        evt = dict(hook_event_name=event, session_id=session, cwd=str(self.root), **fields)
        p = subprocess.run([sys.executable, str(WATCHER)], input=json.dumps(evt),
                           cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout else {}

    def put(self, name='work.md', fields='', body=None):
        p = self.hd / name
        p.write_text('---\ntopic: work\nstatus: open\n' + fields + '\n---\n' +
                     (body or '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun checks.\n'))
        return p

    def save(self, request='checkpoint-1', topic='work', **extra):
        doc = {'topic': topic, 'description': 'Fix the next failing check.',
               'body': '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun checks.\n'}
        doc.update(extra)
        return self.cli('save', '--session', 'one', '--request-id', request,
                        input=json.dumps(doc))

    def test_lookup_distinguishes_empty_closed_stale_and_claimed(self):
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'none')
        old = self.put('20260901-1000-work.md')
        newer = self.put('20260902-1000-work.md')
        self.cli('resume', newer)
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'none')
        newer.unlink()
        os.utime(old, (1, 1))
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'stale')
        os.utime(old, None)
        self.cli('claim', old, '--owner', 'other')
        result = json.loads(self.cli('lookup').stdout)
        self.assertEqual(result['outcome'], 'claimed')
        self.assertIn('another session', result['action'])

    def test_every_mutation_enforces_owner_and_claim_is_exclusive(self):
        p = self.put()
        children = [subprocess.Popen([sys.executable, str(LEDGER), 'claim', str(p), '--owner', owner],
                    cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for owner in ('one', 'two')]
        for child in children:
            child.communicate(timeout=10)
        self.assertEqual(sorted(c.returncode for c in children), [0, 1])
        for command in ('resume', 'supersede', 'abandon', 'release'):
            self.assertNotEqual(self.cli(command, p, ok=False).returncode, 0, command)

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses filesystem permission bits')
    def test_read_error_is_not_absence_and_write_error_is_actionable(self):
        p = self.put()
        p.chmod(0)
        try:
            result = json.loads(self.cli('lookup', ok=False).stdout)
            self.assertEqual(result['outcome'], 'error')
        finally:
            p.chmod(0o600)
        self.hd.chmod(0o500)
        try:
            failure = self.cli('claim', p, '--owner', 'one', ok=False)
            self.assertNotEqual(failure.returncode, 0)
            self.assertNotIn('Traceback', failure.stderr)
            self.assertIn('blocked', failure.stderr)
        finally:
            self.hd.chmod(0o700)

    def test_save_is_idempotent_and_preserves_independent_threads(self):
        first = json.loads(self.save().stdout)
        self.assertEqual(first['outcome'], 'saved')
        self.assertEqual(json.loads(self.save().stdout)['path'], first['path'])
        self.save('independent', 'other')
        items = json.loads(self.cli('lookup').stdout)['items']
        self.assertEqual({h['topic'] for h in items}, {'work', 'other'})
        next_doc = json.loads(self.save('next', 'renamed', predecessor=first['path']).stdout)
        self.assertNotEqual(next_doc['path'], first['path'])
        self.assertEqual({h['topic'] for h in json.loads(self.cli('lookup').stdout)['items']}, {'renamed', 'other'})

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses permission bits')
    def test_save_falls_back_and_invalid_body_preserves_checkpoint(self):
        self.hd.chmod(0o500)
        try:
            saved = json.loads(self.save().stdout)
            self.assertEqual(saved['outcome'], 'saved')
            self.assertNotEqual(Path(saved['path']).parent, self.hd)
            self.assertEqual(json.loads(self.cli('lookup').stdout)['total'], 1)
        finally:
            self.hd.chmod(0o700)
        failure = self.cli('save', '--session', 'one', '--request-id', 'bad',
                           input=json.dumps({'topic':'work', 'body':'word ' * 1601}), ok=False)
        self.assertEqual(json.loads(failure.stdout)['outcome'], 'invalid')
        self.assertEqual(json.loads(self.cli('lookup').stdout)['total'], 1)

    def test_retrieval_is_read_only_and_execute_package_is_bounded(self):
        p = self.put(fields='references: notes.txt#L2-L2, ./notes.txt#L2-L2\nskills: writer, alias')
        (self.root / 'notes.txt').write_text('background\nRequired constraint.\n' + 'noise ' * 20000)
        skill = self.root / 'writer.md'
        skill.write_text('Required writing rules.')
        catalog = self.root / 'catalog.json'
        catalog.write_text(json.dumps({'skills': {'writer': str(skill), 'alias': str(skill)}}))
        result = json.loads(self.cli('prepare', p, '--session', 'one').stdout)
        self.assertEqual(result['outcome'], 'retrieved')
        self.assertEqual(len(result['references']), 1)
        self.assertNotIn('noise', result['references'][0]['text'])
        self.assertEqual(result['skills'], [])
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'available')
        package = json.loads(self.cli('prepare', p, '--session', 'one', '--execute', '--catalog', catalog).stdout)
        self.assertEqual(package['outcome'], 'prepared')
        self.assertEqual(len(package['skills']), 1)
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'claimed')
        self.cli('acknowledge', p, '--session', 'one')
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'none')

    def test_large_reference_blocks_preparation_without_claiming(self):
        p = self.put(fields='references: huge.txt')
        (self.root / 'huge.txt').write_text('a' * 250000)
        result = json.loads(self.cli('prepare', p, '--session', 'one', '--execute', ok=False).stdout)
        self.assertEqual(result['outcome'], 'needs-context')
        self.assertNotIn('body', result)
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'available')

    def test_verification_keeps_true_pipeline_status_and_bounded_output(self):
        p = self.put(fields="verify: printf 'failure'; false | tail -1")
        self.cli('prepare', p, '--session', 'one', '--execute')
        self.assertNotEqual(self.cli('acknowledge', p, '--session', 'one', ok=False).returncode, 0)
        result = json.loads(self.cli('verify', p, '--session', 'one', ok=False).stdout)
        self.assertEqual(result['outcome'], 'verification-failed')
        self.assertNotEqual(result['exit_code'], 0)
        self.assertNotEqual(self.cli('acknowledge', p, '--session', 'one', ok=False).returncode, 0)
        self.cli('release', p, '--owner', 'one')
        p = self.put(fields="verify: python3 -c \"print('x'*100000)\"")
        self.cli('prepare', p, '--session', 'one', '--execute')
        response = self.cli('verify', p, '--session', 'one').stdout
        self.assertLess(len(response), 4000)
        self.assertEqual(json.loads(response)['outcome'], 'verified')
        self.assertGreater(Path(json.loads(response)['log']).stat().st_size, 100000)
        self.cli('acknowledge', p, '--session', 'one')

    def test_preparation_cannot_acknowledge_changed_dependencies(self):
        (self.root / 'notes').write_text('constraint')
        p = self.put(fields='references: notes')
        self.cli('prepare', p, '--session', 'one', '--execute')
        (self.root / 'notes').write_text('changed constraint')
        self.assertNotEqual(self.cli('acknowledge', p, '--session', 'one', ok=False).returncode, 0)

    def test_verification_timeout_keeps_handoff_recoverable(self):
        p = self.put(fields='verify: sleep 10')
        self.cli('prepare', p, '--session', 'one', '--execute')
        result = json.loads(self.cli('verify', p, '--session', 'one', '--timeout', '0.1', ok=False).stdout)
        self.assertEqual(result['outcome'], 'verification-timeout')
        self.cli('release', p, '--owner', 'one')
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'available')

    def test_hooks_give_empty_action_and_separate_retrieve_from_resume(self):
        empty = self.hook('SessionStart')
        self.assertIn('No open handoff', json.dumps(empty))
        self.put(fields='skills: execution-only')
        retrieved = self.hook('UserPromptSubmit', prompt='Retrieve your handoff')
        self.assertIn('prepare', json.dumps(retrieved))
        self.assertNotIn('--execute', json.dumps(retrieved))
        resumed = self.hook('UserPromptSubmit', prompt='resume')
        self.assertIn('--execute', json.dumps(resumed))
        self.assertNotIn('First load', json.dumps(resumed))

    def test_session_checkpoint_identity_ignores_old_file_mtime(self):
        # A threshold crossing supplies the exact session/trigger identity used by save.
        self.env['HANDOFF_AT'] = '50000'
        tr = self.root / 'trace.jsonl'
        tr.write_text(json.dumps({'type':'assistant','message':{'model':'test','usage':{'input_tokens':1000}}}) + '\n' +
                      json.dumps({'type':'assistant','message':{'model':'test','usage':{'input_tokens':60000}}}) + '\n')
        sid = 'protocol-' + self.root.parent.name
        self.hook('PostToolUse', session=sid, transcript_path=str(tr))
        self.hook('SessionStart', session='other')
        unrelated = json.loads(self.save().stdout)
        self.cli('resume', unrelated['path'])
        stopped = self.hook('Stop', session=sid)
        self.assertEqual(stopped.get('decision'), 'block')
        doc = {'topic':'actual', 'description':'Required checkpoint', 'body':'## Objective\nFix it.\n## Current state\nReady.\n## Next steps\nTest.\n'}
        result = json.loads(self.cli('save', '--session', sid, '--request-id', 'fired', input=json.dumps(doc)).stdout)
        self.assertIn(sid, Path(result['path']).read_text())
        self.assertIn('reason: context-pressure', Path(result['path']).read_text())
        self.assertNotIn('reason: context-pressure', Path(unrelated['path']).read_text())

    def test_resolution_and_history_are_bounded_as_history_grows(self):
        for i in range(80):
            self.put('history-%03d.md' % i, fields='created: 2026-09-28T10:%02d\ndescription: %s' % (i % 60, 'long ' * 1000))
        result = self.cli('resolve', 'work', '--json').stdout
        self.assertLess(len(result), 6000)
        self.assertEqual(len(json.loads(result)['chain']), 1)
        page = json.loads(self.cli('history', '--topic', 'work', '--limit', '5').stdout)
        self.assertEqual(len(page['items']), 5)
        self.assertEqual(page['next_offset'], 5)
        self.assertLess(len(json.dumps(self.hook('SessionStart'))), 3000)

    def test_resume_metrics_record_sizes_without_content(self):
        self.env['CONTEXT_WATCH_RESUME_LOG'] = str(self.root / 'metrics.jsonl')
        self.env.pop('CONTEXT_WATCH_LOG')
        self.cli('lookup')
        p = self.put(body='## Objective\nPRIVATE-MARKER\n## Current state\nReady\n## Next steps\nTest\n')
        self.cli('prepare', p, '--session', 'one')
        raw = (self.root / 'metrics.jsonl').read_text()
        self.assertNotIn('PRIVATE-MARKER', raw)
        rows = [json.loads(l) for l in raw.splitlines()]
        self.assertEqual([r['operation'] for r in rows], ['lookup', 'prepare'])
        self.assertIn('package_bytes', rows[-1]['metrics'])
        self.assertIn('startup_input_tokens', rows[-1])

    def test_concurrent_identical_saves_publish_once(self):
        raw = json.dumps({'topic':'work', 'body':'## Objective\nFix\n## Current state\nReady\n## Next steps\nTest\n'})
        calls = [subprocess.Popen([sys.executable, str(LEDGER), 'save', '--session', 'one', '--request-id', 'same'],
                 cwd=self.root, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(3)]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs = list(pool.map(lambda child: child.communicate(raw, timeout=10), calls))
        results = []
        for child, (out, err) in zip(calls, outputs):
            self.assertEqual(child.returncode, 0, out + err)
            results.append(json.loads(out)['path'])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(json.loads(self.cli('history').stdout)['total'], 1)

    def test_preparation_cannot_acknowledge_superseded_work(self):
        first = json.loads(self.save().stdout)['path']
        self.cli('prepare', first, '--session', 'one', '--execute')
        self.save('replacement', 'new-name', predecessor=first)
        response = self.cli('acknowledge', first, '--session', 'one', ok=False)
        self.assertNotEqual(response.returncode, 0)

    def test_loaded_skill_aliases_are_counted_but_not_emitted(self):
        skill = self.root / 'skill.md'
        skill.write_text('Already present policy.')
        p = self.put(fields='skills: first, alias')
        catalog = self.root / 'catalog.json'
        catalog.write_text(json.dumps({'skills':{'first':str(skill),'alias':str(skill)},'loaded':[str(skill)]}))
        r = json.loads(self.cli('prepare', p, '--session', 'one', '--execute', '--catalog', catalog).stdout)
        self.assertEqual(r['skills'], [])
        self.assertEqual(r['metrics']['already_loaded_bytes'], len(skill.read_bytes()))
        self.assertGreater(r['metrics']['workflow_bytes'], 0)

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses permission bits')
    def test_all_destinations_denied_preserves_draft_and_reports_blocked(self):
        fallback = Path(self.tmp.name) / '.claude/handoffs/project'
        fallback.mkdir(parents=True)
        self.hd.chmod(0o500)
        fallback.chmod(0o500)
        try:
            raw = json.dumps({'topic':'work', 'body':'## Objective\nFix\n## Current state\nReady\n## Next steps\nTest\n'})
            r = json.loads(self.cli('save', '--session','one','--request-id','denied',input=raw,ok=False).stdout)
            self.assertEqual(r['outcome'], 'blocked')
            self.assertIn('Do not clear', r['action'])
            self.assertEqual(json.loads(self.cli('history').stdout)['total'], 0)
        finally:
            self.hd.chmod(0o700)
            fallback.chmod(0o700)

    def test_oversized_metadata_cannot_flood_resolution(self):
        self.put(fields='references: ' + ', '.join('missing-%d' % i for i in range(4000)))
        result = self.cli('resolve', 'work', '--json', ok=False)
        self.assertLess(len(result.stdout), 32000)
        self.assertNotIn('Traceback', result.stderr)

    def test_explicit_topic_after_first_page_routes_without_ambiguity(self):
        for i in range(8):
            p = self.hd / ('topic-%d.md' % i)
            p.write_text('---\ntopic: topic-%d\nstatus: open\ncreated: 2026-09-28T10:0%d\n---\n' % (i, i))
        response = json.dumps(self.hook('UserPromptSubmit', prompt='resume topic-0'))
        self.assertIn('prepare', response)
        self.assertIn('topic-0.md', response)
        self.assertIn('--execute', response)

    def test_interrupted_publication_is_recoverable_by_same_request(self):
        saved = json.loads(self.save().stdout)
        receipt = Path(saved['path'] + '.published')
        self.assertTrue(receipt.exists())
        # Crash boundary: document reached disk, publication receipt did not.
        receipt.unlink()
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'incomplete')
        retried = json.loads(self.save().stdout)
        self.assertEqual(retried['path'], saved['path'])
        self.assertEqual(json.loads(self.cli('lookup').stdout)['total'], 1)

    def test_acknowledgment_retry_does_not_require_reclaiming_closed_work(self):
        p = json.loads(self.save().stdout)['path']
        self.cli('prepare', p, '--session', 'one', '--execute')
        self.cli('acknowledge', p, '--session', 'one')
        self.assertEqual(json.loads(self.cli('acknowledge', p, '--session', 'one').stdout)['outcome'], 'resumed')
        self.assertEqual(json.loads(self.cli('lookup').stdout)['outcome'], 'none')

    def test_legacy_supersede_preserves_publication_integrity(self):
        first = json.loads(self.save().stdout)['path']
        second = json.loads(self.save('next', 'other').stdout)['path']
        self.cli('supersede', first, '--by', second)
        result = json.loads(self.cli('lookup').stdout)
        self.assertEqual(result['outcome'], 'available')
        self.assertEqual(result['total'], 1)

    def test_empty_startup_does_not_trigger_checkpoint_skill_loading(self):
        result = json.dumps(self.hook('SessionStart'))
        self.assertIn('No open handoff', result)
        self.assertNotIn('[context-watch]', result)
        self.assertIn('no skill loading', result)

    def test_lookup_and_history_bound_malformed_metadata(self):
        self.put(fields='git: ' + 'g' * 40000)
        self.assertLess(len(self.cli('lookup').stdout), 32000)
        self.put(fields='created: ' + 'bad-date' * 5000)
        self.assertLess(len(self.cli('history').stdout), 32000)


if __name__ == '__main__':
    unittest.main()
