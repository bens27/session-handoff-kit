"""G9: without a wrapper id, the parent agent process anchors the terminal.

A session after /clear gets a new session id but keeps its claude/codex
process, so the process (pid plus start time) is the terminal that survives.
"""
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'skills/session-handoff/hooks'))
import handoff_identity  # noqa: E402
import handoff_protocol  # noqa: E402

START = 'Tue Nov 17 14:13:01 2026'
WRAPPERS = ('HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID', 'HERDR_ENV', 'HERDR_PANE_ID', 'HANDOFF_PROCESS_ANCHOR')


def table(*rows):
    """`ps -A -o pid=,ppid=,lstart=,comm=` rows: (pid, ppid, command)."""
    return '\n'.join('%6d %6d %s %s' % (pid, ppid, START, comm) for pid, ppid, comm in rows)


class Clean(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in WRAPPERS}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        handoff_identity._process_anchor_cache.clear()
        self.addCleanup(handoff_identity._process_anchor_cache.clear)

    def ps(self, text, pid=40):
        return mock.patch.multiple(handoff_identity, _process_table=mock.Mock(return_value=text),
                                   _own_pid=mock.Mock(return_value=pid))


class ProcessAnchorTests(Clean):
    def test_nearest_claude_ancestor_names_the_terminal(self):
        rows = table((1, 0, '/sbin/launchd'), (10, 1, '-zsh'), (20, 10, '/Users/x/.local/bin/claude'),
                     (30, 20, '/bin/zsh'), (40, 30, 'python3'))
        with self.ps(rows):
            first = handoff_identity.terminal_id()
        self.assertTrue(first.startswith('proc-claude-20-'), first)
        handoff_identity._process_anchor_cache.clear()
        with self.ps(rows.replace('/bin/zsh', '/bin/bash')):  # a later hook, same process
            self.assertEqual(handoff_identity.terminal_id(), first)

    def test_codex_ancestor_counts(self):
        with self.ps(table((1, 0, 'launchd'), (20, 1, 'codex'), (40, 20, 'python3'))):
            self.assertTrue(handoff_identity.terminal_id().startswith('proc-codex-20-'))

    def test_no_agent_ancestor_keeps_no_terminal(self):
        with self.ps(table((1, 0, 'launchd'), (10, 1, 'zsh'), (40, 10, 'python3'))):
            self.assertEqual(handoff_identity.terminal_id(), '')

    def test_wrapper_ids_still_win(self):
        os.environ['HANDOFF_TERMINAL_ID'] = 'tab-7'
        with self.ps(table((1, 0, 'launchd'), (20, 1, 'claude'), (40, 20, 'python3'))):
            self.assertEqual(handoff_identity.terminal_id(), 'tab-7')

    def test_opt_out_and_ps_failure_keep_no_terminal(self):
        os.environ['HANDOFF_PROCESS_ANCHOR'] = '0'
        with self.ps(table((1, 0, 'launchd'), (20, 1, 'claude'), (40, 20, 'python3'))):
            self.assertEqual(handoff_identity.terminal_id(), '')
        del os.environ['HANDOFF_PROCESS_ANCHOR']
        with self.ps(''):
            self.assertEqual(handoff_identity.terminal_id(), '')

    def test_pid_reuse_with_new_start_time_is_another_terminal(self):
        with self.ps(table((1, 0, 'launchd'), (20, 1, 'claude'), (40, 20, 'python3'))):
            first = handoff_identity.terminal_id()
        handoff_identity._process_anchor_cache.clear()
        reused = table((1, 0, 'launchd'), (20, 1, 'claude'), (40, 20, 'python3')).replace('14:13:01', '18:00:00')
        with self.ps(reused):
            self.assertNotEqual(handoff_identity.terminal_id(), first)


