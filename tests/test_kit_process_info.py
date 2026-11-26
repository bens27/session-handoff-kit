"""G50: the kit's process-info for a TID is a bracketed, deterministic read.

herdr pane list must give exactly one pane with the TID's terminal_id; herdr pane
process-info reads that pane; herdr pane get confirms the pane id still maps to
the terminal_id. A mismatch retries once from the list, then fails. Processes are
annotated with the TID's sessions, matched by pid and exact kernel start (G52).
Every test runs with HOME in a temporary folder and a scripted fake Herdr that
only records what it was asked.

Run: python3 -m pytest -q tests/test_kit_process_info.py"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_terminal  # noqa: E402

TERM = 'term_aaa'
DROP = ('KIT_TERMINAL_ID', 'HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH', 'HERDR_BIN_PATH', 'TMUX', 'TMUX_PANE')

# Each command answers from its own queue in order; the last answer repeats.
FAKE = r'''import json, os, sys
d = os.environ['FAKE_HERDR_DIR']
with open(os.path.join(d, 'calls'), 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
script = json.load(open(os.path.join(d, 'script.json')))
key = sys.argv[2] if len(sys.argv) > 2 else ''
seen = os.path.join(d, 'n-' + key)
n = int(open(seen).read()) if os.path.exists(seen) else 0
open(seen, 'w').write(str(n + 1))
answers = script.get(key) or [None]
answer = answers[min(n, len(answers) - 1)]
if answer is None:
    sys.exit(1)
print(json.dumps(answer))
'''


def pane(pane_id, terminal_id=TERM):
    return {'pane_id': pane_id, 'terminal_id': terminal_id, 'agent_status': 'idle'}


def listing(*panes):
    return {'id': 'cli:pane:list', 'result': {'panes': list(panes)}}


def got(pane_id, terminal_id=TERM):
    return {'id': 'cli:pane:get', 'result': {'pane': pane(pane_id, terminal_id)}}


def info(pane_id, *pids):
    procs = [{'pid': p, 'name': 'claude', 'argv0': 'claude', 'cmdline': 'claude'} for p in pids]
    return {'id': 'cli:pane:process_info', 'result': {'process_info': {
        'pane_id': pane_id, 'shell_pid': 1, 'foreground_process_group_id': pids[0] if pids else 1,
        'foreground_processes': procs}, 'type': 'pane_process_info'}}


class ProcessInfoByTid(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name))
        self.fake = self.home / 'herdr'
        self.fake.mkdir()
        exe = self.fake / 'herdr'
        exe.write_text('#!%s\n%s' % (sys.executable, FAKE))
        exe.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k not in DROP}
        env.update(HOME=str(self.home), HERDR_BIN_PATH=str(exe), FAKE_HERDR_DIR=str(self.fake))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.me = os.getpid()
        self.start = list(handoff_terminal.proc_info(self.me)[1])
        self.tid = 'tid-' + 'a' * 32
        self.write_record(self.tid, herdr={'pane_id': 'w1:p1', 'terminal_id': TERM}, sessions=[
            {'kit_session': 'ks-claude', 'agent': 'claude', 'provider_session': 's-1', 'form': 'herdr',
             'process': {'pid': self.me, 'start': self.start}},
            {'kit_session': 'ks-reused', 'agent': 'codex', 'provider_session': 's-2', 'form': 'herdr',
             'process': {'pid': self.me, 'start': [self.start[0], self.start[1] + 1]}}])

    def write_record(self, tid, **fields):
        folder = self.home / '.context-watch' / 'terminals'
        folder.mkdir(parents=True, exist_ok=True)
        record = dict({'version': 1, 'tid': tid, 'kind': 'terminal', 'shell': {'pid': 1, 'start': [0, 0]}}, **fields)
        (folder / (tid + '.json')).write_text(json.dumps(record))

    def script(self, **answers):
        (self.fake / 'script.json').write_text(json.dumps({k.replace('_', '-'): v for k, v in answers.items()}))

    def calls(self):
        path = self.fake / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_one_pane_confirmed_names_its_sessions_by_pid_and_start(self):
        self.script(list=[listing(pane('w1:p1'), pane('w1:p2', 'term_other'))],
                    process_info=[info('w1:p1', self.me)], get=[got('w1:p1')])
        result = handoff_terminal.process_info(self.tid)
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['pane_id'], result['terminal_id']), ('w1:p1', TERM))
        [proc] = result['processes']
        self.assertEqual(proc['pid'], self.me)
        self.assertEqual(proc['sessions'], ['ks-claude'], 'a pid reused at another start is not this session')
        self.assertEqual([c[:2] for c in self.calls()], [['pane', 'list'], ['pane', 'process-info'], ['pane', 'get']])

    def test_pane_renumbered_between_calls_is_caught_and_reread(self):
        self.script(list=[listing(pane('w1:p1')), listing(pane('w2:p4'))],
                    process_info=[info('w1:p1', self.me), info('w2:p4', self.me)],
                    get=[got('w1:p1', 'term_someone_else'), got('w2:p4')])
        result = handoff_terminal.process_info(self.tid)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['pane_id'], 'w2:p4')
        self.assertEqual(sum(c[:2] == ['pane', 'list'] for c in self.calls()), 2)

    def test_second_mismatch_fails_instead_of_guessing(self):
        self.script(list=[listing(pane('w1:p1'))], process_info=[info('w1:p1', self.me)],
                    get=[got('w1:p1', 'term_someone_else')])
        result = handoff_terminal.process_info(self.tid)
        self.assertFalse(result['ok'])
        self.assertNotIn('processes', result)
        self.assertEqual(sum(c[:2] == ['pane', 'list'] for c in self.calls()), 2, 'retries once, not more')

    def test_process_info_from_another_pane_is_a_mismatch(self):
        self.script(list=[listing(pane('w1:p1'))], process_info=[info('w9:p9', self.me)], get=[got('w1:p1')])
        self.assertFalse(handoff_terminal.process_info(self.tid)['ok'])

    def test_zero_or_two_matching_panes_fail(self):
        for panes in ([pane('w1:p2', 'term_other')], [pane('w1:p1'), pane('w1:p3')]):
            with self.subTest(matches=len([p for p in panes if p['terminal_id'] == TERM])):
                self.script(list=[listing(*panes)], process_info=[info('w1:p1', self.me)], get=[got('w1:p1')])
                result = handoff_terminal.process_info(self.tid)
                self.assertFalse(result['ok'])
                self.assertIn('error', result)

    def test_unreadable_herdr_fails(self):
        self.script(list=[None])
        self.assertFalse(handoff_terminal.process_info(self.tid)['ok'])

    def test_tid_without_a_herdr_terminal_fails_without_calling_herdr(self):
        tid = 'tid-' + 'b' * 32
        self.write_record(tid, sessions=[])
        self.assertFalse(handoff_terminal.process_info(tid)['ok'])
        self.assertFalse(handoff_terminal.process_info('tid-' + 'c' * 32)['ok'], 'unknown TID')
        self.assertEqual(self.calls(), [])

    def test_herdr_is_only_read(self):
        self.script(list=[listing(pane('w1:p1'))], process_info=[info('w1:p1', self.me)], get=[got('w1:p1')])
        handoff_terminal.process_info(self.tid)
        self.assertTrue(self.calls())
        self.assertTrue(all(c[0] == 'pane' and c[1] in ('list', 'process-info', 'get') for c in self.calls()))

    def test_cli_prints_the_result_and_fails_nonzero(self):
        self.script(list=[listing(pane('w1:p1'))], process_info=[info('w1:p1', self.me)], get=[got('w1:p1')])
        run = [sys.executable, str(HOOKS / 'handoff_terminal.py'), 'process-info', self.tid]
        p = subprocess.run(run, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['pane_id'], 'w1:p1')
        self.script(list=[listing()])
        p = subprocess.run(run, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertFalse(json.loads(p.stdout)['ok'])


class SessionEntryNamesItsProcess(unittest.TestCase):
    """The session entry keeps its agent process (pid and exact start) so process-info can name it."""

    def test_record_session_stores_the_agent_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k not in DROP}
            env['HOME'] = os.path.realpath(tmp)
            with mock.patch.dict(os.environ, env, clear=True):
                entry = handoff_terminal.record_session({'session_id': 's-9'}, 'claude')
        pid, start = handoff_terminal._agent_anchor()
        self.assertEqual(entry['process'], {'pid': pid, 'start': list(start)})


if __name__ == '__main__':
    unittest.main()
