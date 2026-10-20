"""Terminal identity: which environment names a terminal, and how it is normalized."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))

import context_watch  # noqa: E402
import handoff_protocol  # noqa: E402

CLEARED = ('HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID', 'CODEX_THREAD_ID', 'HANDOFF_SESSION_ID')


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


if __name__ == '__main__':
    unittest.main()