class AfterClearOwnershipTests(Clean):
    """The reported failure: after /clear, two open checkpoints and none marked mine."""

    def checkpoint(self, root, topic, session, terminal, age_s):
        created = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - age_s))
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(time.time() - age_s))
        path = root / '.handoffs' / ('%s-%s-0123456789ab.md' % (stamp, topic))
        fm = ['---', 'topic: %s' % topic, 'description: %s work' % topic, 'session_id: %s' % session,
              'created: %s' % created, 'status: open']
        if terminal:
            fm.append('terminal: %s' % terminal)
        path.write_text('\n'.join(fm + ['---', '## Objective', 'x', '## Next steps', '1. y', '']))
        os.utime(path, (time.time() - age_s,) * 2)
        return path

    def test_cleared_session_owns_the_checkpoint_its_process_saved(self):
        rows = table((1, 0, 'launchd'), (20, 1, 'claude'), (40, 20, 'python3'))
        with tempfile.TemporaryDirectory() as tmp, self.ps(rows):
            root = Path(os.path.realpath(tmp))
            (root / '.handoffs').mkdir()
            anchor = handoff_identity.terminal_id()
            mine = self.checkpoint(root, 'learnboard', 'old-session', anchor, 120)
            self.checkpoint(root, 'other-work', 'someone-else', 'proc-claude-99-1', 60)
            found = handoff_protocol.lookup(str(root), session='new-session-after-clear')
        self.assertEqual(found['outcome'], 'available', json.dumps(found)[:300])
        self.assertEqual([i['path'] for i in found['items'] if i['mine']], [str(mine)])
        self.assertEqual(found.get('selected'), str(mine))


class AfterClearEndToEndTests(unittest.TestCase):
    """Real `ps`: a shell symlinked as `claude` stands in for the agent process."""

    def run_as_claude(self, base, anchor):
        hooks = REPO / 'skills/session-handoff/hooks'
        (base / 'proj/.handoffs').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(base / 'proj')], check=True)
        os.symlink('/bin/sh', base / 'claude')
        body = '## Objective\nx\n## Current state\ny\n## Next steps\nz\n'
        script = base / 'run.sh'
        script.write_text('\n'.join([
            'cd "%s"' % (base / 'proj'),
            "printf '%%s' '%s' | HANDOFF_PROCESS_ANCHOR=0 \"$PY\" -B \"$L\" save . --session someone-else --request-id r0 >/dev/null"
            % json.dumps(dict(topic='other', description='Other', body=body)),
            "printf '%%s' '%s' | \"$PY\" -B \"$L\" save . --session before-clear --request-id r1 >/dev/null"
            % json.dumps(dict(topic='mine', description='Mine', body=body)),
            'echo \'{"prompt": "continue the handoff", "session": "after-clear"}\' | "$PY" -B "$L" dispatch --input -',
            'true', '']))
        env = {k: v for k, v in os.environ.items() if k not in WRAPPERS + ('CODEX_THREAD_ID', 'HANDOFF_SESSION_ID')}
        env.update(HOME=str(base), TMPDIR=str(base), CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_RESUME_LOG='0',
                   PY=sys.executable, L=str(hooks / 'handoff_ledger.py'))
        if not anchor:
            env['HANDOFF_PROCESS_ANCHOR'] = '0'
        out = subprocess.run([str(base / 'claude'), str(script)], env=env, capture_output=True, text=True, timeout=60)
        self.assertTrue(out.stdout.strip(), out.stderr)
        return json.loads(out.stdout)

    def test_cleared_session_resumes_its_own_checkpoint_not_a_foreign_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_as_claude(Path(os.path.realpath(tmp)), anchor=True)
        self.assertEqual((r['outcome'], r['code']), ('proceed', 'ok'), r)
        self.assertIn('-mine-', r['target']['path'])

    def test_without_the_anchor_the_cleared_session_owns_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_as_claude(Path(os.path.realpath(tmp)), anchor=False)
        self.assertEqual((r['outcome'], r['code']), ('ask', 'foreign_only'), r)


if __name__ == '__main__':
    unittest.main()
