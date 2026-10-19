"""Hermetic CLI/hook recovery contracts; native host restoration is out of scope.

HANDOFF_RECOVERY_SKILL can select an accepted read-only snapshot for comparison.
Every hook, draft, transcript and state write stays in a disposable fixture.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(os.environ.get('HANDOFF_RECOVERY_SKILL',
    str(Path(__file__).resolve().parents[1] / 'skills/session-handoff')))
BODY = '## Objective\nSaved older objective.\n## Current state\nKnown saved facts.\n## Next steps\nReview the saved objective.\n'


class RecoveryFixture:
    def __init__(self, adapter):
        self.adapter = adapter
        self.temp = tempfile.TemporaryDirectory(prefix='handoff-recovery-')
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        self.handoffs = self.root / '.handoffs'
        self.handoffs.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.transcript = self.base / (adapter + '-native-history.jsonl')
        if adapter == 'claude':
            event = {'type': 'assistant', 'message': {'model': 'claude-test',
                     'content': 'UNSAVED_NATIVE_EVIDENCE', 'usage': {'input_tokens': 1000}}}
        else:
            event = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                     'last_token_usage': {'total_tokens': 1000}, 'model_context_window': 200000}}}
        history = json.dumps(event) + '\n'
        if adapter == 'codex':
            history += json.dumps({'type': 'response_item', 'payload': {'type': 'message',
                'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'UNSAVED_NATIVE_EVIDENCE'}]}}) + '\n'
        self.transcript.write_text(history)
        self.history_hash = self.fingerprint(self.transcript)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(
            ('HANDOFF_', 'CONTEXT_WATCH_', 'AGENTSROOM_', 'AUTORESUME', 'TYPESAFE_'))
            and k not in ('CODEX_THREAD_ID', 'TMUX_PANE')}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), TMPDIR=str(self.tmp),
            HANDOFF_TERMINAL_ID='terminal-a', HANDOFF_AUTO='0', CONTEXT_WATCH_AGENT=adapter,
            CODEX_HOME=str(self.home / 'codex'), CLAUDE_CONFIG_DIR=str(self.home / 'claude'),
            CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_JEV='0',
            CONTEXT_WATCH_ORIGIN='test', PYTHONDONTWRITEBYTECODE='1',
            CONTEXT_WATCH_THINKING='0', CONTEXT_WATCH_PENDING='0')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.temp.cleanup()

    @staticmethod
    def fingerprint(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def cli(self, *args, draft=None):
        p = subprocess.run([sys.executable, '-B', '-S', str(SKILL / 'hooks/handoff_ledger.py'), *map(str, args)],
            cwd=self.root, env=self.env, input=json.dumps(draft) if draft is not None else None,
            capture_output=True, text=True, timeout=15)
        if 'Traceback' in p.stderr:
            raise AssertionError(p.stderr)
        return p.returncode, json.loads(p.stdout)

    def crash_save(self, phase):
        draft = self.base / 'retained-draft.json'
        draft.write_text(json.dumps({'topic': 'interrupted', 'description': 'Retained draft.', 'body': BODY}))
        marker = self.base / 'crash-boundary.txt'
        # Fault injection at the filesystem boundary, never a real host process.
        script = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import handoff_protocol as protocol
phase, marker = sys.argv[3], Path(sys.argv[4])
def audit(event,args):
    if event not in ('os.link','os.rename') or len(args)<2:
        return
    destination=str(args[1])
    hit=(phase=='before-document' and event=='os.link' and destination.endswith('.md')) or (phase=='before-receipt' and event=='os.rename' and destination.endswith('.md.published'))
    if hit:
        marker.write_text(event)
        os._exit(71)
sys.addaudithook(audit)
protocol.cli(['save','--session','saver','--request-id','interrupted','--input',sys.argv[2]])
"""
        stopped = subprocess.run([sys.executable, '-B', '-S', '-c', script, str(SKILL / 'hooks'),
            str(draft), phase, str(marker)], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=10)
        if stopped.returncode != 71 or not marker.is_file():
            raise AssertionError('Crash boundary not reached: ' + stopped.stderr + stopped.stdout)
        return draft

    def save(self, topic='older', request='one', session='saver'):
        code, result = self.cli('save', '--session', session, '--request-id', request,
            draft={'topic': topic, 'description': 'Actual saved context only.', 'body': BODY})
        if code != 0 or result['outcome'] != 'saved':
            raise AssertionError(result)
        return Path(result['path'])

    def hook(self, event='SessionStart', **fields):
        evt = dict(hook_event_name=event, session_id='fresh-session', cwd=str(self.root),
                   transcript_path=str(self.transcript))
        evt.update(fields)
        p = subprocess.run([sys.executable, '-B', '-S', str(SKILL / 'hooks/context_watch.py')],
            cwd=self.root, env=self.env, input=json.dumps(evt), capture_output=True,
            text=True, timeout=10)
        note = {}
        if p.stdout.strip():
            if self.adapter == 'codex' and not p.stdout.lstrip().startswith('{'):
                note = {'codex_stdout': p.stdout.strip()}
            else:
                note = json.loads(p.stdout)
        return p.returncode, note, p.stderr

    def observe(self, case, **facts):
        target = os.environ.get('HANDOFF_RECOVERY_MATRIX_LOG')
        if target:
            record = dict(case=case, adapter=self.adapter, observed_at=datetime.now(timezone.utc).isoformat(),
                          fixture=str(self.root), checkpoint_paths=[str(p) for p in self.handoffs.glob('*.md')],
                          native_history_unchanged=self.fingerprint(self.transcript) == self.history_hash, **facts)
            with open(target, 'a') as output:
                output.write(json.dumps(record) + '\n')


