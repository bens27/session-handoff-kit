"""Identity module: pinned on-disk names, one recipe per path, state-dir injection.

The literal derivations below are the frozen contract (SPEC.md, "Temp state"):
a session in flight across an upgrade must find its latch and pointers again.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'skills', 'session-handoff', 'hooks')
sys.path.insert(0, HOOKS)

import context_watch  # noqa: E402
import handoff_dispatch  # noqa: E402
import handoff_identity as identity  # noqa: E402
import handoff_ledger  # noqa: E402
import handoff_protocol  # noqa: E402
import restart_preflight  # noqa: E402


def sha1(text, n):
    return hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:n]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.project = os.path.join(self.tmp, 'project')
        os.makedirs(os.path.join(self.project, '.handoffs'))
        self.state = os.path.join(self.tmp, 'state')
        os.makedirs(self.state)
        env = patch.dict(os.environ, {'HOME': self.tmp, 'HANDOFF_STATE_DIR': self.state,
                                      'HANDOFF_TERMINAL_ID': 'term-1', 'AGENTSROOM_AGENT_ID': '',
                                      'CODEX_THREAD_ID': '', 'HANDOFF_SESSION_ID': ''})
        env.start()
        self.addCleanup(env.stop)


class OneTerminalPointerRecipe(Case):
    """The hook writes the pointer and the protocol reads it: one recipe, not two."""

    def test_hook_writer_and_protocol_reader_agree(self):
        written = context_watch.terminal_pointer_path(self.project, 'term-1')
        self.assertEqual(written, identity.terminal_pointer_path(self.project, 'term-1'))
        with open(written, 'w') as f:
            json.dump(dict(session_id='sess-1', ts=time.time()), f)
        self.assertEqual(handoff_protocol.terminal_session(self.project), 'sess-1')

    def test_no_duplicate_recipe_left_in_callers(self):
        for module in (context_watch, handoff_protocol, handoff_ledger, handoff_dispatch, restart_preflight):
            with open(module.__file__.replace('.pyc', '.py'), encoding='utf-8') as f:
                source = f.read()
            if module is context_watch:
                # Sole exception: the missing-install notice marker, which by
                # definition cannot reach handoff_identity.
                start = source.index('def _claim_lone_notice(')
                end = source.index('\ndef ', start + 1)
                self.assertEqual(source.count('gettempdir'), source[start:end].count('gettempdir'))
                source = source[:start] + source[end:]
            with self.subTest(module=module.__name__):
                self.assertNotIn('gettempdir', source)
                self.assertNotIn('context-watch-terminal-', source)
                self.assertNotIn('context-watch-session-', source)
                self.assertNotIn('context-watch-project-', source)
                self.assertNotIn('context-watch-auto-', source)
                self.assertNotIn('.fired', source)
                self.assertNotIn("+ '.delivery'", source)
                self.assertNotIn("+ '.prepared'", source)

    def test_callers_reexport_the_module_functions(self):
        self.assertIs(handoff_ledger.project_root, identity.project_root)
        self.assertIs(handoff_ledger.session_note_path, identity.session_note_path)
        self.assertIs(handoff_ledger.read_project_pointer, identity.read_project_pointer)
        self.assertIs(handoff_protocol.terminal_id, identity.terminal_id)
        self.assertIs(context_watch.session_key, identity.session_key)
        self.assertIs(context_watch.latch_path, identity.latch_path)


class PinnedDerivations(Case):
    def test_session_note_and_sidecars(self):
        key = sha1(self.project + '\0sess', 20)
        note = os.path.join(self.state, 'context-watch-session-%s.json' % key)
        self.assertEqual(identity.session_note_path(self.project, 'sess'), note)
        self.assertEqual(identity.session_status_path(self.project, 'sess'), note + '.status')
        self.assertEqual(identity.session_routed_path(self.project, 'sess'), note + '.routed')
        self.assertEqual(identity.prepared_receipt_path(self.project, 'sess'), note + '.prepared')
        self.assertEqual(identity.delivery_receipt_path(self.project, 'sess'), note + '.prepared.delivery')
        unidentified = sha1(self.project + '\0unidentified', 20)
        self.assertTrue(identity.session_note_path(self.project).endswith(unidentified + '.json'))

    def test_publisher_lock_is_the_publisher_session_note(self):
        self.assertEqual(identity.publisher_lock_path(self.project),
                         os.path.join(self.state, 'context-watch-session-%s.json'
                                      % sha1(self.project + '\0publisher', 20)))

    def test_latches(self):
        self.assertEqual(identity.latch_path('a/b c'), os.path.join(self.state, 'context-watch-a_b_c.fired'))
        self.assertEqual(identity.latch_path('s', 2), os.path.join(self.state, 'context-watch-s.fired2'))
        self.assertEqual(identity.latch_floor_path('s'), os.path.join(self.state, 'context-watch-s.fired.floor'))
        self.assertEqual(identity.latch_claim_path('s', '.error'),
                         os.path.join(self.state, 'context-watch-s.fired.error'))

    def test_latch_key_falls_back_to_transcript_hash(self):
        self.assertEqual(identity.session_key({'session_id': ' s1 '}), 's1')
        self.assertEqual(identity.session_key({'transcript_path': '/x/t.jsonl'}), 't-' + sha1('/x/t.jsonl', 16))
        self.assertIsNone(identity.session_key({}))

    def test_auto_count_terminal_and_project_pointers(self):
        self.assertEqual(identity.auto_count_path(self.project),
                         os.path.join(self.state, 'context-watch-auto-%s.count' % sha1(self.project, 20)))
        self.assertEqual(identity.terminal_pointer_path(self.project, 'term-1'),
                         os.path.join(self.state, 'context-watch-terminal-%s.json' % sha1(self.project + '\0term-1', 20)))
        self.assertEqual(identity.project_pointer_path('sess'),
                         os.path.join(self.state, 'context-watch-project-%s.json' % sha1('sess', 20)))

    def test_paths_key_on_the_canonical_root_from_any_subdirectory(self):
        sub = os.path.join(self.project, 'src', 'deep')
        os.makedirs(sub)
        self.assertEqual(identity.session_note_path(sub, 's'), identity.session_note_path(self.project, 's'))
        self.assertEqual(identity.terminal_pointer_path(sub, 't'), identity.terminal_pointer_path(self.project, 't'))

    def test_long_terminal_id_keeps_prefix_and_digest(self):
        long = 'x' * 130
        with patch.dict(os.environ, HANDOFF_TERMINAL_ID=long):
            self.assertEqual(identity.terminal_id(),
                             'x' * 100 + '-' + hashlib.sha256(long.encode()).hexdigest()[:16])
        with patch.dict(os.environ, HANDOFF_TERMINAL_ID=' ', AGENTSROOM_AGENT_ID='agent-9'):
            self.assertEqual(identity.terminal_id(), 'agent-9')


class GoldenFilenames(Case):
    """Exact bytes, not the formula: a changed hash input or width breaks these."""
    D = "/state"
    ROOT = "/nonexistent/golden-root"

    def test_project_pointer(self):
        self.assertEqual(identity.project_pointer_path("sess-golden", self.D),
                         "/state/context-watch-project-10dc10fd3575a49a083c.json")

    def test_session_note(self):
        self.assertEqual(identity.session_note_path(self.ROOT, "sess-golden", self.D),
                         "/state/context-watch-session-2f0524095beba74dfa23.json")

    def test_auto_count_and_latch(self):
        self.assertEqual(identity.auto_count_path(self.ROOT, self.D),
                         "/state/context-watch-auto-9a14a76da5025f149466.count")
        self.assertEqual(identity.latch_path("a/b c", 2, self.D), "/state/context-watch-a_b_c.fired2")


class StateDirectory(Case):
    def test_explicit_parameter_wins_over_environment(self):
        other = os.path.join(self.tmp, 'other')
        self.assertEqual(os.path.dirname(identity.latch_path('s', directory=other)), other)
        self.assertEqual(os.path.dirname(identity.session_note_path(self.project, 's', directory=other)), other)

    def test_default_is_gettempdir_when_knob_unset_or_blank(self):
        for value in (None, '  '):
            with self.subTest(value=value):
                with patch.dict(os.environ):
                    os.environ.pop('HANDOFF_STATE_DIR', None)
                    if value is not None:
                        os.environ['HANDOFF_STATE_DIR'] = value
                    with patch.object(tempfile, 'tempdir', self.tmp):
                        self.assertEqual(identity.state_dir(), self.tmp)
                        self.assertEqual(os.path.dirname(identity.project_pointer_path('s')), self.tmp)


class StateDirectoryMustBeAbsolute(Case):
    def test_relative_knob_falls_back_to_gettempdir(self):
        for value in ("state", "./state", "../x"):
            with patch.dict(os.environ, {"HANDOFF_STATE_DIR": value}):
                self.assertEqual(identity.state_dir(), tempfile.gettempdir(), value)

    def test_absolute_knob_is_used(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"HANDOFF_STATE_DIR": d}):
            self.assertEqual(identity.state_dir(), d)


class SessionFallbackOrder(Case):
    def write_pointer(self, session, **extra):
        with open(identity.terminal_pointer_path(self.project, 'term-1'), 'w') as f:
            json.dump(dict(session_id=session, ts=time.time(), **extra), f)

    def test_pointer_then_host_then_environment(self):
        with patch.dict(os.environ, HANDOFF_SESSION_ID='env'):
            self.assertEqual(identity.default_session(self.project), 'env')
            with patch.dict(os.environ, CODEX_THREAD_ID='host'):
                self.assertEqual(identity.default_session(self.project), 'host')
                self.write_pointer('hook', host_session_id='host')
                self.assertEqual(identity.default_session(self.project), 'hook')

    def test_pointer_from_another_host_session_is_ignored(self):
        self.write_pointer('hook', host_session_id='old-host')
        with patch.dict(os.environ, CODEX_THREAD_ID='new-host'):
            self.assertEqual(identity.terminal_session(self.project), '')

    def test_child_environment_drops_session_ids_only(self):
        child = identity.without_session_identity(dict(CODEX_THREAD_ID='a', HANDOFF_SESSION_ID='b', KEEP='c'))
        self.assertEqual(child, dict(KEEP='c'))

    def test_runner_environment_owns_one_terminal_and_drops_agent_id(self):
        base = dict(CODEX_THREAD_ID='a', HANDOFF_SESSION_ID='b', AGENTSROOM_AGENT_ID='agent-x', KEEP='c')
        first, second = identity.runner_environment(base), identity.runner_environment(base)
        for child in (first, second):
            self.assertEqual(set(child), {'KEEP', 'HANDOFF_TERMINAL_ID'})
            self.assertRegex(child['HANDOFF_TERMINAL_ID'], r'^runner-[0-9a-f]{32}$')
        self.assertNotEqual(first['HANDOFF_TERMINAL_ID'], second['HANDOFF_TERMINAL_ID'])
        for blank in ('', '  '):
            self.assertTrue(identity.runner_environment(dict(HANDOFF_TERMINAL_ID=blank))['HANDOFF_TERMINAL_ID'].startswith('runner-'))
        inherited = [identity.runner_environment(dict(base, HANDOFF_TERMINAL_ID='tmux-x')) for _ in range(2)]
        for child in inherited:
            self.assertRegex(child['HANDOFF_TERMINAL_ID'], r'^runner-[0-9a-f]{32}$')
            self.assertNotIn('AGENTSROOM_AGENT_ID', child)
        self.assertNotEqual(inherited[0]['HANDOFF_TERMINAL_ID'], inherited[1]['HANDOFF_TERMINAL_ID'])


if __name__ == '__main__':
    unittest.main()
