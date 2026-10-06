"""Restart economics at the public ledger CLI and hook stdin/stdout seams."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
BODY = '## Objective\nFinish work.\n## Current state\nReady.\n## Next steps\nRun checks.\n'


class SessionBloatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('HANDOFF_', 'CONTEXT_WATCH_', 'AUTORESUME', 'AGENTSROOM_'))}
        self.env.update(HOME=self.tmp.name, TMPDIR=self.tmp.name, AGENTSROOM_AGENT_ID='terminal-a',
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', CONTEXT_WATCH_THINKING='0')
        self.clock = None

    def python_command(self, script, *args):
        if self.clock is None:
            return [sys.executable, str(script), *args]
        # Fake only the external wall clock; exercise the real CLI/hook code.
        bootstrap = '''import sys, runpy
from datetime import datetime
from unittest.mock import patch
sys.path.insert(0, sys.argv.pop(1))
import handoff_protocol, handoff_ledger
stamp = float(sys.argv.pop(1))
class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(stamp, tz)
handoff_protocol.datetime = handoff_ledger.datetime = Clock
sys.argv = sys.argv[1:]
with patch('time.time', return_value=stamp):
    runpy.run_path(sys.argv[0], run_name='__main__')
'''
        return [sys.executable, '-c', bootstrap, str(HOOKS), str(self.clock), str(script), *args]

    def cli(self, *args, doc=None, session='s1'):
        p = subprocess.run(self.python_command(HOOKS / 'handoff_ledger.py', *args, '--session', session),
                           input=json.dumps(doc) if doc else None, cwd=self.root, env=self.env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return json.loads(p.stdout)

    def hook(self, tokens, session='s1', event='PostToolUse', startup=31000):
        trace = self.root / (session + '.jsonl')
        trace.write_text('\n'.join(json.dumps({'message': {'model': 'claude-fable-5-1',
                         'usage': {'input_tokens': n}}}) for n in (startup, tokens)))
        p = subprocess.run(self.python_command(HOOKS / 'context_watch.py'),
                           input=json.dumps(dict(hook_event_name=event, session_id=session,
                                                 cwd=str(self.root), transcript_path=str(trace))),
                           cwd=self.root, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def save(self, session='s1', topic='work', body=BODY, **extra):
        return self.cli('save', doc=dict(topic=topic, description='Continue work', body=body, **extra), session=session)

    def test_quick_pressure_after_resume_saves_but_pauses_restart(self):
        self.hook(140000)
        first = self.save()
        self.assertEqual(first['transition']['kind'], 'agents_restart')
        self.cli('resume', first['path'], session='s2')
        notice = self.hook(140000, session='s2')
        second = self.save(session='s2')
        self.assertEqual(second['outcome'], 'saved')
        self.assertEqual(second['transition']['kind'], 'paused')
        self.assertIn('rapid-resume', second['transition']['diagnostics']['reasons'])
        self.assertIn('open', second['transition']['action'])
        self.assertNotIn('make your last action a call', notice)
        self.assertIn('paused', notice)
        startup = self.hook(31000, session='s3', event='SessionStart')
        self.assertIn('paused', startup)
        self.assertNotIn('Fully automatic:', startup)
        stopped = self.hook(140000, session='s2', event='Stop')
        self.assertIn('paused', stopped)
        self.assertNotIn('type /clear', stopped)
        self.assertEqual(self.cli('resume', second['path'], session='s3')['outcome'], 'resumed')

    def test_third_topic_save_pauses_without_any_resume(self):
        self.hook(140000)
        self.save(body=BODY + 'first\n')
        second = self.save(body=BODY + 'second\n')
        self.assertEqual(second['transition']['kind'], 'agents_restart')
        third = self.save(body=BODY + 'third\n')
        self.assertIn('repeated-topic', third['transition']['diagnostics']['reasons'])
        retry = self.save(body=BODY + 'third\n')
        self.assertEqual(retry['path'], third['path'])
        self.assertEqual(retry['transition']['kind'], 'paused')

    def test_host_budget_counts_restart_intents_and_is_terminal_scoped(self):
        self.hook(140000)
        parked = self.save(topic='parked', reason='user-parked')
        self.assertEqual(parked['transition']['kind'], 'agents_restart')
        for i in range(4):
            out = self.save(topic='work-%d' % i)
            self.assertEqual(out['transition']['kind'], 'agents_restart')
        sixth = self.save(topic='sixth')
        self.assertIn('restart-budget', sixth['transition']['diagnostics']['reasons'])
        self.env['AGENTSROOM_AGENT_ID'] = 'terminal-b'
        other = self.save(topic='other')
        self.assertEqual(other['transition']['kind'], 'agents_restart')

    def test_large_next_steps_are_delivered_once(self):
        steps = 'specific next action ' * 1000
        body = BODY.replace('Run checks.', steps)
        saved = self.save(body=body)
        out = self.cli('resume', saved['path'], session='s2')
        self.assertEqual(out['body'], body)
        self.assertFalse('next_step' in out)
        self.assertEqual(out['next_step_section'], 'Next steps')
        self.assertLess(len(json.dumps(out)), len(body) + 5000)

    def test_resume_reuses_delivered_body_references_and_loaded_skills(self):
        ref = self.root / 'notes.txt'
        ref.write_text('Required reference.\n')
        skill = Path(self.tmp.name) / '.agents/skills/review/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('Review the change carefully.\n')
        catalog = self.root / 'catalog.json'
        catalog.write_text(json.dumps(dict(skills={'review': str(skill)}, loaded=[str(skill)])))
        path = self.save(references=['notes.txt'], skills=['review'])['path']
        retrieved = self.cli('prepare', path, session='s2')
        out = self.cli('resume', path, '--catalog', str(catalog), '--reuse-receipt',
                       retrieved['delivery_receipt'], session='s2')
        self.assertFalse('body' in out)
        self.assertEqual(out['references'], [])
        self.assertEqual(out['skills'], [])
        self.assertGreater(out['metrics']['reused_bytes'], 0)
        self.assertEqual(out['metrics']['deduplicated_skills'], 1)
        self.assertIn('still in this session context', out['action'])

    def test_resume_reports_estimated_working_room_and_exact_delivery_size(self):
        self.env['HANDOFF_AT'] = '80000'
        self.hook(31000, session='s2')
        path = self.save()['path']
        out = self.cli('resume', path, session='s2')
        metrics = out['metrics']
        self.assertEqual(metrics['observed_occupancy_tokens'], 31000)
        self.assertEqual(metrics['handoff_threshold_tokens'], 80000)
        self.assertEqual(metrics['delivery_bytes'], len(json.dumps(out, ensure_ascii=False).encode()))
        self.assertLess(metrics['estimated_working_room_tokens'], 49000)
        self.assertTrue(any('working room' in w for w in out['warnings']))

    def test_pressure_notice_shows_assumptions_and_minimal_save_contract(self):
        notice = self.hook(140000)
        self.assertIn('window ~200,000 [assumed]', notice)
        self.assertIn('startup ~31,000', notice)
        self.assertIn('working room', notice)
        for heading in ('## Objective', '## Current state', '## Next steps'):
            self.assertIn(heading, notice)

    def test_expensive_reads_are_advisory_and_never_truncated(self):
        ref = self.root / 'long.md'
        ref.write_text('essential\n' * 100)
        body = BODY.replace('Run checks.', 'Re-read the whole long.md before starting work.')
        out = self.save(body=body, references=['long.md#L1-L100'])
        self.assertTrue(any('defer' in w for w in out['warnings']))
        resumed = self.cli('resume', out['path'], session='s2')
        self.assertEqual(resumed['references'][0]['text'], ref.read_text())

    def test_new_session_parking_respects_host_restart_cooldown(self):
        first = self.save(reason='user-parked')
        self.assertEqual(first['transition']['kind'], 'agents_restart')
        second = self.save(session='s2', topic='next-work', reason='user-parked')
        self.assertEqual(second['transition']['kind'], 'paused')
        self.assertIn('restart-cooldown', second['transition']['diagnostics']['reasons'])

    def test_normal_resume_and_expired_hour_budget_allow_restart(self):
        self.clock = time.time()
        self.hook(140000)
        first = self.save()
        self.cli('resume', first['path'], session='s2')
        self.clock += 601
        notice = self.hook(140000, session='s2')
        self.assertNotIn('automatic handoff paused (', notice)
        self.assertEqual(self.save(session='s2')['transition']['kind'], 'agents_restart')
        for i in range(3):
            self.save(session='s2', topic='other-%d' % i)
        self.assertEqual(self.save(session='s2', topic='budget-full')['transition']['kind'], 'paused')
        self.clock += 3601
        self.hook(140000, session='s3')
        self.assertEqual(self.save(session='s3', topic='budget-reset')['transition']['kind'], 'agents_restart')

    def test_lost_ephemeral_note_still_detects_rapid_resume(self):
        first = self.save()
        self.cli('resume', first['path'], session='s2')
        # Simulate cleanup of temporary hook notes; durable ledger remains.
        for note in Path(self.tmp.name).glob('context-watch-session-*.json'):
            note.unlink()
        self.hook(140000, session='s2')
        out = self.save(session='s2')
        self.assertIn('rapid-resume', out['transition']['diagnostics']['reasons'])

    def test_paused_tmux_stop_preserves_checkpoint(self):
        self.env.pop('AGENTSROOM_AGENT_ID')
        self.env.update(HANDOFF_AUTO='1', HANDOFF_TERMINAL_ID='tmux-a', TMUX_PANE='%fake')
        self.hook(140000)
        first = self.save()
        self.assertEqual(first['transition']['kind'], 'tmux')
        self.cli('resume', first['path'], session='s2')
        self.hook(140000, session='s2')
        second = self.save(session='s2')
        self.assertEqual(second['transition']['kind'], 'paused')
        self.assertIn('paused', self.hook(140000, session='s2', event='Stop'))
        self.assertIn('status: open', Path(second['path']).read_text())

    def test_headless_runner_stops_after_paused_save(self):
        self.env.pop('AGENTSROOM_AGENT_ID')
        child = self.root / 'child.py'
        child.write_text('''import json, os, subprocess, sys
from pathlib import Path
root = Path.cwd()
(root / 'runs.txt').open('a').write('run\\n')
ledger = %r
hook = %r
body = %r
def cli(*args, doc=None):
    p = subprocess.run([sys.executable, ledger, *args], input=json.dumps(doc) if doc else None,
                       capture_output=True, text=True, check=True)
    return json.loads(p.stdout)
def pressure(sid):
    trace = root / (sid + '.jsonl')
    trace.write_text('\\n'.join(json.dumps({'message': {'model': 'claude-test', 'usage': {'input_tokens': n}}}) for n in (31000, 140000)))
    subprocess.run([sys.executable, hook], input=json.dumps(dict(hook_event_name='PostToolUse', session_id=sid, cwd=str(root), transcript_path=str(trace))), capture_output=True, text=True, check=True)
pressure('first')
saved = cli('save', '--session', 'first', doc=dict(topic='work', description='work', body=body))
cli('resume', saved['path'], '--session', 'second')
pressure('second')
cli('save', '--session', 'second', doc=dict(topic='work', description='next', body=body + 'Later.\\n', predecessor=saved['path']))
''' % (str(HOOKS / 'handoff_ledger.py'), str(HOOKS / 'context_watch.py'), BODY))
        p = subprocess.run([sys.executable, str(HOOKS / 'context_watch.py'), 'auto', '--max', '3', '--',
                            sys.executable, str(child)], cwd=self.root, env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 75, p.stderr)
        self.assertIn('automatic handoff paused', p.stderr)
        self.assertEqual((self.root / 'runs.txt').read_text(), 'run\n')

    def test_invalid_or_cross_session_receipt_reloads_full_context(self):
        for token, session in (('invalid', 's2'), ('valid', 's3')):
            with self.subTest(token=token):
                ref = self.root / 'notes.txt'
                ref.write_text('Keep this context.\n')
                path = self.save(topic='reuse-' + token, references=['notes.txt'])['path']
                got = self.cli('prepare', path, session='s2')
                receipt = got['delivery_receipt'] if token == 'valid' else token
                out = self.cli('resume', path, '--reuse-receipt', receipt, session=session)
                self.assertEqual(out['body'], BODY)
                self.assertEqual(out['references'][0]['text'], ref.read_text())
                self.assertEqual(out['metrics']['reused_bytes'], 0)

    def test_filtered_resume_metrics_measure_only_delivered_json(self):
        path = self.save()['path']
        out = self.cli('resume', path, '--fields', 'metrics', session='s2')
        self.assertEqual(set(out), {'outcome', 'action', 'metrics'})
        self.assertEqual(out['metrics']['delivery_bytes'], len(json.dumps(out, ensure_ascii=False).encode()))

    def test_save_retry_after_transfer_does_not_restart_closed_checkpoint(self):
        first = self.save()
        self.cli('resume', first['path'], session='s2')
        retried = self.save()
        self.assertEqual(retried['path'], first['path'])
        self.assertEqual(retried['transition']['kind'], 'none')

    def test_working_room_uses_effective_startup_floor_threshold(self):
        self.hook(145000, session='s2', startup=140000)
        path = self.save()['path']
        out = self.cli('resume', path, session='s2')
        self.assertEqual(out['metrics']['handoff_threshold_tokens'], 180000)
        self.assertIn('raised to window', out['metrics']['threshold_source'])


if __name__ == '__main__':
    unittest.main()
