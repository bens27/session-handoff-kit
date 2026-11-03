"""Terminal identity: which environment names a terminal, and how it is normalized."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))

import context_watch  # noqa: E402
import handoff_identity  # noqa: E402
import handoff_protocol  # noqa: E402
import watch_decision  # noqa: E402

CLEARED = ('HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID', 'CODEX_THREAD_ID', 'HANDOFF_SESSION_ID',
           'HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH', 'TMUX_PANE')


def environment(**values):
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    env.update(values)
    return mock.patch.dict(os.environ, env, clear=True)


class TerminalIdentityTests(unittest.TestCase):
    def test_explicit_terminal_id_wins_over_agentsroom_agent_id(self):
        with environment(HANDOFF_TERMINAL_ID='tmux-abc', AGENTSROOM_AGENT_ID='agent-1'):
            self.assertEqual(handoff_protocol.terminal_id(), 'tmux-abc')

    def test_blank_explicit_terminal_id_falls_back_to_agentsroom_agent_id(self):
        with environment(HANDOFF_TERMINAL_ID='  \n ', AGENTSROOM_AGENT_ID=' agent-1 '):
            self.assertEqual(handoff_protocol.terminal_id(), 'agent-1')

    def test_no_identity_is_empty(self):
        with environment(HANDOFF_TERMINAL_ID=' ', AGENTSROOM_AGENT_ID=''):
            self.assertEqual(handoff_protocol.terminal_id(), '')

    def test_herdr_pane_names_the_terminal_per_server(self):
        with environment(HERDR_ENV='1', HERDR_PANE_ID='w5:p7', HERDR_SOCKET_PATH='/a/herdr.sock'):
            one = handoff_protocol.terminal_id()
        with environment(HERDR_ENV='1', HERDR_PANE_ID='w5:p7', HERDR_SOCKET_PATH='/b/herdr.sock'):
            other_server = handoff_protocol.terminal_id()
        self.assertRegex(one, r'^herdr-[0-9a-f]{8}-w5:p7$')
        self.assertNotEqual(one, other_server)
        self.assertEqual(one, handoff_identity.herdr_terminal_id('w5:p7', '/a/herdr.sock'))

    def test_herdr_pane_ranks_below_explicit_and_agentsroom_ids(self):
        herdr = dict(HERDR_ENV='1', HERDR_PANE_ID='w5:p7', HERDR_SOCKET_PATH='/a/herdr.sock')
        with environment(HANDOFF_TERMINAL_ID='runner-1', **herdr):
            self.assertEqual(handoff_protocol.terminal_id(), 'runner-1')
        with environment(AGENTSROOM_AGENT_ID='agent-1', **herdr):
            self.assertEqual(handoff_protocol.terminal_id(), 'agent-1')
        with environment(HERDR_ENV='0', HERDR_PANE_ID='w5:p7'):
            self.assertEqual(handoff_protocol.terminal_id(), '')

    def test_short_identity_is_kept_verbatim(self):
        value = 'agent-' + 'x' * 114  # exactly 120 characters
        with environment(AGENTSROOM_AGENT_ID=value):
            self.assertEqual(handoff_protocol.terminal_id(), value)

    def test_long_identities_sharing_a_prefix_stay_distinct_and_bounded(self):
        prefix = 'agent-' + 'p' * 130
        with environment(AGENTSROOM_AGENT_ID=prefix + '-one'):
            one = handoff_protocol.terminal_id()
        with environment(AGENTSROOM_AGENT_ID=prefix + '-two'):
            two = handoff_protocol.terminal_id()
        with environment(AGENTSROOM_AGENT_ID=prefix + '-one'):
            again = handoff_protocol.terminal_id()
        self.assertNotEqual(one, two)
        self.assertEqual(one, again)
        self.assertLessEqual(max(len(one), len(two)), 120)
        self.assertTrue(one.startswith(prefix[:100]))

    def test_same_role_agentsroom_tabs_keep_separate_terminal_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            (root / '.handoffs').mkdir(parents=True)
            with mock.patch.object(tempfile, 'tempdir', tmp):
                with environment(AGENTSROOM_AGENT_ID='agent-100-backend'):
                    context_watch.write_terminal_pointer(str(root), 'session-a')
                with environment(AGENTSROOM_AGENT_ID='agent-200-backend'):
                    context_watch.write_terminal_pointer(str(root), 'session-b')
                with environment(AGENTSROOM_AGENT_ID='agent-100-backend'):
                    self.assertEqual(handoff_protocol.terminal_session(str(root)), 'session-a')
                with environment(AGENTSROOM_AGENT_ID='agent-200-backend'):
                    self.assertEqual(handoff_protocol.terminal_session(str(root)), 'session-b')
                with environment(AGENTSROOM_AGENT_ID='agent-300-backend'):
                    self.assertEqual(handoff_protocol.terminal_session(str(root)), '')


class TerminalOverrideTests(unittest.TestCase):
    """Thresholds the Herdr plugin sets for one pane."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.project = self.home / 'project'
        self.project.mkdir()
        user = self.home / '.context-watch'
        user.mkdir()
        (user / 'thresholds.json').write_text('{"at": 150000, "warning_window_tokens": 40000}')
        self.herdr = dict(HOME=str(self.home), HERDR_ENV='1', HERDR_PANE_ID='w1:p1',
                          HERDR_SOCKET_PATH='/a/herdr.sock', HANDOFF_AT='', CONTEXT_WATCH_WARNING_WINDOW='')

    def override(self, **values):
        with environment(HOME=str(self.home)):
            path = Path(handoff_identity.terminal_settings_path(
                handoff_identity.herdr_terminal_id('w1:p1', '/a/herdr.sock')))
        self.assertTrue(path.is_relative_to(self.home))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values))

    def resolve(self, **extra):
        with environment(**dict(self.herdr, **extra)):
            limit = context_watch.resolve_threshold('claude-test', 200000, str(self.project))
            lead = watch_decision.warning_lead(context_watch.warning_window_candidates(str(self.project)))
        return limit, lead[:2]

    def test_pane_override_beats_config_and_env_beats_pane(self):
        self.assertEqual(self.resolve(), ((150000, 'config:at'), (40000, str(self.home / '.context-watch/thresholds.json'))))
        self.override(at=90000, warning_window_tokens=10000)
        self.assertEqual(self.resolve(), ((90000, 'terminal:at'), (10000, 'terminal override')))
        self.assertEqual(self.resolve(HERDR_PANE_ID='w1:p2')[0], (150000, 'config:at'))
        self.assertEqual(self.resolve(HANDOFF_AT='70000', CONTEXT_WATCH_WARNING_WINDOW='5000'),
                         ((70000, 'HANDOFF_AT'), (5000, 'env CONTEXT_WATCH_WARNING_WINDOW')))


if __name__ == '__main__':
    unittest.main()
