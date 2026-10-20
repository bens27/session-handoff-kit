"""Item 4: CLI JSON is compact by default; --pretty restores indentation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks/handoff_ledger.py'


class CompactJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name).resolve())
        (Path(self.root) / '.handoffs').mkdir()
        self.env = dict(os.environ, HOME=self.root, TMPDIR=self.root, HANDOFF_TERMINAL_ID='term',
                        AGENTSROOM_AGENT_ID='', HANDOFF_SESSION_ID='s1', CODEX_THREAD_ID='')
        doc = json.dumps(dict(topic='work', description='d',
                              body='## Objective\nX\n## Current state\nY\n## Next steps\nZ\n'))
        p = subprocess.run([sys.executable, str(LEDGER), 'save'], input=doc,
                           env=self.env, cwd=self.root, text=True, capture_output=True, timeout=60)
        self.assertEqual(json.loads(p.stdout)['outcome'], 'saved', p.stdout + p.stderr)

    def run_cli(self, *args, stdin=None):
        p = subprocess.run([sys.executable, str(LEDGER)] + list(args), input=stdin, env=self.env,
                           cwd=self.root, text=True, capture_output=True, timeout=60)
        json.loads(p.stdout)
        return p.stdout

    def test_json_outputs_are_one_line_unless_pretty(self):
        cases = [
            ('dispatch', '--root', self.root),
            ('resolve', 'work', self.root, '--json'),
            ('new-path', 'work', self.root, '--json'),
            ('list', self.root, '--json'),
            ('hold', '--scope', 'project', '--reason', 'wait', '--root', self.root),
        ]
        request = json.dumps(dict(prompt='continue the handoff'))
        for args in cases:
            with self.subTest(args[0]):
                compact = self.run_cli(*args, stdin=request)
                self.assertEqual(compact.strip().count('\n'), 0, compact)
                pretty = self.run_cli(*(args + ('--pretty',)), stdin=request)
                self.assertGreater(pretty.strip().count('\n'), 0, pretty)
                if args[0] == 'hold':
                    self.run_cli('release', '--scope', 'project', '--root', self.root)
