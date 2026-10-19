"""Original-save retry identity under a fresh hook pointer (F1) and
transfer-receipt loss on receipt-era records (F2)."""
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / 'skills/session-handoff/hooks'
LEDGER = HOOKS / 'handoff_ledger.py'
WATCH = HOOKS / 'context_watch.py'
BODY = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun the parser checks.\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.home = base
        self.root = base / 'project'
        (self.root / '.handoffs').mkdir(parents=True)
        (self.root / 'notes.md').write_text('# Notes\nKeep the parser fixtures.\n')
        skill = base / 'skills/tdd/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('---\nname: tdd\n---\nWrite the failing test first.\n')
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID',
                                 'HANDOFF_SKILL_ROOTS', 'CODEX_HOME', 'HANDOFF_AUTO', 'HANDOFF_RUN_ID')}
        self.env.update(HOME=str(base), TMPDIR=str(base), CONTEXT_WATCH_ORIGIN='test', CONTEXT_WATCH_LOG='0',
                        CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='',
                        HANDOFF_SKILL_ROOTS=str(base / 'skills'))

    def run_cli(self, *args, input=None, terminal=None):
        env = dict(self.env)
        if terminal:
            env['HANDOFF_TERMINAL_ID'] = terminal
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=self.root, env=env,
                           input=input, capture_output=True, text=True, timeout=60)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return p.returncode, json.loads(p.stdout)

    def save(self, doc, session, request, terminal):
        return self.run_cli('save', '--session', session, '--request-id', request,
                            input=json.dumps(doc), terminal=terminal)

    def session_start(self, session, terminal):
        env = dict(self.env, HANDOFF_TERMINAL_ID=terminal)
        event = dict(hook_event_name='SessionStart', source='startup', session_id=session, cwd=str(self.root))
        p = subprocess.run([sys.executable, str(WATCH)], cwd=self.root, env=env, input=json.dumps(event),
                           capture_output=True, text=True, timeout=60)
        return p.stdout

    def checkpoints(self):
        return sorted(str(p) for p in (self.root / '.handoffs').glob('*.md'))


