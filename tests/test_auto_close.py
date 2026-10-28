"""#162: a choose/listing retires a local checkpoint whose ticket is DONE and whose
named commits are on origin/main, by a sealed `retired` line in the #159 notes
sidecar. The checkpoint file is never written. Any missing evidence (backlog
unavailable, unknown ticket, won't-fix, fetch failure, unresolvable or unlanded
commit, uncommitted work at save) leaves the checkpoint open."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / 'skills/session-handoff/hooks/handoff_ledger.py'
sys.path.insert(0, str(REPO / 'tests'))
from test_mcp_client import FakeServer  # noqa: E402

TICKET = '6cf33871-1ff9-4d27-a175-a869d775ee84'
TOKEN = 'sekrit-token-162'
BODY = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun the parser checks.\n'
DONE = {'status': 'done', 'wontFix': False}


class AutoCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.root = base / 'project'
        self.origin = base / 'origin.git'
        (base / 'skills').mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('GIT_') and k not in (
                        'CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID',
                        'HANDOFF_SKILL_ROOTS', 'CODEX_HOME', 'HANDOFF_AUTO', 'HANDOFF_RUN_ID', 'AGENTSROOM_MCP_SESSION')}
        self.env.update(HOME=str(base), TMPDIR=str(base), CONTEXT_WATCH_ORIGIN='test', CONTEXT_WATCH_LOG='0',
                        CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='',
                        HANDOFF_SKILL_ROOTS=str(base / 'skills'), GIT_AUTHOR_NAME='t',
                        GIT_AUTHOR_EMAIL='t@example.com', GIT_COMMITTER_NAME='t',
                        GIT_COMMITTER_EMAIL='t@example.com', GIT_CONFIG_NOSYSTEM='1',
                        GIT_TERMINAL_PROMPT='0', AGENTSROOM_MCP_SESSION=TOKEN)
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.origin)], env=self.env, check=True)
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('remote', 'add', 'origin', str(self.origin))
        # The fake server's .mcp.json must not make every save look dirty.
        Path(self.root, '.git/info/exclude').write_text('.mcp.json\n')
        self.commit('init')
        self.git('push', '-q', 'origin', 'main')

    # -- helpers ------------------------------------------------------------
    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def serve(self, tasks=None):
        server = FakeServer(tasks=tasks if tasks is not None else {TICKET: DONE})
        self.addCleanup(server.close)
        entry = {'type': 'http', 'url': server.url,
                 'headers': {'Authorization': 'Bearer ${AGENTSROOM_MCP_SESSION}'}}
        Path(self.root, '.mcp.json').write_text(json.dumps({'mcpServers': {'AgentsRoom-MCP': entry}}))
        return server

    def cli(self, *args, input=None, terminal='term-a'):
        env = dict(self.env, HANDOFF_TERMINAL_ID=terminal)
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=self.root, env=env,
                           input=input, capture_output=True, text=True, timeout=60)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return p.returncode, json.loads(p.stdout)

    def save(self, **extra):
        doc = dict(topic='parser', description='Parser', body=BODY, ticket=TICKET, **extra)
        code, out = self.cli('save', '.', '--session', 'sess-a', '--request-id', 'req-a', input=json.dumps(doc))
        self.assertEqual(out['outcome'], 'saved', out)
        return out['path']

    def lookup(self):
        return self.cli('lookup', '.', terminal='term-b')[1]

    def sidecar_lines(self, path):
        side = Path(path + '.notes.jsonl')
        return [json.loads(l) for l in side.read_text().splitlines() if l.strip()] if side.exists() else []

    def retired_lines(self, path):
        return [l for l in self.sidecar_lines(path) if l.get('retired')]

    def assertStillOpen(self, path, out):
        self.assertEqual(self.retired_lines(path), [], out)
        self.assertEqual(out['outcome'], 'available', out)
        self.assertEqual([i['path'] for i in out['items']], [path], out)
        self.assertFalse(out.get('auto_closed'), out)

    # -- the happy path -----------------------------------------------------
    def test_retires_when_ticket_done_and_commits_on_origin_main(self):
        self.serve()
        path = self.save()
        before = Path(path).read_bytes()
        out = self.lookup()
        self.assertEqual(out['outcome'], 'none', out)
        self.assertEqual([c['path'] for c in out.get('auto_closed', [])], [path], out)
        self.assertEqual(Path(path).read_bytes(), before)  # the .md is never written
        [line] = self.retired_lines(path)
        self.assertEqual(line['reason'], 'ticket-done')
        self.assertEqual(line['evidence']['ticket'], TICKET)
        self.assertEqual(line['evidence']['status'], 'done')
        self.assertEqual(line['evidence']['origin_main'], self.git('rev-parse', 'origin/main'))
        self.assertIn(self.git('rev-parse', 'HEAD'), line['evidence']['commits'])
        self.assertIsInstance(line.get('mac'), str)

    def test_retired_checkpoint_shows_closed_in_history(self):
        self.serve()
        path = self.save()
        self.lookup()
        code, out = self.cli('history', '.')
        [item] = [i for i in out['items'] if i['path'] == path]
        self.assertEqual(item['status'], 'retired', out)

    def test_retire_is_idempotent(self):
        self.serve()
        path = self.save()
        self.lookup()
        out = self.lookup()
        self.assertEqual(len(self.retired_lines(path)), 1)
        self.assertFalse(out.get('auto_closed'), out)  # already closed, nothing new to report

    def test_base_commit_must_also_be_on_origin_main(self):
        self.serve()
        local = self.commit('local only, never pushed')
        self.git('reset', '-q', '--hard', 'origin/main')
        path = self.save(base=local)
        self.assertStillOpen(path, self.lookup())

    # -- every missing piece of evidence is a no-op -------------------------
    def test_no_op_when_backlog_unavailable(self):
        path = self.save()  # no .mcp.json: the client cannot be configured
        self.assertStillOpen(path, self.lookup())

    def test_no_op_when_ticket_not_found(self):
        self.serve(tasks={})
        path = self.save()
        self.assertStillOpen(path, self.lookup())

    def test_no_op_when_wont_fix(self):
        for task in ({'status': 'done', 'wontFix': True}, {'status': 'wontfix', 'wontFix': False},
                     {'status': 'cancelled', 'wontFix': False}, {'status': 'in_progress', 'wontFix': False}):
            with self.subTest(task=task):
                self.setUp()
                self.serve(tasks={TICKET: task})
                path = self.save()
                self.assertStillOpen(path, self.lookup())

    def test_no_op_when_fetch_fails(self):
        self.serve()
        path = self.save()
        self.git('remote', 'set-url', 'origin', str(self.origin) + '-gone')
        self.assertStillOpen(path, self.lookup())

    def test_no_op_when_commit_not_on_origin_main(self):
        self.serve()
        self.commit('local work, not landed')
        path = self.save()
        self.assertStillOpen(path, self.lookup())

    def test_no_op_when_commit_unresolvable(self):
        self.serve()
        path = self.save()
        text = Path(path).read_text()
        # Simulate a checkpoint whose recorded sha is gone (e.g. saved in a deleted clone):
        # rewrite history so HEAD and origin/main no longer contain it.
        head = self.git('rev-parse', '--short', 'HEAD')
        self.assertIn('@' + head, text)
        self.git('checkout', '-q', '--orphan', 'fresh')
        self.commit('unrelated root')
        self.git('push', '-q', '-f', 'origin', 'fresh:main')
        self.git('branch', '-q', '-D', 'main')
        self.git('reflog', 'expire', '--expire=now', '--all')
        self.git('gc', '-q', '--prune=now')
        self.assertStillOpen(path, self.lookup())

    def test_no_op_when_save_had_uncommitted_work(self):
        self.serve()
        Path(self.root, 'wip.txt').write_text('not committed\n')
        path = self.save()
        self.assertStillOpen(path, self.lookup())

    def test_no_op_without_ticket(self):
        self.serve()
        doc = dict(topic='parser', description='Parser', body=BODY)
        code, out = self.cli('save', '.', '--session', 'sess-a', '--request-id', 'req-a', input=json.dumps(doc))
        path = out['path']
        self.assertStillOpen(path, self.lookup())

    def test_resume_never_auto_closes(self):
        self.serve()
        path = self.save()
        code, out = self.cli('resume', 'parser', '--session', 'sess-b', terminal='term-b')
        self.assertEqual(out['outcome'], 'resumed', out)
        self.assertEqual(self.retired_lines(path), [])

    # -- the sealed line ------------------------------------------------------
    def test_tampered_retired_line_is_a_problem_and_does_not_close(self):
        self.serve(tasks={})  # later lookups must not re-retire it
        path = self.save()
        sys.path.insert(0, str(LEDGER.parent))
        try:
            import handoff_notes
        finally:
            sys.path.remove(str(LEDGER.parent))
        side = Path(path + '.notes.jsonl')
        cid = next(l.split(':', 1)[1].strip() for l in Path(path).read_text().splitlines()
                   if l.startswith('checkpoint_id:'))
        side.write_text(json.dumps(dict(seq=0, retired=True, reason='ticket-done', evidence={},
                                        created='2026-10-28T00:00:00', checkpoint_id=cid, mac='0' * 64)) + '\n')
        old_home = os.environ.get('HOME')
        os.environ['HOME'] = self.env['HOME']
        try:
            state = handoff_notes.read(path, cid)
        finally:
            os.environ['HOME'] = old_home
        self.assertIsNone(state['retired'])
        self.assertEqual(state['problems'], 1)
        out = self.lookup()  # the forged line stays on disk but does not close the checkpoint
        self.assertEqual([i['path'] for i in out['items']], [path], out)

    def test_retired_line_never_delivered_even_with_reused_receipt(self):
        # The notes digest ignores retirement, so a receipt issued before the retire
        # still verifies; read() stays the only source of retired state and render()
        # must never show the retired line as a note.
        self.serve()
        path = self.save()
        code, first = self.cli('prepare', path, '--session', 'sess-a')
        self.assertEqual(first['outcome'], 'retrieved', first)
        self.lookup()
        self.assertEqual(len(self.retired_lines(path)), 1)
        code, again = self.cli('prepare', path, '--session', 'sess-a', '--reuse-receipt', first['delivery_receipt'])
        body = again.get('body', '')
        self.assertNotIn('## Updates since checkpoint', body, again)
        self.assertNotIn('ticket-done', json.dumps(again), again)
        self.assertFalse(again.get('note_problems'), again)

    def test_note_still_allowed_after_retire(self):
        self.serve()
        path = self.save()
        self.lookup()
        code, out = self.cli('note', path, '--text', 'Landed in 3b19e7e.', '--session', 'sess-a')
        self.assertEqual((code, out['outcome']), (0, 'noted'), out)
        self.assertEqual(len(self.retired_lines(path)), 1)


if __name__ == '__main__':
    unittest.main()
