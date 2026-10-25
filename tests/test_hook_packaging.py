"""Packaging must reject drift between plugin and installed hook registrations."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class HookPackagingTests(unittest.TestCase):
    def test_packaging_rejects_plugin_timeout_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / 'kit'
            shutil.copytree(REPO, checkout, symlinks=True, ignore=shutil.ignore_patterns(
                '.git', '.worktrees', '.agentsroom', '.handoffs', 'dist', '__pycache__'))
            hooks_path = checkout / 'plugins/session-handoff/hooks/hooks.json'
            hooks = json.loads(hooks_path.read_text())
            hooks['hooks']['PostToolUse'][0]['hooks'][0]['timeout'] = 21
            hooks_path.write_text(json.dumps(hooks, indent=2) + '\n')
            result = subprocess.run(
                [sys.executable, 'tests/verify-packaging.py'], cwd=checkout,
                env=dict(os.environ, CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_RESUME_LOG='0',
                         CONTEXT_WATCH_JEV='0', CONTEXT_WATCH_ORIGIN='test'),
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0,
                                'verify-packaging accepted a plugin timeout of 21 instead of 20')
            self.assertIn('FAIL hooks-json-generated', result.stdout)


if __name__ == '__main__':
    unittest.main()
