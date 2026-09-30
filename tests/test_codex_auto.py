"""Public launcher checks using an external Codex executable fixture."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'skills/session-handoff/codex-auto'

class LauncherTests(unittest.TestCase):
    def test_launch_and_child_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'bin'
            binary.mkdir()
            codex = binary / 'codex'
            codex.write_text('#!/usr/bin/env python3\nimport os,sys,json\nfrom pathlib import Path\nPath(os.environ["CAPTURE"]).write_text(json.dumps({"args":sys.argv[1:],"auto":os.getenv("HANDOFF_AUTO"),"threshold":os.getenv("HANDOFF_AT")}))\nsys.exit(int(os.getenv("CHILD_EXIT","0")))\n')
            codex.chmod(0o755)
            env = dict(os.environ, HOME=str(root), CODEX_HOME=str(root / 'profile'),
                       CAPTURE=str(root / 'capture'), PATH=str(binary)+os.pathsep+os.environ['PATH'])
            result = subprocess.run([str(CLI), '--at', '120000', '--', 'literal $(touch NEVER)'],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / 'capture').read_text())
            self.assertEqual(data['args'], ['exec', '--enable', 'hooks', '--sandbox', 'workspace-write', '--', 'literal $(touch NEVER)'])
            self.assertEqual((data['auto'], data['threshold']), ('1', '120000'))
            self.assertFalse((root / 'NEVER').exists())
            self.assertTrue((root / 'profile/skills/session-handoff/SKILL.md').is_file())
            self.assertIn('SessionStart', json.loads((root / 'profile/hooks.json').read_text())['hooks'])
            (root / 'capture').unlink()
            result = subprocess.run([str(CLI), '--setup'], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'capture').exists())
            result = subprocess.run([str(CLI), '--max-runs', '0'], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            env['CHILD_EXIT'] = '7'
            result = subprocess.run([str(CLI)], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(json.loads((root / 'capture').read_text())['args'][-2:], ['--', 'resume'])

if __name__ == '__main__':
    unittest.main()
