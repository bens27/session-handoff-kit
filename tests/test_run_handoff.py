"""#164: run-register / run-abandon / run-list through the ledger CLI, and a
registered run carried from save to resume with its live state.

The kit observes runs only; these tests own (and kill) their child processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / 'skills/session-handoff/hooks/handoff_ledger.py'
BODY = '## Objective\nFix it.\n## Current state\nSuite running.\n## Next steps\nRead the suite log.\n'
HEADING = '## Registered processes'


class RunHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.root = base / 'project'
        (self.root / '.handoffs').mkdir(parents=True)
        (base / 'skills').mkdir()
        self.log = base / 'suite.log'
        self.log.write_text('collected 3 items\n')
        self.rc = base / 'suite.rc'
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID',
                                 'HANDOFF_SKILL_ROOTS', 'CODEX_HOME', 'HANDOFF_AUTO', 'HANDOFF_RUN_ID')}
        self.env.update(HOME=str(base), TMPDIR=str(base), CONTEXT_WATCH_ORIGIN='test', CONTEXT_WATCH_LOG='0',
                        CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='',
                        HANDOFF_SKILL_ROOTS=str(base / 'skills'))

    def cli(self, *args, input=None, terminal='term-a'):
        env = dict(self.env, HANDOFF_TERMINAL_ID=terminal)
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=self.root, env=env,
                           input=input, capture_output=True, text=True, timeout=60)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return p.returncode, json.loads(p.stdout)

    def sleeper(self, seconds=30):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%d)' % seconds])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def register(self, *extra, session='sess-a'):
        return self.cli('run-register', '--session', session, '--log', self.log, '--cmd', 'pytest -q',
                        '--cwd', self.root, *extra)

    def save(self, session='sess-a', request='req-a'):
        doc = dict(topic='suite', description='Suite', body=BODY)
        code, out = self.cli('save', '.', '--session', session, '--request-id', request, input=json.dumps(doc))
        self.assertEqual(out['outcome'], 'saved', out)
        return out['path']

    def resume(self):
        return self.cli('resume', 'suite', '--session', 'sess-b', terminal='term-b')[1]

    def test_register_list_abandon_cli(self):
        proc = self.sleeper()
        code, out = self.register('--pid', proc.pid, '--rc-file', self.rc)
        self.assertEqual((code, out['outcome'], out['state']), (0, 'registered', 'running'), out)
        run_id = out['run_id']
        code, listed = self.cli('run-list', '--session', 'sess-a')
        self.assertEqual((code, [r['run_id'] for r in listed['runs']]), (0, [run_id]), listed)
        self.assertEqual(listed['runs'][0]['state'], 'running')
        code, out = self.cli('run-abandon', run_id, '--reason', 'moved to CI', '--session', 'sess-a')
        self.assertEqual((code, out['outcome'], out['run']['state']), (0, 'abandoned', 'abandoned'), out)
        self.assertIsNone(proc.poll(), 'abandon must never touch the process')

    def test_register_needs_pid_or_command_id(self):
        code, out = self.register()
        self.assertEqual((code, out['outcome']), (1, 'invalid'), out)
        code, out = self.register('--command-id', 'cmd-42')
        self.assertEqual((code, out['outcome'], out['state']), (0, 'registered', 'unobservable'), out)

    def test_abandon_unknown_run_is_invalid(self):
        code, out = self.cli('run-abandon', 'run-0000000000000000', '--reason', 'x', '--session', 'sess-a')
        self.assertEqual((code, out['outcome']), (1, 'invalid'), out)

    def test_save_snapshots_runs_and_resume_delivers_live_state(self):
        proc = self.sleeper()
        run_id = self.register('--pid', proc.pid, '--rc-file', self.rc)[1]['run_id']
        path = self.save()
        text = Path(path).read_text()
        self.assertIn(run_id, text.split('\n---', 1)[0], 'save must snapshot the run in frontmatter')
        out = self.resume()
        self.assertEqual(out['outcome'], 'resumed', out)
        body = out['body']
        self.assertIn(HEADING, body)
        self.assertIn('%s: running' % run_id, body)
        self.assertIn('> log: %s' % self.log, body)
        self.assertEqual(out.get('runs'), 1, out)

    def test_run_that_exited_after_save_is_delivered_live_not_as_snapshot(self):
        proc = self.sleeper()
        run_id = self.register('--pid', proc.pid, '--rc-file', self.rc)[1]['run_id']
        self.save()
        proc.kill()
        proc.wait()
        self.rc.write_text('0\n')
        body = self.resume()['body']
        self.assertIn('%s: exited rc=0' % run_id, body)
        self.assertNotIn('%s: running' % run_id, body)

    def test_snapshot_survives_a_lost_registry(self):
        proc = self.sleeper()
        run_id = self.register('--pid', proc.pid)[1]['run_id']
        self.save()
        runs_dir = self.root / '.handoffs' / 'runs'
        for f in runs_dir.iterdir():
            f.unlink()
        body = self.resume()['body']
        self.assertIn('%s: running' % run_id, body)

    def test_abandoned_run_is_not_snapshotted(self):
        proc = self.sleeper()
        run_id = self.register('--pid', proc.pid)[1]['run_id']
        self.cli('run-abandon', run_id, '--reason', 'not needed', '--session', 'sess-a')
        self.save()
        self.assertNotIn(HEADING, self.resume()['body'])

    def test_save_without_runs_adds_no_section(self):
        self.save()
        self.assertNotIn(HEADING, self.resume()['body'])


if __name__ == '__main__':
    unittest.main()
