"""Checkpoints stay out of Git by default: a save keeps .handoffs/.gitignore in place."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / 'skills/session-handoff/hooks/handoff_ledger.py'
BODY = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun the parser checks.\n'
CLEARED = ('CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID', 'HANDOFF_AUTO',
           'HANDOFF_RUN_ID', 'TMUX_PANE', 'AUTORESUME', 'CONTEXT_WATCH_AUTORESUME', 'HERDR_ENV', 'HERDR_PANE_ID')


class HandoffsGitignoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / 'tmp').mkdir()
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        self.env.update(HOME=self.tmp.name, TMPDIR=str(Path(self.tmp.name) / 'tmp'), CONTEXT_WATCH_ORIGIN='test',
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, env=self.env)

    def save(self):
        doc = dict(topic='work', description='About work', body=BODY)
        p = subprocess.run([sys.executable, str(LEDGER), 'save', '--session', 's1'], cwd=self.root,
                           env=self.env, input=json.dumps(doc), capture_output=True, text=True, timeout=30)
        saved = json.loads(p.stdout)
        self.assertEqual(saved['outcome'], 'saved', saved)
        return Path(saved['path'])

    def ignored(self, path):
        return subprocess.run(['git', 'check-ignore', '-q', str(path)], cwd=self.root, env=self.env).returncode == 0

    def test_without_handoffs_dir_save_stays_outside_repo(self):
        path = self.save()
        self.assertFalse(str(path).startswith(str(self.root.resolve()) + os.sep), path)
        self.assertFalse((self.root / '.handoffs').exists())

    def test_existing_handoffs_dir_gains_ignore_file(self):
        (self.root / '.handoffs').mkdir()
        path = self.save()
        self.assertEqual(path.parent, (self.root / '.handoffs').resolve())
        self.assertEqual((self.root / '.handoffs' / '.gitignore').read_text(), '*\n')
        self.assertTrue(self.ignored(path))
        self.assertTrue(self.ignored(str(path) + '.published'))

    def test_existing_ignore_file_is_left_alone(self):
        (self.root / '.handoffs').mkdir()
        ignore = self.root / '.handoffs' / '.gitignore'
        ignore.write_text('')  # documented opt-out: commit handoffs
        path = self.save()
        self.assertEqual(ignore.read_text(), '')
        self.assertFalse(self.ignored(path))


if __name__ == '__main__':
    unittest.main()
