"""G48, G49, G52: the terminal registry.

A shell-start snippet runs `handoff_terminal.py register`, which writes
~/.context-watch/terminals/<TID>.json (shell pid and exact start, tty, and the
Herdr terminal only when that pane's shell is this shell or one of its
ancestors) and prints `export KIT_TERMINAL_ID=<TID>`. Each SessionStart adds a
session entry under the TID, but only when that shell is an ancestor of the
hook, compared as pid plus exact kernel start time; otherwise the session gets
a background TID anchored to its agent process. Every test runs with HOME in a
temporary folder and a fake Herdr binary.

Run: python3 -m pytest -q tests/test_terminal_registry.py"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / 'skills/session-handoff/hooks'
TERMINAL = HOOKS / 'handoff_terminal.py'
sys.path.insert(0, str(HOOKS))
import handoff_terminal  # noqa: E402

DROP = ('KIT_TERMINAL_ID', 'HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID', 'HERDR_ENV', 'HERDR_PANE_ID',
        'HERDR_SOCKET_PATH', 'HERDR_BIN_PATH', 'TMUX', 'TMUX_PANE', 'CODEX_THREAD_ID', 'HANDOFF_SESSION_ID')

# Records the session the hook process sees, as the SessionStart hook would.
RECORD = ('import json, sys; sys.path.insert(0, %s); import handoff_terminal as t; '
          'print(json.dumps(t.record_session(json.loads(sys.argv[1]), sys.argv[2])))' % json.dumps(str(HOOKS)))


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)
        self.env = {k: v for k, v in os.environ.items() if k not in DROP}
        self.env.update(HOME=str(self.home), TMPDIR=str(self.home))
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def registry(self):
        return self.home / '.context-watch' / 'terminals'

    def record_of(self, tid):
        return json.loads((self.registry() / (tid + '.json')).read_text())

    def sh(self, script, env=None):
        p = subprocess.run(['/bin/sh', '-c', script], env=dict(self.env, **(env or {})), capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return p.stdout

    def register_and_record(self, *sessions, env=None):
        """One shell: register, then run each (event, agent) as a hook child of that shell."""
        lines = ['eval "$(%s %s register --shell-pid $$)"' % (sys.executable, TERMINAL), 'echo "tid=$KIT_TERMINAL_ID"']
        for evt, agent in sessions:
            lines.append("%s -c '%s' '%s' %s" % (sys.executable, RECORD, json.dumps(evt), agent))
        out = self.sh('\n'.join(lines), env).splitlines()
        tid = out[0].split('=', 1)[1]
        return tid, [json.loads(line) for line in out[1:]]


class KernelProcInfo(unittest.TestCase):
    """G52: parent pid and exact start come from the kernel, never ps."""

    def test_own_process_reads_parent_and_microsecond_start(self):
        info = handoff_terminal.proc_info(os.getpid())
        self.assertIsNotNone(info)
        ppid, start = info
        self.assertEqual(ppid, os.getppid())
        self.assertEqual(len(start), 2)
        self.assertLessEqual(start[0], time.time() + 1)
        self.assertGreater(start[0], 1_000_000_000)

    def test_two_reads_agree_exactly(self):
        self.assertEqual(handoff_terminal.proc_info(os.getpid()), handoff_terminal.proc_info(os.getpid()))

    def test_missing_process_is_unreadable(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        self.assertIsNone(handoff_terminal.proc_info(child.pid))

    def test_ps_is_never_run(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('ps ran')), \
                mock.patch('subprocess.check_output', side_effect=AssertionError('ps ran')):
            self.assertTrue(handoff_terminal.is_ancestor(os.getppid(), handoff_terminal.proc_info(os.getppid())[1]))


class AncestryByPidAndStart(unittest.TestCase):
    """G52 against a fake kernel table: pid -> (ppid, (sec, usec))."""

    def walk(self, table, own, shell_pid, shell_start):
        with mock.patch.object(handoff_terminal, 'proc_info', side_effect=table.get), \
                mock.patch.object(handoff_terminal, '_own_pid', return_value=own):
            return handoff_terminal.is_ancestor(shell_pid, shell_start)

    def test_same_start_second_is_not_the_same_process(self):
        table = {40: (30, (1000, 900)), 30: (10, (1000, 800)), 10: (1, (1000, 2)), 1: (0, (5, 0))}
        self.assertTrue(self.walk(table, 40, 10, (1000, 2)))
        self.assertFalse(self.walk(table, 40, 10, (1000, 1)), 'pid 10 reused within the same second')

    def test_unreadable_link_is_no_match(self):
        table = {40: (30, (1000, 900)), 10: (1, (1000, 2))}  # 30 cannot be read
        self.assertFalse(self.walk(table, 40, 10, (1000, 2)))

    def test_depth_stops_at_64(self):
        table = {pid: (pid - 1, (1000, pid)) for pid in range(2, 200)}
        self.assertTrue(self.walk(table, 150, 150 - 63, (1000, 150 - 63)))
        self.assertFalse(self.walk(table, 150, 150 - 64, (1000, 150 - 64)))


class Register(Sandbox):
    """G48: the snippet writes the TID record and exports KIT_TERMINAL_ID."""

    def test_register_writes_the_shell_and_exports_the_tid(self):
        out = self.sh('%s %s register; echo "shell=$$"' % (sys.executable, TERMINAL)).splitlines()
        self.assertTrue(out[0].startswith('export KIT_TERMINAL_ID='), out)
        tid = out[0].split('=', 1)[1]
        record = self.record_of(tid)
        self.assertEqual(record['tid'], tid)
        self.assertEqual(record['shell']['pid'], int(out[1].split('=')[1]))
        self.assertEqual(len(record['shell']['start']), 2)
        self.assertEqual(record['sessions'], [])
        self.assertNotIn('herdr', record)

    def test_each_shell_gets_its_own_tid(self):
        first = self.sh('%s %s register' % (sys.executable, TERMINAL))
        second = self.sh('%s %s register' % (sys.executable, TERMINAL))
        self.assertNotEqual(first, second)

    def fake_herdr(self):
        """A herdr that reports pane P1 with terminal T9 and a shell pid read from a file."""
        pidfile = self.home / 'pane-shell'
        script = self.home / 'herdr'
        script.write_text('#!/bin/sh\ncase "$1 $2" in\n'
                          '  "pane get") echo \'{"result":{"pane":{"pane_id":"P1","terminal_id":"T9"}}}\';;\n'
                          '  "pane process-info") printf \'{"result":{"process_info":{"pane_id":"P1","shell_pid":%%s}}}\' '
                          '"$(cat %s)";;\n  *) exit 2;;\nesac\n' % pidfile)
        script.chmod(0o755)
        return dict(HERDR_ENV='1', HERDR_PANE_ID='P1', HERDR_BIN_PATH=str(script)), pidfile

    def test_herdr_terminal_only_when_the_pane_shell_contains_this_shell(self):
        env, pidfile = self.fake_herdr()
        script = 'echo $$ > %s; %s %s register' % (pidfile, sys.executable, TERMINAL)
        tid = self.sh(script, env).strip().split('=', 1)[1]
        self.assertEqual(self.record_of(tid)['herdr'], {'pane_id': 'P1', 'terminal_id': 'T9'})
        script = 'echo 1 > %s; %s %s register' % (pidfile, sys.executable, TERMINAL)  # pane shell is launchd
        tid = self.sh(script, env).strip().split('=', 1)[1]
        self.assertNotIn('herdr', self.record_of(tid), 'pid 1 is never a pane shell')

    def test_inherited_pane_id_whose_shell_is_elsewhere_is_not_recorded(self):
        env, pidfile = self.fake_herdr()
        other = subprocess.Popen(['/bin/sleep', '30'])
        self.addCleanup(other.kill)
        pidfile.write_text(str(other.pid))
        tid = self.sh('%s %s register' % (sys.executable, TERMINAL), env).strip().split('=', 1)[1]
        self.assertNotIn('herdr', self.record_of(tid))


class Sessions(Sandbox):
    """G49: session entries under the TID; untrusted hints become background TIDs."""

    def test_claude_then_codex_in_one_shell_share_the_tid(self):
        tid, (first, second) = self.register_and_record(
            ({'session_id': 'c-1', 'hook_event_name': 'SessionStart'}, 'claude'),
            ({'session_id': 'x-2', 'hook_event_name': 'SessionStart'}, 'codex'))
        self.assertEqual(first['tid'], tid)
        self.assertEqual(second['tid'], tid)
        sessions = self.record_of(tid)['sessions']
        self.assertEqual([(s['agent'], s['provider_session']) for s in sessions], [('claude', 'c-1'), ('codex', 'x-2')])
        self.assertEqual({s['form'] for s in sessions}, {'terminal'})
        self.assertEqual(len({s['kit_session'] for s in sessions}), 2)
        self.assertTrue(all(s['parent'] is None for s in sessions))

    def test_repeated_session_start_keeps_one_entry(self):
        evt = {'session_id': 'c-1', 'hook_event_name': 'SessionStart'}
        tid, _ = self.register_and_record((evt, 'claude'), (evt, 'claude'))
        self.assertEqual(len(self.record_of(tid)['sessions']), 1)

    def test_inherited_tid_outside_its_shell_gets_a_background_tid(self):
        tid = self.sh('%s %s register --shell-pid $$' % (sys.executable, TERMINAL)).strip().split('=', 1)[1]
        # A process that inherited KIT_TERMINAL_ID but is not under that shell (a bg job).
        out = subprocess.run([sys.executable, '-c', RECORD, json.dumps({'session_id': 'bg-s'}), 'claude'],
                             env=dict(self.env, KIT_TERMINAL_ID=tid), capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        entry = json.loads(out.stdout)
        self.assertNotEqual(entry['tid'], tid)
        self.assertTrue(entry['tid'].startswith('bg-'), entry)
        self.assertEqual(entry['form'], 'background')
        self.assertEqual(self.record_of(tid)['sessions'], [])
        background = self.record_of(entry['tid'])
        self.assertEqual([s['provider_session'] for s in background['sessions']], ['bg-s'])
        self.assertIn('anchor', background)

    def test_unknown_tid_or_none_gets_a_background_tid(self):
        for env in ({'KIT_TERMINAL_ID': 'tid-nope'}, {'KIT_TERMINAL_ID': '../escape'}, {}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                entry = handoff_terminal.record_session({'session_id': 's'}, 'claude')
                self.assertTrue(entry['tid'].startswith('bg-'), entry)
        self.assertFalse((self.home / '.context-watch' / 'escape.json').exists())

    def test_subagent_entry_names_its_parent(self):
        tid, (parent, child) = self.register_and_record(
            ({'session_id': 'c-1', 'hook_event_name': 'SessionStart'}, 'claude'),
            ({'session_id': 'c-1', 'agent_id': 'a-7', 'hook_event_name': 'SubagentStart'}, 'claude'))
        self.assertEqual(child['tid'], tid)
        self.assertEqual(child['form'], 'subagent')
        self.assertEqual(child['provider_session'], 'a-7')
        self.assertEqual(child['parent'], parent['kit_session'])

    def test_herdr_form_when_the_record_has_a_herdr_terminal(self):
        script = self.home / 'herdr'
        pidfile = self.home / 'pane-shell'
        script.write_text('#!/bin/sh\ncase "$1 $2" in\n'
                          '  "pane get") echo \'{"result":{"pane":{"pane_id":"P1","terminal_id":"T9"}}}\';;\n'
                          '  "pane process-info") printf \'{"result":{"process_info":{"shell_pid":%%s}}}\' '
                          '"$(cat %s)";;\nesac\n' % pidfile)
        script.chmod(0o755)
        env = dict(HERDR_ENV='1', HERDR_PANE_ID='P1', HERDR_BIN_PATH=str(script))
        lines = ['echo $$ > %s' % pidfile, 'eval "$(%s %s register --shell-pid $$)"' % (sys.executable, TERMINAL),
                 "%s -c '%s' '%s' claude" % (sys.executable, RECORD, json.dumps({'session_id': 'h-1'}))]
        entry = json.loads(self.sh('\n'.join(lines), env).splitlines()[-1])
        self.assertEqual(entry['form'], 'herdr')


class SessionStartHook(Sandbox):
    """context_watch's SessionStart adds the entry; a registry failure never breaks startup."""

    def test_session_start_records_a_background_entry(self):
        proj = self.home / 'proj'
        proj.mkdir()
        subprocess.run(['git', 'init', '-q', str(proj)], check=True)
        evt = dict(hook_event_name='SessionStart', source='startup', session_id='s-hook', cwd=str(proj))
        p = subprocess.run([sys.executable, '-B', str(HOOKS / 'context_watch.py')], input=json.dumps(evt),
                           env=dict(self.env, CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_JEV='0',
                                    HANDOFF_PROCESS_ANCHOR='0'),
                           cwd=str(proj), capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        found = [json.loads(f.read_text()) for f in self.registry().glob('bg-*.json')]
        self.assertEqual([[s['provider_session'] for s in r['sessions']] for r in found], [['s-hook']])


if __name__ == '__main__':
    unittest.main()
