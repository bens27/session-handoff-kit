"""Checkpoint header metadata: validated ticket:/base:/role: lines (SPEC §7.1)."""
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
sys.path.insert(0, str(HOOKS))
import handoff_contract  # noqa: E402

BODY = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun the parser checks.\n'
TICKET = '6cf33871-1ff9-4d27-a175-a869d775ee84'
CLEARED = ('CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID', 'HANDOFF_AUTO',
           'HANDOFF_RUN_ID', 'TMUX_PANE', 'AUTORESUME', 'CONTEXT_WATCH_AUTORESUME', 'AGENTSROOM_MCP_SESSION')


class CheckpointMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / 'tmp').mkdir()
        self.root = Path(self.tmp.name) / 'project'
        (self.root / '.handoffs').mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k not in CLEARED and not k.startswith('GIT_')}
        self.env.update(HOME=self.tmp.name, TMPDIR=str(Path(self.tmp.name) / 'tmp'), CONTEXT_WATCH_ORIGIN='test',
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def init_repo(self):
        self.git('init', '-q')
        (self.root / 'a.txt').write_text('a\n')
        self.git('add', 'a.txt')
        self.git('commit', '-q', '-m', 'init')
        return self.git('rev-parse', 'HEAD')

    def run_cli(self, *args, input=None):
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=self.root, env=self.env,
                           input=input, capture_output=True, text=True, timeout=30)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return p.returncode, json.loads(p.stdout)

    def save(self, session='s1', **doc):
        doc = dict(dict(topic='work', description='About work', body=BODY), **doc)
        return self.run_cli('save', '--session', session, input=json.dumps(doc))

    def frontmatter(self, path):
        head = Path(path).read_text().split('\n---\n', 1)[0]
        return dict(line.split(': ', 1) for line in head.splitlines()[1:] if ': ' in line)

    def checkpoints(self):
        return sorted(p for p in (self.root / '.handoffs').glob('*.md'))

    def test_save_writes_validated_ticket_base_and_role(self):
        head = self.init_repo()
        code, out = self.save(ticket=TICKET, base=head[:9], role='backend')
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        fm = self.frontmatter(out['path'])
        self.assertEqual((fm['ticket'], fm['base'], fm['role']), (TICKET, head, 'backend'))

    def test_short_ticket_id_is_accepted(self):
        code, out = self.save(ticket='6cf33871')
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        self.assertEqual(self.frontmatter(out['path'])['ticket'], '6cf33871')

    def test_invalid_values_are_refused_before_anything_is_written(self):
        head = self.init_repo()
        cases = [
            ('ticket', '#95'), ('ticket', '6CF33871'), ('ticket', '6cf3387'), ('ticket', TICKET + 'x'),
            ('ticket', 95), ('base', 'HEAD'), ('base', 'abc12'), ('base', 'f' * 40), ('base', head + '\n'),
            ('role', 'Backend'), ('role', 'role:backend'), ('role', 'a' * 41), ('role', 'back end'),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                code, out = self.save(**{key: value})
                self.assertEqual((code, out['outcome']), (1, 'invalid'), out)
                self.assertIn(key, out['action'])
                self.assertEqual(self.checkpoints(), [])

    def test_base_outside_a_git_repository_is_refused(self):
        code, out = self.save(base='a' * 40)
        self.assertEqual((code, out['outcome']), (1, 'invalid'), out)
        self.assertIn('base', out['action'])
        self.assertEqual(self.checkpoints(), [])

    def test_absent_metadata_writes_no_lines_and_resumes(self):
        code, out = self.save()
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        fm = self.frontmatter(out['path'])
        self.assertFalse({'ticket', 'base', 'role'} & set(fm))
        self.assertEqual(handoff_contract.checkpoint_metadata(fm), {})
        code, resumed = self.run_cli('resume', out['path'], '--session', 's2')
        self.assertEqual((code, resumed['outcome']), (0, 'resumed'), resumed)

    def test_checkpoint_with_metadata_resumes(self):
        head = self.init_repo()
        code, out = self.save(ticket=TICKET, base=head, role='backend')
        self.assertEqual(out['outcome'], 'saved', out)
        code, resumed = self.run_cli('resume', out['path'], '--session', 's2')
        self.assertEqual((code, resumed['outcome']), (0, 'resumed'), resumed)

    def test_retry_of_same_draft_returns_same_checkpoint(self):
        head = self.init_repo()
        draft = dict(ticket=TICKET, base=head[:8], role='backend')
        first = self.save(**draft)[1]
        again = self.save(**draft)[1]
        self.assertEqual((again['outcome'], again['path']), ('saved', first['path']), again)
        self.assertEqual(len(self.checkpoints()), 1)

    def test_successor_inherits_ticket_and_role_but_not_base(self):
        head = self.init_repo()
        first = self.save(ticket=TICKET, base=head, role='backend')[1]
        second = self.save(description='Later', body=BODY + 'Later.\n', predecessor=first['path'])[1]
        self.assertEqual(second['outcome'], 'saved', second)
        fm = self.frontmatter(second['path'])
        self.assertEqual((fm.get('ticket'), fm.get('role'), fm.get('base')), (TICKET, 'backend', None))
        third = self.save(description='Cleared', body=BODY + 'Cleared.\n', predecessor=second['path'],
                          ticket='', role='')[1]
        self.assertEqual(third['outcome'], 'saved', third)
        self.assertFalse({'ticket', 'role'} & set(self.frontmatter(third['path'])))

    def test_retry_finishes_after_base_commit_disappears(self):
        self.init_repo()
        (self.root / 'a.txt').write_text('b\n')
        self.git('commit', '-qam', 'two')
        second = self.git('rev-parse', 'HEAD')
        draft = dict(ticket=TICKET, base=second[:8])
        first = self.save(**draft)[1]
        self.assertEqual(first['outcome'], 'saved', first)
        os.remove(first['path'] + '.published')  # interrupted before the receipt
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.git('reflog', 'expire', '--expire=now', '--all')
        self.git('gc', '-q', '--prune=now')
        self.assertNotEqual(subprocess.run(['git', '-C', str(self.root), 'cat-file', '-e', second],
                                           env=self.env).returncode, 0)
        code, again = self.save(**draft)
        self.assertEqual((code, again['outcome'], again.get('path')), (0, 'saved', first['path']), again)
        self.assertTrue(os.path.exists(first['path'] + '.published'))

    def test_base_naming_a_tag_or_ref_instead_of_a_commit_is_refused(self):
        head = self.init_repo()
        self.git('tag', '-a', '-m', 'v1', 'v1')
        tag = self.git('rev-parse', 'v1')
        self.assertNotEqual(tag, head)
        self.git('branch', 'deadbeef')  # a hex-looking ref name that git resolves before short shas
        for value in (tag, tag[:8], 'deadbeef'):
            with self.subTest(base=value):
                code, out = self.save(base=value)
                self.assertEqual((code, out['outcome']), (1, 'invalid'), out)
                self.assertIn('base', out['action'])
                self.assertEqual(self.checkpoints(), [])

    def test_inherited_values_pass_through_the_reader(self):
        prior = dict(ticket=TICKET, role='Bad Role')
        doc = handoff_contract.continuation_document(dict(topic='t'), prior)
        self.assertEqual((doc['ticket'], doc['role']), (TICKET, ''))

    def test_spec_reader_snippet_matches_parse_front_matter(self):
        import re
        import handoff_identity
        spec = (REPO / 'SPEC.md').read_text()
        snippet = re.search(r'\*\*Header format\.\*\*.*?```python\n(.*?)```', spec, re.S).group(1)
        for text in ('---\ntopic: a\nBase: x: y\n  ---  \nbody', '---\ntopic: a\n', 'no header', '',
                     ' --- \nrole: r\n---\n', '---\n---\n'):
            with self.subTest(text=text):
                scope = dict(text=text)
                exec(snippet, scope)
                self.assertEqual(scope['fm'], handoff_identity.parse_front_matter(text))

    def test_reader_keeps_only_well_formed_values(self):
        fm = dict(ticket=TICKET, base='a' * 40, role='backend')
        self.assertEqual(handoff_contract.checkpoint_metadata(fm), fm)
        self.assertEqual(handoff_contract.checkpoint_metadata(dict(ticket='#95', base='HEAD', role='Back End')), {})


    def test_resume_reports_git_only_staleness_when_backlog_is_offline(self):
        head = self.init_repo()
        out = self.save(ticket=TICKET, base=head)[1]
        self.git('commit', '-q', '--allow-empty', '-m', 'landed\n\nTicket: ' + TICKET)
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        code, resumed = self.run_cli('resume', out['path'], '--session', 's2')
        self.assertEqual((code, resumed['outcome']), (0, 'resumed'), resumed)
        verdict = resumed['staleness']
        self.assertEqual((verdict['verdict'], verdict['source']), ('progressed', 'git-only'), verdict)
        self.assertIn('AGENTSROOM_MCP_SESSION', verdict['backlog'])
        self.assertTrue(verdict['reason'])
        self.assertTrue(resumed['action'].startswith('Staleness progressed (git-only): '), resumed['action'])

    def test_resume_reports_backlog_staleness_and_never_prints_the_token(self):
        from test_mcp_client import FakeServer
        server = FakeServer(tasks={TICKET: {'status': 'done'}})
        self.addCleanup(server.close)
        token = 'tok-' + 'z' * 20
        (self.root / '.mcp.json').write_text(json.dumps({'mcpServers': {'AgentsRoom-MCP': {'url': server.url}}}))
        head = self.init_repo()
        out = self.save(ticket=TICKET, base=head)[1]
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.env['AGENTSROOM_MCP_SESSION'] = token
        p = subprocess.run([sys.executable, str(LEDGER), 'resume', out['path'], '--session', 's2'], cwd=self.root,
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertNotIn(token, p.stdout + p.stderr)
        resumed = json.loads(p.stdout)
        self.assertEqual(resumed['outcome'], 'resumed', resumed)
        self.assertEqual((resumed['staleness']['verdict'], resumed['staleness']['source']),
                         ('superseded', 'git+backlog'), resumed['staleness'])

    def test_resume_without_metadata_is_unclassified(self):
        out = self.save()[1]
        resumed = self.run_cli('resume', out['path'], '--session', 's2')[1]
        self.assertEqual(resumed['staleness']['verdict'], 'unclassified', resumed)


if __name__ == '__main__':
    unittest.main()