class NoCheckpointRecoveryTests(unittest.TestCase):
    def test_only_foreign_checkpoint_requires_explicit_selection(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                f.env['HANDOFF_TERMINAL_ID'] = 'terminal-b'
                other = f.save('foreign')
                original = other.read_bytes()
                f.env['HANDOFF_TERMINAL_ID'] = 'terminal-a'
                self.assertEqual(f.hook(source='startup')[:2], (0, {}))
                self.assertEqual(f.hook('UserPromptSubmit', prompt='continue the handoff')[:2], (0, {}))
                code, result = f.cli('resume', '--session', 'new-a')
                f.observe('foreign-only', outcome=result['outcome'], foreign_unconsumed=other.read_bytes() == original)
                self.assertEqual((code, result['outcome']), (0, 'choose'))
                self.assertEqual(other.read_bytes(), original)
                explicit = f.cli('resume', other, '--session', 'explicit-adopter')[1]
                self.assertEqual(explicit['outcome'], 'resumed')
                self.assertEqual(explicit['body'], BODY)

    def test_clean_no_checkpoint_and_repeated_startup_are_nonconsuming(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                for _ in range(3):
                    self.assertEqual(f.hook(source='startup')[:2], (0, {}))
                    self.assertEqual(f.hook('UserPromptSubmit', prompt='Work on the current request')[:2], (0, {}))
                    self.assertEqual(f.cli('lookup')[1]['outcome'], 'none')
                    self.assertEqual(f.cli('resume')[1]['outcome'], 'none')
                    code, result = f.cli('resume', '--session', 'fresh')
                    self.assertEqual((code, result['outcome']), (0, 'none'))
                    self.assertNotIn('body', result)
                self.assertEqual(f.cli('history')[1]['total'], 0)
                self.assertEqual(list(f.handoffs.glob('*.md')), [])
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)
                f.observe('clean-and-repeated-startup', outcome='none', retries=3)

    def test_abrupt_exit_before_save_preserves_native_resume_events_and_history(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                draft = f.base / 'unsaved-draft.json'
                draft.write_text(json.dumps({'topic': 'never-saved', 'body': BODY}))
                stopped = subprocess.run([sys.executable, '-B', '-S', '-c', 'raise SystemExit(23)'],
                    cwd=f.root, env=f.env, capture_output=True, timeout=5)
                self.assertEqual(stopped.returncode, 23)
                for source in ('resume', 'fork', 'resume'):
                    self.assertEqual(f.hook(source=source)[:2], (0, {}))
                code, result = f.cli('resume', '--session', 'native-restored')
                self.assertEqual((code, result['outcome']), (0, 'none'))
                self.assertEqual(f.cli('history')[1]['total'], 0)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)
                self.assertTrue(draft.is_file())
                f.observe('abrupt-before-save-native-event', outcome='none', child_exit=23,
                          actual_host_history_restore='not tested')

    def test_interrupted_before_document_has_no_handoff_and_exact_retry_can_publish(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                draft = f.crash_save('before-document')
                self.assertEqual(list(f.handoffs.glob('*.md')), [])
                self.assertEqual(f.hook(source='startup')[:2], (0, {}))
                self.assertEqual(f.cli('resume', '--session', 'fresh')[1]['outcome'], 'none')
                self.assertEqual(f.cli('history')[1]['total'], 0)
                self.assertTrue(draft.is_file())
                f.observe('interrupted-before-document', outcome='none', checkpoint_exists=False,
                          retained_draft_exists=draft.is_file())
                code, saved = f.cli('save', '--session', 'saver', '--request-id', 'interrupted', '--input', draft)
                self.assertEqual((code, saved['outcome']), (0, 'saved'))
                code, retry = f.cli('save', '--session', 'saver', '--request-id', 'interrupted', '--input', draft)
                self.assertEqual(retry['path'], saved['path'])
                self.assertEqual(f.cli('history')[1]['total'], 1)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)

    def test_interrupted_before_publication_receipt_blocks_consumption_then_repairs(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                f.crash_save('before-receipt')
                path, = f.handoffs.glob('*.md')
                original = path.read_bytes()
                self.assertFalse(Path(str(path) + '.published').exists())
                self.assertEqual(f.cli('lookup')[1]['outcome'], 'incomplete')
                for _ in range(2):
                    code, note, error = f.hook(source='startup')
                    self.assertEqual(code, 0, error)
                    self.assertIn(' repair ', json.dumps(note))
                    self.assertEqual(f.cli('resume', path, '--session', 'fresh')[1]['outcome'], 'incomplete')
                self.assertEqual(path.read_bytes(), original)
                self.assertNotIn(b'status: resumed', path.read_bytes())
                f.observe('interrupted-before-receipt', outcome='incomplete', checkpoint_exists=path.exists(),
                          publication_exists=Path(str(path) + '.published').exists(), retries=2)
                self.assertEqual(f.cli('repair', path, '--session', 'fresh')[1]['outcome'], 'saved')
                self.assertEqual(f.cli('lookup')[1]['outcome'], 'available')
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)

    def test_deleted_explicit_target_never_falls_back_to_older_or_foreign_work(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                older = f.save('older')
                requested = f.save('current', 'current')
                f.env['HANDOFF_TERMINAL_ID'] = 'terminal-b'
                foreign = f.save('foreign', 'foreign')
                f.env['HANDOFF_TERMINAL_ID'] = 'terminal-a'
                before = {p: p.read_bytes() for p in (older, foreign)}
                requested.unlink()
                self.assertFalse(requested.exists())
                for command in ('prepare', 'resume'):
                    code, result = f.cli(command, requested, '--session', 'fresh')
                    f.observe('deleted-explicit-target', command=command, outcome=result['outcome'],
                              requested_path=str(requested), requested_exists=requested.exists(),
                              selected_path=result.get('selected'), returned_body='body' in result, action=result['action'])
                    self.assertEqual((code, result['outcome']), (0, 'none'))
                    self.assertIn('lookup', result['action'])
                    self.assertNotIn('body', result)
                    self.assertNotIn('selected', result)
                self.assertEqual(f.hook(source='resume')[:2], (0, {}))
                for p, data in before.items():
                    self.assertEqual(p.read_bytes(), data)
                self.assertEqual(f.cli('prepare', older, '--session', 'fresh')[1]['body'], BODY)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)

    def test_truncated_checkpoint_is_not_consumed_or_silently_reconstructed(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                path = f.save('truncated')
                original = path.read_bytes()
                header = original.split(b'\n---\n', 1)[0]
                damaged = header + b'\n---\n## Objective\nTruncated draft only.\n'
                path.write_bytes(damaged)
                self.assertEqual(f.hook(source='resume')[:2], (0, {}))
                self.assertEqual(f.cli('resume', path, '--session', 'fresh')[1]['outcome'], 'incomplete')
                code, repair = f.cli('repair', path, '--session', 'fresh')
                self.assertEqual((code, repair['outcome']), (1, 'blocked'))
                self.assertEqual(path.read_bytes(), damaged)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)
                f.observe('truncated-checkpoint', outcome='incomplete', repair='blocked',
                          checkpoint_exists=path.exists(), damaged_bytes_preserved=True)
                # Restoring known original bytes is an explicit fixture action,
                # not an automatic reconstruction by the kit.
                path.write_bytes(original)
                self.assertEqual(f.cli('repair', path, '--session', 'fresh')[1]['outcome'], 'saved')
                self.assertEqual(f.cli('prepare', path, '--session', 'fresh')[1]['body'], BODY)

    def test_missing_checkpoint_with_transfer_receipt_does_not_replay_stale_body(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                path = f.save('missing-receipt')
                self.assertEqual(f.cli('resume', path, '--session', 'holder')[1]['outcome'], 'resumed')
                receipt, = (f.home / '.local/state/session-handoff/transfers').glob('*.json')
                before = receipt.read_bytes()
                path.unlink()
                self.assertFalse(path.exists())
                for _ in range(2):
                    self.assertEqual(f.hook(source='startup')[:2], (0, {}))
                    result = f.cli('resume', '--session', 'fresh')[1]
                    self.assertEqual(result['outcome'], 'none')
                    self.assertNotIn('body', result)
                self.assertEqual(receipt.read_bytes(), before)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)
                f.observe('receipt-target-absent', outcome='none', requested_exists=path.exists(),
                          receipt_preserved=True, stale_body_replayed=False)

    def test_pending_restart_intent_with_absent_target_never_redispatches(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                path = f.save('missing-intent')
                args = dict(root=str(f.root), path=str(path), session='saver', terminal='terminal-a',
                            request_id='fixture-restart', workers=[], host_capable=True)
                def preflight():
                    p = subprocess.run([sys.executable, '-B', '-S', str(SKILL / 'hooks/restart_preflight.py'), 'preflight'],
                        cwd=f.root, env=f.env, input=json.dumps(args), text=True,
                        capture_output=True, timeout=10)
                    self.assertNotIn('Traceback', p.stderr)
                    return json.loads(p.stdout)
                self.assertEqual(preflight()['outcome'], 'ready')
                intent, = (f.handoffs / 'restart').glob('*.json')
                before = intent.read_bytes()
                path.unlink()
                self.assertFalse(path.exists())
                for _ in range(3):
                    self.assertEqual(f.hook(source='startup')[:2], (0, {}))
                    result = preflight()
                    self.assertEqual(result['outcome'], 'pending')
                    self.assertFalse(result['dispatch'])
                    self.assertEqual(f.cli('resume', '--session', 'fresh')[1]['outcome'], 'none')
                self.assertEqual(intent.read_bytes(), before)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)
                f.observe('intent-target-absent', outcome='pending', requested_exists=path.exists(),
                          retries=3, dispatch=False, actual_host_dispatch='not performed')

    def test_older_own_checkpoint_does_not_hijack_native_or_unrelated_continuation(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                older = f.save('older-only')
                before = older.read_bytes()
                self.assertEqual(f.hook(source='startup')[:2], (0, {}))
                self.assertEqual(f.hook(source='resume')[:2], (0, {}))
                self.assertEqual(f.hook('UserPromptSubmit', prompt='Continue my current native conversation task')[:2], (0, {}))
                code, note, error = f.hook('UserPromptSubmit', prompt='continue the handoff')
                self.assertEqual(code, 0, error)
                self.assertIn(older.name, json.dumps(note))
                self.assertEqual(older.read_bytes(), before)
                self.assertEqual(f.cli('prepare', older, '--session', 'fresh')[1]['body'], BODY)
                self.assertEqual(older.read_bytes(), before)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)
                f.observe('older-own-only', outcome='explicit-handoff-offer-only',
                          saved_body='Saved older objective; no unsaved reconstruction', older_unconsumed=True)

    def test_pressure_then_repeated_stop_without_save_never_clears_or_loops(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                binary = f.base / 'bin'
                binary.mkdir()
                capture = f.base / 'tmux-calls.txt'
                tmux = binary / 'tmux'
                tmux.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' + repr(str(capture)) + ').write_text("called")\n')
                tmux.chmod(0o755)
                f.env.update(HANDOFF_AUTO='1', TMUX_PANE='%fixture', HANDOFF_AT='130000',
                             PATH=str(binary) + os.pathsep + f.env.get('PATH', ''))
                if adapter == 'claude':
                    high = {'type': 'assistant', 'message': {'model': 'claude-test', 'usage': {'input_tokens': 140000}}}
                else:
                    high = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                        'last_token_usage': {'total_tokens': 140000}, 'model_context_window': 200000}}}
                with f.transcript.open('a') as output:
                    output.write(json.dumps(high) + '\n')
                f.history_hash = f.fingerprint(f.transcript)
                code, notice, error = f.hook('PostToolUse')
                self.assertEqual(code, 0, error)
                self.assertIn('context-pressure', json.dumps(notice))
                for _ in range(3):
                    self.assertEqual(f.hook('Stop', stop_hook_active=True)[0], 0)
                self.assertFalse(capture.exists())
                self.assertEqual(f.cli('lookup')[1]['outcome'], 'none')
                self.assertEqual(f.cli('history')[1]['total'], 0)
                self.assertEqual(f.fingerprint(f.transcript), f.history_hash)
                f.observe('pressure-no-save-repeated-stop', outcome='none', retries=3,
                          simulated_tmux_calls=0, actual_host_restart='not performed')

    def test_untagged_different_session_requires_explicit_choice(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                f.env.pop('HANDOFF_TERMINAL_ID')
                path = f.save('untagged', session='sess-B')
                original = path.read_bytes()
                f.env.update(HANDOFF_TERMINAL_ID='terminal-a', HANDOFF_SESSION_ID='sess-A')
                lookup = f.cli('lookup')[1]
                code, result = f.cli('resume', '--session', 'sess-A')
                f.observe('untagged-different-session', outcome=result['outcome'],
                          lookup_selected=lookup.get('selected'), checkpoint_unchanged=path.read_bytes()==original)
                self.assertNotIn('selected', lookup)
                self.assertEqual((code, result['outcome']), (0, 'choose'))
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(f.cli('resume', path, '--session', 'sess-A')[1]['outcome'], 'resumed')

    def test_untagged_same_session_remains_implicitly_recoverable(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                f.env.pop('HANDOFF_TERMINAL_ID')
                path = f.save('same-session', session='sess-A')
                f.env.update(HANDOFF_TERMINAL_ID='terminal-a', HANDOFF_SESSION_ID='sess-A')
                lookup = f.cli('lookup')[1]
                self.assertEqual(Path(lookup['selected']).resolve(), path.resolve())
                result = f.cli('resume', '--session', 'sess-A')[1]
                self.assertEqual(result['outcome'], 'resumed')
                self.assertEqual(result['body'], BODY)
                f.observe('untagged-same-session', outcome=result['outcome'], same_session_selected=True)

    def test_untagged_no_current_terminal_retains_portable_selection(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                f.env.pop('HANDOFF_TERMINAL_ID')
                path = f.save('portable', session='sess-B')
                f.env['HANDOFF_SESSION_ID'] = 'sess-A'
                lookup = f.cli('lookup')[1]
                self.assertEqual(Path(lookup['selected']).resolve(), path.resolve())
                result = f.cli('resume', '--session', 'sess-A')[1]
                self.assertEqual(result['outcome'], 'resumed')
                f.observe('untagged-no-current-terminal', outcome=result['outcome'], portable_selection=True)

    def test_untagged_unknown_current_session_fails_safe(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                f.env.pop('HANDOFF_TERMINAL_ID')
                path = f.save('unknown-caller', session='sess-B')
                original = path.read_bytes()
                f.env['HANDOFF_TERMINAL_ID'] = 'terminal-a'
                lookup = f.cli('lookup')[1]
                code, result = f.cli('resume')
                f.observe('untagged-current-session-unknown', outcome=result['outcome'],
                          lookup_selected=lookup.get('selected'), checkpoint_unchanged=path.read_bytes()==original)
                self.assertNotIn('selected', lookup)
                self.assertEqual((code, result['outcome']), (1, 'blocked'))
                self.assertEqual(path.read_bytes(), original)

    def test_untagged_unknown_checkpoint_session_requires_explicit_choice(self):
        for adapter in ('claude', 'codex'):
            with self.subTest(adapter=adapter), RecoveryFixture(adapter) as f:
                path = f.handoffs / 'unidentified.md'
                path.write_text('---\ntopic: unidentified\nstatus: open\nterminal: \nsession_id: \n---\n' + BODY)
                original = path.read_bytes()
                f.env['HANDOFF_SESSION_ID'] = 'sess-A'
                lookup = f.cli('lookup')[1]
                code, result = f.cli('resume', '--session', 'sess-A')
                f.observe('untagged-checkpoint-session-unknown', outcome=result['outcome'],
                          lookup_selected=lookup.get('selected'), checkpoint_unchanged=path.read_bytes()==original)
                self.assertNotIn('selected', lookup)
                self.assertEqual((code, result['outcome']), (0, 'choose'))
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(f.cli('resume', path, '--session', 'sess-A')[1]['outcome'], 'resumed')
