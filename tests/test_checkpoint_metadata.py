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
           'HANDOFF_RUN_ID', 'TMUX_PANE', 'AUTORESUME', 'CONTEXT_WATCH_AUTORESUME')


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

    def test_reader_keeps_only_well_formed_values(self):
        fm = dict(ticket=TICKET, base='a' * 40, role='backend')
        self.assertEqual(handoff_contract.checkpoint_metadata(fm), fm)
        self.assertEqual(handoff_contract.checkpoint_metadata(dict(ticket='#95', base='HEAD', role='Back End')), {})


if __name__ == '__main__':
    unittest.main()