class OriginalRetryIdentityTests(Base):
    """F1: the SessionStart command for an intent-less unpublished save must finish that save."""

    def interrupted_successor(self):
        first = dict(topic='parser', description='Parser', body=BODY, references=['notes.md'], skills=['tdd'],
                     continuity=[dict(id='goal', text='Keep fixtures', status='open')])
        code, a = self.save(first, 'sess-a', 'req-a', 'term-a')
        self.assertEqual(a['outcome'], 'saved', a)
        second = dict(topic='parser', description='Parser 2', body=BODY.replace('Fix it', 'Finish it'))
        code, b = self.save(second, 'sess-a', 'req-b', 'term-a')
        self.assertEqual(b['outcome'], 'saved', b)
        # Crash before the receipt, with no durable intent (legacy-style interruption).
        os.unlink(b['path'] + '.published')
        for intent in glob.glob(str(self.home / '.local/state/session-handoff/publications/*.json')):
            if json.loads(Path(intent).read_text())['path'] == os.path.realpath(b['path']):
                os.unlink(intent)
        return second, b['path']

    def assert_complete_resume(self, path):
        code, out = self.run_cli('resume', path, '--session', 'sess-r', '--fields', 'path,body', terminal='term-r')
        self.assertEqual(out['outcome'], 'resumed', out)
        self.assertEqual([r['path'] for r in out['references']], [str(self.root / 'notes.md')])
        self.assertEqual([s['name'] for s in out['skills']], ['tdd'])
        self.assertEqual([c['id'] for c in out['continuity']], ['goal'])

    def test_startup_retry_command_seals_the_original_checkpoint_despite_fresh_pointer(self):
        draft, path = self.interrupted_successor()
        note = self.session_start('sess-fresh', 'term-a')
        self.assertIn('--session sess-a', note)
        before = self.checkpoints()
        code, out = self.save(draft, 'sess-a', 'req-b', 'term-a')
        self.assertEqual((code, out['outcome'], out['path']), (0, 'saved', path), out)
        self.assertEqual(self.checkpoints(), before)
        self.assertTrue(os.path.exists(path + '.published'))
        self.assert_complete_resume(path)

    def test_pointer_free_retry_control_still_seals_the_original(self):
        draft, path = self.interrupted_successor()
        code, out = self.save(draft, 'sess-a', 'req-b', 'term-a')
        self.assertEqual((code, out['outcome'], out['path']), (0, 'saved', path), out)
        self.assert_complete_resume(path)

    def test_new_content_cannot_become_a_successor_of_an_own_unpublished_checkpoint(self):
        draft, path = self.interrupted_successor()
        self.session_start('sess-fresh', 'term-a')
        before = self.checkpoints()
        code, out = self.save(dict(draft, body=BODY + '\nMore.\n'), 'sess-fresh', 'req-new', 'term-a')
        self.assertEqual((code, out['outcome']), (1, 'incomplete'), out)
        self.assertEqual(out['path'], path)
        self.assertIn('--request-id', out['action'])
        self.assertEqual(self.checkpoints(), before)

    def test_explicit_session_without_matching_original_still_uses_hook_identity(self):
        draft, path = self.interrupted_successor()
        self.session_start('sess-fresh', 'term-a')
        code, out = self.save(dict(topic='other', description='Other', body=BODY), 'sess-a', 'req-unrelated', 'term-a')
        self.assertEqual(out['outcome'], 'saved', out)
        self.assertIn('session_id: sess-fresh', Path(out['path']).read_text())

    def test_another_terminal_cannot_retry_the_original_under_its_session(self):
        draft, path = self.interrupted_successor()
        self.session_start('sess-z', 'term-z')
        before = self.checkpoints()
        code, out = self.save(draft, 'sess-a', 'req-b', 'term-z')
        self.assertNotEqual(out['outcome'], 'saved', out)
        self.assertEqual(self.checkpoints(), before)
        self.assertFalse(os.path.exists(path + '.published'))


    def legacy(self, *args, terminal='term-a'):
        env = dict(self.env, HANDOFF_TERMINAL_ID=terminal)
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=self.root, env=env,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def test_incomplete_guidance_names_abandon_and_distinct_topic_escapes(self):
        draft, path = self.interrupted_successor()
        code, out = self.save(dict(draft, body=BODY + '\nMore.\n'), 'sess-a', 'req-new', 'term-a')
        self.assertEqual(out['outcome'], 'incomplete', out)
        self.assertIn('abandon', out['action'])
        self.assertIn('distinct topic', out['action'])
        code, other = self.save(dict(topic='independent', description='Other', body=BODY), 'sess-a', 'req-o', 'term-a')
        self.assertEqual(other['outcome'], 'saved', other)

    def test_explicit_abandon_of_unrepairable_checkpoint_lifts_the_gate_without_inheritance(self):
        draft, path = self.interrupted_successor()
        code, repaired = self.run_cli('repair', path, '--session', 'sess-a', terminal='term-a')
        self.assertEqual(repaired['outcome'], 'needs-original-draft', repaired)
        code, text = self.legacy('abandon', path)
        self.assertEqual(code, 0, text)
        code, out = self.save(dict(draft, body=BODY + '\nRestarted.\n'), 'sess-a', 'req-new', 'term-a')
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        saved = Path(out['path']).read_text()
        # The abandoned record was never trusted: nothing is inherited from it.
        for key in ('skills', 'references'):
            self.assertIn('\n%s: \n' % key, saved)
        self.assertNotIn('continuity:', saved)
        self.assertTrue(any('inherit' in w for w in out['warnings']), out)
        code, closed = self.run_cli('prepare', path, '--session', 'sess-r', terminal='term-r')
        self.assertNotEqual(closed['outcome'], 'incomplete', closed)
        self.assertEqual(self.session_start('sess-n', 'term-a').strip(), '')

    def test_abandoned_unpublished_record_stays_untrusted_and_unresumable(self):
        draft, path = self.interrupted_successor()
        self.legacy('abandon', path)
        code, out = self.run_cli('resume', path, '--session', 'sess-r', terminal='term-r')
        self.assertNotEqual(out['outcome'], 'resumed', out)
        self.assertFalse(os.path.exists(path + '.published'))

