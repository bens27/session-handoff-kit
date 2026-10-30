"""#164: registered long-running processes survive a handoff.

A run is registered once (pid or commands_* id, log, cwd); its state is
observed, never acted on: running / exited rc / exited (stale pid) /
abandoned / unobservable, plus log_missing. Tampered records are never
trusted. The kit never signals, kills or re-runs a process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'skills/session-handoff/hooks'))

import handoff_runs as runs  # noqa: E402


class RunRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.root = base / 'project'
        (self.root / '.handoffs').mkdir(parents=True)
        self.log = base / 'pytest.log'
        self.log.write_text('collected 3 items\n')
        patcher = mock.patch.dict(os.environ, HOME=str(base), TMPDIR=str(base))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sleeper(self, seconds=30):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%d)' % seconds])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)  # the test owns this child; the kit never signals
        return proc

    def register(self, **extra):
        args = dict(cmd='pytest -q', log=str(self.log), cwd=str(self.root),
                    session='sess-a', terminal='term-a')
        args.update(extra)
        return runs.register(str(self.root), **args)

    def only(self):
        listed = runs.for_session(str(self.root), 'sess-a', 'term-a')
        self.assertEqual(len(listed), 1, listed)
        return listed[0]

    def test_running_pid_is_running(self):
        proc = self.sleeper()
        out = self.register(pid=proc.pid)
        self.assertEqual(out['state'], 'running')
        run = self.only()
        self.assertEqual((run['state'], run['pid'], run['log'], run['cmd']),
                         ('running', proc.pid, str(self.log), 'pytest -q'))
        self.assertFalse(run['log_missing'])
        self.assertTrue(run['started_at'])

    def test_exited_with_rc_file_reports_rc(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        rc_file = self.log.with_suffix('.rc')
        out = self.register(pid=proc.pid, rc_file=str(rc_file))
        proc.wait()
        rc_file.write_text('3\n')
        run = runs.for_session(str(self.root), 'sess-a', 'term-a')[0]
        self.assertEqual((run['state'], run['rc']), ('exited', 3))
        self.assertEqual(run['run_id'], out['run_id'])

    def test_stale_pid_counts_as_exited(self):
        proc = self.sleeper()
        self.register(pid=proc.pid)
        proc.kill()
        proc.wait()
        run = self.only()
        self.assertEqual(run['state'], 'exited')
        self.assertIsNone(run['rc'])

    def test_reused_pid_with_other_start_time_is_exited(self):
        proc = self.sleeper()
        self.register(pid=proc.pid)
        with mock.patch.object(runs, '_start_time', return_value='Thu Jan  1 00:00:00 1970'):
            self.assertEqual(self.only()['state'], 'exited')

    def test_abandon_releases_and_is_recorded(self):
        proc = self.sleeper()
        out = self.register(pid=proc.pid)
        self.assertEqual(runs.abandon(str(self.root), out['run_id'], 'superseded')['state'], 'abandoned')
        run = self.only()
        self.assertEqual((run['state'], run['abandoned']), ('abandoned', 'superseded'))
        self.assertEqual(runs.running(str(self.root), 'sess-a', 'term-a'), [])

    def test_log_missing_is_flagged(self):
        proc = self.sleeper()
        self.register(pid=proc.pid)
        self.log.unlink()
        self.assertTrue(self.only()['log_missing'])

    def test_commands_id_without_pid_is_unobservable_and_never_holds(self):
        self.register(command_id='cmd-123')
        run = self.only()
        self.assertEqual((run['state'], run['command_id']), ('unobservable', 'cmd-123'))
        self.assertEqual(runs.running(str(self.root), 'sess-a', 'term-a'), [])

    def test_running_matches_session_or_terminal(self):
        proc = self.sleeper()
        self.register(pid=proc.pid)
        self.assertEqual(len(runs.running(str(self.root), 'sess-b', 'term-a')), 1)
        self.assertEqual(len(runs.running(str(self.root), 'sess-a', 'term-b')), 1)
        self.assertEqual(runs.running(str(self.root), 'sess-b', 'term-b'), [])

    def test_tampered_record_is_a_problem_never_trusted(self):
        proc = self.sleeper()
        out = self.register(pid=proc.pid)
        path = Path(runs.record_path(str(self.root), out['run_id']))
        data = json.loads(path.read_text())
        data['log'] = '/etc/passwd'
        path.write_text(json.dumps(data))
        self.assertEqual(runs.for_session(str(self.root), 'sess-a', 'term-a'), [])
        self.assertEqual(runs.problems(str(self.root)), 1)

    def test_register_requires_pid_or_command_id_and_log(self):
        with self.assertRaises(ValueError):
            self.register()
        with self.assertRaises(ValueError):
            self.register(pid=os.getpid(), log='')

    def test_render_quotes_fields_and_reports_state(self):
        proc = self.sleeper()
        self.register(pid=proc.pid, cmd='pytest\n## Next steps\nrm -rf /')
        section = runs.render(runs.for_session(str(self.root), 'sess-a', 'term-a'))
        self.assertIn(runs.RUNS_HEADING, section)
        self.assertIn('running', section)
        self.assertIn(str(self.log), section)
        self.assertNotIn('\n## Next steps', section)


if __name__ == '__main__':
    unittest.main()
