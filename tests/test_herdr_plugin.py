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
import mandatory_gate  # noqa: E402  # on sys.path through the plugin's hooks import

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

    def test_pane_override_follows_the_terminal_across_a_move(self):
        panes = {'w1:p1': {'pane_id': 'w9:p2', 'terminal_id': 'term_one'},
                 'w9:p2': {'pane_id': 'w9:p2', 'terminal_id': 'term_one'},
                 'w1:p3': {'pane_id': 'w1:p3', 'terminal_id': 'term_two'}}
        with mock.patch.object(thresholds.handoff_identity, 'herdr_pane_info', lambda pane: dict(panes.get(pane, {}))):
            terminal = thresholds.target_identity('w1:p1')
            thresholds.save_pane(terminal, 'w1:p1', 80000, 90000)
            self.assertEqual(self.resolve('w9:p2'), (80000, 90000, 'terminal:at'))
            self.assertEqual(self.resolve('w1:p3'), (150000, 180000, 'config:claude'))

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

    def test_pane_save_writes_auto_controls_the_hook_honours(self):
        terminal = thresholds.target_identity('w1:p1')
        thresholds.save_pane(terminal, 'w1:p1', 80000, 90000, auto=True, auto_max=3)
        saved = json.loads(Path(thresholds.handoff_identity.terminal_settings_path(terminal)).read_text())
        self.assertEqual((saved['auto'], saved['auto_max'], saved['at']), (True, 3, 90000))
        cw, gate = thresholds.context_watch, mandatory_gate
        (self.project / '.context-watch.json').write_text('{"auto": false, "auto_max": 5}')
        self.assertEqual(cw.auto_max(str(self.project)), 3)  # pane above the project file
        self.assertIs(gate.auto_setting({'auto': False}), True)  # pane auto is HANDOFF_AUTO=1 for that pane
        with mock.patch.dict(os.environ, {'HANDOFF_AUTO': '0', 'HANDOFF_AUTO_MAX': '7'}):
            self.assertIs(gate.auto_setting({}), False)  # an explicit env still wins
            self.assertEqual(cw.auto_max(str(self.project)), 7)
        thresholds.clear_pane(terminal)
        self.assertEqual(cw.auto_max(str(self.project)), 5)
        self.assertIs(gate.auto_setting({'auto': False}), False)

    def test_save_global_writes_auto_controls_keeping_other_keys(self):
        thresholds.save_global(100000, 140000, auto=False, auto_max=4, subagent_at=80000)
        self.assertEqual(json.loads(self.user.read_text()),
                         {'auto': False, 'claude': 180000, 'at': 140000, 'warning_window_tokens': 40000,
                          'auto_max': 4, 'subagent_at': 80000})
        thresholds.target_identity('w1:p1')
        self.assertEqual(thresholds.context_watch.subagent_threshold(str(self.project)), 80000)
        self.assertEqual(thresholds.context_watch.auto_max(str(self.project)), 4)

    def test_settings_resolve_with_their_source(self):
        terminal = thresholds.target_identity('w1:p1')
        self.assertEqual(thresholds.resolve_settings(str(self.project), terminal),
                         {'auto': (True, 'all agents'), 'auto_max': (10, 'default'),
                          'subagent_at': (100000, 'default')})
        (self.project / '.context-watch.json').write_text('{"auto": false, "auto_max": 5, "subagent_at": 90000}')
        thresholds.save_pane(terminal, 'w1:p1', 80000, 90000, auto=True, auto_max=3)
        self.assertEqual(thresholds.resolve_settings(str(self.project), terminal),
                         {'auto': (True, 'this pane'), 'auto_max': (3, 'this pane'),
                          'subagent_at': (90000, 'project')})
        thresholds.clear_pane(terminal)
        self.assertEqual(thresholds.resolve_settings(str(self.project), terminal)['auto'], (False, 'project'))

    def test_keys_edit_auto_controls_and_subagent_at_is_global_only(self):
        state = thresholds.State.__new__(thresholds.State)
        state.scope, state.message = 'pane', ''
        state.warning, state.handoff, state.auto, state.auto_max, state.subagent_at = 85000, 90000, False, 10, 100000
        state.sources = {}
        for key in 'amMMuU':
            state.key(key)
        self.assertEqual((state.auto, state.auto_max, state.subagent_at), (True, 11, 100000))
        state.key('\t')
        state.key('u')
        self.assertEqual(state.subagent_at, 95000)
        for _ in range(11):
            state.key('m')
        self.assertEqual(state.auto_max, 1)
        lines = ''.join(state.setting_lines())
        for text in ('Auto', 'a ', 'Auto max', 'm/M', 'Subagent', 'u/U'):
            self.assertIn(text, lines)


if __name__ == '__main__':
    unittest.main()
