"""Herdr thresholds plugin: what it saves is what the hook reads."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'herdr-plugin'))

import thresholds  # noqa: E402

CLEARED = ('HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_AT', 'CONTEXT_WATCH_WARNING_WINDOW',
           'CONTEXT_WATCH_TOKENS', 'CONTEXT_WATCH_TOKENS_MAP', 'CONTEXT_WATCH_PERCENT',
           'HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH')


class ThresholdsPluginTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.project = self.home / 'project'
        self.project.mkdir()
        self.user = self.home / '.context-watch' / 'thresholds.json'
        self.user.parent.mkdir()
        self.user.write_text('{"auto": true, "claude": 180000}')
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update(HOME=str(self.home), HERDR_SOCKET_PATH='/a/herdr.sock')
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def resolve(self, pane):
        thresholds.target_identity(pane)
        return thresholds.resolve(str(self.project), 'claude-test', 200000)

    def test_save_global_keeps_other_keys(self):
        thresholds.save_global(100000, 140000)
        self.assertEqual(json.loads(self.user.read_text()),
                         {'auto': True, 'claude': 180000, 'at': 140000, 'warning_window_tokens': 40000})
        self.assertEqual(self.resolve('w1:p1'), (100000, 140000, 'config:at'))

    def test_pane_override_applies_to_that_pane_until_cleared(self):
        terminal = thresholds.target_identity('w1:p1')
        path = Path(thresholds.handoff_identity.terminal_settings_path(terminal))
        self.assertTrue(path.is_relative_to(self.home))
        thresholds.save_pane(terminal, 'w1:p1', 80000, 90000)
        self.assertEqual(self.resolve('w1:p1'), (80000, 90000, 'terminal:at'))
        self.assertEqual(self.resolve('w1:p2'), (150000, 180000, 'config:claude'))
        thresholds.clear_pane(terminal)
        thresholds.clear_pane(terminal)  # already gone
        self.assertEqual(self.resolve('w1:p1'), (150000, 180000, 'config:claude'))

    def test_bar_marks_both_thresholds(self):
        plain = thresholds.bar(44, 65000, 100000, 130000, 200000)
        for code in (thresholds.DIM, thresholds.BOLD, thresholds.RESET, thresholds.WARN, thresholds.HOT):
            plain = plain.replace(code, '')
        self.assertEqual((len(plain), plain.index('|'), plain.index('!')), (44, 22, 28))

    def test_warning_stays_at_or_below_handoff(self):
        state = thresholds.State.__new__(thresholds.State)
        state.warning, state.handoff = 85000, 90000
        state.adjust('warning', thresholds.STEP * 3)
        self.assertEqual(state.warning, 90000)
        state.adjust('handoff', -thresholds.STEP)
        self.assertEqual(state.handoff, 90000)


if __name__ == '__main__':
    unittest.main()
