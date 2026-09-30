"""Exercise the launcher CLI with recording host executables and scratch config."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'skills/session-handoff/claude-auto'

class LauncherTests(unittest.TestCase):
    def test_launch_configures_hooks_and_preserves_literal_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'claude'
            config.mkdir()
            (config / 'settings.json').write_text(json.dumps({'model': 'existing'}))
            binary = root / 'bin'
            binary.mkdir()
            for name in ('claude', 'tmux'):
                exe = binary / name
                exe.write_text('#!/usr/bin/env python3\nimport os,sys,json\nfrom pathlib import Path\nPath(os.environ["CAPTURE"]).write_text(json.dumps({"args":sys.argv,"auto":os.getenv("HANDOFF_AUTO"),"max":os.getenv("HANDOFF_AUTO_MAX")}))\n')
                exe.chmod(0o755)
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config), CAPTURE=str(root / 'capture'),
                       PATH=str(binary)+os.pathsep+os.environ['PATH'], TMUX_PANE='%99')
            result = subprocess.run([str(CLI), '--max-clears', '3', '--', 'literal $(touch NEVER)'],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / 'capture').read_text())
            self.assertEqual(data['args'][1:], ['literal $(touch NEVER)'])
            self.assertEqual((data['auto'], data['max']), ('1', '3'))
            self.assertFalse((root / 'NEVER').exists())
            settings = json.loads((config / 'settings.json').read_text())
            self.assertEqual(settings['model'], 'existing')
            self.assertIn('Stop', settings['hooks'])
            self.assertTrue((config / 'skills/session-handoff/SKILL.md').is_file())
            env.pop('TMUX_PANE')
            result = subprocess.run([str(CLI), '--at', '120000', '--', 'literal $(touch NEVER)'],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / 'capture').read_text())
            self.assertEqual(data['args'][1:4], ['new-session', '-c', str(root.resolve())])
            import shlex
            child = shlex.split(data['args'][4])
            self.assertIn('HANDOFF_AT=120000', child)
            self.assertEqual(child[-1], 'literal $(touch NEVER)')
            result = subprocess.run([str(CLI), '--max-clears', '0'], cwd=root,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

if __name__ == '__main__':
    unittest.main()