class ReceiptLossTests(Base):
    """F2: a receipt-era transfer whose receipt is unusable is untraceable, not session-owned."""

    def resumed(self):
        code, saved = self.save(dict(topic='mine', description='Mine', body=BODY), 'sess-a', 'req-1', 'term-a')
        code, out = self.run_cli('resume', saved['path'], '--session', 'sess-r', terminal='term-r')
        self.assertEqual(out['outcome'], 'resumed', out)
        receipts = glob.glob(str(self.home / '.local/state/session-handoff/transfers/*.json'))
        self.assertEqual(len(receipts), 1)
        return saved['path'], receipts[0]

    def damage(self, receipt, how):
        if how == 'missing':
            os.unlink(receipt)
        elif how == 'corrupt':
            Path(receipt).write_text('{"nested": ' * 200 + '1' + '}' * 200)
        else:
            data = json.loads(Path(receipt).read_text())
            data['terminal'] = 'term-x'
            Path(receipt).write_text(json.dumps(data))

    def successor(self, session, terminal, request):
        return self.save(dict(topic='mine', description='Next', body=BODY + '\nNext.\n'), session, request, terminal)

    def test_unusable_receipt_blocks_holder_session_in_any_terminal_with_recovery(self):
        for how in ('missing', 'corrupt', 'altered'):
            with self.subTest(how=how):
                self.setUp()
                path, receipt = self.resumed()
                self.damage(receipt, how)
                before = self.checkpoints()
                for terminal in ('term-x', 'term-r'):
                    code, out = self.successor('sess-r', terminal, 'req-' + terminal)
                    self.assertEqual((code, out['outcome']), (1, 'conflict'), out)
                    self.assertIn('recover-transfer', out['recovery']['command'])
                    code, replay = self.run_cli('resume', path, '--session', 'sess-r', terminal=terminal)
                    self.assertNotEqual(replay['outcome'], 'resumed', replay)
                self.assertEqual(self.checkpoints(), before)

    def test_user_attested_recovery_restores_the_holder_after_receipt_loss(self):
        path, receipt = self.resumed()
        self.damage(receipt, 'missing')
        code, out = self.successor('sess-r', 'term-r', 'req-1')
        recovery = out['recovery']['command'].split()[2:]
        code, rec = self.run_cli(*recovery, '--session', 'sess-r', '--confirm-holder', terminal='term-r')
        self.assertEqual(rec['outcome'], 'recovered', rec)
        code, again = self.successor('sess-r', 'term-x', 'req-x')
        self.assertEqual(again['outcome'], 'conflict', again)
        code, ok = self.successor('sess-r', 'term-r', 'req-1')
        self.assertEqual(ok['outcome'], 'saved', ok)

    def test_valid_receipt_controls_holder_continues_foreign_terminal_refused(self):
        path, receipt = self.resumed()
        code, foreign = self.successor('sess-r', 'term-x', 'req-x')
        self.assertEqual(foreign['outcome'], 'conflict', foreign)
        code, own = self.successor('sess-r', 'term-r', 'req-r')
        self.assertEqual(own['outcome'], 'saved', own)

    def test_true_legacy_transfer_without_receipt_marker_keeps_session_compatibility(self):
        path, receipt = self.resumed()
        os.unlink(receipt)
        # A pre-receipt kit wrote only status/resumed/resumed_by; strip the receipt-era marker.
        text = Path(path).read_text()
        Path(path).write_text(''.join(l for l in text.splitlines(True) if not l.startswith('transfer_receipt:')))
        code, own = self.successor('sess-r', 'term-r', 'req-r')
        self.assertEqual(own['outcome'], 'saved', own)


if __name__ == '__main__':
    unittest.main()
