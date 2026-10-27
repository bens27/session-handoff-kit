"""v3-3 startup adoption (SPEC 7.3d): SessionStart reads the pointer index.

One eligible pointer adopts, several follow `resume_pointer_policy`, none is a
fast no-op. Identity comes from hook/host facts only, the body is delivered
once, and the manual agents_restart prompt still works afterwards.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_protocol as protocol
import restart_preflight as restart

BODY = '## Objective\nFix it\n## Current state\nSaved %s\n## Next steps\nTest it\n'


class StartupAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name).resolve())
        (Path(self.root) / '.handoffs').mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('CODEX_THREAD_ID', 'HANDOFF_', 'CONTEXT_WATCH_', 'AUTORESUME',
                                         'AGENTSROOM_'))}
        self.env.update(HOME=self.root, USERPROFILE=self.root, TMPDIR=self.root,
                        HANDOFF_TERMINAL_ID='term', CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0')
        patcher = patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, 'tempdir', None)
        self.prompts = {}

    # -- fixtures -----------------------------------------------------------
    def park(self, marker, request_id, session='old', terminal='term', topic='work', staged=False):
        # Preflight keeps one pending intent per terminal, so several same-terminal
        # candidates only arise from older ledgers or races; `staged` reproduces that.
        guard = patch.object(restart, 'intent_expired', lambda record: True) if staged else patch.dict({})
        with patch.dict(os.environ, dict(HANDOFF_TERMINAL_ID=terminal)), guard:
            path = protocol.save(self.root, session, 'save-' + request_id, {
                'topic': topic, 'description': 'Continue ' + marker, 'body': BODY % marker})['path']
            ready = restart.preflight(self.root, path, session, terminal, request_id,
                                      workers=[], host_capable=True)
        self.assertEqual(ready['outcome'], 'ready', ready)
        self.prompts[request_id] = ready['restart_prompt']
        return path

    def config(self, user=None, project=None):
        if user is not None:
            folder = Path(self.root) / '.context-watch'
            folder.mkdir(exist_ok=True)
            (folder / 'thresholds.json').write_text(json.dumps(user))
        if project is not None:
            (Path(self.root) / '.context-watch.json').write_text(json.dumps(project))

    def authorize(self, **user):
        self.config(user=dict(autoresume=True, **user))

    def start(self, session='new', cwd=None, source='startup', **env):
        trace = Path(self.root) / (session + '.jsonl')
        trace.touch()
        began = time.monotonic()
        run = subprocess.run([sys.executable, str(HOOKS / 'context_watch.py')],
                             input=json.dumps(dict(hook_event_name='SessionStart', source=source,
                                                   session_id=session, cwd=str(cwd or self.root),
                                                   transcript_path=str(trace))),
                             cwd=str(cwd or self.root), env=dict(self.env, **env),
                             capture_output=True, text=True, timeout=30)
        self.elapsed = time.monotonic() - began
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn('Traceback', run.stderr)
        if not run.stdout.strip():
            return ''
        return json.loads(run.stdout)['hookSpecificOutput']['additionalContext']

    def adopt_prompt(self, request_id, **env):
        handle = shlex.split(self.prompts[request_id])[-1]
        run = subprocess.run([sys.executable, str(HOOKS / 'restart_preflight.py'), 'adopt', handle],
                             env=dict(self.env, **env), cwd=self.root, text=True, capture_output=True,
                             timeout=30)
        return run.stdout, json.loads(run.stdout)

    def state(self, request_id):
        record = restart.status(self.root, request_id)
        return record['state'], record.get('successor_session')

    # -- one / none ---------------------------------------------------------
    def test_one_eligible_pointer_adopts_and_delivers_body_once(self):
        self.park('ONE-MARKER', 'req-1')
        self.authorize()
        context = self.start()
        self.assertEqual(context.count('ONE-MARKER'), 1, context)
        self.assertIn('"outcome": "resumed"', context)
        self.assertNotIn('restart_preflight.py adopt', context, 'no second generic resume command')
        self.assertEqual(self.state('req-1'), ('retrieved', 'new'))

    def test_no_pointer_is_a_fast_silent_noop(self):
        self.authorize()
        context = self.start()
        self.assertNotIn('startup-adoption', context)
        self.assertFalse((Path(self.root) / '.handoffs' / 'pointers').exists())
        self.assertLess(self.elapsed, 5)

    def test_settled_pointer_is_silent(self):
        self.park('SETTLED-MARKER', 'req-1')
        restart.record_refusal(self.root, 'req-1', 'user-declined')
        self.authorize()
        context = self.start()
        self.assertNotIn('SETTLED-MARKER', context)
        self.assertNotIn('startup-adoption', context)

    # -- once-only delivery -------------------------------------------------
    def test_second_start_and_later_restart_prompt_do_not_redeliver(self):
        self.park('ONCE-MARKER', 'req-1')
        self.authorize()
        self.assertEqual(self.start().count('ONCE-MARKER'), 1)
        self.assertNotIn('ONCE-MARKER', self.start(source='clear'))
        raw, again = self.adopt_prompt('req-1')
        self.assertEqual(again['outcome'], 'already-delivered', again)
        self.assertNotIn('ONCE-MARKER', raw)
        self.assertEqual(again['restart']['successor_session'], 'new')
        self.assertEqual(self.state('req-1'), ('retrieved', 'new'))

    def test_manual_restart_prompt_works_without_startup_authorization(self):
        self.park('MANUAL-MARKER', 'req-1')
        context = self.start()  # no autoresume: startup must not adopt
        self.assertNotIn('MANUAL-MARKER', context)
        self.assertEqual(self.state('req-1'), ('pending', None))
        raw, adopted = self.adopt_prompt('req-1')
        self.assertEqual(adopted['outcome'], 'resumed', adopted)
        self.assertEqual(raw.count('MANUAL-MARKER'), 1)
        self.assertEqual(self.state('req-1'), ('retrieved', 'new'))

    # -- identity -----------------------------------------------------------
    def test_foreign_terminal_pointer_is_never_a_candidate(self):
        self.park('FOREIGN-MARKER', 'req-1', terminal='elsewhere')
        self.authorize()
        context = self.start()
        self.assertNotIn('FOREIGN-MARKER', context)
        self.assertEqual(self.state('req-1'), ('pending', None))

    def test_predecessor_session_restarting_does_not_adopt_itself(self):
        self.park('SELF-MARKER', 'req-1')
        self.authorize()
        context = self.start(session='old')
        self.assertNotIn('SELF-MARKER', context)
        self.assertEqual(self.state('req-1'), ('pending', None))

    def test_without_terminal_identity_nothing_is_adopted(self):
        self.park('NOTERM-MARKER', 'req-1')
        self.authorize()
        context = self.start(HANDOFF_TERMINAL_ID='')
        self.assertNotIn('NOTERM-MARKER', context)
        self.assertEqual(self.state('req-1'), ('pending', None))

    def test_resume_and_fork_preserve_native_precedence(self):
        self.park('NATIVE-MARKER', 'req-1')
        self.authorize()
        for source in ('resume', 'fork'):
            self.assertNotIn('NATIVE-MARKER', self.start(source=source))
        self.assertEqual(self.state('req-1'), ('pending', None))

    def test_invalid_relevant_pointer_blocks_selection_with_diagnostic(self):
        self.park('VALID-MARKER', 'req-1')
        self.authorize()
        pointers = Path(self.root) / '.handoffs' / 'pointers'
        broken = pointers / ('0' * 64 + '.json')
        broken.write_text('{broken')
        os.chmod(broken, 0o600)
        context = self.start()
        self.assertNotIn('VALID-MARKER', context)
        self.assertIn('invalid-pointer', context)
        self.assertEqual(self.state('req-1'), ('pending', None))

    # -- several: selection policy ------------------------------------------
    def several(self):
        first = self.park('FIRST-MARKER', 'req-1', session='old-a', topic='alpha')
        time.sleep(0.02)
        second = self.park('SECOND-MARKER', 'req-2', session='old-b', topic='beta', staged=True)
        return first, second

    def test_several_default_ask_announces_without_adopting(self):
        self.several()
        self.authorize()
        context = self.start()
        self.assertNotIn('FIRST-MARKER', context)
        self.assertNotIn('SECOND-MARKER', context)
        self.assertIn('"policy": "ask"', context)
        self.assertEqual(context.count('restart_preflight.py adopt'), 2, context)
        self.assertEqual(self.state('req-1'), ('pending', None))
        self.assertEqual(self.state('req-2'), ('pending', None))

    def test_several_latest_owned_adopts_newest(self):
        self.several()
        self.authorize(resume_pointer_policy='latest-owned')
        context = self.start()
        self.assertEqual(context.count('SECOND-MARKER'), 1, context)
        self.assertNotIn('FIRST-MARKER', context)
        self.assertEqual(self.state('req-2'), ('retrieved', 'new'))
        self.assertEqual(self.state('req-1'), ('pending', None))

    def test_several_manual_announces_without_adopting(self):
        self.several()
        self.authorize(resume_pointer_policy='manual')
        context = self.start()
        self.assertIn('"policy": "manual"', context)
        self.assertNotIn('SECOND-MARKER', context)
        self.assertEqual(self.state('req-2'), ('pending', None))

    def test_project_policy_overrides_global_but_cannot_authorize(self):
        self.several()
        self.config(user=dict(autoresume=True, resume_pointer_policy='manual'),
                    project=dict(resume_pointer_policy='latest-owned'))
        context = self.start()
        self.assertIn('SECOND-MARKER', context)
        self.assertIn('"policy_source": "project"', context)

    def test_project_config_alone_never_authorizes_continuation(self):
        self.park('PROJECT-MARKER', 'req-1')
        self.config(project=dict(autoresume=True, resume_pointer_policy='latest-owned'))
        context = self.start()
        self.assertNotIn('PROJECT-MARKER', context)
        self.assertEqual(self.state('req-1'), ('pending', None))

    def test_malformed_policy_disables_automatic_selection(self):
        self.several()
        self.authorize(resume_pointer_policy='newest-please')
        context = self.start()
        self.assertIn('invalid-resume-pointer-policy', context)
        self.assertNotIn('SECOND-MARKER', context)
        self.assertEqual(self.state('req-2'), ('pending', None))

    def test_exact_host_request_selects_before_policy_and_never_falls_back(self):
        self.several()
        self.authorize(resume_pointer_policy='latest-owned')
        context = self.start(HANDOFF_RESTART_REQUEST_ID='req-1')
        self.assertIn('FIRST-MARKER', context)
        self.assertNotIn('SECOND-MARKER', context)
        self.assertEqual(self.state('req-1'), ('retrieved', 'new'))

    def test_exact_host_request_without_candidate_never_falls_back(self):
        self.several()
        self.authorize(resume_pointer_policy='latest-owned')
        context = self.start(HANDOFF_RESTART_REQUEST_ID='req-missing')
        self.assertIn('requested-intent-not-eligible', context)
        self.assertNotIn('SECOND-MARKER', context)
        self.assertEqual(self.state('req-2'), ('pending', None))


class PortskillStartupTests(unittest.TestCase):
    """ce8a995f #86: a session started in a scratch temp cwd must not adopt from there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.tmpdir = self.base / 'tmp'
        self.scratch = self.tmpdir / 'scratch'
        self.scratch.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('CODEX_THREAD_ID', 'HANDOFF_', 'CONTEXT_WATCH_', 'AUTORESUME',
                                         'AGENTSROOM_'))}
        self.env.update(HOME=str(self.base), TMPDIR=str(self.tmpdir), AGENTSROOM_AGENT_ID='terminal-a',
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', AUTORESUME='1')

    def test_startup_in_scratch_temp_cwd_is_a_noop_that_creates_nothing(self):
        run = subprocess.run([sys.executable, str(HOOKS / 'context_watch.py')],
                             input=json.dumps(dict(hook_event_name='SessionStart', source='startup',
                                                   session_id='new', cwd=str(self.scratch))),
                             cwd=str(self.scratch), env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn('startup-adoption', run.stdout)
        self.assertFalse((self.scratch / '.handoffs').exists())
        self.assertFalse((self.tmpdir / '.handoffs').exists())


if __name__ == '__main__':
    unittest.main()
